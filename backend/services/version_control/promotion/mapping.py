"""Versioned, schema-aware mapping without modifying human prose."""

from dataclasses import dataclass
import re

from backend.services.version_control import contracts as vc


@dataclass(frozen=True)
class RenderedPayload:
    serialized_space: dict
    description: str | None
    environment: dict


TOKEN = re.compile(r"--[^\n]*(?:\n|$)|/\*[\s\S]*?\*/|'(?:''|[^'])*'|`(?:``|[^`])+`|[A-Za-z_][A-Za-z_0-9]*|\s+|.")
IDENTIFIER = re.compile(r'(?:`(?:``|[^`])+`|[A-Za-z_][A-Za-z_0-9]*)\Z')


ENVIRONMENT_KEYS = frozenset({'workspace_id', 'space_id', 'warehouse_id', 'sample_warehouse_id',
                              'parent_path', 'folder', 'permissions', 'principals'})


# Schema paths use [] for an array entry. Unknown identifier fields fail closed.
IDENTIFIER_PATHS = {
    ('data_sources', 'tables', '[]', 'identifier'): 'table',
    ('data_sources', 'metric_views', '[]', 'identifier'): 'metric_view',
    ('data_sources', 'catalogs', '[]', 'identifier'): 'catalog',
    ('data_sources', 'schemas', '[]', 'identifier'): 'schema',
    ('instructions', 'join_specs', '[]', 'left', 'identifier'): 'table',
    ('instructions', 'join_specs', '[]', 'right', 'identifier'): 'table',
    ('instructions', 'sql_functions', '[]', 'identifier'): 'function',
}


def structured_identifiers(value, path=()):
    """Yield mutable entries and resource kinds from one shared schema allowlist."""
    if isinstance(value, dict):
        for key, child in value.items():
            child_path = (*path, key)
            if key == 'identifier':
                if child_path not in IDENTIFIER_PATHS:
                    raise ValueError('Unrecognized structured identifier path: ' + '.'.join(child_path))
                if not isinstance(child, str) or not child:
                    raise ValueError('Invalid structured identifier')
                yield value, IDENTIFIER_PATHS[child_path]
            else:
                yield from structured_identifiers(child, child_path)
    elif isinstance(value, list):
        for child in value:
            yield from structured_identifiers(child, (*path, '[]'))


def map_sql(sql, mappings, *, fragment=False):
    override = mappings.get('sql:' + sql)
    if override is not None:
        return map_sql(override, {key: value for key, value in mappings.items() if not key.startswith('sql:')},
                       fragment=fragment)
    tokens = TOKEN.findall(sql)
    if any(token in {"'", '`'} for token in tokens):
        raise ValueError('Unterminated quoted SQL token')
    code = [token for token in tokens if not token.isspace() and not token.startswith(('--', '/*', "'"))]
    if (not code or (not fragment and code[0].upper() != 'SELECT')
            or any(token.upper() in {'IDENTIFIER', 'EXECUTE', 'IMMEDIATE', 'WITH', 'UNION', 'PIVOT',
                                     'LATERAL', 'TABLE', 'USE', 'INSERT', 'UPDATE', 'DELETE', 'DROP'} for token in code)
            or any(token in {';', '"', "'", '`', '$', '\\', '{', '}'} for token in code)
            or '/*' in ''.join(code) or code.count('(') != code.count(')')):
        raise ValueError('Unsupported SQL; supply a reviewed exact SQL override')
    output = []
    position = 0
    relation_pending = False
    relation_section = False
    identifier_mappings = {key: value for key, value in mappings.items()
                           if not key.startswith(('sql:', 'target:'))}
    source_catalogs = {key.split('.')[0] for key in identifier_mappings}
    target_identifiers = set(identifier_mappings.values())
    while position < len(tokens):
        token = tokens[position]
        if token.isspace() or token.startswith(('--', '/*', "'")):
            output.append(token)
            position += 1
            continue
        end = position + 1
        parts = [token]
        if IDENTIFIER.fullmatch(token):
            while end + 1 < len(tokens) and tokens[end] == '.' and IDENTIFIER.fullmatch(tokens[end + 1]):
                parts.append(tokens[end + 1])
                end += 2
        identifier = '.'.join(part.strip('`').replace('``', '`') for part in parts)
        if any('.' in part.strip('`') for part in parts):
            raise ValueError('Ambiguous quoted qualified identifier')
        mapped_prefix = next((source for source in sorted(identifier_mappings, key=len, reverse=True)
                              if identifier == source or identifier.startswith(source + '.')), None)
        target_prefix = next((target for target in target_identifiers
                              if identifier == target or identifier.startswith(target + '.')), None)
        if relation_pending:
            if len(parts) < 3 or (mapped_prefix is None and target_prefix is None):
                raise ValueError('Unresolved relation requires an exact reviewed mapping')
            relation_pending = False
        if (len(parts) > 1 and parts[0].strip('`') in source_catalogs
                and mapped_prefix is None and target_prefix is None):
            raise ValueError('Unresolved source identifier')
        if token.upper() in {'FROM', 'JOIN'}:
            relation_pending = True
            relation_section = True
        if token.upper() in {'WHERE', 'GROUP', 'ORDER', 'HAVING', 'LIMIT', 'ON'}:
            relation_section = False
        if token == ',' and relation_section:
            raise ValueError('Comma relations require reviewed explicit JOIN syntax')
        if len(parts) > 1 and mapped_prefix is not None:
            target = (identifier_mappings[mapped_prefix] + identifier[len(mapped_prefix):]).split('.')
            output.append('.'.join(f'`{part}`' for part in target) if any(part.startswith('`') for part in parts)
                          else '.'.join(target))
        else:
            output.append(''.join(tokens[position:end]))
        position = end
    if relation_pending:
        raise ValueError('Incomplete SQL relation')
    return ''.join(output)


