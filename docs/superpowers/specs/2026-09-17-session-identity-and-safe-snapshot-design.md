# 会话绑定身份与安全快照修复设计

**状态：** 待用户审阅
**日期：** 2026-09-17
**关联设计：** `2026-09-11-browser-profile-kernel-design.md`、
`2026-09-17-continuous-recovery-design.md`

## 1. 背景

现有实现已经建立 `profile_id`、manifest、engine、proxy binding 和
profile lease，并让 Playwright 与 Selenium 在注册、健康检查和养号中复用
各自登记的浏览器 profile。但是，最终验收发现两个发布阻塞问题：

1. SQLite 快照失败清理通过 `st_dev + st_ino` 判断最终目标是否仍是本进程
   创建的文件。Linux 可能在删除并重建文件时立即复用 inode，当前实现会
   误删后来出现的替换文件。提交 `b27457d` 的 GitHub Actions 中，Ubuntu
   Python 3.9、3.10、3.11、3.12 均稳定失败在这一边界。
2. 浏览器身份仍从页面上的通用 `[data-email]`、`aria-label` 或 `title`
   属性提取邮箱。页面中的邮件正文、发件人、收件人、隐藏元素或账号切换器
   都可能提供与当前活动 Google 会话无关的邮箱。有效 Cookie、应用壳和
   错误的邮箱字符串可能被组合成假 `authenticated`。

此外，旧数据库迁移仍会对部分空状态记录给出偏宽的 `bound/active` 投影，
依赖安装没有可复现约束，Windows 分支也没有真实 CI runner 验证。

本设计是现有 profile kernel 的加固，不替换其 engine/profile/lease 所有权
模型。实现必须继续同时支持 Playwright 和 Selenium，并复用注册时记录的
同一 engine、profile 和 proxy binding。

## 2. 已确认决策

当前阶段不引入 OAuth/OIDC。采用版本化的 Google 浏览器会话 provider，
在同一浏览器会话和同一 profile lease 中取得结构化账号信息，并严格
fail-closed。

这项决策包含以下限制：

- Google 浏览器内部协议不是公开、长期稳定的产品契约。
- provider 必须隔离协议变化，不能让响应结构渗透到 warmer、health checker
  或 registration。
- 协议不可用、格式变化、账号槽位含糊或无法取得唯一身份时，操作必须返回
  明确失败，不能退回通用 DOM 邮箱字符串。
- OAuth/OIDC 仍是未来可替换的正式身份 provider，但不属于本轮范围。

## 3. 目标

1. 修复跨平台 SQLite 快照发布竞态，任何失败路径都不能覆盖或删除后来出现的
   最终目标文件。
2. 建立统一、版本化、会话绑定的浏览器身份证明协议。
3. 让 registration、health checker 和 warmer 只消费内核身份证明，不再
   独立解析页面邮箱。
4. 对身份不可用、登录缺失、身份不匹配和清理失败进行不同的持久化、重试和
   运维投影。
5. 收紧旧数据迁移，不从不完整元数据制造可信状态，并让坏记录彼此隔离。
6. 增加可复现依赖输入和真实 Windows CI，恢复完整远程合并门禁。

## 4. 非目标

- 本轮不访问真实 Gmail、真实短信供应商、Telegram、生产代理或用户 profile。
- 本轮不自动迁移、复制或备份生产数据库。
- 不把 Appium 开放为账号创建引擎；Appium 继续 fail-closed。
- 不恢复只传 `profile_path` 的身份 API。
- 不在 Playwright 与 Selenium 之间转换 profile。
- 不解析、导出或持久化原始 Cookie、Google 响应正文、账号列表或令牌。
- 不承诺 Google 内部浏览器协议永久兼容。
- 不把依赖锁定等同于外部服务兼容性证明。

## 5. 全局不变量

以下规则对所有实现阶段生效：

