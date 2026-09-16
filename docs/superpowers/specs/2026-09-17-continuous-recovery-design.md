# Continuous Recovery Design

Approved in conversation on 2026-09-17. This extends the operational reliability
design; it does not replace engine-authoritative profile identity.

## First Delivery

1. Consistent SQLite snapshots and migration/rollback documentation, plus local
   CI gates that cannot silently skip required real-browser smoke.
2. Strict operation results and owner-fenced ledger finalization, with an
   explicit supervisor path for verified cancellation/recovery.
3. Just-in-time SMS compensation claims, bounded provider requests, and explicit
   claim-loss outcomes. Slow batches must not lose unstarted claims or report
   unrecorded provider acknowledgements as success.
4. A default-disabled supervised compensation worker, with safe status,
   bounded passes, shutdown, and isolated runtime configuration.

## Constraints

- Python 3.9+; no new distributed scheduler or database dependency.
- Playwright and Selenium reuse their registration engine/profile.
- Appium remains fail-closed; profile_path is not an identity API.
- Tests use temporary databases/profiles, localhost, and fake providers only.
- No passwords, proxy credentials, OTPs, API keys, cookies, or provider payloads
  in status, logs, task metadata, or notifications.
- Preserve the dirty user worktree; no checkout, reset, commit, push, or remote
  CI trigger. Do not stop the existing Web service.
- A local claim cannot ensure external exactly-once execution. Lost ownership
  and ambiguous provider timeouts must remain visible and cannot invent a
  cancelled/completed provider state.

## Ownership

Normal callbacks require current ownership. Supervisor finalization is an
explicit internal operation after resource/process reconciliation, not a
fallback on any ownership error. Completed account attempts stay immutable
when a containing multi-account job fails or is cancelled. Complete degraded
health observations are account facts, not automatic scheduler failures.

## Recovery and Scheduling

Snapshots use SQLite's backup API, restrictive permissions, read-only sources,
and refuse existing destinations. Database snapshots are not complete profile
backups; active browser profiles must not be copied as consistent backups.

Periodic execution is enabled explicitly by runtime configuration. Existing
explicit/manual compensation and startup recovery retain their separately
documented semantics. Application factories do not start the periodic worker.
The launcher owns its lifecycle and the worker uses a separate runtime lock.
Each pass has a bounded order count and time budget, respects durable backoff,
and publishes finite counters/status without provider responses.

## Acceptance

- Seven fake requests advancing a simulated clock ten seconds each complete
  once each without expiry of unstarted claims or duplicate provider calls.
- Lost claims never overwrite a new owner and never create a successful pass
  for an unrecorded acknowledgement.
- Boolean-only health/warm results and non-boolean success markers fail closed.
- A foreign owner cannot finalize running/pending attempts; legitimate verified
  supervision can reconcile abandoned jobs.
- Snapshots restore committed database records, including when WAL is active,
  without modifying the source or overwriting an existing destination.
- Default startup creates no periodic provider work; explicit enablement has
  one supervised runtime owner, safe status, and bounded shutdown.
- Full unittest, compile, dependency, JavaScript, whitespace, and localhost
  browser checks run before reporting completion; remote CI remains unverified.
