"""Restore Job entrypoint: caller supplies only an immutable operation ID."""

import argparse
from uuid import UUID


def run(operation_id, *, service, executor):
    if str(UUID(operation_id)) != operation_id:
        raise ValueError("Canonical operation_id required")
    return service.run(operation_id, executor)


def main(argv=None):
    parser = argparse.ArgumentParser()
    parser.add_argument("--operation-id", required=True)
    args = parser.parse_args(argv)
    from backend.jobs import build_vc_runtime
    return build_vc_runtime("restore").run("restore", args.operation_id)


if __name__ == "__main__":
    main()