- `profile_id` 是数据库与 manifest 的唯一绑定键，`profile_path` 不是身份。
- manifest 决定 engine；调用方不能覆盖已登记 engine。
- 浏览器启动、身份探测、必要登录、活动和关闭必须处于同一个 profile lease。
- 浏览器及临时身份页必须在释放 lease 前确认关闭。
- 清理失败优先于业务成功，不能返回假成功。
- 当前活动会话身份必须在每次 registration 最终确认、health probe 和 warm
  操作中重新观察，不能只相信历史 `identity_verified` 位。
- 身份证据只在当前进程和当前调用内有效；持久化层只保留非敏感审计元数据。
- 所有测试使用临时 SQLite、临时 profile、localhost 和 fake provider。
- 日志、任务结果、通知、manifest 和普通 API 不得包含密码、代理凭据、OTP、
  Cookie、provider payload、账号列表或绝对 profile 路径。

## 6. 安全 SQLite 快照协议

### 6.1 对外接口

保留现有接口：

```python
backup_database(source: Path, destination: Path) -> Path
```

调用方仍必须提供不存在的最终目标。源数据库以只读 URI 打开，SQLite
`backup()` API 负责包含已经提交的 WAL 内容。快照工具不自动选择生产路径，
也不自动覆盖任何历史快照。

### 6.2 写入阶段

1. 校验源文件和目标父目录；源文件必须是普通文件，拒绝源、目标或目标父路径
   中的 symlink。
2. 检查最终目标及其 `-wal`、`-shm`、`-journal` sidecar 均不存在。
3. 在最终目标的同一目录创建不可预测名称的内部临时文件，使用独占创建和
   `0600` 权限。
4. SQLite 只向内部临时文件执行 backup，不提前创建最终目标。
5. 将临时快照切换到 `journal_mode=DELETE`，执行 `PRAGMA integrity_check`，
   要求唯一结果为 `ok`。
6. 关闭 SQLite 连接后同步临时快照文件；POSIX 上使用文件描述符 `fsync`。

内部临时文件名不能从账号、环境名或目标内容推导。错误日志只输出固定错误码，
不输出源路径、目标路径或 SQLite 异常载荷。

### 6.3 无覆盖发布

发布操作由平台适配器负责，并满足“目标存在即失败”这一原子契约：

- POSIX 本地文件系统：在同一目录使用 hard-link no-clobber 发布，然后删除
  内部临时名字。`link` 返回 `EEXIST` 时保留最终目标并失败。
- Windows：使用同卷、目标存在时拒绝覆盖的 rename 语义。测试必须证明目标
  已存在时不会被替换。
- 不支持所需原子原语的文件系统：明确返回 `snapshot_publish_unsupported`，
  不得退回 `os.replace()` 或 check-then-rename。

POSIX 成功发布后同步父目录，确保目录项具有崩溃持久性。如果文件已经发布但
父目录同步失败，返回 `snapshot_durability_unconfirmed` 并保留最终目标，不能
通过删除目标伪装成完整回滚。任何阶段失败时只清理本次生成的内部临时文件；
失败路径永远不对最终目标执行 `unlink()`。

### 6.4 竞态与取消

必须覆盖以下情况：

- 最终目标在校验后、发布前由另一个进程创建；
- Linux 立即复用 inode；
- 最终目标变成普通文件或 symlink；
- 临时数据库连接、backup、integrity check、sync 或发布失败；
- `KeyboardInterrupt`、`CancelledError` 或其他 `BaseException`；
- sidecar 已存在；
- 源数据库缺失、损坏或存在未提交事务。

最终目标只由成功的无覆盖发布创建。已经存在或竞态中出现的最终目标必须逐字节
保留。

## 7. 会话绑定身份架构

### 7.1 模块边界

新增 `core/session_identity.py`，负责纯协议解析、账号槽位关联、结果分类和
敏感字段边界。`core/profile_runtime.py` 继续作为 browser kernel，负责在
profile lease 内协调 transport、会话事实和 manifest。

边界分为三层：

