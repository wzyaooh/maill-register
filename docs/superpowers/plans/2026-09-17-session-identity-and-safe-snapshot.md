# 会话绑定身份与安全快照修复 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Every step uses checkbox (`- [ ]`) syntax for tracking. Each task ends with a separate Chinese commit and an independently runnable test cycle.

**Goal:** 修复 SQLite 快照竞态，建立版本化且绑定当前浏览器会话的身份协议，并让 Playwright、Selenium、registration、health checker、warmer、重试、迁移和 CI 共同遵守同一组安全不变量。

**Architecture:** `core/session_identity.py` 隔离固定 Google 浏览器身份 provider 的传输、解析和会话槽位关联；`BrowserProfileKernel` 在单一 manifest 选择的 engine、profile、proxy 和 lease 内调用该 provider。快照发布使用同目录随机临时文件和平台 no-clobber 发布适配器；任务 ledger、迁移投影和 CI constraints 只保存有限的非敏感事实。

**Tech Stack:** Python 3.9+, SQLite/WAL, `dataclasses`, `asyncio`, Playwright, Selenium, unittest, Flask worker, GitHub Actions, POSIX `link`/`fsync` 和 Windows 同卷 no-clobber 发布语义。

**Spec:** `docs/superpowers/specs/2026-09-17-session-identity-and-safe-snapshot-design.md`

## Global Constraints

- `profile_id` 是数据库与 manifest 的唯一绑定键；`profile_path` 只保留兼容字段，不能作为身份 API。
- manifest 决定 engine；调用方不能覆盖已登记的 engine、profile 或 proxy binding。
- 浏览器启动、身份探测、必要登录、活动、临时页/窗口关闭、进程检查和 lease 释放必须处于同一个 profile lease。
- Playwright 与 Selenium 都必须保留，并各自复用注册时登记的 engine、profile 和 proxy；不得跨 engine 打开 profile。
- provider 名称固定为 `google_browser_accounts_v1`，endpoint 固定为 `https://accounts.google.com/ListAccounts?gpsia=1&source=ChromiumBrowser&json=standard`。
- provider 导航超时上限为 15 秒，响应正文上限为 262144 bytes，账号记录上限为 32 条。
- provider 解析失败、协议格式变化、身份含糊、槽位无法确认、临时页关闭失败、窗口恢复失败和 lease 释放未确认时必须 fail-closed。
- 不得从 `[data-email]`、`aria-label`、`title`、body text、邮件正文、发件人、收件人、账号切换器、Cookie 名称或 adapter 自报的 `authenticated=True` 形成身份事实。
- `SessionIdentityProof` 只在当前进程和当前调用栈有效；数据库、manifest、任务结果和日志只保存 provider 版本、时间和有限错误码等非敏感审计元数据。
- Appium 继续 fail-closed；旧的仅传 `profile_path` warmer/identity API 不得恢复。
- 测试只使用临时 SQLite、临时 profile、localhost 和 fake provider；不访问真实 Gmail、短信供应商、Telegram、生产代理、生产凭据或用户 profile。
- 日志、通知、ledger、manifest、普通 API 和迁移报告不得包含密码、OTP、代理凭据、Cookie、provider payload、账号列表、页面源码或绝对 profile 路径。
- 每项修改遵循 RED -> GREEN -> REFACTOR；每个任务使用单独中文提交，不自动 push 或触发外部流程。

## 文件总览

本计划只允许下列文件在对应任务中创建或修改；执行者先确认工作区中没有同名未提交用户改动。

| 任务 | 文件 | 职责 |
| --- | --- | --- |
| 1 | `core/database_backup.py`、`tests/test_database_backup.py` | 同目录临时快照、完整性校验、跨平台无覆盖发布和失败清理 |
| 2 | `core/session_identity.py`、`core/secret_safety.py`、`tests/test_session_identity.py` | provider 常量、严格解析、槽位关联、不可伪造 proof 和安全错误边界 |
| 3 | `core/profile_runtime.py`、`core/account_warmer.py`、`core/runners.py`、`core/selenium_runner.py`、`core/health_checker.py`、`tests/test_browser_probe_contract.py`、`tests/test_warmer_manifest_contract.py`、`tests/test_health_contract.py`、`tests/test_profile_registration_lifecycle.py`、`tests/test_session_identity_engines.py` | 两引擎 transport、同 lease 生命周期、registration/health/warm 接线和清理语义 |
| 4 | `core/retry_engine.py`、`core/job_ledger.py`、`core/database.py`、`core/secret_safety.py`、`web/worker.py`、`tests/test_job_ledger.py`、`tests/test_operation_execution_invariants.py` | `identity_unavailable` 分类、attempt/job ledger 结果投影和有界 retry engine |
| 5 | `core/database.py`、`core/account_manager.py`、`tests/test_profile_persistence.py`、`tests/test_migration_contract.py` | 逐行事务迁移、严格 `profile_id` 类型、可信状态投影和结构化迁移统计 |
| 6 | `constraints/python3.9.txt`、`constraints/python3.10.txt`、`constraints/python3.11.txt`、`constraints/python3.12.txt`、`start.sh`、`.github/workflows/ci.yml`、`docs/dependency-matrix.md`、`tests/test_ci_dependency_matrix.py`、`tests/test_appium_contract.py`、`tests/test_warmer_manifest_contract.py` | 可复现依赖、Windows CI、Appium/legacy 收口和跨平台门禁 |

---

### Task 1: 备份:实现跨平台无覆盖数据库快照发布

**Files:**

- Modify: `core/database_backup.py:11-51`，保留 `backup_database(source: Path, destination: Path) -> Path` 公共签名，新增内部临时文件和平台发布适配器。
- Modify: `tests/test_database_backup.py:1-140`，把现有最终目标测试改为临时文件发布合同，并增加 inode 复用、竞态、sidecar、同步和取消测试。

**Interfaces:**

