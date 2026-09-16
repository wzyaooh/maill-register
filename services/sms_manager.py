"""
SMS Manager - Unified async SMS service handler
Provides a single interface for all SMS services:
  - 5sim.net (with cancel/finish/balance)
  - SMS-Activate
  - OnlineSIM
  - GetSMS
"""
import asyncio
import inspect
import json
import re
import logging
import math
from time import monotonic
import aiohttp
from config.settings import Config
from core.job_ledger import default_ledger_path
from core.sms_orders import SmsOrderStore
from core.secret_safety import has_durable_sms_context, normalize_error_code

logger = logging.getLogger('gmail_creator_sms')

MAX_POLL_SECONDS = 180
POLL_INTERVAL = 5


class SmsProviderError(RuntimeError):
    """A normalized provider failure safe to expose to operation callers."""

    def __init__(self, provider, operation, error_code="provider_error"):
        self.provider = str(provider or "")[:64]
        self.operation = str(operation or "")[:32]
        normalized = normalize_error_code(
            error_code, default="provider_error", allow_empty=False,
        )
        if normalized not in {"provider_error", "provider_timeout", "provider_rejected"}:
            normalized = "provider_error"
        self.error_code = normalized
        super().__init__(self.error_code)


def _resolve_order_store(order_store):
    if order_store is False:
        return None
    return order_store if order_store is not None else SmsOrderStore(default_ledger_path())


def _local_order(store, service_name, provider_order_id, local_order_id=None):
    if store is None:
        return None
    if local_order_id:
        return store.get_order(local_order_id)
    finder = getattr(store, "get_by_provider_order", None)
    if callable(finder):
        return finder(service_name, provider_order_id)
    return None


def _provider_functions(service_name):
    return {
        "5sim": {
            "cancel": _cancel_5sim_order,
            "finish": _finish_5sim_order,
        },
        "sms_activate": {
            "cancel": _cancel_sms_activate_order,
            "finish": _finish_sms_activate_order,
        },
        "onlinesim": {
            "cancel": _cancel_onlinesim_order,
            "finish": None,
        },
        "getsms": {
            "cancel": _cancel_getsms_order,
            "finish": _finish_getsms_order,
        },
    }.get(str(service_name or "").strip().lower(), {})


_PROVIDER_FAILURE_WORDS = frozenset({
    "error", "errors", "failed", "failure", "failing", "rejected",
    "denied", "invalid", "declined", "forbidden", "not_found",
    "no_balance", "no_numbers", "no_number", "no_operation",
    "bad_request", "unauthorized", "cancel_failed", "finish_failed",
})
_PROVIDER_SUCCESS_WORDS = frozenset({
    "ok", "success", "succeeded", "completed", "complete", "finished",
    "finish", "cancelled", "canceled", "cancel", "accepted", "true",
    "access_ready", "access_number", "status_ok", "status_finish",
})


def _provider_status_is_failure(value, operation):
    """Interpret a provider status without treating cancel acknowledgements as errors."""
    if isinstance(value, bool):
        return value is False
    text = str(value or "").strip().lower().replace("-", "_")
    if not text:
        return False
    if text in _PROVIDER_SUCCESS_WORDS:
        # A cancelled acknowledgement is the expected successful response to
        # a cancellation request, but is a failure for a finish request.
        if operation == "finish" and text in {"cancelled", "canceled", "cancel"}:
            return True
        return False
    if text in _PROVIDER_FAILURE_WORDS or text in {"false", "0"}:
        return True
    return text.startswith(("error", "failed", "failure", "bad_", "no_"))


def _body_contains_provider_failure(value, operation, key=""):
    """Recursively inspect common JSON provider response shapes."""
    if isinstance(value, bool):
        return value is False if key in {"success", "ok", "result", "response"} else False
    if isinstance(value, dict):
        for raw_key, nested in value.items():
            name = str(raw_key or "").strip().lower().replace("-", "_")
            if name in {"success", "ok"} and isinstance(nested, bool) and not nested:
                return True
            if name in {"status", "result", "response", "code"}:
                if isinstance(nested, (str, int, float, bool)) and _provider_status_is_failure(nested, operation):
                    return True
            if name in {"error", "errors", "failure", "failed", "rejected"}:
                if nested not in (None, False, "", [], {}, 0):
                    return True
            if _body_contains_provider_failure(nested, operation, name):
                return True
        return False
    if isinstance(value, (list, tuple)):
        return any(_body_contains_provider_failure(item, operation, key) for item in value)
    if isinstance(value, str):
        return _provider_status_is_failure(value, operation) if key in {
            "status", "result", "response", "code", "success", "ok"
        } else value.strip().lower() in {"false", "error", "failed", "failure"}
    return False


def _provider_result_is_failure(value, operation):
    """Interpret a provider adapter return value without trusting payloads."""
    if value is None:
        # Existing adapters return ``None`` after validating their response.
        return False
    if isinstance(value, bool):
        return value is False
    if isinstance(value, (int, float)):
        return value == 0
    if isinstance(value, (dict, list, tuple)):
        return _body_contains_provider_failure(value, operation)
    if isinstance(value, str):
        text = value.strip()
        if not text:
            return False
        if text.startswith(("{", "[")):
            try:
                decoded = json.loads(text)
            except (TypeError, ValueError):
                decoded = None
            if decoded is not None:
                return _body_contains_provider_failure(decoded, operation)
        return _provider_status_is_failure(text, operation)
    return False