1. **Identity transport**：由 Playwright/Selenium adapter 使用同一浏览器
   context/profile 访问固定 Google 身份端点，只返回受大小限制的响应和最终
   URL。
2. **Versioned provider**：解析某一已知版本的 Google 结构化响应，输出统一的
   非敏感账号槽位集合。端点和解析器版本固定在代码中，不能由 Web 参数或环境
   变量任意指定，以免形成 SSRF。
3. **Session resolver**：把 Gmail 当前会话槽位、provider 账号记录、expected
   email、manifest email 和受信任 origin 关联为一次性身份证明。

warmer、health checker 和 registration 不读取 provider payload，也不实现
自己的邮箱选择器。

### 7.2 内部数据契约

核心内部类型采用等价于以下结构的不可变对象：

```python
@dataclass(frozen=True)
class AccountSessionRecord:
    slot: int
    email: str
    valid_session: bool

@dataclass(frozen=True)
class SessionIdentityProof:
    provider: str
    provider_version: int
    session_slot: int
    observed_email: str
    final_origin: str
    collected_at: str
```

`SessionIdentityProof` 只在当前调用栈中存在，并携带内核私有的进程内证据标记。
普通 dict、adapter 返回的 `authenticated=True`、Cookie 名称或持久化字段都
不能伪造该标记。

provider 原始响应在解析后立即释放，不写入数据库、manifest、任务结果或日志。
响应必须设置硬性大小上限；超过上限返回 `identity_unavailable`。解析器只接受
完整响应，不能截断后继续解析。

### 7.3 受信任 transport

Playwright transport：

1. 记录当前业务 page。
2. 在同一 persistent context 中创建临时 page。
3. 导航到代码内固定的 HTTPS Google identity endpoint。
4. 校验最终 URL 仍属于允许的 Google identity origin。
5. 取得受大小限制的结构化正文。
6. 在返回业务 page 前关闭临时 page。

Selenium transport：

1. 记录当前 window handle 和完整 handle 集合。
2. 在同一 driver/profile 中创建临时 tab。
3. 导航、校验最终 origin 并取得受大小限制的结构化正文。
4. 关闭临时 tab。
5. 恢复原始 handle，并确认没有遗留额外窗口。

transport 必须继续使用 manifest 记录的 browser channel、profile 和 proxy。
不得把 Cookie 复制到 `requests`、aiohttp 或另一个浏览器进程，也不得绕开
profile lease。

临时页创建失败、导航失败、重定向到未允许 origin、正文过大、关闭失败、句柄
恢复失败或任务取消，都必须产生结构化失败。关闭/恢复失败升级为
`cleanup_failed`，即使已经得到匹配邮箱也不能返回成功。

### 7.4 provider 版本化

首个 provider 记为 `google_browser_accounts_v1`，固定访问：

```text
https://accounts.google.com/ListAccounts?gpsia=1&source=ChromiumBrowser&json=standard
```

请求不得接受调用方追加 query、header 或替代 URL。导航上限为 15 秒，响应正文
上限为 262144 bytes，账号记录上限为 32 条。正常响应的最终 URL 必须保持
`https://accounts.google.com/ListAccounts`；重定向到明确的 Google 登录路径只
能产生 `login_required`，其他路径或 origin 产生 `identity_unavailable`。

provider 负责：

- 去除已知且严格匹配的 anti-XSSI 前缀；
- 使用标准 JSON parser，禁止正则提取邮箱；
- 校验顶层结构和每个必需字段的类型；
- 规范化邮箱大小写，但不猜测缺失域名或修改本地部分；
- 拒绝重复槽位、重复有效邮箱、负数槽位、未知关键结构和含糊记录；
- 只保留 `slot/email/valid_session` 这三个解析后字段。

协议结构发生变化时新增 provider 版本。不得直接修改旧版本解析语义后继续声称
是同一版本。旧 provider 失效时返回 `identity_unavailable`，不会退回 DOM。

### 7.5 身份关联规则

