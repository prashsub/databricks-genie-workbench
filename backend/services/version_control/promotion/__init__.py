"""Target-local cross-workspace promotion."""

from backend.services.version_control import contracts as vc
from .packages import build, encode, immutable_put


class PromotionService:
    def __init__(self, **ports):
        self.__dict__.update(ports)

    def package(self, version_id, mapping, policy):
        if self.flags.enabled('vc_promotion_enabled') is not True:
            raise PermissionError('Promotion writes disabled')
        version = self.ledger.get_version(self.source_binding, version_id)
        if version.version_id != version_id or version.context.binding != self.source_binding:
            raise ValueError('Immutable source binding/version mismatch')
        manifest, files = build(version, mapping, policy)
        prefix = f'{self.outbound_volume}/sha256/{manifest.package_digest}'
        for name, content in files.items():
            immutable_put(self.store, f'{prefix}/{name}', content)
        reference = vc.PackageRef(manifest.package_digest, f'{prefix}/manifest.json')
        immutable_put(self.store, reference.manifest_uri, encode(manifest))
        self.store.publish(reference)
        return reference