async def _invoke_provider(fn, *args):
    """Call sync or async provider doubles through one awaitable boundary."""
    value = fn(*args)
    if inspect.isawaitable(value):
        return await value
    return value


async def _await_uncancelled(awaitable):
    """Finish a provider cleanup request even when its caller is cancelled."""
    task = asyncio.ensure_future(awaitable)
    cancelled = False
    while True:
        try:
            return await asyncio.shield(task), cancelled
        except asyncio.CancelledError:
            cancelled = True
            if task.done():
                try:
                    return task.result(), cancelled
                except BaseException as exc:
                    return exc, cancelled


async def _validated_provider_response(response, provider, operation):
    """Raise a normalized error for HTTP/provider-level failures.

    A successful TCP request is not a successful compensation.  Adapters
    must reject non-2xx responses and the common textual/JSON failure forms
    before the local order is marked completed or cancelled.
    """
    try:
        status = int(getattr(response, "status", 0) or 0)
    except (TypeError, ValueError, OverflowError):
        status = 0

    # Keep the structured value intact while checking it.  Several provider
    # clients expose only ``json()`` (or return a dict from it), and converting
    # that value to ``str`` first loses nested ``errors``/``failure`` signals.
    body = None
    text_reader = getattr(response, "text", None)
    if callable(text_reader):
        try:
            body = text_reader()
            if inspect.isawaitable(body):
                body = await body
        except Exception:
            body = None
    if isinstance(body, bytes):
        body = body.decode("utf-8", errors="replace")
    if (body is None or body == ""):
        json_reader = getattr(response, "json", None)
        if callable(json_reader):
            try:
                body = json_reader()
                if inspect.isawaitable(body):
                    body = await body
            except Exception:
                body = None

    operation = str(operation or "").strip().lower()
    if status < 200 or status >= 300:
        raise SmsProviderError(provider, operation, "provider_rejected")

    decoded = body if isinstance(body, (dict, list, tuple, bool, int, float)) else None
    text = str(body or "").strip() if not isinstance(body, (dict, list, tuple)) else ""
    if decoded is None and text.startswith(("{", "[")):
        try:
            decoded = json.loads(text)
        except (TypeError, ValueError):
            decoded = None
    upper = text.upper()
    if upper.startswith(("ERROR", "BAD_", "NO_", "ACCESS_ERROR", "FAILED")):
        raise SmsProviderError(provider, operation, "provider_rejected")
    # Several JSON APIs return HTTP 200 with an explicit unsuccessful status.
    if decoded is not None and _body_contains_provider_failure(decoded, operation):
        raise SmsProviderError(provider, operation, "provider_rejected")
    if decoded is None and re.search(
        r'(?i)["\'](?:status|result|response|success|ok)["\']\s*:\s*'
        r'(?:["\'](?:error|failed|failure|rejected|denied|invalid|false)["\']|false\b)',
        text,
    ):
        raise SmsProviderError(provider, operation, "provider_rejected")
    return text


# ══════════════════════════════════════════════════════════════════════════════
#  PUBLIC API (async)
# ══════════════════════════════════════════════════════════════════════════════

async def get_phone_from_any_service(*, order_store=None, job_id="", attempt_id="",
                                     lease_seconds=180):
    """
    Try to get a phone number from any configured SMS service.
    Returns: {'phone': str, 'id': str, 'service': str} or None
    """
    store = _resolve_order_store(order_store)
    if not has_durable_sms_context(job_id, attempt_id, store):
        raise ValueError("durable SMS attempt context is required")
    services = [
        ("5sim",         Config.FIVESIM_API_KEY,       _get_5sim_phone),
        ("sms_activate", Config.SMS_ACTIVATE_API_KEY,   _get_sms_activate_phone),
        ("onlinesim",    Config.ONLINESIM_API_KEY,      _get_onlinesim_phone),
        ("getsms",       Config.GETSMS_API_KEY,         _get_getsms_phone),
    ]

    for name, api_key, get_fn in services:
        if not api_key:
            continue
        try:
            logger.info(f"Trying SMS service: {name}")
            result = await get_fn()
            if result:
                result['service'] = name
                if store is not None:
                    # Persist the provider allocation before returning the
                    # phone to the registration flow.  A failed local write
                    # must not silently leak a paid provider order.
                    order = None
                    persistence_error = None
                    try:
                        order = store.create_order(
                            name, result.get("id", ""), result.get("phone", ""),
                            job_id=job_id, attempt_id=attempt_id,
                            lease_seconds=lease_seconds,
                        )
                        order = store.activate(order["order_id"])
                    except BaseException as exc:
                        persistence_error = exc

                    if persistence_error is not None:
                        logger.error(
                            "SMS order persistence failed: %s",
                            type(persistence_error).__name__,
                        )
                        provider_order_id = str(result.get("id", ""))
                        cancel_fn = _provider_functions(name).get("cancel")
                        cancel_result = None
                        cancel_error = None
                        cleanup_cancelled = False
                        if cancel_fn and provider_order_id:
                            try:
                                cancel_result, cleanup_cancelled = await _await_uncancelled(
                                    _invoke_provider(cancel_fn, provider_order_id)
                                )
                                if isinstance(cancel_result, BaseException):
                                    cancel_error = cancel_result
                                elif _provider_result_is_failure(cancel_result, "cancel"):
                                    cancel_error = SmsProviderError(
                                        name, "cancel", "provider_rejected"
                                    )
                            except BaseException as exc:
                                # The allocation is already paid for.  Keep
                                # the failure visible instead of allowing the
                                # per-provider loop to silently move on.
                                cancel_error = exc

                        # If the allocation row made it to SQLite, retain a
                        # durable cancellation outcome for the compensation
                        # worker.  A failed activation may leave it allocating.
                        local_cleanup_error = None
                        if order is not None and isinstance(order, dict):
                            try:
                                local_id = order.get("order_id")
                                store.request_cancel(
                                    local_id, error_code="provider_error"
                                )
                                claimed = store.claim_due_compensation(
                                    order_id=local_id, action="cancel"
                                )
                                token = claimed[0].get("compensation_claim_token") \
                                    if claimed else None
                                store.record_compensation(
                                    local_id, action="cancel",
                                    success=cancel_error is None,
                                    error_code=(
                                        getattr(cancel_error, "error_code", "provider_error")
                                        if cancel_error is not None else ""
                                    ),
                                    provider_status=(
                                        "error" if cancel_error is not None else "ok"
                                    ),
                                    claim_token=token,
                                )
                            except BaseException as exc:
                                local_cleanup_error = exc
                                logger.warning(
                                    "SMS order cleanup state could not be persisted"
                                )
                        if cancel_error is not None:
                            logger.warning(
                                "SMS order cleanup after persistence failure failed: %s",
                                type(cancel_error).__name__,
                            )
                        if isinstance(persistence_error, asyncio.CancelledError) or cleanup_cancelled:
                            raise asyncio.CancelledError()
                        if cancel_error is not None or local_cleanup_error is not None:
                            cause = local_cleanup_error or cancel_error or persistence_error
                            raise SmsProviderError(name, "allocate", "provider_error") from cause
                        if not cancel_fn or not provider_order_id:
                            # There is no provider acknowledgement to prove
                            # that the paid allocation was released.
                            raise SmsProviderError(
                                name, "allocate", "provider_error"
                            ) from persistence_error
                        continue
                    result["local_order_id"] = order["order_id"]
                logger.info("Got phone from %s", name)
                return result
        except asyncio.CancelledError:
            raise
        except SmsProviderError:
            raise
        except Exception as e:
            logger.warning("%s failed: %s", name, type(e).__name__)

    logger.error("No SMS service available or all failed.")
    return None


