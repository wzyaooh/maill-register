"""Session-authenticated API. Automation only runs in supervised workers."""
import csv
import importlib.util
import io
import json
import logging
import os
import re
import secrets
import socket
import sys
import threading
import time
from collections import Counter
from datetime import timedelta
from pathlib import Path

from flask import Flask, jsonify, redirect, render_template, request, session, url_for
from werkzeug.exceptions import HTTPException
from werkzeug.security import check_password_hash, generate_password_hash

from core.database import DatabaseManager
from core.browser_capabilities import capability_report, get_capability_matrix
from core.profile_runtime import ProfileRuntime, proxy_launch_config
from core.secret_safety import (
    SAFE_ACCOUNT_EXPORT_FIELDS,
    redact_text,
    safe_exception_message,
    safe_account_metadata_rows,
    sanitize_operation_value,
)
from web.configuration import Configuration, is_secret
from web.tasks import ACTIVE, TaskManager


ROOT = Path(__file__).resolve().parents[1]
ANSI = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]")


def redact(value, sensitive):
    value = sanitize_operation_value(value, sensitive, preserve_sensitive=True)
    if isinstance(value, dict):
        return {redact(key, sensitive): redact(item, sensitive) for key, item in value.items()}
    if isinstance(value, list):
        return [redact(item, sensitive) for item in value]
    if isinstance(value, str):
        return ANSI.sub("", redact_text(value, sensitive))
    return value


def _mask_proxy_resource(content):
    """Keep proxy endpoints useful while removing userinfo from API output."""
    masked = []
    for raw_line in str(content or "").splitlines(keepends=True):
        newline = "\n" if raw_line.endswith("\n") else ""
        line = raw_line[:-1] if newline else raw_line
        if not line.strip():
            masked.append(raw_line)
            continue
        if line.lstrip().startswith("#"):
            # Comments are still returned by the resource API and often carry
            # copied proxy examples.  Preserve their endpoint so an unchanged
            # API round-trip can restore the original comment line.
            masked.append(_mask_proxy_comment(line) + newline)
            continue
        config = proxy_launch_config(line.strip())
        if config and ("username" in config or "password" in config):
            endpoint = config["server"].split("://", 1)[-1]
            masked.append(endpoint + ":[redacted]:[redacted]" + newline)
        else:
            masked.append(redact_text(line, ()) + newline)
    return "".join(masked)


def _proxy_config_from_token(token):
    """Parse a proxy token after trimming comment punctuation."""
    candidate = str(token or "").strip().strip("()[]{}<>,;\"'")
    config = proxy_launch_config(candidate)
    if config and ("username" in config or "password" in config):
        return config
    return None


def _mask_proxy_comment(line):
    """Mask proxy credentials embedded in a comment while retaining context."""
    match = re.match(r"^(\s*#\s*)(.*)$", str(line))
    if not match:
        return redact_text(line, ())
    prefix, body = match.groups()
    # A comment often contains prose before a copied canonical proxy.  Replace
    # only the token that parses as a proxy and redact any other labelled
    # credential text through the normal structural sanitizer.
    pieces = re.split(r"(\s+)", body)
    changed = False
    for index, piece in enumerate(pieces):
        config = _proxy_config_from_token(piece)
        if not config:
            pieces[index] = redact_text(piece, ())
            continue
        endpoint = config["server"].split("://", 1)[-1]
        pieces[index] = endpoint + ":[redacted]:[redacted]"
        changed = True
    masked = prefix + "".join(pieces)
    return masked if changed else redact_text(line, ())


def _proxy_endpoint_from_line(line):
    """Find a credential-bearing or masked proxy endpoint in a resource line."""
    text = str(line or "")
    stripped = text.strip()
    comment = stripped.startswith("#")
    candidates = [stripped]
    if comment:
        candidates = [stripped[1:].strip()] + re.split(r"\s+", stripped[1:].strip())
    for candidate in candidates:
        config = _proxy_config_from_token(candidate)
        if config:
            return config["server"].split("://", 1)[-1], comment
    return None, comment


