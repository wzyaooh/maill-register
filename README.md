# Gmail Creator

提供中文 Web 控制台的账号自动化管理项目，支持后台任务、账号管理、静态代理与 KKOIP 动态池管理，以及相互隔离的开发、正式运行环境。

请仅在获得授权的范围内使用，并遵守平台及服务供应商的使用条款。

## 目录

- [快速启动](#quick-start)
- [开发与正式环境](#environments)
- [Web 功能](#web-features)
- [代理管理](#proxy-management)
- [配置说明](#configuration)
- [任务、数据与备份](#data)
- [直接使用 Python 启动](#python-entry)
- [项目结构](#structure)
- [运行要求](#requirements)
- [验证与常见问题](#validation)
- [使用限制与版权](#legal)

<a id="quick-start"></a>
## 快速启动

### 1. 准备运行环境

- 推荐 Python 3.10 或更高版本，最低支持 Python 3.9。
- [start.sh](./start.sh) 适用于 macOS / Linux；Windows 可使用[直接 Python 入口](#python-entry)。
- 首次安装需要网络连接，以下载 Python 依赖和 Playwright Chromium。

在项目目录执行所需环境的初始化命令：

```bash
# 开发环境
./start.sh dev --setup

# 正式环境
./start.sh prod --setup
```

`--setup` 会创建对应虚拟环境、安装 [requirements.txt](./requirements.txt) 中的依赖、下载 Chromium，并初始化环境配置与资源文件。初始化完成后不会自动启动服务。

如需指定 Python：

```bash
PYTHON=/path/to/python3.11 ./start.sh dev --setup
```

### 2. 设置管理员密码

根据需要编辑以下配置文件：

| 环境 | 配置文件 |
| --- | --- |
| 开发 | `runtime/dev/.env` |
| 正式 | `runtime/prod/.env` |

为 `WEB_ADMIN_PASSWORD` 设置至少 **16 个字符**的强密码。开发、正式环境应使用不同的密码。系统没有默认管理员密码，登录页面只需输入管理员密码。

可以生成随机密码后，将结果填入相应配置文件。以下示例使用开发环境解释器；仅初始化正式环境时，将 `dev` 替换为 `prod`：

```bash
venv/dev/bin/python -c "import secrets; print(secrets.token_urlsafe(24))"
```

配置模板：

- [开发环境模板](./config/environments/dev.env.example)
- [正式环境模板](./config/environments/prod.env.example)

### 3. 启动服务

```bash
./start.sh dev
# 浏览器访问 http://127.0.0.1:8081
```

```bash
./start.sh prod
# 浏览器访问 http://127.0.0.1:8080
```

脚本以前台方式运行，按 `Ctrl+C` 停止。长期部署可使用 systemd、launchd 等进程管理工具。

普通启动不会安装或更新依赖；需要安装或更新时显式执行对应环境的 `--setup`。

<a id="environments"></a>
## 开发与正式环境

| 项目 | 开发环境 | 正式环境 |
| --- | --- | --- |
| 启动命令 | `./start.sh dev` | `./start.sh prod` |
| 默认监听地址 | `127.0.0.1:8081` | `127.0.0.1:8080` |
| Web 服务器 | Flask 开发服务器 | Waitress |
| Python 虚拟环境 | `venv/dev/` | `venv/prod/` |
| 运行根目录 | `runtime/dev/` | `runtime/prod/` |
| 默认浏览器模式 | 有头模式 | 无头模式 |
| 服务日志级别 | DEBUG | INFO |
| 页面标识 | 开发环境 | 正式环境 |

两个环境分别保存配置、代理文件、账号数据库、断点、任务记录和日志，并使用不同的登录 Cookie。它们可以同时运行；同一环境只允许一个 Web 服务使用其任务目录。

初始化时仅复制姓名库和 User-Agent 种子资源，代理文件从空列表开始。再次初始化会保留现有配置和数据。配置中的文件路径必须位于所选环境目录内。

### 开发模式

- 仅允许监听本机回环地址。
- 页面模板支持自动重载。
- 修改 Python 代码后需要重启服务。
- 不启用交互式调试器或自动进程重载，以避免正在执行的浏览器任务被意外中断。

### 正式部署

可以显式指定监听地址和端口：

```bash
./start.sh prod --host 0.0.0.0 --port 8080
```

远程访问应通过 HTTPS 反向代理或受信任的加密隧道，并使用防火墙限制访问范围。使用 HTTPS 时，在正式环境配置中设置：

```dotenv
WEB_COOKIE_SECURE=true
```

本机明文 HTTP 测试时保持 `false`，否则浏览器不会发送安全 Cookie。

修改端口示例：

```bash
./start.sh dev --port 8082
```

环境隔离针对项目配置、运行文件和登录会话。短信供应商账户、代理出口、Appium 设备和语音服务端口等外部资源，需要按部署需求单独规划。

<a id="web-features"></a>
## Web 功能

| 菜单 | 功能 |
| --- | --- |
| 总览 | 账号数量、可用比例、策略与短信服务分布、任务动态、会话历史和服务配置状态 |
| 创建账号 | Ghost、Premium、YouTube、Workspace 模式；选择引擎、数量、短信验证及并行参数 |
| 任务与日志 | 执行进度、日志、任务参数、结构化结果、历史记录和停止操作 |
| 账号管理 | 搜索、状态筛选、批量选择、密码按需显示、导出、健康检查和养号 |
| 代理管理 | 静态代理编辑与文件导入、KKOIP 配置、选池策略、代理检测、公开代理获取和检测统计 |
| 资源管理 | 姓名库、User-Agent 文件编辑与保存 |
| 系统配置 | 分组查看和编辑账号、浏览器、短信、验证码、行为及通知等配置 |
| 工具与服务 | 配置自检、账号数据迁移、Telegram 测试、短信余额查询、断点恢复与清除、语音服务启停 |

### 创建与养号

- 创建数量：每批 **1–100** 个账号。
- 创建引擎：Playwright、Selenium，以及实验性的 Appium 流程。
- Ghost 使用标准流程且默认不启用短信 API；Premium 默认启用短信 API。
- YouTube、Workspace 使用各自的创建入口，可按页面选项启用短信验证。
- Playwright / Selenium 支持串行和并行创建，并行数量为 **1–5** 个工作线程。
- 并行创建需要先配置固定账号密码；串行创建可生成密码。
- 创建页的注册前预热时长为 **0–60 分钟**，由 Selenium 非短信路径使用。
- 账号管理页支持通过 Playwright 或 Selenium 对全部或选中的账号养号，时长为 **1–60 分钟**。

### 账号检查与导出

- 健康检查通过 IMAP 进行，并将可确定的状态更新写入数据库。
- 密码默认隐藏，显式查看后会自动隐藏，离开账号页面也会清除显示。
- 支持 CSV、JSON 和 `邮箱:密码` 格式的 TXT 导出。
- 导出包含全部账号，不受页面搜索或勾选范围影响。
- 导出文件包含明文密码。CSV 会对可能被电子表格当作公式的单元格加前导单引号；需要原始值时使用 JSON 或 TXT。

<a id="proxy-management"></a>
## 代理管理

### 静态代理

在「代理管理」中粘贴文本，或导入 UTF-8 TXT 文件。支持追加和替换编辑区内容，文件及合并后的内容最多 **1 MiB**。

每行一条代理，支持以下格式：

```text
# 无认证代理
192.0.2.10:8080

# 带认证代理：host:port:user:pass
198.51.100.20:8080:example-user:example-password
```

- `#` 开头的行为注释。
- 按上述格式填写，不要添加 `http://`、`socks5://` 等 URL 前缀。
- 文件导入只更新编辑区，点击「保存代理文件」后才写入服务器。
- 列表预览隐藏认证信息；原始文本编辑区仍显示明文，请勿公开分享。
- 「获取免费代理」会连接公开来源、检测并保存代理。完成后可重新读取文件。

### KKOIP 动态池

动态池配置和选池策略都位于独立的「代理管理」菜单。

| 配置项 | 作用 |
| --- | --- |
| `KOOIP_ENABLED` | 启用动态池 |
| `KOOIP_USER_ID` | 用户 ID |
| `KOOIP_AUTH_NAME` / `KOOIP_AUTH_PASSWORD` | 认证信息 |
| `KOOIP_COUNTRY` | 国家或区域参数 |
| `KOOIP_GATEWAY` / `KOOIP_GATEWAY_PORT` | 网关与端口 |
| `KOOIP_SESSION_POOL_SIZE` | 粘性会话池容量 |
| `KOOIP_STICKY_SESSION` | 是否使用粘性会话 |
| `KOOIP_ROTATE_INTERVAL` | 会话轮换间隔 |
| `PROXY_POOL_PREFERENCE` | `auto`、`static` 或 `kooip` |

页面名称为 **KKOIP**，配置字段使用 `KOOIP_*`，动态池标识为 `kooip`。请按供应商实际提供的网关及认证要求填写参数。

### 池状态

- 静态条目数来自最近读取或保存的代理文件。
- 动态容量表示配置的会话数量，不是已确认的独立出口 IP 数量。
- 健康统计取自最近完成的代理检测任务，并标注检测时间。
- 检测结果是历史快照；修改配置或代理文件后，应重新检测。
- 每个后台任务会创建自己的代理池。

<a id="configuration"></a>
## 配置说明

业务配置由 [config/settings.py](./config/settings.py) 定义，Web 配置表单按其声明生成。代理相关字段集中在「代理管理」，其他字段位于「系统配置」。

- 业务配置保存到当前环境的 `.env`，后续任务读取新的配置快照。
- 服务器环境变量优先于 `.env`；被环境变量覆盖的配置在页面中显示为只读。
- 密钥不回传原值，留空表示保留，显式清空才会删除。
- 代理页和系统配置页分别保存，保存或重新读取一页不会丢弃另一页未保存的编辑。
- 业务任务运行期间，配置、资源文件及断点的修改会被阻止。
- 管理员认证和 Cookie 设置在服务启动时读取，修改后需要重启服务。
- 已运行的语音服务维持启动时的配置，修改后需重新启动该服务。

### Web 管理配置

| 配置项 | 说明 |
| --- | --- |
| `WEB_ADMIN_PASSWORD` | 必填，至少 16 个字符的管理员密码 |
| `WEB_SECRET_KEY` | 可选的会话签名密钥；未配置时重启会使现有登录失效 |
| `WEB_COOKIE_SECURE` | HTTPS 部署设为 `true`，本机 HTTP 测试设为 `false` |

登录会话有效期为 8 小时。修改管理员密码时，如配置了固定 `WEB_SECRET_KEY`，也应轮换该密钥。各环境应使用独立的密码、签名密钥和服务凭据。

### 服务集成

| 类别 | 已接入服务 | 主要配置 |
| --- | --- | --- |
| 短信验证 | 5sim、SMS-Activate、OnlineSIM、GetSMS | `FIVESIM_API_KEY`、`SMS_ACTIVATE_API_KEY`、`ONLINESIM_API_KEY`、`GETSMS_API_KEY` |
| 短信余额查询 | 5sim、SMS-Activate | 对应供应商 API 密钥 |
| 验证码服务 | 2Captcha、Anti-Captcha、CapMonster | `TWOCAPTCHA_API_KEY`、`ANTICAPTCHA_API_KEY`、`CAPMONSTER_API_KEY` |
| 通知 | Telegram | `TELEGRAM_BOT_TOKEN`、`TELEGRAM_CHAT_ID` |
| 语音验证码 | 独立语音服务 | `VOICE_SERVER_TOKEN` |

「已配置」只表示存在配置值，不代表网络连通、余额充足或服务调用成功。短信、验证码等第三方服务可能收费；Telegram 测试会实际发送消息。

<a id="data"></a>
## 任务、数据与备份

以下路径均相对于所选环境的运行根目录：

| 路径 | 内容 |
| --- | --- |
| `.env` | 环境配置及凭据 |
| `data/database.db` | SQLite 账号数据库与会话统计 |
| `data/web/tasks.db` | 后台任务记录 |
| `data/web/*.log` | 任务日志及服务日志 |
| `data/session_state.json` | 串行批次恢复断点 |
| `config/proxies.txt` | 静态代理 |
| `config/user_agents.txt` | User-Agent 资源 |
| `data/names.txt` | 姓名资源 |

### 任务行为

- 每个环境同一时间允许一个业务任务，可同时运行一个语音服务任务。
- 并行创建属于同一个后台任务，由任务内部管理工作线程。
- 关闭网页或退出登录不会停止后台任务。
- 「执行完成」表示操作已返回，不代表全部成功，应检查结果中的成功数、失败数和错误信息。
- 串行批次按已完成账号保存断点，支持多次中断后继续恢复。
- 并行批次不支持断点恢复；停止后已保存账号仍保留，未完成的工作需要重新安排。
- POSIX 系统停止任务时先请求退出，必要时在十秒后强制清理进程组。Windows 下需注意检查残留浏览器子进程。
- 停止或重启 Web 服务会影响其后台任务，部署操作应尽量安排在任务空闲时。

### 迁移与备份

「工具与服务 → 数据迁移」可将运行目录内的 `data/accounts.json` 或 `data/accounts.txt` 导入 SQLite。迁移是显式操作，不在启动时自动执行。

迁移已有环境前，先停止相关服务并备份配置、数据库、任务记录和资源文件，再将需要的数据放入目标环境。开发、正式环境的运行目录不会自动互相复制数据。

配置、运行目录及虚拟环境被 Git 忽略。数据库、原始日志和导出文件可能包含明文凭据，请限制文件权限并保护备份。页面的自动脱敏不能替代对原始文件的保护。

<a id="python-entry"></a>
## 直接使用 Python 启动

在已安装依赖的 Python 环境中，也可以使用统一入口：

```bash
python auto_gmail_creator.py --env dev
python auto_gmail_creator.py --env prod
python -m web --env prod --host 127.0.0.1 --port 8080
```

不传 `--env` 时，服务使用项目根目录的 `.env` 和 `data/`，默认监听 `127.0.0.1:8080`。`--web` 参数可选。

```bash
python auto_gmail_creator.py --host 127.0.0.1 --port 8080
```

### Windows 示例

```powershell
py -3 -m venv venv\dev
.\venv\dev\Scripts\python.exe -m pip install -r requirements.txt
.\venv\dev\Scripts\python.exe -m playwright install chromium
.\venv\dev\Scripts\python.exe -m web.runtime dev
```

配置 `runtime\dev\.env` 中的管理员密码后启动：

```powershell
.\venv\dev\Scripts\python.exe auto_gmail_creator.py --env dev
```

正式环境使用相同流程，将命令及路径中的 `dev` 替换为 `prod`。

<a id="structure"></a>
## 项目结构

```text
maill-register/
├── start.sh                   # 开发 / 正式环境启动与初始化
├── auto_gmail_creator.py       # Python Web 启动入口
├── requirements.txt           # Python 依赖
├── config/
│   ├── settings.py            # 业务配置声明
│   ├── constants.py           # 常量
│   └── environments/          # 开发 / 正式配置模板
├── web/
│   ├── server.py              # Flask / Waitress 启动及服务锁
│   ├── runtime.py             # 独立运行目录初始化
│   ├── app.py                 # 登录、页面路由与 API
│   ├── configuration.py       # 配置与资源文件管理
│   ├── tasks.py               # 任务存储与子进程管理
│   ├── worker.py              # 业务任务调度
│   ├── templates/             # 中文页面模板
│   └── static/                # 页面样式、交互及业务脚本
├── core/
│   ├── creation_flow.py       # 创建与恢复流程
│   ├── batch_runner.py        # 并行批次
│   ├── runners.py             # Playwright / Appium 流程
│   ├── selenium_runner.py     # Selenium 流程
│   ├── progress.py            # 后台进度与输出
│   ├── database.py            # SQLite 存储
│   ├── account_manager.py     # 账号管理与导出
│   ├── account_warmer.py      # 账号养号
│   ├── health_checker.py      # IMAP 健康检查
│   ├── proxy_manager.py       # 静态与动态代理池
│   └── session_resume.py      # 断点存储与恢复
├── services/                  # 短信与语音服务
├── js/                        # 浏览器端辅助脚本
├── data/                      # 数据及资源种子文件
├── tests/                     # 回归测试
├── runtime/                   # 各环境运行数据，初始化后生成
└── venv/                      # 各环境虚拟环境，初始化后生成
```

<a id="requirements"></a>
## 运行要求

- **Playwright**：需要安装浏览器二进制；Linux 可能还需系统库，安装步骤应使用相应管理员权限。
- **有头浏览器**：服务器需要可用的图形会话；正式环境模板默认启用无头模式。
- **Selenium**：需要可用的 Chrome 及驱动环境。
- **Appium**：需要独立运行 `127.0.0.1:4723` 服务并连接 Android 设备或模拟器。当前创建路径为实验性实现，不保存已验证账号，也不支持并行创建。
- **语音服务**：从 Web 启动时监听 `127.0.0.1:5000`，需要非空且非 `changeme` 的 `VOICE_SERVER_TOKEN`；音频转换需要 FFmpeg。
- `/voice` 和 `/otp` 使用 `X-Voice-Token` 请求头或 `token` 查询参数认证，优先使用请求头。远程回调需要单独配置受保护的 HTTPS 入口。
- 语音服务独立运行，当前创建流程不会自动读取语音 OTP API。
- 配置开关的实际效果取决于对应引擎；任务中的验证码、平台验证和外部服务结果均应以实际执行结果为准。

<a id="validation"></a>
## 验证与常见问题

### 回归测试

测试使用 Python 标准库 `unittest`，覆盖登录、配置、资源、任务、断点、进程生命周期以及开发／正式环境隔离。测试不创建真实账号，不购买短信，不向 Telegram 发送消息；部分测试会临时启动本机 HTTP 服务。

在项目目录使用已安装依赖的解释器运行：

```bash
venv/dev/bin/python -m unittest discover -s tests -p 'test_web*.py' -v
```

也可以在激活虚拟环境后执行：

```bash
python -m unittest discover -s tests -p 'test_web*.py' -v
```

### 常见问题

| 情况 | 检查方式 |
| --- | --- |
| 提示虚拟环境不存在 | 先执行相应环境的 `./start.sh dev --setup` 或 `./start.sh prod --setup` |
| 提示管理员密码未设置或过短 | 检查所选环境的 `.env`，密码至少 16 个字符 |
| 本机 HTTP 登录后仍无法进入 | 检查 `WEB_COOKIE_SECURE` 是否为 `false`，并确认访问了正确的环境和端口 |
| 提示已有 Web 服务使用任务目录 | 同一运行环境只能启动一个实例；先处理已有实例，不要直接删除锁文件 |
| 端口被占用 | 用 `--port` 选择其他端口，或停止对应的服务 |
| 开发模式不能绑定外网地址 | 开发模式限定本机访问；远程部署使用正式模式并配置 HTTPS |
| 浏览器启动失败 | 检查 Playwright 浏览器、Chrome / 驱动、系统库及图形环境 |
| 配置或资源无法保存 | 检查是否有运行中的业务任务、是否被环境变量覆盖，以及路径是否位于当前环境目录内 |
| 断点文件无法读取 | 先备份文件，再通过「工具与服务」清除断点 |
| 显示已配置但服务调用失败 | 检查任务日志中的网络、认证、余额和供应商响应 |

<a id="legal"></a>
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
