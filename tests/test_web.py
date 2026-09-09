"""Offline regression tests for the web API, configuration, and task boundary."""
import ast
import csv
import io
import json
import os
import shutil
import stat
import subprocess
import sys
import tempfile
import unittest
from html.parser import HTMLParser
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from flask import template_rendered

from web.app import create_app
from web.configuration import Configuration
from web.tasks import normalize_params


ROOT = Path(__file__).resolve().parents[1]
ADMIN_PASSWORD = "test-only-long-password"
SESSION_KEY = "test-only-explicit-session-signing-key"


class Document(HTMLParser):
    def __init__(self, html):
        super().__init__()
        self.elements = []
        self.feed(html)

    def handle_starttag(self, tag, attrs):
        self.elements.append((tag, dict(attrs)))

    def find(self, tag, **attrs):
        return [
            element
            for name, element in self.elements
            if name == tag and all(element.get(key) == value for key, value in attrs.items())
        ]


class ProjectFixture(unittest.TestCase):
    def setUp(self):
        # Keep even TemporaryDirectory's files inside this checkout, not /tmp.
        directory = tempfile.TemporaryDirectory(prefix=".test-web-", dir=ROOT)
        self.addCleanup(directory.cleanup)
        self.workspace = Path(directory.name)
        scratch = patch("tempfile.tempdir", str(self.workspace))
        scratch.start()
        self.addCleanup(scratch.stop)
        self.root = self.workspace / "project"
        (self.root / "config").mkdir(parents=True)
        shutil.copyfile(ROOT / "config/settings.py", self.root / "config/settings.py")


class WebFixture(ProjectFixture):
    def setUp(self):
        super().setUp()
        self.app = create_app(
            root=self.root,
            password=ADMIN_PASSWORD,
            secret_key=SESSION_KEY,
            environment={},
        )
        self.app.config["TESTING"] = True
        self.client = self.app.test_client()
        self.configuration = self.app.extensions["web_configuration"]
        self.tasks = self.app.extensions["web_tasks"]
        self.db = self.app.extensions["web_database"]
        self.launcher = patch(
            "web.tasks.subprocess.Popen",
            side_effect=AssertionError("Tests must not launch automation workers"),
        )
        self.popen = self.launcher.start()
        self.addCleanup(self.launcher.stop)

    def csrf(self, client=None):
        with (client or self.client).session_transaction() as current:
            return current["csrf"]

    def login(self, client=None):
        client = client or self.client
        self.assertEqual(client.get("/login").status_code, 200)
        response = client.post(
            "/login", data={"password": ADMIN_PASSWORD, "csrf_token": self.csrf(client)}
        )
        self.assertEqual(response.status_code, 302)
        self.assertEqual(response.headers["Location"], "/")
        return self.csrf(client)

    def request_json(self, method, path, payload=None):
        kwargs = {"headers": {"X-CSRF-Token": self.csrf()}}
        if payload is not None:
            kwargs["json"] = payload
        return self.client.open(path, method=method, **kwargs)

    def account(self, email="offline@example.test", password="Account-only-secret-937!", **values):
        self.assertTrue(self.db.save_account(email, password, **values))
        return next(account for account in self.db.get_all_accounts() if account["email"] == email)

    def fake_workers(self):
        self.popen.side_effect = None
        self.popen.return_value = Mock(pid=987654321)
        # Exercise real task validation, persistence and launch arguments, but
        # never run supervisors against a fabricated operating-system PID.
        supervisor = patch("web.tasks.threading.Thread")
        threads = supervisor.start()
        self.addCleanup(supervisor.stop)
        return threads


