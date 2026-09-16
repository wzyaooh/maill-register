import asyncio
import http.server
import os
import shutil
import ssl
import subprocess
import tempfile
import threading
import types
import unittest
import urllib.request
from pathlib import Path
from unittest.mock import patch

from core.profile_runtime import BrowserProfileKernel, ProfileRuntime


class _FixtureHandler(http.server.BaseHTTPRequestHandler):
    ROUTES = {
        "/inbox": (200, "Inbox Compose Primary", {}),
        "/login": (200, "Sign in to Gmail Email or phone", {}),
        "/challenge": (200, "Verify it's you unusual activity", {}),
        "/misleading": (200, "Inbox Compose Primary", {}),
        "/redirect": (302, "", {"Location": "/inbox"}),
    }

    def do_GET(self):  # noqa: N802 - stdlib handler API
        status, body, headers = self.ROUTES.get(self.path, (404, "not found", {}))
        self.send_response(status)
        for name, value in headers.items():
            self.send_header(name, value)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body.encode("utf-8"))))
        self.end_headers()
        self.wfile.write(body.encode("utf-8"))

    def log_message(self, *_args):
        return


class _LocalProtocolServer:
    def __init__(self):
        self.directory = tempfile.TemporaryDirectory()
        self.httpd = None
        self.httpsd = None
        self.threads = []
        self._start_servers()

    def _serve(self, server):
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        self.threads.append(thread)

    def _start_servers(self):
        self.httpd = http.server.ThreadingHTTPServer(
            ("127.0.0.1", 0), _FixtureHandler
        )
        self._serve(self.httpd)

        cert = Path(self.directory.name) / "cert.pem"
        key = Path(self.directory.name) / "key.pem"
        subprocess.run(
            [
                shutil.which("openssl"), "req", "-x509", "-newkey", "rsa:2048",
                "-nodes", "-days", "1", "-subj", "/CN=127.0.0.1",
                "-keyout", str(key), "-out", str(cert),
            ],
            check=True,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        self.httpsd = http.server.ThreadingHTTPServer(
            ("127.0.0.1", 0), _FixtureHandler
        )
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        context.load_cert_chain(str(cert), str(key))
        self.httpsd.socket = context.wrap_socket(self.httpsd.socket, server_side=True)
        self._serve(self.httpsd)

    @property
    def http_base(self):
        return "http://127.0.0.1:%s" % self.httpd.server_port

    @property
    def https_base(self):
        return "https://127.0.0.1:%s" % self.httpsd.server_port

    def close(self):
        for server in (self.httpd, self.httpsd):
            if server is not None:
                server.shutdown()
                server.server_close()
        for thread in self.threads:
            thread.join(timeout=2)
        self.directory.cleanup()


class _LocalPage:
    def __init__(self, url, observed=None, shell=False):
        self.target = url
        self.url = ""
        self.body = ""
        self.observed = observed
        self.shell = shell
        self.response_status = None

    async def goto(self, _url, **_kwargs):
        request = urllib.request.Request(self.target)
        context = ssl._create_unverified_context() if self.target.startswith("https://") else None
        with urllib.request.urlopen(request, context=context) as response:
            self.url = response.geturl()
            self.response_status = response.status
            self.body = response.read().decode("utf-8")
        return types.SimpleNamespace(status=self.response_status)

    async def wait_for_timeout(self, *_args):
        return None

    async def content(self):
        return self.body

    async def evaluate(self, script):
        if "querySelectorAll('[data-email]" in script:
            return self.observed
        if "querySelector('[role=\"main\"]" in script:
            return self.shell
        return None


class _LocalContext:
    def __init__(self):
        self.closed = False

    async def cookies(self):
        # Deliberately valid-looking Google cookie facts. The classifier must
        # still reject them when the final origin is a local host.
        return [{
            "name": "SID", "value": "fixture-session", "domain": ".google.com",
            "secure": True, "expires": 4102444800,
        }]

    async def close(self):
        self.closed = True


class _LocalPlaywrightManager:
    def __init__(self, page):
        self.page = page
        self.context = _LocalContext()
        self.closed = False

    async def initialize(self, **_kwargs):
        return True

    async def close(self):
        self.closed = True
        await self.context.close()

    def get_runtime_info(self):
        return {}


class _LocalDriver:
    def __init__(self, url, observed=None, shell=False):
        self.target = url
        self.current_url = ""
        self.page_source = ""
        self.observed = observed
        self.shell = shell
        self.closed = False
        self.response_status = None

    def get(self, _url):
        request = urllib.request.Request(self.target)
        context = ssl._create_unverified_context() if self.target.startswith("https://") else None
        with urllib.request.urlopen(request, context=context) as response:
            self.current_url = response.geturl()
            self.response_status = response.status
            self.page_source = response.read().decode("utf-8")

    def execute_script(self, script):
        if "querySelectorAll('[data-email]" in script:
            return self.observed
        if "querySelector('[role=\"main\"]" in script:
            return self.shell
        return None

    def get_cookies(self):
        return [{
            "name": "SID", "value": "fixture-session", "domain": ".google.com",
            "secure": True, "expires": 4102444800,
        }]

    def quit(self):
        self.closed = True


@unittest.skipUnless(shutil.which("openssl"), "openssl is required for local HTTPS fixture")
class LocalBrowserProtocolTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = _LocalProtocolServer()

    @classmethod
    def tearDownClass(cls):
        cls.server.close()

    def _runtime(self, engine):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        runtime = ProfileRuntime(directory.name)
        handle = runtime.provision("user@example.test", engine)
        runtime.bind(handle, "user@example.test")
        runtime.mark_ready(handle)
        return runtime, handle

    def test_both_engines_reject_http_and_https_local_inbox_pages(self):
        for engine in ("playwright", "selenium"):
            for base in (self.server.http_base, self.server.https_base):
                with self.subTest(engine=engine, scheme=base.split(":", 1)[0]):
                    runtime, handle = self._runtime(engine)
                    if engine == "playwright":
                        page = _LocalPage(
                            base + "/inbox", observed="user@example.test", shell=True
                        )
                        manager = _LocalPlaywrightManager(page)
                        fake = types.SimpleNamespace(
                            PlaywrightStealthManager=lambda: manager
                        )
                        with patch.dict(os.sys.modules, {"core.stealth_browser": fake}):
                            observation = asyncio.run(
                                BrowserProfileKernel(runtime).probe_playwright(
                                    handle, expected_email="user@example.test"
                                )
                            )
                        self.assertEqual(observation["origin"],
                                         "%s://127.0.0.1:%s" % (
                                             base.split(":", 1)[0],
                                             base.rsplit(":", 1)[1],
                                         ))
                        self.assertFalse(observation["authenticated"])
                        self.assertEqual(observation.status, "login_required")
                        self.assertTrue(manager.closed)
                    else:
                        driver = _LocalDriver(
                            base + "/inbox", observed="user@example.test", shell=True
                        )
                        fake_runner = types.SimpleNamespace(
                            create_driver=lambda **_kwargs: driver,
                            get_runtime_info=lambda _driver: {},
                        )
                        with patch.dict(os.sys.modules, {"core.selenium_runner": fake_runner}):
                            observation = BrowserProfileKernel(runtime).probe_selenium(
                                handle, expected_email="user@example.test"
                            )
                        self.assertFalse(observation["authenticated"])
                        self.assertEqual(observation.status, "login_required")
                        self.assertTrue(driver.closed)

    def test_local_login_challenge_and_misleading_pages_keep_distinct_status(self):
        scenarios = (
            ("/login", "login_required"),
            ("/challenge", "challenge"),
            ("/misleading", "login_required"),
        )
        for engine in ("playwright", "selenium"):
            for path, expected_status in scenarios:
                with self.subTest(engine=engine, path=path):
                    runtime, handle = self._runtime(engine)
                    url = self.server.https_base + path
                    if engine == "playwright":
                        page = _LocalPage(url, observed=None, shell=False)
                        manager = _LocalPlaywrightManager(page)
                        fake = types.SimpleNamespace(
                            PlaywrightStealthManager=lambda: manager
                        )
                        with patch.dict(os.sys.modules, {"core.stealth_browser": fake}):
                            observation = asyncio.run(
                                BrowserProfileKernel(runtime).probe_playwright(
                                    handle, expected_email="user@example.test"
                                )
                            )
                    else:
                        driver = _LocalDriver(url, observed=None, shell=False)
                        fake_runner = types.SimpleNamespace(
                            create_driver=lambda **_kwargs: driver,
                            get_runtime_info=lambda _driver: {},
                        )
                        with patch.dict(os.sys.modules, {"core.selenium_runner": fake_runner}):
                            observation = BrowserProfileKernel(runtime).probe_selenium(
                                handle, expected_email="user@example.test"
                            )
                    self.assertEqual(observation.status, expected_status)
                    self.assertFalse(observation["authenticated"])

    def test_local_redirect_is_reported_from_final_origin_not_body_words(self):
        runtime, handle = self._runtime("playwright")
        page = _LocalPage(
            self.server.https_base + "/redirect",
            observed="user@example.test",
            shell=True,
        )
        manager = _LocalPlaywrightManager(page)
        fake = types.SimpleNamespace(PlaywrightStealthManager=lambda: manager)
        with patch.dict(os.sys.modules, {"core.stealth_browser": fake}):
            observation = asyncio.run(
                BrowserProfileKernel(runtime).probe_playwright(
                    handle, expected_email="user@example.test"
                )
            )
        self.assertEqual(observation["final_url"], self.server.https_base + "/inbox")
        self.assertEqual(observation["response_status"], 200)
        self.assertEqual(observation.status, "login_required")
        self.assertFalse(observation["authenticated"])


if __name__ == "__main__":
    unittest.main()
