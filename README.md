<div align="center">

```
  ██████╗ ███╗   ███╗ █████╗ ██╗██╗         ██╗███╗   ██╗███████╗██╗███╗   ██╗██╗████████╗██╗   ██╗
 ██╔════╝ ████╗ ████║██╔══██╗██║██║         ██║████╗  ██║██╔════╝██║████╗  ██║██║╚══██╔══╝╚██╗ ██╔╝
 ██║  ███╗██╔████╔██║███████║██║██║         ██║██╔██╗ ██║█████╗  ██║██╔██╗ ██║██║   ██║    ╚████╔╝
 ██║   ██║██║╚██╔╝██║██╔══██║██║██║         ██║██║╚██╗██║██╔══╝  ██║██║╚██╗██║██║   ██║     ╚██╔╝
 ╚██████╔╝██║ ╚═╝ ██║██║  ██║██║███████╗    ██║██║ ╚████║██║     ██║██║ ╚████║██║   ██║      ██║
  ╚═════╝ ╚═╝     ╚═╝╚═╝  ╚═╝╚═╝╚══════╝    ╚═╝╚═╝  ╚═══╝╚═╝     ╚═╝╚═╝  ╚═══╝╚═╝   ╚═╝      ╚═╝
 ███████╗ █████╗  ██████╗████████╗ ██████╗ ██████╗ ██╗   ██╗    ██████╗  ██████╗ ██████╗  ██████╗
 ██╔════╝██╔══██╗██╔════╝╚══██╔══╝██╔═══██╗██╔══██╗╚██╗ ██╔╝    ╚════██╗██╔═══██╗╚════██╗██╔════╝
 █████╗  ███████║██║        ██║   ██║   ██║██████╔╝ ╚████╔╝      █████╔╝██║   ██║ █████╔╝███████╗
 ██╔══╝  ██╔══██║██║        ██║   ██║   ██║██╔══██╗  ╚██╔╝      ██╔═══╝ ██║   ██║██╔═══╝ ██╔══██║
 ██║     ██║  ██║╚██████╗   ██║   ╚██████╔╝██║  ██║   ██║       ███████╗╚██████╔╝███████╗╚██████╔╝
 ╚═╝     ╚═╝  ╚═╝ ╚═════╝   ╚═╝    ╚═════╝ ╚═╝  ╚═╝   ╚═╝       ╚══════╝ ╚═════╝ ╚══════╝ ╚═════╝
```
# 🏭 Gmail Infinity Factory 2026

**The most powerful and stealthiest Gmail account automation engine of 2026**