class AuthenticationTests(WebFixture):
    def test_admin_password_requires_at_least_sixteen_characters(self):
        for password in ("", "short", "x" * 15):
            with self.subTest(length=len(password)), self.assertRaises(ValueError):
                create_app(
                    root=self.root, password=password, secret_key=SESSION_KEY, environment={}
                )

    def test_unauthenticated_html_redirects_and_every_api_family_rejects(self):
        response = self.client.get("/")
        self.assertEqual(response.status_code, 302)
        self.assertEqual(response.headers["Location"], "/login")
        for method, path in (
            ("GET", "/api/overview"),
            ("GET", "/api/system"),
            ("GET", "/api/accounts"),
            ("POST", "/api/accounts/1/password"),
            ("POST", "/api/accounts/export"),
            ("GET", "/api/settings"),
            ("PUT", "/api/settings"),
            ("POST", "/api/settings/validate"),
            ("GET", "/api/resources/proxies"),
            ("PUT", "/api/resources/proxies"),
            ("GET", "/api/session"),
            ("DELETE", "/api/session"),
            ("GET", "/api/tasks"),
            ("POST", "/api/tasks"),
            ("GET", "/api/tasks/missing"),
            ("POST", "/api/tasks/missing/cancel"),
        ):
            with self.subTest(method=method, path=path):
                response = self.client.open(path, method=method)
                self.assertEqual(response.status_code, 401)
                self.assertIn("error", response.get_json())
        self.popen.assert_not_called()

    def test_login_requires_csrf_before_checking_password(self):
        self.client.get("/login")
        for token in (None, "", "not-the-session-token"):
            with self.subTest(token=token):
                form = {"password": ADMIN_PASSWORD}
                if token is not None:
                    form["csrf_token"] = token
                self.assertEqual(self.client.post("/login", data=form).status_code, 403)
        with self.client.session_transaction() as current:
            self.assertFalse(current.get("authenticated"))

    def test_invalid_password_never_authenticates_or_echoes_password(self):
        self.client.get("/login")
        wrong = "wrong-password-unique-7481"
        response = self.client.post(
            "/login", data={"password": wrong, "csrf_token": self.csrf()}
        )
        self.assertEqual(response.status_code, 401)
        self.assertNotIn(wrong, response.get_data(as_text=True))
        self.assertEqual(self.client.get("/api/accounts").status_code, 401)

    def test_successful_login_rotates_session_and_csrf(self):
        self.client.get("/login")
        old_token = self.csrf()
        with self.client.session_transaction() as current:
            current["pre_login_marker"] = "must be discarded"
        response = self.client.post(
            "/login", data={"password": ADMIN_PASSWORD, "csrf_token": old_token}
        )
        self.assertEqual(response.status_code, 302)
        with self.client.session_transaction() as current:
            self.assertTrue(current["authenticated"])
            self.assertTrue(current.permanent)
            self.assertNotIn("pre_login_marker", current)
            self.assertNotEqual(current["csrf"], old_token)
        cookie = response.headers["Set-Cookie"]
        self.assertIn("HttpOnly", cookie)
        self.assertIn("SameSite=Strict", cookie)
        self.assertNotIn(ADMIN_PASSWORD, cookie)
        self.assertEqual(self.client.get("/api/accounts").status_code, 200)
        response = self.client.put(
            "/api/settings",
            json={"values": {"HEADLESS_MODE": True}},
            headers={"X-CSRF-Token": old_token},
        )
        self.assertEqual(response.status_code, 403)
        self.assertFalse(self.configuration.path.exists())

    def test_all_authenticated_mutations_require_matching_csrf(self):
        self.login()
        for method, path, data in (
            ("PUT", "/api/settings", {"values": {"HEADLESS_MODE": True}}),
            ("PUT", "/api/resources/names", {"content": "Offline Person"}),
            ("POST", "/api/accounts/1/password", {}),
            ("POST", "/api/accounts/export", {"format": "json"}),
            ("POST", "/api/tasks", {"action": "validate"}),
            ("POST", "/api/settings/validate", {}),
            ("POST", "/api/tasks/missing/cancel", {}),
            ("DELETE", "/api/session", {}),
            ("POST", "/logout", {}),
        ):
            for token in (None, "wrong-token", "非ASCII"):
                with self.subTest(method=method, path=path, token=token):
                    headers = {} if token is None else {"X-CSRF-Token": token}
                    response = self.client.open(path, method=method, json=data, headers=headers)
                    self.assertEqual(response.status_code, 403)
        self.assertFalse(self.configuration.path.exists())
        self.assertEqual(self.tasks.store.list(), [])
        self.popen.assert_not_called()

    def test_csrf_from_another_browser_session_is_rejected(self):
        first = self.login()
        other = self.app.test_client()
        self.login(other)
        self.assertNotEqual(first, self.csrf(other))
        response = other.post("/logout", headers={"X-CSRF-Token": first})
        self.assertEqual(response.status_code, 403)
        self.assertEqual(other.get("/api/settings").status_code, 200)

    def test_logout_clears_authentication_and_old_csrf_cannot_be_reused(self):
        token = self.login()
        self.assertEqual(self.client.get("/logout").status_code, 405)
        response = self.request_json("POST", "/logout")
        self.assertEqual(response.status_code, 302)
        self.assertEqual(response.headers["Location"], "/login")
        with self.client.session_transaction() as current:
            self.assertNotIn("authenticated", current)
            self.assertNotIn("csrf", current)
        self.assertEqual(self.client.get("/api/settings").status_code, 401)
        replay = self.client.post("/logout", headers={"X-CSRF-Token": token})
        self.assertEqual(replay.status_code, 302)
        self.assertEqual(replay.headers["Location"], "/login")
        self.assertEqual(
            self.client.post(
                "/api/accounts/export",
                json={"format": "json"},
                headers={"X-CSRF-Token": token},
            ).status_code,
            401,
        )

    def test_login_rate_limit_blocks_valid_password_until_window_expires(self):
        self.client.get("/login")
        with patch("web.app.time.monotonic", return_value=1000):
            for _ in range(5):
                response = self.client.post(
                    "/login", data={"password": "incorrect", "csrf_token": self.csrf()}
                )
                self.assertEqual(response.status_code, 401)
            response = self.client.post(
                "/login", data={"password": ADMIN_PASSWORD, "csrf_token": self.csrf()}
            )
            self.assertEqual(response.status_code, 429)
        with patch("web.app.time.monotonic", return_value=1901):
            response = self.client.post(
                "/login", data={"password": ADMIN_PASSWORD, "csrf_token": self.csrf()}
            )
            self.assertEqual(response.status_code, 302)

    def test_responses_have_security_headers_including_errors(self):
        for response in (self.client.get("/login"), self.client.get("/api/accounts")):
            with self.subTest(status=response.status_code):
                self.assertEqual(response.headers["Cache-Control"], "no-store")
                self.assertEqual(response.headers["X-Content-Type-Options"], "nosniff")
                self.assertEqual(response.headers["X-Frame-Options"], "DENY")
                self.assertEqual(response.headers["Referrer-Policy"], "no-referrer")
                self.assertIn("frame-ancestors 'none'", response.headers["Content-Security-Policy"])
                self.assertNotIn("'unsafe-inline'", response.headers["Content-Security-Policy"])


class AccountTests(WebFixture):
    def setUp(self):
        super().setUp()
        self.login()

    def test_account_list_masks_secrets_but_explicit_reveal_returns_password(self):
        saved = self.account(proxy="proxy.test:8000:user:proxy-secret", phone_number="+15550000101")
        response = self.client.get("/api/accounts")
        self.assertEqual(response.status_code, 200)
        row = response.get_json()["accounts"][0]
        self.assertEqual(row["email"], saved["email"])
        self.assertTrue(row["has_password"])
        for key in ("password", "proxy", "phone_number"):
            self.assertNotIn(key, row)
            self.assertNotIn(saved[key], response.get_data(as_text=True))
        response = self.request_json("POST", f"/api/accounts/{saved['id']}/password")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.get_json(), {"password": saved["password"]})
        self.assertEqual(response.headers["Cache-Control"], "no-store")

    def test_empty_password_and_nonexistent_reveal_are_distinguishable(self):
        saved = self.account(password="")
        self.assertFalse(self.client.get("/api/accounts").get_json()["accounts"][0]["has_password"])
        response = self.request_json("POST", f"/api/accounts/{saved['id']}/password")
        self.assertEqual(response.get_json(), {"password": ""})
        self.assertEqual(self.request_json("POST", "/api/accounts/99999/password").status_code, 404)

    def test_json_and_text_exports_are_explicit_secret_bearing_downloads(self):
        saved = self.account(first_name="测试")
        for kind in ("json", "txt"):
            with self.subTest(kind=kind):
                response = self.request_json("POST", "/api/accounts/export", {"format": kind})
                self.assertEqual(response.status_code, 200)
                self.assertEqual(
                    response.headers["Content-Disposition"], f'attachment; filename="accounts.{kind}"'
                )
                self.assertIn(saved["password"], response.get_data(as_text=True))
                if kind == "json":
                    self.assertEqual(response.get_json(), self.db.get_all_accounts())
                else:
                    self.assertEqual(
                        response.get_data(as_text=True), f"{saved['email']}:{saved['password']}\n"
                    )

    def test_csv_export_neutralizes_all_formula_prefixes_and_preserves_normal_text(self):
        values = {
            "email": "=HYPERLINK(\"example.test\")",
            "password": "+SUM(1,2)",
            "first_name": "-1+2",
            "last_name": "@SUM(1,2)",
            "proxy": "\t=1+2",
            "strategy": "\r=1+2",
            "sms_service": "=1+2",
            "status": "+1",
        }
        dangerous = self.account(**values)
        normal = self.account(
            email="normal@example.test",
            password="safe-password",
            first_name='名字, "quoted"\nsecond line',
        )
        response = self.request_json("POST", "/api/accounts/export", {"format": "csv"})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.mimetype, "text/csv")
        self.assertEqual(response.headers["Content-Disposition"], 'attachment; filename="accounts.csv"')
        content = response.get_data(as_text=True)
        self.assertTrue(content.startswith("\ufeff"))
        rows = list(csv.DictReader(io.StringIO(content.lstrip("\ufeff"), newline="")))
        self.assertEqual(len(rows), 2)
        unsafe = next(row for row in rows if row["email"] == "'" + dangerous["email"])
        for key, value in values.items():
            with self.subTest(column=key):
                self.assertEqual(unsafe[key], "'" + value)
        safe = next(row for row in rows if row["email"] == normal["email"])
        self.assertEqual(safe["first_name"], normal["first_name"])
        self.assertEqual(safe["password"], normal["password"])
        self.assertEqual(safe["created_at"], normal["created_at"])

    def test_export_rejects_unknown_formats_and_non_object_json(self):
        for payload in ({"format": "html"}, {"format": "../csv"}, {}, [], "json"):
            with self.subTest(payload=payload):
                response = self.request_json("POST", "/api/accounts/export", payload)
                self.assertEqual(response.status_code, 400)
                self.assertNotIn("Content-Disposition", response.headers)

    def test_overview_aggregates_database_records_and_reports_only_service_readiness(self):
        self.account(strategy="standard", sms_service="offline")
        self.account(email="other@example.test", status="disabled", strategy="standard")
        self.db.save_session_stats(2, 1, 1, {"standard": 2}, {"timeout": 1}, 12)
        self.configuration.save({"FIVESIM_API_KEY": "Configured-service-secret-481"})
        response = self.client.get("/api/overview")
        self.assertEqual(response.status_code, 200)
        data = response.get_json()
        self.assertEqual(data["accounts"], {
            "total": 2, "active": 1, "success_rate": 50.0,
            "strategies": {"standard": 2}, "sms_services": {"offline": 1},
        })
        self.assertEqual(data["sessions"][0]["strategies_used"], {"standard": 2})
        self.assertEqual(data["sessions"][0]["errors"], {"timeout": 1})
        self.assertTrue(data["services"]["FIVESIM_API_KEY"])
        self.assertFalse(data["services"]["TELEGRAM_BOT_TOKEN"])
        self.assertNotIn("Configured-service-secret-481", response.get_data(as_text=True))