def _restore_masked_proxy_resource(content, original):
    """Restore credentials for unchanged masked lines before a resource save."""
    originals = {}
    for raw_line in str(original or "").splitlines():
        endpoint, comment = _proxy_endpoint_from_line(raw_line)
        if endpoint:
            originals.setdefault((endpoint, comment), raw_line)
    restored = []
    for raw_line in str(content or "").splitlines(keepends=True):
        newline = "\n" if raw_line.endswith("\n") else ""
        line = raw_line[:-1] if newline else raw_line
        endpoint, comment = _proxy_endpoint_from_line(line)
        if endpoint:
            original_line = originals.get((endpoint, comment))
            if original_line is not None and "[redacted]" in line:
                line = original_line
        restored.append(line + newline)
    return "".join(restored)


def create_app(root=None, password=None, secret_key=None, environment=None, code_root=None, deployment=None):
    if deployment not in (None, "dev", "prod"):
        raise ValueError("Environment must be dev or prod")
    root = Path(root or ROOT).resolve()
    configuration = Configuration(root, environment, code_root=code_root)
    scheduler_settings = configuration.compensation_scheduler_settings()
    local = {}
    if configuration.path.exists():
        from dotenv import dotenv_values
        local = dotenv_values(configuration.path, interpolate=False)
    env = configuration.environment
    password = password or env.get("WEB_ADMIN_PASSWORD") or local.get("WEB_ADMIN_PASSWORD")
    if not password or len(password) < 16:
        raise ValueError("Set WEB_ADMIN_PASSWORD to a unique password of at least 16 characters")
    app = Flask(__name__)
    app.config.update(
        SECRET_KEY=secret_key or env.get("WEB_SECRET_KEY") or local.get("WEB_SECRET_KEY") or secrets.token_hex(32),
        SESSION_COOKIE_NAME=f"gmail_web_{deployment}" if deployment else "gmail_web_session",
        DEPLOYMENT=deployment,
        SESSION_COOKIE_HTTPONLY=True,
        SESSION_COOKIE_SAMESITE="Strict",
        SESSION_COOKIE_SECURE=(env.get("WEB_COOKIE_SECURE", local.get("WEB_COOKIE_SECURE", "false")).lower() == "true"),
        PERMANENT_SESSION_LIFETIME=timedelta(hours=8),
        MAX_CONTENT_LENGTH=2 * 1024 * 1024,
    )
    password_hash = generate_password_hash(password, method="pbkdf2:sha256:600000")
    db = DatabaseManager(str(root / "data/database.db"))
    # Reconcile the filesystem profile registry with the account database at
    # startup.  This is diagnostic/recoverable: a malformed or stale profile
    # must be visible to operators, but it must not prevent the web console
    # from starting and exposing the rest of the account data.
    profile_runtime = ProfileRuntime(str(root))
    try:
        profile_diagnostics = profile_runtime.reconcile(database=db)
    except Exception as exc:
        app_logger = logging.getLogger("gmail_creator_web")
        # Reconciliation errors are operator diagnostics, not a channel for
        # exception payloads.  Lower layers may include credentials or proxy
        # userinfo in their messages, so retain only the exception type and a
        # stable public status.
        app_logger.warning("Profile reconciliation failed: %s", type(exc).__name__)
        profile_diagnostics = [{
            "state": "corrupt", "error_code": "reconciliation_failed",
            "status": "profile_unavailable", "message": "profile reconciliation failed",
        }]
    tasks = TaskManager(root, configuration)
    app.extensions.update(
        web_configuration=configuration, web_tasks=tasks, web_database=db,
        profile_runtime=profile_runtime, profile_diagnostics=profile_diagnostics,
        compensation_scheduler_settings=scheduler_settings,
    )
    failures = {}
    auth_lock = threading.Lock()

    def csrf_token():
        if "csrf" not in session:
            session["csrf"] = secrets.token_urlsafe(32)
        return session["csrf"]

    def json_body():
        data = request.get_json()
        if not isinstance(data, dict):
            raise ValueError("Expected a JSON object")
        return data

    def sensitive_values():
        values = configuration.values()
        items = [value for key, value in values.items() if is_secret(key) and value]
        items.extend(account["password"] for account in db.get_all_accounts() if account["password"])
        items.append(password)
        configured_path = root / values["PROXY_FILE"]
        resource = configured_path.read_text(encoding="utf-8") if configured_path.is_file() else ""
        items.extend(line.strip() for line in resource.splitlines()
                     if len(line.strip().split(":")) == 4 and not line.lstrip().startswith("#"))
        items.extend(tasks.store._known_secret_values())
        return tuple(dict.fromkeys(item for item in items if item))

    def public(value):
        return redact(value, sensitive_values())

    def public_task(task, sensitive=None):
        sensitive = sensitive_values() if sensitive is None else sensitive
        safe_task = sanitize_operation_value(task, sensitive)
        progress = safe_task["progress"]
        return {
            **safe_task,
            "error": redact(safe_task["error"], sensitive),
            "result": redact(safe_task["result"], sensitive),
            "progress": ({**progress, "message": redact(progress.get("message", ""), sensitive)}
                         if progress else None),
        }

    def public_tasks():
        sensitive = sensitive_values()
        return [public_task(task, sensitive) for task in tasks.store.list()]

    @app.before_request
    def protect():
        if request.endpoint == "static":
            return None
        authenticated = session.get("authenticated") and (
            deployment is None or session.get("deployment") == deployment
        )
        if request.endpoint != "login" and not authenticated:
            if request.path.startswith("/api/"):
                return jsonify(error="Authentication required"), 401
            return redirect(url_for("login"))
        if request.method not in ("GET", "HEAD", "OPTIONS"):
            supplied = request.headers.get("X-CSRF-Token") or request.form.get("csrf_token", "")
            expected = session.get("csrf", "")
            if not supplied or not expected or not secrets.compare_digest(supplied.encode(), expected.encode()):
                return jsonify(error="Invalid CSRF token; reload the page"), 403
        return None

    @app.after_request
    def headers(response):
        response.headers["Cache-Control"] = "no-store"
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["Content-Security-Policy"] = (
            "default-src 'self'; script-src 'self'; style-src 'self'; "
            "img-src 'self' data:; connect-src 'self'; object-src 'none'; "
            "base-uri 'none'; frame-ancestors 'none'; form-action 'self'"
        )
        if app.config["SESSION_COOKIE_SECURE"]:
            response.headers["Strict-Transport-Security"] = "max-age=31536000"
        return response

    @app.errorhandler(ValueError)
    def invalid(exc):
        try:
            sensitive = sensitive_values()
        except Exception:
            sensitive = ()
        return jsonify(error=safe_exception_message(
            exc, sensitive, fallback="Invalid request"
        )), 400

    @app.errorhandler(KeyError)
    def missing(exc):
        return jsonify(error="Not found"), 404

    @app.errorhandler(HTTPException)
    def http_error(exc):
        try:
            sensitive = sensitive_values()
        except Exception:
            sensitive = ()
        return jsonify(error=safe_exception_message(
            getattr(exc, "description", ""), sensitive,
            fallback="HTTP request failed",
        )), exc.code

    @app.errorhandler(OSError)
    def storage_error(exc):
        # Logging a traceback here can copy credentials embedded in a driver or
        # filesystem exception.  Keep only the stable type; the client gets a
        # generic storage error below.
        app.logger.warning("Web storage operation failed: %s", type(exc).__name__)
        return jsonify(error="Server I/O operation failed; check server logs"), 500

    @app.route("/login", methods=["GET", "POST"])
    def login():
        error = None
        status = 200
        if request.method == "POST":
            ip = request.remote_addr or "unknown"
            with auth_lock:
                current = time.monotonic()
                for address in list(failures):
                    if not failures[address] or failures[address][-1] < current - 900:
                        failures.pop(address)
                attempts = failures.setdefault(ip, [])
                if len(attempts) >= 5 or len(failures) > 4096:
                    error, status = "Too many attempts. Try again in 15 minutes.", 429
                elif not check_password_hash(password_hash, request.form.get("password", "")):
                    attempts.append(current)
                    error, status = "Incorrect administrator password.", 401
                else:
                    failures.pop(ip, None)
            if error is None:
                session.clear()
                session["authenticated"] = True
                session["deployment"] = deployment
                session.permanent = True
                csrf_token()
                return redirect(url_for("index"))
        return render_template("login.html", csrf_token=csrf_token(), error=error, deployment=deployment), status

    @app.post("/logout")
    def logout():
        session.clear()
        return redirect(url_for("login"))

    @app.get("/")
    def index():
        return render_template("index.html", csrf_token=csrf_token(), deployment=deployment)

    @app.get("/api/overview")
    def overview():
        accounts = db.get_all_accounts()
        account_metadata = safe_account_metadata_rows(accounts)
        total = len(accounts)
        active = sum(a["status"] == "active" for a in accounts)
        values = configuration.values()
        services = {key: bool(value) for key, value in values.items()
                    if key.endswith("_API_KEY") or key in ("TELEGRAM_BOT_TOKEN", "VOICE_SERVER_TOKEN")}
        history = db.get_session_history(30)
        for item in history:
            for key in ("strategies_used", "errors"):
                item[key] = json.loads(item[key])
            item["errors"] = public(item["errors"])
        return jsonify(
            accounts={"total": total, "active": active, "success_rate": active / total * 100 if total else 0,
                      "strategies": dict(Counter(
                          a.get("strategy") or "unknown" for a in account_metadata
                      )),
                      "sms_services": dict(Counter(
                          a.get("sms_service") for a in account_metadata
                          if a.get("sms_service")
                      ))},
            sessions=history, tasks=public_tasks(), services=services, engine=values["ENGINE_MODE"],
        )

    @app.get("/api/system")
    def system():
        dependencies = {name: importlib.util.find_spec(name) is not None for name in (
            "rich", "dotenv", "requests", "aiohttp", "numpy", "playwright", "playwright_stealth",
            "selenium", "appium", "flask", "speech_recognition", "pydub", "waitress")}
        try:
            with socket.create_connection(("127.0.0.1", 4723), timeout=0.3):
                appium_available = True
        except OSError:
            appium_available = False
        diagnostics = []
        for item in app.extensions.get("profile_diagnostics", []):
            if isinstance(item, dict):
                diagnostics.append({key: value for key, value in item.items() if key != "path"})
        # The matrix is a public contract, while verification evidence is
        # deliberately opt-in and local.  Do not infer verification merely
        # because a Python package is importable or a port is reachable.
        browser_capabilities = {
            engine: capability_report(engine)
            for engine in get_capability_matrix()
        }
        return jsonify(python=sys.version.split()[0], dependencies=dependencies, environment=deployment or "legacy",
                       appium_available=appium_available,
                       voice_running=any(t["action"] == "voice" for t in tasks.store.active()),
                       browser_capabilities=browser_capabilities,
                       browser_smoke_opt_in=os.environ.get("RUN_REAL_BROWSER_SMOKE") == "1",
                       profiles=diagnostics,
                       notes=[
                           "Dependency discovery does not verify browser binaries or external services.",
                           "Appium port availability does not verify a connected Android device.",
                           "Appium registration is explicitly disabled until the native lifecycle contract is complete.",
                           "Proxy/behavior options reflect existing engine capabilities; not all engines use every option.",
                           "Use HTTPS for remote access; configure WEB_COOKIE_SECURE=true behind TLS.",
                       ])

    @app.get("/api/compensation-scheduler")
    def compensation_scheduler_status():
        from web.compensation_scheduler import read_scheduler_status

        stale_after = max(
            scheduler_settings["interval_seconds"] * 2,
            scheduler_settings["time_budget_seconds"] * 2,
            60,
        )
        return jsonify(status=read_scheduler_status(
            root,
            enabled=scheduler_settings["enabled"],
            stale_after_seconds=stale_after,
        ))

    @app.get("/api/accounts")
    def accounts():
        rows = []
        for account in db.get_all_accounts():
            # Keep this projection allow-listed.  Free-form columns such as
            # ``notes`` may contain OTPs or copied service credentials from
            # legacy imports and must never become API metadata by accident.
            row = safe_account_metadata_rows([account])[0]
            row["has_password"] = bool(account["password"])
            rows.append(row)
        return jsonify(accounts=rows)

    @app.post("/api/accounts/<int:account_id>/password")
    def account_password(account_id):
        # Credentials are needed by supervised workers, never by the browser
        # console.  Keep the route as an explicit denial for old clients.
        return jsonify(error="Password retrieval is disabled"), 403

    @app.post("/api/accounts/export")
    def export():
        kind = json_body().get("format")
        accounts = safe_account_metadata_rows(db.get_all_accounts())
        if kind == "json":
            content, mimetype = json.dumps(accounts, ensure_ascii=False, indent=2), "application/json"
        elif kind == "txt":
            content = "".join(f"{a.get('email', '')}\n" for a in accounts)
            mimetype = "text/plain"
        elif kind == "csv":
            buffer = io.StringIO()
            writer = csv.writer(buffer)
            columns = list(SAFE_ACCOUNT_EXPORT_FIELDS)
            writer.writerow(columns)
            # Prevent spreadsheet formula execution when exported cells are opened.
            writer.writerows([
                ("'" + str(a.get(key, ""))) if str(a.get(key, "")).startswith(("=", "+", "-", "@", "\t", "\r"))
                else a.get(key, "") for key in columns
            ] for a in accounts)
            content, mimetype = "\ufeff" + buffer.getvalue(), "text/csv"
        else:
            raise ValueError("format must be csv, json or txt")
        response = app.response_class(content, mimetype=mimetype)
        response.headers["Content-Disposition"] = f'attachment; filename="accounts.{kind}"'
        return response

    @app.get("/api/settings")
    def settings():
        return jsonify(fields=configuration.fields())

    def require_idle():
        if any(task["action"] != "voice" for task in tasks.store.active()):
            raise ValueError("Wait for the active task to finish before changing configuration or session state")

    @app.put("/api/settings")
    def save_settings():
        with tasks.lock:
            require_idle()
            configuration.save(json_body().get("values"))
        return jsonify(fields=configuration.fields())

    @app.route("/api/resources/<kind>", methods=["GET", "PUT"])
    def resource(kind):
        with tasks.lock:
            if request.method == "PUT":
                require_idle()
                payload = json_body().get("content")
                if kind == "proxies":
                    current = configuration.read_resource(kind)["content"]
                    payload = _restore_masked_proxy_resource(payload, current)
                configuration.save_resource(kind, payload)
            result = configuration.read_resource(kind)
            if kind == "proxies":
                result["content"] = _mask_proxy_resource(result["content"])
            return jsonify(result)

    @app.route("/api/session", methods=["GET", "DELETE"])
    def saved_session():
        from core.session_resume import SessionManager
        manager = SessionManager(str(root / "data/session_state.json"))
        with tasks.lock:
            if request.method == "DELETE":
                require_idle()
                manager.clear_state()
            path = Path(manager.filepath)
            # Unlike the CLI convenience loader, corrupt state must be visible.
            state = json.loads(path.read_text(encoding="utf-8")) if path.exists() else None
            remaining = len(manager.get_remaining(state))
        return jsonify(state=public(state), remaining=remaining)

    @app.get("/api/tasks")
    def list_tasks():
        return jsonify(tasks=public_tasks())

    @app.get("/api/tasks/<task_id>")
    def task_detail(task_id):
        task = tasks.store.get(task_id)
        if task is None:
            raise KeyError(task_id)
        return jsonify(task=public_task(task), logs=public(tasks.store.logs(task_id)))

    @app.post("/api/tasks")
    def start_task():
        data = json_body()
        return jsonify(task=public_task(tasks.start(data.get("action"), data.get("params", {})))), 202

    @app.post("/api/settings/validate")
    def validate():
        return jsonify(task=public_task(tasks.start("validate", {}))), 202

    @app.post("/api/tasks/<task_id>/cancel")
    def cancel(task_id):
        return jsonify(task=public_task(tasks.cancel(task_id)))

    return app
