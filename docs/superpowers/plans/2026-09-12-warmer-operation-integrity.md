# Warmer Operation Integrity Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use
> `superpowers:test-driven-development` task by task and
> `superpowers:verification-before-completion` before claiming completion.

**Goal:** Make warm success, adapter cleanup, and cancellation supervision
truthful and observable while preserving the engine-authoritative profile
kernel.

**Architecture:** `core/account_warmer.py` owns one mutable operation report
inside the profile lease. Browser adapters expose explicit close outcomes.
`web/tasks.py` verifies process-group termination before final cancellation.
Profile locks remain OS-level locks; tests prove they are reacquirable after an
interrupted worker exits.

**Spec:**
`docs/superpowers/specs/2026-09-12-operational-reliability-design.md`

### Task 1: Truthful activity outcome

**Files:**

- Create: `tests/test_warmer_operation_integrity.py`
- Modify: `core/account_warmer.py`

- [ ] Write failing Playwright and Selenium tests where authentication succeeds
  but every activity navigation fails.
- [ ] Assert `activity_failed`, nonzero attempts, zero successes, and a redacted
  last error.
- [ ] Add one mixed-outcome test and require at least one successful activity
  for `success=true`.
- [ ] Implement a bounded activity loop that cannot busy-spin on immediate
  failures and returns stable counters for both engines.
- [ ] Run the focused test module and existing warmer manifest tests.

### Task 2: Cleanup is part of the result

**Files:**

- Modify: `tests/test_warmer_operation_integrity.py`
- Modify: `core/account_warmer.py`
- Modify: `core/stealth_browser.py`

- [ ] Write failing tests for Playwright `close()` and Selenium `quit()` errors.
- [ ] Change adapter close to attempt every owned resource and return/raise an
  aggregate failure after best-effort cleanup.
- [ ] Finalize the warm result only after cleanup, returning `cleanup_failed`,
  `cleanup_status=failed`, and `browser_process_stopped=false` when shutdown is
  not confirmed.
- [ ] Keep the profile lease held for the complete cleanup attempt.
- [ ] Run adapter, profile persistence, and warmer tests.

### Task 3: Cancellation process and lease verification

**Files:**

- Modify: `tests/test_web_process.py`
- Modify: `web/tasks.py`

- [ ] Add a real local worker fixture that acquires a `ProfileLease`, starts a
  resistant descendant, and is cancelled by `TaskManager`.
- [ ] Assert the process group no longer exists and a new process can acquire
  the same profile lease after cancellation.
- [ ] Make `_kill_later` return verified process-group state and refuse to store
  `cancelled` when cleanup cannot be confirmed; record `cleanup_failed` instead.
- [ ] Cover graceful exit, forced kill, inaccessible process group, and repeated
  cancellation without contacting an external service.
- [ ] Run all process lifecycle tests.

### Task 4: Registration outcome separation

**Files:**

- Modify: `tests/test_profile_registration_lifecycle.py`
- Modify: `core/runners.py`
- Modify: `core/selenium_runner.py`

- [ ] Add failing tests proving a committed account remains a registration
  success while post-registration warm failure is retained separately.
- [ ] Return/persist a structured `registration_result` and `warm_result`
  projection without logging credentials or proxy secrets.
- [ ] Ensure lease handoff still closes registration browser before reacquiring
  the profile for warming.
- [ ] Run both registration lifecycle suites.

### Task 5: Verification

- [ ] Run `python -m unittest tests.test_warmer_operation_integrity -v`.
- [ ] Run warmer, profile runtime, registration lifecycle, and Web process tests.
- [ ] Run the full unit-test suite.
- [ ] Run `python -m compileall -q core web tests`.
- [ ] Run `node --check web/static/app.js` and `git diff --check`.
- [ ] Confirm the existing dev server PID is still running.
