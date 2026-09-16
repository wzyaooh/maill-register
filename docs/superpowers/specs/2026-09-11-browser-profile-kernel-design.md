# Browser Profile Kernel Consistency Design

**Status:** Approved for implementation

## Goal

Make registration, account warming, and account health checks reuse the same
browser engine, persistent profile, browser identity, network binding, and
exclusive ownership contract for each account. Keep both Playwright and
Selenium, but never use one engine to open a profile registered by the other.

This design is for session correctness and operational consistency. It does not
add new fingerprint-evasion behavior. Existing optional page scripts remain
compatibility features; the source of truth for a profile is the browser
runtime and its manifest, not generated JavaScript strings.

## Non-goals

- Converting a Playwright profile into a Selenium profile or the reverse.
- Automatically repairing an account/profile conflict by choosing one side.
- Treating a URL substring as proof that a particular mailbox is authenticated.
- Persisting proxy passwords, account passwords, or raw cookies in the manifest.
- Claiming that a manifest can make a host's OS, TLS stack, GPU, fonts, or
  network indistinguishable from another host.

## Source of Truth and Data Model

Each durable profile is a directory below the selected runtime root:

```text
runtime/<environment>/data/profiles/<profile_id>/
  profile_manifest.json
  profile.lock
  <Chromium user-data files>
```

`profile_id` is a random, immutable identifier. It is not derived from a
username or email. The profile directory must stay below the runtime data
root, must not be a symlink, and is created with mode `0700`.

The manifest is JSON, written atomically with mode `0600`, and contains:

```json
{
  "schema_version": 1,
  "profile_id": "...",
  "engine": "playwright",
  "email": "user@example.test",
  "state": "provisioning",
  "identity_state": "native",
  "browser": {
    "channel": "chrome",
    "major_version": "134"
  },
  "identity": {
    "user_agent": "...",
    "viewport": {"width": 1366, "height": 768},
    "locale": "en-US",
    "timezone_id": "America/New_York",
    "geolocation": {"longitude": -74.006, "latitude": 40.7128},
    "is_mobile": false,
    "has_touch": false,
    "hardware_concurrency": 8,
    "device_memory": 8,
    "client_hints": {}
  },
  "network": {
    "bound": false,
    "endpoint_hash": "",
    "source": ""
  },
  "created_at": "...",
  "updated_at": "..."
}
```

The manifest never contains proxy credentials. The account database remains the
credential source for the current proxy string; the kernel compares its
non-secret endpoint fingerprint with `network.endpoint_hash` before opening a
bound profile. A missing or changed bound proxy is an explicit
`proxy_unavailable`/`proxy_mismatch` result, never an implicit direct
connection.

The accounts table retains `profile_path` for backward compatibility but adds:

- `profile_id`
- `engine`
- `profile_state`
- `identity_state`
- `browser_status`
- `mailbox_status`
- `overall_status`
- `browser_checked_at`
- `mailbox_checked_at`
- `last_error_code`

`profile_id` is the binding key. `profile_path` is validated and normalized to
the path derived from that id for new records. A database row and manifest with
different profile ids, engines, or emails is a hard `profile_conflict`.

## Lifecycle

Profiles move through explicit states:

```text
provisioning -> bound -> ready
       |          |       |
       +----------+-------+--> orphaned / corrupt / retired
```

1. Registration provisions a profile and writes the manifest before launching
   the browser.
2. The browser adapter records the actual channel and major version after
   launch, then registration binds the profile to the verified email and
   marks it `ready` only after account persistence succeeds.
3. Any failed registration or cancelled process marks an unbound profile
   `orphaned`; it is not silently reused on a retry.
4. Startup reconciliation detects malformed manifests, missing directories,
   stale bindings, and legacy profile directories. It marks them explicitly;
   it does not guess an original identity.
5. Legacy rows without a manifest are `legacy_unbound`. A deliberate adoption
   creates a manifest with `identity_state=identity_reconstructed` and requires
   a subsequent identity verification before it can be `ready`.

Manifest writes and account binding use a recoverable two-phase sequence. A
  manifest in `bound` without a matching database row is reconciled to
  `orphaned`; a database row referring to a missing/malformed manifest is
  returned as `profile_conflict` or `profile_unavailable`.

## Lease Protocol

Every profile operation (`open`, `probe`, `login`, `warm`, and `close`) obtains
an OS-level, cross-process exclusive lease on `profile.lock` before starting a
browser. The lease is independent of Chromium's own lock files.

The lock record contains only `pid`, `operation`, `engine`, and timestamps. It
is advisory metadata; the OS lock is authoritative. Acquisition is nonblocking
by default and may use a bounded timeout. Failure returns `profile_busy`.
The lease is released in all normal, exception, cancellation, and SIGTERM
paths. A stale metadata record is harmless once the OS lock is released; PID
inspection is used only for diagnostics and reconciliation.

No operation may fall back to a different browser engine when a lease is busy.
Per-profile operations are serialized by the same lease in both web workers and
direct CLI calls.

## Browser Kernel and Adapters

`core/profile_runtime.py` owns manifest validation, path resolution, lifecycle,
lease acquisition, proxy binding checks, and normalized browser observations.
It exposes a small kernel API:

