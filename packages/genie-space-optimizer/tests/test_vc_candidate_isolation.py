from dataclasses import dataclass

import pytest

from genie_space_optimizer.integration.version_control import EvaluationBinding


@dataclass
class IsolationRegistry:
    managed: bool = False
    owner: str | None = "run"

    def resolve_physical(self, workspace_id, space_id):
        return object() if self.managed else None

    def optimizer_owner(self, workspace_id, space_id):
        return self.owner


@pytest.mark.parametrize("managed,owner", [(True, "run"), (False, None), (False, "other")])
def test_candidate_evaluation_cannot_target_managed_binding(managed, owner):
    resource = EvaluationBinding("workspace", "candidate", "run")
    with pytest.raises(PermissionError):
        resource.validate(IsolationRegistry(managed, owner))


def test_positive_optimizer_ownership_is_required():
    resource = EvaluationBinding("workspace", "candidate", "run")
    assert resource.validate(IsolationRegistry()) == resource
