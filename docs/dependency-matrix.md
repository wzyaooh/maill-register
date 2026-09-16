# Dependency and Runtime Matrix

This project has two supported desktop browser adapters.  The matrix below is
the contract used by local development and CI; it is intentionally separate
from provider credentials and production services.

| Component | Minimum declared version | Current local verification | Runtime requirements |
| --- | --- | --- | --- |
| Python | 3.9 | 3.9.6 | CI runs 3.9, 3.10, 3.11 and 3.12 |
| Playwright | 1.40 | 1.60.0 | Run `playwright install chromium`; the browser binary is not installed by `pip` |
| `playwright-stealth` | 1.0.6 | 2.0.3 | Loaded by the Playwright adapter when available |
| Selenium | 4.15 | 4.36.0 | A local Chrome/Chromium and a matching ChromeDriver are required |
| `webdriver-manager` | 4.0 | installed with requirements | May help provision a driver, but does not bypass the major-version check |
| Flask | 3.0 | 3.1.3 | Web console and API |
| `urllib3` | 2.5 | 2.6.3 | Python builds linked to LibreSSL may emit a compatibility warning |
| Appium Python client | 3.1 | 5.3.1 | Package is optional for diagnostics; account creation is currently fail-closed |

## CI and Operating-System Matrix

The required CI matrix is intentionally small enough to run on every change
while still exercising the supported Python and desktop-runtime families:

| CI OS | Python | Default verification | Browser smoke |
| --- | --- | --- | --- |
| Ubuntu | 3.9, 3.10, 3.11, 3.12 | Full unittest suite, compile checks, dependency check | Required separate Python 3.12 job on push/PR |
| macOS 14 | 3.9, 3.11, 3.12 | Full unittest suite, compile checks, dependency check | Local opt-in |

macOS jobs use the Python distribution selected by `actions/setup-python`; the
same rule is recommended for local development through Homebrew, pyenv, or
another managed distribution.  `/usr/bin/python3` can be linked against
LibreSSL.  That can produce an `urllib3` `NotOpenSSLWarning`, but it is an
environment warning rather than proof that a browser session or provider call
works.  Do not downgrade `urllib3` to hide it: Selenium currently requires
`urllib3>=2.5`.  Use an OpenSSL-linked Python build when the warning must be
removed.

The CI dependency check is `python -m pip check` after installing the declared
minimum dependency ranges.  It catches incompatible transitive resolutions
before browser tests run. The separate real-browser job is required on every
push and pull request. Manual dispatch still offers the `real_browser_smoke`
input; set it to `true` to include that job in a manually requested run.

## Browser Contract

Registration writes the engine, browser channel/major, identity and proxy
binding to the profile manifest.  Health and warm operations must reopen that
same profile with the recorded engine while holding its lease.  A package being
importable, or an Appium port being reachable, does not prove that the runtime
can safely use a profile.

For Selenium, the Chrome and ChromeDriver major versions must match.  The
adapter reports `runtime_mismatch` or `runtime_unavailable` before navigation
when they cannot be verified.  The real smoke suite skips with a diagnostic when
Chrome, ChromeDriver, or a matching pair is unavailable; it never silently
falls back to a different browser. Those local skips become gate failures in
the required smoke runner, rather than successful CI jobs. CI provisions Chrome
and a compatible ChromeDriver with `browser-actions/setup-chrome@v1`, using its
`chrome-path` and `chromedriver-path` outputs, and verifies matching majors.
There is no reliance on Ubuntu-specific Chromium package paths.

For Playwright, the bundled Chromium executable must exist.  The smoke suite
uses `python -m playwright install --with-deps chromium` in CI and skips when a
local installation is absent.

## Verification Modes

The ordinary push and pull-request workflow installs `requirements.txt`, runs
the full `unittest` suite, compiles Python modules, checks browser protocol
fixtures, validates `web/static/app.js`, and performs `git diff --check`.  It
does not contact Gmail, buy SMS numbers, send Telegram notifications, or read
production credentials.

Real browser smoke runs in a separate required push/PR job. For manual
`workflow_dispatch`, set `real_browser_smoke` to `true`. Run the same execution
gate locally with `GMAIL_CONFIG_FROM_ENV=1 python -m tools.run_browser_smoke`;
the runner opts into the local HTTP fixture and requires both Playwright and
Selenium smoke tests to actually complete successfully. Missing tests, skips
(including skipped subtests), failures, or errors produce nonzero exit status.
The tests verify profile persistence, identity/runtime
recording, locale and geolocation settings, storage continuity, process
shutdown, and lease reacquisition.  They do not log in to a real account.

## macOS Notes

The reference development machine currently uses Python 3.9.6, Playwright
1.60.0, Selenium 4.36.0, and LibreSSL 2.8.3.  `urllib3` 2.x prints a
`NotOpenSSLWarning` under that Python build because it expects OpenSSL 1.1.1 or
newer.  It is an environment warning, not evidence that a browser or provider
is healthy.  Prefer a Python build linked against a current OpenSSL for new
deployments.

On macOS, set `CHROME_BINARY` and `CHROMEDRIVER_PATH` when the binaries are not
on `PATH`.  The smoke tests compare their majors before creating a session.
`RUN_REAL_BROWSER_SMOKE=1` is required when running the unittest module directly;
the gate runner sets this opt-in before loading the tests. Normal unit tests
remain local and deterministic. Only temporary profiles and localhost fixtures
are used by the smoke; no saved account profile or production service is opened.

## Appium Status

Appium is represented in the dependency and system diagnostics so an operator
can identify an installed client or a listening `127.0.0.1:4723` service.  The
creation path remains unsupported and returns without starting a device
session.  It may be enabled only after a real account, database row,
job/attempt ledger entry, and profile/identity lifecycle have been verified.