```python
runtime = ProfileRuntime(runtime_root)
handle = runtime.resolve(profile_id=..., expected_email=..., expected_engine=...)
with runtime.lease(handle, operation="health"):
    manifest = runtime.load(handle)
```

The Playwright and Selenium adapters remain in their existing modules, but
their launch configuration comes only from the resolved manifest:

- Playwright uses `launch_persistent_context` with the stored context options,
  stored proxy binding, and the recorded channel policy.
- Selenium uses `--user-data-dir`, stored user agent/window/locale settings,
  CDP timezone/geolocation where supported, and the same proxy binding.
- A browser major-version mismatch is `runtime_mismatch`; it is not silently
  hidden by regenerating a new identity.
- Adapters report structured observations to the kernel. They do not decide
  account status independently.

The existing JS compatibility scripts are not identity stores. They must not
  generate identity values from `Date.now()` or unseeded `Math.random()` for
  health/warm operations. Registration may keep existing optional scripts, but
  all values that affect the persisted profile identity are supplied by the
  manifest.

## Browser Authentication Probe

The kernel's probe combines browser-protocol facts:

- final origin and redirect chain parsed with URL parsing;
- response/navigation outcome;
- presence of relevant persistent Google auth cookies;
- semantic login/challenge/application DOM signals;
- an explicit account identity observation when available.

The probe returns one of:

```text
not_configured, authenticated, login_required, challenge,
account_mismatch, profile_busy, runtime_unavailable, runtime_mismatch, error
```

`authenticated` is not accepted as the expected account until the observed
email is confirmed or the profile has a verified binding established by the
same runtime. An account selector or unknown identity is
`identity_unverified`/`account_mismatch`, not `active`.

Health never enters credentials or mutates the profile beyond the minimum
navigation required for the probe.

## Health Contract (C)

Health always computes both channels when configured:

```json
{
  "email": "user@example.test",
  "engine": "playwright",
  "profile_id": "...",
  "browser_status": "authenticated",
  "mailbox_status": "active",
  "status": "active",
  "message": "Browser session and IMAP are available",
  "browser_checked_at": "...",
  "mailbox_checked_at": "...",
  "checked_at": "...",
  "last_error_code": ""
}
```

Mailbox statuses:

```text
not_configured, active, password_changed, locked, suspended,
network_error, error
```

The compatibility `status` is derived, never assigned by one channel:

- both active: `active`;
- one active and one unavailable/error/busy: `degraded`;
- both explicitly locked/challenged: `locked`;
- either explicitly suspended: `suspended`;
- IMAP explicitly rejects credentials: `password_changed`;
- browser lease busy: `degraded` with `browser_status=profile_busy`;
- neither channel can establish a fact: `error` or `network_error`.

Accounts without a profile return `browser_status=not_configured` and still
run IMAP. A browser runtime failure may fall back to IMAP, but a profile
conflict, busy lease, identity mismatch, or proxy mismatch is retained in the
browser channel and is never relabeled as an account lock.

The database stores the complete snapshot atomically. The Web API returns safe
profile metadata and both health channels; it does not expose absolute profile
paths, proxy strings, cookies, or passwords in ordinary account listings.

## Warm Contract

The warm task receives account ids and duration only. The account's resolved
manifest selects the engine. For compatibility, a caller may send a requested
engine, but a mismatch is rejected before any browser starts. Mixed account
selections are grouped by recorded engine and run through the corresponding
adapter.

Warmer behavior:

1. Resolve account/manifest and acquire the profile lease.
2. Open the recorded engine with the recorded identity and proxy binding.
3. Probe the existing session.
4. If `authenticated`, skip credential entry.
5. If `login_required`, perform the normal login flow in that same session,
   then probe again and require identity confirmation.
6. If `profile_busy`, `account_mismatch`, `challenge`, `runtime_mismatch`, or
   `proxy_mismatch`, stop and return the structured failure.
7. Perform the existing bounded warm activity and close the adapter before
   releasing the lease.

## API and UI Compatibility

The existing `status` and `notes` fields remain for older clients. New fields
are additive. The warm engine selector is removed from the normal UI; the
server derives engine per account. Old clients sending an engine receive a
clear mismatch error rather than a cross-engine launch.

The account table displays engine, profile state, and the two health channel
statuses without exposing filesystem paths.

## Testing and Acceptance

Unit and protocol tests must cover:

- atomic manifest creation, validation, schema upgrade, and corrupt-file
  handling;
- stable identity across repeated opens;
- path containment and symlink rejection;
- database/manifest conflict detection and two-phase reconciliation;
- cross-process lease exclusion and guaranteed release;
- proxy binding hash and no silent direct fallback;
- Playwright/Selenium dispatch from the manifest;
- browser probe identity mismatch, challenge, login-required, and
  authenticated cases;
- C-scheme status derivation for every channel combination;
- IMAP fallback only for unavailable profiles;
- warm engine mismatch rejection and mixed-engine grouping;
- registration success/failure/retry/crash lifecycle;
- legacy rows marked `legacy_unbound`/`identity_reconstructed`;
- safe Web API serialization and UI rendering.

Real-browser smoke tests are opt-in and use a local test profile; they must not
create accounts or send credentials to external services by default.