返回 `authenticated` 必须同时满足：

1. 业务页面最终 URL 是允许的 Google HTTPS application origin。
2. URL 明确包含合法的 Gmail session slot，且该 slot 可由 provider 唯一定位。
3. 对应记录的 `valid_session` 为真。
4. provider 邮箱、expected email、manifest email 和数据库账号完全一致。
5. 现有 Cookie 校验和 application shell 校验均通过。
6. provider proof、Cookie 和 application shell 都来自同一次 lease 内的当前
   浏览器会话。
7. adapter 和临时身份页完成清理，浏览器进程状态可确认。

仅凭以下内容不得形成身份证明：

- `[data-email]`、`aria-label`、`title` 或 body text；
- 邮件正文、发件人、收件人或账号切换器；
- manifest 中历史 `identity_verified`；
- Cookie 名称或存在性提示；
- URL 查询字符串中的任意邮箱；
- adapter 自报 `authenticated=True`；
- 之前一次 health/warm 的成功结果。

如果 provider 没有可靠槽位字段，禁止仅按返回数组顺序猜测 `/mail/u/N/`。
实现前必须用 provider 合同 fixture 明确槽位语义；真实 staging 未验证前，功能
状态只能是“本地协议验证通过”，不能标记为生产验证通过。

## 8. 状态与错误分类

浏览器状态词汇新增：

```text
identity_unavailable
```

关键状态定义：

| 状态 | 含义 | 重试策略 | profile 处理 |
| --- | --- | --- | --- |
| `authenticated` | 当前会话身份和认证事实完整匹配 | 不重试 | 可继续 |
| `login_required` | 可靠事实表明当前没有登录会话 | warmer 可在同一 lease 登录一次 | 保留 |
| `identity_unavailable` | provider/transport/协议无法给出唯一身份 | 有界重试 | 保留但不标记 ready |
| `account_mismatch` | 已证明当前有效会话属于另一个账号 | 不自动重试登录 | 隔离并要求人工处置 |
| `challenge` | 明确存在验证挑战 | 按现有挑战策略 | 保留 |
| `cleanup_failed` | 页面、窗口、进程或 lease 清理不可确认 | 补偿清理后再决定 | 标记清理失败 |

`identity_unavailable` 是浏览器事实，不等同于密码错误、邮箱锁定或 IMAP
失败。若 mailbox 为 active，总体状态为 `degraded`；若两个通道都无法建立
事实，总体状态为 `error` 或 `network_error`，不得投影为 `active`。

retry engine 将 `identity_unavailable` 视为有界、可退避重试错误；
`account_mismatch` 和 `cleanup_failed` 不进入普通登录重试。所有持久化错误码
继续使用 allowlist，不写入 provider 异常文本。

## 9. 三条业务流程

### 9.1 Registration

1. provision manifest 并取得 registration lease。
2. 使用选定 engine 完成注册流程。
3. 到达 Gmail application origin 后取得新的 session identity proof。
4. 只有 proof、Cookie、application shell、expected email 全部匹配，才允许
   bind 数据库账号并把 manifest 标记为 `ready`。
5. `identity_unavailable` 不得被当成注册成功；保留可恢复状态并记录有界错误码。
6. `account_mismatch` 将 profile 隔离，不能把当前 profile 绑定给 expected
   email，也不能在同一会话中覆盖性登录。
7. 浏览器和临时页关闭完成后才允许释放 lease 和返回成功。

### 9.2 Health checker

1. 解析 manifest、校验 engine/proxy 并取得 health lease。
2. 使用登记 engine 打开同一 profile。
3. 不输入账号密码，只取得当前 session identity、Cookie 和 application shell。
4. 分别持久化 browser/mailbox 两个通道事实。
5. identity provider 不可用时保留 mailbox 结果，但 browser 返回
   `identity_unavailable`。
6. 关闭 adapter、临时页和进程后释放 lease。

### 9.3 Warmer

