"""Composition boundary for module-owned native Job handlers."""


def build_vc_runtime(kind):
    raise PermissionError(f"VC {kind} runtime not integrated; writes remain disabled")
