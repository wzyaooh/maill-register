"""Small, shared output policy for account and proxy secrets.

Database rows intentionally contain credentials for the registration and IMAP
flows.  This module defines the opposite boundary: fields that are safe to
copy into ordinary diagnostics or metadata exports.  Callers must opt into the
database row when they actually need a credential for an in-memory operation.
"""

import re
from typing import Any, Dict, Iterable, List, Mapping, Sequence


SAFE_ACCOUNT_EXPORT_FIELDS = (
    "id", "email", "first_name", "last_name", "birthday", "gender",
    "strategy", "sms_service", "status", "created_at", "profile_id",
    "engine", "profile_state", "identity_state", "browser_status",
    "mailbox_status", "overall_status", "browser_checked_at",
    "mailbox_checked_at", "last_error_code",
)

# SMS registration/verification codes are shared by the phone adapter, order
# store, retry engine, database projection, and worker API.  Define them once
# so a new durable boundary cannot accidentally accept a different subset.
SMS_ERROR_CODES = frozenset({
    "sms_finish_failed", "sms_poll_failed", "sms_cancel_failed", "sms_error",
    "sms_no_balance", "sms_no_number", "sms_no_phone_input",
    "sms_phone_rejected", "sms_no_code_page", "sms_no_code_input",
    "sms_code_rejected", "sms_import_error", "sms_missing_attempt_context",
    "send_sms_blocked",
    "send_sms_escaped", "all_failed",
})

# ``last_error_code`` is useful in account metadata, but it is populated by
# compatibility paths as well as the typed health/profile protocols.  Keep a
# finite vocabulary at the export boundary instead of copying arbitrary text.
SAFE_ERROR_CODES = frozenset({
    "", "authenticated", "not_configured", "login_required", "challenge",
    "account_mismatch", "profile_conflict", "profile_busy",
    "profile_unavailable", "runtime_unavailable", "runtime_mismatch",
    "proxy_unavailable", "proxy_mismatch", "password_changed", "locked",
    "suspended", "network_error", "error", "unknown", "timeout", "cancelled",
    "activity_failed",
    "cleanup_failed", "browser_cleanup_failed", "database_binding_failed",
    "database_binding_missing", "invalid_profile_id", "unsafe_profile_path",
    "profile_missing", "profile_not_directory", "duplicate_profile_binding",
    "orphaned", "retired", "reconciliation_failed",
    "legacy_unbound", "binding_mismatch", "creation_failed",
    "worker_exception", "worker_error", "configuration_error", "worker_lost",
    "job_failed", "job_finalized", "blocked", "interrupted",
    "phone_required", "qr_blocked", "ip_flagged", "username_taken",
    "browser_crash", "captcha", "unsupported", "provider_rejected",
    "provider_timeout", "provider_error", "order_expired", "cancel_failed",
    "finish_failed", "sms_timeout", "compensation_claimed", "compensation_claim_lost",
    "compensation_failed", "invalid_reconciliation_result", "identity_unavailable",
}) | SMS_ERROR_CODES

# Standard exception class names are retained for compatibility with the
# creation result UI.  This is deliberately finite; arbitrary exception text
# and custom class names are reduced to ``error`` at durable boundaries.
SAFE_EXCEPTION_CODES = frozenset({
    "Exception", "RuntimeError", "ValueError", "TypeError", "KeyError",
    "IndexError", "AttributeError", "LookupError", "OSError", "IOError",
    "TimeoutError", "ConnectionError", "PermissionError", "AssertionError",
    "CancelledError", "KeyboardInterrupt",
})

