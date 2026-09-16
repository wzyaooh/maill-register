# Final Operational Convergence Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use `superpowers:test-driven-development` task by task and `superpowers:verification-before-completion` before claiming completion.

**Goal:** Close the remaining reliability, secret-boundary, browser-smoke, retry, SMS-compensation, legacy, and CI gaps without weakening the existing engine-authoritative profile kernel.

**Architecture:** Keep `ProfileRuntime` and `JobLedger` as the durable sources of truth. Extend the retry engine with operation-aware policy and use it for Web health, warm, and compensation attempts. Keep SMS provider state and claim fencing in `SmsOrderStore`, and expose only finite status codes at every persistence boundary.

**Tech Stack:** Python 3.9+, SQLite, Playwright, Selenium, Flask, unittest, GitHub Actions, Node syntax checks.

**Spec:** `docs/superpowers/specs/2026-09-12-operational-reliability-design.md`

## Global Constraints

- Playwright and Selenium remain supported and reuse their registration engine/profile.
- No test contacts Gmail, buys a real SMS number, uses production credentials, or opens a production profile.
- Passwords, proxy credentials, cookies, OTPs, API keys, and provider payloads never enter logs, task metadata, exports, or operation ledgers.
- Appium remains explicitly unsupported until it has the complete lifecycle contract.
- Existing user changes in the dirty worktree are preserved; no reset, checkout, commit, or push is performed.

### Task 1: Synchronised observations and classifier evidence

Modify `core/profile_runtime.py` and tests so dict and attribute reads stay identical after every mutation, and expose only protocol-derived authentication facts.

### Task 2: Operation-aware retry ledger

Modify `core/retry_engine.py`, `web/worker.py`, and ledger tests so health/warm/compensation attempts call the same begin/record/decision path, retry only transient failures, and never start an attempt for a terminal job.

### Task 3: SMS compensation hardening

Modify `core/sms_orders.py` and `services/sms_manager.py` with finite provider status codes, cancellation-safe claim release, HTTP/provider failure detection, and restart/timeout tests.

### Task 4: Capability smoke and runtime matrix

Complete local Playwright/Selenium smoke assertions, driver/browser major-version checks, and capability evidence reporting while keeping the tests opt-in.

### Task 5: Legacy/Appium boundary, CI, and dependency documentation

Remove stale path-only/Appium claims from user-facing docs, add static boundary tests, create a CI matrix with an opt-in smoke job, and document the tested dependency combinations and known LibreSSL/ChromeDriver limitations.

### Task 6: Full verification

Run the complete unittest suite, compile checks, JavaScript syntax check, and diff check. Report skipped smoke tests and environment warnings separately.