[![Python](https://img.shields.io/badge/Python-3.9%2B-blue?logo=python&logoColor=white)](https://python.org)
[![Version](https://img.shields.io/badge/Version-2026.1.0-green)](https://github.com/ShadowHacker0/gmail-infinity-factory)
[![License](https://img.shields.io/badge/License-Proprietary-red)](LICENSE)
[![Platform](https://img.shields.io/badge/Platform-Windows%20%7C%20Linux%20%7C%20macOS-lightgrey)](https://github.com)
[![Stealth](https://img.shields.io/badge/Stealth-30%2F30-brightgreen)](https://github.com)
[![Author](https://img.shields.io/badge/Author-Shadow-purple)](https://github.com/ShadowHackrs)

</div>

---

## 📖 Table of Contents

- [Web interface](#web-interface)
- [Overview](#-overview)
- [Key Features](#-key-features)
- [Project Structure](#-project-structure)
- [Requirements](#-requirements)
- [Installation](#-installation)
- [Configuration](#-configuration)
- [Usage](#-usage)
- [Module Descriptions](#-module-descriptions)
- [Supported Providers](#-supported-providers)
- [Legal Disclaimer](#-legal-disclaimer)
- [Copyright](#-copyright)

---

## Web interface

> **Current source of truth:** the executable entry point is
> `auto_gmail_creator.py`, configuration is loaded from `.env` by
> `config/settings.py`, and account storage is SQLite. The older promotional
> sections below describe a different layout/menu; `main.py`,
> `config/settings.yaml`, CloakBrowser and SecureVault are not implementations
> in this checkout.

### Install and launch

Use Python 3.10+ for a new installation and an isolated environment:

```bash
python3 -m venv venv
source venv/bin/activate                 # Windows: venv\Scripts\activate
python -m pip install -r requirements.txt
python -m playwright install chromium
```

Set `WEB_ADMIN_PASSWORD` in the server environment or in a local `.env` file.
Use a unique password of at least 16 characters; there is no default password,
public registration, or unauthenticated setup endpoint. Never commit this file.
The browser login only asks for this administrator password.

```bash
# Web interface, local access: http://127.0.0.1:8080
python auto_gmail_creator.py

# LAN/server access: http://SERVER_ADDRESS:8080
python auto_gmail_creator.py --host 0.0.0.0 --port 8080

# Equivalent module entry point
python -m web --host 0.0.0.0 --port 8080
```

The application now starts the Web interface by default. The old interactive
terminal menu has been removed; `--web` remains accepted for existing launch
commands but is no longer required. Waitress is used instead of Flask's
development server, and the launcher resolves the project directory automatically.
Background creation and resume operations live in `core/creation_flow.py`;
`core/progress.py` only provides non-interactive task progress and output.

For remote access, put the Web service behind an **HTTPS reverse proxy** or an
encrypted tunnel. With HTTPS, set `WEB_COOKIE_SECURE=true` before starting.
Do not set this flag for plain HTTP local testing, because browsers will then
refuse to send the session cookie. Do not expose the HTTP port directly to the
public Internet; use firewall restrictions. Proxy headers are deliberately not
trusted, so login throttling behind a proxy is shared by its source IP.

Sessions expire after eight hours and, by default, after a server restart.
An optional strong `WEB_SECRET_KEY` makes sessions survive restarts; keep it
secret. Changing the administrator password should be accompanied by rotating
that key if it was explicitly configured.

### Feature coverage

| Existing capability | Web operation |
| --- | --- |
| Ghost / Premium account creation | Creation page: standard flow, SMS on/off |
| YouTube / Workspace flow | Creation page: flow selector |
| Playwright / Selenium / Appium | Creation page: engine selector |
| Serial multi-account tasks | Creation page: quantity and warmup duration |
| Previously standalone threaded batch runner | Creation page: parallel switch, 1–5 workers |
| Dashboard, strategies, batch history | Overview page and structured task results |
| Configuration overview | Shared configuration editor covering **every** `Config` environment field; proxy groups live in their dedicated menu |
| Saved accounts | Search, select, reveal password explicitly |
| CSV / JSON / TXT export | Authenticated download, with plaintext-credential warning |
| Network and proxy checks | Proxy page: background test and result/log view |
| Static proxy import | Dedicated Proxy Management page: paste text, import UTF-8 TXT (append/replace), preview and save |
| Names, user agents | Resource text editors with validation |
| KKOIP dynamic proxy pool | Dedicated Proxy Management page: credentials, capacity, sticky sessions, rotation and pool preference |
| Account health check | All accounts or selected accounts; persisted status updates |
| Post-creation warming module | All/selected accounts, Playwright or Selenium |
| Public proxy fetching | Background fetch/test/save to configured proxy file |
| Telegram test | Connection test plus actual test-message delivery |
| SMS balance helper | 5sim / SMS-Activate, the providers supported by the existing helper |
| Startup configuration validation | Tools/settings: background validation report |
| Old account-data migration | Tools: explicit migration into SQLite |
| Interrupted serial batch | Tools: inspect, resume, or clear saved state |
| Voice OTP server | Tools: start and stop, task log and status |
| Ending a browser session | Web logout; server shutdown remains a deployment operation |

All tasks have persistent history, progress, logs, structured results and a stop
operation. Completed task status means the operation returned, **not** that every
account succeeded: inspect successes/failures and validation errors in its result.
Only one automation/maintenance task may run at a time, with one optional voice
service alongside it. Parallel creation runs its workers inside that one task.
There is no arbitrary shell command execution API.

### Configuration and data behavior

- Open **Proxy Management** (`#proxies`) for static proxies, KKOIP and proxy
  selection settings. These fields share the settings editor and validation API
  with the general configuration page, without duplicate controls. Saving or
  reloading one page preserves unsaved configuration drafts on the other page.
- TXT import stages content in the proxy editor; it does not overwrite the
  server file until **Save proxy file** is clicked. Files must be UTF-8 and the
  combined content must not exceed 1 MiB. The preview table hides credentials.
- Proxy cards show the last read/saved static count and configured dynamic
  session capacity, not live exit IPs. Health counts come from the latest
  completed proxy check in retained task history and are explicitly marked as
  a historical snapshot. Run another check after changing the configuration.
- The settings page edits `.env` atomically. Environment variables set by the
  deployment take precedence and are shown read-only.
- Passwords and API tokens are never returned by the settings API. An untouched
  secret is preserved; explicit clearing removes it. New tasks receive a fresh
  configuration snapshot without restarting the Web server.
- Configuration, resources and saved sessions cannot be changed while a business
  task is active. A running voice service retains its startup configuration until
  it is restarted.
- Resource editors only access `.txt` files under `config/` and `data/`.
  Proxy syntax matches the current engines: `host:port` or `host:port:user:pass`,
  one proxy per line; comment lines start with `#`.
- Existing accounts are read from `data/database.db`; use the migration operation
  to import legacy `data/accounts.json` / `data/accounts.txt`. Migration is an
  explicit Web operation, not a startup side effect.
- Task state and logs live under `data/web/`, excluded from Git. Browser-visible
  logs/results redact configured secrets and stored account passwords. Raw worker
  logs and account exports **can contain credentials**: protect the server files,
  backups and downloaded exports. SQLite is not encrypted by this change.
- CSV cells that could be interpreted as spreadsheet formulas receive a leading
  apostrophe. Use JSON or TXT when exact unmodified credential values are needed.
- The launcher prevents a second Web server from using the same project's
  task store.
- Serial tasks checkpoint after each completed account. Repeated resume preserves
  earlier counts and indexes. The in-progress account is not checkpointed until
  its attempt finishes. Parallel batches do **not** currently support resume;
  stopping one preserves already stored accounts but discards unfinished work.
- Stop sends a termination signal, allowing cleanup, then forcefully terminates
  the worker process group after ten seconds if necessary on POSIX systems.
  Workers also stop when their Web parent disappears. Windows termination cannot
  guarantee cleanup of browser grandchildren; check external processes there.

### External services and inherited limitations

- Appium requires an independently started server at `127.0.0.1:4723` and a
  connected Android device/emulator. Its existing creation flow is incomplete
  and does not persist a verified account; the Web UI does not claim otherwise.
  Parallel Appium jobs are rejected.
- Playwright needs installed browser binaries. Headed mode on a server requires
  a graphical session/display; configure `HEADLESS_MODE` appropriately.
- The optional voice worker requires `VOICE_SERVER_TOKEN` and binds only to
  `127.0.0.1:5000` when launched from Web. Publish `/voice` separately through an
  authenticated HTTPS reverse proxy if your telephony provider needs a webhook.
  Both `/voice` and `/otp` require the configured token via `X-Voice-Token` or
  the `token` query parameter. Header authentication is preferred.
  Audio conversion also requires FFmpeg. The main creation engines do not
  currently consume the voice OTP API automatically.
- Configuration switches reflect existing code; exposing them does not implement
  previously unused flags or make every engine support every setting. In
  particular, the user-selected warmup duration belongs to the Selenium path;
  Playwright has its own existing pre/post-warming timing.
- A configured API key or an open Appium port is not a successful connectivity
  test. Use the available check operations and inspect their results.
- Use automation only where authorized and comply with provider terms.

### Validation

The regression tests use the Python standard-library test runner. They do not
create real accounts, make paid SMS requests, or contact Telegram:

```bash
python -m unittest discover -s tests -p 'test_web*.py' -v
```

---

## 🌐 Overview

**Gmail Infinity Factory 2026** is an advanced Gmail account automation engine built on next-generation stealth technology. It leverages **CloakBrowser** and **Playwright** with a multi-layer architecture that accurately mimics real human behavior and evades all automated detection systems.

The project is written entirely in **Python 3.9+** and ships with:

- An authenticated Web dashboard with background tasks and live logs
- AES-128 encrypted credential storage via **SecureVault**
- Intelligent proxy rotation with automated health-checking
- A fully integrated synthetic human identity generator
- Complete SMS verification support to bypass phone challenges
- An account warming engine covering YouTube, Google Search, and Gmail

---

## ✨ Key Features

### 🧬 Stealth & Detection Evasion

| Feature | Details |
|---------|---------|
| **CloakBrowser** | C++-level stealth engine — passes 30/30 detection tests |
| **Browser Fingerprinting** | 50,000+ unique digital fingerprints with round-robin rotation |
| **Mouse Behavior Engine** | Human-like cursor movement via Bézier curve algorithms |
| **Typing Simulator** | Character-by-character input with randomized delays to bypass bot detection |
| **WebGL / Canvas Spoofing** | GPU ID and Canvas fingerprint forgery |
| **AudioContext Spoofing** | Audio fingerprint randomization |
| **TimeZone Auto-detection** | Automatically derives timezone & locale from proxy IP (GeoIP) |

### 🔐 Security & Credential Storage

- **SecureVault** — Fernet symmetric encryption (AES-128-CBC + HMAC-SHA256) for every credential record
- **Sensitive Data Masking** — Passwords, emails, and phone numbers are automatically masked in all log output
- **Encrypted Key Persistence** — Vault key is stored locally and reused across sessions

### 👤 Identity Generation

- **PersonaGenerator** — Generates complete human personas: first/last name, age, city, state, occupation, interests
- **Faker / Mimesis integration** — Realistic US-based names drawn from a pool of 50+ cities and 50 states
- Supports both male and female personas with ages ranging from 18 to 65

### 📱 SMS Verification

| Provider | Site | Notes |
|----------|------|-------|
| **5sim** | 5sim.net | Highest success rate — real SIM cards |
| **sms-activate** | sms-activate.ru | Reliable Russian provider |
| **TextVerified** | textverified.com | Real US numbers |
| **VirtualSMS** | Various | Free alternative |

### 🌐 Proxy Management

- Supports `HTTP` / `HTTPS` / `SOCKS5`
- Automated health-check before each operation
- Intelligent rotation — proxies are blacklisted automatically after 3 consecutive failures
- Full authentication support: `user:pass@host:port`

### 🔥 Account Warming

- **Gmail Activity Simulator** — Reading, composing, labeling emails
- **YouTube Warmup Engine** — Real watch sessions and interactions
- **Google Search Simulator** — Organic browsing and click-through
- **Reputation Builder** — Sender score and trust-signal cultivation

---

## 🗂️ Project Structure

```
gmail_infinity_factory_2026/
│
├── auto_gmail_creator.py       # Entry point — authenticated Web server
├── web/                       # Web pages, API, authentication and task workers
├── requirements.txt           # Python dependencies
├── .gitignore                 # Git exclusions
│
├── config/                    # Configuration files
│   ├── settings.yaml          # Master config (SMS, CAPTCHA, Proxy, Browser)
│   ├── fingerprints.json      # Digital fingerprint database (50k+ entries)
│   └── proxies.txt            # Proxy list
│
├── core/                      # Core stealth engine
│   ├── __init__.py
│   ├── creation_flow.py        # Shared non-interactive creation and resume flow
│   ├── progress.py             # Background task progress and log output
│   ├── stealth_browser.py     # Stealth browser framework (CloakBrowser + Playwright)
│   ├── behavior_engine.py     # Human behavior simulation (Mouse, Keyboard, Scroll)
│   ├── fingerprint_generator.py  # Fingerprint generator (UA, Screen, GPU, Audio, Font)
│   ├── detection_evasion.py   # Detection bypass layer (webdriver, CDP, headless leaks)
│   ├── cloak_launcher.py      # CloakBrowser launcher with automatic Playwright fallback
│   └── proxy_manager.py       # Advanced proxy manager with rotation, health-check & stats
│
├── creators/                  # Account creation strategies
│   └── ...
│
├── identity/                  # Persona and identity generation
│   └── ...
│
├── verification/              # Verification and authentication
│   ├── __init__.py
│   ├── sms_providers.py       # SMS API clients (5sim, sms-activate, textverified)
│   ├── captcha_solver.py      # CAPTCHA solvers (CapSolver, 2Captcha, AntiCaptcha)
│   ├── email_recovery.py      # Recovery email management and verification
│   └── voice_verification.py  # Voice verification as SMS alternative
│
├── warming/                   # Account warming & reputation building
│   ├── __init__.py
│   ├── activity_simulator.py  # Gmail activity simulation (read, compose, organize)
│   ├── google_services.py     # YouTube + Google Search warmup engines
│   └── reputation_builder.py  # Sender score and trust reputation builder
│
├── api/                       # REST API and dashboard
│   └── ...
│
├── output/                    # Output files (excluded from Git)
│   ├── successful_accounts.json
│   ├── failed_attempts.json
│   └── metrics.json
│
├── credentials/               # Encrypted credentials (excluded from Git)
│   ├── accounts.enc
│   └── .vault.key
│
└── logs/                      # Runtime logs (excluded from Git)
    └── gmail_factory_YYYYMMDD.log
```

---

## 📋 Requirements

### System Requirements

| Requirement | Minimum Version |
|-------------|----------------|
| **Python** | 3.9+ |
| **Chrome** | 120+ (for `undetected-chromedriver`) |
| **RAM** | 4 GB (8 GB recommended for Batch Mode) |
| **OS** | Windows 10/11 · Linux · macOS |

### Optional External Requirements

- **CloakBrowser** — for maximum stealth performance
- **SMS API Key** — from any supported provider
- **CAPTCHA API Key** — CapSolver, 2Captcha, or AntiCaptcha
- **Residential Proxies** — for optimal results

---

## 🚀 Installation

### Step 1 — Clone the repository
```bash
git clone https://github.com/ShadowHackrs/Gmail-infinity.git
cd Gmail-infinity
```

### Step 2 — Create a virtual environment (recommended)
```bash
# Windows
python -m venv venv
venv\Scripts\activate

# Linux / macOS
python3 -m venv venv
source venv/bin/activate
```

### Step 3 — Install dependencies
```bash
pip install -r requirements.txt
```

### Step 4 — Install Playwright browser
```bash
playwright install chromium
```

### Step 5 — Install CloakBrowser (recommended)
```bash
pip install "cloakbrowser>=0.3.15"

# Full support with GeoIP auto-detection
pip install "cloakbrowser[geoip]"
```

### Step 6 — Install SMS and CAPTCHA providers (optional)
```bash
# SMS providers
pip install fivesim smsactivateru

# CAPTCHA solvers
pip install 2captcha-python anticaptchaofficial capsolver
```

---

## ⚙️ Configuration

### Master Configuration File: `config/settings.yaml`

```yaml
# ===========================
#  Gmail Infinity Factory 2026
#  Master System Configuration
# ===========================

system:
  max_concurrent_creations: 5   # Max parallel account creation workers
  headless_mode: true           # Run browser headless (true = faster)
  debug_mode: false             # Verbose debug logging

verification:
  sms:
    primary_provider: "5sim"    # Primary SMS provider
    5sim:
      api_key: "YOUR_5SIM_API_KEY"
    sms_activate:
      api_key: "YOUR_SMS_ACTIVATE_API_KEY"
  captcha:
    provider: "capsolver"
    capsolver:
      api_key: "YOUR_CAPSOLVER_API_KEY"

proxy:
  provider: "residential_pool"  # residential / datacenter / mobile
  rotation_interval: 1          # Rotate proxy after every X operations
```

### Proxy List: `config/proxies.txt` (Static Pool)

```
# Proxy format:
# protocol://host:port
# protocol://user:pass@host:port

http://192.168.1.1:8080
socks5://user:password@proxy.example.com:1080
https://residential.proxy.com:3128
```

### KKOIP Dynamic Pool

The static pool above is kept separate from the KKOIP dynamic residential pool.
The existing `KOOIP_*` environment keys and `kooip` pool identifier are retained
for compatibility. Correcting the display name does not change the existing
gateway or authentication implementation; use your provider's actual gateway
settings rather than assuming that the legacy default below is appropriate.
Enable the dynamic pool via environment variables (credential gateway mode — no
whitelist or API signing required):

```bash
KOOIP_ENABLED=True
KOOIP_USER_ID=123456789          # provider user ID
KOOIP_AUTH_NAME=abcdefg          # global security auth username
KOOIP_AUTH_PASSWORD=abcdefg1234  # global security auth password
KOOIP_COUNTRY=US                 # US / US_California / US_California_city_LosAngeles / global
KOOIP_GATEWAY=gate.kookeey.info  # legacy default; replace with your provider's gateway
KOOIP_GATEWAY_PORT=1000
KOOIP_SESSION_POOL_SIZE=10       # number of sticky sessions in the pool
KOOIP_STICKY_SESSION=True        # False = rotate exit IP on every request
KOOIP_ROTATE_INTERVAL=           # "" (none) / 5m / 1h auto-rotation per session
PROXY_POOL_PREFERENCE=auto       # auto / static / kooip
```

Unhealthy KKOIP sessions are automatically replaced with fresh ones; static
proxies are blacklisted as before.

### Setting Up API Keys

Open `config/settings.yaml` and enter your keys in the appropriate sections:
- **5sim.net** → `verification.sms.5sim.api_key`
- **CapSolver** → `verification.captcha.capsolver.api_key`

---

## 🖥️ Usage

### Basic Launch (Web)
```bash
python auto_gmail_creator.py
```

Open `http://127.0.0.1:8080` in a browser and sign in using the configured
administrator password. Set `--host` and `--port` when needed.

### Recommended Workflow

1. Open **System Configuration** and **Proxy Management** to prepare settings.
2. Use **Tools & Services** for configuration validation and optional data migration.
3. Start an authorized task from **Create Accounts**.
4. Follow progress, results and cancellation controls in **Tasks & Logs**.
5. Use **Account Management** for health checks, warming and exports.
6. Use **Tools & Services** to inspect, resume or clear an interrupted serial batch.
7. Log out when finished. Logging out does not stop server-side tasks.

---

## 🔧 Module Descriptions

### `core/` — Stealth Engine

| File | Description |
|------|-------------|
| `stealth_browser.py` | Full stealth browser framework — integrates CloakBrowser and Playwright with JavaScript injection to bypass all detection |
| `behavior_engine.py` | Human behavior simulation — Bézier curve mouse movement, randomized typing, natural scroll patterns |
| `fingerprint_generator.py` | Fingerprint generator — User-Agent, Screen resolution, GPU, AudioContext, and Font fingerprints |
| `detection_evasion.py` | Detection bypass layer — fixes `navigator.webdriver`, CDP artifacts, and headless browser leaks |
| `cloak_launcher.py` | CloakBrowser launcher with seamless automatic fallback to Playwright |
| `proxy_manager.py` | Advanced proxy manager with rotation, health-checking, per-proxy statistics, and auto-blacklisting |

### `verification/` — Verification Layer

| File | Description |
|------|-------------|
| `sms_providers.py` | Full API clients for 5sim, sms-activate, and TextVerified |
| `captcha_solver.py` | CAPTCHA solving via CapSolver, 2Captcha, and AntiCaptcha |
| `email_recovery.py` | Recovery email management and verification flow |
| `voice_verification.py` | Voice-based phone verification as an alternative to SMS |

### `warming/` — Account Warming

| File | Description |
|------|-------------|
| `activity_simulator.py` | Full Gmail activity simulation — reading, composing, and organizing emails |
| `google_services.py` | YouTube watch session and Google Search simulation engines |
| `reputation_builder.py` | Sender score and account trust reputation builder |

---

## 📡 Supported Providers

### SMS Providers

| Provider | URL | Notes |
|----------|-----|-------|
| 5sim.net | https://5sim.net | **Recommended** — highest success rate |
| sms-activate | https://sms-activate.ru | Reliable — large number pool |
| TextVerified | https://textverified.com | Real US phone numbers |

### CAPTCHA Solvers

| Provider | URL | Notes |
|----------|-----|-------|
| CapSolver | https://capsolver.com | **Recommended** — supports reCAPTCHA v3 |
| 2Captcha | https://2captcha.com | Reliable and fast |
| AntiCaptcha | https://anti-captcha.com | Good alternative |

---

## ⚠️ Legal Disclaimer

> **Important — Read before using**

This project was created strictly for **security research** and **technical testing** purposes.

- **Terms of Service Violation:** Automated Gmail account creation violates [Google's Terms of Service](https://policies.google.com/terms). Violating these terms may result in account suspension and legal consequences.
- **Legal Responsibility:** The end user bears full and sole responsibility for any misuse or unlawful application of this software.
- **Isolated Environments Only:** This tool must only be used in isolated test environments or within legally and officially authorized boundaries.
- **No Commercial Resale:** Selling accounts created with this tool for commercial purposes is strictly prohibited.

> **The developer is not responsible** for any illegal use or misuse of this software.

---

## 📄 Copyright

```
╔══════════════════════════════════════════════════════════════╗
║                    COPYRIGHT NOTICE                          ║
╠══════════════════════════════════════════════════════════════╣
║                                                              ║
║   Gmail Infinity Factory 2026                                ║
║   Version: 2026.1.0                                         ║
║                                                              ║
║   Copyright (c) 2026 Shadow (ShadowHacker0)                 ║
║   All Rights Reserved.                                       ║
║                                                              ║
║   This software and all its source files, modules,          ║
║   documentation, and associated assets are the exclusive    ║
║   intellectual property of their author, Shadow.            ║
║                                                              ║
║   THE FOLLOWING ARE STRICTLY PROHIBITED:                     ║
║   ✗ Copying, distributing, or republishing any part of      ║
║     this codebase without prior written permission          ║
║   ✗ Commercial use without an explicit license agreement    ║
║   ✗ Claiming authorship or presenting under another name    ║
║   ✗ Integrating into commercial or open-source products     ║
║                                                              ║
║   PERMITTED USES:                                            ║
║   ✓ Personal use for educational and research purposes      ║
║   ✓ Reading the code for learning                           ║
║   ✓ Contributing improvements via Pull Requests to the      ║
║     official repository                                     ║
║                                                              ║
║   Contact & Licensing:                                       ║
║   GitHub  →  https://github.com/ShadowHacker0               ║
║                                                              ║
╚══════════════════════════════════════════════════════════════╝
```
## 🎥 Demo Video

[![Watch Demo](https://img.youtube.com/vi/Ovz5KMg086k/maxresdefault.jpg)](https://www.youtube.com/watch?v=Ovz5KMg086k)

---

## 🌐 Official Website

🔗 https://www.shadowhackr.com/2026/04/gmail-2026.html

---

<div align="center">
**© 2026 Shadow Hacker - All Rights Reserved**

[Website](https://www.shadowhackr.com) • [Facebook](https://www.facebook.com/ShadowHackr) • 
**Built with ❤️ and ☕ by Shadow**

*"Stealth is an art. Automation is a science. We combine both."*

[![GitHub](https://img.shields.io/badge/GitHub-ShadowHacker0-black?logo=github)](https://github.com/ShadowHackrs)

</div>