# Browser registration/verification labels cross logs, account metadata, and
# notifications.  Keep those values finite at every caller boundary instead
# of allowing a provider response or a UI string to become durable text.
SAFE_FLOW_MODES = frozenset({
    "standard", "youtube", "workspace", "mobile_ua", "offline",
})
SAFE_SMS_SERVICES = frozenset({
    "5sim", "sms_activate", "onlinesim", "getsms", "offline",
})
SAFE_VERIFICATION_METHODS = frozenset({
    "skip", "email_instead", "alt_skip", "recovery_email", "js_skip",
    "back_nav", "url_bypass", "keyboard_nav", "session_reset",
    "send_sms_escaped", "send_sms_blocked", "all_failed", "qr_escaped",
    "qr_blocked", "no_verification", "sms_no_balance", "sms_no_number",
    "sms_no_phone_input", "sms_phone_rejected", "sms_no_code_page",
    "sms_timeout", "sms_no_code_input", "sms_code_rejected",
    "sms_finish_failed", "sms_poll_failed", "sms_cancel_failed",
    "sms_import_error", "sms_error", "sms_missing_attempt_context",
    "sms_5sim", "sms_sms_activate",
    "sms_onlinesim", "sms_getsms",
})
_SAFE_CLEANUP_STATUSES = frozenset({"not_started", "completed", "failed"})
_SAFE_REGISTRATION_STATUSES = frozenset({
    "created", "failed", "unverified", "bound", "ready", "skipped",
})
_SAFE_RESULT_COUNTERS = frozenset({
    "attempts", "pages_checked", "actions_completed", "duration_seconds",
})
_LABEL_SECRET_MARKER = re.compile(
    r"(?i)(?:password|passwd|pwd|token|secret|api[_ -]?key|authorization|"
    r"bearer|cookie|session|otp|verification|sms[_ -]?code|private[_ -]?key)"
)
_LABEL_MAX_LENGTH = 128


