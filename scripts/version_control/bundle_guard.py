"""Sanctioned deploy guard; emit read-only evidence, never upload Genie content."""

import argparse
from dataclasses import asdict
import json
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from backend.services.version_control.platform import bundle_detection_evidence


def main(argv=None):
    parser = argparse.ArgumentParser()
    parser.add_argument("bundles", nargs="+", type=Path)
    args = parser.parse_args(argv)
    evidence = [bundle_detection_evidence(path) for path in args.bundles]
    print(json.dumps([asdict(item) for item in evidence], sort_keys=True))
    return 0 if all(item.status == "clear" for item in evidence) else 1


if __name__ == "__main__":
    raise SystemExit(main())
