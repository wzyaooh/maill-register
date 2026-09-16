# Operational Reliability and Secret-Safety Design

## Goal

Finish the browser-profile work as an operational system rather than a set of
adapter-local conventions. A warm attempt must report real activity and real
cleanup, credentials must not leave their intended storage boundary, browser
authentication must be established from protocol facts, and every retryable
external operation must have durable ownership and recovery state.

## Delivery Order

The implementation order is fixed because later phases depend on contracts
introduced by earlier phases:

1. warm operation integrity and cancellation cleanup;
2. password/log/notification/export redaction;
3. strict browser authentication observations and local protocol fixtures;
4. one durable attempt/job ledger connected to the retry engine;
5. durable SMS orders with timeout compensation;
6. Playwright/Selenium identity capability matrix and opt-in real smoke tests;
7. Appium lifecycle, removal of path-only compatibility, CI, and dependency
   support matrix.

Every phase must preserve both Playwright and Selenium. No phase may create a
real account, buy an SMS number, or use stored production credentials in an
automated test.

## 1. Warm Operation Integrity

A warm result is an operation report, not a boolean projection. Its stable
fields are:

```json
{
  "success": false,
  "browser_status": "authenticated",
  "error_code": "activity_failed",
  "activity_attempts": 3,
  "activity_successes": 0,
  "last_activity_error": "navigation failed",
  "cleanup_status": "completed",
  "lease_released": true,
  "browser_process_stopped": true
}
```

At least one post-authentication activity must succeed. Navigation failures are
counted and retained without leaking target content. A cleanup failure takes
precedence over an otherwise successful result and returns `cleanup_failed`.
The lease remains held until the adapter confirms shutdown. If graceful close
cannot confirm termination, cancellation supervision owns the process group
and must verify that the group is gone before recording `cancelled`.

Registration and post-registration warming are separate outcomes. A committed
account is not rolled back by a later warm failure, but the warm failure is
persisted and displayed explicitly.

## 2. Secret Boundaries

Passwords may exist only in the account credential store and in the immediate
in-memory call that needs them. They are excluded from structured logs,
exceptions, Telegram messages, ordinary account APIs, task results, and default
exports. Proxy credentials follow the same output policy.

The default export is metadata-only. A credential export, if retained at all,
must be an explicit privileged action with a warning, a narrow response, and no
server-side log of its payload. Existing password-view and plaintext-export
routes are removed or changed to deny by default.

## 3. Browser Authentication Protocol

Authentication is classified from a normalized observation containing:

- final URL and trusted HTTPS origin;
- navigation response status and redirect outcome where available;
- scoped Google authentication cookies, including cookie domain, security, and
  expiry facts rather than names alone;
- explicit application-shell selectors;
- explicit login and challenge selectors;
- a normalized observed account identity and its source.

Generic body words such as `Primary`, `Compose`, or `Google Account` cannot
establish authentication. A trusted mail/account origin plus an auth cookie and
an application-shell signal may establish a native profile's session. A
reconstructed identity additionally requires an observed email matching the
manifest. Login and challenge evidence overrides positive evidence.

Local HTML pages served from an ephemeral HTTP server exercise redirects,
cookies, login pages, challenge pages, and misleading text for both adapters.
They never depend on Gmail or external network availability.

## 4. Attempt and Job Ledger

One SQLite ledger owns every create, warm, health, and compensation job. A job
is the user-visible unit; an attempt is one execution with immutable input
identity and mutable lifecycle state.

Jobs record `job_id`, kind, subject, requested engine, state, retry policy,
timestamps, and terminal summary. Attempts record `attempt_id`, ordinal,
strategy, worker ownership/heartbeat, start/finish timestamps, normalized error
code, retry decision, cooldown, and result metadata. Secrets are referenced, not
copied into the ledger.

The retry engine drives `should_retry`, next strategy, and cooldown before a new
attempt is claimed. Serial, parallel, resumed, and Web execution use the same
claim/finalize API. A stale `running` attempt is recovered as interrupted and is
eligible for policy-controlled retry; completed attempts are never rerun.

## 5. SMS Order Lifecycle

SMS orders are durable before a number is handed to a registration flow. The
record contains provider, provider order id, redacted phone metadata, owning
job/attempt, state, lease/expiry times, polling state, compensation attempts,
and normalized provider errors. API keys and full provider payloads are never
stored.

State transitions are idempotent:

```text
allocating -> active -> code_received -> completed
                  \-> cancel_pending -> cancelled
                  \-> expired -> cancel_pending
```

Startup and periodic reconciliation claim expired orders and retry provider
cancellation with bounded backoff. Provider `finish` and `cancel` responses are
recorded; exceptions may not be swallowed.

## 6. Identity Capability Matrix and Smoke Tests

Capabilities are data, not documentation-only claims. Each adapter reports
whether it can enforce user agent, viewport, locale, Accept-Language, timezone,
geolocation, browser channel/version, persistent storage, proxy endpoint, and
authenticated proxy credentials. Values are `native`, `best_effort`,
`unsupported`, or `not_verified`, with an optional reason.

Local opt-in smoke tests launch real Playwright and Selenium browsers with
temporary profiles, observe their local HTTP/JavaScript-visible identity, close
them, reacquire the profile lease, and confirm no owned browser process remains.
External Gmail and production-proxy smoke tests remain separate manual gates.

## 7. Final Convergence

Appium either adopts the same job/attempt and account lifecycle contract or is
reported as an explicitly unsupported registration engine; it must not create
partial anonymous account rows. Once migrations and diagnostics can identify
legacy rows, health and worker APIs stop accepting `profile_path` as identity.
Only `profile_id` crosses service boundaries.

CI runs supported Python/OS/dependency combinations, unit and local protocol
tests, compile checks, JavaScript syntax checks, and opt-in browser smoke jobs.
The dependency matrix must account for macOS system Python using LibreSSL;
either pin a compatible `urllib3` line there or require a managed Python linked
against supported OpenSSL. The README states tested and unsupported
combinations precisely.

## Cross-Phase Invariants

- A positive result is impossible when required work or cleanup failed.
- A lease is released only after the owned browser is confirmed stopped.
- Cancellation is terminal only after process cleanup is verified.
- Passwords, proxy secrets, cookies, and SMS API keys never enter logs or
  durable operation metadata.
- Every retry and external order has one durable owner and an idempotency key.
- Local deterministic tests are the default; real-provider tests are opt-in.