_SENSITIVE_EXACT = {
    "password", "passwd", "pwd", "pass", "token", "secret",
    "api_key", "authorization", "cookie", "cookies", "proxy",
    "proxy_url", "proxy_server", "proxy_auth", "proxy_credentials",
    "proxy_user", "proxy_username", "proxy_password", "proxy_token",
    "auth_name", "auth_password", "auth_token", "user_id",
    "access_token", "refresh_token", "storage_state", "session_cookie",
    "otp", "verification_code", "one_time_code", "sms_code", "auth_code",
    "otp_code", "cookie_value", "cookie_header", "cookie_jar", "cookie_data",
    "session", "session_id", "session_key", "session_state", "session_data",
    "bearer", "basic", "authorization_header", "authorization_value",
    "x_api_key", "oauth2_token", "oauth2_access_token", "private_key",
    "privatekey", "signing_key", "client_secret", "secret_key",
    "payload", "provider_payload", "provider_response", "response_payload",
    "request_payload", "response_body", "raw_response", "raw_payload",
    "page_content", "dom_snapshot", "raw_html",
}
_SENSITIVE_SUFFIX = re.compile(
    r"(?:^|[_-])(password|passwd|pwd|pass|token|secret|api[_-]?key|"
    r"authorization|credentials?|auth[_-]?(?:name|user|password|token)|"
    r"user[_-]?id|access[_-]?token|refresh[_-]?token|storage[_-]?state|"
    r"cookie(?:s|[_-]?(?:value|header|jar|data))?|"
    r"session[_-]?(?:id|key|state|data|cookie|cookies)|"
    r"otp[_-]?code|bearer|basic|authorization[_-]?(?:header|value)|"
    r"x[_-]?api[_-]?key|oauth2?[_-]?(?:token|access[_-]?token)|"
    r"private[_-]?key|signing[_-]?key|client[_-]?secret|secret[_-]?key)$",
    re.IGNORECASE,
)
_CREDENTIAL_KEY_PART = re.compile(
    r"(?:^|[-_:])(password|passwd|pwd|pass|secret|token|credential|credentials|"
    r"auth|user|username|bearer|basic|private|signing|oauth2?|api[_-]?key)(?:$|[-_:])",
    re.IGNORECASE,
)
_KEY_VALUE_SECRET = re.compile(
    r"(?i)(\b(?:password|passwd|pwd|token|secret|api[_ -]?key|authorization|"
    r"pass|auth[_ -]?(?:name|password|token)|user[_ -]?id|"
    r"otp(?:[_ -]?code)?|verification[_ -]?code|one[_ -]?time[_ -]?code|"
    r"sms[_ -]?code|auth[_ -]?code|"
    r"proxy(?:[_ -]?(?:url|server|credential|credentials|auth|user|password|token))?|"
    r"cookie(?:s)?|session(?:[_ -]?(?:id|key|state|data|cookie|cookies|token))?|"
    r"bearer|basic|authorization[_ -]?(?:header|value)|"
    r"x[_ -]?api[_ -]?key|oauth2?[_ -]?(?:token|access[_ -]?token)|"
    r"private[_ -]?key|signing[_ -]?key|client[_ -]?secret|secret[_ -]?key)\b\s*[:=]\s*)"
    r"(?!\[redacted(?:-[^\]]+)?\])([^\s,;}\]]+)"
)
_URL_USERINFO = re.compile(
    r"(?i)(?<![\w.-])((?:https?|socks[45]?)://)?"
    r"([^\s/@:]+):([^\s/@]+)@([^\s,;}\]]+)"
)
_CANONICAL_PROXY = re.compile(
    r"(?i)(?<![\w.-])(?:[a-z0-9][a-z0-9.-]*|\[[0-9a-f:]+\]):"
    r"\d{1,5}:[^\s:;,}\]]+:[^\s,;}\]]+"
)
_URL_QUERY_SECRET = re.compile(
    r"(?i)([?&](?:sig|signature|hmac|oauth[_-]?signature|"
    r"key|api[_-]?key|access[_-]?key|credential|x-(?:amz|goog)-"
    r"(?:signature|credential))=)([^&#\s,;}\]]+)"
)
_AUTH_SCHEME = re.compile(
    r"(?i)\b(Bearer|Basic)\s+([A-Za-z0-9._~+/=-]+)"
)
_COOKIE_HEADER = re.compile(
    r"(?i)(\b(?:Cookie|Set-Cookie)\s*:\s*)([^\r\n]+)"
)
_EXPLICIT_TOKEN_KEY_VALUE = re.compile(
    r"(?i)(\b(?:access[_-]?token|refresh[_-]?token|id[_-]?token|"
    r"oauth[_-]?token|session[_-]?token|storage[_-]?state|client[_-]?secret|"
    r"jwt|api[_-]?key|oauth2[_-]?token|x[_-]?api[_-]?key|"
    r"authorization[_-]?(?:header|value)|bearer|private[_-]?key|"
    r"signing[_-]?key)\b\s*[:=]\s*)"
    r"(?!\[redacted(?:-[^\]]+)?\])([^\s,;}\]]+)"
)
_JWT_TOKEN = re.compile(
    r"(?i)(?<![A-Za-z0-9_-])eyJ[A-Za-z0-9_-]{5,}\.[A-Za-z0-9_-]{5,}"
    r"\.[A-Za-z0-9_-]{5,}(?![A-Za-z0-9_-])"
)
_AWS_ACCESS_KEY = re.compile(r"\b(?:AKIA|ASIA)[0-9A-Z]{16}\b")


def is_sensitive_key(key: Any) -> bool:
    """Return whether a structured field name may carry a credential."""
    normalized = _normalise_key(key)
    if normalized in _SENSITIVE_EXACT:
        return True
    # Free-form diagnostic keys (for example ``request failed with token``)
    # are values to redact, not schema fields to silently remove.  Restrict
    # suffix matching to identifier-shaped keys so those keys can be retained
    # under a safe marker for operators.
    if not re.fullmatch(r"[A-Za-z0-9_]+", normalized):
        return False
    return bool(_SENSITIVE_SUFFIX.search(normalized))


def _normalise_key(key: Any) -> str:
    """Normalize identifier spellings, including lowerCamelCase fields."""
    raw = str(key).strip()
    # Split acronym-to-word and ordinary lower-to-upper transitions so keys
    # such as ``authToken`` and ``OTPCode`` reach the same policy table.
    raw = re.sub(r"([A-Z]+)([A-Z][a-z])", r"\1_\2", raw)
    raw = re.sub(r"([a-z0-9])([A-Z])", r"\1_\2", raw)
    return raw.lower().replace("-", "_")


