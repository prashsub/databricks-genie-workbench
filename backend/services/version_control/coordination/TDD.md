# M03 TDD execution record

All pytest commands use `. .venv/bin/activate && python -m pytest`.

1. `backend/tests/test_vc_coordination.py -q`: RED missing coordination package; GREEN 1 collected case passed. Enrollment is an injected serialized authority, not an informational Delta key.
2. `backend/tests/test_vc_coordination.py -q -k test_two_concurrent_reservations_have_exactly_one_winner`: RED zero winners; GREEN full file 2 cases. Barrier race, durable loser fact, lying statement return rejected by read-back.
3. `backend/tests/test_vc_coordination.py -q -k test_same_sp_stale_attempt_cannot_renew_or_patch`: RED missing renew (3 parameters); GREEN full file 5 cases. Attempt, generation and revision fence shared-SP workers.
4. `backend/tests/test_vc_coordination.py -q -k test_reservation_is_not_admission_and_requires_committed_preimage`: RED missing admit; GREEN full file 7 cases, including typed provisional create intent.