1. 解析 manifest、校验 engine/proxy 并取得 warm lease。
2. 第一次 probe 取得新的 session identity proof。
3. `authenticated` 时直接执行有界活动。
4. 只有明确 `login_required` 时才能在同一会话中输入凭据。
5. 登录后重新取得完整 session identity proof；不能复用登录前结果。
6. `identity_unavailable`、`account_mismatch`、`challenge` 或 cleanup failure
   立即停止，不能为了成功率切换 engine、profile、proxy 或身份来源。
7. 活动、关闭、进程检查和 lease 释放都成功后才返回成功。

## 10. Manifest 与持久化边界

manifest 可以记录以下非敏感审计字段：

```json
{
  "identity_protocol": "google_browser_accounts_v1",
  "identity_provider_version": 1,
  "identity_verified_email": "user@example.test",
  "identity_verified_at": "2026-09-17T00:00:00Z"
}
```

这些字段证明某次历史验证发生过，不是当前会话凭证。每次 health/warm 仍必须
重新验证。

禁止持久化：

- provider 原始响应和账号数组；
- Cookie 名称、值、hash 或 expiry 集合；
- window handle、页面源码、response headers；
- Google 内部 obfuscated id、头像 URL 或显示名称；
- 密码、OTP、代理认证和 token。

普通 Web API 只暴露 provider 名称、版本、最后验证时间和受限状态，不暴露
原始 observed email 之外的账号数据。

## 11. 旧数据迁移

### 11.1 Schema backfill

对于非空 `profile_id` 但缺少显式可信状态的旧行，默认投影为：

```text
profile_state=legacy_unbound
identity_state=identity_reconstructed
browser_status=not_configured
overall_status=unknown
```

不得仅凭非空 `profile_id` 生成 `bound`，也不得仅凭历史 `status=active` 生成
可信浏览器状态。已经具有完整、相互一致的 native manifest 和显式状态的行
保持不变。

### 11.2 JSON/TXT 导入

- `profile_id` 原始类型必须是字符串；数字、数组、对象和布尔值均为 invalid。
- email、engine 和状态字段使用现有严格 normalizer。
- 每条记录使用独立事务；一条失败只回滚本条，并继续处理后续记录。
- 重复执行迁移保持幂等，不覆盖已验证账号，不制造重复 profile。
- 新的内部迁移接口返回 `imported/skipped/invalid/conflicted` 计数。
- 现有返回整数的兼容入口保留，由新接口投影 `imported`，避免破坏旧调用方。
- 迁移报告不包含邮箱、密码、代理、原始记录或绝对路径。

迁移前由操作员显式执行数据库快照命令。系统不会自动复制生产数据库，也不会
在快照失败时继续执行具有状态变化的迁移。

## 12. 依赖可复现性与跨平台 CI

### 12.1 依赖约束

`requirements.txt` 继续声明支持范围；新增按 Python minor 版本生成并审核的
`constraints/python3.9.txt`、`constraints/python3.10.txt`、
`constraints/python3.11.txt` 和 `constraints/python3.12.txt`。安装入口和 CI
按当前解释器选择对应文件，例如：

```text
python3.11 -m pip install -r requirements.txt -c constraints/python3.11.txt
```

每份 constraints 固定所有直接和传递依赖，并允许用 PEP 508 marker 表达真正
存在的 OS 差异。constraints 更新必须通过显式维护命令执行，并在同一变更中
运行完整矩阵。不能在应用启动时自动升级依赖。若当前 Python minor 没有对应
constraints，setup 明确失败并给出支持版本，不静默退回无限制解析。

跨 OS wheel/hash 差异由 constraints 生成流程和 CI 验证处理；不能用一台 macOS
机器的 `pip freeze` 冒充通用锁文件。

### 12.2 CI 矩阵

保留现有 Ubuntu 与 macOS Python 矩阵和 Ubuntu 双引擎真实 localhost smoke，
并增加至少一个 `windows-latest` Python 3.11 作业，运行：