def _looks_like_credential_key(key: Any, secrets: Sequence[Any] = ()) -> bool:
    """Reject mapping keys that encode credentials instead of field names."""
    text = str(key).strip()
    if not text:
        return False
    if any(text == str(secret) for secret in secrets if secret is not None and str(secret)):
        return True
    if _URL_USERINFO.search(text) or _CANONICAL_PROXY.search(text):
        return True
    return bool(_CREDENTIAL_KEY_PART.search(text))


def _is_otp_value(key: Any, value: Any) -> bool:
    """Identify an unlabelled numeric one-time code without hiding status codes."""
    normalized = _normalise_key(key)
    if normalized not in {"code", "otp", "verification_code", "one_time_code", "sms_code", "auth_code"}:
        return False
    if normalized != "code":
        return True
    if isinstance(value, bool):
        return False
    if isinstance(value, int):
        return 1000 <= value <= 99_999_999
    return isinstance(value, str) and bool(re.fullmatch(r"\d{4,8}", value.strip()))


def redact_text(value: Any, secrets: Sequence[Any] = ()) -> str:
    """Redact known secret values and common labelled credential forms.

    This is intentionally conservative at output boundaries.  It does not
    alter values used by registration or mailbox operations; it only protects
    text that is about to be persisted or displayed as diagnostics.
    """
    text = "" if value is None else str(value)
    # Structural forms are handled before known values.  This catches a proxy
    # even when its credential is split across stream chunks or is not present
    # in the current secret provider snapshot.
    text = _CANONICAL_PROXY.sub("[redacted-proxy]", text)
    text = _URL_USERINFO.sub("[redacted-url]", text)
    # Signed media/provider URLs carry credentials in query parameters that do
    # not use the usual ``token=``/``api_key=`` labels.  Redact the value even
    # when it is not present in the current secret snapshot.
    text = _URL_QUERY_SECRET.sub(r"\1[redacted]", text)
    # Header and bearer forms are common in copied exception text.  Replace
    # the complete value, including values not present in the current secret
    # provider snapshot.  Cookie headers are treated as an opaque blob because
    # individual cookie names are not useful diagnostics at this boundary.
    text = _COOKIE_HEADER.sub(r"\1[redacted-cookie]", text)
    text = _AUTH_SCHEME.sub(r"\1 [redacted]", text)
    text = _EXPLICIT_TOKEN_KEY_VALUE.sub(r"\1[redacted]", text)
    text = _JWT_TOKEN.sub("[redacted-token]", text)
    text = _AWS_ACCESS_KEY.sub("[redacted-token]", text)
    text = _KEY_VALUE_SECRET.sub(r"\1[redacted]", text)
    # Do not globally replace one-character (or similarly short) values.  A
    # short password such as ``a`` would otherwise corrupt every normal word
    # in a task log.  Labelled fields above still redact those values.
    known = sorted({str(item) for item in secrets if item is not None and str(item)},
                   key=len, reverse=True)
    for secret in known:
        if len(secret) < 4:
            continue
        if len(secret) < 8 and re.fullmatch(r"[A-Za-z0-9]+", secret):
            text = re.sub(
                r"(?<![A-Za-z0-9])" + re.escape(secret) + r"(?![A-Za-z0-9])",
                "[redacted]", text,
            )
        else:
            text = text.replace(secret, "[redacted]")
    return text


def safe_exception_message(exc: Any, secrets: Sequence[Any] = (),
                           fallback: str = "Operation failed",
                           limit: int = 512) -> str:
    """Return a bounded, control-free diagnostic for an exception.

    Exception text is an untrusted output boundary: provider libraries and
    browser drivers frequently include request URLs, headers, or credentials
    in their messages.  Keep a short redacted message for interactive API
    callers, but never allow an exception to expand a task record or inject
    terminal control characters.
    """
    try:
        maximum = max(1, min(int(limit), 4096))
    except (TypeError, ValueError):
        maximum = 512
    try:
        raw = "" if exc is None else str(exc)
    except Exception:
        raw = ""
    try:
        safe = redact_text(raw, secrets)
    except Exception:
        safe = ""
    # Strip ANSI/control characters and collapse multiline provider output so
    # one exception cannot forge additional log or JSON lines.
    safe = re.sub(r"[\x00-\x1f\x7f-\x9f]+", " ", safe)
    safe = re.sub(r"\s+", " ", safe).strip()
    if not safe:
        try:
            safe = redact_text(fallback, secrets)
        except Exception:
            safe = "Operation failed"
        safe = re.sub(r"[\x00-\x1f\x7f-\x9f]+", " ", safe)
        safe = re.sub(r"\s+", " ", safe).strip() or "Operation failed"
    if len(safe) > maximum:
        safe = safe[:maximum].rstrip()
    return safe