async def get_code_from_service(service_name: str, order_id: str,
                                wait_time: int = MAX_POLL_SECONDS,
                                *, order_store=None, local_order_id=None):
    """
    Wait for and retrieve the SMS verification code.
    Returns: str (code) or None
    """
    poll_fn = {
        '5sim':         _poll_5sim_code,
        'sms_activate': _poll_sms_activate_code,
        'onlinesim':    _poll_onlinesim_code,
        'getsms':       _poll_getsms_code,
    }.get(service_name)

    if not poll_fn:
        logger.error("Unknown SMS service")
        return None

    store = _resolve_order_store(order_store)
    order = _local_order(store, service_name, order_id, local_order_id)
    if order is not None:
        try:
            store.mark_awaiting_code(order["order_id"])
        except ValueError:
            # A terminal order is already reconciled; do not poll it again.
            if order.get("state") in ("completed", "cancelled", "compensation_failed"):
                return None
        except Exception as exc:
            raise SmsProviderError(service_name, "poll", "provider_error") from exc

    def _persist_poll_cancel(error_code: str):
        """Best-effort local fencing that never hides the original outcome."""
        failures = []
        if order is None:
            return failures
        try:
            store.mark_polled(order["order_id"])
        except BaseException as exc:
            failures.append(exc)
        try:
            store.request_cancel(order["order_id"], error_code=error_code)
        except BaseException as exc:
            failures.append(exc)
        return failures

    try:
        code = await poll_fn(order_id, wait_time)
    except asyncio.CancelledError:
        failures = _persist_poll_cancel("provider_timeout")
        if failures:
            logger.error(
                "SMS poll cancellation state persistence failed: %s",
                type(failures[0]).__name__,
            )
        raise
    except Exception as exc:
        failures = _persist_poll_cancel("provider_error")
        if failures:
            logger.error(
                "SMS poll failure state persistence failed: %s",
                type(failures[0]).__name__,
            )
        logger.warning("%s poll failed: %s", service_name, type(exc).__name__)
        raise SmsProviderError(service_name, "poll") from exc
    if order is not None:
        try:
            store.mark_polled(order["order_id"])
            if code:
                store.mark_code_received(order["order_id"])
            else:
                store.request_cancel(order["order_id"], error_code="sms_timeout")
        except BaseException as exc:
            # Never return an OTP that is not represented by a durable order
            # state.  Try to queue cancellation even when the first local
            # write (poll count or code transition) failed.
            failures = []
            if code:
                try:
                    store.request_cancel(order["order_id"], error_code="provider_error")
                except BaseException as cleanup_error:
                    failures.append(cleanup_error)
            if failures:
                logger.error(
                    "SMS poll result cleanup persistence failed: %s",
                    type(failures[0]).__name__,
                )
            raise SmsProviderError(service_name, "poll", "provider_error") from exc
    return code