class ConfigurationTests(ProjectFixture):
    def setUp(self):
        super().setUp()
        self.configuration = Configuration(self.root, environment={})

    def test_schema_covers_every_config_environment_field_and_its_runtime_type(self):
        source = (self.root / "config/settings.py").read_text(encoding="utf-8")
        tree = ast.parse(source)
        declaration = next(
            node for node in tree.body if isinstance(node, ast.ClassDef) and node.name == "Config"
        )
        namespace = {"os": SimpleNamespace(getenv=lambda name, default=None: default)}
        # Evaluate only Config's declaration with defaults: do not import
        # settings.py, load the checkout's .env, or run Config.validate().
        exec(compile(ast.Module(body=[declaration], type_ignores=[]), "<Config defaults>", "exec"), namespace)
        defaults = {
            key: value for key, value in vars(namespace["Config"]).items() if key.isupper()
        }
        fields = {field["key"]: field for field in self.configuration.fields()}
        self.assertEqual(set(fields), set(defaults))
        self.assertGreater(len(fields), 60)
        for key, default in defaults.items():
            with self.subTest(key=key):
                expected_type = {bool: "bool", int: "int", list: "list", str: "str"}[type(default)]
                self.assertEqual(fields[key]["type"], expected_type)
                self.assertTrue(fields[key]["group"])
                self.assertFalse(fields[key]["readonly"])
                if isinstance(default, list):
                    self.assertEqual(fields[key]["value"].split(","), default)
                else:
                    self.assertEqual(fields[key]["value"], default)

    def test_sensitive_fields_are_marked_secret_and_never_echo_values(self):
        secrets = {
            "YOUR_PASSWORD", "FIVESIM_API_KEY", "SMS_ACTIVATE_API_KEY",
            "ONLINESIM_API_KEY", "GETSMS_API_KEY", "TWOCAPTCHA_API_KEY",
            "ANTICAPTCHA_API_KEY", "CAPMONSTER_API_KEY", "KOOIP_USER_ID",
            "KOOIP_AUTH_NAME", "KOOIP_AUTH_PASSWORD", "TELEGRAM_BOT_TOKEN",
            "VOICE_SERVER_TOKEN",
        }
        self.configuration.save({key: "secret-" + key for key in secrets})
        fields = {field["key"]: field for field in self.configuration.fields()}
        for key in secrets:
            with self.subTest(key=key):
                self.assertTrue(fields[key]["secret"])
                self.assertTrue(fields[key]["configured"])
                self.assertEqual(fields[key]["value"], "")
                self.assertNotIn("secret-" + key, json.dumps(fields))

    def test_save_round_trips_types_preserves_unrelated_lines_and_sets_private_permissions(self):
        self.configuration.path.write_text("# keep this comment\nCUSTOM_VALUE='untouched'\n", encoding="utf-8")
        self.configuration.save({
            "HEADLESS_MODE": True,
            "BROWSER_TIMEOUT": "45",
            "ENGINE_MODE": "selenium",
            "PROXY_COUNTRY_ROTATION": "US,GB",
            "RECOVERY_EMAIL": "offline@example.test",
        })
        reloaded = Configuration(self.root, environment={})
        fields = {field["key"]: field for field in reloaded.fields()}
        self.assertIs(fields["HEADLESS_MODE"]["value"], True)
        self.assertEqual(fields["BROWSER_TIMEOUT"]["value"], 45)
        self.assertEqual(fields["ENGINE_MODE"]["value"], "selenium")
        self.assertEqual(fields["PROXY_COUNTRY_ROTATION"]["value"], "US,GB")
        self.assertEqual(fields["RECOVERY_EMAIL"]["value"], "offline@example.test")
        text = self.configuration.path.read_text(encoding="utf-8")
        self.assertIn("# keep this comment", text)
        self.assertIn("CUSTOM_VALUE='untouched'", text)
        self.assertEqual(stat.S_IMODE(self.configuration.path.stat().st_mode), 0o600)
        self.assertEqual(list(self.root.glob(".env-web-*")), [])

    def test_omitted_secret_is_kept_and_explicit_empty_secret_is_cleared(self):
        secret = "literal'quote-${NOT_INTERPOLATED}-password"
        self.configuration.save({"YOUR_PASSWORD": secret})
        self.configuration.save({"BROWSER_TIMEOUT": 19})
        self.assertEqual(Configuration(self.root, {}).values()["YOUR_PASSWORD"], secret)
        self.configuration.save({"YOUR_PASSWORD": ""})
        reloaded = Configuration(self.root, {})
        self.assertEqual(reloaded.values()["YOUR_PASSWORD"], "")
        field = next(field for field in reloaded.fields() if field["key"] == "YOUR_PASSWORD")
        self.assertFalse(field["configured"])

    def test_environment_overrides_are_readonly_masked_and_never_written(self):
        self.configuration.save({"YOUR_PASSWORD": "local-secret", "ENGINE_MODE": "playwright"})
        before = self.configuration.path.read_bytes()
        overridden = Configuration(self.root, {
            "YOUR_PASSWORD": "environment-secret", "ENGINE_MODE": "selenium",
        })
        fields = {field["key"]: field for field in overridden.fields()}
        self.assertEqual(overridden.values()["YOUR_PASSWORD"], "environment-secret")
        self.assertEqual(fields["YOUR_PASSWORD"]["value"], "")
        self.assertTrue(fields["YOUR_PASSWORD"]["readonly"])
        self.assertTrue(fields["YOUR_PASSWORD"]["configured"])
        self.assertEqual(fields["ENGINE_MODE"]["value"], "selenium")
        for key, value in (("YOUR_PASSWORD", ""), ("ENGINE_MODE", "appium")):
            with self.subTest(key=key), self.assertRaisesRegex(ValueError, "server environment"):
                overridden.save({key: value})
        self.assertEqual(self.configuration.path.read_bytes(), before)
        overridden.save({"BROWSER_TIMEOUT": 42})
        self.assertNotIn("environment-secret", self.configuration.path.read_text())
        self.assertEqual(Configuration(self.root, {}).values()["YOUR_PASSWORD"], "local-secret")

    def test_explicit_empty_environment_does_not_inherit_process_overrides(self):
        with patch.dict(os.environ, {"ENGINE_MODE": "appium", "YOUR_PASSWORD": "process-secret"}):
            isolated = Configuration(self.root, environment={})
            self.assertEqual(isolated.values()["ENGINE_MODE"], "playwright")
            self.assertEqual(isolated.values()["YOUR_PASSWORD"], "")

    def test_invalid_types_ranges_and_text_are_rejected_without_partial_writes(self):
        self.configuration.save({"BROWSER_TIMEOUT": 30})
        before = self.configuration.path.read_bytes()
        cases = [
            ("HEADLESS_MODE", value) for value in ("true", "false", 0, 1, None, [])
        ] + [
            ("BROWSER_TIMEOUT", value)
            for value in (True, False, 1.5, None, [], {}, "1.5", "bad", "", -1, 86400001)
        ] + [
            ("RECOVERY_EMAIL", value)
            for value in (None, True, 123, [], {}, "bad\ntext", "bad\rtext", "bad\x00text", "x" * 4097)
        ] + [
            ("PROXY_COUNTRY_ROTATION", ["US", "GB"]),
            ("NOT_A_CONFIG_FIELD", "value"),
        ]
        for key, value in cases:
            with self.subTest(key=key, value=repr(value)[:60]), self.assertRaises(ValueError):
                self.configuration.save({"YOUR_BIRTHDAY": "1 1 1990", key: value})
            self.assertEqual(self.configuration.path.read_bytes(), before)
        for value in (None, [], "text", 1):
            with self.subTest(values=value), self.assertRaises(ValueError):
                self.configuration.save(value)
        self.assertEqual(list(self.root.glob(".env-web-*")), [])

    def test_every_declared_enum_rejects_unknown_options(self):
        choices = {
            "ENGINE_MODE": ("playwright", "selenium", "appium"),
            "YOUR_GENDER": ("1", "2", "3"),
            "PROXY_TYPE": ("residential", "mobile", "datacenter"),
            "PROXY_POOL_PREFERENCE": ("auto", "static", "kooip"),
            "WARMING_INTENSITY": ("low", "medium", "high"),
            "LOG_LEVEL": ("DEBUG", "INFO", "WARNING", "ERROR"),
            "EXPORT_FORMAT": ("txt", "csv", "json"),
        }
        for key, options in choices.items():
            with self.subTest(key=key):
                self.assertEqual(self.configuration.schema[key]["choices"], list(options))
                for value in options:
                    self.configuration.save({key: value})
                    self.assertEqual(self.configuration.values()[key], value)
                with self.assertRaises(ValueError):
                    self.configuration.save({key: "invalid-option"})

    def test_integer_boundaries_are_accepted(self):
        for value in (0, 86400000):
            with self.subTest(value=value):
                self.configuration.save({"BROWSER_TIMEOUT": value})
                self.assertEqual(self.configuration.values()["BROWSER_TIMEOUT"], str(value))

    def test_resources_round_trip_and_missing_files_are_empty(self):
        for kind, path, content in (
            ("proxies", "config/proxies.txt", "# offline\nproxy.test:1\nproxy.test:65535:user:pass\n"),
            ("names", "data/names.txt", "Offline Person\n测试 用户\n"),
            ("user_agents", "config/user_agents.txt", "OfflineBrowser/1.0\n"),
        ):
            with self.subTest(kind=kind):
                self.assertEqual(self.configuration.read_resource(kind), {"path": path, "content": ""})
                self.configuration.save_resource(kind, content)
                self.assertEqual(
                    self.configuration.read_resource(kind), {"path": path, "content": content}
                )
                self.assertEqual((self.root / path).read_text(), content)
                self.assertEqual(list((self.root / path).parent.glob(".web-resource-*")), [])

    def test_resource_paths_reject_traversal_secrets_and_non_text_targets(self):
        outside = self.workspace / "outside.txt"
        outside.write_text("must remain unchanged", encoding="utf-8")
        cases = (
            "../outside.txt", str(outside), ".env", "web/notes.txt",
            "config/password.txt", "config/5sim_config.txt", "data/state.json",
            "config/../notes.txt", "config/nested/../../../outside.txt",
        )
        for key in ("PROXY_FILE", "NAMES_FILE", "USER_AGENTS_FILE"):
            for value in cases:
                with self.subTest(key=key, path=value), self.assertRaises(ValueError):
                    self.configuration.save({key: value})
        self.assertEqual(outside.read_text(), "must remain unchanged")
        self.assertFalse(self.configuration.path.exists())

    def test_resource_symlinks_cannot_escape_allowed_directories(self):
        target = self.workspace / "outside.txt"
        target.write_text("outside content", encoding="utf-8")
        (self.root / "config/linked.txt").symlink_to(target)
        with self.assertRaises(ValueError):
            self.configuration.save({"PROXY_FILE": "config/linked.txt"})
        self.assertEqual(target.read_text(), "outside content")

    def test_invalid_environment_resource_path_is_validated_on_read_and_write(self):
        configuration = Configuration(self.root, {"NAMES_FILE": "../outside.txt"})
        with self.assertRaises(ValueError):
            configuration.read_resource("names")
        with self.assertRaises(ValueError):
            configuration.save_resource("names", "new content")
        self.assertFalse((self.workspace / "outside.txt").exists())

    def test_invalid_resource_content_does_not_overwrite_existing_file(self):
        original = "proxy.test:8000\n"
        self.configuration.save_resource("proxies", original)
        for content in (
            "proxy.test:0", "proxy.test:65536", "proxy.test:not-port", ":8000",
            "proxy.test:8000:user", "https://proxy.test:8000", "proxy.test",
            None, [], "null\x00byte", "界" * (1024 * 1024 // 3 + 1),
        ):
            with self.subTest(content=repr(content)[:60]), self.assertRaises(ValueError):
                self.configuration.save_resource("proxies", content)
            self.assertEqual(self.configuration.read_resource("proxies")["content"], original)
        with self.assertRaises(ValueError):
            self.configuration.read_resource("unknown")
        with self.assertRaises(ValueError):
            self.configuration.save_resource("unknown", "data")


class TaskValidationTests(unittest.TestCase):
    def test_create_defaults_and_explicit_engine_override(self):
        self.assertEqual(normalize_params("create", {}, "selenium"), {
            "engine": "selenium", "num_accounts": 1, "warmup_minutes": 5,
            "flow_mode": "standard", "use_sms_api": False, "parallel": False, "max_threads": 3,
        })
        params = normalize_params("create", {
            "engine": "playwright", "flow_mode": "workspace", "use_sms_api": True,
            "parallel": True, "max_threads": 5, "num_accounts": 100, "warmup_minutes": 0,
        }, "appium")
        self.assertEqual(params["engine"], "playwright")
        self.assertTrue(params["parallel"])
        self.assertEqual(params["num_accounts"], 100)

    def test_integer_parameters_reject_coercion_and_enforce_boundaries(self):
        for action, name, low, high in (
            ("create", "num_accounts", 1, 100),
            ("create", "warmup_minutes", 0, 60),
            ("create", "max_threads", 1, 5),
            ("warm", "duration_minutes", 1, 60),
        ):
            for invalid in (low - 1, high + 1, str(low), True, False, None, [], 1.5):
                with self.subTest(action=action, name=name, value=invalid), self.assertRaises(ValueError):
                    normalize_params(action, {name: invalid}, "playwright")
            for valid in (low, high):
                with self.subTest(action=action, name=name, boundary=valid):
                    self.assertEqual(normalize_params(action, {name: valid}, "playwright")[name], valid)

    def test_invalid_action_params_unknown_keys_and_options_are_rejected(self):
        for action in (None, "", "../worker", "shell", [], {}):
            with self.subTest(action=action), self.assertRaises(ValueError):
                normalize_params(action, {}, "playwright")
        for params in (None, [], "{}", 1):
            with self.subTest(params=params), self.assertRaises(ValueError):
                normalize_params("create", params, "playwright")
        for action in ("create", "health", "warm", "validate", "voice"):
            with self.subTest(action=action), self.assertRaises(ValueError):
                normalize_params(action, {"command": "must never execute"}, "playwright")
        for params in (
            {"engine": "unknown"}, {"flow_mode": "unknown"}, {"parallel": 1},
            {"use_sms_api": "false"}, {"engine": "appium", "parallel": True},
        ):
            with self.subTest(params=params), self.assertRaises(ValueError):
                normalize_params("create", params, "playwright")

    def test_account_selection_is_deduplicated_without_mutating_input(self):
        ids = [3, 1, 3, 2]
        for action in ("health", "warm"):
            with self.subTest(action=action):
                result = normalize_params(action, {"account_ids": ids}, "appium")
                self.assertEqual(result["account_ids"], [3, 1, 2])
                self.assertEqual(ids, [3, 1, 3, 2])
                if action == "warm":
                    self.assertEqual(result["engine"], "playwright")
                    self.assertEqual(result["duration_minutes"], 3)
        for ids in ("1,2", None, {}, [0], [-1], [True], ["1"], [1.0], list(range(1, 10002))):
            with self.subTest(ids=repr(ids)[:50]), self.assertRaises(ValueError):
                normalize_params("health", {"account_ids": ids}, "playwright")
        with self.assertRaises(ValueError):
            normalize_params("warm", {"engine": "appium"}, "playwright")

    def test_parameterless_actions_accept_only_an_empty_object(self):
        for action in (
            "proxy_test", "proxy_fetch", "telegram_test", "sms_balance",
            "validate", "migrate", "resume", "voice",
        ):
            with self.subTest(action=action):
                self.assertEqual(normalize_params(action, {}, "playwright"), {})
                with self.assertRaises(ValueError):
                    normalize_params(action, {"unexpected": True}, "playwright")


class TaskApiTests(WebFixture):
    def setUp(self):
        super().setUp()
        self.login()

    def test_start_persists_normalized_task_and_spawns_only_the_isolated_worker(self):
        self.fake_workers()
        self.configuration.environment.update({
            "WEB_ADMIN_PASSWORD": "admin-must-not-reach-worker",
            "WEB_SECRET_KEY": "signing-key-must-not-reach-worker",
        })
        self.configuration.save({"ENGINE_MODE": "appium", "FIVESIM_API_KEY": "worker-service-secret"})
        response = self.request_json("POST", "/api/tasks", {
            "action": "create", "params": {"engine": "selenium", "num_accounts": 2},
        })
        self.assertEqual(response.status_code, 202)
        task = response.get_json()["task"]
        self.assertEqual(task["status"], "running")
        self.assertEqual(task["params"]["engine"], "selenium")
        self.assertEqual(task["params"]["num_accounts"], 2)
        self.assertEqual(task["params"]["warmup_minutes"], 5)
        self.assertEqual(self.tasks.store.get(task["id"])["pid"], 987654321)
        args, kwargs = self.popen.call_args
        self.assertEqual(args[0], [sys.executable, "-m", "web.worker", task["id"]])
        self.assertEqual(kwargs["cwd"], self.root)
        self.assertEqual(kwargs["stdin"], subprocess.DEVNULL)
        self.assertEqual(kwargs["stderr"], subprocess.STDOUT)
        self.assertEqual(kwargs["start_new_session"], os.name != "nt")
        environment = kwargs["env"]
        self.assertEqual(environment["ENGINE_MODE"], "selenium")
        self.assertEqual(environment["FIVESIM_API_KEY"], "worker-service-secret")
        self.assertEqual(environment["WEB_TASK_DIRECTORY"], str(self.root / "data/web"))
        self.assertEqual(environment["WEB_PARENT_PID"], str(os.getpid()))
        self.assertEqual(environment["PYTHONDONTWRITEBYTECODE"], "1")
        self.assertNotIn("WEB_ADMIN_PASSWORD", environment)
        self.assertNotIn("WEB_SECRET_KEY", environment)
        self.assertNotIn("worker-service-secret", response.get_data(as_text=True))

    def test_invalid_task_requests_never_create_a_record_or_process(self):
        for data in (
            {}, [], {"action": "unknown"}, {"action": "create", "params": []},
            {"action": "create", "params": {"num_accounts": True}},
            {"action": "health", "params": {"account_ids": ["1"]}},
        ):
            with self.subTest(data=data):
                response = self.request_json("POST", "/api/tasks", data)
                self.assertEqual(response.status_code, 400)
                self.assertIn("error", response.get_json())
                self.assertEqual(self.tasks.store.list(), [])
        self.popen.assert_not_called()

    def test_running_and_stopping_tasks_reject_concurrent_work(self):
        self.fake_workers()
        first = self.request_json("POST", "/api/tasks", {"action": "health"}).get_json()["task"]
        for status in ("running", "stopping"):
            self.tasks.store.update(first["id"], status=status)
            for path, body in (
                ("/api/tasks", {"action": "create"}),
                ("/api/tasks", {"action": "proxy_test"}),
                ("/api/settings/validate", {}),
            ):
                with self.subTest(status=status, path=path, body=body):
                    response = self.request_json("POST", path, body)
                    self.assertEqual(response.status_code, 400)
                    self.assertIn("already running", response.get_json()["error"])
        self.assertEqual(self.popen.call_count, 1)
        self.assertEqual(len(self.tasks.store.list()), 1)

    def test_old_active_work_stays_visible_and_blocks_launch_after_history_limit(self):
        active = self.tasks.store.create("health", {"account_ids": []})
        completed_ids = []
        for _ in range(101):
            completed = self.tasks.store.create("validate", {})
            self.tasks.store.update(completed["id"], status="succeeded")
            completed_ids.append(completed["id"])
        rows = self.client.get("/api/tasks").get_json()["tasks"]
        self.assertEqual(len(rows), 101)
        self.assertIn(active["id"], {row["id"] for row in rows})
        self.assertNotIn(completed_ids[0], {row["id"] for row in rows})
        self.assertEqual(
            [task["id"] for task in self.tasks.store.active()], [active["id"]]
        )
        response = self.request_json("POST", "/api/tasks", {"action": "validate"})
        self.assertEqual(response.status_code, 400)
        self.popen.assert_not_called()

    def test_voice_can_coexist_with_one_worker_but_not_another_voice(self):
        self.fake_workers()
        self.configuration.save({"VOICE_SERVER_TOKEN": "Voice-service-secret-839"})
        first = self.request_json("POST", "/api/tasks", {"action": "voice"})
        self.assertEqual(first.status_code, 202)
        self.assertEqual(self.popen.call_args.kwargs["env"]["VOICE_SERVER_HOST"], "127.0.0.1")
        other = self.request_json("POST", "/api/tasks", {"action": "validate"})
        self.assertEqual(other.status_code, 202)
        for action in ("voice", "health"):
            with self.subTest(action=action):
                self.assertEqual(
                    self.request_json("POST", "/api/tasks", {"action": action}).status_code, 400
                )
        self.assertEqual(self.popen.call_count, 2)

    def test_voice_requires_nondefault_token_without_launching(self):
        for value in ("", "changeme"):
            self.configuration.save({"VOICE_SERVER_TOKEN": value})
            response = self.request_json("POST", "/api/tasks", {"action": "voice"})
            self.assertEqual(response.status_code, 400)
            self.assertIn("VOICE_SERVER_TOKEN", response.get_json()["error"])
        self.assertEqual(self.tasks.store.active(), [])
        self.assertTrue(all(task["status"] == "failed" for task in self.tasks.store.list()))
        self.popen.assert_not_called()

    def test_completed_work_allows_next_task_and_validation_route_uses_real_manager(self):
        self.fake_workers()
        previous = self.tasks.store.create("health", {"account_ids": []})
        self.tasks.store.update(previous["id"], status="succeeded")
        response = self.request_json("POST", "/api/settings/validate")
        self.assertEqual(response.status_code, 202)
        self.assertEqual(response.get_json()["task"]["action"], "validate")
        self.popen.assert_called_once()

    def test_launch_failure_is_persisted_and_does_not_block_future_work(self):
        self.popen.side_effect = OSError("controlled worker startup failure")
        with self.assertRaisesRegex(OSError, "controlled worker startup failure"):
            self.tasks.start("validate", {})
        task = self.tasks.store.list()[0]
        self.assertEqual(task["status"], "failed")
        self.assertTrue(task["finished_at"])
        self.assertIn("controlled worker startup failure", task["error"])
        self.assertEqual(self.tasks.store.active(), [])
        self.fake_workers()
        self.assertEqual(self.tasks.start("health", {})["status"], "running")

    def test_active_work_blocks_configuration_resource_and_session_mutation(self):
        session_path = self.root / "data/session_state.json"
        original = '{"batch_config":{"num_accounts":2},"completed_indices":[0]}'
        session_path.write_text(original, encoding="utf-8")
        task = self.tasks.store.create("health", {"account_ids": []})
        for status in ("running", "stopping"):
            self.tasks.store.update(task["id"], status=status)
            for method, path, payload in (
                ("PUT", "/api/settings", {"values": {"HEADLESS_MODE": True}}),
                ("PUT", "/api/resources/names", {"content": "must not be written"}),
                ("DELETE", "/api/session", None),
            ):
                with self.subTest(status=status, path=path):
                    self.assertEqual(self.request_json(method, path, payload).status_code, 400)
            self.assertEqual(self.client.get("/api/settings").status_code, 200)
            self.assertEqual(self.client.get("/api/resources/names").status_code, 200)
            self.assertEqual(self.client.get("/api/session").status_code, 200)
        self.assertEqual(session_path.read_text(), original)
        self.assertFalse(self.configuration.path.exists())
        self.assertFalse((self.root / "data/names.txt").exists())

    def test_task_details_and_list_redact_config_account_proxy_and_named_secrets(self):
        saved = self.account()
        api_secret = "Configured-api-key-secret-248"
        proxy = "proxy.test:8080:proxy-user:proxy-secret-195"
        self.configuration.save({"FIVESIM_API_KEY": api_secret})
        self.configuration.save_resource("proxies", proxy + "\n")
        task = self.tasks.store.create("health", {"account_ids": [saved["id"]]})
        raw = f"\x1b[31m{api_secret} {saved['password']} {proxy} {ADMIN_PASSWORD}\x1b[0m"
        self.tasks.store.update(
            task["id"], status="succeeded",
            result={"password": "unknown-result-secret", "nested": [raw]},
            progress={"completed": 1, "total": 1, "message": raw},
        )
        (self.tasks.store.directory / (task["id"] + ".log")).write_text(
            raw + "\npassword=unconfigured-log-secret", encoding="utf-8"
        )
        for path in ("/api/tasks", f"/api/tasks/{task['id']}", "/api/overview"):
            with self.subTest(path=path):
                response = self.client.get(path)
                self.assertEqual(response.status_code, 200)
                text = response.get_data(as_text=True)
                for value in (
                    api_secret, saved["password"], proxy, ADMIN_PASSWORD,
                    "unknown-result-secret", "unconfigured-log-secret",
                ):
                    self.assertNotIn(value, text)
                self.assertNotIn("\\u001b", text)
                self.assertIn("[redacted]", text)
        persisted = self.tasks.store.get(task["id"])
        self.assertEqual(persisted["result"]["password"], "unknown-result-secret")
        self.assertIn(api_secret, self.tasks.store.logs(task["id"]))

    def test_missing_task_detail_and_cancel_return_not_found(self):
        self.assertEqual(self.client.get("/api/tasks/unknown").status_code, 404)
        self.assertEqual(self.request_json("POST", "/api/tasks/unknown/cancel").status_code, 404)
        task = self.tasks.store.create("validate", {})
        self.tasks.store.update(task["id"], status="succeeded")
        response = self.request_json("POST", f"/api/tasks/{task['id']}/cancel")
        self.assertEqual(response.status_code, 400)

    def test_cancel_marks_stopping_and_repeated_request_does_not_resignal(self):
        self.fake_workers()
        task = self.tasks.start("health", {})
        with patch.object(self.tasks, "_signal") as signal_worker:
            response = self.request_json("POST", f"/api/tasks/{task['id']}/cancel")
            self.assertEqual(response.status_code, 200)
            self.assertEqual(response.get_json()["task"]["status"], "stopping")
            repeated = self.request_json("POST", f"/api/tasks/{task['id']}/cancel")
            self.assertEqual(repeated.status_code, 200)
            self.assertEqual(repeated.get_json()["task"]["status"], "stopping")
            signal_worker.assert_called_once()


class SettingsAndSessionApiTests(WebFixture):
    def setUp(self):
        super().setUp()
        self.login()

    def test_settings_api_keeps_omitted_secret_and_clears_explicit_empty_value(self):
        response = self.request_json("PUT", "/api/settings", {
            "values": {"YOUR_PASSWORD": "private-api-settings-secret", "HEADLESS_MODE": True},
        })
        self.assertEqual(response.status_code, 200)
        self.assertNotIn("private-api-settings-secret", response.get_data(as_text=True))
        fields = {field["key"]: field for field in response.get_json()["fields"]}
        self.assertEqual(fields["YOUR_PASSWORD"]["value"], "")
        self.assertTrue(fields["YOUR_PASSWORD"]["configured"])
        self.assertIs(fields["HEADLESS_MODE"]["value"], True)
        response = self.request_json("PUT", "/api/settings", {"values": {"BROWSER_TIMEOUT": 55}})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(self.configuration.values()["YOUR_PASSWORD"], "private-api-settings-secret")
        response = self.request_json("PUT", "/api/settings", {"values": {"YOUR_PASSWORD": ""}})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(Configuration(self.root, {}).values()["YOUR_PASSWORD"], "")

    def test_settings_api_rejects_invalid_json_types_and_readonly_overrides(self):
        for payload in ([], {}, {"values": []}, {"values": {"HEADLESS_MODE": "true"}}):
            with self.subTest(payload=payload):
                self.assertEqual(self.request_json("PUT", "/api/settings", payload).status_code, 400)
        self.configuration.environment["YOUR_PASSWORD"] = "environment-only-api-secret"
        response = self.client.get("/api/settings")
        self.assertNotIn("environment-only-api-secret", response.get_data(as_text=True))
        field = next(field for field in response.get_json()["fields"] if field["key"] == "YOUR_PASSWORD")
        self.assertTrue(field["readonly"])
        response = self.request_json("PUT", "/api/settings", {"values": {"YOUR_PASSWORD": ""}})
        self.assertEqual(response.status_code, 400)
        self.assertFalse(self.configuration.path.exists())

    def test_resource_api_reads_writes_and_rejects_invalid_targets_or_content(self):
        response = self.request_json("PUT", "/api/resources/names", {"content": "Test Person\n"})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.get_json(), {"path": "data/names.txt", "content": "Test Person\n"})
        self.assertEqual(self.client.get("/api/resources/names").get_json(), response.get_json())
        for path, payload in (
            ("/api/resources/unknown", {"content": "content"}),
            ("/api/resources/names", {"content": 1}),
            ("/api/resources/proxies", {"content": "host:0"}),
        ):
            with self.subTest(path=path, payload=payload):
                self.assertEqual(self.request_json("PUT", path, payload).status_code, 400)
        response = self.request_json("PUT", "/api/settings", {"values": {"NAMES_FILE": "../outside.txt"}})
        self.assertEqual(response.status_code, 400)
        self.assertEqual((self.root / "data/names.txt").read_text(), "Test Person\n")

    def test_missing_session_and_repeated_deletion_are_idempotent(self):
        self.assertEqual(self.client.get("/api/session").get_json(), {"state": None, "remaining": 0})
        for _ in range(2):
            response = self.request_json("DELETE", "/api/session")
            self.assertEqual(response.status_code, 200)
            self.assertEqual(response.get_json(), {"state": None, "remaining": 0})

    def test_saved_session_reports_remaining_work_without_modifying_file_then_deletes(self):
        state = {
            "saved_at": "2026-01-01 00:00:00",
            "batch_config": {"num_accounts": 5, "engine": "playwright"},
            "completed_indices": [0, 2, 4],
            "results": {"successes": 2, "failures": 1},
        }
        path = self.root / "data/session_state.json"
        path.write_text(json.dumps(state), encoding="utf-8")
        before = path.read_bytes()
        response = self.client.get("/api/session")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.get_json(), {"state": state, "remaining": 2})
        self.assertEqual(path.read_bytes(), before)
        response = self.request_json("DELETE", "/api/session")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.get_json(), {"state": None, "remaining": 0})
        self.assertFalse(path.exists())

    def test_malformed_session_is_reported_and_can_be_explicitly_deleted(self):
        path = self.root / "data/session_state.json"
        path.write_text('{"batch_config": incomplete', encoding="utf-8")
        before = path.read_bytes()
        response = self.client.get("/api/session")
        self.assertEqual(response.status_code, 400)
        self.assertIn("error", response.get_json())
        self.assertEqual(path.read_bytes(), before)
        response = self.request_json("DELETE", "/api/session")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.get_json(), {"state": None, "remaining": 0})
        self.assertFalse(path.exists())

    def test_voice_alone_does_not_lock_configuration_or_session_editing(self):
        self.tasks.store.create("voice", {})
        self.assertEqual(
            self.request_json("PUT", "/api/settings", {"values": {"HEADLESS_MODE": True}}).status_code, 200
        )
        self.assertEqual(
            self.request_json("PUT", "/api/resources/names", {"content": "Test Person"}).status_code, 200
        )
        self.assertEqual(self.request_json("DELETE", "/api/session").status_code, 200)


