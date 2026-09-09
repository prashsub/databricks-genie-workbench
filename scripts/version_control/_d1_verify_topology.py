"""D1: validate the two-workspace promotion topology live (steps 1 and 2 only).

Drives TwoWorkspacePromotion.verify_identities_and_securables() and
verify_negative_permissions() — the package-independent steps — against the
authored VC_TWO_WORKSPACE_CONFIG. Step 3 (promote/retry/reverse-receipt) needs
the package + deployed Job and is exercised in D2.
"""

import json
import os
from pathlib import Path

from backend.tests.integration.test_vc_two_workspace_promotion import (
    TwoWorkspacePromotion,
)


def main() -> None:
    config = json.loads(Path(os.environ["VC_TWO_WORKSPACE_CONFIG"]).read_text())
    platform = TwoWorkspacePromotion(config)
    platform.verify_identities_and_securables()
    print("OK verify_identities_and_securables")
    platform.verify_negative_permissions()
    print("OK verify_negative_permissions")


if __name__ == "__main__":
    main()