async def cancel_order(service_name: str, order_id: str, *, order_store=None,
                       local_order_id=None, max_attempts=3, backoff_seconds=30):
    """Cancel an SMS order and durably record success or retry state."""
    service_name = str(service_name or "").strip().lower()
    store = _resolve_order_store(order_store)
    order = _local_order(store, service_name, order_id, local_order_id)
    if order is not None:
        if order.get("state") == "cancelled":
            return {"ok": True, "state": "cancelled", "local_order_id": order["order_id"]}
        if order.get("state") == "completed":
            return {"ok": True, "state": "completed", "local_order_id": order["order_id"]}
        store.request_cancel(order["order_id"])
        claimed = store.claim_due_compensation(
            order_id=order["order_id"], action="cancel",
        )
        if not claimed:
            current = store.get_order(order["order_id"])
            return {
                "ok": False,
                "state": (current or order).get("state", "cancel_pending"),
                "error_code": "compensation_claimed",
                "local_order_id": order["order_id"],
            }
        order = claimed[0]
        claim_token = order.get("compensation_claim_token")
    else:
        claim_token = None
    cancel_fn = _provider_functions(service_name).get("cancel")
    if cancel_fn is None:
        error = SmsProviderError(service_name, "cancel", "provider_rejected")
        if order is not None:
            store.record_compensation(
                order["order_id"], action="cancel", success=False,
                error_code=error.error_code, max_attempts=max_attempts,
                backoff_seconds=backoff_seconds, claim_token=claim_token,
            )
        raise error
    try:
            provider_result = await cancel_fn(order_id)
            if _provider_result_is_failure(provider_result, "cancel"):
                raise SmsProviderError(service_name, "cancel", "provider_rejected")
    except asyncio.CancelledError:
        # Cancellation can arrive after the claim but before the provider
        # request completes.  Release the fence and leave a durable retryable
        # state before propagating cancellation to the caller.
        if order is not None:
            try:
                store.record_compensation(
                    order["order_id"], action="cancel", success=False,
                    error_code="provider_timeout", provider_status="timeout",
                    max_attempts=max_attempts, backoff_seconds=backoff_seconds,
                    claim_token=claim_token,
                )
            except (KeyError, ValueError, OSError):
                pass
        raise
    except SmsProviderError as exc:
        if order is not None:
            store.record_compensation(
                order["order_id"], action="cancel", success=False,
                error_code=exc.error_code, provider_status="error",
                max_attempts=max_attempts, backoff_seconds=backoff_seconds,
                claim_token=claim_token,
            )
        raise
    except (asyncio.TimeoutError, TimeoutError) as exc:
        if order is not None:
            store.record_compensation(
                order["order_id"], action="cancel", success=False,
                error_code="provider_timeout", provider_status="timeout",
                max_attempts=max_attempts, backoff_seconds=backoff_seconds,
                claim_token=claim_token,
            )
        logger.warning("SMS cancel timed out on %s: %s", service_name, type(exc).__name__)
        raise SmsProviderError(service_name, "cancel", "provider_timeout") from exc
    except Exception as exc:
        if order is not None:
            store.record_compensation(
                order["order_id"], action="cancel", success=False,
                error_code="provider_error", max_attempts=max_attempts,
                backoff_seconds=backoff_seconds, claim_token=claim_token,
            )
        logger.warning("Failed to cancel SMS order on %s: %s", service_name, type(exc).__name__)
        raise SmsProviderError(service_name, "cancel") from exc
    if order is not None:
        try:
            saved = store.record_compensation(
                order["order_id"], action="cancel", success=True,
                provider_status="ok", max_attempts=max_attempts,
                backoff_seconds=backoff_seconds, claim_token=claim_token,
            )
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            # The provider request already succeeded.  Keep the local order
            # pending for reconciliation and never expose a generic exception
            # that an outer caller might interpret as permission to retry a
            # different operation.
            logger.error("SMS cancel result persistence failed: %s", type(exc).__name__)
            raise SmsProviderError(service_name, "cancel", "provider_error") from exc
        return {"ok": True, "state": saved["state"], "local_order_id": order["order_id"]}
    return {"ok": True, "state": "cancelled"}