class PersistedDataSafetyTests(WebFixture):
    def setUp(self):
        super().setUp()
        self.login()

    def test_overview_and_session_redact_secrets_in_values_and_error_keys(self):
        token = "historical-api-test-secret"
        self.configuration.save({"FIVESIM_API_KEY": token})
        self.db.save_session_stats(1, 0, 1, {"standard": 1},
                                   {f"request failed with {token}": 1}, 2)
        path = self.root / "data/session_state.json"
        path.write_text(json.dumps({
            "batch_config": {"num_accounts": 1}, "completed_indices": [],
            "results": {"successes": 0, "failures": 0, "password": token},
        }), encoding="utf-8")
        for url in ("/api/overview", "/api/session"):
            response = self.client.get(url)
            self.assertEqual(response.status_code, 200)
            self.assertNotIn(token, response.get_data(as_text=True))
            self.assertIn("[redacted]", response.get_data(as_text=True))

    def test_structurally_invalid_sessions_are_reported_and_remain_clearable(self):
        path = self.root / "data/session_state.json"
        states = [
            [], {}, {"batch_config": None}, {"batch_config": {"num_accounts": True}},
            {"batch_config": {"num_accounts": 3}, "completed_indices": None},
            {"batch_config": {"num_accounts": 3}, "completed_indices": [0, 0]},
            {"batch_config": {"num_accounts": 3}, "completed_indices": [5]},
            {"batch_config": {"num_accounts": 3}, "results": []},
        ]
        for state in states:
            with self.subTest(state=state):
                path.write_text(json.dumps(state), encoding="utf-8")
                response = self.client.get("/api/session")
                self.assertEqual(response.status_code, 400)
                self.assertIn("Invalid saved session", response.get_json()["error"])
                self.assertEqual(self.request_json("DELETE", "/api/session").status_code, 200)
                self.assertFalse(path.exists())

    def test_short_secret_does_not_corrupt_task_identifiers_or_control_fields(self):
        self.configuration.save({"YOUR_PASSWORD": "a"})
        task = self.tasks.store.create("validate", {})
        self.tasks.store.update(task["id"], status="completed", result={"password": "a"})
        detail = self.client.get(f"/api/tasks/{task['id']}").get_json()["task"]
        listing = self.client.get("/api/tasks").get_json()["tasks"][0]
        for item in (detail, listing):
            self.assertEqual(item["id"], task["id"])
            self.assertEqual(item["action"], "validate")
            self.assertEqual(item["status"], "completed")
            self.assertEqual(item["progress"]["total"], 1)
            self.assertNotEqual(item["result"].get("password"), "a")