def stable_exception_code(exc: Any, prefix: str = "error") -> str:
    """Return a deterministic error code containing only an exception type.

    This is intended for durable worker/ledger state where even a redacted
    exception message is too much information.  Class names are normalized to
    identifier characters so a custom exception cannot inject delimiters or
    control text into the code.
    """
    raw_prefix = str(prefix or "error")
    safe_prefix = re.sub(r"[^A-Za-z0-9_.-]+", "_", raw_prefix).strip("._-") or "error"
    name = getattr(type(exc), "__name__", "Exception") or "Exception"
    safe_name = re.sub(r"[^A-Za-z0-9_.-]+", "_", str(name)).strip("._-") or "Exception"
    return (safe_prefix + ":" + safe_name)[:128]


def normalize_error_code(value: Any, default: str = "error", *, allow_empty: bool = True) -> str:
    """Return one finite, case-normalized machine error code.

    Durable operation records must never contain copied provider payloads or
    arbitrary exception messages.  Known protocol codes (and a deliberately
    small set of standard exception names kept for legacy result displays) are
    retained; everything else becomes ``default``.  Empty values remain empty
    when ``allow_empty`` is true so successful attempts do not acquire a fake
    failure code.
    """
    raw = "" if value is None else str(value).strip()
    if not raw:
        return "" if allow_empty else (str(default or "error").strip().lower() or "error")
    lowered = raw.lower()
    # Canonical protocol codes are all lower-case identifiers.  Match them
    # case-insensitively, but return the canonical spelling from the set.
    for code in SAFE_ERROR_CODES:
        if code and lowered == code.lower():
            return code
    for code in SAFE_EXCEPTION_CODES:
        if raw == code:
            return code
    # Error-code columns are identifiers, never free-form diagnostics.  A
    # value containing delimiters/whitespace (for example
    # ``authorization_header=...`` or a provider URL) is treated as a
    # malformed payload and always collapses to the generic safe code even
    # when the caller's fallback is ``unknown``.
    if not re.fullmatch(r"[A-Za-z][A-Za-z0-9_.-]{0,63}", raw):
        return "error"
    fallback = str(default or "error").strip().lower()
    if fallback in SAFE_ERROR_CODES:
        return fallback
    return "error"


def normalize_flow_mode(value: Any, default: str = "standard") -> str:
    """Return a known registration strategy label.

    Flow mode is used in human-readable progress, account metadata, and
    retry records.  Treat values supplied by compatibility callers as
    untrusted and collapse anything outside the finite strategy vocabulary.
    """
    raw = "" if value is None else str(value)
    candidate = raw.strip().lower()
    candidate = candidate.replace("-", "_")
    if candidate.startswith("selenium_"):
        candidate = candidate[len("selenium_"):]
    elif candidate.startswith("playwright_"):
        candidate = candidate[len("playwright_"):]
    if candidate in SAFE_FLOW_MODES:
        return candidate
    # Metadata crosses process/API/export boundaries, so unknown labels must
    # not become a new implicit protocol.  Legacy callers can still use the
    # explicit ``offline`` enum above; every other value collapses safely.
    fallback = str(default or "standard").strip().lower().replace("-", "_")
    return fallback if fallback in SAFE_FLOW_MODES else "standard"


def normalize_sms_service(value: Any, default: str = "") -> str:
    """Return a provider name only when it is one of the supported services."""
    raw = "" if value is None else str(value)
    candidate = raw.strip().lower()
    candidate = candidate.replace("-", "_")
    if candidate in SAFE_SMS_SERVICES:
        return candidate
    fallback = str(default or "").strip().lower().replace("-", "_")
    return fallback if fallback in SAFE_SMS_SERVICES else ""