def map_executable_fields(value, mappings, path=()):
    if isinstance(value, dict):
        for key, child in value.items():
            if key in ENVIRONMENT_KEYS:
                raise ValueError('Environment bindings cannot appear in portable content')
            if key in {'expression', 'sql_expression', 'query', 'function_name'}:
                raise ValueError('Unsupported executable field requires schema review')
            if key == 'sql' or (key == 'content' and value.get('format') == 'SQL'):
                join_fragment = path == ('instructions', 'join_specs', '[]')
                snippet_fragment = path in {
                    ('instructions', 'sql_snippets', kind, '[]')
                    for kind in ('filters', 'expressions', 'measures')}
                fragment_mode = join_fragment or snippet_fragment
                if isinstance(child, list):
                    rendered = []
                    for index, fragment_sql in enumerate(child):
                        if join_fragment and index > 0:
                            rendered.append(fragment_sql)
                        else:
                            rendered.append(map_sql(fragment_sql, mappings,
                                                    fragment=fragment_mode or index > 0))
                    # Validate statement-wide guards across clause-array boundaries too.
                    if not fragment_mode:
                        map_sql(''.join(rendered), mappings)
                    value[key] = rendered
                else:
                    value[key] = map_sql(child, mappings, fragment=fragment_mode)
            else:
                map_executable_fields(child, mappings, (*path, key))
    elif isinstance(value, list):
        for child in value:
            map_executable_fields(child, mappings, (*path, '[]'))


class MappingTransformer:
    version = 'vc-map/1'

    def render(self, artifact, mapping, target_binding):
        if mapping.target_binding != target_binding or target_binding.space_id is None:
            raise ValueError('Mapping requires exact pre-enrolled target binding')
        if mapping.transformer_version != self.version:
            raise ValueError('Unsupported mapping transformer version')
        content = vc.to_wire(artifact['serialized_space'])
        for entry, _kind in structured_identifiers(content):
            identifier = entry['identifier']
            if identifier not in mapping.mappings and identifier not in mapping.mappings.values():
                raise ValueError('Unresolved structured identifier')
            entry['identifier'] = mapping.mappings.get(identifier, identifier)
        map_executable_fields(content, mapping.mappings)
        environment = {key.removeprefix('target:'): value for key, value in mapping.mappings.items()
                       if key in {'target:warehouse_id', 'target:parent_path'}}
        environment['consumers'] = tuple(sorted(value for key, value in mapping.mappings.items()
                                                if key.startswith('target:consumer:')))
        return RenderedPayload(content, artifact.get('description'), environment)
