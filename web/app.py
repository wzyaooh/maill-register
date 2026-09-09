"""Session-authenticated API. Automation only runs in supervised workers."""
import csv
import importlib.util
import io
import json
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
from web.configuration import Configuration, is_secret
from web.tasks import ACTIVE, TaskManager


ROOT = Path(__file__).resolve().parents[1]
ANSI = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]")


def redact(value, sensitive):
    if isinstance(value, dict):
        return {redact(key, sensitive): ("[redacted]" if is_secret(str(key).upper()) and isinstance(item, str)
                                        else redact(item, sensitive))
                for key, item in value.items()}
    if isinstance(value, list):
        return [redact(item, sensitive) for item in value]
    if not isinstance(value, str):
        return value
    value = ANSI.sub("", value)
    for secret in sorted(sensitive, key=len, reverse=True):
        if secret:
            value = value.replace(secret, "[redacted]")
    value = re.sub(r"(?i)(password\s*[:=]\s*)[^\s|]+", r"\1[redacted]", value)
    return value


def create_app(root=None, password=None, secret_key=None, environment=None):
    root = Path(root or ROOT).resolve()
    configuration = Configuration(root, environment)
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
        SECRET_KEY=secret_key or env.get("WEB_SECRET_KEY") or secrets.token_hex(32),
        SESSION_COOKIE_NAME="gmail_web_session",
        SESSION_COOKIE_HTTPONLY=True,
        SESSION_COOKIE_SAMESITE="Strict",
        SESSION_COOKIE_SECURE=(env.get("WEB_COOKIE_SECURE", local.get("WEB_COOKIE_SECURE", "false")).lower() == "true"),
        PERMANENT_SESSION_LIFETIME=timedelta(hours=8),
        MAX_CONTENT_LENGTH=2 * 1024 * 1024,
    )
    password_hash = generate_password_hash(password, method="pbkdf2:sha256:600000")
    db = DatabaseManager(str(root / "data/database.db"))
    tasks = TaskManager(root, configuration)
    app.extensions.update(web_configuration=configuration, web_tasks=tasks, web_database=db)
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
        return items

    def public(value):
        return redact(value, sensitive_values())

    def public_task(task, sensitive=None):
        sensitive = sensitive_values() if sensitive is None else sensitive
        progress = task["progress"]
        return {
            **task,
            "error": redact(task["error"], sensitive),
            "result": redact(task["result"], sensitive),
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
        if request.endpoint != "login" and not session.get("authenticated"):
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
        return jsonify(error=str(exc)), 400

    @app.errorhandler(KeyError)
    def missing(exc):
        return jsonify(error="Not found"), 404

    @app.errorhandler(HTTPException)
    def http_error(exc):
        return jsonify(error=exc.description), exc.code

    @app.errorhandler(OSError)
    def storage_error(exc):
        app.logger.exception("Web operation failed")
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
                session.permanent = True
                csrf_token()
                return redirect(url_for("index"))
        return render_template("login.html", csrf_token=csrf_token(), error=error), status

    @app.post("/logout")
    def logout():
        session.clear()
        return redirect(url_for("login"))

    @app.get("/")
    def index():
        return render_template("index.html", csrf_token=csrf_token())

    @app.get("/api/overview")
    def overview():
        accounts = db.get_all_accounts()
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
                      "strategies": dict(Counter(a["strategy"] or "unknown" for a in accounts)),
                      "sms_services": dict(Counter(a["sms_service"] for a in accounts if a["sms_service"]))},
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
        return jsonify(python=sys.version.split()[0], dependencies=dependencies,
                       appium_available=appium_available,
                       voice_running=any(t["action"] == "voice" for t in tasks.store.active()),
                       notes=[
                           "Dependency discovery does not verify browser binaries or external services.",
                           "Appium port availability does not verify a connected Android device.",
                           "Appium's existing flow is incomplete and does not persist a verified account.",
                           "Proxy/behavior options reflect existing engine capabilities; not all engines use every option.",
                           "Use HTTPS for remote access; configure WEB_COOKIE_SECURE=true behind TLS.",
                       ])

    @app.get("/api/accounts")
    def accounts():
        rows = []
        for account in db.get_all_accounts():
            row = {key: value for key, value in account.items() if key not in ("password", "proxy", "phone_number")}
            row["has_password"] = bool(account["password"])
            rows.append(row)
        return jsonify(accounts=rows)

    @app.post("/api/accounts/<int:account_id>/password")
    def account_password(account_id):
        for account in db.get_all_accounts():
            if account["id"] == account_id:
                return jsonify(password=account["password"])
        raise KeyError(account_id)

    @app.post("/api/accounts/export")
    def export():
        kind = json_body().get("format")
        accounts = db.get_all_accounts()
        if kind == "json":
            content, mimetype = json.dumps(accounts, ensure_ascii=False, indent=2), "application/json"
        elif kind == "txt":
            content = "".join(f"{a['email']}:{a['password']}\n" for a in accounts)
            mimetype = "text/plain"
        elif kind == "csv":
            buffer = io.StringIO()
            writer = csv.writer(buffer)
            columns = ["email", "password", "first_name", "last_name", "proxy", "strategy",
                       "sms_service", "status", "created_at"]
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
                configuration.save_resource(kind, json_body().get("content"))
            return jsonify(configuration.read_resource(kind))

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