- Consumes: 现有 `backup_database(source, destination)` 调用、SQLite `Connection.backup()`、目标父目录 `Path`。
- Produces: `_create_private_temp(destination: Path) -> tuple[int, Path]`、`_publish_noclobber(temp_path: Path, destination: Path) -> None`、`_sync_file(path: Path) -> None`、`_sync_directory(directory: Path) -> None`；发布失败使用固定错误码 `snapshot_publish_unsupported` 或 `snapshot_durability_unconfirmed`，不暴露异常正文。

- [ ] **Step 1: Write the failing test for the Linux replacement-file regression.**

```python
def test_failure_never_unlinks_replacement_even_when_inode_is_reused(self):
    self.create_database()
    original_connect = sqlite3.connect

    def connect(*args, **kwargs):
        if not kwargs.get("uri"):
            self.destination.unlink()
            self.destination.write_bytes(b"replacement sentinel")
            raise sqlite3.OperationalError("synthetic destination failure")
        return original_connect(*args, **kwargs)

    with patch.object(database_backup.sqlite3, "connect", side_effect=connect):
        with self.assertRaises(sqlite3.OperationalError):
            database_backup.backup_database(self.source, self.destination)
    self.assertEqual(self.destination.read_bytes(), b"replacement sentinel")
```

- [ ] **Step 2: Run the focused test to verify the current implementation fails for the right reason.**

Run: `venv/dev/bin/python -m unittest tests.test_database_backup.DatabaseBackupTests.test_failure_never_unlinks_replacement_even_when_inode_is_reused -v`

Expected: FAIL because the current implementation creates the final destination before SQLite work and its `st_dev + st_ino` cleanup can unlink the replacement sentinel.

- [ ] **Step 3: Add race, symlink, sidecar, cancellation, and durability RED cases.**

```python
def test_publish_race_preserves_file_created_after_initial_check(self):
    self.create_database()
    original_publish = database_backup._publish_noclobber

    def racing_publish(temp_path, destination):
        destination.write_bytes(b"racing sentinel")
        return original_publish(temp_path, destination)

    with patch.object(database_backup, "_publish_noclobber", side_effect=racing_publish):
        with self.assertRaises(FileExistsError):
            database_backup.backup_database(self.source, self.destination)
    self.assertEqual(self.destination.read_bytes(), b"racing sentinel")

def test_cancelled_snapshot_removes_only_internal_temp_file(self):
    self.create_database()
    with patch.object(database_backup, "_sync_file", side_effect=KeyboardInterrupt):
        with self.assertRaises(KeyboardInterrupt):
            database_backup.backup_database(self.source, self.destination)
    self.assertFalse(self.destination.exists())
    self.assertEqual(list(self.directory.glob(".snapshot-*.tmp")), [])
```

Also assert that an existing or dangling destination symlink, any `-wal`/`-shm`/`-journal` sidecar, a missing source, an invalid database, a non-`ok` integrity result, and a destination that exists at publish time leave every pre-existing byte unchanged. Patch `_sync_directory` to raise after a successful publish and assert the destination remains while the public call reports `snapshot_durability_unconfirmed`.

- [ ] **Step 4: Run all new RED cases before changing production code.**

Run: `venv/dev/bin/python -m unittest tests.test_database_backup -v`

Expected: the replacement-file test and the new publication tests fail; the failures identify final-target creation/cleanup rather than test fixture errors.

- [ ] **Step 5: Implement same-directory private temporary creation.**

Create a random name with `tempfile.mkstemp(prefix=".snapshot-", suffix=".tmp", dir=str(destination.parent))`, set mode `0600`, and keep the returned descriptor open until the SQLite destination connection is closed. Reject source, destination, parent, and sidecar symlinks with `os.lstat`/`os.path.lexists`; open the source read-only with `source.resolve().as_uri() + "?mode=ro"` and `uri=True`. Never open or unlink the final destination during the backup, integrity check, or exception path.

- [ ] **Step 6: Implement file sync and integrity barriers.**

Run `source_connection.backup(destination_connection)`, execute `PRAGMA journal_mode=DELETE`, require `destination_connection.execute("PRAGMA integrity_check").fetchall() == [("ok",)]`, close both connections, then call `_sync_file(temp_path)`. `_sync_file` opens the temporary path read-only and calls `os.fsync` on POSIX; a platform without the required primitive raises the fixed unsupported error. Error logging prints only `Database snapshot failed.`.

- [ ] **Step 7: Implement platform no-clobber publication.**

On POSIX call `os.link(temp_path, destination)` in the same directory; convert `EEXIST` to `FileExistsError` and remove only `temp_path` in the caller. On Windows use a same-volume rename primitive that refuses an existing destination; if the platform cannot guarantee refusal of overwrite, raise `snapshot_publish_unsupported` instead of calling `os.replace`. After a successful POSIX link, call `_sync_directory(destination.parent)`; if that call fails, preserve the published destination and raise `snapshot_durability_unconfirmed`.

- [ ] **Step 8: Run the focused suite to verify GREEN and inspect cleanup.**

Run: `venv/dev/bin/python -m unittest tests.test_database_backup -v`

Expected: all snapshot tests pass on the current platform, the destination is mode `0600`, the source remains unchanged, no `.snapshot-*.tmp` file remains, and no test observes a replacement sentinel being deleted.

- [ ] **Step 9: Refactor the CLI and comments around the final-target contract.**

Keep `main(argv=None)` returning `1` on all snapshot exceptions and printing only `Database snapshot failed.`; do not include source path, destination path, SQLite error text, or temporary name. Add a docstring that states the caller must provide a new destination and that failure cleanup never touches the final path.

- [ ] **Step 10: Commit the independently reviewed snapshot change.**

Run: `git diff --check && git add core/database_backup.py tests/test_database_backup.py && git commit -m "备份:实现跨平台无覆盖数据库快照发布"`

