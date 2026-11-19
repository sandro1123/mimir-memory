# Task 6 Report: Retention fail-closed proof

Workdir: ~/mimir-1.8.0-dev on n100, branch feat/1.8.0.

## Steps performed
1. Read brief,  (execute_due ordering) and  (, transaction/event behavior).
2. Created :
   - : due fact job, monkeypatch  to raise; asserts  raises and fact content/status/version unchanged and job status != executed.
   - : due message job, monkeypatch retention transaction to raise on  for ; asserts content not purged and job status != executed.

## Findings
- No product changes needed in  or .
- Fact path:  runs before the purge/mark-executed transaction, so tombstone failure propagates with no purge and no executed mark.
- Message/source/candidate purge path: purge,  event insert, and job status update occur in the same ; ledger/event failure rolls back, leaving content unpurged and job non-executed.
- Exempt/held/legal-hold branches unchanged.

## Tests
-  ->  (warnings:  deprecation from test helper).
- No true RED on product: after fixing test fixture schema values (owner/domain/fact_type), tests passed against existing implementation. No fix commit for product code required.

## Concerns
- Tests depend on schema registries/valid enum values and monkeypatch internals (transaction/event insert shape).
- Only targeted fail-closed tests run; broader retention suite not executed.

## Commit
-   (adds ).
