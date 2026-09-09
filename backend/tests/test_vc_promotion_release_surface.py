"""Offline composition test for the operator release surface (D2.5b).

`build_release_surface` must assemble the *same* live governed leaf graph as
`resolve_governed_ports` and bind a `ReleaseCommands` over the real
`PromotionService` with `facts.record_request` as the durable request writer.
Verified with the fake adapters so the live driver exercises credentials +
the real approval workflow, never the composition.
"""

from backend.services.version_control.governance.facts import DurableOperationFacts
from backend.services.version_control.promotion import PromotionService
from backend.services.version_control.promotion.releases import ReleaseCommands
from backend.tests.test_vc_live_seams import FakeAdapters, _config
from scripts.version_control.promotion_release import (
    approve_record,
    build_release_surface,
)


def test_release_surface_binds_real_commands_over_the_live_graph():
    surface = build_release_surface(_config(), adapters=FakeAdapters())
    assert isinstance(surface.commands, ReleaseCommands)
    assert isinstance(surface.promotion, PromotionService)
    assert isinstance(surface.facts, DurableOperationFacts)
    # The command surface writes durable requests through the real facts store,
    # and drives the same promotion service the Job later executes.
    assert surface.commands.request_writer == surface.facts.record_request
    assert surface.commands.promotion is surface.promotion
    # Leaves are shared instances (single graph), not rebuilt per consumer.
    assert surface.promotion.facts is surface.facts
    assert surface.promotion.approvals is surface.approvals


def test_approve_record_is_approved_with_one_vote_per_approver():
    import backend.services.version_control.contracts as vc

    inputs = vc.ApprovalInputs(
        'VC/1.0', '00000000-0000-4000-8000-000000000004', 'promotion',
        '00000000-0000-4000-8000-000000000005', 'a' * 64,
        vc.Fingerprints('b' * 64, 'c' * 64, 'd' * 64, 'vc-c14n/1'),
        'e' * 64, 'f' * 64, '1' * 64, 'vc-map/1', 'vc-c14n/1',
        vc.BindingRef('00000000-0000-4000-8000-0000000000bd', 1, 'k', 'target', 'space', 'prod'),
        vc.Fingerprints('2' * 64, '3' * 64, '4' * 64, 'vc-c14n/1'),
        '5' * 64, '6' * 64, '7' * 64, '8' * 64, {}, 'requester', {},
        __import__('datetime').datetime.now(__import__('datetime').UTC))
    record = approve_record(inputs, ('human-1', 'human-2'))
    assert record.status == vc.FactStatus.APPROVED
    assert {vote.approver_id for vote in record.votes} == {'human-1', 'human-2'}
    assert len(record.approval_digest) == 64
