"""Synchronous Selenium adapter for the shared durable SMS verification core.

The SMS lifecycle stays in :mod:`core.phone_bypass` and
:mod:`services.sms_manager`.  This module only translates the small page
protocol used by that async core to Selenium's blocking WebDriver API.
"""
import asyncio
import re

from core import phone_bypass
from core.secret_safety import has_durable_sms_context


class _SeleniumPage:
    """Async page facade backed by a Selenium WebDriver."""

    def __init__(self, driver, wait):
        self._driver = driver
        self._wait = wait

    @property
    def url(self):
        return getattr(self._driver, "current_url", "") or ""

    async def content(self):
        return getattr(self._driver, "page_source", "") or ""

    async def wait_for_timeout(self, timeout_ms):
        # The shared core deliberately owns its polling cadence.  Yielding to
        # the event loop is enough here; Selenium's calls remain synchronous.
        await asyncio.sleep(max(0, float(timeout_ms or 0)) / 1000.0)

    async def query_selector(self, selector):
        from selenium.webdriver.common.by import By

        try:
            by, value = _selenium_locator(selector)
            elements = self._driver.find_elements(by, value)
        except Exception:
            return None
        for element in elements or ():
            try:
                if element.is_displayed() and element.is_enabled():
                    return _SeleniumElement(element)
            except Exception:
                continue
        return None

    async def wait_for_selector(self, selector, timeout=5000):
        # ``phone_bypass`` calls this for buttons.  A short bounded polling
        # loop keeps the adapter usable with dynamic Selenium pages while
        # avoiding an unbounded implicit wait.
        deadline = asyncio.get_running_loop().time() + max(0, timeout) / 1000.0
        while True:
            element = await self.query_selector(selector)
            if element is not None:
                return element
            if asyncio.get_running_loop().time() >= deadline:
                return None
            await asyncio.sleep(0.05)


class _SeleniumElement:
    def __init__(self, element):
        self._element = element

    async def is_visible(self):
        try:
            return bool(self._element.is_displayed())
        except Exception:
            return False

    async def click(self):
        self._element.click()

    async def tap(self):
        await self.click()

    async def fill(self, value):
        self._element.clear()
        self._element.send_keys(value)


def _run(coroutine):
    """Run one adapter coroutine without nesting an active event loop."""
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(coroutine)
    # Selenium registration is a synchronous API.  If an embedder invokes it
    # from an event-loop thread, fail closed rather than nesting asyncio.run.
    coroutine.close()
    raise RuntimeError("Selenium SMS verification cannot run inside an active event loop")


def _selenium_locator(selector):
    """Translate the small selector subset shared by the two engines."""
    from selenium.webdriver.common.by import By

    text_match = re.search(r"^([a-zA-Z0-9_-]+):has-text\(['\"](.*?)['\"]\)$", selector)
    if text_match:
        tag, text = text_match.groups()
        return By.XPATH, (
            f"//{tag}[contains(normalize-space(.), {_xpath_literal(text)})]"
        )
    return By.CSS_SELECTOR, selector


def _xpath_literal(value):
    """Quote arbitrary selector text for an XPath string literal."""
    if "'" not in value:
        return f"'{value}'"
    if '"' not in value:
        return f'"{value}"'
    parts = value.split("'")
    quoted = []
    for index, part in enumerate(parts):
        if part:
            quoted.append(f"'{part}'")
        if index < len(parts) - 1:
            quoted.append('"\'"')
    return "concat(" + ", ".join(quoted) + ")"


def run_selenium_sms_verification(driver, wait, *, use_sms_api=False,
                                  job_id="", attempt_id="", order_store=None):
    """Run the shared SMS flow through a Selenium page facade.

    Returns the stable ``(success, method_or_error)`` tuple used by the
    Playwright flow.  Missing durable ownership context is a configuration
    error, never an implicit provider allocation.
    """
    if not use_sms_api:
        return False, "PHONE_REQUIRED"
    if not has_durable_sms_context(job_id, attempt_id, order_store):
        return False, "sms_missing_attempt_context"
    try:
        return _run(phone_bypass._sms_api_verification(
            _SeleniumPage(driver, wait),
            job_id=job_id, attempt_id=attempt_id, order_store=order_store,
        ))
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        operation = str(getattr(exc, "operation", "") or "").lower()
        return False, {
            "finish": "sms_finish_failed",
            "poll": "sms_poll_failed",
            "cancel": "sms_cancel_failed",
        }.get(operation, "sms_error")