class PageContractTests(WebFixture):
    def test_login_renders_real_template_with_password_and_csrf_controls(self):
        rendered = []

        def capture(sender, template, context, **extra):
            rendered.append((template.name, context))

        with template_rendered.connected_to(capture, self.app):
            response = self.client.get("/login")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.mimetype, "text/html")
        self.assertEqual(rendered[0][0], "login.html")
        document = Document(response.get_data(as_text=True))
        self.assertEqual(len(document.find("input", name="password", type="password")), 1)
        controls = document.find("input", name="csrf_token", type="hidden")
        self.assertEqual(len(controls), 1)
        self.assertEqual(controls[0]["value"], self.csrf())
        self.assertEqual(rendered[0][1]["csrf_token"], self.csrf())
        self.assertTrue(any(form.get("method", "").lower() == "post" for form in document.find("form")))
        self.assertNotIn(ADMIN_PASSWORD, response.get_data(as_text=True))

    def test_authenticated_index_renders_real_template_and_only_local_assets(self):
        self.login()
        rendered = []

        def capture(sender, template, context, **extra):
            rendered.append((template.name, context))

        with template_rendered.connected_to(capture, self.app):
            response = self.client.get("/")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.mimetype, "text/html")
        self.assertEqual(rendered[0][0], "index.html")
        self.assertEqual(rendered[0][1]["csrf_token"], self.csrf())
        html = response.get_data(as_text=True)
        document = Document(html)
        self.assertIn(self.csrf(), html)
        scripts = document.find("script")
        self.assertTrue(scripts)
        for script in scripts:
            self.assertTrue(script.get("src", "").startswith("/static/"), script)
        styles = document.find("link", rel="stylesheet")
        self.assertTrue(styles)
        for stylesheet in styles:
            self.assertTrue(stylesheet.get("href", "").startswith("/static/"), stylesheet)
        self.assertNotIn(ADMIN_PASSWORD, html)
        for asset in [script["src"] for script in scripts] + [style["href"] for style in styles]:
            with self.subTest(asset=asset):
                asset_response = self.client.get(asset)
                try:
                    self.assertEqual(asset_response.status_code, 200)
                finally:
                    asset_response.close()

    def test_system_route_discovers_dependencies_without_launching_or_network_access(self):
        self.login()
        with patch("web.app.importlib.util.find_spec", return_value=None) as discovery, patch(
            "web.app.socket.create_connection", side_effect=OSError("offline test")
        ) as connection:
            response = self.client.get("/api/system")
        self.assertEqual(response.status_code, 200)
        data = response.get_json()
        self.assertEqual(data["python"], sys.version.split()[0])
        self.assertFalse(data["appium_available"])
        self.assertFalse(data["voice_running"])
        self.assertIn("playwright", data["dependencies"])
        self.assertIn("selenium", data["dependencies"])
        self.assertTrue(all(value is False for value in data["dependencies"].values()))
        self.assertTrue(data["notes"])
        discovery.assert_any_call("playwright")
        connection.assert_called_once_with(("127.0.0.1", 4723), timeout=0.3)
        self.popen.assert_not_called()

    def test_proxy_management_has_own_menu_editor_and_configuration_form(self):
        self.login()
        html = self.client.get("/").get_data(as_text=True)
        document = Document(html)
        links = document.find("a", href="#proxies")
        self.assertTrue(any(link.get("data-page") == "proxies" for link in links))
        proxy_page = html[html.index('id="page-proxies"'):html.index('id="page-resources"')]
        resource_page = html[html.index('id="page-resources"'):html.index('id="page-settings"')]
        self.assertIn("KKOIP", proxy_page)
        self.assertNotIn("KooIP", proxy_page)
        self.assertIn('id="editor-proxies"', proxy_page)
        self.assertIn('id="proxy-import-file"', proxy_page)
        self.assertIn('id="proxy-settings-form"', proxy_page)
        self.assertIn('data-action="proxy_test"', proxy_page)
        self.assertIn('data-action="proxy_fetch"', proxy_page)
        self.assertNotIn('id="editor-proxies"', resource_page)
        self.assertIn('id="editor-names"', resource_page)
        self.assertIn('id="editor-user_agents"', resource_page)
        identifiers = [attrs["id"] for _, attrs in document.elements if "id" in attrs]
        self.assertEqual(len(identifiers), len(set(identifiers)))

    def test_authenticated_unknown_route_is_json_not_found(self):
        self.login()
        response = self.client.get("/api/not-a-real-route")
        self.assertEqual(response.status_code, 404)
        self.assertIn("error", response.get_json())


if __name__ == "__main__":
    unittest.main()