async def finish_order(service_name: str, order_id: str, *, order_store=None,
                       local_order_id=None, max_attempts=3, backoff_seconds=30):
    """Finish an SMS order and durably record the provider outcome."""
    service_name = str(service_name or "").strip().lower()
    store = _resolve_order_store(order_store)
    order = _local_order(store, service_name, order_id, local_order_id)
    if order is not None and order.get("state") == "completed":
        return {"ok": True, "state": "completed", "local_order_id": order["order_id"]}
    if order is not None:
        if order.get("state") == "code_received":
            store.request_finish(order["order_id"])
        claimed = store.claim_due_compensation(
            order_id=order["order_id"], action="finish",
        )
        if not claimed:
            current = store.get_order(order["order_id"])
            return {
                "ok": False,
                "state": (current or order).get("state", "finish_pending"),
                "error_code": "compensation_claimed",
                "local_order_id": order["order_id"],
            }
        order = claimed[0]
        claim_token = order.get("compensation_claim_token")
    else:
        claim_token = None
    finish_fn = _provider_functions(service_name).get("finish")
    if finish_fn is None:
        # Some providers have no explicit finish endpoint; the local order is
        # still closed once verification succeeded.
        if order is not None:
            try:
                saved = store.record_compensation(
                    order["order_id"], action="finish", success=True,
                    provider_status="not_supported", max_attempts=max_attempts,
                    backoff_seconds=backoff_seconds, claim_token=claim_token,
                )
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.error("SMS finish result persistence failed: %s", type(exc).__name__)
                raise SmsProviderError(service_name, "finish", "provider_error") from exc
            return {"ok": True, "state": saved["state"], "local_order_id": order["order_id"]}
        return {"ok": True, "state": "completed"}
    try:
        provider_result = await finish_fn(order_id)
        if _provider_result_is_failure(provider_result, "finish"):
            raise SmsProviderError(service_name, "finish", "provider_rejected")
    except asyncio.CancelledError:
        if order is not None:
            try:
                store.record_compensation(
                    order["order_id"], action="finish", success=False,
                    error_code="provider_timeout", provider_status="timeout",
                    max_attempts=max_attempts, backoff_seconds=backoff_seconds,
                    claim_token=claim_token,
                )
            except (KeyError, ValueError, OSError):
                pass
        raise
    except SmsProviderError as exc:
        if order is not None:
            store.record_compensation(
                order["order_id"], action="finish", success=False,
                error_code=exc.error_code, provider_status="error",
                max_attempts=max_attempts, backoff_seconds=backoff_seconds,
                claim_token=claim_token,
            )
        raise
    except (asyncio.TimeoutError, TimeoutError) as exc:
        if order is not None:
            store.record_compensation(
                order["order_id"], action="finish", success=False,
                error_code="provider_timeout", provider_status="timeout",
                max_attempts=max_attempts, backoff_seconds=backoff_seconds,
                claim_token=claim_token,
            )
        logger.warning("SMS finish timed out on %s: %s", service_name, type(exc).__name__)
        raise SmsProviderError(service_name, "finish", "provider_timeout") from exc
    except Exception as exc:
        if order is not None:
            store.record_compensation(
                order["order_id"], action="finish", success=False,
                error_code="provider_error", max_attempts=max_attempts,
                backoff_seconds=backoff_seconds, claim_token=claim_token,
            )
        logger.warning("Failed to finish SMS order on %s: %s", service_name, type(exc).__name__)
        raise SmsProviderError(service_name, "finish") from exc
    if order is not None:
        try:
            saved = store.record_compensation(
                order["order_id"], action="finish", success=True,
                provider_status="ok", max_attempts=max_attempts,
                backoff_seconds=backoff_seconds, claim_token=claim_token,
            )
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            # Do not call cancel after a successful provider finish.  The
            # durable finish-pending row remains available to the next
            # compensation pass once the claim expires.
            logger.error("SMS finish result persistence failed: %s", type(exc).__name__)
            raise SmsProviderError(service_name, "finish", "provider_error") from exc
        return {"ok": True, "state": saved["state"], "local_order_id": order["order_id"]}
    return {"ok": True, "state": "completed"}


async def reconcile_expired_orders(*, order_store=None, limit=100,
                                   max_attempts=3, backoff_seconds=30,
                                   provider_timeout=30, time_budget_seconds=None,
                                   cancel_event=None):
    """Retry due provider compensation after a worker or lease expires.

    The method is intentionally internal-facing: callers provide the same
    SQLite store used by the registration attempt, while provider responses
    are reduced to success/failure and a short status label.
    """
    store = _resolve_order_store(order_store)
    if store is None:
        return {"claimed": 0, "cancelled": 0, "completed": 0, "failed": 0}
    if (isinstance(provider_timeout, bool) or not isinstance(provider_timeout, (int, float))
            or not math.isfinite(provider_timeout) or not 0 < provider_timeout <= 30):
        raise ValueError("invalid SMS provider timeout")
    if time_budget_seconds is not None and (
        isinstance(time_budget_seconds, bool) or not isinstance(time_budget_seconds, (int, float))
        or not math.isfinite(time_budget_seconds) or not 0 < time_budget_seconds <= 300
    ):
        raise ValueError("invalid SMS compensation time budget")
    deadline = monotonic() + time_budget_seconds if time_budget_seconds is not None else None
    expired = store.claim_expired(limit=limit)
    claimed_ids = {item["order_id"] for item in expired}
    stats = {"claimed": len(claimed_ids), "cancelled": 0, "completed": 0, "failed": 0}
    failure_codes = set()
    attempted_ids = set()

    def claim_lost():
        stats["failed"] += 1
        failure_codes.add("compensation_claim_lost")

    def record_failure(order, error_code, provider_status):
        try:
            saved = store.record_compensation(
                order["order_id"], action=order.get("pending_action") or "cancel",
                success=False, error_code=error_code, provider_status=provider_status,
                max_attempts=max_attempts, backoff_seconds=backoff_seconds,
                claim_token=order.get("compensation_claim_token"),
            )
        except (KeyError, ValueError):
            claim_lost()
            return
        stats["failed"] += 1
        failure_codes.add(
            "compensation_failed" if saved["state"] == "compensation_failed"
            else normalize_error_code(
                saved.get("last_error_code"), default="provider_error", allow_empty=False
            )
        )

    # Only lease work that can start now; a slow serial batch must not consume
    # the leases of orders that have not issued a provider request yet.
    for _ in range(min(limit, 1000)):
        if cancel_event is not None and cancel_event.is_set():
            break
        if deadline is not None and monotonic() >= deadline:
            break
        due = store.claim_due_compensation(limit=1, exclude_order_ids=attempted_ids)
        if not due:
            break
        order = due[0]
        attempted_ids.add(order["order_id"])
        claimed_ids.add(order["order_id"])
        stats["claimed"] = len(claimed_ids)
        action = order.get("pending_action") or "cancel"
        provider = order.get("provider", "")
        provider_order_id = order.get("provider_order_id", "")
        claim_token = order.get("compensation_claim_token")
        try:
            store.assert_compensation_claim(
                order["order_id"], claim_token=claim_token, action=action,
            )
        except (KeyError, ValueError):
            claim_lost()
            continue
        provider_fn = _provider_functions(provider).get(action)
        if provider_fn is None:
            record_failure(order, "provider_rejected", "unsupported")
            continue
        try:
            request_timeout = provider_timeout
            if deadline is not None:
                request_timeout = min(provider_timeout, max(0.001, deadline - monotonic()))
            provider_result = await asyncio.wait_for(
                provider_fn(provider_order_id), timeout=request_timeout,
            )
            if _provider_result_is_failure(provider_result, action):
                raise SmsProviderError(provider, action, "provider_rejected")
        except asyncio.CancelledError:
            try:
                store.record_compensation(
                    order["order_id"], action=action, success=False,
                    error_code="provider_timeout", provider_status="timeout",
                    max_attempts=max_attempts, backoff_seconds=backoff_seconds,
                    claim_token=claim_token,
                )
            except (KeyError, ValueError, OSError):
                pass
            raise
        except SmsProviderError as exc:
            record_failure(order, exc.error_code, "error")
            continue
        except (asyncio.TimeoutError, TimeoutError):
            record_failure(order, "provider_timeout", "timeout")
            continue
        except Exception:
            record_failure(order, "provider_error", "error")
            continue
        try:
            saved = store.record_compensation(
                order["order_id"], action=action, success=True,
                provider_status="ok", max_attempts=max_attempts,
                backoff_seconds=backoff_seconds,
                claim_token=claim_token,
            )
        except (KeyError, ValueError, OSError):
            # Another worker may have renewed/finished the claim.  It is not
            # a provider failure and must not overwrite that worker's state.
            claim_lost()
            continue
        if saved["state"] == "cancelled":
            stats["cancelled"] += 1
        elif saved["state"] == "completed":
            stats["completed"] += 1
    # Backoff must not erase an unresolved failure. Healthy work beyond the
    # batch limit and historical terminal rows are not failures of this pass.
    pending_error = store.pending_compensation_error()
    if pending_error:
        failure_codes.add(pending_error)
    for code in ("compensation_claim_lost", "compensation_failed", "provider_rejected", "provider_timeout", "provider_error"):
        if code in failure_codes:
            stats["error_code"] = code
            break
    return stats


