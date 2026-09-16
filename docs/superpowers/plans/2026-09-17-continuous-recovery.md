# Continuous Recovery Implementation Plan

> **For agentic workers:** Use superpowers:executing-plans or superpowers:subagent-driven-development task by task. Use test-driven-development and verification-before-completion. Preserve the existing dirty checkout; do not commit or clean it.

**Goal:** Deliver the first continuous-recovery batch approved in conversation.

**Architecture:** Keep ProfileRuntime, JobLedger, and SmsOrderStore authoritative.
Claim SMS work when it can execute, fence ordinary callbacks by owner, and make
periodic work an explicitly enabled launcher-supervised process.

**Tech Stack:** Python 3.9+, SQLite, unittest, Flask, Playwright, Selenium, GitHub Actions.

**Spec:** docs/superpowers/specs/2026-09-17-continuous-recovery-design.md

## Global Constraints

- Python 3.9+; no new distributed scheduler or database dependency.
- Playwright and Selenium reuse their registration engine/profile.
- Appium remains fail-closed; profile_path is not an identity API.
- Tests use temporary databases/profiles, localhost, and fake providers only.
- No passwords, proxy credentials, OTPs, API keys, cookies, or provider payloads in status, logs, task metadata, or notifications.
- Preserve the dirty user worktree; no checkout, reset, commit, push, or remote CI trigger. Do not stop the existing Web service.

### Task 1: SQLite Recovery Baseline and Required Smoke Gate

Files: create core/database_backup.py, tests/test_database_backup.py,
tools/run_browser_smoke.py, tests/test_browser_smoke_gate.py; modify
.github/workflows/ci.yml and docs/dependency-matrix.md; create
docs/database-recovery.md. Do not modify ledger, worker, or SMS code.

Interface: backup_database(source: Path, destination: Path) -> Path. Use a
read-only SQLite source, exclusive destination creation with mode 0600, backup
API, integrity check, and explicit failure cleanup. Never overwrite any file,
including symlinks; reject a missing source without creating it. The CLI must
not log database content. No automatic snapshot of production data this turn.

- [ ] Write tests exercising WAL committed content, existing/symlink
  destinations, missing sources, failure cleanup, and restrictive permissions.
  The decisive assertion is a restored SQL row, not a source-text match:
  `self.assertEqual(restored.execute('SELECT value FROM fixture').fetchone(), ('committed',))`.
- [ ] Run `venv/dev/bin/python -m unittest tests.test_database_backup -v` RED.
- [ ] Implement backup with `sqlite3.connect(source.resolve().as_uri() + '?mode=ro', uri=True)`
  and `source_connection.backup(destination_connection)`; exclusive creation
  and cleanup must surround both operations.
- [ ] Add a smoke runner checking unittest failures AND required Playwright /
  Selenium test execution. A synthetic skipped required test must return a
  nonzero gate result; normal executed successful tests return zero.
- [ ] Run both new focused test modules GREEN. CI runs the gate on push/PR
  using provisioned matching browser/driver binaries, not hard-coded Ubuntu
  Chromium paths; preserve manual dispatch support and use supported runners.
- [ ] Document migration-before-upgrade snapshots, rollback compatibility,
  confidential snapshot handling, and offline profile-backup limitations.
  Report RED/GREEN commands and results; no commit.

### Task 2: Strict Results and Durable Ownership

Files: core/job_ledger.py, core/retry_engine.py, web/worker.py, web/tasks.py;
tests/test_job_ledger.py and tests/test_operation_execution_invariants.py.

- [ ] Add RED tests for boolean-only health/warm results, string/numeric success
  markers, foreign-owner finalization, completed subject fact preservation,
  and legitimate verified supervisor cancellation.
- [ ] Remove boolean shortcuts for health/warm and require actual boolean
  success markers where present. Keep complete degraded health observations.
- [ ] Introduce a unique process execution identity, replacing PID-only retry
  ownership. Fence finalization atomically; separate explicit supervision from
  ordinary completion and do not downgrade ownership errors into compatibility.
- [ ] Run focused ledger/worker/service tests GREEN and review call sites.

### Task 3: Just-in-Time SMS Claims and Result Confirmation

Files: services/sms_manager.py, core/sms_orders.py, core/secret_safety.py,
tests/test_sms_compensation.py, tests/test_sms_orders.py.

- [ ] Persist the slow-batch regression: seven fake requests advance a clock
  ten seconds each; assert cancelled=7, pending=0, and each provider ID called
  once after another pass. Add lost-before-request, lost-during-request,
  slow/hung-provider, and cancellation regressions.
- [ ] Run RED against current batch claims and silent claim-loss handling.
- [ ] Select due candidates within the limit, claim immediately before each
  bounded request, and return a finite claim-loss error when an acknowledgement
  cannot be recorded. Preserve immutable terminal states and durable backoff.
- [ ] Run compensation/orders/manager tests GREEN and document the remaining
  external exactly-once limitation. Do not infer pass success from counter sums:
  claimed also counts newly expired intents.

### Task 4: Default-Disabled Supervised Periodic Compensation

Files: web/compensation_scheduler.py, web/server.py, web/configuration.py,
web/app.py; tests/test_compensation_scheduler.py and README.md.

- [ ] Add RED tests for disabled defaults, non-overlapping runtime ownership,
  one bounded pass, finite status, shutdown, and configuration isolation.
- [ ] Add a separate worker entry point with runtime flock/Windows locking,
  explicit enablement, interruptible interval waits, bounded passes, and safe
  atomic status. Launcher supervision owns start/stop; app factories do not.
- [ ] Expose read-only authenticated status using the existing administrator
  boundary. Keep explicit/manual and startup recovery semantics documented.
- [ ] Run focused scheduler/server/Web tests GREEN using fake providers only.

### Task 5: Final Verification

- [ ] Run full unittest discovery, explicit localhost browser smoke gate,
  compileall, node syntax, pip check, and git diff --check.
- [ ] Record implemented scope, skips/warnings, review findings, and remaining
  provider/manual-recovery/remote-CI limitations. Preserve uncommitted changes.