Expected: the commit contains only Task 1 files and `git show --check --oneline HEAD` succeeds.

### Task 2: 内核:定义版本化浏览器会话身份证明协议

**Files:**

- Create: `core/session_identity.py`，隔离 provider 常量、传输边界无关的 JSON 解析、邮箱规范化、槽位解析和一次性 proof。
- Modify: `core/secret_safety.py:30-55`，把 `identity_unavailable` 纳入安全错误码 allowlist，但不保存 provider payload。
- Create: `tests/test_session_identity.py`，覆盖合法响应、格式变化、大小/深度/数量限制和身份关联。

**Interfaces:**

- Consumes: 固定 endpoint 的完整 `bytes`/`str` 响应、最终 URL、业务页面 Gmail URL、expected email、manifest email。
- Produces: `IDENTITY_PROVIDER = "google_browser_accounts_v1"`、`IDENTITY_PROVIDER_VERSION = 1`、`IDENTITY_ENDPOINT`、`MAX_RESPONSE_BYTES = 262144`、`MAX_RECORDS = 32`、`NAVIGATION_TIMEOUT_MS = 15000`；不可变 `AccountSessionRecord(slot: int, email: str, valid_session: bool)`、`SessionIdentityProof(provider: str, provider_version: int, session_slot: int, observed_email: str, final_origin: str, collected_at: str)`、`IdentityObservation(status: str, proof: Optional[SessionIdentityProof], error_code: str)`；函数 `parse_google_accounts_v1(body: bytes, final_url: str) -> tuple[AccountSessionRecord, ...]`、`resolve_session_identity(records, session_slot, expected_email, manifest_email, final_origin, collected_at=None) -> IdentityObservation`、`parse_gmail_session_slot(url: str) -> Optional[int]`。

- [ ] **Step 1: Write parser and resolver RED tests before creating the module.**

```python
VALID = b')]}\'\n{"accounts":[{"slot":0,"email":"User@Example.test","valid_session":true}]}'

def test_v1_parser_normalizes_email_and_strips_exact_xssi_prefix(self):
    records = parse_google_accounts_v1(VALID, "https://accounts.google.com/ListAccounts")
    self.assertEqual(records, (AccountSessionRecord(0, "user@example.test", True),))

def test_resolver_requires_unique_slot_and_all_expected_emails(self):
    records = (AccountSessionRecord(0, "user@example.test", True),)
    result = resolve_session_identity(
        records, session_slot=0, expected_email="user@example.test",
        manifest_email="user@example.test", final_origin="https://mail.google.com",
    )
    self.assertEqual(result.status, "authenticated")
    self.assertEqual(result.proof.session_slot, 0)
```

- [ ] **Step 2: Run the new module to verify the import and contract fail.**

Run: `venv/dev/bin/python -m unittest tests.test_session_identity -v`

Expected: FAIL with missing `core.session_identity` or missing provider symbols.

- [ ] **Step 3: Add strict parser failure tests with finite error results.**

For each case assert `IdentityObservation.status == "identity_unavailable"` and `error_code == "identity_unavailable"`, and assert `repr(result)` does not contain the input body: malformed JSON, missing top-level `accounts`, non-list accounts, non-integer/negative/boolean slot, non-string email, non-boolean `valid_session`, duplicate slot, duplicate valid email, more than 32 records, a body larger than 262144 bytes, an unknown required key, and a JSON nesting depth greater than 16. Add a login redirect fixture whose final URL is `https://accounts.google.com/signin` and assert `status == "login_required"`; any other Google path, HTTP scheme, port, userinfo, trailing-dot host, or non-Google host must return `identity_unavailable`.

- [ ] **Step 4: Add slot and mismatch tests.**

```python
def test_slot_mismatch_and_page_account_a_cannot_bind_session_b(self):
    records = (
        AccountSessionRecord(0, "a@example.test", True),
        AccountSessionRecord(1, "b@example.test", True),
    )
    result = resolve_session_identity(
        records, session_slot=1, expected_email="a@example.test",
        manifest_email="a@example.test", final_origin="https://mail.google.com",
    )
    self.assertEqual(result.status, "account_mismatch")
    self.assertIsNone(result.proof)

def test_ambiguous_slot_is_unavailable_instead_of_using_array_order(self):
    records = (AccountSessionRecord(0, "a@example.test", True),)
    result = resolve_session_identity(
        records, session_slot=None, expected_email="a@example.test",
        manifest_email="a@example.test", final_origin="https://mail.google.com",
    )
    self.assertEqual(result.status, "identity_unavailable")
```

- [ ] **Step 5: Implement the versioned parser and immutable result types.**

Accept only the exact anti-XSSI prefix `)]}'\n`, standard `json.loads`, the defined object/list/field types, normalized lowercase mailbox addresses, unique slots/emails, and bounded record count/depth. Keep the raw body local to the parser and return only the three fields in each `AccountSessionRecord`; never attach the parsed account list to an exception or log record.

- [ ] **Step 6: Implement strict origin and session-slot association.**

Use `urllib.parse.urlsplit`; accept only HTTPS, empty userinfo, default port, exact host `accounts.google.com` for the provider response and exact application origin host `mail.google.com` for the business page. Parse only `/mail/u/<non-negative integer>/` as the session slot; reject query-string email values and any URL without a unique slot. `resolve_session_identity` must require provider email, expected email, manifest email and valid session to match exactly after normalization, otherwise return `account_mismatch` or `identity_unavailable` without a proof.

- [ ] **Step 7: Implement the private evidence marker and proof lifetime.**

Create a module-private sentinel and attach it only to `SessionIdentityProof` instances constructed by `resolve_session_identity`; expose no setter and do not implement a dict-to-proof coercion. `IdentityObservation` must be immutable and serializable only through its finite `status`/`error_code` fields; `SessionIdentityProof` must not implement JSON export.

- [ ] **Step 8: Run parser tests GREEN and the existing error protocol tests.**

