"""Target-local promotion Job entrypoint."""

import argparse
from uuid import UUID


def run(operation_id, *, service, executor):
    if str(UUID(operation_id)) != operation_id:
        raise ValueError('Canonical operation_id required')
    return service.execute(operation_id, executor)


def main(argv=None):
    parser = argparse.ArgumentParser()
    parser.add_argument('--operation-id', required=True)
    args = parser.parse_args(argv)
    if str(UUID(args.operation_id)) != args.operation_id:
        raise ValueError('Canonical operation_id required')
    from backend.jobs import build_vc_runtime
    return build_vc_runtime('promotion').run('promotion', args.operation_id)


if __name__ == '__main__':
    main()
