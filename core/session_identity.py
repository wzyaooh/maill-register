"""Versioned, browser-bound session identity protocol.

This module intentionally contains no browser, HTTP client, cookie, or DOM
transport.  A Playwright/Selenium adapter supplies the complete response from
the fixed provider endpoint; this module validates that response and binds the
result to the Gmail session slot observed in the same browser page.

Provider payloads never leave the parser.  Protocol failures use a finite
exception carrying only a status/error code, while successful calls return
small immutable records and a short-lived proof marked by a private sentinel.
"""

import json
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Dict, Iterable, Optional, Tuple
from urllib.parse import urlsplit


IDENTITY_PROVIDER = "google_browser_accounts_v1"
IDENTITY_PROVIDER_VERSION = 1
IDENTITY_ENDPOINT = (
    "https://accounts.google.com/ListAccounts"
    "?gpsia=1&source=ChromiumBrowser&json=standard"
)
MAX_RESPONSE_BYTES = 262144
MAX_RECORDS = 32
NAVIGATION_TIMEOUT_MS = 15000
MAX_JSON_DEPTH = 16
_XSSI_PREFIX = b")]}'\n"
_PROVIDER_HOST = "accounts.google.com"
_APPLICATION_HOST = "mail.google.com"
_PROVIDER_PATH = "/ListAccounts"
_LOGIN_PATH_PREFIX = "/signin"
_PROVIDER_QUERY = "gpsia=1&source=ChromiumBrowser&json=standard"
_EMAIL = re.compile(r"^[^@\s\x00-\x1f\x7f]+@[^@\s\x00-\x1f\x7f]+\.[^@\s\x00-\x1f\x7f]+$")
_GMAIL_SLOT_PATH = re.compile(r"^/mail/u/([0-9]+)/$")
_PROOF_EVIDENCE_MARKER = object()
_STATUSES = frozenset({
    "authenticated", "login_required", "identity_unavailable",
    "account_mismatch", "cleanup_failed",
})


class _ProtocolViolation(ValueError):
    """Internal parser failure that must not escape with input data."""


class IdentityProtocolError(ValueError):
    """Finite provider/transport protocol failure.

    The exception deliberately stores no URL, response body, JSON object, or
    browser-driver message.  Callers can map ``status``/``code`` directly to
    the browser observation vocabulary.
    """

    def __init__(self, status: str = "identity_unavailable"):
        normalized = status if status in _STATUSES else "identity_unavailable"
        self.status = normalized
        self.code = normalized
        super().__init__(normalized)


def _normalize_email(value: Any) -> str:
    if not isinstance(value, str):
        raise _ProtocolViolation()
    candidate = value.strip().lower()
    if len(candidate) > 320 or not _EMAIL.fullmatch(candidate):
        raise _ProtocolViolation()
    return candidate


def _safe_origin_parts(value: Any, expected_host: str):
    if not isinstance(value, str) or not value or len(value) > 4096:
        raise _ProtocolViolation()
    try:
        parsed = urlsplit(value)
        # Accessing ``port`` forces urllib to reject malformed numeric ports.
        port = parsed.port
    except (TypeError, ValueError):
        raise _ProtocolViolation()
    if (
        parsed.scheme != "https"
        or parsed.hostname != expected_host
        or port is not None
        or parsed.username is not None
        or parsed.password is not None
        or parsed.netloc != expected_host
    ):
        raise _ProtocolViolation()
    return parsed


def _validate_provider_url(final_url: Any) -> str:
    try:
        parsed = _safe_origin_parts(final_url, _PROVIDER_HOST)
    except _ProtocolViolation:
        raise
    path = parsed.path
    if path == _LOGIN_PATH_PREFIX or path.startswith(_LOGIN_PATH_PREFIX + "/"):
        # A provider redirect to the canonical Google sign-in surface is a
        # reliable login fact, even though there is no account payload.
        raise IdentityProtocolError("login_required")
    if path != _PROVIDER_PATH:
        raise _ProtocolViolation()
    # The transport is only allowed to navigate to the fixed endpoint.  An
    # empty query is retained for local fixtures; the production endpoint's
    # exact query is the only other accepted form.
    if parsed.query not in ("", _PROVIDER_QUERY) or parsed.fragment:
        raise _ProtocolViolation()
    return "https://" + _PROVIDER_HOST + _PROVIDER_PATH


