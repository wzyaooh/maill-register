# SMS Order Persistence and Compensation Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use `superpowers:test-driven-development` task by task and `superpowers:verification-before-completion` before claiming completion.

**Goal:** Persist every SMS order before it is handed to a registration flow, make provider finish/cancel operations durable and idempotent, and recover expired orders without storing OTPs or API secrets.

**Architecture:** `core/sms_orders.py` owns a small SQLite state machine in the same database boundary as jobs and attempts. `services/sms_manager.py` remains the provider adapter and reports normalized operation outcomes. `core/phone_bypass.py` owns the order context for one verification attempt and passes job/attempt ownership through the Playwright flow. A reconciliation method claims due compensation rows and retries provider cancellation with bounded backoff.

**Tech Stack:** Python 3.9, SQLite, asyncio/aiohttp, unittest, existing `JobLedger` and `secret_safety` helpers.

**Spec:** `docs/superpowers/specs/2026-09-12-operational-reliability-design.md`

## Global Constraints

- Tests are offline and never buy a real number, use production credentials, or persist OTP text.
- Playwright and Selenium remain available; SMS changes must not remove either engine.
- Provider IDs and phone metadata are operational identifiers only; API keys, full provider payloads, and OTPs never enter logs or durable metadata.
- Cancellation and finish operations are idempotent and retryable; exceptions become normalized error codes and are not silently discarded.

### Task 1: Durable order state machine

**Files:**
- Create: `core/sms_orders.py`
- Modify: `core/job_ledger.py` or shared schema only if required
- Test: `tests/test_sms_orders.py`

- [ ] Write tests for allocation, activation, awaiting-code, code-received, completion, cancellation, expiration, and idempotent repeated transitions.
- [ ] Verify raw SQLite rows contain only masked phone metadata and no OTP/API-key values.
- [ ] Implement `SmsOrderStore` with `create_order`, transition methods, `claim_expired`, `list_due_compensation`, and `record_compensation` using immediate SQLite transactions.
- [ ] Add ownership fields (`job_id`, `attempt_id`), deadline/poll timestamps, compensation counters, and normalized error code.
- [ ] Run `venv/dev/bin/python -m unittest -v tests.test_sms_orders`.

### Task 2: Provider adapter outcomes

**Files:**
- Modify: `services/sms_manager.py`
- Test: `tests/test_sms_manager.py`

- [ ] Write offline tests proving provider allocation is persisted before the caller receives the number, provider cancel/finish success updates state, and provider exceptions produce a durable retryable failure.
- [ ] Add optional order-store/job/attempt context to public SMS functions without breaking non-persistent callers.
- [ ] Return normalized outcome objects and raise/propagate a typed provider error after recording it; never log order IDs with secrets or provider payloads.
- [ ] Add async `reconcile_expired_orders` with bounded backoff and a provider callback seam for tests.
- [ ] Run SMS manager and secret-boundary tests.

### Task 3: Registration integration

**Files:**
- Modify: `core/phone_bypass.py`, `core/runners.py`, `core/creation_flow.py`, `core/batch_runner.py`
- Test: `tests/test_sms_registration_integration.py`

- [ ] Write tests that pass a durable job/attempt context through Playwright verification and assert order ownership is recorded.
- [ ] Create an order immediately after provider allocation, mark it active before returning the phone, and mark awaiting/code-received/completed or cancel-pending on each branch.
- [ ] Ensure timeout, rejected phone, missing input, cancellation, and unexpected exceptions all request idempotent cancellation and preserve the original normalized error code.
- [ ] Keep OTP only in the immediate in-memory call; never include it in result metadata, logs, or account rows.
- [ ] Run both Playwright and Selenium creation/ledger suites.

### Task 4: Startup/periodic compensation

**Files:**
- Modify: `web/worker.py`, `web/tasks.py` or a dedicated scheduler hook
- Test: `tests/test_sms_compensation.py`

- [ ] Write tests for an expired order, one failed cancellation, bounded retry delay, eventual cancellation, and exhausted compensation becoming `compensation_failed`.
- [ ] Invoke reconciliation at worker startup and expose a narrow internal trigger for periodic calls; do not add an unauthenticated public route.
- [ ] Verify stale jobs can be reconciled after process restart and repeated scans do not duplicate provider calls for a non-due row.
- [ ] Run the full suite and compile/JS/diff checks.
