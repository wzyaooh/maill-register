# Browser Profile Kernel Consistency Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make registration, warming, and health checks share a durable, engine-specific browser profile kernel with stable identity, exclusive leases, explicit lifecycle, and a two-channel health contract.

**Architecture:** Add `core/profile_runtime.py` as the source of truth for profile ids, manifests, lifecycle, proxy binding, lease ownership, browser observations, and health-status derivation. Existing Playwright and Selenium modules become adapters that consume resolved manifests; worker/API/UI code dispatches by the recorded engine and persists both browser and mailbox health facts.

**Tech Stack:** Python 3.9+, SQLite, standard-library `fcntl`/`msvcrt` file locking, Playwright async API, Selenium WebDriver/CDP, Flask, unittest/pytest.

**Spec:** `docs/superpowers/specs/2026-09-11-browser-profile-kernel-design.md`

## Global Constraints

- Both Playwright and Selenium remain supported; a profile may only be opened by the engine recorded at registration.
- The manifest is the source of truth for browser identity; no health/warm identity value may be generated from unseeded `Date.now()` or `Math.random()`.
- Every profile operation acquires the same cross-process lease before launching a browser; `profile_busy` is never relabeled as `locked` and never triggers a cross-engine or direct-network fallback.
- Manifest writes are atomic, mode `0600`; profile directories are contained under the selected runtime data root and mode `0700`.
- Proxy passwords, account passwords, cookies, and raw storage state never enter the manifest or ordinary account-list API responses.
- Health returns `browser_status`, `mailbox_status`, and a derived compatibility `status`; one channel must not overwrite the other.
- Old rows without a manifest are explicitly `legacy_unbound`/`identity_reconstructed`; migration never pretends to restore unknown identity.
- Existing profile persistence edits in the worktree must be preserved and integrated, not reverted.

### Task 1: Profile runtime primitives

**Files:**
- Create: `core/profile_runtime.py`
- Create: `tests/test_profile_runtime.py`

**Interfaces:**
- `ProfileRuntime(root: str | Path)` resolves profiles below `root / "data" / "profiles"`.
- `ProfileRuntime.provision(email, engine, proxy=None, identity=None) -> ProfileHandle` creates a random `profile_id`, an atomic manifest in `provisioning`, and a contained profile directory.
- `ProfileRuntime.resolve(profile_id=None, profile_path=None, expected_email=None, expected_engine=None) -> ProfileHandle` validates path, manifest, binding, and engine; raises `ProfileConflictError`, `ProfileUnavailableError`, or `ProfileRuntimeMismatchError` with stable `.code` values.
- `ProfileRuntime.load(handle) -> dict`, `bind(handle, email, browser=None)`, `mark_orphaned(handle, reason)`, `mark_corrupt(handle, reason)`, and `reconcile() -> list[dict]` implement lifecycle transitions.
- `ProfileRuntime.lease(handle, operation, timeout=0) -> ProfileLease`; `ProfileLease` supports `with`, `acquire`, `release`, and records only pid/operation/engine/timestamps in the lock file.
- `build_identity(profile_id, engine, mobile=False) -> dict` returns deterministic native context values; `proxy_binding(proxy) -> dict` returns a non-secret endpoint hash.
- `classify_browser_observation(observation, expected_email=None) -> str` and `derive_overall_status(browser_status, mailbox_status) -> str` are pure functions with the statuses in the spec.

- [ ] **Step 1: Write failing tests** for path containment, random stable ids, manifest atomicity/permissions, deterministic identities, proxy hash redaction, lifecycle transitions, corrupt manifests, and two-process lease exclusion.
- [ ] **Step 2: Run the focused tests** with `pytest tests/test_profile_runtime.py -q`; confirm failures are due to missing runtime primitives.
- [ ] **Step 3: Implement the minimal runtime module** using dataclasses, JSON validation, `os.replace`, `fsync`, and POSIX/Windows nonblocking locks. Reject symlinks and paths outside the runtime root.
- [ ] **Step 4: Run the focused tests** again and verify all runtime tests pass, including a subprocess lease test.
- [ ] **Step 5: Run `python3 -m compileall -q core tests`** and record the result.

### Task 2: Database binding and health snapshot storage

**Files:**
- Modify: `core/database.py`
- Modify: `core/account_manager.py`
- Create: `tests/test_profile_database_contract.py`

**Interfaces:**
- `DatabaseManager.save_account(...)` accepts `profile_id`, `engine`, `profile_state`, and `identity_state` while retaining `profile_path` compatibility.
- `DatabaseManager.update_health_snapshot(email, snapshot) -> bool` atomically updates browser/mailbox/overall statuses, timestamps, and `last_error_code`.
- `DatabaseManager.get_account_by_profile_id(profile_id) -> dict | None` enforces one account per profile.
- Existing JSON migration carries `profile_id`, `engine`, and health metadata when present and otherwise marks the row `legacy_unbound`.

