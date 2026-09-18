"""Two-channel account health checks.

Health is deliberately an observation operation, not a login helper.  When an
account has a registered browser profile the profile manifest selects the
engine, proxy binding, identity and lease.  The browser probe never enters
credentials; IMAP is checked independently and the compatibility ``status``
is derived only after both channel facts are available.
"""

import asyncio
import imaplib
import inspect
import logging
import socket
import threading
import time
from datetime import datetime, timezone
from functools import wraps

from core.profile_runtime import (
    BrowserProfileKernel,
    ProfileConflictError,
    ProfileRuntime,
    ProfileRuntimeError,
    classify_browser_observation,
    derive_overall_status,
)

logger = logging.getLogger("gmail_creator_health")


class _HybridMethod:
    """Bind a method to an instance while retaining old class-call syntax."""

    def __init__(self, function):
        self.function = function
        wraps(function)(self)
        parameters = list(inspect.signature(function).parameters.values())[1:]
        self.signature = inspect.Signature(parameters=parameters)

    def __get__(self, instance, owner):
        target = instance if instance is not None else owner()

        def bound(*args, **kwargs):
            return self.function(target, *args, **kwargs)

        bound.__name__ = self.function.__name__
        bound.__doc__ = self.function.__doc__
        bound.__signature__ = self.signature
        return bound


def _utc_now():
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def _run_coroutine_sync(coroutine):
    """Run an async Playwright probe from synchronous health APIs."""
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(coroutine)

    result = []
    errors = []

    def runner():
        try:
            result.append(asyncio.run(coroutine))
        except BaseException as exc:
            errors.append(exc)

    thread = threading.Thread(target=runner, name="profile-health-sync", daemon=True)
    thread.start()
    thread.join()
    if errors:
        raise errors[0]
    return result[0] if result else None


def _normalise_channel_result(value):
    """Accept the three-field test-double shape and optional metadata fields."""
    values = list(value) if isinstance(value, (tuple, list)) else [value]
    values.extend([""] * max(0, 5 - len(values)))
    return tuple(values[:5])


