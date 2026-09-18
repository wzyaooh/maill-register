"""Browser-boundary doubles; production identity parsing remains real."""

import json
import types

from core.session_identity import IDENTITY_ENDPOINT

GMAIL_URL = "https://mail.google.com/mail/u/0/#inbox"


def provider_body(email):
    records = [] if email is None else [
        {"slot": 0, "email": email, "valid_session": True}
    ]
    return b")]}'\n" + json.dumps({"accounts": records}).encode()


class ProviderPage:
    def __init__(self, email):
        self.email = email
        self.closed = False
        self.url = "about:blank"

    async def goto(self, url, **kwargs):
        assert url == IDENTITY_ENDPOINT
        self.url = url
        return types.SimpleNamespace(
            status=200, url=url, body=lambda: provider_body(self.email),
        )

    async def close(self):
        self.closed = True


class ProviderContext:
    def __init__(self, email="bound@example.test", cookies=()):
        self.email = email
        self._cookies = cookies
        self.pages = []

    async def new_page(self):
        page = ProviderPage(self.email)
        self.pages.append(page)
        return page

    async def cookies(self):
        return self._cookies


def install_selenium_provider(driver, email="bound@example.test"):
    """Add tab operations without replacing business navigation or scripts."""
    business_get = driver.get
    business_execute = driver.execute_script
    urls = {}
    driver.current_window_handle = "business"
    driver.window_handles = ["business"]

    def switch(handle):
        assert handle in driver.window_handles
        urls[driver.current_window_handle] = driver.current_url
        driver.current_window_handle = handle
        driver.current_url = urls.get(handle, "about:blank")

    def new_window(kind):
        assert kind == "tab"
        driver.window_handles.append("provider")
        switch("provider")

    def get(url):
        if driver.current_window_handle == "provider":
            assert url == IDENTITY_ENDPOINT
            driver.current_url = url
        else:
            business_get(url)

    def execute(script, *args):
        if driver.current_window_handle == "provider":
            assert "document.body" in script
            return provider_body(email).decode()
        return business_execute(script, *args)

    def close():
        assert driver.current_window_handle == "provider"
        driver.window_handles.remove("provider")

    driver.switch_to = types.SimpleNamespace(new_window=new_window, window=switch)
    driver.get = get
    driver.execute_script = execute
    driver.close = close
    return driver