- [ ] **Step 1: Add failing tests** for schema migration on an old database, profile uniqueness, account/manifest conflict fields, atomic health snapshot round-trip, and legacy JSON marking.
- [ ] **Step 2: Run `pytest tests/test_profile_database_contract.py -q`** and verify the expected missing-column/API failures.
- [ ] **Step 3: Add additive columns and methods** with defaults that preserve existing account rows and update existing `profile_path` behavior.
- [ ] **Step 4: Run the focused tests** and then the existing database/profile tests.
- [ ] **Step 5: Run `git diff --check`** for this task.

### Task 3: Stable identity and browser adapter launch contracts

**Files:**
- Modify: `core/stealth_browser.py`
- Modify: `core/selenium_runner.py`
- Create: `tests/test_browser_adapter_identity.py`

**Interfaces:**
- `PlaywrightStealthManager.initialize(..., profile_path=None, profile_manifest=None, lease_owned=False, purpose="registration")` consumes manifest identity when supplied and does not regenerate it.
- `create_driver(proxy=None, profile_path=None, profile_manifest=None, lease_owned=False)` consumes stored UA/window/locale/timezone/geolocation and rejects a bound proxy mismatch.
- Both adapters expose actual browser channel/version through `get_runtime_info()` or an equivalent returned record so the kernel can update the manifest.
- Health/warm adapter paths skip registration-only warmup/cookie mutation and optional fingerprint scripts unless explicitly requested by the caller.

- [ ] **Step 1: Write failing tests** asserting repeated launches receive identical native options, manifest values override random defaults, actual browser version is recorded, and no unseeded identity generation occurs on probe paths.
- [ ] **Step 2: Run the focused tests** and confirm current random/default behavior fails them.
- [ ] **Step 3: Refactor launch configuration** to use the runtime's manifest identity, add native CDP timezone/geolocation where supported, and separate registration behavior from probe/warm behavior. Do not add new evasion scripts.
- [ ] **Step 4: Run the focused tests** with fake Playwright/Selenium objects and verify both adapters consume the same manifest.
- [ ] **Step 5: Run existing `tests/test_profile_persistence.py`** to ensure prior behavior remains covered.

### Task 4: Registration profile lifecycle integration

**Files:**
- Modify: `core/runners.py`
- Modify: `core/selenium_runner.py`
- Modify: `core/creation_flow.py`
- Modify: `core/batch_runner.py`
- Create: `tests/test_profile_registration_lifecycle.py`

**Interfaces:**
- Registration creates a `ProfileHandle` before browser launch, passes its manifest to the selected adapter, records actual runtime info, binds the verified email, and marks the profile `ready` only after the account row is committed.
- Registration failure/cancellation marks the unbound profile `orphaned`; retries always get a fresh `profile_id`.
- Automatic post-registration warming reacquires the same handle/lease after the registration browser closes.
- Account rows persist `engine`, `profile_id`, derived `profile_path`, and `profile_state`.

- [ ] **Step 1: Write failing lifecycle tests** for Playwright and Selenium success, failure, retry id isolation, and browser-close-before-reopen ordering.
- [ ] **Step 2: Run the focused tests** and verify current username-derived paths and partial saves fail the assertions.
- [ ] **Step 3: Integrate `ProfileRuntime`** into both registration paths and preserve Appium as `legacy_unbound`/out of profile scope.
- [ ] **Step 4: Run focused lifecycle tests** and existing creation/worker tests.
- [ ] **Step 5: Run `python3 -m compileall -q core tests`**.

### Task 5: Kernel browser probe and account identity verification

**Files:**
- Modify: `core/profile_runtime.py`
- Modify: `core/account_warmer.py`
- Create: `tests/test_browser_probe_contract.py`

**Interfaces:**
- `BrowserObservation` contains parsed origin/redirect outcome, response status, auth-cookie names, login/challenge/application signals, and observed email/identity confidence.
- `BrowserProfileKernel.probe_playwright(handle, ...) -> BrowserObservation` and `probe_selenium(handle, ...) -> BrowserObservation` acquire/release the lease and never enter credentials.
- `BrowserProfileKernel.login_and_warm(handle, email, password, duration_minutes) -> WarmResult` uses the recorded adapter, re-probes after login, and refuses identity mismatch/challenge/runtime/proxy errors.
- All callers consume normalized observation/status values; no caller performs URL substring status decisions.

- [ ] **Step 1: Write failing probe tests** for authenticated, login-required, challenge, account mismatch, unknown identity, profile busy, runtime mismatch, and no-profile cases.
- [ ] **Step 2: Run `pytest tests/test_browser_probe_contract.py -q`** and confirm the current URL heuristic cannot satisfy the cases.
- [ ] **Step 3: Implement protocol-level observation collection** using URL parsing, response/navigation state, cookies, semantic selectors, and explicit identity binding. Keep JavaScript only for unavoidable DOM observation, never as the identity source.
- [ ] **Step 4: Refactor both warmers** to call the kernel and use the same lease/session for probe, optional login, activity, and close.
- [ ] **Step 5: Run focused probe/warm tests** plus the previous warmer tests.