- 完整 unittest；
- Windows 文件锁与任务取消分支；
- SQLite 无覆盖发布测试；
- compileall、JavaScript syntax、pip check 和 diff check。

Windows 浏览器 smoke 可以作为后续独立门禁，但 Windows 核心进程/文件语义必须
在本轮真实 runner 上验证，不能只依赖 POSIX 上的模拟测试。

## 13. 测试策略

### 13.1 快照测试

- WAL 已提交记录存在，未提交记录不存在；
- 最终目标和 dangling symlink 永不覆盖；
- 发布前出现替换文件时逐字节保留；
- 强制模拟相同 `st_dev/st_ino` 时仍不删除最终目标；
- POSIX/Windows publish adapter 的 no-clobber 行为；
- integrity、sync、publish 和取消失败只清理内部临时文件；
- 权限保持私有；
- CLI 不泄露路径和异常载荷。

### 13.2 Provider parser 测试

- 合法单账号、多账号和不同 slot；
- anti-XSSI 前缀；
- malformed JSON、错误字段类型、重复 slot、重复邮箱；
- 无效 session、缺失 slot、负数 slot、超大响应；
- 协议版本变化和未知关键结构；
- 错误结果不包含原始 payload。

### 13.3 双引擎协议矩阵

Playwright 和 Selenium 都必须覆盖 registration、health、warm：

| 场景 | 预期 |
| --- | --- |
| 当前会话 A，expected A | 可形成 proof |
| 当前会话 B，页面中出现 A | `account_mismatch`，不得绑定 A |
| 当前会话 B，expected B，页面中出现 A | 只认 provider 的 B |
| 多账号但 slot 唯一 | 使用唯一 slot 记录 |
| 多账号且 slot 含糊 | `identity_unavailable` |
| provider 重定向到登录页 | `login_required` |
| provider 格式变化 | `identity_unavailable` |
| 临时页关闭失败 | `cleanup_failed` |
| 任务取消 | 关闭临时页和 adapter，释放 lease |
| engine/proxy 与 manifest 不一致 | 启动前拒绝 |

测试必须断言真实业务结果、状态和资源清理，不能只检查某段 JavaScript 字符串
是否存在。fake response 必须完整模拟已定义 provider 结构。

### 13.4 真实浏览器与 staging

CI localhost smoke 验证：

- 两个 engine 都真实启动；
- persistent profile 数据能够重开；
- 临时身份页生命周期和原始页面恢复；
- lease/进程清理；
- required smoke 不允许 skip 后仍返回成功。

真实 Google staging 是显式 opt-in，使用专用测试账号和隔离 profile。它不进入
默认 CI，也不读取用户账号。首次 staging 完成前，文档必须标记
`google_browser_accounts_v1` 为“本地协议验证，生产未验证”。

## 14. 安全与隐私

- provider endpoint 是代码内 allowlist，不接受 API/UI/环境传入 URL。
- 只允许 HTTPS，严格解析 hostname，不使用字符串后缀判断 origin。
- 响应大小有硬上限，JSON 深度和记录数量有界。
- provider 异常映射为 allowlist 错误码，不记录异常正文。
- 原始响应和 proof 不跨进程、不进入任务 ledger。
- profile lease 覆盖所有临时页和 transport 活动。
- `account_mismatch` 不触发自动输入另一个账号密码。
- 任何弱 DOM 信号都不能提升 browser status。
- Appium 和 legacy `profile_path` 继续 fail-closed。

## 15. 可观测性

允许记录和统计：

- provider 名称和版本；
- 成功/失败计数；
- `identity_unavailable`、`account_mismatch`、`cleanup_failed` 等错误码；
- 有界耗时；
- profile_id 的现有非敏感内部关联方式。

禁止记录：邮箱列表、provider body、Cookie、密码、代理认证、页面源码、绝对路径
和外部服务响应。

管理台可以显示：最后身份验证时间、provider 版本、browser status 和是否需要
人工处理。不能显示其他已登录 Google 账号。