async def check_balance(service_name: str = None):
    """Check balance for a specific service or all configured services."""
    results = {}
    services = {
        '5sim': (Config.FIVESIM_API_KEY, _get_5sim_balance),
        'sms_activate': (Config.SMS_ACTIVATE_API_KEY, _get_sms_activate_balance),
    }

    if service_name:
        key, fn = services.get(service_name, (None, None))
        if key and fn:
            results[service_name] = await fn()
    else:
        for name, (key, fn) in services.items():
            if key:
                try:
                    results[name] = await fn()
                except Exception:
                    results[name] = None

    return results


def format_phone_for_google(phone: str) -> str:
    """
    Format phone number for Google's input field.
    Google expects the number WITH country code but sometimes without '+'.
    """
    phone = phone.strip()
    if phone.startswith('+'):
        return phone
    if len(phone) > 10 and not phone.startswith('+'):
        return '+' + phone
    return phone


# ══════════════════════════════════════════════════════════════════════════════
# 5sim implementation
# ══════════════════════════════════════════════════════════════════════════════

def _5sim_headers():
    return {"Authorization": f"Bearer {Config.FIVESIM_API_KEY}", "Accept": "application/json"}


async def _get_5sim_balance():
    url = "https://5sim.net/v1/user/profile"
    async with aiohttp.ClientSession() as session:
        async with session.get(url, headers=_5sim_headers(), timeout=aiohttp.ClientTimeout(total=10)) as resp:
            if resp.status == 200:
                data = await resp.json()
                balance = data.get("balance", 0)
                logger.info(f"5sim balance: {balance}")
                return balance
    return None


async def _get_5sim_phone():
    balance = await _get_5sim_balance()
    if balance is not None and balance < 1:
        logger.error(f"5sim balance too low: {balance}")
        return None

    country = getattr(Config, 'FIVESIM_COUNTRY', 'usa')
    operator = getattr(Config, 'FIVESIM_OPERATOR', 'any')
    url = f"https://5sim.net/v1/user/buy/activation/{country}/{operator}/google"

    async with aiohttp.ClientSession() as session:
        async with session.get(url, headers=_5sim_headers(), timeout=aiohttp.ClientTimeout(total=15)) as resp:
            if resp.status != 200:
                text = await resp.text()
                logger.error("5sim buy failed (%s)", resp.status)
                return None
            data = await resp.json()

    phone = data.get("phone", "")
    order_id = str(data.get("id", ""))
    if phone and order_id:
        return {"phone": phone, "id": order_id}
    return None


async def _poll_5sim_code(order_id: str, wait_time: int):
    url = f"https://5sim.net/v1/user/check/{order_id}"
    deadline = asyncio.get_running_loop().time() + wait_time

    async with aiohttp.ClientSession() as session:
        while asyncio.get_running_loop().time() < deadline:
            try:
                async with session.get(url, headers=_5sim_headers(), timeout=aiohttp.ClientTimeout(total=10)) as resp:
                    if resp.status == 200:
                        data = await resp.json()
                        status = data.get("status", "")
                        if status == "CANCELED":
                            logger.warning("5sim: order was cancelled")
                            return None
                        sms_list = data.get("sms", [])
                        if sms_list:
                            code = sms_list[0].get("code")
                            if code:
                                logger.info("5sim verification code received")
                                return code
            except Exception as e:
                logger.warning("5sim poll error: %s", type(e).__name__)
            await asyncio.sleep(POLL_INTERVAL)

    logger.warning("5sim: timed out waiting for code")
    return None


