"""Engine-authoritative account warming.

The warmer is deliberately a thin policy layer around
``BrowserProfileKernel``.  A call is valid only when it can resolve a durable
profile manifest.  The manifest is checked before an adapter is imported, the
same OS-level profile lease is held for the whole browser session, and the
adapter is closed before that lease is released.

There is no path-only warmer API.  ``profile_path`` is an internal value passed
to a browser adapter *after* the profile id has been resolved and validated;
callers must supply ``profile_id``.
"""

import asyncio
import inspect
import logging
import random
import threading
import time
from typing import Any, Dict, Optional

from core.profile_runtime import (
    BrowserProfileKernel,
    ENGINES,
    ProfileConflictError,
    ProfileRuntime,
    ProfileRuntimeError,
    ProfileUnavailableError,
    inspect_process_stopped,
    identity_is_verified,
)

logger = logging.getLogger("gmail_creator_postwarmer")


def _error_result(email: str, code: str, message: str,
                 profile_id: Optional[str] = None,
                 engine: Optional[str] = None) -> Dict[str, Any]:
    """Return one stable, non-boolean failure shape for every caller."""
    browser_status = {
        "profile_conflict": "profile_conflict",
        "profile_unavailable": "profile_unavailable",
        "profile_busy": "profile_busy",
        "proxy_mismatch": "proxy_mismatch",
        "proxy_unavailable": "proxy_unavailable",
        "runtime_mismatch": "runtime_mismatch",
        "account_mismatch": "account_mismatch",
        "challenge": "challenge",
        "login_required": "login_required",
        "identity_unavailable": "identity_unavailable",
        "not_configured": "not_configured",
        "activity_failed": "authenticated",
        "cleanup_failed": "cleanup_failed",
    }.get(code, "runtime_unavailable")
    return {
        "email": email,
        "profile_id": profile_id or "",
        "engine": engine or "",
        "success": False,
        "browser_status": browser_status,
        "error_code": code,
        "message": message or code,
        "activity_attempts": 0,
        "activity_successes": 0,
        "last_activity_error": "",
        "cleanup_status": "not_started",
        "lease_released": False,
        "browser_process_stopped": True,
    }