def has_durable_sms_context(job_id: Any, attempt_id: Any, order_store: Any) -> bool:
    """Return whether an SMS flow has an owned, persistent attempt context."""
    return (
        isinstance(job_id, str) and bool(job_id.strip())
        and isinstance(attempt_id, str) and bool(attempt_id.strip())
        and order_store is not None
        and order_store is not False
    )


def _safe_metadata_label(value: Any) -> bool:
    """Return whether a compatibility label is bounded and non-credential-like."""
    candidate = "" if value is None else str(value)
    if not candidate or len(candidate) > _LABEL_MAX_LENGTH:
        return False
    if _LABEL_SECRET_MARKER.search(candidate):
        return False
    if re.search(r"(?i)(?:https?|socks[45]?)://|@[^\s]+", candidate):
        return False
    # Preserve CR/LF only for legacy formula-injection tests/exports; other
    # controls are not meaningful metadata and could forge terminal lines.
    if any(ord(char) < 32 and char not in "\r\n\t" for char in candidate):
        return False
    return True


def normalize_verification_method(value: Any, default: str = "unknown") -> str:
    """Return a finite phone/QR verification outcome label."""
    candidate = "" if value is None else str(value).strip().lower()
    candidate = candidate.replace("-", "_")
    if candidate in SAFE_VERIFICATION_METHODS:
        return candidate
    fallback = str(default or "unknown").strip().lower().replace("-", "_")
    return fallback if fallback in SAFE_VERIFICATION_METHODS else "unknown"


def safe_warm_result_summary(value: Any) -> Dict[str, Any]:
    """Project a warmer result onto bounded, machine-readable fields.

    Warmer adapters may include provider messages, URLs, account identifiers,
    or exception text in their compatibility result.  Registration runners
    only need the outcome and cleanup/lease state for a log entry, so discard
    every other field before formatting the result.
    """
    if not isinstance(value, Mapping):
        return {
            "success": False,
            "error_code": "error",
            "cleanup_status": "unknown",
            "browser_process_stopped": False,
            "lease_released": False,
        }
    raw_error = value.get("error_code")
    error_code = normalize_error_code(raw_error, default="error", allow_empty=True)
    raw_cleanup = value.get("cleanup_status")
    cleanup_status = (
        raw_cleanup if isinstance(raw_cleanup, str)
        and raw_cleanup in _SAFE_CLEANUP_STATUSES else "unknown"
    )
    return {
        "success": value.get("success") is True,
        "error_code": error_code,
        "cleanup_status": cleanup_status,
        "browser_process_stopped": value.get("browser_process_stopped") is True,
        "lease_released": value.get("lease_released") is True,
    }


def safe_registration_result_summary(value: Any) -> Dict[str, Any]:
    """Project a registration outcome onto the durable account protocol.

    Registration adapters may return provider messages, exception objects, or
    browser observations alongside the actual outcome.  Account rows only
    need a finite status/error vocabulary and a few bounded counters.  Keep
    this projection deliberately separate from :func:`safe_warm_result_summary`
    so a warm failure can never overwrite a successful registration.
    """
    if not isinstance(value, Mapping):
        return {
            "success": False,
            "status": "failed",
            "error_code": "error",
        }

    error_code = normalize_error_code(
        value.get("error_code") or value.get("error_type"),
        default="error",
        allow_empty=True,
    )
    raw_status = value.get("status")
    status = (
        raw_status if isinstance(raw_status, str)
        and raw_status in _SAFE_REGISTRATION_STATUSES else None
    )
    success = value.get("success") is True or value.get("ok") is True
    if status is None:
        status = "created" if success else "failed"
    if success and error_code:
        # A successful operation with an error marker is contradictory.  Keep
        # the durable projection internally consistent and let callers retain
        # the richer in-memory result if they need it.
        error_code = ""
    result: Dict[str, Any] = {
        "success": success,
        "status": status,
        "error_code": error_code,
    }
    for key in _SAFE_RESULT_COUNTERS:
        raw = value.get(key)
        if isinstance(raw, bool):
            continue
        if isinstance(raw, int) and 0 <= raw <= 1_000_000:
            result[key] = raw
        elif isinstance(raw, float) and 0 <= raw <= 1_000_000:
            # Duration is the only counter where a fractional value is useful.
            if key == "duration_seconds":
                result[key] = round(raw, 3)
    method = value.get("verification_method") or value.get("method")
    if isinstance(method, str):
        normalized_method = normalize_verification_method(method, default="unknown")
        if normalized_method != "unknown":
            result["verification_method"] = normalized_method
    return result


