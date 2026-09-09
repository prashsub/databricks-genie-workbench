"""Execute reviewed owner migrations, never author DDL or Genie content."""

import re
from hashlib import sha256
from pathlib import Path
from uuid import UUID

from .bundles import preflight_deployment

# Owner migration bytes live in backend/version_control_ddl/ and are authored by
# the DDL owners (M02/M03/M06/M07). M08 invokes them; it never edits them.
_DDL_ROOT = Path(__file__).resolve().parents[3] / "version_control_ddl"
_STATEMENT = re.compile(
    r"CREATE\s+(TABLE|VOLUME)\s+IF\s+NOT\s+EXISTS\s+"
    r"\$\{catalog\}\.\$\{control_schema\}\.(\w+)",
    re.IGNORECASE,
)
_IDENTIFIER = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")


OWNER_SPECS = (
    ("M02", "table", "genie_space_versions"),
    ("M02", "table", "genie_space_registry"),
    ("M03", "table", "genie_ops_coordination"),
    ("M06", "table", "genie_space_operations"),
    ("M02", "volume", "vc_snapshots"),
    ("M06", "volume", "vc_approval_evidence"),
    ("M07", "volume", "vc_outbound_packages"),
    ("M07", "volume", "vc_target_receipts"),
)


def _split_statements(text):
    return [part.strip() for part in text.split(";") if part.strip()]


def _revision(statements):
    return "sha256:" + sha256("\n".join(statements).encode()).hexdigest()


def _classify(statement):
    match = _STATEMENT.match(statement)
    if match is None:
        return None
    return ("table" if match.group(1).upper() == "TABLE" else "volume"), match.group(2)


def build_owner_manifests(ddl_root=None):
    """Read the reviewed owner DDL files into provisioning manifests.

    Each `OWNER_SPECS` object is matched to its `CREATE ... IF NOT EXISTS`
    statement by name; the revision is the SHA-256 of the exact reviewed bytes.
    This authors no DDL — it only transcribes and hashes what the owners wrote.
    """

    root = Path(ddl_root) if ddl_root is not None else _DDL_ROOT
    objects = {}
    for path in sorted(root.glob("*.sql")):
        for statement in _split_statements(path.read_text()):
            classified = _classify(statement)
            if classified is None:
                raise ValueError(f"Unrecognized owner migration statement in {path.name}")
            kind, name = classified
            if name in objects:
                raise ValueError(f"Duplicate owner migration for {name}")
            objects[name] = (kind, statement)
    manifests = []
    for owner, kind, name in OWNER_SPECS:
        if name not in objects:
            raise ValueError(f"Missing owner migration for {name}")
        found_kind, statement = objects[name]
        if found_kind != kind:
            raise ValueError(f"Owner migration kind mismatch for {name}")
        manifests.append({"owner": owner, "kind": kind, "name": name,
                          "idempotent": True, "revision": _revision([statement]),
                          "statements": [statement]})
    return manifests


class OwnerMigrationRunner:
    """Execute reviewed owner migration bytes and grants against warehouse SQL.

    Runs as the provisioner identity. `execute` is a blocking callable that runs
    one rendered SQL statement. `${catalog}`/`${control_schema}` are the only
    permitted templates and are replaced with validated, backtick-quoted
    identifiers — never caller input.
    """

    def __init__(self, execute, catalog, control_schema, grants):
        if not (_IDENTIFIER.fullmatch(catalog) and _IDENTIFIER.fullmatch(control_schema)):
            raise ValueError("Catalog and control schema must be simple identifiers")
        self._execute = execute
        self._catalog = catalog
        self._control_schema = control_schema
        self._grants = tuple(grants)

    def _render(self, text):
        rendered = (text.replace("${catalog}", f"`{self._catalog}`")
                        .replace("${control_schema}", f"`{self._control_schema}`"))
        if "${" in rendered:
            raise ValueError("Unresolved template placeholder in owner migration")
        return rendered

    def validate_owner_spec(self, spec):
        statements = spec.get("statements")
        if not statements or spec.get("idempotent") is not True:
            return False
        if spec.get("revision") != _revision(statements):
            return False
        for statement in statements:
            residual = statement.replace("${catalog}", "").replace("${control_schema}", "")
            if "${" in residual:
                return False
            classified = _classify(statement)
            if classified is None:
                return False
            kind, name = classified
            if name != spec.get("name") or kind != spec.get("kind"):
                return False
        return True

    def apply_owner_spec(self, spec):
        for statement in spec["statements"]:
            self._execute(self._render(statement))

    def apply_grants(self):
        for grant in self._grants:
            self._execute(self._render(grant))


def provision(manifests, runner) -> None:
    specs = {}
    for manifest in manifests:
        key = tuple(manifest.get(field) for field in ("owner", "kind", "name"))
        if (key not in OWNER_SPECS or key in specs
                or manifest.get("idempotent") is not True
                or not re.fullmatch(r"sha256:[0-9a-f]{64}", manifest.get("revision", ""))):
            raise ValueError("Invalid, duplicate, or unreviewed owner spec")
        specs[key] = dict(manifest)
    if set(specs) != set(OWNER_SPECS):
        raise ValueError("All four owner tables and separate Volumes are required")
    for key in OWNER_SPECS:
        if runner.validate_owner_spec(specs[key]) is not True:
            raise ValueError("Unverified owner migration bytes or grants")
    for key in OWNER_SPECS:
        runner.apply_owner_spec(specs[key])
    runner.apply_grants()


def repeatable_sandbox_provision(root, selection, runner):
    if (not selection.get("profile") or selection["profile"].upper() == "DEFAULT"
            or selection.get("target") != "vc-sandbox"
            or not selection.get("package_target")
            or selection.get("disposable_nonproduction") is not True):
        raise ValueError("Explicit nonproduction profile and sandbox target required")
    operation_id = selection["operation_id"]
    if str(UUID(operation_id)) != operation_id:
        raise ValueError("Canonical provisioning operation_id required")
    root = Path(root).resolve()
    preflight_deployment(root)
    if runner.verify_selection(selection) is not True:
        raise PermissionError("Live provisioning identity/host/workspace not verified")
    profile_args = ["--profile", selection["profile"]]
    variables = [f"--var={key}={value}" for key, value in sorted(selection.get("variables", {}).items())]
    root_args = ["--target", selection["target"], *profile_args, *variables]
    before = runner.content_fingerprint()
    previous = None
    for cycle in range(2):
        runner.command(["databricks", "bundle", "validate", "--strict", *root_args], cwd=root)
        runner.command(["databricks", "bundle", "validate", "--strict", "--target", selection["package_target"],
                        *profile_args, *selection.get("package_variables", [])],
                       cwd=root / "packages/genie-space-optimizer")
        runner.command(["databricks", "bundle", "deploy", *root_args], cwd=root)
        runner.command(["databricks", "bundle", "run", "vc-provision", *root_args,
                        "--params", f"operation_id={operation_id}"], cwd=root)
        if runner.content_fingerprint() != before:
            raise PermissionError("Provisioning changed governed Genie content")
        current = runner.provisioning_fingerprint()
        if cycle and current != previous:
            raise PermissionError("Provisioning was not repeatable")
        previous = current