def _success_result(email: str, handle: Any, manifest: Dict[str, Any],
                    activity: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    activity = activity or {}
    return {
        "email": email,
        "profile_id": handle.profile_id,
        "engine": manifest.get("engine", ""),
        "success": True,
        "browser_status": "authenticated",
        "error_code": "",
        "message": "Browser session warmed",
        "activity_attempts": int(activity.get("attempts", 0)),
        "activity_successes": int(activity.get("successes", 0)),
        "last_activity_error": str(activity.get("last_error", "") or ""),
        "cleanup_status": "not_started",
        "lease_released": False,
        "browser_process_stopped": True,
    }


def _activity_error(_exc: BaseException) -> str:
    """Return a useful diagnostic without copying exception payload secrets."""
    return "activity navigation failed (%s)" % type(_exc).__name__


def _activity_failed_result(email: str, handle: Any, engine: str,
                            activity: Dict[str, Any]) -> Dict[str, Any]:
    result = _error_result(
        email, "activity_failed", "No browser warm activity completed",
        handle.profile_id, engine,
    )
    result.update({
        "activity_attempts": int(activity.get("attempts", 0)),
        "activity_successes": int(activity.get("successes", 0)),
        "last_activity_error": str(activity.get("last_error", "") or ""),
    })
    return result


def _lease_is_released(lease: Any) -> bool:
    """Inspect real leases and the small wrapper leases used by contract tests."""
    current = lease
    for _ in range(3):
        if current is None:
            return False
        # Prefer the public protocol exposed by ProfileLease.  Require actual
        # booleans so dynamic proxy/mock attributes cannot turn an unknown
        # state into a successful release claim.
        try:
            release_verified = getattr(current, "release_verified", None)
        except BaseException:
            release_verified = None
        if type(release_verified) is bool:
            try:
                held = getattr(current, "held", None)
            except BaseException:
                held = None
            return release_verified and held is False
        try:
            private_release_verified = getattr(current, "_release_verified", None)
        except BaseException:
            private_release_verified = None
        if type(private_release_verified) is bool:
            try:
                held = getattr(current, "_held", None)
            except BaseException:
                held = None
            return private_release_verified and held is False
        try:
            private_held = getattr(current, "_held", None)
        except BaseException:
            private_held = None
        if type(private_held) is bool:
            return private_held is False
        current = getattr(current, "inner", None)
    # A context manager with no inspectable state cannot prove that its OS lock
    # was released.  Keep the result fail-closed and quarantine the profile.
    return False


def _apply_cleanup_result(result: Optional[Dict[str, Any]], runtime: ProfileRuntime,
                          handle: Any, cleanup: Dict[str, Any],
                          *, preserve_operation_error: bool = False) -> None:
    if result is None:
        return
    # Adapter cleanup is a security boundary.  Only literal booleans from the
    # adapter contract are facts; values such as ``"false"`` or an omitted
    # field are unknown and must fail closed.
    valid_mapping = isinstance(cleanup, dict)
    success_fact = cleanup.get("success") if valid_mapping else None
    stopped_fact = cleanup.get("browser_process_stopped") if valid_mapping else None
    success = success_fact is True
    stopped = stopped_fact is True
    if success and stopped:
        result["cleanup_status"] = "completed"
        result["browser_process_stopped"] = stopped
        return

    previous_code = str(result.get("error_code") or "")
    if preserve_operation_error and previous_code and previous_code != "cleanup_failed":
        # A deterministic profile/runtime contract failure is already a
        # terminal operation result.  Keep that primary code while recording
        # the independent cleanup failure, so callers can distinguish a
        # runtime mismatch from a browser that also failed to close.
        result.update({
            "success": False,
            "message": "Browser cleanup could not be confirmed",
            "cleanup_status": "failed",
            "browser_process_stopped": stopped,
            "cleanup_error_code": "cleanup_failed",
            "lease_released": False,
        })
    else:
        result.update({
            "success": False,
            "error_code": "cleanup_failed",
            "message": "Browser cleanup could not be confirmed",
            "cleanup_status": "failed",
            "browser_process_stopped": stopped,
            # A released file descriptor is not a safe lease release while the
            # browser may still own the profile directory.
            "lease_released": False,
        })
        if previous_code and previous_code != "cleanup_failed":
            result["operation_error_code"] = previous_code
    try:
        runtime.mark_cleanup_failed(handle, "browser_cleanup_failed")
    except BaseException:
        logger.error("Unable to quarantine profile after browser cleanup failure")


def _apply_lease_result(result: Optional[Dict[str, Any]], runtime: ProfileRuntime,
                        handle: Any, lease: Any) -> None:
    """Make lease release a required part of a successful warm operation."""
    if result is None or lease is None:
        return
    if result.get("cleanup_status") == "failed":
        # A browser that may still own the profile makes the overall lease
        # contract unsafe even when the OS lock itself happened to close.
        result["lease_released"] = False
        return
    if result.get("browser_process_stopped") is not True:
        previous_code = str(result.get("error_code") or "")
        result.update({
            "success": False,
            "error_code": "cleanup_failed",
            "message": "Browser process stop could not be confirmed",
            "cleanup_status": "failed",
            "lease_released": False,
        })
        if previous_code and previous_code != "cleanup_failed":
            result["operation_error_code"] = previous_code
        try:
            runtime.mark_cleanup_failed(handle, "browser_process_running")
        except BaseException:
            logger.error("Unable to quarantine profile after process stop failure")
        return
    if _lease_is_released(lease):
        result["lease_released"] = True
        return
    previous_code = str(result.get("error_code") or "")
    result.update({
        "success": False,
        "error_code": "cleanup_failed",
        "message": "Profile lease release could not be confirmed",
        "cleanup_status": "failed",
        "lease_released": False,
        "browser_process_stopped": result.get("browser_process_stopped") is True,
    })
    if previous_code and previous_code != "cleanup_failed":
        result["operation_error_code"] = previous_code
    try:
        runtime.mark_cleanup_failed(handle, "lease_release_failed")
    except BaseException:
        logger.error("Unable to quarantine profile after lease release failure")


async def _close_playwright_manager(manager: Any) -> Dict[str, Any]:
    async def await_close_uncancelled(value: Any) -> Any:
        """Finish adapter shutdown even when the caller is cancelled again."""
        async def invoke() -> Any:
            return await _maybe_await(value)

        task = asyncio.ensure_future(invoke())
        while True:
            try:
                return await asyncio.shield(task)
            except asyncio.CancelledError:
                if task.done():
                    # The adapter itself may have been cancelled/failed.  Do
                    # not let task.result() re-raise CancelledError and skip
                    # the operation report; convert it to an explicit cleanup
                    # failure instead.
                    try:
                        return task.result()
                    except BaseException:
                        return {
                            "success": False,
                            "browser_process_stopped": False,
                        }
                # A cancellation of the warm operation must not cancel the
                # close task while it still owns the profile directory.
                continue

    try:
        awaitable = manager.close()
        outcome = await await_close_uncancelled(awaitable)
    except BaseException as exc:
        logger.error("Playwright browser cleanup failed: %s", type(exc).__name__)
        return {"success": False, "browser_process_stopped": False}
    outcome_missing = outcome is None
    if isinstance(outcome, dict):
        cleanup = {
            "success": outcome.get("success") is True,
            "browser_process_stopped": outcome.get("browser_process_stopped") is True,
        }
    else:
        # A close method that does not return the documented facts cannot
        # prove that the browser stopped.  Treat it as a cleanup failure.
        cleanup = {"success": False, "browser_process_stopped": False}

    # Adapter close booleans are advisory when a concrete process/browser
    # handle is exposed.  A live process is always a cleanup failure, even if
    # the adapter reported success; an explicitly stopped process can confirm
    # the positive result but must not hide an adapter close failure.
    observed = inspect_process_stopped(manager)
    if observed is False:
        cleanup.update({"success": False, "browser_process_stopped": False})
    elif observed is True:
        cleanup["browser_process_stopped"] = True
        # Selenium/Playwright test doubles and older adapters may return None
        # from a successful close.  A concrete post-close stopped fact is
        # enough to establish success in that compatibility case; malformed
        # mapping fields remain failures above.
        if outcome_missing:
            cleanup["success"] = True
    return cleanup


def _selenium_process_stopped(driver: Any) -> Optional[bool]:
    return inspect_process_stopped(driver)


def _close_selenium_driver(driver: Any) -> Dict[str, Any]:
    try:
        driver.quit()
    except BaseException as exc:
        logger.error("Selenium browser cleanup failed: %s", type(exc).__name__)
        stopped = _selenium_process_stopped(driver)
        return {
            "success": False,
            "browser_process_stopped": False if stopped is not True else True,
        }
    stopped = _selenium_process_stopped(driver)
    # Selenium's ``quit`` has no reliable return value.  Require a concrete
    # process observation after it returns; unknown is deliberately failure.
    return {
        "success": stopped is True,
        "browser_process_stopped": stopped is True,
    }


async def _run_playwright_activity(page: Any,
                                   duration_minutes: int) -> Dict[str, Any]:
    target = max(0, int(duration_minutes or 0)) * 60
    if target <= 0:
        return {"attempts": 0, "successes": 0, "last_error": ""}
    started = time.monotonic()
    attempts = successes = 0
    last_error = ""
    services = (
        "https://mail.google.com/", "https://www.youtube.com/",
        "https://drive.google.com/", "https://www.google.com/",
    )
    while time.monotonic() - started < target:
        attempts += 1
        try:
            await _maybe_await(page.goto(
                random.choice(services), timeout=15000,
                wait_until="domcontentloaded",
            ))
            remaining = target - (time.monotonic() - started)
            if target > 0 and remaining > 0:
                await _maybe_await(page.wait_for_timeout(
                    min(1000, max(50, int(remaining * 1000)))
                ))
            successes += 1
        except Exception as exc:
            last_error = _activity_error(exc)
            remaining = target - (time.monotonic() - started)
            if target > 0 and remaining > 0:
                try:
                    await _maybe_await(page.wait_for_timeout(
                        min(1000, max(50, int(remaining * 1000)))
                    ))
                except Exception:
                    await asyncio.sleep(min(1.0, max(0.05, remaining)))
        if time.monotonic() - started >= target:
            break
    return {"attempts": attempts, "successes": successes,
            "last_error": last_error}


def _run_selenium_activity(driver: Any,
                           duration_minutes: int) -> Dict[str, Any]:
    target = max(0, int(duration_minutes or 0)) * 60
    if target <= 0:
        return {"attempts": 0, "successes": 0, "last_error": ""}
    started = time.monotonic()
    attempts = successes = 0
    last_error = ""
    services = (
        "https://mail.google.com/", "https://www.youtube.com/",
        "https://www.google.com/",
    )
    while time.monotonic() - started < target:
        attempts += 1
        try:
            driver.get(random.choice(services))
            remaining = target - (time.monotonic() - started)
            if target > 0 and remaining > 0:
                time.sleep(min(1, max(0.05, remaining)))
            successes += 1
        except Exception as exc:
            last_error = _activity_error(exc)
            remaining = target - (time.monotonic() - started)
            if target > 0 and remaining > 0:
                time.sleep(min(1, max(0.05, remaining)))
        if time.monotonic() - started >= target:
            break
    return {"attempts": attempts, "successes": successes,
            "last_error": last_error}


def _resolve_profile(email: str, profile_id: Optional[str],
                    requested_engine: Optional[str], proxy: Optional[str],
                    runtime: Optional[ProfileRuntime] = None):
    """Resolve and validate a profile before importing/starting an adapter."""
    runtime = runtime or ProfileRuntime.from_environment()
    if not profile_id:
        raise ProfileUnavailableError("a registered profile_id is required for warming")
    if requested_engine is not None and requested_engine not in ENGINES:
        raise ProfileConflictError("requested warmer engine is unsupported")

    # ``resolve`` loads and validates the manifest, including the immutable
    # account and engine binding.  No adapter module is imported before this
    # point.
    handle = runtime.resolve(
        profile_id=profile_id,
        expected_email=email,
        expected_engine=requested_engine,
    )
    manifest = runtime.load(handle)
    runtime.validate_proxy(manifest, proxy)
    if manifest.get("engine") not in ENGINES:
        raise ProfileConflictError("profile is not bound to a supported warmer engine")
    if manifest.get("state") not in ("bound", "ready"):
        raise ProfileUnavailableError("profile is not ready for warming")
    return runtime, handle, manifest


def _validate_locked_profile(runtime: ProfileRuntime, handle: Any,
                             email: str, expected_engine: str,
                             proxy: Optional[str]) -> Dict[str, Any]:
    """Re-read the manifest after acquiring the lease to close the TOCTOU gap."""
    manifest = runtime.load(handle)
    if manifest.get("engine") != expected_engine:
        raise ProfileConflictError("profile engine changed before warm launch")
    if str(manifest.get("email", "")).lower() != email.lower():
        raise ProfileConflictError("profile account binding changed before warm launch")
    if manifest.get("state") not in ("bound", "ready"):
        raise ProfileUnavailableError("profile is not ready for warming")
    runtime.validate_proxy(manifest, proxy)
    return manifest


async def _maybe_await(value: Any) -> Any:
    """Accept real async adapter methods and small synchronous test doubles."""
    if inspect.isawaitable(value):
        return await value
    return value


def _assert_lease_stable(lease: Any) -> None:
    """Run the kernel's directory/inode check when a real lease is present."""
    checker = getattr(lease, "assert_stable", None)
    if callable(checker):
        checker()


def _mark_lease_integrity_failure(result: Optional[Dict[str, Any]],
                                  runtime: ProfileRuntime, handle: Any,
                                  email: str, error: BaseException) -> Dict[str, Any]:
    """Convert a mid-session profile swap into an explicit failed result."""
    if isinstance(error, asyncio.CancelledError):
        code = "cancelled"
    elif isinstance(error, ProfileRuntimeError):
        code = getattr(error, "code", None) or "profile_conflict"
    else:
        # A stability check that raises an arbitrary BaseException is itself a
        # cleanup-integrity failure; it must never be mislabeled as an account
        # or profile binding conflict.
        code = "cleanup_failed"
    if result is None:
        result = _error_result(
            email,
            code, "Profile lease integrity check failed",
            getattr(handle, "profile_id", ""),
        )
    previous_code = str(result.get("error_code") or "")
    result.update({
        "success": False,
        "error_code": code,
        "message": "Profile lease integrity check failed",
        "cleanup_status": "failed",
        "lease_released": False,
    })
    if previous_code and previous_code != code:
        result["operation_error_code"] = previous_code
    try:
        runtime.mark_cleanup_failed(handle, code)
    except BaseException:
        logger.error("Unable to quarantine profile after lease integrity failure")
    return result


async def _page_text(page: Any) -> str:
    try:
        return str(await _maybe_await(page.content()) or "")
    except Exception:
        try:
            return str(await _maybe_await(
                page.evaluate("() => document.body ? document.body.innerText : ''")
            ) or "")
        except Exception:
            return ""


async def _application_shell_playwright(page: Any) -> Optional[bool]:
    try:
        value = await _maybe_await(page.evaluate("""() => Boolean(
            document.querySelector('[role="main"], [aria-label*="Inbox" i],
                a[href*="#inbox"], [data-view-id], [data-mail-shell])
        )"""))
        return value if isinstance(value, bool) else None
    except Exception:
        return None


async def _context_cookies(context: Any) -> list:
    try:
        return list(await _maybe_await(context.cookies()) or []) if context else []
    except Exception:
        return []


async def _playwright_authenticated(page: Any, context: Any, email: str,
                                    manifest: Optional[Dict[str, Any]] = None):
    kernel = BrowserProfileKernel()
    text = await _page_text(page)
    shell = await _application_shell_playwright(page)
    business_origin = str(getattr(page, "url", "") or "")
    facts = kernel._identity_auth_facts(
        await kernel._fetch_identity_playwright(
            context, page,
            expected_email=email,
            manifest_email=(manifest or {}).get("email") or email,
        ),
        expected_email=email,
        manifest=manifest,
        text=text,
        cookies=await _context_cookies(context),
        origin=business_origin,
        application_shell=shell,
    )
    return facts["authenticated"], facts["status"], facts["signals"], facts.get("observed_email")


def _persist_identity_observation(runtime: ProfileRuntime, handle: Any,
                                  manifest: Dict[str, Any],
                                  observed_email: Optional[str]) -> Dict[str, Any]:
    """Persist a fresh browser identity observation when the manifest needs it.

    The observation is made by the already-open adapter session while the
    profile lease is held.  Only the normalized email and verification time are
    written; credentials, cookies, and page contents never enter the manifest.
    """
    if not observed_email:
        return manifest
    expected = str(manifest.get("email") or "").strip().lower()
    observed = str(observed_email).strip().lower()
    if not expected or observed != expected:
        raise ProfileConflictError("observed browser identity does not match profile")
    if not identity_is_verified(manifest):
        return runtime.mark_identity_verified(handle, observed)
    return manifest


async def _warm_playwright_session(runtime: ProfileRuntime, handle: Any,
                                   manifest: Dict[str, Any], email: str,
                                   password: str, duration_minutes: int,
                                   proxy: Optional[str]):
    """Warm one Playwright session while the profile lease is held."""
    manager = None
    adapter_launch_started = False
    preserve_operation_error = False
    operation_result = None
    profile_lease = None
    try:
        # The adapter import and constructor are intentionally inside the lease:
        # even adapter initialization must not touch a profile concurrently.
        with runtime.lease(handle, "warm") as profile_lease:
            try:
                _assert_lease_stable(profile_lease)
                locked_manifest = _validate_locked_profile(
                    runtime, handle, email, "playwright", proxy
                )
            except ProfileRuntimeError as exc:
                operation_result = _error_result(
                    email, exc.code, exc.code, handle.profile_id, "playwright"
                )
                return operation_result
            except asyncio.CancelledError:
                operation_result = _error_result(
                    email, "cancelled", "Warm operation cancelled",
                    handle.profile_id, "playwright",
                )
                raise
            except BaseException as exc:
                operation_result = _mark_lease_integrity_failure(
                    None, runtime, handle, email, exc
                )
                return operation_result
            try:
                try:
                    from core.stealth_browser import PlaywrightStealthManager
                except ImportError as exc:
                    # No adapter was constructed, so there is no browser
                    # process to clean up.  Keep this a runtime failure rather
                    # than manufacturing a cleanup failure for a missing
                    # optional dependency.
                    operation_result = _error_result(
                        email, "runtime_unavailable", type(exc).__name__,
                        handle.profile_id, "playwright",
                    )
                    return operation_result
                # From this point onward the adapter may have spawned a
                # browser even if its constructor/initialize call raises.
                adapter_launch_started = True
                manager = PlaywrightStealthManager()
                operation_result = _error_result(
                    email, "runtime_unavailable", "Playwright runtime unavailable",
                    handle.profile_id, "playwright",
                )
                initialized = await _maybe_await(manager.initialize(
                    proxy=proxy,
                    profile_path=str(handle.path),
                    profile_manifest=locked_manifest,
                    lease_owned=True,
                    profile_lease=profile_lease,
                    purpose="warm",
                ))
                if not initialized:
                    operation_result = _error_result(
                        email, "runtime_unavailable", "Playwright runtime unavailable",
                        handle.profile_id, "playwright",
                    )
                    return operation_result

                runtime_info_getter = getattr(manager, "get_runtime_info", None)
                if callable(runtime_info_getter):
                    runtime_info = await _maybe_await(runtime_info_getter()) or {}
                    if runtime_info:
                        runtime.record_runtime(handle, runtime_info)

                page = getattr(manager, "page", None)
                context = getattr(manager, "context", None)
                if page is None:
                    operation_result = _error_result(
                        email, "runtime_unavailable", "Playwright page unavailable",
                        handle.profile_id, "playwright",
                    )
                    return operation_result

                await _maybe_await(page.goto(
                    "https://mail.google.com/", timeout=30000,
                    wait_until="domcontentloaded",
                ))
                authenticated, status, _signals_value, observed = \
                    await _playwright_authenticated(page, context, email, locked_manifest)
                if status in ("account_mismatch", "challenge", "identity_unavailable", "cleanup_failed"):
                    operation_result = _error_result(
                        email, status,
                        "Browser profile identity does not match the account" if status == "account_mismatch"
                        else ("Browser profile requires a challenge" if status == "challenge"
                              else "Browser session identity is unavailable"),
                        handle.profile_id, "playwright",
                    )
                    return operation_result
                if authenticated:
                    locked_manifest = _persist_identity_observation(
                        runtime, handle, locked_manifest, observed
                    )

                if not authenticated:
                    await _maybe_await(page.goto(
                        "https://accounts.google.com/signin", timeout=30000,
                        wait_until="domcontentloaded",
                    ))
                    email_input = await _maybe_await(page.query_selector('input[type="email"]'))
                    if email_input is not None:
                        await _maybe_await(email_input.fill(email))
                        await _maybe_await(page.click(
                            'button:has-text("Next"), button:has-text("التالي")'
                        ))
                        await _maybe_await(page.wait_for_timeout(1200))
                    password_input = await _maybe_await(page.query_selector('input[type="password"]'))
                    if password_input is not None:
                        await _maybe_await(password_input.fill(password))
                        await _maybe_await(page.click(
                            'button:has-text("Next"), button:has-text("التالي")'
                        ))
                        await _maybe_await(page.wait_for_timeout(2500))

                    authenticated, status, _signals_value, observed = \
                        await _playwright_authenticated(page, context, email, locked_manifest)
                    if status in ("account_mismatch", "challenge", "identity_unavailable", "cleanup_failed"):
                        operation_result = _error_result(
                            email, status,
                            "Login resolved to a different account" if status == "account_mismatch"
                            else ("Login challenge detected" if status == "challenge"
                                  else "Browser session identity is unavailable"),
                            handle.profile_id, "playwright",
                        )
                        return operation_result
                    if authenticated:
                        locked_manifest = _persist_identity_observation(
                            runtime, handle, locked_manifest, observed
                        )
                if not authenticated:
                    operation_result = _error_result(
                        email, "login_required", "Browser profile requires login",
                        handle.profile_id, "playwright",
                    )
                    return operation_result

                activity = await _run_playwright_activity(page, duration_minutes)
                operation_result = _success_result(
                    email, handle, locked_manifest, activity
                ) if int(duration_minutes or 0) <= 0 or activity["successes"] > 0 \
                    else _activity_failed_result(email, handle, "playwright", activity)
                return operation_result
            except asyncio.CancelledError:
                operation_result = _error_result(
                    email, "cancelled", "Warm operation cancelled",
                    handle.profile_id, "playwright",
                )
                raise
            except BaseException as exc:
                # Convert adapter initialization/navigation exceptions into a
                # result before cleanup runs.  The cleanup result can then
                # either preserve this runtime error or take precedence with
                # ``cleanup_failed``.
                if isinstance(exc, ProfileRuntimeError):
                    preserve_operation_error = True
                    error_code = exc.code
                else:
                    error_code = "runtime_unavailable"
                operation_result = _error_result(
                    email, error_code,
                    exc.code if isinstance(exc, ProfileRuntimeError) else type(exc).__name__,
                    handle.profile_id, "playwright",
                )
                return operation_result
            finally:
                try:
                    _assert_lease_stable(profile_lease)
                except BaseException as exc:
                    operation_result = _mark_lease_integrity_failure(
                        operation_result, runtime, handle, email, exc
                    )
                if manager is not None:
                    cleanup = await _close_playwright_manager(manager)
                    _apply_cleanup_result(
                        operation_result, runtime, handle, cleanup,
                        preserve_operation_error=preserve_operation_error,
                    )
                elif adapter_launch_started:
                    _apply_cleanup_result(
                        operation_result, runtime, handle,
                        {"success": False, "browser_process_stopped": False},
                        preserve_operation_error=preserve_operation_error,
                    )
                elif operation_result is not None:
                    operation_result["cleanup_status"] = "not_started"
                    operation_result["browser_process_stopped"] = True
                try:
                    _assert_lease_stable(profile_lease)
                except BaseException as exc:
                    operation_result = _mark_lease_integrity_failure(
                        operation_result, runtime, handle, email, exc
                    )
        if operation_result is not None:
            _apply_lease_result(operation_result, runtime, handle, profile_lease)
            if operation_result.get("cleanup_status") == "failed":
                operation_result["lease_released"] = False
            else:
                operation_result["lease_released"] = (
                    _lease_is_released(profile_lease)
                    and operation_result.get("browser_process_stopped", True)
                )
        return operation_result
    except ProfileRuntimeError as exc:
        resolved_handle = locals().get("handle")
        operation_result = _error_result(
            email, exc.code, exc.code,
            getattr(resolved_handle, "profile_id", ""),
            manifest.get("engine", "playwright") if isinstance(manifest, dict)
            else "playwright",
        )
        operation_result["lease_released"] = _lease_is_released(profile_lease)
        return operation_result
    except asyncio.CancelledError:
        if operation_result is None:
            operation_result = _error_result(
                email, "cancelled", "Warm operation cancelled",
                getattr(locals().get("handle"), "profile_id", ""),
                "playwright",
            )
        else:
            operation_result.update({
                "success": False,
                "error_code": "cancelled",
                "message": "Warm operation cancelled",
                "lease_released": False,
            })
        # Preserve task cancellation for callers, but let the outer finally
        # finish lease/process bookkeeping before the exception propagates.
        raise
    except BaseException as exc:
        logger.debug("Playwright warm failed: %s", type(exc).__name__)
        resolved_handle = locals().get("handle")
        if operation_result is None:
            operation_result = _error_result(
                email, "runtime_unavailable", type(exc).__name__,
                getattr(resolved_handle, "profile_id", ""), "playwright"
            )
        else:
            previous_code = str(operation_result.get("error_code") or "")
            operation_result.update({
                "success": False,
                "error_code": "cleanup_failed",
                "message": "Warm operation cleanup could not be confirmed",
                "cleanup_status": "failed",
                "lease_released": False,
            })
            if previous_code and previous_code != "cleanup_failed":
                operation_result["operation_error_code"] = previous_code
            try:
                runtime.mark_cleanup_failed(handle, "warm_cleanup_failed")
            except BaseException:
                logger.error("Unable to quarantine profile after warm failure")
        return operation_result
    finally:
        # A return from inside the lease block still runs this outer finally
        # after the context manager has released the OS lock.
        if operation_result is not None and profile_lease is not None:
            _apply_lease_result(operation_result, runtime, handle, profile_lease)
            if operation_result.get("cleanup_status") != "failed":
                operation_result["lease_released"] = (
                    _lease_is_released(profile_lease)
                    and operation_result.get("browser_process_stopped", True)
                )


def _application_shell_selenium(driver: Any) -> Optional[bool]:
    try:
        value = driver.execute_script("""return Boolean(
            document.querySelector('[role="main"], [aria-label*="Inbox" i],
                a[href*="#inbox"], [data-view-id], [data-mail-shell])
        );""")
        return value if isinstance(value, bool) else None
    except Exception:
        return None


def _selenium_authenticated(driver: Any, email: str,
                            manifest: Optional[Dict[str, Any]] = None):
    source = str(getattr(driver, "page_source", "") or "")
    kernel = BrowserProfileKernel()
    business_origin = str(getattr(driver, "current_url", "") or "")
    shell = _application_shell_selenium(driver)
    try:
        cookies = driver.get_cookies() or []
    except Exception:
        cookies = []
    facts = kernel._identity_auth_facts(
        kernel._fetch_identity_selenium(
            driver,
            expected_email=email,
            manifest_email=(manifest or {}).get("email") or email,
        ),
        expected_email=email,
        manifest=manifest,
        text=source,
        cookies=cookies,
        origin=business_origin,
        application_shell=shell,
    )
    return facts["authenticated"], facts["status"], facts["signals"], facts.get("observed_email")


def _warm_selenium_session(runtime: ProfileRuntime, handle: Any,
                           manifest: Dict[str, Any], email: str,
                           password: str, duration_minutes: int,
                           proxy: Optional[str]):
    """Warm one Selenium session while the profile lease is held."""
    driver = None
    adapter_launch_started = False
    preserve_operation_error = False
    operation_result = None
    profile_lease = None
    try:
        with runtime.lease(handle, "warm") as profile_lease:
            try:
                _assert_lease_stable(profile_lease)
                locked_manifest = _validate_locked_profile(
                    runtime, handle, email, "selenium", proxy
                )
            except ProfileRuntimeError as exc:
                operation_result = _error_result(
                    email, exc.code, exc.code, handle.profile_id, "selenium"
                )
                return operation_result
            except asyncio.CancelledError:
                operation_result = _error_result(
                    email, "cancelled", "Warm operation cancelled",
                    handle.profile_id, "selenium",
                )
                raise
            except BaseException as exc:
                operation_result = _mark_lease_integrity_failure(
                    None, runtime, handle, email, exc
                )
                return operation_result
            # Imports are inside the lease for the same reason as Playwright:
            # no adapter may initialize against an unowned profile directory.
            from core.selenium_runner import create_driver
            try:
                from core.selenium_runner import get_runtime_info
            except (ImportError, AttributeError):
                # Small compatibility doubles (and older adapters) may not
                # expose the helper; their driver can still carry the same
                # runtime-info attribute.
                get_runtime_info = lambda value: getattr(
                    value, "_profile_runtime_info", {}
                )
            from selenium.webdriver.common.by import By
            from selenium.webdriver.support.wait import WebDriverWait
            from selenium.webdriver.support import expected_conditions as EC

            try:
                adapter_launch_started = True
                operation_result = _error_result(
                    email, "runtime_unavailable", "Selenium runtime unavailable",
                    handle.profile_id, "selenium",
                )
                driver = create_driver(
                    proxy=proxy,
                    profile_path=str(handle.path),
                    profile_manifest=locked_manifest,
                    lease_owned=True,
                    profile_lease=profile_lease,
                    purpose="warm",
                )
                if not driver:
                    adapter_launch_started = False
                    operation_result = _error_result(
                        email, "runtime_unavailable", "Selenium runtime unavailable",
                        handle.profile_id, "selenium",
                    )
                    return operation_result
                runtime_info = get_runtime_info(driver) or {}
                if runtime_info:
                    runtime.record_runtime(handle, runtime_info)
                driver.get("https://mail.google.com/")
                authenticated, status, _signals_value, observed = _selenium_authenticated(
                    driver, email, locked_manifest
                )
                if status in ("account_mismatch", "challenge", "identity_unavailable", "cleanup_failed"):
                    operation_result = _error_result(
                        email, status,
                        "Browser profile identity does not match the account" if status == "account_mismatch"
                        else ("Browser profile requires a challenge" if status == "challenge"
                              else "Browser session identity is unavailable"),
                        handle.profile_id, "selenium",
                    )
                    return operation_result
                if authenticated:
                    locked_manifest = _persist_identity_observation(
                        runtime, handle, locked_manifest,
                        observed,
                    )

                if not authenticated:
                    driver.get("https://accounts.google.com/signin")
                    wait = WebDriverWait(driver, 15)
                    email_input = wait.until(EC.presence_of_element_located(
                        (By.CSS_SELECTOR, 'input[type="email"]')
                    ))
                    email_input.send_keys(email)
                    driver.find_element(By.XPATH, "//button[contains(text(), 'Next')]").click()
                    password_input = wait.until(EC.presence_of_element_located(
                        (By.CSS_SELECTOR, 'input[type="password"]')
                    ))
                    password_input.send_keys(password)
                    driver.find_element(By.XPATH, "//button[contains(text(), 'Next')]").click()
                    time.sleep(2)
                    authenticated, status, _signals_value, observed = _selenium_authenticated(
                        driver, email, locked_manifest
                    )
                    if status in ("account_mismatch", "challenge", "identity_unavailable", "cleanup_failed"):
                        operation_result = _error_result(
                            email, status,
                            "Login resolved to a different account" if status == "account_mismatch"
                            else ("Login challenge detected" if status == "challenge"
                                  else "Browser session identity is unavailable"),
                            handle.profile_id, "selenium",
                        )
                        return operation_result
                    if authenticated:
                        locked_manifest = _persist_identity_observation(
                            runtime, handle, locked_manifest,
                            observed,
                        )
                if not authenticated:
                    operation_result = _error_result(
                        email, "login_required", "Browser profile requires login",
                        handle.profile_id, "selenium",
                    )
                    return operation_result

                activity = _run_selenium_activity(driver, duration_minutes)
                operation_result = _success_result(
                    email, handle, locked_manifest, activity
                ) if int(duration_minutes or 0) <= 0 or activity["successes"] > 0 \
                    else _activity_failed_result(email, handle, "selenium", activity)
                return operation_result
            except asyncio.CancelledError:
                operation_result = _error_result(
                    email, "cancelled", "Warm operation cancelled",
                    handle.profile_id, "selenium",
                )
                raise
            except BaseException as exc:
                # A driver factory can fail after starting a child process but
                # before returning a driver object.  Preserve a structured
                # runtime error and let the launch-attempt flag force a
                # fail-closed cleanup result.
                if isinstance(exc, ProfileRuntimeError):
                    preserve_operation_error = True
                    error_code = exc.code
                else:
                    error_code = "runtime_unavailable"
                operation_result = _error_result(
                    email, error_code,
                    exc.code if isinstance(exc, ProfileRuntimeError) else type(exc).__name__,
                    handle.profile_id, "selenium",
                )
                return operation_result
            finally:
                try:
                    _assert_lease_stable(profile_lease)
                except BaseException as exc:
                    operation_result = _mark_lease_integrity_failure(
                        operation_result, runtime, handle, email, exc
                    )
                if driver is not None:
                    cleanup = _close_selenium_driver(driver)
                    _apply_cleanup_result(
                        operation_result, runtime, handle, cleanup,
                        preserve_operation_error=preserve_operation_error,
                    )
                elif adapter_launch_started:
                    _apply_cleanup_result(
                        operation_result, runtime, handle,
                        {"success": False, "browser_process_stopped": False},
                        preserve_operation_error=preserve_operation_error,
                    )
                elif operation_result is not None:
                    operation_result["cleanup_status"] = "not_started"
                    operation_result["browser_process_stopped"] = True
                try:
                    _assert_lease_stable(profile_lease)
                except BaseException as exc:
                    operation_result = _mark_lease_integrity_failure(
                        operation_result, runtime, handle, email, exc
                    )
        if operation_result is not None:
            _apply_lease_result(operation_result, runtime, handle, profile_lease)
            if operation_result.get("cleanup_status") == "failed":
                operation_result["lease_released"] = False
            else:
                operation_result["lease_released"] = (
                    _lease_is_released(profile_lease)
                    and operation_result.get("browser_process_stopped", True)
                )
        return operation_result
    except ProfileRuntimeError as exc:
        resolved_handle = locals().get("handle")
        operation_result = _error_result(
            email, exc.code, exc.code,
            getattr(resolved_handle, "profile_id", ""),
            manifest.get("engine", "selenium") if isinstance(manifest, dict)
            else "selenium",
        )
        operation_result["lease_released"] = _lease_is_released(profile_lease)
        return operation_result
    except asyncio.CancelledError:
        if operation_result is None:
            operation_result = _error_result(
                email, "cancelled", "Warm operation cancelled",
                getattr(locals().get("handle"), "profile_id", ""),
                "selenium",
            )
        else:
            operation_result.update({
                "success": False,
                "error_code": "cancelled",
                "message": "Warm operation cancelled",
                "lease_released": False,
            })
        raise
    except BaseException as exc:
        logger.debug("Selenium warm failed: %s", type(exc).__name__)
        resolved_handle = locals().get("handle")
        if operation_result is None:
            operation_result = _error_result(
                email, "runtime_unavailable", type(exc).__name__,
                getattr(resolved_handle, "profile_id", ""), "selenium"
            )
        else:
            previous_code = str(operation_result.get("error_code") or "")
            operation_result.update({
                "success": False,
                "error_code": "cleanup_failed",
                "message": "Warm operation cleanup could not be confirmed",
                "cleanup_status": "failed",
                "lease_released": False,
            })
            if previous_code and previous_code != "cleanup_failed":
                operation_result["operation_error_code"] = previous_code
            try:
                runtime.mark_cleanup_failed(handle, "warm_cleanup_failed")
            except BaseException:
                logger.error("Unable to quarantine profile after warm failure")
        return operation_result
    finally:
        if operation_result is not None and profile_lease is not None:
            _apply_lease_result(operation_result, runtime, handle, profile_lease)
            if operation_result.get("cleanup_status") != "failed":
                operation_result["lease_released"] = (
                    _lease_is_released(profile_lease)
                    and operation_result.get("browser_process_stopped", True)
                )


def _run_coroutine_sync(coroutine):
    """Run an async Playwright/kernel operation from sync callers safely."""
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(coroutine)

    # A sync API can still be called by code that already owns an event loop.
    # Run the coroutine on a short-lived helper thread instead of nesting
    # ``asyncio.run`` and turning a valid profile operation into an exception.
    result = []
    error = []

    def runner():
        try:
            result.append(asyncio.run(coroutine))
        except BaseException as exc:  # propagate the original failure
            error.append(exc)

    thread = threading.Thread(target=runner, name="profile-warm-sync", daemon=True)
    thread.start()
    thread.join()
    if error:
        raise error[0]
    return result[0] if result else _error_result(
        "", "runtime_unavailable", "warm operation did not return"
    )


async def warm_account_playwright(email: str, password: str,
                                  duration_minutes: int = 3,
                                  profile_id: Optional[str] = None,
                                  engine: str = "playwright",
                                  proxy: Optional[str] = None):
    """Warm a profile whose registered engine is Playwright."""
    try:
        # The specialized entry point is engine-authoritative even when a
        # caller passes ``engine=None``: it can never become a Selenium launch.
        runtime, handle, _manifest = _resolve_profile(
            email, profile_id, "playwright", proxy
        )
        if engine not in (None, "playwright"):
            return _error_result(
                email, "profile_conflict",
                "requested engine differs from Playwright warmer",
                handle.profile_id, "playwright",
            )
        return await BrowserProfileKernel(runtime).login_and_warm(
            handle, email, password, duration_minutes, proxy=proxy,
            expected_engine="playwright",
        )
    except ProfileRuntimeError as exc:
        return _error_result(email, exc.code, exc.code, profile_id, "playwright")


def warm_account_selenium(email: str, password: str,
                          duration_minutes: int = 3,
                          profile_id: Optional[str] = None,
                          engine: str = "selenium",
                          proxy: Optional[str] = None):
    """Warm a profile whose registered engine is Selenium."""
    try:
        runtime, handle, _manifest = _resolve_profile(
            email, profile_id, "selenium", proxy
        )
        if engine not in (None, "selenium"):
            return _error_result(
                email, "profile_conflict",
                "requested engine differs from Selenium warmer",
                handle.profile_id, "selenium",
            )
        return _run_coroutine_sync(BrowserProfileKernel(runtime).login_and_warm(
            handle, email, password, duration_minutes, proxy=proxy,
            expected_engine="selenium",
        ))
    except ProfileRuntimeError as exc:
        return _error_result(email, exc.code, exc.code, profile_id, "selenium")


def warm_account(email: str, password: str, duration_minutes: int = 3,
                 profile_id: Optional[str] = None,
                 engine: Optional[str] = None,
                 proxy: Optional[str] = None):
    """Resolve the recorded engine and dispatch through the browser kernel."""
    try:
        runtime, handle, manifest = _resolve_profile(
            email, profile_id, engine, proxy
        )
        recorded_engine = manifest["engine"]
        if engine is not None and engine != recorded_engine:
            return _error_result(
                email, "profile_conflict",
                "requested engine differs from registration engine",
                handle.profile_id, recorded_engine,
            )
        return _run_coroutine_sync(BrowserProfileKernel(runtime).login_and_warm(
            handle, email, password, duration_minutes, proxy=proxy,
            expected_engine=recorded_engine,
        ))
    except ProfileRuntimeError as exc:
        return _error_result(email, exc.code, exc.code, profile_id, engine)


__all__ = ["warm_account", "warm_account_playwright", "warm_account_selenium"]