def sanitize_operation_value(value: Any, secrets: Sequence[Any] = (), *, preserve_sensitive: bool = False) -> Any:
    """Remove secret-bearing fields from durable operation metadata.

    Results and progress are public operation records, so a field named
    ``password`` or ``proxy`` is omitted rather than retained with a masked
    value.  Free-form strings remain useful while known or labelled secrets
    are replaced.
    """
    if isinstance(value, Mapping):
        result = {}
        for key, item in value.items():
            if is_sensitive_key(key) or _is_otp_value(key, item):
                if preserve_sensitive:
                    result[key] = "[redacted]"
                continue
            safe_key = "[redacted]" if _looks_like_credential_key(key, secrets) else key
            result[safe_key] = sanitize_operation_value(
                item, secrets, preserve_sensitive=preserve_sensitive
            )
        return result
    if isinstance(value, (list, tuple)):
        items = [sanitize_operation_value(item, secrets, preserve_sensitive=preserve_sensitive)
                 for item in value]
        return tuple(items) if isinstance(value, tuple) else items
    if isinstance(value, str):
        return redact_text(value, secrets)
    return value


def safe_account_metadata(account: Dict[str, Any]) -> Dict[str, Any]:
    """Project one database account row onto non-secret metadata fields."""
    result = {}
    for key in SAFE_ACCOUNT_EXPORT_FIELDS:
        if key not in account:
            continue
        value = account.get(key, "")
        if key == "strategy":
            value = normalize_flow_mode(value)
        elif key == "sms_service":
            value = normalize_sms_service(value)
        if key == "last_error_code":
            # This column is operator metadata, but legacy callers can write
            # arbitrary text there.  Keep only the finite protocol vocabulary
            # so a copied exception or credential cannot become an export.
            value = value if isinstance(value, str) and value in SAFE_ERROR_CODES else ""
        result[key] = value
    return result


def safe_account_metadata_rows(accounts: Iterable[Dict[str, Any]]) -> List[Dict[str, Any]]:
    return [safe_account_metadata(account) for account in accounts]


def safe_proxy_label(proxy: Any) -> str:
    """Return only a proxy scheme/host/port label, never userinfo."""
    try:
        from core.profile_runtime import proxy_launch_config
        config = proxy_launch_config(proxy)
        if config:
            return str(config.get("server") or "[proxy]")
    except Exception:
        pass
    return "[proxy]"


def safe_network_trust_summary(value: Any) -> Dict[str, Any]:
    """Project network-trust diagnostics onto a non-identifying result.

    The trust builder needs the provider-derived IP/location internally for
    its decision, but those facts are not part of a task result, ledger
    summary, or API response.  Preserve only the boolean classification and a
    finite display status; an incomplete provider response remains unknown
    instead of being mistaken for a residential network.
    """
    if not isinstance(value, Mapping):
        return {"status": "unknown", "is_datacenter": False}
    is_datacenter = value.get("is_datacenter")
    if type(is_datacenter) is not bool:
        return {"status": "unknown", "is_datacenter": False}
    if is_datacenter:
        return {"status": "datacenter", "is_datacenter": True}
    # The current provider implementation returns an IP and location when it
    # has a usable classification.  Keep compatibility with adapters that
    # explicitly report an unknown/error result without exposing their fields.
    if str(value.get("ip") or "").strip().lower() in ("", "unknown"):
        return {"status": "unknown", "is_datacenter": False}
    return {"status": "residential", "is_datacenter": False}
