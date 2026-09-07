"""Versioned, schema-aware mapping without modifying human prose."""

from dataclasses import dataclass
import re

from backend.services.version_control import contracts as vc


@dataclass(frozen=True)
class RenderedPayload:
    serialized_space: dict
    description: str | None


TOKEN = re.compile(r"--[^\n]*(?:\n|$)|/\*[\s\S]*?\*/|'(?:''|[^'])*'|`(?:``|[^`])+`|[A-Za-z_][A-Za-z_0-9]*|\s+|.")
IDENTIFIER = re.compile(r'(?:`(?:``|[^`])+`|[A-Za-z_][A-Za-z_0-9]*)\Z')


def map_sql(sql, mappings):
    tokens = TOKEN.findall(sql)
    output = []
    position = 0
    while position < len(tokens):
        token = tokens[position]
        end = position + 1
        parts = [token]
        if IDENTIFIER.fullmatch(token):
            while end + 1 < len(tokens) and tokens[end] == '.' and IDENTIFIER.fullmatch(tokens[end + 1]):
                parts.append(tokens[end + 1])
                end += 2
        identifier = '.'.join(part.strip('`').replace('``', '`') for part in parts)
        if identifier in mappings:
            target = mappings[identifier].split('.')
            output.append('.'.join(f'`{part}`' for part in target) if any(part.startswith('`') for part in parts)
                          else '.'.join(target))
        else:
            output.append(''.join(tokens[position:end]))
        position = end
    return ''.join(output)


def map_executable_fields(value, mappings):
    if isinstance(value, dict):
        for key, child in value.items():
            if key == 'sql' or (key == 'content' and value.get('format') == 'SQL'):
                fragments = isinstance(child, list)
                rendered = map_sql(''.join(child) if fragments else child, mappings)
                value[key] = [rendered] if fragments else rendered
            else:
                map_executable_fields(child, mappings)
    elif isinstance(value, list):
        for child in value:
            map_executable_fields(child, mappings)


class MappingTransformer:
    version = 'vc-map/1'

    def render(self, artifact, mapping, target_binding):
        content = vc.to_wire(artifact['serialized_space'])
        sources = content.get('data_sources', {})
        for kind in ('tables', 'metric_views', 'catalogs', 'schemas'):
            for entry in sources.get(kind, []):
                entry['identifier'] = mapping.mappings.get(entry['identifier'], entry['identifier'])
        map_executable_fields(content, mapping.mappings)
        return RenderedPayload(content, artifact.get('description'))
