"""Verified native read-only transport selection."""

from backend.services.version_control.platform.capabilities import topology_read_ready


def separate_volumes(*paths):
    identities = []
    for path in paths:
        parts = path.split('/')
        if len(parts) != 5 or parts[:2] != ['', 'Volumes'] or any(not part or part in {'.', '..'} for part in parts[2:]):
            raise ValueError('Separate Volume securables required, not prefixes within a Volume')
        identities.append(tuple(part.lower() for part in parts[2:]))
    if len(set(identities)) != len(paths):
        raise ValueError('Package, approval and receipt writers require distinct Volumes')


class NativeTransport:
    def __init__(self, probe, source_workspace, target_workspace):
        self.probe = probe
        self.source_workspace = source_workspace
        self.target_workspace = target_workspace

    def verify(self):
        proof = self.probe()
        if (not topology_read_ready(proof) or proof['source_workspace'] != self.source_workspace
                or proof['target_workspace'] != self.target_workspace):
            raise PermissionError('Native read-only transport unsupported or unverified; topology disabled')
        return 'shared_uc' if proof['source_metastore'] == proof['target_metastore'] else proof['artifact_representation']