Run: `venv/dev/bin/python -m unittest tests.test_session_identity tests.test_error_protocol -v`

Expected: all provider fixtures pass, malformed inputs fail closed, the proof contains no raw response, and `identity_unavailable` is accepted by `normalize_error_code` while arbitrary text remains rejected.

- [ ] **Step 9: Refactor names and module exports for Task 3.**

Export only the constants, frozen data classes, parser, resolver, and slot parser listed in the interface block. Keep transport-specific Playwright/Selenium code out of this module so protocol changes cannot leak into warmer or health checker.

- [ ] **Step 10: Commit the provider contract.**

Run: `git diff --check && git add core/session_identity.py core/secret_safety.py tests/test_session_identity.py && git commit -m "内核:定义版本化浏览器会话身份证明协议"`

Expected: the commit contains only the parser/error vocabulary and its tests; no browser is launched.

### Task 3: 浏览器:接通双引擎会话身份与生命周期清理

**Files:**

- Modify: `core/profile_runtime.py:2267-2825`，让 `BrowserProfileKernel` 在 health/warm 和注册事实中通过 provider transport 取得当前 proof，并删除通用 DOM 邮箱认证路径。
- Modify: `core/account_warmer.py:499-1060`，删除 `_observed_email_playwright`/`_observed_email_selenium` 的认证用途，保持活动和清理结果在同一 lease 内。
- Modify: `core/runners.py:344-410`，registration 使用 kernel/provider 事实，不再把页面 selector 作为邮箱证据。
- Modify: `core/selenium_runner.py:400-430`，Selenium registration 走同一 resolver。
- Modify: `core/health_checker.py:88-320`，health 只消费 kernel observation，保留 mailbox 独立事实。
- Modify: `tests/test_browser_probe_contract.py`、`tests/test_warmer_manifest_contract.py`、`tests/test_health_contract.py`、`tests/test_profile_registration_lifecycle.py`，更新 engine/lease/provider 合同。
- Create: `tests/test_session_identity_engines.py`，用对称 fake Playwright/Selenium transport 覆盖当前会话与错误页面组合。

**Interfaces:**

- Consumes: Task 2 的 `parse_google_accounts_v1`、`resolve_session_identity`、`parse_gmail_session_slot`；已有 `ProfileRuntime.lease()`、manifest engine/proxy、Playwright context 和 Selenium driver。
- Produces: `BrowserProfileKernel._fetch_identity_playwright(context, business_page, timeout_ms=15000) -> IdentityObservation`、`BrowserProfileKernel._fetch_identity_selenium(driver, timeout_ms=15000) -> IdentityObservation`、`BrowserProfileKernel.probe_playwright(...) -> BrowserObservation`、`BrowserProfileKernel.probe_selenium(...) -> BrowserObservation`；两种 observation 都必须携带非序列化的 proof marker，清理失败时统一返回 `cleanup_failed`。

- [ ] **Step 1: Add the failing Playwright and Selenium symmetry tests.**

```python
def test_page_email_a_cannot_override_provider_session_b_for_both_engines(self):
    for engine in ("playwright", "selenium"):
        with self.subTest(engine=engine):
            runtime, handle = make_ready_runtime(engine, "a@example.test")
            fake = make_engine_fixture(
                engine=engine,
                business_url="https://mail.google.com/mail/u/1/#inbox",
                dom_email="a@example.test",
                provider_body=provider_body(slot=1, email="b@example.test"),
            )
            with install_fake_engine(engine, fake):
                observation = probe_with_kernel(runtime, handle, engine, "a@example.test")
            self.assertEqual(observation.status, "account_mismatch")
            self.assertFalse(observation["authenticated"])
            self.assertNotEqual(observation.get("observed_email"), "a@example.test")
```

Also add a fake provider with two records and no unique current slot; assert `identity_unavailable`, and a provider redirect to `/signin`; assert `login_required`. These tests intentionally include `a@example.test` in DOM attributes and body text to prove the DOM is diagnostic only.

- [ ] **Step 2: Run the new symmetry tests to verify the current selector path fails.**

Run: `venv/dev/bin/python -m unittest tests.test_session_identity_engines -v`

Expected: FAIL because current kernel code calls `[data-email], [aria-label*="@"], [title*="@"]` and can accept the DOM address without a provider proof.

- [ ] **Step 3: Add lifecycle RED tests for the transport and lease boundary.**

Assert for both engines that the provider endpoint is exactly `IDENTITY_ENDPOINT`, navigation timeout is `15000` ms, response reads stop at `262144` bytes, no cookies are copied to an HTTP client, temporary Playwright pages are closed, Selenium window handles return to the original set, and all provider activity occurs after `lease-enter` and before `lease-exit`. Add cancellation and close-failure cases that assert `cleanup_failed`, `lease_released == False`, profile state `cleanup_failed`, and no successful warm result even when the provider had already returned a matching email.

- [ ] **Step 4: Run the existing browser/warmer/health lifecycle tests RED.**

Run: `venv/dev/bin/python -m unittest tests.test_browser_probe_contract tests.test_warmer_manifest_contract tests.test_health_contract tests.test_profile_registration_lifecycle -v`

Expected: the new provider-required assertions fail while unrelated mailbox tests continue to run; the failure points to missing transport/proof integration.

- [ ] **Step 5: Implement Playwright temporary-page transport inside the held lease.**

In `BrowserProfileKernel.probe_playwright` and the warm path, retain the business page, create a temporary page from the same persistent context, navigate only to the fixed endpoint with `timeout=15000`, validate the final provider URL, read at most `262144` bytes without truncating a parseable response, close the temporary page in an uncancelled `finally`, and pass the complete body to `parse_google_accounts_v1`. Do not call `requests`, `aiohttp`, or a second browser context.

- [ ] **Step 6: Implement Selenium temporary-tab transport inside the held lease.**

