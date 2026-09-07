"""Versioned, schema-aware mapping without modifying human prose."""

from dataclasses import dataclass

from backend.services.version_control import contracts as vc


@dataclass(frozen=True)
class RenderedPayload:
    serialized_space: dict
    description: str | None


class MappingTransformer:
    version = 'vc-map/1'

    def render(self, artifact, mapping, target_binding):
        content = vc.to_wire(artifact['serialized_space'])
        sources = content.get('data_sources', {})
        for kind in ('tables', 'metric_views', 'catalogs', 'schemas'):
            for entry in sources.get(kind, []):
                entry['identifier'] = mapping.mappings.get(entry['identifier'], entry['identifier'])
        return RenderedPayload(content, artifact.get('description'))