async def _cancel_5sim_order(order_id: str):
    url = f"https://5sim.net/v1/user/cancel/{order_id}"
    async with aiohttp.ClientSession() as session:
        async with session.get(url, headers=_5sim_headers(), timeout=aiohttp.ClientTimeout(total=10)) as resp:
            await _validated_provider_response(resp, "5sim", "cancel")
            logger.info("5sim order cancelled")


async def _finish_5sim_order(order_id: str):
    url = f"https://5sim.net/v1/user/finish/{order_id}"
    async with aiohttp.ClientSession() as session:
        async with session.get(url, headers=_5sim_headers(), timeout=aiohttp.ClientTimeout(total=10)) as resp:
            await _validated_provider_response(resp, "5sim", "finish")
            logger.info("5sim order finished")


# ══════════════════════════════════════════════════════════════════════════════
# SMS-Activate implementation
# ══════════════════════════════════════════════════════════════════════════════

async def _get_sms_activate_balance():
    url = "https://api.sms-activate.org/stubs/handler_api.php"
    params = {"api_key": Config.SMS_ACTIVATE_API_KEY, "action": "getBalance"}
    async with aiohttp.ClientSession() as session:
        async with session.get(url, params=params, timeout=aiohttp.ClientTimeout(total=10)) as resp:
            text = (await resp.text()).strip()
            if text.startswith("ACCESS_BALANCE:"):
                balance = float(text.split(":")[1])
                logger.info(f"SMS-Activate balance: {balance}")
                return balance
    return None


async def _get_sms_activate_phone():
    url = "https://api.sms-activate.org/stubs/handler_api.php"
    params = {
        "api_key": Config.SMS_ACTIVATE_API_KEY,
        "action": "getNumber",
        "service": "go",
        "country": Config.SMS_ACTIVATE_COUNTRY,
    }
    async with aiohttp.ClientSession() as session:
        async with session.get(url, params=params, timeout=aiohttp.ClientTimeout(total=15)) as resp:
            text = (await resp.text()).strip()

    if text.startswith("ACCESS_NUMBER"):
        parts = text.split(":")
        if len(parts) >= 3:
            return {"phone": parts[2], "id": parts[1]}
    elif "NO_NUMBERS" in text:
        logger.warning("SMS-Activate: no numbers available")
    elif "NO_BALANCE" in text:
        logger.error("SMS-Activate: insufficient balance")
    else:
        logger.warning("SMS-Activate returned an unexpected response")
    return None


async def _poll_sms_activate_code(order_id: str, wait_time: int):
    url = "https://api.sms-activate.org/stubs/handler_api.php"
    params = {
        "api_key": Config.SMS_ACTIVATE_API_KEY,
        "action": "getStatus",
        "id": order_id,
    }
    deadline = asyncio.get_running_loop().time() + wait_time

    async with aiohttp.ClientSession() as session:
        while asyncio.get_running_loop().time() < deadline:
            try:
                async with session.get(url, params=params, timeout=aiohttp.ClientTimeout(total=10)) as resp:
                    text = (await resp.text()).strip()
                    if text.startswith("STATUS_OK"):
                        code = text.split(":")[1]
                        logger.info("SMS-Activate verification code received")
                        return code
                    elif text == "STATUS_CANCEL":
                        logger.warning("SMS-Activate: order cancelled")
                        return None
            except Exception as e:
                logger.warning("SMS-Activate poll error: %s", type(e).__name__)
            await asyncio.sleep(POLL_INTERVAL)

    logger.warning("SMS-Activate: timed out waiting for code")
    return None


async def _cancel_sms_activate_order(order_id: str):
    url = "https://api.sms-activate.org/stubs/handler_api.php"
    params = {"api_key": Config.SMS_ACTIVATE_API_KEY, "action": "setStatus", "id": order_id, "status": 8}
    async with aiohttp.ClientSession() as session:
        async with session.get(url, params=params, timeout=aiohttp.ClientTimeout(total=10)) as resp:
            text = await _validated_provider_response(resp, "sms_activate", "cancel")
            if text and not text.upper().startswith(("ACCESS_", "STATUS_")):
                raise SmsProviderError("sms_activate", "cancel", "provider_rejected")
            logger.info("SMS-Activate cancel request completed")


async def _finish_sms_activate_order(order_id: str):
    url = "https://api.sms-activate.org/stubs/handler_api.php"
    params = {"api_key": Config.SMS_ACTIVATE_API_KEY, "action": "setStatus", "id": order_id, "status": 6}
    async with aiohttp.ClientSession() as session:
        async with session.get(url, params=params, timeout=aiohttp.ClientTimeout(total=10)) as resp:
            text = await _validated_provider_response(resp, "sms_activate", "finish")
            if text and not text.upper().startswith(("ACCESS_", "STATUS_")):
                raise SmsProviderError("sms_activate", "finish", "provider_rejected")
            logger.info("SMS-Activate finish request completed")


# ══════════════════════════════════════════════════════════════════════════════
# OnlineSIM implementation
# ══════════════════════════════════════════════════════════════════════════════

