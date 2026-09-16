"""Normalize legacy and structured runner outcomes at orchestration boundaries.

Browser runners historically returned booleans, while newer flows can return
``(success, error_code)`` or a structured mapping. Keeping the coercion in one
place prevents a false tuple from being treated as truthy and gives the job
ledger one finite error-code contract.
"""

from typing import Any, Tuple

from core.secret_safety import (
    normalize_error_code,
    safe_registration_result_summary,
    safe_warm_result_summary,
)


_ALIASES = {
    "phone_required": "phone_required",
    "phone_required_error": "phone_required",
    "phone": "phone_required",
    "qr_blocked": "qr_blocked",
    "qr_code": "qr_blocked",
    "qr": "qr_blocked",
    "ip_flagged": "ip_flagged",
    "timeout": "timeout",
    "time_out": "timeout",
    "browser_crash": "browser_crash",
    "browser_error": "browser_crash",
    "unknown_error": "unknown",
    "unknown": "unknown",
    "error": "error",
}


def _candidate(value: Any) -> Any:
    if isinstance(value, dict):
        return (
            value.get("error_code")
            or value.get("error_type")
            or value.get("last_error_code")
            or value.get("status")
            or ""
        )
    if isinstance(value, (tuple, list)):
        return value[1] if len(value) > 1 else ""
    return ""


def coerce_creation_result(value: Any, *, default_error: str = "unknown") -> Tuple[bool, str]:
    """Return ``(success, finite_error_code)`` for a creation runner result."""
    if isinstance(value, dict):
        success = bool(value.get("success", value.get("ok", False)))
    elif isinstance(value, (tuple, list)):
        success = bool(value[0]) if value else False
    else:
        success = bool(value)
    if success:
        return True, ""

    raw = _candidate(value)
    alias = _ALIASES.get(str(raw or "").strip().lower())
    if alias:
        return False, alias
    return False, normalize_error_code(
        raw, default=default_error, allow_empty=False
    )


def safe_creation_result_metadata(value: Any) -> dict:
    """Keep only safe registration/warm projections for attempt metadata."""
    if not isinstance(value, dict):
        return {}
    metadata = {}
    if value.get("registration_result") is not None:
        metadata["registration_result"] = safe_registration_result_summary(
            value.get("registration_result")
        )
    if value.get("warm_result") is not None:
        warm = value.get("warm_result")
        metadata["warm_result"] = (
            safe_warm_result_summary(warm) if warm else {}
        )
    return metadata


__all__ = ["coerce_creation_result", "safe_creation_result_metadata"]