In `BrowserProfileKernel._fetch_identity_selenium`, save `driver.current_window_handle` and the complete handle tuple, open a new tab through the same driver/profile, navigate to the fixed endpoint, validate final URL and body size, close the temporary tab, restore the original handle, and assert the handle set equals the saved tuple. A tab close or handle restoration error returns `IdentityObservation("cleanup_failed", None, "cleanup_failed")` and quarantines the profile.

- [ ] **Step 7: Replace kernel selectors with provider proof plus existing shell/cookie facts.**

Keep `_playwright_application_shell` and `_selenium_application_shell` as diagnostic shell checks. Remove `_playwright_identity`, `_selenium_identity`, `_observed_email_playwright`, and `_observed_email_selenium` from the authentication decision. Build `BrowserObservation` from provider `observed_email`, `session_slot`, trusted origin, cookie validation, application shell, and the private proof marker; a DOM email may remain in an explicitly named diagnostic field only if it is never passed to `classify_session_auth`.

- [ ] **Step 8: Rewire registration, health, and warm to consume the same kernel contract.**

Registration must call the provider after reaching Gmail and before `mark_identity_verified`/`mark_ready`; health must call `probe_playwright` or `probe_selenium` selected by manifest and never enter credentials; warm may enter credentials only for `login_required`, then must call the provider again before activity. Every call validates manifest engine/proxy before adapter import and releases the lease only after adapter/process cleanup is confirmed.

- [ ] **Step 9: Run all browser contract tests GREEN and inspect no-fallback source.**

Run: `venv/dev/bin/python -m unittest tests.test_session_identity_engines tests.test_browser_probe_contract tests.test_warmer_manifest_contract tests.test_health_contract tests.test_profile_registration_lifecycle -v`

Expected: Playwright and Selenium have identical status outcomes for A/B, ambiguous slot, login redirect, malformed provider, close failure, cancellation, and engine/proxy mismatch. `rg -n "querySelectorAll\('\[data-email\]|aria-label\*=" core/profile_runtime.py core/account_warmer.py` finds no authentication call site; any remaining selector is a test fixture or diagnostic-only path with an assertion that it cannot set `observed_email`.

- [ ] **Step 10: Refactor shared cleanup helpers and commit both engines together.**

Make the two transports share bounded URL/body validation and status mapping while retaining engine-specific page/tab operations. Keep Playwright and Selenium in this one release unit; do not commit one engine without the other.

Run: `git diff --check && git add core/profile_runtime.py core/account_warmer.py core/runners.py core/selenium_runner.py core/health_checker.py tests/test_browser_probe_contract.py tests/test_warmer_manifest_contract.py tests/test_health_contract.py tests/test_profile_registration_lifecycle.py tests/test_session_identity_engines.py && git commit -m "浏览器:接通双引擎会话身份与生命周期清理"`

Expected: the commit contains no new public `profile_path` identity argument and all local engine contract tests are green.

### Task 4: 任务:接通身份错误分类与持久化重试

**Files:**

- Modify: `core/retry_engine.py:198-430`，将 `identity_unavailable` 纳入 health/warm 有界 retry allowlist，将 `account_mismatch`、`cleanup_failed` 保持为普通登录重试的终止错误。
- Modify: `core/job_ledger.py:237-850`，确保 attempt result 只保存有限错误码和非敏感 metadata，owner-fenced finish 不接受外部 owner。
- Modify: `core/database.py:20-50, 666-850`，增加 `identity_unavailable` browser/error vocabulary，禁止把它投影成 `active`。
- Modify: `core/secret_safety.py:30-55`，同步错误码 allowlist。
- Modify: `web/worker.py:121-570`，把 provider/cleanup 结果接入 `_operation_result_outcome` 和 `_run_retryable_operation`。
- Modify: `tests/test_job_ledger.py`、`tests/test_operation_execution_invariants.py`，补齐结果类型、owner fence、retry 和状态投影测试。

**Interfaces:**

- Consumes: Task 3 的 `BrowserObservation.status` 和 warmer result `{success: bool, error_code, cleanup_status, browser_process_stopped, lease_released}`。
- Produces: `RetryEngine.retryable_errors("health"|"warm"|"compensation") -> frozenset[str]` 包含 `identity_unavailable`；`JobLedger.finish_attempt(...)` 对 owner 和有限 error code 原子校验；`web.worker._operation_result_outcome(action, result) -> tuple[bool, str]` 对非布尔 success、缺少清理事实和未知 provider 状态返回 `invalid_reconciliation_result`；`DatabaseManager.update_health_snapshot` 持久化 `identity_unavailable` 而不提升总体信任。

- [ ] **Step 1: Add RED tests for status and retry classification.**

```python
def test_identity_unavailable_is_bounded_retry_but_mismatch_and_cleanup_stop(self):
    engine = RetryEngine()
    self.assertTrue(engine.should_retry("identity_unavailable", 1, operation="warm"))
    self.assertFalse(engine.should_retry("account_mismatch", 1, operation="warm"))
    self.assertFalse(engine.should_retry("cleanup_failed", 1, operation="warm"))

def test_worker_rejects_truthy_string_success_and_missing_cleanup_facts(self):
    self.assertEqual(
        _operation_result_outcome("warm", {"success": "true"}),
        (False, "invalid_reconciliation_result"),
    )
    self.assertEqual(
        _operation_result_outcome("warm", {"success": True}),
        (False, "invalid_reconciliation_result"),
    )
```

- [ ] **Step 2: Add RED tests for durable ownership and subject fact preservation.**

Create two `JobLedger` instances over one temporary SQLite file. Start an attempt with worker `owner-a`; assert `finish_attempt(..., worker_id="owner-b")` raises `ValueError` and leaves the row `running`. Finish it with `owner-a`; assert `subject`, `account_id`, `engine`, `profile_id`, `proxy_label`, and normalized `identity_unavailable` remain readable. Add a completed subject test proving a subsequent worker cannot create a second running attempt for the same logical operation.