async def _get_onlinesim_phone():
    url = "https://onlinesim.io/api/getNum.php"
    params = {
        "apikey": Config.ONLINESIM_API_KEY,
        "service": "Google",
        "country": Config.ONLINESIM_COUNTRY,
    }
    async with aiohttp.ClientSession() as session:
        async with session.get(url, params=params, timeout=aiohttp.ClientTimeout(total=15)) as resp:
            data = await resp.json()

        if data.get("response") == 1:
            tzid = str(data.get("tzid", ""))
            if tzid:
                state_url = "https://onlinesim.io/api/getState.php"
                state_params = {"apikey": Config.ONLINESIM_API_KEY, "tzid": tzid}
                for _ in range(10):
                    try:
                        async with session.get(state_url, params=state_params, timeout=aiohttp.ClientTimeout(total=10)) as sr:
                            state_data = await sr.json()
                            if isinstance(state_data, list) and len(state_data) > 0:
                                item = state_data[0]
                                if item.get("number"):
                                    return {"phone": item["number"], "id": tzid}
                    except Exception:
                        pass
                    await asyncio.sleep(3)
        else:
            logger.warning("OnlineSIM getNum returned an unexpected response")
    return None


async def _poll_onlinesim_code(order_id: str, wait_time: int):
    url = "https://onlinesim.io/api/getState.php"
    params = {"apikey": Config.ONLINESIM_API_KEY, "tzid": order_id}
    deadline = asyncio.get_running_loop().time() + wait_time

    async with aiohttp.ClientSession() as session:
        while asyncio.get_running_loop().time() < deadline:
            try:
                async with session.get(url, params=params, timeout=aiohttp.ClientTimeout(total=10)) as resp:
                    data = await resp.json()
                    if isinstance(data, list) and len(data) > 0:
                        msg = data[0].get("msg", "")
                        if msg:
                            code_match = re.search(r'(\d{5,8})', str(msg))
                            if code_match:
                                code = code_match.group(1)
                                logger.info("OnlineSIM verification code received")
                                return code
            except Exception as e:
                logger.warning("OnlineSIM poll error: %s", type(e).__name__)
            await asyncio.sleep(POLL_INTERVAL)

    logger.warning("OnlineSIM: timed out waiting for code")
    return None


async def _cancel_onlinesim_order(order_id: str):
    url = "https://onlinesim.io/api/setOperationRevise.php"
    params = {"apikey": Config.ONLINESIM_API_KEY, "tzid": order_id}
    async with aiohttp.ClientSession() as session:
        async with session.get(url, params=params, timeout=aiohttp.ClientTimeout(total=10)) as resp:
            await _validated_provider_response(resp, "onlinesim", "cancel")
            logger.info("OnlineSIM cancel request completed")


# ══════════════════════════════════════════════════════════════════════════════
# GetSMS implementation
# ══════════════════════════════════════════════════════════════════════════════

async def _get_getsms_phone():
    url = "https://api.getsms.io/stubs/handler_api.php"
    params = {
        "api_key": Config.GETSMS_API_KEY,
        "action": "getNumber",
        "service": "go",
        "country": Config.GETSMS_COUNTRY,
    }
    async with aiohttp.ClientSession() as session:
        async with session.get(url, params=params, timeout=aiohttp.ClientTimeout(total=15)) as resp:
            text = (await resp.text()).strip()

    if text.startswith("ACCESS_NUMBER"):
        parts = text.split(":")
        if len(parts) >= 3:
            return {"phone": parts[2], "id": parts[1]}

    logger.warning("GetSMS getNumber returned an unexpected response")
    return None


async def _poll_getsms_code(order_id: str, wait_time: int):
    url = "https://api.getsms.io/stubs/handler_api.php"
    params = {
        "api_key": Config.GETSMS_API_KEY,
        "action": "getStatus",
        "id": order_id,
    }
    deadline = asyncio.get_running_loop().time() + wait_time

    async with aiohttp.ClientSession() as session:
        while asyncio.get_running_loop().time() < deadline:
            try:
                async with session.get(url, params=params, timeout=aiohttp.ClientTimeout(total=10)) as resp:
                    text = (await resp.text()).strip()
                    if text.startswith("STATUS_OK"):
                        code = text.split(":")[1]
                        logger.info("GetSMS verification code received")
                        return code
                    elif text == "STATUS_CANCEL":
                        return None
            except Exception as e:
                logger.warning("GetSMS poll error: %s", type(e).__name__)
            await asyncio.sleep(POLL_INTERVAL)

    logger.warning("GetSMS: timed out waiting for code")
    return None


async def _cancel_getsms_order(order_id: str):
    url = "https://api.getsms.io/stubs/handler_api.php"
    params = {"api_key": Config.GETSMS_API_KEY, "action": "setStatus", "id": order_id, "status": 8}
    async with aiohttp.ClientSession() as session:
        async with session.get(url, params=params, timeout=aiohttp.ClientTimeout(total=10)) as resp:
            text = await _validated_provider_response(resp, "getsms", "cancel")
            if text and not text.upper().startswith(("ACCESS_", "STATUS_")):
                raise SmsProviderError("getsms", "cancel", "provider_rejected")
            logger.info("GetSMS cancel request completed")


async def _finish_getsms_order(order_id: str):
    url = "https://api.getsms.io/stubs/handler_api.php"
    params = {"api_key": Config.GETSMS_API_KEY, "action": "setStatus", "id": order_id, "status": 6}
    async with aiohttp.ClientSession() as session:
        async with session.get(url, params=params, timeout=aiohttp.ClientTimeout(total=10)) as resp:
            text = await _validated_provider_response(resp, "getsms", "finish")
            if text and not text.upper().startswith(("ACCESS_", "STATUS_")):
                raise SmsProviderError("getsms", "finish", "provider_rejected")
            logger.info("GetSMS finish request completed")