## 16. 发布顺序

实现拆成可独立审核的中文提交：

1. `备份:实现跨平台无覆盖数据库快照发布`
2. `内核:定义版本化浏览器会话身份证明协议`
3. `浏览器:接通双引擎会话身份与生命周期清理`
4. `任务:接通身份错误分类与持久化重试`
5. `迁移:增加逐行回滚与严格身份投影`
6. `工程:锁定依赖并补充跨平台门禁`

每个提交都遵循 RED -> GREEN -> REFACTOR：先增加能在当前代码上失败的回归
测试，确认失败原因正确，再写最小实现并运行相关回归。不得把多个根因混入一次
修复。

## 17. 部署与回滚

1. 先部署安全快照修复并恢复远程 CI 全绿，不涉及 schema 变化。
2. 再部署 session identity parser 和状态词汇，但不提供弱 fallback 开关。
3. Playwright 与 Selenium 的 transport 作为同一个上线单元；任一 engine 未
   通过相同合同，就不部署身份接通提交。任何中间实现都不能借用另一 engine
   打开其 profile。
4. 两个 engine 都通过合同后，再同时接通 registration/health/warm。
5. 迁移前显式生成并校验数据库快照。
6. 迁移只改变不能证明可信的旧记录；显式验证的 native 记录不得降级。
7. 最后启用依赖 constraints 和 Windows CI。

回滚只允许回滚代码和新增的非敏感状态投影，不能恢复旧的弱 DOM 身份判定。
迁移回滚使用操作员显式创建的 SQLite 快照；浏览器 profile 不在数据库快照中，
不能把数据库回滚误称为完整 profile 回滚。

## 18. 验收标准

满足以下全部条件后才能声称本轮完成：

- Linux inode 复用回归通过，最终替换文件始终保留。
- GitHub Actions 的 Ubuntu、macOS、Windows 必需作业全部通过。
- Playwright 和 Selenium 都从 manifest 选择原登记 engine/profile/proxy。
- registration、health 和 warm 不再使用通用 DOM 邮箱作为认证证据。
- “登录 B、页面出现 A”在两个 engine、三条业务流程中均不能绑定 A。
- provider 不可用或含糊时返回 `identity_unavailable`，无 DOM fallback。
- 登录后重新验证身份，历史 proof 不能复用。
- 临时页、browser process 和 lease 的清理失败覆盖业务成功。
- migration 不制造 `bound/active` 信任，并能跳过坏记录继续导入。
- 完整 unittest、真实 localhost browser gate、compileall、Node syntax、
  pip check、dependency constraints 和 diff check 全部通过。
- 没有真实 Gmail、短信、Telegram、生产代理、用户 profile 或生产凭据进入测试。
- 真实 Google staging 未运行时，交付说明明确保留该限制。

## 19. 已考虑但未采用的方案

### 19.1 继续收紧 DOM selector

拒绝。无论 selector 多具体，页面 DOM 都可能同时包含多个账号、邮件地址或隐藏
元素，无法建立当前活动会话与邮箱之间的协议级关联。

### 19.2 仅相信 manifest 历史绑定

拒绝。profile 可以被外部打开、切换账号或替换 Cookie；历史绑定不能证明当前
会话仍属于同一账号。

### 19.3 把 Cookie 复制到独立 HTTP 客户端

拒绝。它扩大凭据暴露面，可能绕开浏览器 proxy/TLS/session 行为，也不再是
严格意义上的同一浏览器会话。

### 19.4 当前立即引入 OAuth/OIDC

暂不采用。它提供更正式的身份来源，但需要客户端注册、授权同意、token 生命周期
和额外凭据边界，会显著改变当前自动化流程。内核 provider 接口保留未来替换
空间。

### 19.5 快照失败时使用 `os.replace()`

拒绝。`os.replace()` 会覆盖竞态中出现的最终目标，违反快照工具的 no-clobber
承诺。