- [ ] **Step 3: Run focused RED tests against the current policy.**

Run: `venv/dev/bin/python -m unittest tests.test_job_ledger tests.test_operation_execution_invariants -v`

Expected: identity retry, strict result, and owner-fence assertions fail while existing creation retry tests pass.

- [ ] **Step 4: Extend the finite error vocabularies.**

Add `identity_unavailable` to `SAFE_ERROR_CODES`, `BROWSER_STATUSES`, `_BROWSER_STATUS_VALUES`, worker transient operation errors, and `RetryEngine.OPERATION_RETRYABLE_ERRORS["health"]`/`["warm"]`. Keep `account_mismatch`, `cleanup_failed`, `login_required`, `challenge`, `profile_conflict`, and `profile_unavailable` in the non-retryable set. Normalize unknown strings to `invalid_reconciliation_result` at the worker boundary and to an empty/unknown durable error at the database boundary.

- [ ] **Step 5: Enforce strict warm/health reconciliation.**

In `_operation_result_outcome`, require `type(result["success"]) is bool` for warm and require `cleanup_status == "completed"`, `browser_process_stopped is True`, and `lease_released is True` before returning success. A complete health snapshot may be successful with `browser_status == "identity_unavailable"` and an independent mailbox fact, but `derive_overall_status` must return `degraded`, `error`, or `network_error`, never `active`, when the browser fact is unavailable.

- [ ] **Step 6: Keep owner-fenced attempt finalization atomic.**

Use `BEGIN IMMEDIATE` and `WHERE attempt_id=? AND state='running' AND worker_id=?` for ordinary `finish_attempt` updates. A supervisor cleanup path may finalize a stale worker only when it supplies an explicit `supervisor=True`/verified stale claim accepted by the existing ledger API; ordinary completion cannot downgrade an owner mismatch into a compatibility success. Persist only `_safe_attempt_metadata` fields and finite error codes.

- [ ] **Step 7: Connect the retry engine to operation execution.**

Ensure `_run_retryable_operation` records `identity_unavailable` as a durable retry decision with bounded cooldown, re-reads the scheduled attempt before retrying, and returns the persisted result when another owner has claimed it. Ensure `account_mismatch` and `cleanup_failed` finish with `retry_decision="stop"`; cleanup failure remains visible even if the adapter reported business success.

- [ ] **Step 8: Run the focused suite GREEN and inspect database projections.**

Run: `venv/dev/bin/python -m unittest tests.test_job_ledger tests.test_operation_execution_invariants tests.test_error_protocol tests.test_health_contract -v`

Expected: all result/owner/retry tests pass; a health row with `mailbox_status="active"` and `browser_status="identity_unavailable"` has `overall_status="degraded"` and `status != "active"`; no ledger row contains provider body, email list, password, cookie, or exception text.

- [ ] **Step 9: Refactor duplicated error sets to one documented allowlist boundary.**

Keep the existing compatibility aliases for creation retry, but make operation policies call `RetryEngine.retryable_errors` and make database/worker code use `normalize_error_code`. Add comments explaining why identity-unavailable retry is bounded and cleanup/mismatch are terminal.

- [ ] **Step 10: Commit task 4.**

Run: `git diff --check && git add core/retry_engine.py core/job_ledger.py core/database.py core/secret_safety.py web/worker.py tests/test_job_ledger.py tests/test_operation_execution_invariants.py && git commit -m "任务:接通身份错误分类与持久化重试"`

Expected: the commit contains the durable policy and its tests, with no browser transport changes.

### Task 5: 迁移:增加逐行回滚与严格身份投影

**Files:**

- Modify: `core/database.py:486-620, 878-930`，收紧 `migration_profile_projection`，新增逐行 savepoint 和结构化报告，同时保留 `run_migration(...) -> int`。
- Modify: `core/account_manager.py:100-180`，新增报告入口并让旧 `migrate_old_data()` 返回 imported 整数。
- Modify: `tests/test_profile_persistence.py:1-380`，保留旧兼容断言并增加坏记录隔离和幂等断言。
- Create: `tests/test_migration_contract.py`，覆盖类型、状态投影、savepoint、冲突分类和报告脱敏。

**Interfaces:**

- Consumes: JSON/TXT legacy records、现有 `DatabaseManager.save_account` 严格 normalizer、临时数据库路径。
- Produces: frozen `MigrationStats(imported: int, skipped: int, invalid: int, conflicted: int)`；`DatabaseManager.run_migration_report(old_json_path="data/accounts.json", old_txt_path="data/accounts.txt") -> MigrationStats`；兼容 `DatabaseManager.run_migration(...) -> int` 返回 `.imported`；`AccountManager.migrate_old_data_report() -> MigrationStats`；`AccountManager.migrate_old_data() -> int` 返回 `.imported`。

- [ ] **Step 1: Add RED tests for strict profile typing and default projection.**

```python
def test_numeric_profile_id_is_invalid_and_does_not_create_a_trusted_row(self):
    source = self.write_json([{
        "email": "numeric@example.test", "password": "secret",
        "profile_id": 12, "status": "active",
    }])
    report = self.db.run_migration_report(str(source), str(self.missing_txt))
    self.assertEqual(report.invalid, 1)
    self.assertIsNone(self.db.get_account_by_profile_id("12"))

def test_nonempty_profile_id_without_native_manifest_is_unknown(self):
    source = self.write_json([{
        "email": "legacy@example.test", "password": "secret",
        "profile_id": "profile-legacy", "profile_path": "/old/profile",
        "status": "active",
    }])
    self.db.run_migration_report(str(source), str(self.missing_txt))
    row = self.db.get_all_accounts()[0]
    self.assertEqual(row["profile_state"], "legacy_unbound")
    self.assertEqual(row["identity_state"], "identity_reconstructed")
    self.assertEqual(row["browser_status"], "not_configured")
    self.assertEqual(row["overall_status"], "unknown")
```

