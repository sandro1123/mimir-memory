# Task 6: Retention fail-closed proof (tombstone/ledger failure => no deletion)

**Files:**
- Modify:  ( per-job ordering)
- Modify:  (only if error propagation/atomicity requires)
- Create: 

**Interfaces:**
- Consumes: , , retention event/ledger writes ( insert +  status update).
- Produces: if tombstone or ledger/event write fails, no purge/content deletion and no  status for that job (exception propagates or job remains non-executed safely). Successful path unchanged; exempt/held/legal-hold branches preserved.

## Steps
1. Write failing fail-closed tests in :
- : schedule due fact retention, monkeypatch  to raise, call  expecting RuntimeError, assert fact content unchanged and retention job status != "executed".
- : schedule due message/source retention, monkeypatch ledger/event write path to raise, assert original content/row not purged and job not marked executed.
2. Run:  expect FAIL.
3. Ensure per-job ordering in : state transition (tombstone/event writes) succeeds and is durable before any destructive purge; if tombstone or ledger/event write fails, abort that job without purge or executed mark.
4. Run:  expect PASS.
5. Commit:  and 

## Context
- Workdir:  on n100 (branch feat/1.8.0). Python 3.12, pytest 9.0.3, ruff 0.15.13.
- Read  ( ~line 100 onward) and  ( ~1688) first.
- Retention jobs/status stored in retention tables;  updates retention_jobs and inserts memory_events; for fact jobs it calls  if status != tombstoned then marks executed. For message/source jobs it purges content and marks executed.
- Fail-closed: no purge/content deletion and no  if tombstone or ledger/event write fails. Exception propagating acceptable if state remains safe/non-executed/unpurged.
- Use CanonicalStore/RetentionService fixtures/helpers consistent with existing tests (no conftest). Inspect existing retention-related tests for patterns if needed.
- Report to:  (detailed). Final message <15 lines with status, commits, test summary, concerns, report path.
