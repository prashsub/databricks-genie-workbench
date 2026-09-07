# M03 TDD execution record

All pytest commands use `. .venv/bin/activate && python -m pytest`.

1. `backend/tests/test_vc_coordination.py -q`: RED missing coordination package; GREEN 1 collected case passed. Enrollment is an injected serialized authority, not an informational Delta key.
2. `backend/tests/test_vc_coordination.py -q -k test_two_concurrent_reservations_have_exactly_one_winner`: RED zero winners; GREEN full file 2 cases. Barrier race, durable loser fact, lying statement return rejected by read-back.