- [ ] **Step 2: Add RED tests for per-record isolation, idempotence, and report secrecy.**

Use one valid record, one record with numeric/array/object/boolean `profile_id`, one duplicate email, and one conflicting profile binding. Assert the valid record imports after the invalid one, the second migration reports `imported == 0`, and `str(report)`/captured logs contain no password, proxy, raw record, absolute source path, or provider text. Verify each record gets its own SQLite savepoint so one failure does not roll back earlier or later valid records.

- [ ] **Step 3: Run migration tests RED.**

Run: `venv/dev/bin/python -m unittest tests.test_migration_contract tests.test_profile_persistence -v`

Expected: current migration either accepts non-string `profile_id`, derives a trusted-looking state, aborts the batch on a bad row, or returns only an integer without the new counters.

- [ ] **Step 4: Implement a strict migration normalizer.**

Before calling `save_account`, require `type(raw_profile_id) is str` when the key is present; reject numbers, booleans, arrays, objects, and empty non-string values as `invalid`. Call existing email/engine/status normalizers. For a nonempty profile id without a complete native manifest and explicit matching status, emit exactly `profile_state="legacy_unbound"`, `identity_state="identity_reconstructed"`, `browser_status="not_configured"`, `overall_status="unknown"`; do not infer `bound`, `ready`, or `active` from path/id/history.

- [ ] **Step 5: Implement per-record transaction and conflict classification.**

Open one transaction for the migration file, issue `SAVEPOINT migration_record_<ordinal>` for each record, release it on success, and roll it back on `ValueError`, duplicate/conflict, or SQLite integrity errors. Increment exactly one of `imported`, `skipped`, `invalid`, `conflicted`; continue to the next record. Never include the original record or exception payload in the report.

- [ ] **Step 6: Implement report and compatibility projections.**

Return `MigrationStats` from the new report methods. Keep the old integer methods as thin wrappers returning `stats.imported` so existing callers and tests remain source-compatible. Keep TXT parsing bounded and classify malformed lines as `invalid` without logging credentials.

- [ ] **Step 7: Run migration tests GREEN and verify native rows remain unchanged.**

Run: `venv/dev/bin/python -m unittest tests.test_migration_contract tests.test_profile_persistence tests.test_profile_reconciliation -v`

Expected: bad rows are isolated, duplicate execution is idempotent, legacy rows project to unknown/unbound defaults, and complete native manifest rows retain their explicit state.

- [ ] **Step 8: Refactor migration logging and operator documentation boundary.**

Log only source kind (`json`/`txt`), counters, and finite error category; do not log filenames, emails, passwords, proxy values, profiles, or raw records. Require operators to run the explicit `database-backup` command before migration; migration code itself must not create an automatic production snapshot.

- [ ] **Step 9: Commit task 5.**

Run: `git diff --check && git add core/database.py core/account_manager.py tests/test_profile_persistence.py tests/test_migration_contract.py && git commit -m "迁移:增加逐行回滚与严格身份投影"`

Expected: only migration/database compatibility code is committed, and the integer API remains available.

### Task 6: 工程:锁定依赖并补充跨平台门禁

**Files:**

- Create: `constraints/python3.9.txt`、`constraints/python3.10.txt`、`constraints/python3.11.txt`、`constraints/python3.12.txt`，固定 requirements 的直接和传递依赖，并用 PEP 508 marker 表达真实 OS 差异。
- Modify: `start.sh:25-55`，按虚拟环境解释器的 minor 版本选择对应 constraints，缺失时以明确错误退出。
- Modify: `.github/workflows/ci.yml:15-130`，为矩阵增加 constraints 输入和 `windows-latest` Python 3.11 核心作业，保留 Ubuntu 双引擎 localhost smoke。
- Modify: `docs/dependency-matrix.md:1-120`，记录四份 constraints、Windows 文件语义门禁、生成/审核命令和真实 staging 限制。
- Modify: `tests/test_ci_dependency_matrix.py`，断言四份 constraints、Windows 矩阵和安装命令存在。
- Modify: `tests/test_appium_contract.py`、`tests/test_warmer_manifest_contract.py`，断言 Appium/legacy `profile_path` 继续 fail-closed。

**Interfaces:**

- Consumes: `requirements.txt`、当前 Python minor、GitHub Actions matrix、Task 1-5 的完整 unittest 和 browser gate。
- Produces: `constraints/python<minor>.txt` 四个可审核输入；`start.sh <dev|prod> --setup` 使用当前解释器对应 constraints；CI 在 Ubuntu/macOS/Windows 安装对应 constraints 并运行完整核心门禁。

- [ ] **Step 1: Add RED tests for dependency and legacy contracts.**

```python
def test_four_minor_constraints_and_windows_gate_are_declared(self):
    for minor in ("3.9", "3.10", "3.11", "3.12"):
        path = ROOT / "constraints" / ("python%s.txt" % minor)
        self.assertTrue(path.is_file())
        self.assertIn("playwright==", path.read_text(encoding="utf-8"))
    workflow = (ROOT / ".github/workflows/ci.yml").read_text(encoding="utf-8")
    self.assertIn("windows-latest", workflow)
    self.assertIn("constraints/python3.11.txt", workflow)
```

Add source-level assertions that public `warm_account`, `warm_account_playwright`, and `warm_account_selenium` signatures do not contain `profile_path`, and that `run_appium_flow` returns the finite `unsupported` result before importing/starting a device session.

- [ ] **Step 2: Run the dependency/legacy tests RED.**

Run: `venv/dev/bin/python -m unittest tests.test_ci_dependency_matrix tests.test_appium_contract tests.test_warmer_manifest_contract -v`

Expected: constraints files and the Windows job are absent; any test that detects a reintroduced path-only identity call fails.

- [ ] **Step 3: Produce and review the four constraints files.**

