import io
import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import unittest
from contextlib import redirect_stderr, redirect_stdout
from http.cookiejar import CookieJar
from pathlib import Path
from unittest.mock import Mock, patch
from urllib.error import URLError
from urllib.parse import urlencode
from urllib.request import HTTPCookieProcessor, Request, build_opener

from web.app import create_app
from web.configuration import Configuration
from web.runtime import prepare_environment
from web.server import lock_server, main


ROOT = Path(__file__).resolve().parents[1]


class EnvironmentTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.project = (Path(temporary.name) / "project with spaces").resolve()
        (self.project / "config/environments").mkdir(parents=True)
        (self.project / "data").mkdir()
        for name in ("dev", "prod"):
            shutil.copyfile(ROOT / f"config/environments/{name}.env.example",
                            self.project / f"config/environments/{name}.env.example")
        shutil.copyfile(ROOT / "config/settings.py", self.project / "config/settings.py")
        (self.project / "config/user_agents.txt").write_text("Test User Agent\n", encoding="utf-8")
        (self.project / "data/names.txt").write_text("Test Person\n", encoding="utf-8")
        (self.project / "config/proxies.txt").write_text("legacy:80:user:secret\n", encoding="utf-8")
        (self.project / ".env").write_text("WEB_ADMIN_PASSWORD=legacy-admin-password\n", encoding="utf-8")
        (self.project / "data/accounts.json").write_text(
            '[{"email":"legacy@example.invalid","password":"legacy-test-password"}]', encoding="utf-8")

    def environment(self, mode):
        return prepare_environment(mode, self.project)

    def application(self, mode):
        root = self.environment(mode)
        with (root / ".env").open("a", encoding="utf-8") as stream:
            stream.write(f"\nWEB_ADMIN_PASSWORD={mode}-only-administrator-password\n")
        app = create_app(root=root, code_root=self.project, deployment=mode, environment={})
        app.config["TESTING"] = True
        self.addCleanup(app.extensions["web_tasks"].close)
        return app

    def login(self, app):
        client = app.test_client()
        client.get("/login")
        with client.session_transaction() as session:
            token = session["csrf"]
        response = client.post("/login", data={
            "password": f"{app.config['DEPLOYMENT']}-only-administrator-password", "csrf_token": token,
        })
        self.assertEqual(response.status_code, 302)
        return client

    def test_prepare_keeps_data_configuration_and_secrets_separate(self):
        dev = self.environment("dev")
        prod = self.environment("prod")
        self.assertNotEqual(dev, prod)
        for root in (dev, prod):
            self.assertNotIn("legacy-admin-password", (root / ".env").read_text())
            self.assertNotIn("secret", (root / "config/proxies.txt").read_text())
            self.assertFalse((root / "data/accounts.json").exists())
            self.assertFalse((root / "data/database.db").exists())
            self.assertEqual((root / "data/names.txt").read_text(), "Test Person\n")
        self.assertIn("HEADLESS_MODE=False", (dev / ".env").read_text())
        self.assertIn("HEADLESS_MODE=True", (prod / ".env").read_text())
        if os.name != "nt":
            self.assertEqual((dev / ".env").stat().st_mode & 0o777, 0o600)
            self.assertEqual((prod / ".env").stat().st_mode & 0o777, 0o600)

    def test_prepare_never_overwrites_existing_environment_files(self):
        dev = self.environment("dev")
        (dev / ".env").write_text("WEB_ADMIN_PASSWORD=my-existing-long-password\n", encoding="utf-8")
        (dev / "config/proxies.txt").write_text("127.0.0.1:8080\n", encoding="utf-8")
        self.environment("dev")
        self.assertEqual((dev / ".env").read_text(), "WEB_ADMIN_PASSWORD=my-existing-long-password\n")
        self.assertEqual((dev / "config/proxies.txt").read_text(), "127.0.0.1:8080\n")

    def test_rejects_symlinked_environment_and_invalid_mode(self):
        prod = self.environment("prod")
        (self.project / "runtime/dev").symlink_to(prod, target_is_directory=True)
        with self.assertRaises(ValueError):
            self.environment("dev")
        with self.assertRaises(ValueError):
            self.environment("../prod")

    def test_resource_and_log_configuration_cannot_escape_environment(self):
        dev = self.environment("dev")
        prod = self.environment("prod")
        config = Configuration(dev, environment={}, code_root=self.project)
        for key in ("PROXY_FILE", "LOG_FILE", "NAMES_FILE", "CHAIN_FILE", "ACCOUNTS_FILE"):
            with self.subTest(key=key), self.assertRaises(ValueError):
                config.save({key: str(prod / "data/shared.txt")})
        config.save({"LOG_FILE": "data/dev-only.log"})
        self.assertEqual(config.values()["LOG_FILE"], "data/dev-only.log")
        overridden = Configuration(dev, environment={"LOG_FILE": str(prod / "data/log.txt")},
                                   code_root=self.project)
        with self.assertRaises(ValueError):
            overridden.values()

    def test_databases_task_history_and_config_updates_are_isolated(self):
        dev = self.application("dev")
        prod = self.application("prod")
        dev.extensions["web_database"].save_account("dev@example.invalid", "test-only")
        dev.extensions["web_tasks"].store.create("validate", {})
        dev.extensions["web_configuration"].save({"YOUR_PASSWORD": "dev-config-only"})
        self.assertEqual(prod.extensions["web_database"].get_all_accounts(), [])
        self.assertEqual(prod.extensions["web_tasks"].store.list(), [])
        self.assertEqual(prod.extensions["web_configuration"].values()["YOUR_PASSWORD"], "")
        self.assertEqual(len(dev.extensions["web_database"].get_all_accounts()), 1)

    def test_cookie_names_and_signatures_do_not_cross_environments(self):
        dev = self.application("dev")
        prod = self.application("prod")
        dev.config["SECRET_KEY"] = prod.config["SECRET_KEY"] = "shared-key-for-isolation-test"
        dev_client = self.login(dev)
        prod_client = prod.test_client()
        dev_cookie = dev.config["SESSION_COOKIE_NAME"]
        prod_cookie = prod.config["SESSION_COOKIE_NAME"]
        self.assertNotEqual(dev_cookie, prod_cookie)
        prod_client.set_cookie(dev_cookie, dev_client.get_cookie(dev_cookie).value)
        self.assertEqual(prod_client.get("/api/overview").status_code, 401)
        prod_client.set_cookie(prod_cookie, dev_client.get_cookie(dev_cookie).value)
        self.assertEqual(prod_client.get("/api/overview").status_code, 401)
        self.assertIn("开发环境", dev_client.get("/").get_data(as_text=True))
        self.assertIn("正式环境", prod_client.get("/login").get_data(as_text=True))

    def test_environment_secret_key_is_loaded_from_its_own_file(self):
        dev = self.environment("dev")
        (dev / ".env").write_text(
            "WEB_ADMIN_PASSWORD=dev-only-administrator-password\nWEB_SECRET_KEY=dev-local-signing-key\n",
            encoding="utf-8")
        app = create_app(root=dev, code_root=self.project, deployment="dev", environment={})
        self.addCleanup(app.extensions["web_tasks"].close)
        self.assertEqual(app.config["SECRET_KEY"], "dev-local-signing-key")

    def test_environment_locks_allow_dev_and_prod_but_not_duplicate_dev(self):
        dev = self.environment("dev")
        prod = self.environment("prod")
        with lock_server(dev), lock_server(prod):
            with self.assertRaises(ValueError):
                lock_server(dev)

    def test_real_workers_use_environment_cwd_and_do_not_import_legacy_accounts(self):
        apps = [self.application("dev"), self.application("prod")]
        jobs = [(app.extensions["web_tasks"], app.extensions["web_tasks"].start("migrate", {}))
                for app in apps]
        for manager, job in jobs:
            deadline = time.monotonic() + 10
            while time.monotonic() < deadline:
                task = manager.store.get(job["id"])
                if task["status"] not in ("running", "stopping"):
                    break
                time.sleep(0.05)
            self.assertEqual(task["status"], "completed", manager.store.logs(job["id"]))
            self.assertEqual(task["result"], {"migrated": 0})
            self.assertTrue((manager.root / "data/database.db").exists())
        self.assertEqual(apps[0].extensions["web_database"].get_all_accounts(), [])
        self.assertEqual(apps[1].extensions["web_database"].get_all_accounts(), [])

    def test_dev_server_is_local_without_debugger_or_process_reload(self):
        application = Mock()
        configuration = Mock()
        configuration.compensation_scheduler_settings.return_value = {"enabled": False}
        application.extensions = {
            "web_tasks": Mock(), "web_configuration": configuration,
        }
        application.config = {}
        with patch("web.server.ROOT", self.project), patch("web.server.os.chdir"), \
                patch.object(sys, "argv", ["app", "--env", "dev"]), \
                patch("web.app.create_app", return_value=application) as factory, \
                patch("waitress.serve") as production_server, patch("web.server.atexit.register"), \
                redirect_stdout(io.StringIO()):
            main()
        production_server.assert_not_called()
        application.run.assert_called_once_with(
            host="127.0.0.1", port=8081, debug=True, use_reloader=False,
            use_debugger=False, load_dotenv=False)
        self.assertTrue(application.config["TEMPLATES_AUTO_RELOAD"])
        self.assertEqual(factory.call_args.kwargs["root"], self.project / "runtime/dev")
        self.assertEqual(factory.call_args.kwargs["code_root"], self.project)
        self.assertTrue((self.project / "runtime/dev/data/web/server.log").exists())

    def test_production_uses_waitress_and_isolated_data(self):
        application = Mock()
        configuration = Mock()
        configuration.compensation_scheduler_settings.return_value = {"enabled": False}
        application.extensions = {
            "web_tasks": Mock(), "web_configuration": configuration,
        }
        with patch("web.server.ROOT", self.project), patch("web.server.os.chdir"), \
                patch.object(sys, "argv", ["app", "--env", "prod"]), \
                patch("web.app.create_app", return_value=application) as factory, \
                patch("waitress.serve") as production_server, patch("web.server.atexit.register"), \
                redirect_stdout(io.StringIO()):
            main()
        application.run.assert_not_called()
        production_server.assert_called_once_with(application, host="127.0.0.1", port=8080, threads=8)
        self.assertEqual(factory.call_args.kwargs["root"], self.project / "runtime/prod")
        self.assertEqual(factory.call_args.kwargs["deployment"], "prod")

    def test_dev_rejects_public_bind_before_creating_runtime(self):
        with patch("web.server.ROOT", self.project), \
                patch.object(sys, "argv", ["app", "--env", "dev", "--host", "0.0.0.0"]), \
                redirect_stderr(io.StringIO()), self.assertRaises(SystemExit) as error:
            main()
        self.assertEqual(error.exception.code, 2)
        self.assertFalse((self.project / "runtime").exists())

    def test_shell_script_reports_setup_and_does_not_install_during_start(self):
        script = self.project / "start.sh"
        shutil.copyfile(ROOT / "start.sh", script)
        for mode in ("dev", "prod"):
            result = subprocess.run(["bash", str(script), mode], capture_output=True, text=True)
            self.assertEqual(result.returncode, 1)
            self.assertIn(f"./start.sh {mode} --setup", result.stderr)
        invalid = subprocess.run(["bash", str(script), "invalid"], capture_output=True, text=True)
        self.assertEqual(invalid.returncode, 2)
        self.assertFalse((self.project / "venv").exists())
        override = subprocess.run(["bash", str(script), "prod", "--env", "dev"],
                                  capture_output=True, text=True)
        self.assertEqual(override.returncode, 2)
        self.assertIn("cannot override", override.stderr)

    def test_shell_script_selects_the_environment_interpreter_and_preserves_arguments(self):
        script = self.project / "start.sh"
        shutil.copyfile(ROOT / "start.sh", script)
        for mode in ("dev", "prod"):
            executable = self.project / f"venv/{mode}/bin/python"
            executable.parent.mkdir(parents=True)
            output = self.project / f"{mode}-arguments.txt"
            environment_output = self.project / f"{mode}-python-environment.txt"
            executable.write_text(
                f'#!/usr/bin/env bash\nprintf "%s\\n" "$@" > "{output}"\n'
                f'printf "%s\\n" "${{PYTHONPATH-unset}}" "${{PYTHONHOME-unset}}" > "{environment_output}"\n',
                encoding="utf-8")
            executable.chmod(0o700)
            result = subprocess.run(["bash", str(script), mode, "--port", "9000"],
                                    capture_output=True, text=True, cwd=self.project.parent,
                                    env=dict(os.environ, PYTHONPATH="/other-environment",
                                             PYTHONHOME="/other-interpreter"))
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(output.read_text().splitlines(), [
                str(self.project / "auto_gmail_creator.py"), "--env", mode, "--port", "9000",
            ])
            self.assertEqual(environment_output.read_text().splitlines(), ["unset", "unset"])

    def test_real_dev_and_prod_servers_can_run_and_authenticate_independently(self):
        from html.parser import HTMLParser

        class LoginForm(HTMLParser):
            token = None

            def handle_starttag(self, tag, attributes):
                fields = dict(attributes)
                if tag == "input" and fields.get("name") == "csrf_token":
                    self.token = fields.get("value")

        def cleanup(process, output):
            if process.poll() is None:
                process.terminate()
            try:
                process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=5)
            output.close()

        jar = CookieJar()
        browser = build_opener(HTTPCookieProcessor(jar))
        addresses = {}
        apps = {}
        for mode in ("dev", "prod"):
            apps[mode] = self.application(mode)
            with socket.socket() as candidate:
                candidate.bind(("127.0.0.1", 0))
                port = candidate.getsockname()[1]
            address = f"http://127.0.0.1:{port}"
            addresses[mode] = address
            output = (self.project / (mode + "-server-output.txt")).open("w", encoding="utf-8")
            environment = dict(os.environ, PYTHONPATH=str(ROOT), PYTHONDONTWRITEBYTECODE="1")
            process = subprocess.Popen([
                sys.executable, "-c",
                "import sys; from pathlib import Path; import web.server as server; "
                "server.ROOT=Path(sys.argv.pop(1)); server.main()",
                str(self.project), "--env", mode, "--port", str(port),
            ], env=environment, stdout=output, stderr=subprocess.STDOUT, start_new_session=True)
            self.addCleanup(cleanup, process, output)
            deadline = time.monotonic() + 15
            while True:
                try:
                    with browser.open(address + "/login", timeout=1) as response:
                        html = response.read().decode()
                    break
                except URLError:
                    if process.poll() is not None or time.monotonic() >= deadline:
                        self.fail("Environment server failed to become responsive: " + mode)
                    time.sleep(0.1)
            form = LoginForm()
            form.feed(html)
            self.assertIsNotNone(form.token)
            request = Request(address + "/login", data=urlencode({
                "csrf_token": form.token, "password": f"{mode}-only-administrator-password",
            }).encode())
            with browser.open(request, timeout=5) as response:
                self.assertEqual(response.status, 200)
                self.assertIn("开发环境" if mode == "dev" else "正式环境", response.read().decode())
        self.assertIn("gmail_web_dev", {cookie.name for cookie in jar})
        self.assertIn("gmail_web_prod", {cookie.name for cookie in jar})
        apps["dev"].extensions["web_database"].save_account("development@example.invalid", "test-password")
        for mode, count in (("dev", 1), ("prod", 0)):
            with browser.open(addresses[mode] + "/api/accounts", timeout=5) as response:
                self.assertEqual(len(json.load(response)["accounts"]), count)


if __name__ == "__main__":
    unittest.main()