class AccountHealthChecker:
    IMAP_HOST = "imap.gmail.com"
    IMAP_PORT = 993

    def __init__(self, runtime=None):
        self.runtime = runtime or ProfileRuntime.from_environment()

    def _profile_for_probe(self, email, profile_id=None, engine=None, proxy=None):
        """Resolve a manifest and return ``(handle, manifest)`` metadata."""
        if not profile_id:
            if proxy:
                raise ProfileConflictError(
                    "a proxy cannot be supplied without a registered profile"
                )
            if engine and engine not in ("playwright", "selenium"):
                raise ProfileConflictError("unsupported browser engine")
            return None, None
        handle = self.runtime.resolve(
            profile_id=profile_id,
            expected_email=email,
            expected_engine=engine,
        )
        manifest = self.runtime.load(handle)
        self.runtime.validate_proxy(manifest, proxy)
        return handle, manifest

    def _probe_browser(self, email, profile_id=None, engine=None, proxy=None):
        """Probe the manifest-selected browser without entering credentials."""
        try:
            handle, manifest = self._profile_for_probe(
                email, profile_id, engine, proxy
            )
            if handle is None:
                return "not_configured", "No registered browser profile", "", "", ""
            recorded_engine = manifest.get("engine", "")
            kernel = BrowserProfileKernel(self.runtime)
            if recorded_engine == "playwright":
                observation = _run_coroutine_sync(
                    kernel.probe_playwright(handle, expected_email=email, proxy=proxy)
                )
            elif recorded_engine == "selenium":
                observation = kernel.probe_selenium(
                    handle, expected_email=email, proxy=proxy
                )
            else:
                return (
                    "profile_conflict", "Profile is bound to an unsupported engine",
                    "profile_conflict", recorded_engine, handle.profile_id,
                )

            status = classify_browser_observation(observation, email)
            if isinstance(observation, dict):
                code = str(observation.get("code") or "")
                message = str(observation.get("error") or "")
            else:
                code = str(getattr(observation, "code", "") or "")
                message = str(getattr(observation, "error", "") or "")
            if not message:
                message = {
                    "authenticated": "Browser session is authenticated",
                    "login_required": "Browser profile requires web login",
                    "challenge": "Browser profile requires a challenge",
                    "account_mismatch": "Browser profile identity does not match the account",
                    "profile_busy": "Browser profile is busy",
                    "runtime_mismatch": "Browser runtime does not match the profile",
                    "proxy_mismatch": "Configured proxy does not match the profile",
                    "proxy_unavailable": "Bound proxy is unavailable",
                    "runtime_unavailable": "Browser runtime is unavailable",
                    "identity_unavailable": "Browser session identity is unavailable",
                    "profile_conflict": "Browser profile binding conflicts with the account",
                }.get(status, "Browser health is unavailable")
            if not code and status not in ("authenticated", "not_configured"):
                code = status
            return status, message, code, recorded_engine, handle.profile_id
        except ProfileRuntimeError as exc:
            return exc.code, exc.code, exc.code, engine or "", profile_id or ""
        except Exception as exc:
            logger.debug("Browser health probe failed for %s: %s", email, type(exc).__name__)
            return "runtime_unavailable", "Browser probe failed", "runtime_unavailable", engine or "", profile_id or ""

    def _check_mailbox(self, email, password):
        """Return an independent IMAP fact as ``(status, message, code)``."""
        mail = None
        try:
            mail = imaplib.IMAP4_SSL(self.IMAP_HOST, self.IMAP_PORT, timeout=15)
            mail.login(email, password)
            mail.select("INBOX")
            return "active", "IMAP login successful", ""
        except imaplib.IMAP4.error as exc:
            error = str(exc)
            lowered = error.lower()
            if any(token in lowered for token in (
                "invalid", "credential", "authenticationfailed", "auth failed",
                "password",
            )):
                return "password_changed", "Invalid credentials — password may have been changed", "password_changed"
            if any(token in lowered for token in ("web login", "less secure", "imap disabled")):
                return "locked", "Account requires web login — may be locked", "locked"
            if any(token in lowered for token in ("suspended", "disabled")):
                return "suspended", "Account suspended by Google", "suspended"
            return "error", "IMAP operation failed", "error"
        except (ConnectionError, TimeoutError, socket.timeout, OSError):
            return "network_error", "Cannot connect to Gmail IMAP server", "network_error"
        except Exception as exc:
            return "error", "IMAP operation failed (%s)" % type(exc).__name__, "error"
        finally:
            if mail is not None:
                try:
                    mail.logout()
                except Exception:
                    pass

    def _snapshot_from_browser(self, email, password, browser_values):
        """Combine a browser fact with the independent IMAP fact."""
        checked_at = _utc_now()
        browser_checked_at = _utc_now()
        browser_values = _normalise_channel_result(browser_values)
        browser_status, browser_message, browser_code, recorded_engine, resolved_id = browser_values

        mailbox_checked_at = _utc_now()
        mailbox_status, mailbox_message, mailbox_code, _unused, _unused_id = _normalise_channel_result(
            self._check_mailbox(email, password)
        )

        browser_status = browser_status or "error"
        mailbox_status = mailbox_status or "error"
        overall = derive_overall_status(browser_status, mailbox_status)
        if mailbox_code and mailbox_status in ("password_changed", "suspended", "locked"):
            last_error_code = mailbox_code
        elif browser_code:
            last_error_code = browser_code
        else:
            last_error_code = mailbox_code

        if overall == "active":
            message = (
                "Browser session and IMAP are available"
                if browser_status == "authenticated"
                else "IMAP is available; no browser profile is configured"
            )
        elif overall == "degraded":
            message = "; ".join(item for item in (browser_message, mailbox_message) if item)
        elif overall == "password_changed":
            message = mailbox_message or "Mailbox credentials were rejected"
        elif overall == "suspended":
            message = mailbox_message or "Account is suspended"
        elif overall == "locked":
            message = mailbox_message or browser_message or "Account is locked"
        else:
            message = "; ".join(item for item in (browser_message, mailbox_message) if item)

        return {
            "email": email,
            "profile_id": resolved_id or "",
            "engine": recorded_engine or "",
            "browser_status": browser_status,
            "mailbox_status": mailbox_status,
            "status": overall,
            "overall_status": overall,
            "message": message,
            "browser_message": browser_message,
            "mailbox_message": mailbox_message,
            "browser_checked_at": browser_checked_at,
            "mailbox_checked_at": mailbox_checked_at,
            "checked_at": checked_at,
            "last_error_code": last_error_code or "",
        }

    @_HybridMethod
    def check_single(self, email, password, profile_id=None, engine=None, proxy=None):
        """Check browser and mailbox independently and derive one snapshot."""
        browser_values = _normalise_channel_result(self._probe_browser(
            email, profile_id=profile_id,
            engine=engine, proxy=proxy,
        ))
        return self._snapshot_from_browser(email, password, browser_values)

    @_HybridMethod
    def check_legacy(self, email, password):
        """Diagnose a path-only account without treating its path as identity."""
        result = self._snapshot_from_browser(
            email,
            password,
            (
                "profile_unavailable",
                "Legacy browser profile requires explicit adoption",
                "legacy_unbound",
                "",
                "",
            ),
        )
        return result

    @_HybridMethod
    def check_all(self, accounts, delay_between=2):
        """Check all complete account rows, preserving both channel facts."""
        results = []
        for index, account in enumerate(accounts):
            email = account.get("email", "")
            password = account.get("password", "")
            if not email or not password:
                continue
            profile_id = account.get("profile_id") or None
            engine = account.get("engine") or None
            proxy = account.get("proxy") or None
            if profile_id or engine or proxy:
                result = self.check_single(
                    email, password, profile_id=profile_id,
                    engine=engine, proxy=proxy,
                )
            elif account.get("profile_path"):
                result = self.check_legacy(email, password)
            else:
                result = self.check_single(email, password)
            results.append(result)
            logger.info("Health check: %s -> %s", email, result["status"])
            if index < len(accounts) - 1:
                time.sleep(delay_between)
        return results

    @staticmethod
    def get_summary(results):
        """Summarize derived statuses; ``degraded`` is never counted active."""
        total = len(results)
        active = sum(1 for item in results if item.get("status") == "active")
        degraded = sum(1 for item in results if item.get("status") == "degraded")
        locked = sum(1 for item in results if item.get("status") == "locked")
        suspended = sum(1 for item in results if item.get("status") == "suspended")
        password_changed = sum(1 for item in results if item.get("status") == "password_changed")
        network_error = sum(1 for item in results if item.get("status") == "network_error")
        errors = sum(1 for item in results if item.get("status") in ("error", "network_error"))
        return {
            "total": total,
            "active": active,
            "degraded": degraded,
            "locked": locked,
            "suspended": suspended,
            "password_changed": password_changed,
            "network_error": network_error,
            "errors": errors,
            "health_rate": (active / total * 100) if total else 0,
        }


__all__ = ["AccountHealthChecker"]
