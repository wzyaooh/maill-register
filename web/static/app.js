(() => {
  "use strict";

  const $ = (id) => document.getElementById(id);
  const csrfToken = document.querySelector('meta[name="csrf-token"]').content;
  const pages = {
    overview: "总览", create: "创建账号", tasks: "任务与日志", accounts: "账号管理",
    proxies: "代理管理", resources: "资源管理", settings: "系统配置", tools: "工具与服务",
  };
  const actionLabels = {
    create: "创建账号", health: "账号健康检测", warm: "账号养号", proxy_test: "代理健康检测",
    proxy_fetch: "获取免费代理", telegram_test: "Telegram 测试", sms_balance: "短信服务余额",
    validate: "配置校验", migrate: "数据迁移", resume: "恢复会话", voice: "语音验证码服务",
  };
  const taskStatuses = {
    running: ["运行中", "info"], stopping: ["停止中", "warning"], completed: ["执行完成", "success"],
    failed: ["失败", "danger"], cleanup_failed: ["清理失败", "danger"], cancelled: ["已取消", "neutral"], interrupted: ["已中断", "warning"],
  };
  const accountStatuses = {
    active: ["可用", "success"], disabled: ["已停用", "danger"], suspended: ["已暂停", "warning"],
    banned: ["已封禁", "danger"], locked: ["已锁定", "warning"], pending: ["待验证", "warning"],
    inactive: ["不可用", "neutral"], unknown: ["未知", "neutral"], error: ["检测异常", "danger"],
    network_error: ["网络异常", "warning"], needs_verification: ["需验证", "warning"],
    degraded: ["部分可用", "warning"], password_changed: ["密码已变更", "danger"],
  };
  const engineLabels = { playwright: "Playwright", selenium: "Selenium", appium: "Appium" };
  const modeLabels = { ghost: "Ghost", premium: "Premium", youtube: "YouTube", workspace: "Workspace" };
  const groupLabels = {
    Account: "账号资料", SMS: "短信验证服务", CAPTCHA: "验证码服务", Proxy: "代理与网络",
    KooIP: "KKOIP 代理池", Browser: "浏览器引擎", Behavior: "自动化行为", Files: "文件与日志",
    Telegram: "Telegram 通知", Voice: "语音验证码",
  };
  const configLabels = {
    YOUR_BIRTHDAY: "默认生日", YOUR_GENDER: "默认性别", YOUR_PASSWORD: "默认账号密码",
    RECOVERY_EMAIL: "恢复邮箱", RECOVERY_PHONE: "恢复手机号", FORCE_RECOVERY_EMAIL: "强制设置恢复邮箱",
    FIVESIM_API_KEY: "5sim API 密钥", FIVESIM_COUNTRY: "5sim 国家", FIVESIM_OPERATOR: "5sim 运营商",
    SMS_ACTIVATE_API_KEY: "SMS-Activate API 密钥", SMS_ACTIVATE_COUNTRY: "SMS-Activate 国家编号",
    ONLINESIM_API_KEY: "OnlineSIM API 密钥", ONLINESIM_COUNTRY: "OnlineSIM 国家编号",
    GETSMS_API_KEY: "GetSMS API 密钥", GETSMS_COUNTRY: "GetSMS 国家",
    TWOCAPTCHA_API_KEY: "2Captcha API 密钥", ANTICAPTCHA_API_KEY: "Anti-Captcha API 密钥",
    CAPMONSTER_API_KEY: "CapMonster API 密钥", ENABLE_REALISTIC_WARMUP: "启用拟真养号",
    WARMUP_MIN_DURATION: "最短养号时长（秒）", QR_MAX_RETRIES: "二维码最大重试次数",
    ROTATE_USER_AGENT: "轮换 User-Agent", ROTATE_SCREEN_SIZE: "轮换屏幕尺寸", ROTATE_TIMEZONE: "轮换时区",
    ENABLE_PROXY: "启用代理", PROXY_FILE: "代理文件路径", PROXY_TYPE: "代理类型",
    ROTATE_PROXY_EVERY: "代理轮换间隔（账号数）", PROXY_COUNTRY_ROTATION: "代理轮换国家列表",
    MOBILE_PROXY_IP_CHANGE_URL: "移动代理换 IP 地址", PROXY_CHANGE_WAIT_TIME: "换 IP 等待时长（秒）",
    PROXY_POOL_PREFERENCE: "优先代理池", KOOIP_ENABLED: "启用 KKOIP", KOOIP_USER_ID: "KKOIP 用户 ID",
    KOOIP_AUTH_NAME: "KKOIP 认证用户名", KOOIP_AUTH_PASSWORD: "KKOIP 认证密码",
    KOOIP_COUNTRY: "KKOIP 国家", KOOIP_GATEWAY: "KKOIP 网关", KOOIP_GATEWAY_PORT: "KKOIP 网关端口",
    KOOIP_SESSION_POOL_SIZE: "KKOIP 会话池大小", KOOIP_STICKY_SESSION: "KKOIP 粘性会话",
    KOOIP_ROTATE_INTERVAL: "KKOIP 轮换间隔", ENGINE_MODE: "默认运行引擎", HEADLESS_MODE: "无头浏览器模式",
    BROWSER_TIMEOUT: "浏览器超时（秒）", ENABLE_SESSION_WARMING: "启用会话预热",
    ENABLE_FINGERPRINT_MASKING: "启用指纹掩码", ENABLE_HUMAN_TYPING_ERRORS: "模拟输入错误",
    DELAY_BETWEEN_ACCOUNTS: "账号间等待时长（秒）", WARMING_INTENSITY: "养号强度",
    ENABLE_MAC_ROTATION: "启用 MAC 轮换", CHANGE_MAC_EVERY: "MAC 轮换间隔（账号数）",
    ENABLE_RECOVERY_CHAIN: "启用恢复邮箱链", CHAIN_FILE: "恢复链文件路径", ENABLE_WORM_AI: "启用 Worm AI",
    ENABLE_CDP_INJECTION: "启用 CDP 注入", ENABLE_COOKIE_REAPER: "启用 Cookie Reaper",
    ENABLE_GHOST_TYPER: "启用 Ghost Typer", ENABLE_POLTERGEIST: "启用 Poltergeist",
    ENABLE_YOUTUBE_MODE: "启用 YouTube 模式", ENABLE_EDU_SPOOF: "启用实验性 EDU 模式",
    USE_ARABIC_NAMES: "使用阿拉伯语姓名", NAMES_FILE: "姓名库文件路径",
    USER_AGENTS_FILE: "User-Agent 文件路径", ENABLE_LOGGING: "启用日志", LOG_FILE: "日志文件路径",
    LOG_LEVEL: "日志级别", ACCOUNTS_FILE: "旧格式账号文件路径", EXPORT_FORMAT: "默认导出格式",
    TELEGRAM_BOT_TOKEN: "Telegram 机器人令牌", TELEGRAM_CHAT_ID: "Telegram 聊天 ID",
    VOICE_SERVER_TOKEN: "语音服务访问令牌",
  };
  const serviceLabels = {
    FIVESIM_API_KEY: "5sim", SMS_ACTIVATE_API_KEY: "SMS-Activate", ONLINESIM_API_KEY: "OnlineSIM",
    GETSMS_API_KEY: "GetSMS", TWOCAPTCHA_API_KEY: "2Captcha", ANTICAPTCHA_API_KEY: "Anti-Captcha",
    CAPMONSTER_API_KEY: "CapMonster", TELEGRAM_BOT_TOKEN: "Telegram", VOICE_SERVER_TOKEN: "语音验证码",
  };
  const systemNoteLabels = {
    "Dependency discovery does not verify browser binaries or external services.": "依赖可发现不代表浏览器二进制已安装，也不代表外部服务已连通。",
    "Appium port availability does not verify a connected Android device.": "Appium 端口可达不代表已连接 Android 设备。",
    "Appium registration is explicitly disabled until the native lifecycle contract is complete.": "Appium 创建已显式禁用；补齐原生生命周期契约前不会启动设备或创建账号。",
    "Proxy/behavior options reflect existing engine capabilities; not all engines use every option.": "代理与行为选项反映现有引擎能力，并非每个引擎都会使用所有选项。",
    "Use HTTPS for remote access; configure WEB_COOKIE_SECURE=true behind TLS.": "远程访问请使用 HTTPS，并在 TLS 反向代理后设置 WEB_COOKIE_SECURE=true。",
  };
  const capabilityLabels = {
    user_agent: "User-Agent", viewport: "视口", locale: "语言区域",
    accept_language: "Accept-Language", timezone: "时区", geolocation: "地理位置",
    browser_channel_version: "浏览器渠道 / 版本", persistent_storage: "持久化存储",
    proxy_endpoint: "代理出口", proxy_credentials: "代理认证",
  };
  const capabilityStatusLabels = {
    native: ["原生", "success"], best_effort: ["尽力而为", "warning"],
    unsupported: ["不支持", "danger"], not_verified: ["未验证", "neutral"],
  };
  const state = {
    page: "", overview: null, tasks: [], tasksLoaded: false, selectedTaskId: null, selectedTask: null,
    accounts: [], accountsLoaded: false, selectedAccounts: new Set(),
    settingsLoaded: false, settingsLoading: false, settingsSaving: false, settingsControls: new Map(),
    savedSettings: [], proxyCheckId: null,
    resourceKind: "names", resources: {}, system: null, session: null, sessionUnreadable: false,
    engineInitialized: false, engineTouched: false,
    polling: false, pollTimer: null, redirecting: false, taskRevision: 0, sessionRevision: 0,
  };
  const pendingGets = new Map();
  const signatures = new Map();
  const mobileQuery = window.matchMedia("(max-width: 820px)");
  const settingsScopes = ["settings", "proxy-settings"];

  function settingsScope(field) {
    return ["Proxy", "KooIP"].includes(field.group) ? "proxy-settings" : "settings";
  }

  function text(value) {
    if (value === null || value === undefined) return "—";
    if (typeof value === "object") return JSON.stringify(value);
    return String(value);
  }

  function json(value) {
    return JSON.stringify(value, null, 2);
  }

  function el(tag, className, content) {
    const node = document.createElement(tag);
    if (className) node.className = className;
    if (content !== undefined) node.textContent = text(content);
    return node;
  }

  function setText(id, value) {
    const node = $(id);
    const content = text(value);
    if (node.textContent !== content) node.textContent = content;
  }

  function changed(key, value) {
    const signature = json(value);
    if (signatures.get(key) === signature) return false;
    signatures.set(key, signature);
    return true;
  }

  function formatDate(value) {
    if (!value) return "—";
    const date = new Date(value);
    if (Number.isNaN(date.getTime())) return text(value);
    return date.toLocaleString("zh-CN", { year: "numeric", month: "2-digit", day: "2-digit",
      hour: "2-digit", minute: "2-digit", second: "2-digit", hour12: false });
  }

  function number(value) {
    const parsed = Number(value);
    return Number.isFinite(parsed) ? parsed : 0;
  }

  function active(task) {
    return task && (task.status === "running" || task.status === "stopping");
  }

  function badge(value, statuses = taskStatuses) {
    const [label, tone] = statuses[value] || [text(value), "neutral"];
    return el("span", `badge ${tone}`, label);
  }

  function progressValues(task) {
    return {
      completed: Math.max(0, number(task.progress?.completed)),
      total: Math.max(0, number(task.progress?.total)),
      message: task.progress?.message ? text(task.progress.message) : "等待进度更新",
    };
  }

  function makeProgress(task) {
    const values = progressValues(task);
    return UI.progress(Math.min(values.completed, values.total || 1), values.total || 1,
      `${actionLabels[task.action] || task.action}：${values.completed} / ${values.total}`);
  }

  function notify(message, type = "success") {
    if (state.redirecting) return;
    const toast = el("div", `toast${type === "error" ? " error" : ""}`);
    toast.setAttribute("role", type === "error" ? "alert" : "status");
    const close = el("button", "", "×");
    close.type = "button";
    close.setAttribute("aria-label", "关闭通知");
    close.addEventListener("click", () => toast.remove());
    toast.append(el("p", "", message), close);
    const region = $("toast-region");
    while (region.children.length >= 4) region.firstElementChild.remove();
    region.append(toast);
    if (type !== "error") window.setTimeout(() => toast.remove(), 6000);
  }

  function report(promise) {
    Promise.resolve(promise).catch((error) => notify(error.message || "操作未完成，请重试。", "error"));
  }

  async function api(path, { method = "GET", body, blob = false } = {}) {
    const controller = new AbortController();
    const timeout = window.setTimeout(() => controller.abort(), blob ? 120000 : 30000);
    const headers = { Accept: blob ? "*/*" : "application/json" };
    if (method !== "GET") {
      headers["Content-Type"] = "application/json";
      headers["X-CSRF-Token"] = csrfToken;
    }
    try {
      const response = await fetch(path, {
        method, headers, credentials: "same-origin", cache: "no-store", signal: controller.signal,
        ...(method !== "GET" ? { body: JSON.stringify(body ?? {}) } : {}),
      });
      if (response.status === 401) {
        state.redirecting = true;
        window.location.assign("/login");
        throw new Error("登录已过期，正在返回登录页。");
      }
      if (response.ok && blob) return response.blob();
      let data;
      try {
        data = await response.json();
      } catch {
        throw new Error(`服务器返回了无法读取的响应（HTTP ${response.status}），请检查服务状态。`);
      }
      if (!response.ok) {
        const detail = data.error || `请求未成功（HTTP ${response.status}）`;
        const error = new Error(response.status === 403 ? `${detail}。请保存未提交的编辑后重新加载页面。` : detail);
        error.status = response.status;
        throw error;
      }
      return data;
    } catch (error) {
      if (error.name === "AbortError") throw new Error("请求超时。操作可能已在服务器提交，请先查看任务列表或重新读取状态再重试。");
      if (error instanceof TypeError) throw new Error("无法连接服务器，请检查网络与服务器运行状态。");
      throw error;
    } finally {
      window.clearTimeout(timeout);
    }
  }

  function get(path) {
    if (!pendingGets.has(path)) {
      const request = api(path).finally(() => {
        if (pendingGets.get(path) === request) pendingGets.delete(path);
      });
      pendingGets.set(path, request);
    }
    return pendingGets.get(path);
  }

  function disable(id, condition) {
    const node = $(id);
    node.disabled = Boolean(condition || node.dataset.busy);
  }

  async function perform(button, operation) {
    if (button?.dataset.busy) return;
    const wasDisabled = button?.disabled;
    if (button) {
      button.dataset.busy = "true";
      button.disabled = true;
      button.classList.add("is-busy");
      button.setAttribute("aria-busy", "true");
    }
    try {
      return await operation();
    } catch (error) {
      notify(error.message || "操作未完成，请重试。", "error");
      return undefined;
    } finally {
      if (button) {
        delete button.dataset.busy;
        button.disabled = wasDisabled;
        button.classList.remove("is-busy");
        button.removeAttribute("aria-busy");
      }
      syncActionButtons();
    }
  }

  function lockFields(form) {
    const fields = Array.from(form.querySelectorAll(UI.CONTROL_SELECTOR));
    const previous = fields.map((field) => field.disabled);
    fields.forEach((field) => { field.disabled = true; });
    return () => fields.forEach((field, index) => { field.disabled = previous[index]; });
  }

  function closeNav(restoreFocus = false) {
    $("sidebar").classList.remove("open");
    $("sidebar").inert = mobileQuery.matches;
    $("nav-backdrop").hidden = true;
    $("nav-toggle").setAttribute("aria-expanded", "false");
    $("nav-toggle").setAttribute("aria-label", "打开导航");
    document.body.classList.remove("nav-open");
    $("main-content").inert = false;
    if (restoreFocus) $("nav-toggle").focus();
  }

  function openNav() {
    $("sidebar").inert = false;
    $("sidebar").classList.add("open");
    $("nav-backdrop").hidden = false;
    $("nav-toggle").setAttribute("aria-expanded", "true");
    $("nav-toggle").setAttribute("aria-label", "关闭导航");
    document.body.classList.add("nav-open");
    $("main-content").inert = true;
    $("sidebar").querySelector('[aria-current="page"]').focus();
  }

  async function showPage({ focus = true } = {}) {
    const requested = window.location.hash.slice(1);
    const page = Object.hasOwn(pages, requested) ? requested : "overview";
    state.page = page;
    Object.keys(pages).forEach((key) => { $(`page-${key}`).hidden = key !== page; });
    document.querySelectorAll("[data-page]").forEach((link) => {
      const selected = link.dataset.page === page;
      link.classList.toggle("active", selected);
      if (selected) link.setAttribute("aria-current", "page");
      else link.removeAttribute("aria-current");
    });
    setText("breadcrumb-page", pages[page]);
    document.title = `${pages[page]} · Gmail Creator`;
    closeNav();
    if (focus) {
      $("main-content").focus({ preventScroll: true });
      window.scrollTo({ top: 0, behavior: "auto" });
    }
    if (page === "overview" || page === "create") await loadOverview();
    if (page === "tasks") {
      await loadTasks();
      if (state.selectedTaskId) await loadTaskDetail();
    }
    if (page === "accounts") await loadAccounts();
    if (page === "settings" && !state.settingsLoaded) await loadSettings();
    if (page === "proxies") {
      await Promise.all([
        state.settingsLoaded ? Promise.resolve() : loadSettings(),
        loadResource("proxies", !resourceState("proxies").dirty), loadTasks(),
      ]);
      renderProxyOverview();
    }
    if (page === "resources") await loadResource(state.resourceKind);
    if (page === "tools") await Promise.all([loadSystem(), loadSession(), loadTasks()]);
  }

  function goToTask(taskId) {
    const different = state.selectedTaskId !== taskId;
    state.selectedTaskId = taskId;
    if (different) {
      state.selectedTask = null;
      $("task-detail").hidden = true;
      $("task-empty").hidden = false;
      $("task-logs").textContent = "正在读取日志…";
    }
    renderTasks();
    if (window.location.hash !== "#tasks") window.location.hash = "tasks";
    else report(loadTaskDetail());
  }

  async function loadOverview() {
    const data = await get("/api/overview");
    state.overview = data;
    if (!state.tasksLoaded && Array.isArray(data.tasks)) state.tasks = data.tasks;
    const accounts = data.accounts || {};
    setText("stat-total", number(accounts.total).toLocaleString("zh-CN"));
    setText("stat-active", number(accounts.active).toLocaleString("zh-CN"));
    setText("stat-rate", `${number(accounts.success_rate).toFixed(1)}%`);
    setText("engine-badge", `默认 ${engineLabels[data.engine] || text(data.engine)}`);
    if (!state.engineInitialized) {
      if (!state.engineTouched && Object.hasOwn(engineLabels, data.engine)) $("create-engine").value = data.engine;
      state.engineInitialized = true;
      if (!Object.hasOwn(engineLabels, data.engine)) {
        $("create-engine").placeholder = "默认引擎无效，请手动选择";
      }
    }
    syncCreation();
    renderOverviewTasks();
    if (changed("services", data.services)) {
      const nodes = Object.entries(data.services || {}).map(([key, configured]) => {
        const item = el("div", "service-item");
        item.append(el("span", "service-name", serviceLabels[key] || key),
          el("span", `badge ${configured ? "success" : "neutral"}`, configured ? "已配置" : "未配置"));
        return item;
      });
      $("overview-services").replaceChildren(...(nodes.length ? nodes : [el("p", "empty-state", "暂无服务配置")]));
    }
    renderDistribution("strategy-stats", accounts.strategies);
    renderDistribution("sms-stats", accounts.sms_services);
    renderSessionHistory(data.sessions || []);
  }

  function renderOverviewTasks() {
    const running = state.tasks.filter(active);
    setText("stat-running", running.length);
    setText("stat-running-note", running.some((task) => task.action === "voice") ? "包含持续运行的语音服务" : "任务执行与结果可实时查看");
    if (!changed("overview-tasks", state.tasks.slice(0, 5))) return;
    const nodes = state.tasks.slice(0, 5).map((task) => {
      const item = el("button", "activity-item");
      item.type = "button";
      item.dataset.taskId = task.id;
      const body = el("span", "activity-body");
      body.append(el("span", "activity-title", actionLabels[task.action] || task.action),
        el("span", "activity-meta", formatDate(task.created_at)));
      item.append(el("span", "activity-marker", active(task) ? "◷" : "≋"), body, badge(task.status));
      return item;
    });
    $("overview-tasks").replaceChildren(...(nodes.length ? nodes : [el("p", "empty-state", "还没有任务，从创建第一个任务开始。")]));
  }

  function renderDistribution(id, source) {
    if (!changed(id, source)) return;
    const entries = Object.entries(source || {});
    const total = entries.reduce((sum, [, count]) => sum + Math.max(0, number(count)), 0);
    const nodes = entries.map(([name, count]) => {
      const row = el("div", "distribution-row");
      const heading = el("div");
      heading.append(el("span", "", name), el("strong", "", `${text(count)} 个`));
      const progress = UI.progress(Math.max(0, number(count)), total || 1, `${name}：${text(count)} 个`);
      row.append(heading, progress);
      return row;
    });
    $(id).replaceChildren(...(nodes.length ? nodes : [el("p", "empty-state", "暂无统计数据")]));
  }

  function renderSessionHistory(sessions) {
    if (!changed("session-history", sessions)) return;
    const nodes = sessions.map((session) => {
      const row = el("tr");
      const duration = number(session.duration_seconds);
      [formatDate(session.session_start || session.created_at || session.started_at), session.total_attempts,
        session.successes, session.failures, duration >= 60 ? `${Math.floor(duration / 60)} 分 ${Math.round(duration % 60)} 秒` : `${Math.round(duration)} 秒`]
        .forEach((value) => row.append(el("td", "", value)));
      const detail = UI.disclosure("策略与错误", [el("pre", "json-panel",
        json({ strategies_used: session.strategies_used ?? {}, errors: session.errors ?? {} }))], { className: "detail-box" });
      const cell = el("td");
      cell.append(detail);
      row.append(cell);
      return row;
    });
    if (!nodes.length) {
      const row = el("tr");
      const cell = el("td", "empty-state", "暂无运行会话记录");
      cell.colSpan = 6;
      row.append(cell);
      nodes.push(row);
    }
    $("session-history").replaceChildren(...nodes);
  }

  async function loadTasks() {
    const revision = state.taskRevision;
    const data = await get("/api/tasks");
    if (revision !== state.taskRevision) return;
    state.tasks = data.tasks || [];
    state.tasksLoaded = true;
    if (!state.tasks.some((task) => task.id === state.selectedTaskId)) {
      state.selectedTaskId = state.tasks[0]?.id || null;
      state.selectedTask = null;
    }
    renderTasks();
    renderOverviewTasks();
    syncActionButtons();
  }

  function renderTasks() {
    const running = state.tasks.filter(active).length;
    setText("nav-task-count", running);
    $("nav-task-count").hidden = running === 0;
    setText("task-count-label", `${state.tasks.length} 个任务 · ${running} 个进行中`);
    if (!changed("task-list", [state.tasks, state.selectedTaskId])) return;
    const nodes = state.tasks.map((task) => {
      const item = el("button", `task-item${task.id === state.selectedTaskId ? " selected" : ""}`);
      item.type = "button";
      item.dataset.taskId = task.id;
      item.setAttribute("aria-pressed", task.id === state.selectedTaskId ? "true" : "false");
      const heading = el("span", "task-item-heading");
      heading.append(el("strong", "", actionLabels[task.action] || task.action), badge(task.status));
      item.append(heading, el("span", "task-item-time", formatDate(task.created_at)),
        el("span", "task-item-message", task.error || progressValues(task).message), makeProgress(task));
      return item;
    });
    $("task-list").replaceChildren(...(nodes.length ? nodes : [el("p", "empty-state", "暂无任务，启动操作后会在这里显示。")]));
    if (!state.selectedTaskId) {
      $("task-empty").hidden = false;
      $("task-detail").hidden = true;
    }
  }

  async function loadTaskDetail() {
    const taskId = state.selectedTaskId;
    const revision = state.taskRevision;
    if (!taskId) return;
    const data = await get(`/api/tasks/${encodeURIComponent(taskId)}`);
    if (state.selectedTaskId !== taskId || revision !== state.taskRevision) return;
    const task = data.task;
    state.selectedTask = task;
    $("task-empty").hidden = true;
    $("task-detail").hidden = false;
    setText("task-title", actionLabels[task.action] || task.action);
    setText("task-id", `TASK / ${task.id}`);
    const [statusLabel, statusTone] = taskStatuses[task.status] || [text(task.status), "neutral"];
    setText("task-status", statusLabel);
    $("task-status").className = `badge ${statusTone}`;
    setText("task-timing", `开始：${formatDate(task.created_at)}${task.finished_at ? `　结束：${formatDate(task.finished_at)}` : ""}`);
    const values = progressValues(task);
    setText("task-progress-message", values.message);
    setText("task-progress-count", values.total ? `${values.completed} / ${values.total}` : `${values.completed} 已完成`);
    $("task-progress-bar").max = values.total || 1;
    $("task-progress-bar").value = Math.min(values.completed, values.total || 1);
    $("task-error").hidden = !task.error;
    setText("task-error", task.error || "");
    setText("task-params", json(task.params || {}));
    setText("task-result", task.result === null || task.result === undefined ? "任务尚未返回结果。" : json(task.result));
    const logs = $("task-logs");
    const previousScroll = logs.scrollTop;
    const content = data.logs || (active(task) ? "等待日志输出…" : "此任务没有日志输出。");
    if (logs.textContent !== content) logs.textContent = content;
    if ($("log-follow").checked) logs.scrollTop = logs.scrollHeight;
    else logs.scrollTop = previousScroll;
    syncActionButtons();
  }

  async function startTask(action, params = {}, endpoint = "/api/tasks") {
    const data = await api(endpoint, { method: "POST", body: endpoint === "/api/tasks" ? { action, params } : {} });
    const task = data.task;
    state.taskRevision += 1;
    pendingGets.delete("/api/tasks");
    state.tasks = [task, ...state.tasks.filter((item) => item.id !== task.id)];
    state.tasksLoaded = true;
    notify(`${actionLabels[action] || action}任务已启动，可在日志中查看结果。`);
    goToTask(task.id);
    renderOverviewTasks();
    syncActionButtons();
    return task;
  }

  async function cancelTask(task, button) {
    if (!task || !active(task)) return;
    const question = task.action === "voice"
      ? "确定停止语音验证码服务？正在处理的音频请求可能被中断。"
      : "确定停止此任务？进行中的浏览器操作会被中断，已完成的数据会保留。";
    const confirmed = await UI.confirm({ title: "停止任务", message: question, confirmText: "停止任务", tone: "danger" });
    if (!confirmed) return;
    await perform(button, async () => {
      const data = await api(`/api/tasks/${encodeURIComponent(task.id)}/cancel`, { method: "POST", body: {} });
      state.taskRevision += 1;
      pendingGets.delete("/api/tasks");
      pendingGets.delete(`/api/tasks/${encodeURIComponent(task.id)}`);
      state.tasks = state.tasks.map((item) => item.id === task.id ? data.task : item);
      if (state.selectedTaskId === task.id) state.selectedTask = data.task;
      notify("停止请求已发送，请等待任务状态更新。");
      renderTasks();
      renderOverviewTasks();
      if (state.page === "tasks") await loadTaskDetail();
    });
  }

  function creationMode() {
    return $("create-mode").value;
  }

  function syncCreation() {
    const engine = $("create-engine").value;
    const appium = engine === "appium";
    if (appium) $("create-parallel").checked = false;
    if (!$("create-submit").dataset.busy) {
      $("create-parallel").disabled = appium;
      $("create-threads").disabled = appium || !$("create-parallel").checked;
      $("create-warmup").disabled = engine !== "selenium";
    }
    $("appium-warning").hidden = !appium;
    $("parallel-warning").hidden = !$("create-parallel").checked;
    setText("summary-mode", `${modeLabels[creationMode()]}${$("create-sms").checked && !["premium", "ghost"].includes(creationMode()) ? " + SMS" : ""}`);
    setText("summary-engine", engineLabels[engine] || "请选择引擎");
    setText("summary-count", `${$("create-count").value || "—"} 个账号`);
    setText("summary-parallel", $("create-parallel").checked ? `${$("create-threads").value || "—"} 线程并行` : "顺序执行");
    disable("create-submit", appium || !Object.hasOwn(engineLabels, engine));
  }

  async function submitCreation(event) {
    event.preventDefault();
    if (!UI.validate($("create-form"))) return;
    const mode = creationMode();
    const engine = $("create-engine").value;
    const parallel = $("create-parallel").checked;
    const params = {
      engine, num_accounts: Number($("create-count").value),
      warmup_minutes: engine === "selenium" ? Number($("create-warmup").value) : 0,
      flow_mode: ["ghost", "premium"].includes(mode) ? "standard" : mode,
      use_sms_api: $("create-sms").checked, parallel,
      max_threads: parallel ? Number($("create-threads").value) : 3,
    };
    if (params.engine === "appium") {
      notify("Appium 创建已禁用；当前版本不会启动设备或创建账号。", "error");
      return;
    }
    if (params.parallel && !(await UI.confirm({
      title: "确认启动并行批次",
      message: "并行批次不支持断点恢复，并且需要预先配置固定密码（YOUR_PASSWORD 或 config/password.txt）。\n请确认已准备好；中断后需手动检查已保存账号。确定启动？",
      confirmText: "确定启动", tone: "warning",
    }))) return;
    await perform($("create-submit"), async () => {
      const unlock = lockFields($("create-form"));
      try {
        await startTask("create", params);
      } finally {
        unlock();
      }
    });
  }

  function filteredAccounts() {
    const query = $("account-search").value.trim().toLocaleLowerCase();
    const status = $("account-status-filter").value;
    return state.accounts.filter((account) => {
      const haystack = [account.id, account.email, account.first_name, account.last_name, account.strategy,
        account.status, account.notes, account.birthday, account.gender, account.engine,
        account.profile_state, account.browser_status, account.mailbox_status]
        .map(text).join(" ").toLocaleLowerCase();
      return (!status || account.status === status) && (!query || haystack.includes(query));
    });
  }

  async function loadAccounts() {
    const data = await get("/api/accounts");
    state.accounts = data.accounts || [];
    state.accountsLoaded = true;
    const ids = new Set(state.accounts.map((account) => Number(account.id)));
    state.selectedAccounts.forEach((id) => { if (!ids.has(id)) state.selectedAccounts.delete(id); });
    const statuses = [...new Set(state.accounts.map((account) => account.status).filter(Boolean))].sort();
    if (changed("account-status-options", statuses)) {
      $("account-status-filter").setOptions([{ value: "", label: "全部状态" },
        ...statuses.map((status) => ({ value: status, label: accountStatuses[status]?.[0] || status }))]);
    }
    renderAccountRows();
    syncActionButtons();
  }

  function renderAccountRows(force = false) {
    const accounts = filteredAccounts();
    const rowsChanged = changed("account-rows", accounts);
    if (rowsChanged || force) {
      const openDetails = new Set(Array.from($("account-rows").querySelectorAll("ui-disclosure[open]")).map((item) => item.dataset.accountDetail));
      const nodes = accounts.map((account) => {
        const id = Number(account.id);
        const row = el("tr");
        const selectCell = el("td", "checkbox-cell");
        const checkbox = UI.create("ui-checkbox", { "aria-label": `选择账号 ${account.email}` });
        checkbox.dataset.accountId = id;
        checkbox.checked = state.selectedAccounts.has(id);
        selectCell.append(checkbox);
        const identity = el("td");
        identity.append(el("div", "account-email", account.email),
          el("div", "account-name", `${[account.first_name, account.last_name].filter(Boolean).join(" ") || "未设置姓名"} · #${id}`));
        const status = el("td");
        status.append(badge(account.status || "unknown", accountStatuses));
        const runtime = el("td", "account-runtime");
        runtime.append(
          el("div", "account-runtime-engine", engineLabels[account.engine] || text(account.engine || "未绑定")),
          el("div", "field-help", `Profile：${text(account.profile_state || "未配置")}`),
          el("div", "field-help", `浏览器：${accountStatuses[account.browser_status]?.[0] || text(account.browser_status || "未检测")}`),
          el("div", "field-help", `邮箱：${accountStatuses[account.mailbox_status]?.[0] || text(account.mailbox_status || "未检测")}`),
        );
        const genders = { "1": "男", "2": "女", "3": "其他" };
        const details = UI.disclosure("查看资料", [
          el("div", "", `生日：${text(account.birthday)}`),
          el("div", "", `性别：${genders[account.gender] || text(account.gender)}`),
          el("div", "", `备注：${text(account.notes)}`),
        ], { open: openDetails.has(String(id)), className: "account-details" });
        details.dataset.accountDetail = id;
        const detailCell = el("td");
        detailCell.append(details);
        row.append(selectCell, identity, status, runtime, el("td", "", account.strategy),
          el("td", "", formatDate(account.created_at)), detailCell);
        return row;
      });
      if (!nodes.length) {
        const row = el("tr");
        const cell = el("td", "empty-state", state.accounts.length ? "没有匹配的账号，请调整筛选条件。" : "暂无账号。创建或迁移数据后会显示在这里。");
        cell.colSpan = 7;
        row.append(cell);
        nodes.push(row);
      }
      $("account-rows").replaceChildren(...nodes);
    }
    setText("account-count", `显示 ${accounts.length} / ${state.accounts.length} 个账号`);
    updateSelection();
  }

  function updateSelection() {
    const visible = filteredAccounts();
    const selectedVisible = visible.filter((account) => state.selectedAccounts.has(Number(account.id))).length;
    const header = $("select-all-accounts");
    header.checked = visible.length > 0 && selectedVisible === visible.length;
    header.indeterminate = selectedVisible > 0 && selectedVisible < visible.length;
    header.disabled = visible.length === 0;
    $("account-rows").querySelectorAll("[data-account-id]").forEach((checkbox) => {
      checkbox.checked = state.selectedAccounts.has(Number(checkbox.dataset.accountId));
    });
    setText("account-selection", state.selectedAccounts.size
      ? `已选择 ${state.selectedAccounts.size} 个账号（含筛选外的选择）`
      : `未选择账号，批量操作将作用于全部 ${state.accounts.length} 个账号`);
    disable("clear-selection", state.selectedAccounts.size === 0);
  }

  function selectedAccountScope() {
    return state.selectedAccounts.size
      ? `已选择的 ${state.selectedAccounts.size} 个账号`
      : `全部 ${state.accounts.length} 个账号（包括搜索结果之外的账号）`;
  }

  async function runAccountTask(action, button) {
    if (action === "warm" && !UI.validate($("warm-form"))) return;
    const method = action === "health" ? "检查浏览器会话与 IMAP 状态" : "在服务器启动注册时记录的浏览器";
    if (!(await UI.confirm({
      title: `确认执行${actionLabels[action]}`,
      message: `确定对${selectedAccountScope()}执行${actionLabels[action]}？\n此操作会使用账号密码并${method}。`,
      confirmText: `执行${actionLabels[action]}`, tone: "warning",
    }))) return;
    const params = { account_ids: [...state.selectedAccounts] };
    if (action === "warm") {
      params.duration_minutes = Number($("warm-duration").value);
    }
    await perform(button, () => startTask(action, params));
  }

  async function exportAccounts() {
    const format = $("export-format").value;
    if (!(await UI.confirm({
      title: "导出全部账号",
      message: `即将导出全部 ${state.accounts.length} 个账号为 ${format.toUpperCase()}，不受筛选或勾选影响。\n文件只包含非敏感账号资料。确定继续？`,
      confirmText: "确定导出", tone: "primary",
    }))) return;
    await perform($("account-export"), async () => {
      const blob = await api("/api/accounts/export", { method: "POST", body: { format }, blob: true });
      const url = URL.createObjectURL(blob);
      const link = el("a");
      link.href = url;
      link.download = `accounts.${format}`;
      document.body.append(link);
      link.click();
      link.remove();
      window.setTimeout(() => URL.revokeObjectURL(url), 60000);
      notify("已开始下载。文件只包含非敏感账号资料。");
    });
  }

  function resourceState(kind) {
    if (!state.resources[kind]) {
      state.resources[kind] = { loaded: false, loading: false, saving: false, importing: false, dirty: false,
        original: "", error: false, pathChanged: false, revision: 0 };
    }
    return state.resources[kind];
  }

  function syncResource(kind) {
    const resource = resourceState(kind);
    const editor = $(`editor-${kind}`);
    editor.disabled = !resource.loaded || resource.loading || resource.saving || resource.importing;
    const save = document.querySelector(`[data-resource-save="${kind}"]`);
    const reload = document.querySelector(`[data-resource-reload="${kind}"]`);
    save.disabled = !resource.loaded || !resource.dirty || resource.loading || resource.saving || resource.importing
      || resource.pathChanged || Boolean(save.dataset.busy);
    reload.disabled = resource.loading || resource.saving || resource.importing || Boolean(reload.dataset.busy);
    const label = resource.saving ? "正在保存" : resource.loading ? "正在读取" : resource.pathChanged ? "路径已变更 · 需重读" : resource.dirty ? "有未保存修改"
      : resource.error ? "读取失败" : resource.loaded ? "与服务器同步" : "未加载";
    setText(`state-${kind}`, label);
    $(`state-${kind}`).className = resource.dirty || resource.error || resource.pathChanged ? "badge warning" : "pill";
    if (kind === "proxies") {
      $("proxy-import-file").disabled = editor.disabled || resource.pathChanged;
      $("proxy-import-mode").disabled = editor.disabled || resource.pathChanged;
      renderProxyOverview();
      renderProxyEntries();
    }
  }

  function proxyLines(content) {
    return content.split(/\r?\n/).map((line) => line.trim()).filter((line) => line && !line.startsWith("#"));
  }

  function renderProxyEntries() {
    const lines = proxyLines($("editor-proxies").value);
    if (!changed("proxy-entry-rows", lines)) return;
    const rows = lines.slice(0, 200).map((line, index) => {
      const parts = line.split(":");
      const valid = [2, 4].includes(parts.length) && parts[0] && /^\d+$/.test(parts[1])
        && Number(parts[1]) >= 1 && Number(parts[1]) <= 65535;
      const row = el("tr");
      row.append(el("td", "", index + 1), el("td", "mono", valid ? `${parts[0]}:${parts[1]}` : "格式待修正"),
        el("td", "", valid ? parts.length === 4 ? "用户名 / 密码（隐藏）" : "无认证" : "—"),
        el("td", "", valid ? "有效" : "无效"));
      return row;
    });
    if (!rows.length) {
      const row = el("tr");
      const cell = el("td", "empty-state", "尚未添加静态代理");
      cell.colSpan = 4;
      row.append(cell);
      rows.push(row);
    }
    $("proxy-entry-rows").replaceChildren(...rows);
    setText("proxy-entry-summary", `编辑区共 ${lines.length} 条代理，预览显示前 ${Math.min(200, lines.length)} 条。凭据不会在预览表中显示。`);
  }

  function renderProxyOverview() {
    const resource = resourceState("proxies");
    setText("proxy-static-count", resource.loaded ? proxyLines(resource.original).length : "—");
    const fields = new Map(state.savedSettings.map((field) => [field.key, field]));
    const value = (key) => fields.get(key)?.value;
    if (fields.size) {
      const enabled = value("KOOIP_ENABLED") === true;
      const configured = ["KOOIP_USER_ID", "KOOIP_AUTH_NAME", "KOOIP_AUTH_PASSWORD"]
        .every((key) => fields.get(key)?.configured);
      const size = Number(value("KOOIP_SESSION_POOL_SIZE"));
      const validSize = Number.isInteger(size) && size >= 0;
      const capacity = !enabled || !configured ? 0 : value("KOOIP_STICKY_SESSION") === false
        ? 1 : validSize ? Math.max(1, size) : "—";
      setText("proxy-dynamic-count", capacity);
      setText("proxy-dynamic-note", !enabled ? "KKOIP 未启用" : !configured ? "已启用，但认证信息不完整"
        : capacity === "—" ? "会话池大小无效，请修正配置" : "配置就绪，实际连通性待检测");
      setText("proxy-pool-choice", ({ auto: "自动", static: "静态", kooip: "KKOIP" })[value("PROXY_POOL_PREFERENCE")] || "配置无效");
    }
    const task = state.tasks.find((item) => item.action === "proxy_test" && item.status === "completed" && item.result?.pools);
    state.proxyCheckId = task?.id || null;
    $("proxy-check-detail").hidden = !task;
    const pools = task?.result.pools;
    setText("proxy-health-count", pools ? `${pools.healthy ?? "—"} / ${pools.total ?? "—"}` : "未检测");
    setText("proxy-check-time", task ? `检测时间：${formatDate(task.finished_at || task.created_at)} · 历史快照` : "最近任务历史中没有已完成的代理检测");
    if (!changed("proxy-check-results", task || null)) return;
    $("proxy-check-results").replaceChildren(...(task ? [["static", "静态代理池"], ["kooip", "KKOIP 动态池"]].map(([key, label]) => {
      const pool = pools[key];
      const section = el("div", "notice subtle");
      section.append(el("strong", "", label), el("p", "", pool
        ? `检测时可用 ${pool.healthy} / ${pool.total} 条` : "该次任务没有返回此池的统计"));
      return section;
    }) : [el("p", "empty-state", "点击「检测代理池」后查看静态池与动态池的健康统计。")]));
  }

  async function importProxyFile() {
    const input = $("proxy-import-file");
    const file = input.files[0];
    const resource = resourceState("proxies");
    if (!file || resource.importing) return;
    const mode = $("proxy-import-mode").value;
    resource.importing = true;
    syncResource("proxies");
    try {
      if (file.size > 1024 * 1024) throw new Error("文件不能超过 1 MiB。");
      if (mode === "replace" && !(await UI.confirm({
        title: "替换编辑区内容",
        message: "替换将覆盖编辑区内容，包括未保存的修改。确定继续？",
        confirmText: "替换", tone: "warning",
      }))) return;
      const before = $("editor-proxies").value;
      const revision = resource.revision;
      const content = new TextDecoder("utf-8", { fatal: true }).decode(await file.arrayBuffer());
      if (content.includes("\0")) throw new Error("代理文件包含无效字符，请使用 UTF-8 文本。");
      if (resource.revision !== revision || resource.pathChanged || resource.saving
          || resource.loading || $("editor-proxies").value !== before) {
        throw new Error("读取文件期间编辑区或文件路径发生了变化，请重新导入。");
      }
      const combined = mode === "replace" ? content : `${before}${before && !before.endsWith("\n") ? "\n" : ""}${content}`;
      if (new Blob([combined]).size > 1024 * 1024) throw new Error("合并后的代理内容超过 1 MiB，请减少条目。");
      $("editor-proxies").value = combined;
      resource.dirty = combined !== resource.original;
      syncResource("proxies");
      notify("文件已导入编辑区，点击「保存代理文件」后才会生效。");
    } finally {
      resource.importing = false;
      input.value = "";
      syncResource("proxies");
    }
  }

  async function loadResource(kind, force = false) {
    const resource = resourceState(kind);
    if ((resource.loaded && !force) || resource.loading || resource.saving || resource.importing) return;
    resource.loading = true;
    resource.error = false;
    const revision = resource.revision;
    syncResource(kind);
    try {
      const data = await get(`/api/resources/${kind}`);
      if (revision !== resource.revision) return;
      resource.original = data.content || "";
      resource.loaded = true;
      resource.dirty = false;
      resource.pathChanged = false;
      $(`editor-${kind}`).value = resource.original;
      setText(`path-${kind}`, data.path);
    } catch (error) {
      resource.error = true;
      throw error;
    } finally {
      resource.loading = false;
      syncResource(kind);
    }
  }

  function activateResource(kind) {
    state.resourceKind = kind;
    document.querySelectorAll("[data-resource-kind]").forEach((tab) => {
      const selected = tab.dataset.resourceKind === kind;
      tab.classList.toggle("active", selected);
      tab.setAttribute("aria-selected", String(selected));
      tab.tabIndex = selected ? 0 : -1;
      $(`resource-${tab.dataset.resourceKind}`).hidden = !selected;
    });
    report(loadResource(kind));
  }

  async function saveResource(kind, button) {
    const resource = resourceState(kind);
    if (!resource.loaded || resource.pathChanged || resource.loading || resource.saving) return;
    const content = $(`editor-${kind}`).value;
    await perform(button, async () => {
      resource.saving = true;
      syncResource(kind);
      try {
        const data = await api(`/api/resources/${kind}`, { method: "PUT", body: { content } });
        resource.original = data.content ?? content;
        $(`editor-${kind}`).value = resource.original;
        resource.dirty = false;
        if (data.path) setText(`path-${kind}`, data.path);
        notify("资源文件已保存，后续任务将读取新内容。");
      } finally {
        resource.saving = false;
        syncResource(kind);
      }
    });
    syncResource(kind);
  }

  function settingChanged(control) {
    if (control.field.readonly) return false;
    if (control.field.secret) return Boolean(control.clear.checked || control.input.value !== "");
    if (control.field.type === "bool") return control.input.checked !== control.baseline;
    return control.input.value !== control.baseline;
  }

  function dirtySettings(scope = null) {
    return [...state.settingsControls.values()].filter((control) =>
      (!scope || settingsScope(control.field) === scope) && settingChanged(control));
  }

  function updateSettingsDirty() {
    state.settingsControls.forEach((control) => {
      control.row.classList.toggle("changed", settingChanged(control));
      if (control.field.secret) {
        control.input.disabled = Boolean(control.field.readonly || control.clear.checked
          || state.settingsSaving || state.settingsLoading);
      }
    });
    settingsScopes.forEach((scope) => {
      const count = dirtySettings(scope).length;
      setText(`${scope}-dirty-count`, count ? `${count} 项修改尚未保存` : "所有配置已同步");
      disable(`${scope}-save`, !count || state.settingsSaving || state.settingsLoading);
    });
  }

  function choiceLabel(key, value) {
    if (key === "YOUR_GENDER") return ({ "1": "男（1）", "2": "女（2）", "3": "其他（3）" })[value] || value;
    if (key === "PROXY_POOL_PREFERENCE" && value === "kooip") return "KKOIP（动态池）";
    const labels = { residential: "住宅代理", mobile: "移动代理", datacenter: "数据中心代理",
      auto: "自动", static: "静态代理", low: "低", medium: "中", high: "高" };
    return labels[value] ? `${labels[value]} · ${value}` : engineLabels[value] || value;
  }

  function renderSettings(fields, scope = null) {
    state.savedSettings = fields;
    state.settingsControls.forEach((control, key) => {
      if (!scope || settingsScope(control.field) === scope) state.settingsControls.delete(key);
    });
    const groups = new Map();
    const sections = [];
    fields.filter((field) => !scope || settingsScope(field) === scope).forEach((field) => {
      const groupName = field.group || "Behavior";
      if (!groups.has(groupName)) {
        const section = el("section", "card settings-section");
        section.dataset.group = groupName;
        const heading = el("div", "settings-group-header");
        const headingBody = el("div");
        const title = el("h2", "", groupLabels[groupName] || groupName);
        title.id = `settings-heading-${groupName}`;
        section.setAttribute("aria-labelledby", title.id);
        headingBody.append(title, el("p", "", `${groupName === "KooIP" ? "KKOIP" : groupName} · 各引擎对选项的支持以现有实现为准`));
        heading.append(headingBody);
        const grid = el("div", "settings-grid");
        section.append(heading, grid);
        groups.set(groupName, { section, grid });
        sections.push(section);
      }
      const row = el("div", "field setting-field");
      row.dataset.search = `${field.key} ${configLabels[field.key] || ""} ${groupName} ${groupLabels[groupName] || ""}`.toLocaleLowerCase();
      const label = el("label", "", configLabels[field.key] || field.key);
      const id = `setting-${field.key}`;
      label.htmlFor = id;
      label.append(el("span", "setting-key", field.key));
      const control = { field, row, input: null, clear: null, baseline: null };
      const shared = { id, name: field.key, "aria-describedby": `${id}-help`, disabled: Boolean(field.readonly) };
      row.append(label);
      let invalidValue = false;
      let input;
      if (field.secret) {
        input = UI.create("ui-input", { ...shared, type: "password", autocomplete: "new-password", maxlength: 4096,
          placeholder: field.configured ? "已配置 · 留空保留原值" : "未配置 · 输入新值" });
        row.append(input);
      } else if (field.type === "bool") {
        const checked = field.value === true || field.value === "true";
        input = UI.create("ui-switch", { ...shared, class: "setting-toggle", checked, "on-text": "已启用", "off-text": "未启用" });
        control.baseline = checked;
        row.append(input);
      } else {
        const value = Array.isArray(field.value) ? field.value.join(",") : field.value === null || field.value === undefined ? "" : String(field.value);
        if (field.choices?.length) {
          input = UI.create("ui-select", shared);
          row.append(input);
          const values = field.choices.map(String);
          const options = values.map((choice) => ({ value: choice, label: choiceLabel(field.key, choice) }));
          if (!values.includes(value)) options.unshift({ value, label: `${value || "空值"}（当前值无效）` });
          input.setOptions(options, value);
        } else if (field.type === "int") {
          invalidValue = !/^\d+$/.test(value) || number(value) > 86400000;
          input = UI.create("ui-number", { ...shared, min: 0, max: 86400000, step: 1,
            value: invalidValue ? "" : value, placeholder: invalidValue ? `当前无效值：${value}` : null });
          row.append(input);
        } else {
          input = UI.create("ui-input", { ...shared, type: "text", autocomplete: "off", maxlength: 4096, value });
          row.append(input);
        }
        control.baseline = input.value;
      }
      control.input = input;
      const help = el("p", "field-help", field.readonly
        ? "由服务器环境变量覆盖，只读。需在服务器调整后重启。"
        : field.secret ? (field.configured ? "已有凭据；不显示原值，留空不会修改。" : "尚未配置凭据。")
          : invalidValue ? `服务器当前值无效：${text(field.value)}。请填写合法整数以修正。`
            : field.type === "list" ? "使用英文逗号分隔多项，不要换行。" : field.type === "int" ? "整数值；单位与语义以配置项说明为准。" : "保存后供后续任务使用。");
      help.id = `${id}-help`;
      row.append(help);
      if (field.secret) {
        const clear = UI.create("ui-checkbox", { id: `${id}-clear`, class: "secret-clear compact",
          label: "显式清空此凭据", disabled: Boolean(field.readonly) });
        row.append(clear);
        control.clear = clear;
      }
      state.settingsControls.set(field.key, control);
      groups.get(groupName).grid.append(row);
    });
    settingsScopes.filter((item) => !scope || item === scope).forEach((item) => {
      $(`${item}-fields`).replaceChildren(...sections.filter((section) => settingsScope({ group: section.dataset.group }) === item));
    });
    if (scope !== "proxy-settings") {
      $("settings-group").setOptions([{ value: "", label: "全部分组" },
        ...[...groups.keys()].filter((group) => settingsScope({ group }) === "settings")
          .map((group) => ({ value: group, label: groupLabels[group] || group }))]);
    }
    state.settingsLoaded = true;
    filterSettings();
    updateSettingsDirty();
    renderProxyOverview();
  }

  function filterSettings() {
    const query = $("settings-search").value.trim().toLocaleLowerCase();
    const group = $("settings-group").value;
    let visible = 0;
    $("settings-fields").querySelectorAll(".settings-section").forEach((section) => {
      let matches = 0;
      section.querySelectorAll(".setting-field").forEach((row) => {
        const show = (!group || section.dataset.group === group) && (!query || row.dataset.search.includes(query));
        row.hidden = !show;
        if (show) matches += 1;
      });
      section.hidden = matches === 0;
      visible += matches;
    });
    $("settings-no-match").hidden = visible !== 0 || !state.settingsLoaded;
    const total = [...state.settingsControls.values()].filter((control) => settingsScope(control.field) === "settings").length;
    setText("settings-visible-count", `${visible} / ${total} 项通用配置 · 代理配置在独立菜单`);
  }

  async function loadSettings(scope = null) {
    if (state.settingsLoading || state.settingsSaving) return;
    if (!state.settingsLoaded) scope = null;
    state.settingsLoading = true;
    const unlocks = settingsScopes.map((item) => lockFields($(`${item}-form`)));
    settingsScopes.forEach((item) => { disable(`${item}-reload`, true); disable(`${item}-save`, true); });
    try {
      const data = await get("/api/settings");
      renderSettings(data.fields || [], scope);
    } finally {
      unlocks.forEach((unlock) => unlock());
      state.settingsLoading = false;
      settingsScopes.forEach((item) => disable(`${item}-reload`, false));
      updateSettingsDirty();
    }
  }

  /** 校验失败的配置项可能正被搜索或分组筛选隐藏，先清除筛选再聚焦。 */
  function revealSettingField(control) {
    if (!control.closest(".setting-field")?.hidden) return;
    $("settings-search").value = "";
    $("settings-group").value = "";
    filterSettings();
  }

  async function saveSettings(event) {
    event.preventDefault();
    const form = event.currentTarget;
    const scope = form.id.replace(/-form$/, "");
    const controls = dirtySettings(scope);
    if (!controls.length || state.settingsSaving) return;
    if (!UI.validate(form, { onInvalid: revealSettingField })) return;
    const clears = controls.filter((control) => control.field.secret && control.clear.checked);
    if (clears.length && !(await UI.confirm({
      title: "清空凭据",
      message: `确定清空以下凭据？相关服务可能停止工作：\n${clears.map((control) => control.field.key).join("\n")}`,
      confirmText: "确定清空", tone: "danger",
    }))) return;
    await perform($(`${scope}-save`), async () => {
      const values = {};
      controls.forEach((control) => {
        const { field, input, clear } = control;
        if (field.secret) values[field.key] = clear.checked ? "" : input.value;
        else if (field.type === "bool") values[field.key] = input.checked;
        else if (field.type === "int") {
          if (!input.value.trim() || !Number.isInteger(Number(input.value))) {
            throw new Error(`${field.key} 必须填写整数。`);
          }
          values[field.key] = Number(input.value);
        } else values[field.key] = input.value;
      });
      state.settingsSaving = true;
      const unlocks = settingsScopes.map((item) => lockFields($(`${item}-form`)));
      settingsScopes.forEach((item) => disable(`${item}-reload`, true));
      try {
        const data = await api("/api/settings", { method: "PUT", body: { values } });
        const fields = data.fields || (await get("/api/settings")).fields;
        renderSettings(fields || [], scope);
        Object.entries({ proxies: "PROXY_FILE", names: "NAMES_FILE", user_agents: "USER_AGENTS_FILE" }).forEach(([kind, key]) => {
          if (Object.hasOwn(values, key)) {
            const resource = resourceState(kind);
            resource.revision += 1;
            pendingGets.delete(`/api/resources/${kind}`);
            if (resource.loaded || resource.loading) resource.pathChanged = true;
            syncResource(kind);
          }
        });
        if (Object.hasOwn(values, "ENGINE_MODE") && !state.engineTouched) state.engineInitialized = false;
        notify("配置已保存。后续任务使用新配置；若更改了资源路径，请重新读取相应文件。");
      } finally {
        state.settingsSaving = false;
        unlocks.forEach((unlock) => unlock());
        settingsScopes.forEach((item) => disable(`${item}-reload`, false));
        updateSettingsDirty();
      }
    });
  }

  async function loadSystem() {
    const data = await get("/api/system");
    state.system = data;
    setText("system-python", `Python ${text(data.python)}`);
    setText("system-appium", data.appium_available ? "端口可达 · 未验证设备" : "端口不可达");
    setText("system-voice", data.voice_running ? "任务运行中 · 未验证端口" : "未运行");
    setText("system-smoke-status", data.browser_smoke_opt_in ? "Smoke 已启用" : "Smoke 未启用");
    $("system-smoke-status").className = `pill${data.browser_smoke_opt_in ? " success" : ""}`;
    if (changed("system-dependencies", data.dependencies)) {
      const nodes = Object.entries(data.dependencies || {}).map(([name, available]) => {
        const item = el("div", "dependency-item");
        item.append(el("span", "", name), el("span", `badge ${available ? "success" : "warning"}`, available ? "已发现" : "未发现"));
        return item;
      });
      $("system-dependencies").replaceChildren(...nodes);
    }
    if (changed("system-capabilities", data.browser_capabilities)) {
      const rows = [];
      Object.entries(data.browser_capabilities || {}).forEach(([engine, capabilities]) => {
        Object.entries(capabilities || {}).forEach(([key, capability]) => {
          const status = capabilityStatusLabels[capability.status] || [text(capability.status), "neutral"];
          const row = el("tr");
          const statusCell = el("td");
          statusCell.append(el("span", `badge ${status[1]}`, status[0]));
          row.append(
            el("td", "capability-engine", engineLabels[engine] || engine),
            el("td", "capability-name", capabilityLabels[key] || key),
            statusCell,
            el("td", "", capability.verified ? "已验证" : "未验证"),
          );
          rows.push(row);
        });
      });
      $("system-capability-rows").replaceChildren(...(rows.length ? rows : [
        (() => {
          const row = el("tr");
          const cell = el("td", "empty-state", "暂无能力矩阵");
          cell.colSpan = 4;
          row.append(cell);
          return row;
        })(),
      ]));
    }
    if (changed("system-notes", data.notes)) {
      $("system-notes").replaceChildren(...(data.notes || []).map((note) => el("li", "", systemNoteLabels[note] || text(note))));
    }
    syncActionButtons();
  }

  async function loadSession() {
    const revision = state.sessionRevision;
    let data;
    try {
      data = await get("/api/session");
    } catch (error) {
      if (revision === state.sessionRevision && error.status === 400) {
        state.session = null;
        state.sessionUnreadable = true;
        setText("saved-session-badge", "断点文件损坏");
        setText("saved-session-summary", "无法读取已保存的断点。请先备份服务器上的断点文件，再使用「清除断点」解除阻塞；已保存账号不受影响。");
        setText("saved-session-json", error.message);
        syncActionButtons();
      }
      throw error;
    }
    if (revision !== state.sessionRevision) return;
    state.session = data;
    state.sessionUnreadable = false;
    const remaining = number(data.remaining);
    setText("saved-session-badge", data.state ? `${remaining} 个待完成` : "无保存会话");
    setText("saved-session-summary", data.state
      ? remaining > 0 ? `发现未完成批次，剩余 ${remaining} 个账号可恢复。恢复会使用保存的配置继续顺序执行。` : "已保存会话中没有待完成账号，可清除断点后开始新批次。"
      : "当前没有保存的会话，可直接创建新的任务。");
    setText("saved-session-json", data.state ? json(data.state) : "没有保存的会话。");
    syncActionButtons();
  }

  function syncActionButtons() {
    syncCreation();
    ["account-health", "account-warm", "account-export"].forEach((id) => disable(id, !state.accountsLoaded || !state.accounts.length));
    const voiceTask = state.tasks.find((task) => task.action === "voice" && active(task));
    disable("voice-start", !state.tasksLoaded || Boolean(voiceTask));
    disable("voice-stop", !voiceTask || voiceTask.status === "stopping");
    const voiceStatus = voiceTask ? voiceTask.status === "stopping" ? ["停止中", "warning"] : ["运行中", "success"] : ["未运行", "neutral"];
    setText("voice-status", state.tasksLoaded ? voiceStatus[0] : "检查中");
    $("voice-status").className = `badge ${voiceStatus[1]}`;
    disable("session-resume", !state.session?.state || number(state.session?.remaining) <= 0);
    disable("session-clear", !state.session?.state && !state.sessionUnreadable);
    $("task-cancel").hidden = !active(state.selectedTask);
    disable("task-cancel", !active(state.selectedTask) || state.selectedTask?.status === "stopping");
    setText("task-cancel", state.selectedTask?.status === "stopping" ? "正在停止…" : "停止任务");
    if (state.settingsLoaded) updateSettingsDirty();
    Object.keys(state.resources).forEach(syncResource);
  }

  async function runToolAction(action, button) {
    const confirmations = {
      proxy_fetch: ["获取免费代理", "此操作会从公开来源获取并测试代理，并修改服务器上的代理文件。\n免费代理可能不可靠或存在隐私风险。确定继续？", "warning"],
      migrate: ["执行数据迁移", "确定执行数据迁移？旧格式账号将导入数据库。\n请确认已做好备份。", "warning"],
      telegram_test: ["发送测试通知", "确定向已配置的 Telegram 聊天发送真实测试通知？", "primary"],
      resume: ["恢复保存的会话", "确定恢复保存的会话？任务会使用保存的批次参数在服务器继续运行。", "primary"],
      voice: ["启动语音验证码服务", "语音服务仅监听服务器 localhost:5000，必须配置非空且非 changeme 的 VOICE_SERVER_TOKEN。\n它独立运行、不使用控制台登录 Cookie，也不会被现有注册流程调用。\n远程使用需受保护的 HTTPS 反向代理。确定启动？", "warning"],
    };
    const confirmation = confirmations[action];
    if (confirmation && !(await UI.confirm({
      title: confirmation[0], message: confirmation[1], confirmText: confirmation[0], tone: confirmation[2],
    }))) return;
    await perform(button, () => startTask(action));
  }

  function setConnection(ok, error = "") {
    $("connection-status").classList.toggle("offline", !ok);
    setText("connection-text", ok ? "实时同步" : "同步中断");
    $("connection-error").hidden = ok;
    if (ok) setText("last-sync", `最后同步 ${new Date().toLocaleTimeString("zh-CN", { hour12: false })}`);
    else setText("connection-error", `状态同步暂时中断，当前数据可能不是最新：${error}。将自动重试；未保存的编辑不会丢失。`);
  }

  async function refreshCurrent() {
    await Promise.all([loadOverview(), loadTasks()]);
    if (state.page === "tasks") await loadTaskDetail();
    if (state.page === "accounts") await loadAccounts();
    if (state.page === "tools") await Promise.all([loadSystem(), loadSession()]);
    if (state.page === "settings" && !state.settingsLoaded) await loadSettings();
    if (state.page === "proxies") {
      if (!state.settingsLoaded) await loadSettings();
      if (!resourceState("proxies").loaded) await loadResource("proxies");
      renderProxyOverview();
    }
    if (state.page === "resources" && !resourceState(state.resourceKind).loaded) await loadResource(state.resourceKind);
    setConnection(true);
  }

  async function poll() {
    window.clearTimeout(state.pollTimer);
    if (state.polling || document.hidden || state.redirecting) {
      if (!state.redirecting) state.pollTimer = window.setTimeout(poll, 3000);
      return;
    }
    state.polling = true;
    try {
      await refreshCurrent();
    } catch (error) {
      if (!state.redirecting) setConnection(false, error.message);
    } finally {
      state.polling = false;
      if (!state.redirecting) state.pollTimer = window.setTimeout(poll, 3000);
    }
  }

  $("nav-toggle").addEventListener("click", () => {
    if ($("sidebar").classList.contains("open")) closeNav(true);
    else openNav();
  });
  $("nav-backdrop").addEventListener("click", () => closeNav(true));
  mobileQuery.addEventListener("change", () => closeNav());
  document.addEventListener("keydown", (event) => {
    if (!$("sidebar").classList.contains("open")) return;
    if (event.key === "Escape") closeNav(true);
    if (event.key === "Tab") {
      const focusable = [...$("sidebar").querySelectorAll("a, button")];
      const first = focusable[0];
      const last = focusable[focusable.length - 1];
      if (event.shiftKey && document.activeElement === first) { event.preventDefault(); last.focus(); }
      else if (!event.shiftKey && document.activeElement === last) { event.preventDefault(); first.focus(); }
    }
  });
  document.querySelectorAll(".sidebar a").forEach((link) => {
    link.addEventListener("click", () => {
      if (link.getAttribute("href") === `#${state.page}`) closeNav(true);
    });
  });
  document.querySelector(".skip-link").addEventListener("click", (event) => {
    event.preventDefault();
    closeNav();
    $("main-content").focus();
  });
  window.addEventListener("hashchange", () => report(showPage()));
  document.addEventListener("visibilitychange", () => {
    if (!document.hidden) report(poll());
  });
  window.addEventListener("beforeunload", (event) => {
    if (dirtySettings().length || Object.values(state.resources).some((resource) => resource.dirty)) {
      event.preventDefault();
      event.returnValue = "";
    }
  });
  $("refresh-button").addEventListener("click", () => perform($("refresh-button"), async () => {
    await refreshCurrent();
    notify("状态已刷新，配置与资源的未保存编辑保持不变。");
  }));

  $("create-form").addEventListener("submit", submitCreation);
  $("create-form").addEventListener("input", syncCreation);
  $("create-form").addEventListener("change", (event) => {
    if (event.target.id === "create-mode") {
      $("create-sms").checked = creationMode() === "premium";
    }
    if (event.target.id === "create-sms" && ["ghost", "premium"].includes(creationMode())) {
      $("create-mode").value = $("create-sms").checked ? "premium" : "ghost";
    }
    if (event.target.id === "create-engine") state.engineTouched = true;
    syncCreation();
  });
  $("task-cancel").addEventListener("click", () => report(cancelTask(state.selectedTask, $("task-cancel"))));
  $("log-follow").addEventListener("change", () => {
    if ($("log-follow").checked) $("task-logs").scrollTop = $("task-logs").scrollHeight;
  });
  document.addEventListener("click", (event) => {
    const taskButton = event.target.closest("[data-task-id]");
    if (taskButton) goToTask(taskButton.dataset.taskId);
    const actionButton = event.target.closest("[data-action]");
    if (actionButton) report(runToolAction(actionButton.dataset.action, actionButton));
  });

  $("account-search").addEventListener("input", () => renderAccountRows());
  $("account-status-filter").addEventListener("change", () => renderAccountRows());
  $("select-all-accounts").addEventListener("change", (event) => {
    filteredAccounts().forEach((account) => {
      if (event.target.checked) state.selectedAccounts.add(Number(account.id));
      else state.selectedAccounts.delete(Number(account.id));
    });
    updateSelection();
  });
  $("account-rows").addEventListener("change", (event) => {
    if (!event.target.matches("[data-account-id]")) return;
    const id = Number(event.target.dataset.accountId);
    if (event.target.checked) state.selectedAccounts.add(id);
    else state.selectedAccounts.delete(id);
    updateSelection();
  });
  $("clear-selection").addEventListener("click", () => {
    state.selectedAccounts.clear();
    updateSelection();
  });
  $("account-health").addEventListener("click", () => report(runAccountTask("health", $("account-health"))));
  $("account-export").addEventListener("click", () => report(exportAccounts()));
  $("warm-form").addEventListener("submit", (event) => {
    event.preventDefault();
    report(runAccountTask("warm", $("account-warm")));
  });

  document.querySelectorAll("[data-resource-kind]").forEach((tab) => {
    tab.addEventListener("click", () => activateResource(tab.dataset.resourceKind));
    tab.addEventListener("keydown", (event) => {
      const tabs = [...document.querySelectorAll("[data-resource-kind]")];
      const index = tabs.indexOf(tab);
      let next;
      if (event.key === "ArrowRight") next = (index + 1) % tabs.length;
      if (event.key === "ArrowLeft") next = (index + tabs.length - 1) % tabs.length;
      if (event.key === "Home") next = 0;
      if (event.key === "End") next = tabs.length - 1;
      if (next === undefined) return;
      event.preventDefault();
      tabs[next].focus();
      activateResource(tabs[next].dataset.resourceKind);
    });
  });
  document.querySelectorAll(".resource-form").forEach((form) => {
    const kind = form.dataset.kind;
    $(`editor-${kind}`).addEventListener("input", () => {
      resourceState(kind).dirty = $(`editor-${kind}`).value !== resourceState(kind).original;
      syncResource(kind);
    });
    form.addEventListener("submit", (event) => {
      event.preventDefault();
      report(saveResource(kind, form.querySelector("[data-resource-save]")));
    });
  });
  document.querySelectorAll("[data-resource-reload]").forEach((button) => {
    button.addEventListener("click", () => report((async () => {
      const kind = button.dataset.resourceReload;
      if (resourceState(kind).dirty && !(await UI.confirm({
        title: "重新读取文件",
        message: "重新读取会丢弃此文件未保存的修改。确定继续？",
        confirmText: "重新读取", tone: "warning",
      }))) return;
      await perform(button, () => loadResource(kind, true));
      syncResource(kind);
    })()));
  });
  $("proxy-import-file").addEventListener("change", () => report(importProxyFile()));
  $("proxy-check-detail").addEventListener("click", () => {
    if (state.proxyCheckId) goToTask(state.proxyCheckId);
  });

  $("settings-search").addEventListener("input", filterSettings);
  $("settings-group").addEventListener("change", filterSettings);
  settingsScopes.forEach((scope) => {
    $(`${scope}-form`).addEventListener("input", updateSettingsDirty);
    $(`${scope}-form`).addEventListener("change", updateSettingsDirty);
    $(`${scope}-form`).addEventListener("submit", saveSettings);
    $(`${scope}-reload`).addEventListener("click", () => report((async () => {
      if (dirtySettings(scope).length && !(await UI.confirm({
        title: "重新读取配置",
        message: "重新读取会丢弃本页未保存的配置修改。确定继续？",
        confirmText: "重新读取", tone: "warning",
      }))) return;
      await perform($(`${scope}-reload`), () => loadSettings(scope));
    })()));
  });
  $("settings-validate").addEventListener("click", () => report((async () => {
    if (dirtySettings().length && !(await UI.confirm({
      title: "校验已保存配置",
      message: "您有未保存的修改。校验只检查服务器已保存的配置，不包含这些修改。继续校验？",
      confirmText: "继续校验", tone: "warning",
    }))) return;
    await perform($("settings-validate"), () => startTask("validate", {}, "/api/settings/validate"));
  })()));
  $("system-refresh").addEventListener("click", () => perform($("system-refresh"), async () => {
    await Promise.all([loadSystem(), loadSession(), loadTasks()]);
    notify("服务器环境与会话状态已更新。");
  }));
  $("voice-start").addEventListener("click", () => report(runToolAction("voice", $("voice-start"))));
  $("voice-stop").addEventListener("click", () => report(cancelTask(state.tasks.find((task) => task.action === "voice" && active(task)), $("voice-stop"))));
  $("session-resume").addEventListener("click", () => report(runToolAction("resume", $("session-resume"))));
  $("session-clear").addEventListener("click", () => report((async () => {
    if (!(await UI.confirm({
      title: "清除会话断点",
      message: "确定永久清除服务器保存的会话断点？\n此操作无法撤销，但不会删除已保存的账号。",
      confirmText: "永久清除", tone: "danger",
    }))) return;
    await perform($("session-clear"), async () => {
      await api("/api/session", { method: "DELETE", body: {} });
      state.sessionRevision += 1;
      pendingGets.delete("/api/session");
      await loadSession();
      notify("会话断点已清除，已保存账号不受影响。");
    });
  })()));

  syncActionButtons();
  report(showPage({ focus: false }));
  report(poll());
})();
