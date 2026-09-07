# M03 TDD execution record

All pytest commands use `. .venv/bin/activate && python -m pytest`.

1. `backend/tests/test_vc_coordination.py -q`: RED missing coordination package; GREEN 1 collected case passed. Enrollment is an injected serialized authority, not an informational Delta key.
2. `backend/tests/test_vc_coordination.py -q -k test_two_concurrent_reservations_have_exactly_one_winner`: RED zero winners; GREEN full file 2 cases. Barrier race, durable loser fact, lying statement return rejected by read-back.
3. `backend/tests/test_vc_coordination.py -q -k test_same_sp_stale_attempt_cannot_renew_or_patch`: RED missing renew (3 parameters); GREEN full file 5 cases. Attempt, generation and revision fence shared-SP workers.
4. `backend/tests/test_vc_coordination.py -q -k test_reservation_is_not_admission_and_requires_committed_preimage`: RED missing admit; GREEN full file 7 cases, including typed provisional create intent.
5. `backend/tests/test_vc_coordination.py -q -k test_admission_atomically_binds_key_digest_approval_and_preimage`: RED revoked grant admitted; GREEN full file 8 cases. All authorizing columns in one CAS, expired/mismatched grants and forged claims rejected.
6. `backend/tests/test_vc_coordination.py -q -k test_same_key_different_digest_rejected_after_row_reuse`: RED missing finish; GREEN full file 9 cases. Append/read-back receipt before reuse; reconstructed service rejects changed digest using durable facts.
7. `backend/tests/test_vc_coordination.py -q -k test_duplicate_completed_request_returns_receipt_without_admission`: RED missing ExistingReceipt; GREEN full file 10 cases. Frozen reserve return type resolved with typed ExistingReceipt carrying original durable receipt (not a new Reservation/AdmissionClaim).
8. `backend/tests/test_vc_coordination.py -q -k test_consumed_approval_survives_row_reuse_and_crash`: RED consumption never published (3 cases); GREEN full file 13 cases. Admission publishes/flush-verifies consumption before returning; failures quarantine retained claim; finish re-verifies before release.
9. `backend/tests/test_vc_recovery.py -q -k test_expired_lease_quarantines_without_takeover`: RED expiry left admitted row (3 cases); GREEN both unit files 16 cases. Reserve/renew/assert expiry quarantines retained attempt and emits durable fact, never takes over.
10. `backend/tests/test_vc_recovery.py -q -k test_matching_get_after_timeout_cannot_resolve_attempt`: RED missing recover; GREEN both files 17 cases. Matching GET cannot replace trusted termination or finish a quarantined claim.