def _check_json_depth(body: bytes) -> None:
    depth = 0
    in_string = False
    escaped = False
    for value in body:
        if in_string:
            if escaped:
                escaped = False
            elif value == 0x5C:  # backslash
                escaped = True
            elif value == 0x22:  # quote
                in_string = False
            continue
        if value == 0x22:
            in_string = True
        elif value in (0x7B, 0x5B):  # object/list open
            depth += 1
            if depth > MAX_JSON_DEPTH:
                raise _ProtocolViolation()
        elif value in (0x7D, 0x5D):
            depth -= 1
            if depth < 0:
                raise _ProtocolViolation()
    if in_string or depth != 0:
        raise _ProtocolViolation()


def _reject_duplicate_keys(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise _ProtocolViolation()
        result[key] = value
    return result


def _reject_json_constant(_value):
    raise _ProtocolViolation()


def _parse_json_body(body: bytes) -> Dict[str, Any]:
    if not isinstance(body, bytes):
        raise _ProtocolViolation()
    if len(body) > MAX_RESPONSE_BYTES or not body.startswith(_XSSI_PREFIX):
        raise _ProtocolViolation()
    payload = body[len(_XSSI_PREFIX):]
    _check_json_depth(payload)
    try:
        value = json.loads(
            payload.decode("utf-8"),
            object_pairs_hook=_reject_duplicate_keys,
            parse_constant=_reject_json_constant,
        )
    except (UnicodeDecodeError, json.JSONDecodeError, _ProtocolViolation):
        raise _ProtocolViolation()
    if not isinstance(value, dict) or set(value) != {"accounts"}:
        raise _ProtocolViolation()
    accounts = value.get("accounts")
    if not isinstance(accounts, list) or len(accounts) > MAX_RECORDS:
        raise _ProtocolViolation()
    return value


@dataclass(frozen=True)
class AccountSessionRecord:
    slot: int
    email: str
    valid_session: bool

    def __post_init__(self):
        if isinstance(self.slot, bool) or not isinstance(self.slot, int) or self.slot < 0:
            raise ValueError("invalid account session record")
        normalized = _normalize_email(self.email)
        if normalized != self.email:
            object.__setattr__(self, "email", normalized)
        if type(self.valid_session) is not bool:
            raise ValueError("invalid account session record")


def _records_from_payload(payload: Dict[str, Any]) -> Tuple[AccountSessionRecord, ...]:
    slots = set()
    active_emails = set()
    records = []
    for item in payload["accounts"]:
        if not isinstance(item, dict) or set(item) != {"slot", "email", "valid_session"}:
            raise _ProtocolViolation()
        slot = item["slot"]
        if isinstance(slot, bool) or not isinstance(slot, int) or slot < 0:
            raise _ProtocolViolation()
        email = _normalize_email(item["email"])
        valid_session = item["valid_session"]
        if type(valid_session) is not bool:
            raise _ProtocolViolation()
        if slot in slots:
            raise _ProtocolViolation()
        slots.add(slot)
        if valid_session and email in active_emails:
            raise _ProtocolViolation()
        if valid_session:
            active_emails.add(email)
        records.append(AccountSessionRecord(slot, email, valid_session))
    return tuple(records)


def parse_google_accounts_v1(body: bytes, final_url: str) -> Tuple[AccountSessionRecord, ...]:
    """Parse one complete ``google_browser_accounts_v1`` response.

    A malformed response raises :class:`IdentityProtocolError` with only the
    finite ``identity_unavailable`` code.  A Google sign-in redirect raises
    the same type with ``login_required``.  No raw body is retained.
    """
    try:
        _validate_provider_url(final_url)
        payload = _parse_json_body(body)
        return _records_from_payload(payload)
    except IdentityProtocolError:
        raise
    except (Exception,):
        raise IdentityProtocolError("identity_unavailable")


def _parse_application_url(value: Any):
    try:
        parsed = _safe_origin_parts(value, _APPLICATION_HOST)
    except _ProtocolViolation:
        raise
    if parsed.query:
        raise _ProtocolViolation()
    if parsed.path in ("", "/"):
        slot = None
    else:
        match = _GMAIL_SLOT_PATH.fullmatch(parsed.path)
        if not match:
            raise _ProtocolViolation()
        slot = int(match.group(1))
    return parsed, slot


def parse_gmail_session_slot(url: str) -> Optional[int]:
    """Return the explicit ``/mail/u/<slot>/`` slot or ``None``."""
    try:
        _parsed, slot = _parse_application_url(url)
        return slot
    except (Exception,):
        return None


@dataclass(frozen=True)
class SessionIdentityProof:
    provider: str
    provider_version: int
    session_slot: int
    observed_email: str
    final_origin: str
    collected_at: str
    _evidence_marker: Any = field(default=None, init=False, repr=False, compare=False)


@dataclass(frozen=True)
class IdentityObservation:
    status: str
    proof: Optional[SessionIdentityProof]
    error_code: str

    def __post_init__(self):
        if self.status not in _STATUSES:
            raise ValueError("invalid identity observation")
        if self.error_code not in ("", self.status):
            raise ValueError("invalid identity observation")
        if self.status == "authenticated" and not isinstance(self.proof, SessionIdentityProof):
            raise ValueError("invalid identity observation")
        if self.status != "authenticated" and self.proof is not None:
            raise ValueError("invalid identity observation")


def _observation(status: str) -> IdentityObservation:
    return IdentityObservation(status, None, "" if status == "authenticated" else status)


def _canonical_application_origin(final_origin: str) -> str:
    _parse_application_url(final_origin)
    return "https://" + _APPLICATION_HOST


def _validated_records(records: Iterable[AccountSessionRecord]):
    if isinstance(records, (str, bytes, dict)):
        raise _ProtocolViolation()
    try:
        values = tuple(records)
    except Exception:
        raise _ProtocolViolation()
    if len(values) > MAX_RECORDS:
        raise _ProtocolViolation()
    slots = set()
    active_emails = set()
    for record in values:
        if not isinstance(record, AccountSessionRecord):
            raise _ProtocolViolation()
        if record.slot in slots:
            raise _ProtocolViolation()
        slots.add(record.slot)
        email = _normalize_email(record.email)
        if record.valid_session and email in active_emails:
            raise _ProtocolViolation()
        if record.valid_session:
            active_emails.add(email)
    return values


def _valid_expected_email(value: Any) -> Optional[str]:
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        return _normalize_email(value)
    except _ProtocolViolation:
        return None


def resolve_session_identity(
    records: Iterable[AccountSessionRecord],
    session_slot: Optional[int],
    expected_email: Optional[str],
    manifest_email: Optional[str],
    final_origin: str,
    collected_at: Optional[str] = None,
) -> IdentityObservation:
    """Bind provider records to one Gmail page/session slot.

    Missing/ambiguous protocol facts return ``identity_unavailable``; a
    present, valid slot that belongs to a different mailbox returns
    ``account_mismatch``.  Only this function can mark a proof with the
    process-private evidence sentinel.
    """
    try:
        values = _validated_records(records)
        _parsed, embedded_slot = _parse_application_url(final_origin)
    except _ProtocolViolation:
        return _observation("identity_unavailable")
    if isinstance(session_slot, bool) or not isinstance(session_slot, int) or session_slot < 0:
        return _observation("identity_unavailable")
    if embedded_slot is not None and embedded_slot != session_slot:
        return _observation("account_mismatch")
    expected = _valid_expected_email(expected_email)
    manifest = _valid_expected_email(manifest_email)
    if expected is None or manifest is None:
        return _observation("identity_unavailable")
    if expected != manifest:
        return _observation("account_mismatch")
    matches = [record for record in values if record.slot == session_slot]
    if len(matches) != 1:
        return _observation("identity_unavailable")
    record = matches[0]
    # A provider payload that explicitly says the selected slot is not a
    # valid session is not sufficient to distinguish a sign-in page from a
    # stale/changed provider schema.  Only the transport's canonical
    # ``/signin`` redirect is allowed to produce ``login_required``; this
    # record-level ambiguity stays fail-closed as ``identity_unavailable``.
    if not record.valid_session:
        return _observation("identity_unavailable")
    if record.email != expected or record.email != manifest:
        return _observation("account_mismatch")
    timestamp = collected_at
    if timestamp is None:
        timestamp = datetime.now(timezone.utc).replace(microsecond=0).isoformat()
    if not isinstance(timestamp, str) or not timestamp or len(timestamp) > 128:
        return _observation("identity_unavailable")
    proof = SessionIdentityProof(
        IDENTITY_PROVIDER,
        IDENTITY_PROVIDER_VERSION,
        session_slot,
        record.email,
        _canonical_application_origin(final_origin),
        timestamp,
    )
    object.__setattr__(proof, "_evidence_marker", _PROOF_EVIDENCE_MARKER)
    return IdentityObservation("authenticated", proof, "")


__all__ = [
    "IDENTITY_PROVIDER", "IDENTITY_PROVIDER_VERSION", "IDENTITY_ENDPOINT",
    "MAX_RESPONSE_BYTES", "MAX_RECORDS", "NAVIGATION_TIMEOUT_MS",
    "AccountSessionRecord", "SessionIdentityProof", "IdentityObservation",
    "IdentityProtocolError", "parse_google_accounts_v1",
    "parse_gmail_session_slot", "resolve_session_identity",
]