For each supported minor version, resolve `requirements.txt` in a clean temporary virtual environment using the matching interpreter, record every direct and transitive package with exact versions, retain markers only for actual OS/Python differences, and run `python -m pip check`. Review that Playwright, Selenium, Appium client, Flask, SQLite-adjacent packages, and their transitive dependencies are all represented; do not use one macOS `pip freeze` as a universal file.

- [ ] **Step 4: Make `start.sh` choose constraints deterministically.**

During `--setup`, compute the selected interpreter minor with `"$BOOTSTRAP" -c 'import sys; print("%d.%d" % sys.version_info[:2])'`, require it to be one of `3.9`, `3.10`, `3.11`, `3.12`, set `CONSTRAINTS="$ROOT/constraints/python${PY_MINOR}.txt"`, fail with `Unsupported Python minor; supported versions are 3.9, 3.10, 3.11, 3.12.` when absent, and install with `-r requirements.txt -c "$CONSTRAINTS"`. Apply the same selection to the existing virtual environment before `playwright install chromium`.

- [ ] **Step 5: Extend CI with explicit constraint paths and Windows core semantics.**

Add `constraint` to every matrix entry; use `python -m pip install -r requirements.txt -c ${{ matrix.constraint }}`. Add `windows-latest`/Python `3.11` with `constraints/python3.11.txt`; run full unittest, SQLite no-clobber/cancellation tests, `compileall`, Node syntax, `pip check`, and `git diff --check`. Keep the Ubuntu `real-browser-smoke` job, provision matching Chrome/ChromeDriver, and do not provide real service credentials.

- [ ] **Step 6: Update dependency documentation and explicit maintenance command.**

Document the command `python -m pip install -r requirements.txt -c constraints/python3.<minor>.txt` for validation, the clean-environment process used to refresh a constraints file, supported Python/OS matrix, Windows file-lock semantics, and the fact that Google staging remains opt-in and unverified by default. State that constraints do not prove external provider compatibility.

- [ ] **Step 7: Re-run legacy/Appium RED cases after wiring constraints.**

Run: `venv/dev/bin/python -m unittest tests.test_appium_contract tests.test_warmer_manifest_contract tests.test_warmer_operation_integrity -v`

Expected: Appium remains `unsupported`, path-only warmer invocation is rejected, manifest engine/proxy mismatch is rejected before adapter launch, and all cleanup/lease assertions remain green.

- [ ] **Step 8: Run the complete local verification gate.**

Run:

```bash
venv/dev/bin/python -m unittest discover -s tests -p 'test*.py' -v
GMAIL_CONFIG_FROM_ENV=1 venv/dev/bin/python -m tools.run_browser_smoke
venv/dev/bin/python -m compileall -q core services web tests tools
node --check web/static/app.js
venv/dev/bin/python -m pip check
git diff --check
```

Expected: every unittest and both engine smoke paths execute rather than skip; missing browser binaries or skipped required tests produce a nonzero smoke result; no command contacts Gmail, SMS, Telegram, production proxy, or user profile.

- [ ] **Step 9: Refactor CI duplication and record remote limitation.**

Keep the matrix readable by using one constraint field per job, one shared dependency installation shape, and one explicit Windows job. In `docs/dependency-matrix.md`, record that GitHub Actions must still run after these commits; local green does not claim remote CI is green until the Ubuntu inode regression and Windows job complete remotely.

- [ ] **Step 10: Commit the engineering gate.**

Run: `git diff --check && git add constraints start.sh .github/workflows/ci.yml docs/dependency-matrix.md tests/test_ci_dependency_matrix.py tests/test_appium_contract.py tests/test_warmer_manifest_contract.py && git commit -m "工程:锁定依赖并补充跨平台门禁"`

Expected: the commit contains constraints, launcher/CI/docs/test changes only and leaves external services untouched.

## Final verification and spec coverage review

After all six commits, run the complete local verification commands from Task 6 again and inspect each approved design requirement against the task mapping:

- Snapshot race, inode reuse, symlink/sidecar refusal, atomic no-clobber publication, directory durability, cancellation, and private permissions are covered by Task 1.
- Fixed provider endpoint, 15-second navigation, 262144-byte response cap, 32-record cap, anti-XSSI/parser/type/depth checks, exact origins, slot ambiguity, proof lifetime, and no DOM fallback are covered by Task 2 and Task 3.
- Playwright/Selenium same-engine manifest selection, same lease, temporary page/tab cleanup, post-login re-probe, process checks, lease release, and cleanup precedence are covered by Task 3.
- `identity_unavailable`, `account_mismatch`, `login_required`, `challenge`, and `cleanup_failed` persistence/retry behavior is covered by Task 4.
- Legacy defaults, strict string `profile_id`, per-record savepoints, idempotence, and integer compatibility are covered by Task 5.
- Four constraints, deterministic setup selection, Windows CI, Ubuntu browser smoke, Appium fail-closed, and legacy API closure are covered by Task 6.

Run the following self-review commands before claiming the plan complete:

```bash
rg -n "TBD|TODO|implement[[:space:]]+later|稍后处理|适当处理|写测试覆盖上述内容|Similar[[:space:]]+to[[:space:]]+Task" docs/superpowers/plans/2026-09-17-session-identity-and-safe-snapshot.md | grep -v "rg -n"
git diff --check
```

Expected: the placeholder scan prints no matching lines and `git diff --check` exits zero. Then compare every function/type named in a later task with the producing task's interface block; names and field types must match exactly.

## Execution handoff

Plan complete and saved to `docs/superpowers/plans/2026-09-17-session-identity-and-safe-snapshot.md`. Two execution options:

1. **Subagent-Driven (recommended)** — dispatch a fresh worker for each task and perform a two-stage review between commits.
2. **Inline Execution** — execute the tasks in this session using `superpowers:executing-plans`, with RED/GREEN checkpoints after each task.

Choose one execution approach before production code changes begin.