### Task 6: C-scheme health checker and worker persistence

**Files:**
- Modify: `core/health_checker.py`
- Modify: `web/worker.py`
- Create: `tests/test_health_contract.py`

**Interfaces:**
- `AccountHealthChecker.check_single(email, password, profile_id=None, profile_path=None, engine=None, proxy=None) -> dict` always returns browser and mailbox facts plus derived `status`.
- Browser failures, busy leases, conflicts, and proxy mismatches remain in `browser_status`; IMAP runs when policy permits and is never overwritten.
- `web.worker.execute("health", ...)` persists `update_health_snapshot` and updates legacy `status/notes` only as a derived compatibility projection.
- `get_summary` counts derived statuses without treating `degraded` as active.

- [ ] **Step 1: Write failing tests** for every browser/mailbox combination in the C matrix, profile busy behavior, IMAP fallback rules, snapshot persistence, and legacy two-argument calls.
- [ ] **Step 2: Run focused health tests** and verify current browser-first early return fails them.
- [ ] **Step 3: Implement dual-channel checking** through `BrowserProfileKernel` and normalized IMAP classification, then persist the full snapshot.
- [ ] **Step 4: Run focused health tests** and all worker tests.
- [ ] **Step 5: Run `git diff --check`**.

### Task 7: Engine-authoritative warm dispatch and API/UI contract

**Files:**
- Modify: `web/tasks.py`
- Modify: `web/worker.py`
- Modify: `web/app.py`
- Modify: `web/static/app.js`
- Modify: `web/templates/index.html`
- Create: `tests/test_engine_authority.py`

**Interfaces:**
- Warm task normalization accepts account ids and duration; the server resolves engine per account. A legacy requested engine is rejected when it conflicts.
- Mixed selections are grouped by recorded engine and dispatched to the matching adapter.
- `/api/accounts` returns engine, profile state/id, and browser/mailbox/overall statuses, but never absolute profile paths or proxy strings.
- The account UI removes the free warm-engine override and renders both health channels and profile state.

- [ ] **Step 1: Write failing API/worker tests** for cross-engine rejection, mixed-engine grouping, no-profile legacy accounts, safe serialization, and new status rendering data.
- [ ] **Step 2: Run focused tests** and confirm current UI/worker allows an override.
- [ ] **Step 3: Implement authoritative dispatch and additive API fields**, retaining compatibility for old clients with clear errors.
- [ ] **Step 4: Run focused API/UI contract tests** and the complete web test set.
- [ ] **Step 5: Run a JavaScript syntax check** with the project-supported runtime and verify no UI code performs health status derivation.

### Task 8: Legacy reconciliation, documentation, and operational diagnostics

**Files:**
- Modify: `core/profile_runtime.py`
- Modify: `core/database.py`
- Modify: `README.md`
- Modify: `data/README.txt`
- Create: `tests/test_profile_reconciliation.py`

**Interfaces:**
- `ProfileRuntime.reconcile()` marks missing/corrupt/orphaned/legacy profiles explicitly and never guesses an original identity.
- A deliberate `adopt_legacy(profile_path, email, engine)` creates `identity_state=identity_reconstructed` and requires a fresh verification before `ready`.
- Diagnostics expose profile id/state/engine/error code without credentials or cookie data.

- [ ] **Step 1: Write failing reconciliation tests** for missing manifests, malformed manifests, legacy directories, stale bindings, and profile id/path collisions.
- [ ] **Step 2: Run focused tests** and confirm no current reconciliation exists.
- [ ] **Step 3: Implement reconciliation/adoption and update documentation** with directory layout, status semantics, proxy limitations, and backup/security guidance.
- [ ] **Step 4: Run focused reconciliation tests** and documentation-sensitive web tests.
- [ ] **Step 5: Run `git diff --check`**.

### Task 9: Full verification and review checkpoint

**Files:**
- Modify: any files required by verified findings only
- Test: all `tests/`

- [ ] **Step 1: Run `python3 -m compileall -q core web tests`**.
- [ ] **Step 2: Run the full suite** with the project environment: `python -m pytest -q` (or the repository's configured equivalent).
- [ ] **Step 3: Run protocol-focused suites** for runtime, registration lifecycle, health, and engine authority.
- [ ] **Step 4: Run an opt-in local Chromium smoke test** for each adapter using a temporary profile; do not create accounts or contact external account services.
- [ ] **Step 5: Inspect `git diff --check`, `git status --short`, and API redaction output; document any untestable external Gmail behavior explicitly.
