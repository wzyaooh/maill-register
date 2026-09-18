"""Durable browser-profile runtime primitives.

This module is deliberately independent from Playwright and Selenium.  It is
the small kernel both adapters use to resolve a profile, validate its binding,
and obtain an exclusive cross-process lease before starting a browser.
"""

import errno
import asyncio
import hashlib
import inspect
import json
import logging
import math
import os
import re
import stat
import tempfile
import time
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, Optional
from urllib.parse import unquote, urlsplit

from core import session_identity as _session_identity
from core.session_identity import (
    IDENTITY_ENDPOINT,
    MAX_RESPONSE_BYTES,
    NAVIGATION_TIMEOUT_MS,
    IdentityObservation,
    IdentityProtocolError,
    parse_google_accounts_v1,
    parse_gmail_session_slot,
    resolve_session_identity,
)

logger = logging.getLogger("gmail_creator_profile_runtime")

try:  # pragma: no cover - Windows is not used by the CI image, but supported.
    import fcntl
except ImportError:  # pragma: no cover
    fcntl = None


ENGINES = ("playwright", "selenium")
PROFILE_STATES = (
    "provisioning", "bound", "ready", "orphaned", "corrupt", "retired",
    "legacy_unbound", "cleanup_failed",
)
BROWSER_STATUSES = (
    "not_configured", "authenticated", "login_required", "challenge",
    "account_mismatch", "profile_conflict", "profile_busy", "runtime_unavailable",
    "runtime_mismatch", "proxy_unavailable", "proxy_mismatch",
    "profile_unavailable", "identity_unavailable", "cleanup_failed", "error",
)
MAILBOX_STATUSES = (
    "not_configured", "active", "password_changed", "locked", "suspended",
    "network_error", "error",
)

_PROFILE_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$")
_HEX64 = re.compile(r"^[0-9a-fA-F]{64}$")
_OBSERVED_EMAIL = re.compile(
    r"(?i)(?<![A-Z0-9._%+-])([A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,63})(?![A-Z0-9._%+-])"
)
_FORBIDDEN_MANIFEST_KEYS = {
    "password", "passwd", "proxy", "cookies", "cookie", "storage_state",
    "access_token", "refresh_token", "secret", "secret_key", "api_key",
    "token", "auth_token", "authorization", "otp", "verification_code",
    "one_time_code", "sms_code", "auth_code", "code", "otp_code",
    "cookie_value", "cookie_header", "cookie_jar", "cookie_data",
    "session", "session_id", "session_key", "session_state", "session_data",
}

# A browser observation is serializable metadata, so the boolean fields in a
# plain mapping are not sufficient proof of a live session.  Kernel-created
# ``BrowserObservation`` instances carry this private process-local marker
# after ``classify_session_auth`` has validated raw cookie facts and selectors.
# It is intentionally never inserted into the mapping itself.
_AUTH_EVIDENCE_TOKEN = object()


def _normalise_manifest_key(key: Any) -> str:
    """Canonicalize JSON field spellings before applying the secret policy."""
    raw = str(key).strip()
    # Handle acronym transitions (``APIKey``) and ordinary lower camel case
    # (``verificationCode``) before comparing against snake_case policy keys.
    raw = re.sub(r"([A-Z]+)([A-Z][a-z])", r"\1_\2", raw)
    raw = re.sub(r"([a-z0-9])([A-Z])", r"\1_\2", raw)
    raw = re.sub(r"[^A-Za-z0-9]+", "_", raw)
    return raw.strip("_").lower()


def _contains_forbidden_manifest_value(value: Any) -> bool:
    if isinstance(value, dict):
        return any(
            _normalise_manifest_key(key) in _FORBIDDEN_MANIFEST_KEYS
            or _contains_forbidden_manifest_value(nested)
            for key, nested in value.items()
        )
    if isinstance(value, list):
        return any(_contains_forbidden_manifest_value(item) for item in value)
    return False


def _utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


class ProfileRuntimeError(RuntimeError):
    """Base error with a stable machine-readable error code."""

    code = "profile_error"

    def __init__(self, message: str = ""):
        super().__init__(message or self.code)


class ProfileConflictError(ProfileRuntimeError):
    code = "profile_conflict"


class ProfileUnavailableError(ProfileRuntimeError):
    code = "profile_unavailable"


class ProfileRuntimeMismatchError(ProfileRuntimeError):
    code = "runtime_mismatch"


class ProfileBusyError(ProfileRuntimeError):
    code = "profile_busy"


class ProxyUnavailableError(ProfileRuntimeError):
    code = "proxy_unavailable"


class ProxyMismatchError(ProfileRuntimeError):
    code = "proxy_mismatch"


@dataclass(frozen=True)
class ProfileHandle:
    profile_id: str
    path: Path
    manifest_path: Path
    lock_path: Path

    @property
    def profile_path(self) -> str:
        """Compatibility representation used by older adapter call sites."""
        return str(self.path)


def build_identity(profile_id: str, engine: str, mobile: bool = False) -> Dict[str, Any]:
    """Build deterministic native browser context values for a profile.

    The values are derived from the immutable profile id.  No launch or probe
    operation may replace them with a fresh random identity.
    """
    if engine not in ENGINES:
        raise ValueError("engine must be playwright or selenium")
    digest = hashlib.sha256((engine + ":" + profile_id).encode("utf-8")).digest()
    widths = (1280, 1366, 1440, 1536, 1600, 1920)
    heights = (720, 768, 900, 864, 900, 1080)
    width = widths[digest[0] % len(widths)]
    height = heights[digest[1] % len(heights)]
    locales = ("en-US", "en-GB", "en-CA", "en-AU")
    timezones = (
        "America/New_York", "America/Chicago", "America/Denver",
        "America/Los_Angeles", "Europe/London", "Australia/Sydney",
    )
    locale = locales[digest[2] % len(locales)]
    timezone_id = timezones[digest[3] % len(timezones)]
    major = 120 + digest[4] % 15
    platform = "Linux x86_64" if engine == "playwright" else "Windows NT 10.0; Win64; x64"
    user_agent = (
        "Mozilla/5.0 (" + platform + ") AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/" + str(major) + ".0.0.0 Safari/537.36"
    )
    hardware = (4, 6, 8, 12)[digest[5] % 4]
    memory = (4, 8, 16)[digest[6] % 3]
    return {
        "user_agent": user_agent,
        "viewport": {"width": width, "height": height},
        "locale": locale,
        "timezone_id": timezone_id,
        "geolocation": {
            "longitude": round(-122.0 + (digest[7] / 255.0) * 80.0, 4),
            "latitude": round(25.0 + (digest[8] / 255.0) * 25.0, 4),
        },
        "is_mobile": bool(mobile),
        "has_touch": bool(mobile),
        "hardware_concurrency": hardware,
        "device_memory": memory,
        "client_hints": {
            "platform": "Android" if mobile else ("Linux" if engine == "playwright" else "Windows"),
            "mobile": mobile,
        },
    }


def _parse_proxy(proxy: Optional[str]) -> Optional[Dict[str, Any]]:
    """Parse every proxy spelling accepted by the application.

    The returned object contains credentials only in memory.  Callers that
    persist a binding must use :func:`proxy_binding`, which stores only the
    endpoint hash.  ``None`` means a non-empty value was malformed and must
    never be interpreted as a direct connection.
    """
    if proxy is None:
        return None
    value = str(proxy).strip()
    if not value:
        return None

    scheme = "http"
    host = port = username = password = None
    if "://" in value:
        try:
            parsed = urlsplit(value)
            scheme = (parsed.scheme or "http").lower()
            host = parsed.hostname
            port = parsed.port
            username = unquote(parsed.username) if parsed.username is not None else None
            password = unquote(parsed.password) if parsed.password is not None else None
        except (TypeError, ValueError):
            return None
    elif "@" in value:
        # user:pass@host:port
        auth, hostport = value.rsplit("@", 1)
        if ":" not in auth or ":" not in hostport:
            return None
        username, password = auth.split(":", 1)
        host, port = hostport.rsplit(":", 1)
    else:
        # host:port:user:pass (the proxy-manager's canonical form) or
        # host:port for an unauthenticated endpoint.
        parts = value.split(":")
        if len(parts) == 4:
            host, port, username, password = parts
        elif len(parts) == 2:
            host, port = parts
        else:
            return None

    if not host or port in (None, ""):
        return None
    try:
        port_number = int(port)
    except (TypeError, ValueError):
        return None
    if not 1 <= port_number <= 65535:
        return None
    host = str(host).strip().lower()
    if not host or any(ch.isspace() for ch in host):
        return None
    return {
        "scheme": scheme,
        "host": host,
        "port": port_number,
        "username": username,
        "password": password,
    }


def proxy_launch_config(proxy: Optional[str]) -> Optional[Dict[str, Any]]:
    """Return an adapter-ready proxy config without persisting its secrets."""
    parsed = _parse_proxy(proxy)
    if parsed is None:
        return None
    host = parsed["host"]
    if ":" in host and not host.startswith("["):
        host = "[" + host + "]"
    config = {"server": "%s://%s:%s" % (
        parsed["scheme"], host, parsed["port"]
    )}
    if parsed.get("username") is not None:
        config["username"] = parsed["username"]
    if parsed.get("password") is not None:
        config["password"] = parsed["password"]
    return config


def _proxy_endpoint(proxy: Optional[str]) -> Optional[str]:
    parsed = _parse_proxy(proxy)
    if parsed is None:
        return None
    host = parsed["host"]
    if ":" in host and not host.startswith("["):
        host = "[" + host + "]"
    return "%s://%s:%s" % (parsed["scheme"], host, parsed["port"])


def proxy_binding(proxy: Optional[str]) -> Dict[str, Any]:
    """Return a non-secret binding for a proxy endpoint."""
    endpoint = _proxy_endpoint(proxy)
    if endpoint is None:
        return {"bound": False, "endpoint_hash": "", "source": ""}
    return {
        "bound": True,
        "endpoint_hash": hashlib.sha256(endpoint.encode("utf-8")).hexdigest(),
        "source": "configured",
    }


def _normalise_browser(browser: Optional[Dict[str, Any]]) -> Dict[str, str]:
    browser = browser or {}
    # ``None`` means no observation was supplied and retains the historical
    # bundled-Chromium default.  An explicit empty channel is different: an
    # adopted legacy profile has an unknown runtime and must allow the first
    # real adapter observation to establish it.
    channel = "chromium" if "channel" not in browser else str(browser.get("channel") or "")
    major = str(browser.get("major_version") or "")
    return {"channel": channel, "major_version": major}


def validate_profile_identity(identity: Optional[Dict[str, Any]]) -> None:
    """Fail closed when a persisted profile identity is incomplete.

    Warm and health launches must consume the identity captured at registration;
    silently filling a missing value (especially with a random user agent) would
    create a different browser persona while reusing the same profile files.
    ``ProfileConflictError`` is used here because an adapter cannot safely
    launch a profile whose immutable binding has been tampered with.
    """
    if not isinstance(identity, dict):
        raise ProfileConflictError("profile identity is not an object")

    user_agent = identity.get("user_agent")
    if not isinstance(user_agent, str) or not user_agent.strip():
        raise ProfileConflictError("profile identity user_agent is missing")

    viewport = identity.get("viewport")
    if not isinstance(viewport, dict):
        raise ProfileConflictError("profile identity viewport is missing")
    for key in ("width", "height"):
        value = viewport.get(key)
        if type(value) is not int or value <= 0:
            raise ProfileConflictError("profile identity viewport is invalid")

    for key in ("locale", "timezone_id"):
        value = identity.get(key)
        if not isinstance(value, str) or not value.strip():
            raise ProfileConflictError("profile identity %s is missing" % key)

    geolocation = identity.get("geolocation")
    if not isinstance(geolocation, dict):
        raise ProfileConflictError("profile identity geolocation is missing")
    for key in ("longitude", "latitude"):
        value = geolocation.get(key)
        try:
            finite = math.isfinite(float(value))
        except (TypeError, ValueError, OverflowError):
            finite = False
        if isinstance(value, bool) or not isinstance(value, (int, float)) or not finite:
            raise ProfileConflictError("profile identity geolocation is invalid")

    for key in ("is_mobile", "has_touch"):
        if type(identity.get(key)) is not bool:
            raise ProfileConflictError("profile identity %s is invalid" % key)
    for key in ("hardware_concurrency", "device_memory"):
        value = identity.get(key)
        if type(value) is not int or value <= 0:
            raise ProfileConflictError("profile identity %s is invalid" % key)

    client_hints = identity.get("client_hints")
    if not isinstance(client_hints, dict):
        raise ProfileConflictError("profile identity client_hints is missing")
    platform = client_hints.get("platform")
    if not isinstance(platform, str) or not platform.strip() or \
            type(client_hints.get("mobile")) is not bool:
        raise ProfileConflictError("profile identity client_hints is invalid")
    if client_hints.get("mobile") != identity.get("is_mobile"):
        raise ProfileConflictError("profile identity mobile hint conflicts")


def classify_browser_observation(observation: Optional[Dict[str, Any]], expected_email: Optional[str] = None,
                                 *, _identity_bound: Optional[bool] = None,
                                 _evidence_token: Any = None) -> str:
    """Map adapter observations to the shared browser-status vocabulary.

    ``authenticated`` is a protocol claim, not a free-form adapter hint.  A
    caller must provide a trusted HTTPS Google origin, a bound identity, and a
    concrete session/application signal.  This prevents local fixtures,
    generic ``authenticated=True`` flags, and stale cookie names from being
    promoted to a successful health result.
    """
    item = observation if isinstance(observation, dict) else {}
    observed = item.get("observed_email") or item.get("account_email")
    expected = normalise_observed_email(expected_email)
    observed_text = normalise_observed_email(observed)

    # Identity mismatch always wins over positive or generic status fields.
    if expected and observed_text and observed_text != expected:
        return "account_mismatch"
    if item.get("account_mismatch") is True or item.get("identity_confidence") == "mismatch":
        return "account_mismatch"

    code = str(item.get("code") or "").strip().lower()
    # Negative protocol states must be evaluated before any positive signal;
    # a page can retain an old cookie while currently showing a challenge or
    # sign-in form.
    if item.get("challenge") is True or item.get("captcha") is True or code == "challenge":
        return "challenge"
    if item.get("login_required") is True or item.get("signin") is True or code == "login_required":
        return "login_required"

    # Runtime/profile/proxy errors are explicit and should not be masked by a
    # stale ``authenticated`` field.
    for status in (
        "profile_busy", "proxy_unavailable", "proxy_mismatch",
        "runtime_mismatch", "runtime_unavailable", "profile_conflict",
    ):
        if item.get(status) is True or code == status:
            return status
    if item.get("not_configured") is True or code == "not_configured":
        return "not_configured"
    if code in BROWSER_STATUSES and code != "authenticated":
        return code

    origin = item.get("origin") or item.get("final_url") or ""
    trusted = _trusted_origin(origin)

    identity_state = str(
        item.get("identity_state") or item.get("profile_identity_state") or ""
    ).strip().lower()
    # Reconstructed/adopted profiles cannot rely on a durable
    # ``identity_verified`` bit alone.  Every probe must observe the account
    # identity again and match the expected mailbox; otherwise a stale cookie
    # and a previously verified manifest could produce a false positive.
    reconstructed = identity_state in {
        "identity_reconstructed", "legacy_unbound", "reconstructed",
    }
    fresh_identity = bool(
        observed_text and (not expected or observed_text == expected)
    )
    if reconstructed:
        # A projected observation does not carry the full manifest.  Accept a
        # reconstructed identity only when the kernel supplied its verified
        # binding marker (or the caller explicitly supplied one) *and* this
        # session observed the matching mailbox address.
        identity_bound = bool(
            fresh_identity
            and (
                item.get("identity_bound") is True
                or _identity_bound is True
                or item.get("identity_verified") is True
            )
        )
    else:
        identity_bound = bool(
            expected and observed_text == expected
            or identity_state == "native"
            or item.get("identity_bound") is True
            or _identity_bound is True
        )

    # Only a validated cookie or an explicitly detected application shell is
    # usable as session evidence.  If raw cookies are supplied, validate them
    # against the same origin/domain/expiry rules used by classify_session_auth.
    # A raw cookie list can be checked directly.  A projected observation does
    # not retain cookie values; it is accepted only when the kernel attached
    # its private evidence marker and the corresponding positive fields are
    # still unchanged.  Cookie names, confidence labels, and generic flags
    # are never evidence on their own.
    evidence_token = _evidence_token
    if evidence_token is None:
        evidence_token = getattr(observation, "_evidence_token", None)
    trusted_session = evidence_token is _AUTH_EVIDENCE_TOKEN
    cookie_evidence = False
    if item.get("cookies") is not None:
        cookie_evidence = _valid_auth_cookie(item.get("cookies"), origin)
    elif trusted_session:
        cookie_evidence = bool(
            item.get("auth_cookie") is True and item.get("authenticated") is True
        )
    application_evidence = (
        item.get("application_shell") is True
    )
    positive_hint = bool(item.get("authenticated") is True or item.get("auth_cookie") is True
                         or item.get("application_signal") is True
                         or item.get("application_shell") is True
                         or code == "authenticated")
    if trusted and identity_bound and cookie_evidence and application_evidence:
        return "authenticated"
    if positive_hint:
        return "login_required"
    return "error"


def derive_overall_status(browser_status: str, mailbox_status: str) -> str:
    """Derive the compatibility status without overwriting either channel."""
    browser_status = browser_status or "error"
    mailbox_status = mailbox_status or "error"
    if mailbox_status == "suspended":
        return "suspended"
    if mailbox_status == "password_changed":
        return "password_changed"
    if browser_status in (
        "profile_busy", "account_mismatch", "profile_conflict",
        "runtime_mismatch", "proxy_mismatch", "proxy_unavailable",
        "profile_unavailable", "cleanup_failed",
        "identity_unavailable",
    ):
        # These are profile/runtime facts, not evidence that the mailbox is
        # locked.  Preserve them as degraded even when IMAP reports a generic
        # web-login lock so callers cannot mistake a binding problem for an
        # account suspension/lock.
        return "degraded"
    if browser_status == "challenge":
        if mailbox_status == "locked":
            return "locked"
        return "degraded"
    if browser_status == "authenticated" and mailbox_status == "active":
        return "active"
    # Legacy/no-profile accounts have no browser fact; IMAP remains authoritative
    # enough to preserve the historical active projection.
    if browser_status == "not_configured" and mailbox_status == "active":
        return "active"
    if browser_status in ("not_configured", "login_required", "runtime_unavailable", "error") and mailbox_status == "locked":
        return "locked"
    if mailbox_status == "active" and browser_status not in ("error", "runtime_unavailable"):
        return "degraded"
    if mailbox_status == "network_error" and browser_status in ("not_configured", "runtime_unavailable", "error"):
        return "network_error"
    if browser_status in ("not_configured", "runtime_unavailable", "error") and mailbox_status in ("not_configured", "error"):
        return "error"
    return "degraded"


@dataclass
class ProfileLease:
    handle: ProfileHandle
    operation: str
    engine: str
    timeout: float = 0
    _stream: Any = None
    _held: bool = False
    _profile_fd: Any = None
    _profile_identity: Any = None
    _release_verified: bool = False
    _release_error: Optional[str] = None

    def _pin_profile_directory(self) -> None:
        """Pin the directory inode that the browser is allowed to use.

        Locking ``profile.lock`` alone is insufficient: the containing
        directory can be renamed and replaced while the lock-file descriptor
        remains held.  Keep a directory descriptor where the platform permits
        it and retain its device/inode identity for later path checks.
        """
        profile_path = Path(self.handle.path)
        try:
            info = os.lstat(str(profile_path))
        except OSError as exc:
            raise ProfileUnavailableError("profile directory is missing") from exc
        if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
            raise ProfileConflictError("profile directory is not a stable directory")

        flags = os.O_RDONLY
        flags |= getattr(os, "O_NOFOLLOW", 0)
        flags |= getattr(os, "O_DIRECTORY", 0)
        try:
            self._profile_fd = os.open(str(profile_path), flags)
            pinned = os.fstat(self._profile_fd)
            self._profile_identity = (pinned.st_dev, pinned.st_ino)
        except OSError as exc:
            if self._profile_fd is not None:
                try:
                    os.close(self._profile_fd)
                except OSError:
                    pass
                self._profile_fd = None
            # Windows does not expose a useful directory fd for this purpose;
            # retain the lstat identity and perform the same path check below.
            if os.name == "nt":  # pragma: no cover - Windows CI is unavailable.
                self._profile_identity = (info.st_dev, info.st_ino)
                return
            raise ProfileUnavailableError("profile directory cannot be opened safely") from exc

    def _assert_stable_unlocked(self) -> None:
        profile_path = Path(self.handle.path)
        try:
            current = os.lstat(str(profile_path))
        except OSError as exc:
            raise ProfileConflictError("profile directory changed while lease was held") from exc
        if stat.S_ISLNK(current.st_mode) or not stat.S_ISDIR(current.st_mode):
            raise ProfileConflictError("profile directory changed while lease was held")
        current_identity = (current.st_dev, current.st_ino)
        if self._profile_identity is not None and current_identity != self._profile_identity:
            raise ProfileConflictError("profile directory changed while lease was held")
        if self._profile_fd is not None:
            try:
                pinned = os.fstat(self._profile_fd)
            except OSError as exc:
                raise ProfileConflictError("profile directory descriptor is no longer valid") from exc
            if (pinned.st_dev, pinned.st_ino) != current_identity:
                raise ProfileConflictError("profile directory changed while lease was held")

        # The lock path must remain the same regular file whose descriptor is
        # held.  This catches replacement of the lock entry independently of
        # the profile directory check.
        lock_path = Path(self.handle.lock_path)
        if self._stream is None:
            return
        try:
            lock_info = os.lstat(str(lock_path))
            if stat.S_ISLNK(lock_info.st_mode) or not stat.S_ISREG(lock_info.st_mode):
                raise ProfileConflictError("profile lock path changed while lease was held")
            held_info = os.fstat(self._stream.fileno())
            if (held_info.st_dev, held_info.st_ino) != (lock_info.st_dev, lock_info.st_ino):
                raise ProfileConflictError("profile lock path changed while lease was held")
        except ProfileRuntimeError:
            raise
        except OSError as exc:
            raise ProfileConflictError("profile lock path changed while lease was held") from exc

    def assert_stable(self) -> "ProfileLease":
        """Verify that the held lease still points at the original profile."""
        if not self._held or self._stream is None:
            raise ProfileConflictError("profile lease is not held")
        self._assert_stable_unlocked()
        return self

    def acquire(self) -> "ProfileLease":
        if self._held:
            return self
        self._release_verified = False
        self._release_error = None
        lock_path = Path(self.handle.lock_path)
        if lock_path.is_symlink() or Path(self.handle.path).is_symlink():
            raise ProfileConflictError("profile lock path cannot be a symlink")
        try:
            self._pin_profile_directory()
            # Re-check before touching the lock entry.  On POSIX the lock is
            # opened relative to the pinned directory fd below, so a rename of
            # the pathname cannot redirect the lock to another tree.
            self._assert_stable_unlocked()
            flags = os.O_RDWR | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0)
            try:
                if self._profile_fd is not None and os.open in getattr(os, "supports_dir_fd", ()):
                    fd = os.open("profile.lock", flags, 0o600, dir_fd=self._profile_fd)
                else:
                    fd = os.open(str(self.handle.lock_path), flags, 0o600)
            except OSError as exc:
                if getattr(exc, "errno", None) == errno.ELOOP:
                    raise ProfileConflictError("profile lock path cannot be a symlink") from exc
                raise
            try:
                self._stream = os.fdopen(fd, "a+", encoding="utf-8")
            except Exception:
                os.close(fd)
                raise
            try:
                os.fchmod(self._stream.fileno(), 0o600)
            except OSError:
                os.chmod(self.handle.lock_path, 0o600)
            deadline = time.monotonic() + max(0, float(self.timeout))
            while True:
                try:
                    if fcntl is not None:
                        fcntl.flock(self._stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                    else:  # pragma: no cover - exercised on Windows.
                        import msvcrt
                        self._stream.seek(0)
                        msvcrt.locking(self._stream.fileno(), msvcrt.LK_NBLCK, 1)
                    break
                except (BlockingIOError, OSError) as exc:
                    busy = isinstance(exc, BlockingIOError) or getattr(exc, "errno", None) in (errno.EACCES, errno.EAGAIN)
                    if not busy or time.monotonic() >= deadline:
                        raise ProfileBusyError("profile lease is already held")
                    time.sleep(0.05)
            self._assert_stable_unlocked()
            self._stream.seek(0)
            self._stream.truncate()
            self._stream.write(json.dumps({
                "pid": os.getpid(), "operation": self.operation,
                "engine": self.engine, "acquired_at": _utc_now(),
            }, sort_keys=True))
            self._stream.flush()
            os.fsync(self._stream.fileno())
            self._held = True
            return self
        except Exception:
            self._close_stream()
            raise

    def _close_stream(self) -> bool:
        stream = self._stream
        success = True
        if stream is not None:
            try:
                if fcntl is not None:
                    fcntl.flock(stream.fileno(), fcntl.LOCK_UN)
                elif os.name == "nt":  # pragma: no cover
                    import msvcrt
                    stream.seek(0)
                    msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
            except Exception:
                success = False
            try:
                stream.close()
            except Exception:
                success = False
            else:
                # A closed descriptor cannot be retried.  Keep the reference
                # only when close itself failed so a caller may retry.
                self._stream = None
        profile_fd = self._profile_fd
        if profile_fd is not None:
            try:
                os.close(profile_fd)
            except OSError:
                success = False
            else:
                self._profile_fd = None
        if success and self._stream is None and self._profile_fd is None:
            self._held = False
            self._release_verified = True
            self._release_error = None
            self._profile_identity = None
            return True
        # Do not claim that a lease is gone when an unlock/close operation was
        # not confirmed.  The profile will be quarantined by the caller.
        self._held = True
        self._release_verified = False
        self._release_error = "lease_release_failed"
        return False

    def release(self) -> bool:
        if self._stream is not None or self._profile_fd is not None:
            return self._close_stream()
        return bool(self._release_verified and not self._held)

    @property
    def held(self) -> bool:
        """Expose lease ownership without requiring callers to inspect fields."""
        return bool(self._held)

    @property
    def release_verified(self) -> bool:
        """Whether the last release closed every owned descriptor."""
        return bool(self._release_verified and not self._held)

    def __enter__(self) -> "ProfileLease":
        return self.acquire()

    def __exit__(self, *_args: Any) -> None:
        self.release()


class ProfileRuntime:
    """Resolve, persist, and lease browser profiles below one runtime root."""

    schema_version = 1

    def __init__(self, root: str):
        self.root = Path(root).expanduser().resolve()
        self.data_root = self.root / "data"
        self.profiles_root = self.data_root / "profiles"
        self.data_root.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.profiles_root.mkdir(parents=True, exist_ok=True, mode=0o700)
        self._reject_symlink(self.data_root)
        self._reject_symlink(self.profiles_root)
        os.chmod(self.data_root, 0o700)
        os.chmod(self.profiles_root, 0o700)

    @classmethod
    def from_environment(cls) -> "ProfileRuntime":
        """Construct the runtime selected by a CLI or supervised web worker."""
        configured = os.getenv("PROFILE_RUNTIME_ROOT") or os.getenv("GMAIL_DATA_ROOT")
        return cls(configured or os.getcwd())

    @staticmethod
    def _reject_symlink(path: Path) -> None:
        if path.is_symlink():
            raise ProfileUnavailableError("profile runtime path cannot be a symlink")

    def _contained(self, path: Path) -> Path:
        candidate = path.expanduser()
        if not candidate.is_absolute():
            candidate = self.root / candidate
        # Resolve only the parent for containment checks.  Resolving the whole
        # path first would hide a symlink at the final component.
        absolute = Path(os.path.abspath(str(candidate)))
        if absolute.exists() and absolute.is_symlink():
            raise ProfileConflictError("profile directory cannot be a symlink")
        candidate = absolute.resolve(strict=False)
        try:
            candidate.relative_to(self.profiles_root.resolve())
        except ValueError:
            raise ProfileConflictError("profile path is outside the runtime profiles root")
        if candidate == self.profiles_root:
            raise ProfileConflictError("profile path must identify a profile directory")
        current = self.profiles_root
        try:
            relative_parts = candidate.relative_to(current.resolve()).parts
        except ValueError:
            raise ProfileConflictError("profile path is outside the runtime profiles root")
        if len(relative_parts) != 1:
            raise ProfileConflictError("nested profile paths are not allowed")
        self._reject_symlink(current)
        # Existing ancestors inside the profile root must not be symlinks.
        cursor = self.profiles_root
        for part in relative_parts:
            cursor = cursor / part
            if cursor.exists() and cursor.is_symlink():
                raise ProfileConflictError("profile path contains a symlink")
        return candidate

    def _handle(self, profile_id: str) -> ProfileHandle:
        if not isinstance(profile_id, str) or not _PROFILE_ID.fullmatch(profile_id):
            raise ProfileConflictError("invalid profile id")
        path = self._contained(self.profiles_root / profile_id)
        return ProfileHandle(profile_id, path, path / "profile_manifest.json", path / "profile.lock")

    def _validate_handle(self, handle: ProfileHandle) -> ProfileHandle:
        """Ensure a caller cannot smuggle an arbitrary path via a handle."""
        if not isinstance(handle, ProfileHandle):
            raise ProfileConflictError("invalid profile handle")
        expected = self._handle(handle.profile_id)
        supplied_path = Path(os.path.abspath(str(handle.path)))
        supplied_manifest = Path(os.path.abspath(str(handle.manifest_path)))
        supplied_lock = Path(os.path.abspath(str(handle.lock_path)))
        if (supplied_path != expected.path or supplied_manifest != expected.manifest_path
                or supplied_lock != expected.lock_path):
            raise ProfileConflictError("profile handle path does not match profile id")
        return expected

    @staticmethod
    def _atomic_json(path: Path, payload: Dict[str, Any]) -> None:
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        fd, temporary = tempfile.mkstemp(prefix=".profile_manifest.", dir=str(path.parent))
        try:
            os.fchmod(fd, 0o600)
            with os.fdopen(fd, "w", encoding="utf-8") as stream:
                json.dump(payload, stream, ensure_ascii=True, sort_keys=True, indent=2)
                stream.write("\n")
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, path)
            os.chmod(path, 0o600)
            try:
                directory_fd = os.open(str(path.parent), os.O_RDONLY)
                try:
                    os.fsync(directory_fd)
                finally:
                    os.close(directory_fd)
            except OSError:
                pass
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)

    @staticmethod
    def _validate_manifest(manifest: Dict[str, Any], profile_id: str) -> None:
        required = ("schema_version", "profile_id", "engine", "email", "state", "identity")
        if not isinstance(manifest, dict) or any(key not in manifest for key in required):
            raise ProfileUnavailableError("profile manifest is incomplete")
        if type(manifest.get("schema_version")) is not int \
                or manifest.get("schema_version") != ProfileRuntime.schema_version:
            raise ProfileUnavailableError("profile manifest schema is invalid")
        if not isinstance(manifest.get("profile_id"), str) or manifest["profile_id"] != profile_id:
            raise ProfileConflictError("manifest profile id does not match directory")
        if not isinstance(manifest.get("engine"), str) or manifest["engine"] not in ENGINES:
            raise ProfileUnavailableError("manifest engine is unsupported")
        if not isinstance(manifest.get("email"), str):
            raise ProfileUnavailableError("manifest email is invalid")
        if not isinstance(manifest.get("state"), str) or manifest["state"] not in PROFILE_STATES:
            raise ProfileUnavailableError("manifest lifecycle state is invalid")
        if manifest["state"] in ("bound", "ready") and not manifest.get("email", "").strip():
            raise ProfileUnavailableError("bound profile manifest email is missing")
        if "identity_state" in manifest and manifest.get("identity_state") not in (
            "native", "identity_reconstructed", "legacy_unbound",
        ):
            raise ProfileUnavailableError("manifest identity state is invalid")
        if not isinstance(manifest["identity"], dict):
            raise ProfileUnavailableError("manifest identity is invalid")
        try:
            validate_profile_identity(manifest["identity"])
        except ProfileConflictError as exc:
            # A malformed/tampered manifest is unavailable to every operation;
            # callers must not synthesize a replacement identity from adapter
            # defaults while reusing the same profile files.
            raise ProfileUnavailableError("profile identity is invalid") from exc
        for key in ("browser", "network"):
            if key in manifest and not isinstance(manifest[key], dict):
                raise ProfileUnavailableError("profile manifest %s is invalid" % key)
        if "identity_verified" in manifest and not isinstance(manifest["identity_verified"], bool):
            raise ProfileUnavailableError("profile manifest identity verification is invalid")
        browser = manifest.get("browser") or {}
        if "channel" in browser and not isinstance(browser.get("channel"), str):
            raise ProfileUnavailableError("profile manifest browser channel is invalid")
        if "major_version" in browser and not isinstance(browser.get("major_version"), str):
            raise ProfileUnavailableError("profile manifest browser version is invalid")
        network = manifest.get("network") or {}
        if "bound" in network and not isinstance(network.get("bound"), bool):
            raise ProfileUnavailableError("profile manifest network binding is invalid")
        if network.get("bound"):
            if not isinstance(network.get("endpoint_hash"), str) \
                    or not _HEX64.fullmatch(network.get("endpoint_hash", "")):
                raise ProfileUnavailableError("profile manifest proxy binding is invalid")
        elif "endpoint_hash" in network and network.get("endpoint_hash") not in ("", None):
            raise ProfileUnavailableError("unbound profile manifest contains a proxy binding")

        if _contains_forbidden_manifest_value(manifest):
            raise ProfileUnavailableError("profile manifest contains secret material")

    def provision(self, email: str, engine: str, proxy: Optional[str] = None,
                  identity: Optional[Dict[str, Any]] = None) -> ProfileHandle:
        if engine not in ENGINES:
            raise ValueError("engine must be playwright or selenium")
        if proxy and _parse_proxy(proxy) is None:
            raise ProxyUnavailableError("configured proxy has an invalid format")
        for _ in range(10):
            profile_id = uuid.uuid4().hex
            handle = self._handle(profile_id)
            profile_identity = identity or build_identity(profile_id, engine)
            validate_profile_identity(profile_identity)
            if _contains_forbidden_manifest_value(profile_identity):
                raise ProfileConflictError("profile identity contains secret material")
            try:
                handle.path.mkdir(mode=0o700)
            except FileExistsError:
                continue
            os.chmod(handle.path, 0o700)
            os.close(os.open(str(handle.lock_path), os.O_RDWR | os.O_CREAT, 0o600))
            os.chmod(handle.lock_path, 0o600)
            manifest = {
                "schema_version": self.schema_version,
                "profile_id": profile_id,
                "engine": engine,
                "email": email or "",
                "state": "provisioning",
                "identity_state": "native",
                "browser": {"channel": "chrome", "major_version": ""},
                "identity": profile_identity,
                "network": proxy_binding(proxy),
                "created_at": _utc_now(),
                "updated_at": _utc_now(),
            }
            self._atomic_json(handle.manifest_path, manifest)
            return handle
        raise ProfileRuntimeError("could not allocate a unique profile id")

    def load(self, handle: ProfileHandle) -> Dict[str, Any]:
        handle = self._validate_handle(handle)
        try:
            if not handle.path.exists() or handle.path.is_symlink():
                raise ProfileUnavailableError("profile directory is missing")
            if handle.manifest_path.is_symlink() or not handle.manifest_path.is_file():
                raise ProfileUnavailableError("profile manifest is missing or unsafe")
            with handle.manifest_path.open("r", encoding="utf-8") as stream:
                manifest = json.load(stream)
        except ProfileRuntimeError:
            raise
        except (OSError, ValueError, TypeError) as exc:
            raise ProfileUnavailableError("profile manifest cannot be read") from exc
        self._validate_manifest(manifest, handle.profile_id)
        return manifest

    def resolve(self, profile_id: Optional[str] = None, profile_path: Optional[str] = None,
                expected_email: Optional[str] = None, expected_engine: Optional[str] = None,
                expected_proxy: Optional[str] = None) -> ProfileHandle:
        if profile_id:
            handle = self._handle(profile_id)
            if profile_path:
                supplied = self._contained(Path(profile_path))
                if supplied != handle.path:
                    raise ProfileConflictError("profile id and path refer to different profiles")
        elif profile_path:
            path = self._contained(Path(profile_path))
            handle = self._handle(path.name)
        else:
            raise ProfileUnavailableError("profile is not configured")
        manifest = self.load(handle)
        if expected_engine and manifest["engine"] != expected_engine:
            raise ProfileConflictError("profile is bound to a different browser engine")
        if expected_email and manifest.get("email", "").lower() != expected_email.lower():
            raise ProfileConflictError("profile is bound to a different account")
        if expected_proxy is not None:
            self.validate_proxy(manifest, expected_proxy)
        return handle

    def _write_update(self, handle: ProfileHandle, **changes: Any) -> Dict[str, Any]:
        manifest = self.load(handle)
        manifest.update(changes)
        manifest["updated_at"] = _utc_now()
        self._atomic_json(handle.manifest_path, manifest)
        return manifest

    def bind(self, handle: ProfileHandle, email: str,
             browser: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        manifest = self.load(handle)
        if manifest.get("state") not in ("provisioning", "bound"):
            raise ProfileConflictError("only a provisioning or bound profile can be bound")
        if manifest.get("email") and email and manifest["email"].lower() != email.lower():
            raise ProfileConflictError("profile email binding cannot be changed")
        # ``bind`` may be called before an adapter can report its runtime.  In
        # that case retain the provisioning channel rather than silently
        # changing a future warm/health launch to bundled Chromium.
        browser_info = (
            _normalise_browser(browser)
            if browser is not None
            else _normalise_browser(manifest.get("browser"))
        )
        verified = manifest.get("identity_verified")
        if verified is None:
            verified = manifest.get("identity_state") == "native"
        return self._write_update(
            handle, email=email, state="bound", browser=browser_info,
            identity_verified=bool(verified),
        )

    def record_runtime(self, handle: ProfileHandle, browser: Optional[Dict[str, Any]]) -> Dict[str, Any]:
        """Record/validate the actual adapter channel and major version."""
        manifest = self.load(handle)
        if manifest.get("state") not in ("provisioning", "bound", "ready"):
            raise ProfileConflictError("profile is not available for runtime binding")
        # An adapter may not expose runtime metadata (older drivers and small
        # test doubles commonly return ``{}``).  That is an absence of an
        # observation, not evidence that the browser was bundled Chromium.
        # Preserve the immutable registration binding until a real channel or
        # version is observed.
        if browser is None or (isinstance(browser, dict) and not browser):
            return manifest
        actual = _normalise_browser(browser)
        expected = _normalise_browser(manifest.get("browser"))
        if expected.get("major_version") and actual.get("major_version") \
                and expected["major_version"] != actual["major_version"]:
            raise ProfileRuntimeMismatchError(
                "browser major version does not match the profile manifest"
            )
        if expected.get("channel") and actual.get("channel") \
                and expected["channel"] != actual["channel"] \
                and manifest.get("state") != "provisioning":
            raise ProfileRuntimeMismatchError(
                "browser channel does not match the profile manifest"
            )
        merged = {"channel": actual.get("channel") or expected.get("channel") or "chrome",
                  "major_version": actual.get("major_version") or expected.get("major_version") or ""}
        return self._write_update(handle, browser=merged)

    def mark_identity_verified(self, handle: ProfileHandle,
                               observed_email: str, database=None) -> Dict[str, Any]:
        """Record an explicit browser identity observation for an adopted profile."""
        manifest = self.load(handle)
        if manifest.get("state") not in ("bound", "ready"):
            raise ProfileConflictError("profile is not bound for identity verification")
        if not isinstance(observed_email, str) or not observed_email.strip():
            raise ProfileConflictError("an observed account identity is required")
        expected = str(manifest.get("email") or "").strip().lower()
        observed = observed_email.strip().lower()
        if not expected or observed != expected:
            raise ProfileConflictError("observed browser identity does not match profile")
        updated = self._write_update(
            handle, identity_verified=True,
            identity_verified_email=observed,
            identity_verified_at=_utc_now(),
        )
        if database is not None:
            updater = getattr(database, "update_profile_state", None)
            if not callable(updater) or not updater(
                    expected, updated.get("state", "bound"),
                    updated.get("identity_state", "identity_reconstructed")):
                raise ProfileRuntimeError("unable to persist identity verification")
        return updated

    def mark_ready(self, handle: ProfileHandle,
                   observed_email: Optional[str] = None, database=None) -> Dict[str, Any]:
        manifest = self.load(handle)
        if manifest.get("state") not in ("bound", "ready"):
            raise ProfileConflictError("only a bound profile can become ready")
        if manifest.get("identity_state") == "identity_reconstructed" \
                and not identity_is_verified(manifest):
            if observed_email is None:
                raise ProfileConflictError(
                    "an adopted profile requires fresh identity verification before ready"
                )
            self.mark_identity_verified(handle, observed_email, database=database)
        updated = self._write_update(handle, state="ready")
        if database is not None:
            updater = getattr(database, "update_profile_state", None)
            if not callable(updater) or not updater(
                    updated.get("email", ""), "ready",
                    updated.get("identity_state", "identity_reconstructed")):
                raise ProfileRuntimeError("unable to persist ready profile state")
        return updated

    def mark_orphaned(self, handle: ProfileHandle, reason: str = "") -> Dict[str, Any]:
        return self._write_update(handle, state="orphaned", error_code=reason or "orphaned")

    def mark_cleanup_failed(self, handle: ProfileHandle,
                            reason: str = "cleanup_failed") -> Dict[str, Any]:
        """Quarantine a profile whose browser shutdown could not be confirmed."""
        return self._write_update(
            handle, state="cleanup_failed", error_code=reason or "cleanup_failed"
        )

    def mark_corrupt(self, handle: ProfileHandle, reason: str = "") -> Dict[str, Any]:
        # A malformed manifest cannot be loaded; retain a sidecar diagnostic.
        payload = {"state": "corrupt", "error_code": reason or "corrupt", "updated_at": _utc_now()}
        try:
            self._atomic_json(handle.path / "profile_diagnostic.json", payload)
        except OSError:
            pass
        return payload

    def validate_proxy(self, manifest: Dict[str, Any], proxy: Optional[str]) -> None:
        expected = manifest.get("network") or {"bound": False, "endpoint_hash": ""}
        if proxy and _parse_proxy(proxy) is None:
            raise ProxyUnavailableError("configured proxy has an invalid format")
        actual = proxy_binding(proxy)
        if not expected.get("bound") and actual.get("bound"):
            raise ProfileConflictError("profile has no bound proxy; caller supplied a proxy")
        if expected.get("bound") and not actual.get("bound"):
            raise ProxyUnavailableError("profile requires its bound proxy")
        if expected.get("bound") and expected.get("endpoint_hash") != actual.get("endpoint_hash"):
            raise ProxyMismatchError("configured proxy does not match profile binding")

    def lease(self, handle: ProfileHandle, operation: str, timeout: float = 0) -> ProfileLease:
        manifest = self.load(handle)
        return ProfileLease(handle, operation, manifest["engine"], timeout)

    @staticmethod
    def _reconcile_accounts(accounts=None, database=None):
        if database is None and accounts is not None and hasattr(accounts, "get_all_accounts"):
            # Accept ``reconcile(database)`` for embedders that used the
            # natural positional spelling before the keyword was documented.
            database, accounts = accounts, None
        if accounts is not None and database is not None:
            raise ValueError("pass accounts or database, not both")
        source = accounts
        if database is not None:
            getter = getattr(database, "get_all_accounts", None)
            source = getter() if callable(getter) else database
        if source is None:
            return None
        if isinstance(source, dict):
            source = [source]
        return [item for item in (source or []) if isinstance(item, dict)]

    @staticmethod
    def _record(profile_id, path, state, error_code="", status="", **values):
        record = {"profile_id": profile_id, "path": str(path), "state": state}
        if error_code:
            record["error_code"] = error_code
        if status:
            record["status"] = status
        for key, value in values.items():
            if value not in (None, ""):
                record[key] = value
        return record

    def _orphan_if_unbound(self, handle):
        """Mark a stale bound profile orphaned while respecting the same lease."""
        with self.lease(handle, "reconcile"):
            manifest = self.load(handle)
            if manifest.get("state") in ("bound", "ready"):
                return self._write_update(
                    handle, state="orphaned", error_code="database_binding_missing"
                )
            return manifest

    def reconcile(self, accounts=None, database=None) -> list:
        """Reconcile filesystem profiles against optional account bindings.

        With no database input this is a read-only filesystem diagnostic.  If
        account rows are supplied, stale bound manifests are transitioned to
        ``orphaned`` under their profile lease, while database-only and
        mismatched bindings are returned as explicit diagnostics.  No
        credential or cookie fields are copied into the result.
        """
        rows = self._reconcile_accounts(accounts, database)
        by_id = {}
        legacy_rows = []
        duplicate_ids = set()
        if rows is not None:
            for row in rows:
                profile_id = str(row.get("profile_id") or "").strip()
                if profile_id and profile_id not in by_id:
                    by_id[profile_id] = row
                elif profile_id:
                    duplicate_ids.add(profile_id)
                elif not profile_id and str(row.get("profile_path") or "").strip():
                    legacy_rows.append(row)

        records = []
        seen = set()
        try:
            entries = sorted(self.profiles_root.iterdir(), key=lambda item: item.name)
        except (OSError, FileNotFoundError):
            entries = []

        for path in entries:
            profile_id = path.name
            seen.add(profile_id)
            if path.is_symlink():
                records.append(self._record(
                    profile_id, path, "corrupt", "unsafe_profile_path", "profile_conflict"
                ))
                continue
            if not path.is_dir():
                records.append(self._record(
                    profile_id, path, "corrupt", "profile_not_directory", "profile_conflict"
                ))
                continue
            if not _PROFILE_ID.fullmatch(profile_id):
                records.append(self._record(
                    profile_id, path, "legacy_unbound", "legacy_profile_name"
                ))
                continue
            manifest_path = path / "profile_manifest.json"
            if manifest_path.is_symlink():
                records.append(self._record(
                    profile_id, path, "corrupt", "unsafe_manifest_path", "profile_conflict"
                ))
                continue
            if not manifest_path.is_file():
                matching_legacy = None
                for row in legacy_rows:
                    try:
                        candidate = Path(str(row.get("profile_path"))).expanduser()
                        if not candidate.is_absolute():
                            candidate = self.root / candidate
                        if candidate.resolve(strict=False) == path.resolve(strict=False):
                            matching_legacy = row
                            break
                    except (OSError, RuntimeError):
                        continue
                if matching_legacy is None:
                    records.append(self._record(
                        profile_id, path, "legacy_unbound", "manifest_missing"
                    ))
                else:
                    records.append(self._record(
                        profile_id, path, "legacy_unbound", "database_legacy_path",
                        email=matching_legacy.get("email", ""),
                    ))
                continue

            handle = self._handle(profile_id)
            try:
                manifest = self.load(handle)
            except ProfileRuntimeError as exc:
                records.append(self._record(
                    profile_id, path, "corrupt", exc.code, "profile_unavailable"
                ))
                continue

            state = manifest.get("state")
            record = self._record(
                profile_id, path, state,
                engine=manifest.get("engine"),
                email=manifest.get("email", ""),
                identity_state=manifest.get("identity_state", ""),
            )
            if rows is not None:
                account = by_id.get(profile_id)
                if account is None and state in ("bound", "ready"):
                    try:
                        self._orphan_if_unbound(handle)
                    except ProfileBusyError:
                        record.update({"state": state, "error_code": "profile_busy",
                                       "status": "profile_busy"})
                    else:
                        record.update({"state": "orphaned",
                                       "error_code": "database_binding_missing",
                                       "status": "profile_conflict"})
                elif account is not None:
                    supplied_path = str(account.get("profile_path") or "").strip()
                    path_mismatch = False
                    if supplied_path:
                        try:
                            path_mismatch = self._contained(Path(supplied_path)) != handle.path
                        except ProfileRuntimeError:
                            path_mismatch = True
                    email_mismatch = bool(
                        account.get("email") and
                        str(account.get("email")).strip().lower() !=
                        str(manifest.get("email") or "").strip().lower()
                    )
                    engine_mismatch = bool(
                        account.get("engine") and account.get("engine") != manifest.get("engine")
                    )
                    if path_mismatch or email_mismatch or engine_mismatch:
                        record.update({"state": "corrupt", "error_code": "binding_mismatch",
                                       "status": "profile_conflict"})
            records.append(record)

        if rows is not None:
            for profile_id in sorted(duplicate_ids):
                account = by_id.get(profile_id, {})
                records.append(self._record(
                    profile_id, self.profiles_root / profile_id, "corrupt",
                    "duplicate_profile_binding", "profile_conflict",
                    email=account.get("email", ""), engine=account.get("engine", ""),
                ))
            for profile_id, account in sorted(by_id.items()):
                if profile_id in seen:
                    continue
                if not _PROFILE_ID.fullmatch(profile_id):
                    records.append(self._record(
                        profile_id, self.profiles_root / profile_id, "corrupt",
                        "invalid_profile_id", "profile_unavailable",
                        email=account.get("email", ""), engine=account.get("engine", ""),
                    ))
                    continue
                try:
                    expected_path = self._contained(self.profiles_root / profile_id)
                except ProfileRuntimeError:
                    expected_path = self.profiles_root / profile_id
                records.append(self._record(
                    profile_id, expected_path, "corrupt", "profile_missing",
                    "profile_unavailable", email=account.get("email", ""),
                    engine=account.get("engine", ""),
                ))
            for row in legacy_rows:
                raw_path = str(row.get("profile_path") or "").strip()
                if not raw_path:
                    continue
                try:
                    candidate = Path(raw_path).expanduser()
                    if not candidate.is_absolute():
                        candidate = self.root / candidate
                    candidate = candidate.resolve(strict=False)
                except (OSError, RuntimeError):
                    candidate = Path(raw_path)
                if not candidate.exists():
                    records.append(self._record(
                        "", candidate, "legacy_unbound", "database_legacy_path",
                        email=row.get("email", ""),
                    ))
        return records

    def adopt_legacy(self, profile_path: str, email: str, engine: str,
                     database=None, password: Optional[str] = None,
                     **account_fields) -> ProfileHandle:
        if not isinstance(email, str) or not email.strip():
            raise ProfileConflictError("an account email is required for adoption")
        if engine not in ENGINES:
            raise ProfileConflictError("adopted profile engine is unsupported")
        adopted_proxy = account_fields.get("proxy")
        if adopted_proxy and _parse_proxy(adopted_proxy) is None:
            raise ProxyUnavailableError("configured proxy has an invalid format")
        path = self._contained(Path(profile_path))
        if not path.exists() or not path.is_dir():
            raise ProfileUnavailableError("legacy profile directory is missing")
        handle = self._handle(path.name)
        # Adoption mutates an existing browser directory and therefore follows
        # the same cross-process lease protocol as open/probe/warm operations.
        # This also prevents two recovery jobs from both claiming the basename.
        with ProfileLease(handle, "adopt", engine):
            if handle.manifest_path.is_symlink() or handle.manifest_path.exists():
                raise ProfileConflictError("profile already has a manifest")
            if database is not None:
                existing = getattr(database, "get_account_by_profile_id", lambda _id: None)(
                    handle.profile_id
                )
                if existing:
                    raise ProfileConflictError("profile id is already bound to an account")
            identity = build_identity(handle.profile_id, engine)
            manifest = {
                "schema_version": self.schema_version, "profile_id": handle.profile_id,
                "engine": engine, "email": email.strip(), "state": "bound",
                "identity_state": "identity_reconstructed", "identity_verified": False,
                # Empty means unknown; the first actual runtime observation binds
                # the channel without treating a legacy profile as Chromium.
                "browser": {"channel": "", "major_version": ""},
                "identity": identity, "network": proxy_binding(adopted_proxy),
                "created_at": _utc_now(), "updated_at": _utc_now(),
            }
            self._atomic_json(handle.manifest_path, manifest)
            os.chmod(handle.path, 0o700)
            os.chmod(handle.lock_path, 0o600)
            if database is not None and password is not None:
                try:
                    saved = database.save_profile_binding(
                        email=email.strip(), password=password,
                        profile_id=handle.profile_id, engine=engine,
                        profile_path=str(handle.path), profile_state="bound",
                        identity_state="identity_reconstructed", **account_fields
                    )
                except Exception as exc:
                    try:
                        self._write_update(
                            handle, state="orphaned", error_code="database_binding_failed"
                        )
                    except Exception as cleanup_exc:
                        logger.debug(
                            "Unable to mark adopted profile orphaned: %s",
                            type(cleanup_exc).__name__,
                        )
                    raise ProfileConflictError(
                        "unable to persist adopted profile binding"
                    ) from exc
                if not saved:
                    self._write_update(
                        handle, state="orphaned", error_code="database_binding_failed"
                    )
                    raise ProfileConflictError("unable to persist adopted profile binding")
        return handle


def default_profile_runtime() -> ProfileRuntime:
    return ProfileRuntime.from_environment()


_PROCESS_STATE_UNKNOWN = object()


def inspect_process_stopped(resource: Any, *, max_depth: int = 3) -> Optional[bool]:
    """Return a process/browser stop fact when the adapter exposes one.

    Browser libraries do not share one process API.  Selenium commonly exposes
    ``service.process.poll()``, while test doubles and wrappers use
    ``process``, ``returncode`` or ``is_alive``.  Playwright's Browser exposes
    ``is_connected`` instead.  This helper deliberately accepts only concrete
    boolean/integer results so dynamic proxy objects (including ``Mock``) do
    not accidentally look like a live or stopped process.

    ``None`` means the adapter exposes no inspectable fact; callers may then
    use an explicit close result, but must treat an explicit false as failure.
    """
    if resource is None:
        return None
    queue = [(resource, 0)]
    seen = set()
    saw_stopped = False
    process_names = (
        "process", "browser_process", "_process", "_browser_process",
        "service", "_service", "browser", "_browser", "context",
    )
    while queue:
        current, depth = queue.pop(0)
        if current is None or id(current) in seen or depth > max_depth:
            continue
        seen.add(id(current))

        poll = getattr(current, "poll", None)
        if callable(poll):
            try:
                value = poll()
            except BaseException:
                value = _PROCESS_STATE_UNKNOWN
            # A real subprocess poll returns None while running and an int
            # return code after exit.  Ignore opaque proxy return values.
            if value is None:
                # A live process anywhere in the object graph dominates every
                # stopped/unknown observation.  Do not return ``True`` merely
                # because another nested process was already stopped.
                return False
            if type(value) is int:
                saw_stopped = True
            # ``bool`` is an ``int`` subclass, but a boolean poll result is not
            # a subprocess exit code.  Treat it as unknown and keep walking.

        is_alive = getattr(current, "is_alive", None)
        if callable(is_alive):
            try:
                value = is_alive()
            except BaseException:
                value = _PROCESS_STATE_UNKNOWN
            if type(value) is bool:
                if value:
                    return False
                saw_stopped = True

        returncode = getattr(current, "returncode", None)
        if type(returncode) is int:
            saw_stopped = True
        if returncode is None and hasattr(current, "returncode"):
            # ``None`` is meaningful only for an object that really exposes a
            # returncode property; dynamic mocks are filtered by type below.
            module_name = getattr(type(current), "__module__", "")
            if module_name not in ("unittest.mock", "mock"):
                return False

        is_connected = getattr(current, "is_connected", None)
        if callable(is_connected):
            try:
                value = is_connected()
            except BaseException:
                value = _PROCESS_STATE_UNKNOWN
            if type(value) is bool:
                if value:
                    return False
                saw_stopped = True

        closed = getattr(current, "closed", None)
        if type(closed) is bool:
            if closed:
                saw_stopped = True
                # ``closed`` describes only this wrapper.  Adapters may keep
                # a browser/service child reachable after their high-level
                # object reports closed, so continue walking the object graph
                # before accepting the stopped observation.
                pass
            # A concrete ``closed=False`` flag is evidence of a live resource.
            elif closed is False:
                return False

        if depth < max_depth:
            for name in process_names:
                try:
                    nested = getattr(current, name, None)
                except BaseException:
                    nested = None
                if nested is None or nested is current:
                    continue
                # Avoid recursively following dynamically generated mocks.
                module_name = getattr(type(nested), "__module__", "")
                if module_name in ("unittest.mock", "mock"):
                    continue
                queue.append((nested, depth + 1))
    return True if saw_stopped else None


def registration_cleanup_verified(value: Any) -> bool:
    """Accept cleanup only when the adapter reported both required facts.

    Registration must not reopen a persistent profile until the previous
    browser has explicitly confirmed a successful close and a stopped process.
    ``bool`` coercion is intentionally avoided so adapter values such as
    ``1``/``"true"`` cannot turn an unknown cleanup state into success.
    """
    return (
        isinstance(value, dict)
        and value.get("success") is True
        and value.get("browser_process_stopped") is True
    )


def release_registration_lease(lease: Any) -> bool:
    """Release a registration lease and return only a literal success fact."""
    if lease is None:
        return False
    try:
        return lease.release() is True
    except BaseException:
        return False


class BrowserObservation(dict):
    """Dictionary-compatible normalized browser protocol observation."""

    def __init__(self, _expected_email: Optional[str] = None,
                 _identity_bound: Optional[bool] = None,
                 _trusted_session: bool = False,
                 _evidence_token: Any = None, **values: Any):
        # Use the mapping as the single source of truth while retaining the
        # attribute-style access used by older adapters and callers.  A plain
        # ``dict.update`` bypasses ``__setitem__`` for subclasses, so all
        # mutation helpers below explicitly synchronize both views.
        self._expected_email = str(_expected_email or "").strip().lower() or None
        self._identity_bound = _identity_bound if type(_identity_bound) is bool else None
        self._evidence_token = None
        dict.__init__(self)
        self.update(values)
        # Set the marker after initial population because ``update`` treats
        # protocol-field mutations as invalidating any prior proof.
        if _trusted_session is True or _evidence_token is _AUTH_EVIDENCE_TOKEN:
            self._evidence_token = _AUTH_EVIDENCE_TOKEN

    def __setattr__(self, key: Any, value: Any) -> None:
        """Keep direct attribute writes on the same mapping source of truth.

        Older callers use both ``observation.code = ...`` and
        ``observation["code"] = ...``.  Without this proxy the two views can
        diverge, and the computed ``status`` property may classify a different
        protocol state than the value an API consumer reads.  Private fields
        are implementation context and remain ordinary attributes.
        """
        if key.startswith("_") or key == "status":
            object.__setattr__(self, key, value)
            return
        # During dict.__init__ the mapping is not ready yet.  Use the normal
        # object path until the class has established its private context.
        if "_evidence_token" not in self.__dict__:
            object.__setattr__(self, key, value)
            return
        self.__setitem__(key, value)

    def __delattr__(self, key: Any) -> None:
        """Mirror direct attribute deletion into the observation mapping."""
        if key.startswith("_") or key == "status":
            object.__delattr__(self, key)
            return
        if key in self:
            self.__delitem__(key)
            return
        # Match normal attribute semantics for an unknown attribute while
        # avoiding a stale public value in ``__dict__``.
        object.__delattr__(self, key)

    _EVIDENCE_FIELDS = frozenset({
        "authenticated", "auth_cookie", "application_shell", "origin",
        "final_url", "observed_email", "account_email", "identity_bound",
        "identity_state", "profile_identity_state", "code", "challenge",
        "captcha", "login_required", "signin", "cookies",
    })

    def __setitem__(self, key: Any, value: Any) -> None:
        dict.__setitem__(self, key, value)
        # ``status`` is a computed property; storing a shadow attribute would
        # make attribute and mapping reads disagree with the classifier.
        if key != "status":
            self.__dict__[key] = value
        if key in self._EVIDENCE_FIELDS:
            self._evidence_token = None

    def __delitem__(self, key: Any) -> None:
        dict.__delitem__(self, key)
        if key != "status":
            self.__dict__.pop(key, None)
        if key in self._EVIDENCE_FIELDS:
            self._evidence_token = None

    def update(self, *args: Any, **kwargs: Any) -> None:
        values = dict(*args, **kwargs)
        for key, value in values.items():
            self[key] = value

    def setdefault(self, key: Any, default: Any = None) -> Any:
        if key not in self:
            self[key] = default
        return self[key]

    def pop(self, key: Any, *args: Any) -> Any:
        if key not in self:
            if args:
                return args[0]
            raise KeyError(key)
        value = dict.pop(self, key)
        if key != "status":
            self.__dict__.pop(key, None)
        if key in self._EVIDENCE_FIELDS:
            self._evidence_token = None
        return value

    def clear(self) -> None:
        keys = list(self.keys())
        dict.clear(self)
        for key in keys:
            if key != "status":
                self.__dict__.pop(key, None)
        self._evidence_token = None

    def __ior__(self, other: Any):
        self.update(other)
        return self

    @property
    def status(self) -> str:
        # Probe callers know the identity they asked the adapter to verify.
        # Keep that context private so the mapping remains safe to serialize,
        # while ensuring the attribute projection applies the exact same
        # identity check as the classifier that produced ``authenticated``.
        return classify_browser_observation(
            self, self._expected_email, _identity_bound=self._identity_bound,
            _evidence_token=self._evidence_token,
        )


def _apply_probe_cleanup(observation: Optional[Dict[str, Any]], runtime: Any,
                         handle: Any, engine: str, cleanup: Any) -> Dict[str, Any]:
    """Fold adapter shutdown into the browser observation before lease exit.

    A health probe is not successful merely because navigation found an inbox.
    If the adapter cannot prove that its browser process stopped, quarantine
    the profile and expose ``cleanup_failed`` so callers do not release/reuse
    a directory that may still be owned by a live browser.
    """
    if cleanup is None:
        # ``None`` is used only when no adapter was created, so there is no
        # browser process to verify.  Once an adapter returns a value, both
        # cleanup facts must be literal booleans from its contract.
        success, stopped = True, True
    elif isinstance(cleanup, dict):
        success = cleanup.get("success") is True
        stopped = cleanup.get("browser_process_stopped") is True
    else:
        success, stopped = False, False
    if observation is None:
        observation = BrowserObservation(
            code="cleanup_failed" if not (success and stopped) else "error",
            error="cleanup_failed" if not (success and stopped) else "probe_empty",
            profile_id=getattr(handle, "profile_id", ""), engine=engine,
        )
    if not (success and stopped):
        observation.update({
            "authenticated": False,
            "code": "cleanup_failed",
            "error": "cleanup_failed",
            "cleanup_status": "failed",
            "browser_process_stopped": stopped,
            "lease_released": False,
        })
        try:
            runtime.mark_cleanup_failed(handle, "browser_cleanup_failed")
        except Exception as exc:
            logger.error(
                "Unable to quarantine profile after browser cleanup failure: %s",
                type(exc).__name__,
            )
    else:
        observation.update({
            "cleanup_status": "completed",
            "browser_process_stopped": True,
            "lease_released": True,
        })
    return observation


def _apply_probe_exception(observation: Optional[Dict[str, Any]], runtime: Any,
                           handle: Any, engine: str,
                           error: BaseException) -> BrowserObservation:
    """Turn an uncaught probe/lease exception into a durable failure fact.

    Adapter shutdown and lease release run in nested ``finally`` blocks.  A
    context manager or stability check can nevertheless raise a
    ``BaseException`` (including ``KeyboardInterrupt``).  Returning a
    structured observation here keeps the already-collected evidence from
    being discarded and ensures the profile is quarantined before reuse.
    """
    if isinstance(error, asyncio.CancelledError):
        code = "cancelled"
    elif isinstance(error, ProfileRuntimeError):
        code = getattr(error, "code", None) or "profile_conflict"
    else:
        code = "cleanup_failed"
    if observation is None:
        observation = BrowserObservation(
            code=code, error=code,
            profile_id=getattr(handle, "profile_id", ""), engine=engine,
        )
    if code != "cancelled" or observation.get("cleanup_status"):
        observation.update({
            "cleanup_status": "failed",
            "browser_process_stopped": False,
            "lease_released": False,
        })
    if observation is not None and observation:
        previous = str(
            observation.get("code") or observation.get("error") or ""
        )
        observation.update({
            "authenticated": False,
            "code": code,
            "error": code,
            "cleanup_status": "failed",
            "browser_process_stopped": False,
            "lease_released": False,
        })
        if previous and previous != code:
            observation["operation_error_code"] = previous
    try:
        runtime.mark_cleanup_failed(handle, code)
    except BaseException as exc:
        logger.error(
            "Unable to quarantine profile after probe exception: %s",
            type(exc).__name__,
        )
    return observation


def _lease_release_verified(lease: Any) -> bool:
    """Inspect a real lease (or a thin test wrapper) after context exit."""
    current = lease
    for _ in range(3):
        if current is None:
            return False
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
    # A context manager without a concrete lease state cannot prove that the
    # OS lock was released.  Unknown state is deliberately fail-closed.
    return False


def _apply_probe_lease_release(observation: Optional[Dict[str, Any]],
                               runtime: Any, handle: Any, lease: Any) -> None:
    """Make an unverified profile lease release a probe failure."""
    if observation is None or lease is None:
        return
    if observation.get("cleanup_status") == "failed":
        observation["lease_released"] = False
        return
    if _lease_release_verified(lease):
        observation["lease_released"] = True
        return
    previous = str(observation.get("code") or observation.get("error") or "")
    observation.update({
        "authenticated": False,
        "code": "cleanup_failed",
        "error": "cleanup_failed",
        "cleanup_status": "failed",
        # Lease release is a reconciliation boundary.  Preserve only the
        # literal boolean fact already produced by the cleanup protocol;
        # values such as ``"false"`` are unknown and must fail closed.
        "browser_process_stopped": observation.get("browser_process_stopped") is True,
        "lease_released": False,
    })
    if previous and previous != "cleanup_failed":
        observation["operation_error_code"] = previous
    try:
        runtime.mark_cleanup_failed(handle, "lease_release_failed")
    except BaseException as exc:
        logger.error(
            "Unable to quarantine profile after lease release failure: %s",
            type(exc).__name__,
        )


async def _close_async_manager_uncancelled(manager: Any):
    """Close an async adapter to completion even after repeated cancellation.

    The returned tuple is ``(cleanup_mapping, cancellation_seen)``.  The
    caller applies the cleanup mapping before re-raising cancellation so the
    profile result never claims a clean shutdown that was not observed.
    """
    try:
        value = manager.close()
    except BaseException:
        return {"success": False, "browser_process_stopped": False}, False

    async def invoke():
        if inspect.isawaitable(value):
            return await value
        return value

    task = asyncio.ensure_future(invoke())
    cancellation_seen = False
    while True:
        try:
            outcome = await asyncio.shield(task)
            break
        except asyncio.CancelledError:
            cancellation_seen = True
            if task.done():
                try:
                    outcome = task.result()
                except BaseException:
                    return {
                        "success": False,
                        "browser_process_stopped": False,
                    }, cancellation_seen
                break
            # Keep waiting for the adapter's close task.  A profile lease may
            # not be released while Chromium still owns the directory.
            continue
        except BaseException:
            return {"success": False, "browser_process_stopped": False}, cancellation_seen

    outcome_missing = outcome is None
    if isinstance(outcome, dict):
        cleanup = {
            "success": outcome.get("success") is True,
            "browser_process_stopped": outcome.get("browser_process_stopped") is True,
        }
    else:
        # A manager that does not return the protocol facts cannot prove a
        # clean shutdown.  ``None`` is therefore a failure once a manager was
        # created (the no-manager case is handled separately above).
        cleanup = {"success": False, "browser_process_stopped": False}
    observed = inspect_process_stopped(manager)
    if observed is False:
        cleanup.update({"success": False, "browser_process_stopped": False})
    elif observed is True:
        cleanup["browser_process_stopped"] = True
        if outcome_missing:
            # A few adapter versions return no value from close(); the
            # concrete stopped observation is the only valid compatibility
            # proof in that case.  Malformed mapping fields stay failures.
            cleanup["success"] = True
    return cleanup, cancellation_seen


def _safe_url(value: Any) -> str:
    try:
        return str(value or "")
    except Exception:
        return ""


def _origin(url: str) -> str:
    try:
        parsed = urlsplit(url)
        return "%s://%s" % (parsed.scheme.lower(), parsed.netloc.lower()) if parsed.scheme and parsed.netloc else ""
    except Exception:
        return ""


def _cookie_names(cookies: Any) -> list:
    names = []
    try:
        for cookie in cookies or []:
            name = cookie.get("name") if isinstance(cookie, dict) else getattr(cookie, "name", None)
            if name:
                names.append(str(name))
    except Exception:
        pass
    return sorted(set(names))


def _semantic_signals(text: str) -> Dict[str, bool]:
    try:
        lowered = str(text or "").lower()
    except Exception:
        lowered = ""
    # A bare word such as ``challenge`` occurs in ordinary help and product
    # copy.  Only explicit security/challenge phrases are strong enough to
    # override a session observation.
    challenge_patterns = (
        r"\bcaptcha\b",
        r"\bverify\s+it(?:['’]?s)\s+you\b",
        r"\bverify\s+(?:your\s+)?(?:identity|account)\b",
        r"\bconfirm\s+it(?:['’]?s)\s+you\b",
        r"\bconfirm\s+(?:your\s+)?(?:identity|account)\b",
        r"\bunusual\s+activity\b",
        r"\bsuspicious\s+sign[ -]?in\b",
        r"\bsecurity\s+(?:check|challenge)\b",
        r"\b(?:2|two)[ -]?step\s+verification\b",
        r"\bverification\s+required\b",
        r"\bchallenge(?:d)?\s+(?:required|verification|your\s+identity)\b",
    )
    login_patterns = (
        r"\bemail\s+or\s+phone\b",
        r"\benter\s+(?:your\s+)?email\b",
        r"\bforgot\s+email\b",
        r"\buse\s+another\s+account\b",
        r"\bsign\s+in\s+to\s+(?:gmail|google|your\s+account)\b",
        r"\bsignin\s+to\s+(?:gmail|google|your\s+account)\b",
        r"\bgoogle\s+sign[ -]?in\b",
        r"\blog\s*in\s+to\s+(?:gmail|google|your\s+account)\b",
    )
    return {
        "login_required": any(re.search(pattern, lowered) for pattern in login_patterns),
        "challenge": any(re.search(pattern, lowered) for pattern in challenge_patterns),
        # One generic word is not an auth assertion.  Keep this as a coarse
        # diagnostic only; ``classify_session_auth`` still requires a trusted
        # origin and a bound identity before accepting it.
        "application_signal": _looks_like_application_shell(lowered),
    }


def normalise_observed_email(value: Any) -> Optional[str]:
    """Extract one normalized email from a trusted account-label observation."""
    try:
        text = str(value or "")
    except Exception:
        return None
    match = _OBSERVED_EMAIL.search(text)
    return match.group(1).lower() if match else None


def _manifest_has_durable_binding(manifest: Optional[Dict[str, Any]]) -> bool:
    """Return whether a mapping looks like a persisted profile manifest.

    A few embedders pass a tiny ``{"identity_state": "native"}`` fixture to
    the classifier.  Real manifests always carry structural fields; those
    fields opt the caller into the stricter email-binding rule below.
    """
    if not isinstance(manifest, dict):
        return False
    return bool(set(manifest).intersection({
        "schema_version", "profile_id", "engine", "state", "identity",
        "browser", "network",
    }))


def _strict_manifest_email(manifest: Optional[Dict[str, Any]]) -> Optional[str]:
    """Normalize a persisted manifest email without extracting embedded text."""
    if not isinstance(manifest, dict) or not isinstance(manifest.get("email"), str):
        return None
    raw = manifest.get("email", "").strip().lower()
    normalized = normalise_observed_email(raw)
    return normalized if normalized and normalized == raw else None


def identity_is_verified(manifest: Optional[Dict[str, Any]]) -> bool:
    """Return whether a manifest contains a trustworthy identity binding.

    Native profiles are verified by registration.  Reconstructed (adopted)
    profiles need both the boolean marker and the normalized email recorded by
    a fresh browser observation.  Requiring the email prevents a stale
    ``identity_verified=true`` flag from bypassing adoption checks.
    """
    item = manifest if isinstance(manifest, dict) else {}
    if item.get("identity_state") == "native":
        # Durable/native profiles are only verified when their immutable
        # manifest contains a syntactically valid email.  Keep accepting the
        # historical minimal fixture shape used by embedders, which has no
        # durable structural fields and therefore cannot be persisted.
        if _manifest_has_durable_binding(item):
            return _strict_manifest_email(item) is not None
        return True
    if not item.get("identity_verified", False):
        return False
    expected = normalise_observed_email(item.get("email"))
    observed = normalise_observed_email(item.get("identity_verified_email"))
    return bool(expected and observed and expected == observed)


_AUTH_COOKIE_NAMES = frozenset({
    "SID", "HSID", "SSID", "LSID", "OSID", "ACCOUNT_CHOOSER",
    "SIDCC", "__Secure-1PSID", "__Secure-1PSIDTS", "__Host-1PSID",
})

_TRUSTED_SESSION_ORIGINS = frozenset({
    "https://mail.google.com", "https://www.google.com",
})


def _origin_host(origin: Any) -> str:
    """Return a strict HTTPS hostname for an origin/page URL."""
    try:
        raw_value = str(origin or "")
        value = raw_value.strip()
    except Exception:
        return ""
    if not value or "://" not in value:
        return ""
    if value != raw_value or any(
        ord(char) < 0x20 or ord(char) == 0x7f for char in value
    ):
        return ""
    try:
        parsed = urlsplit(value)
        if parsed.scheme.lower() != "https":
            return ""
        # Userinfo, ports, and malformed host/port values are not part of a
        # trusted Google origin.  In particular, do not let a credential-like
        # netloc be mistaken for ``mail.google.com``.
        if parsed.username is not None or parsed.password is not None:
            return ""
        if parsed.port is not None:
            return ""
        # Reject encoded controls in a full page URL before reducing it to a
        # host.  Otherwise a value such as ``%0a`` could smuggle a second
        # logical origin through downstream string comparisons.
        decoded_url = unquote(value)
        if any(ord(char) < 0x20 or ord(char) == 0x7f for char in decoded_url):
            return ""
        host = parsed.hostname or ""
    except (TypeError, ValueError):
        return ""
    try:
        host = host.encode("ascii").decode("ascii").lower()
    except (AttributeError, UnicodeError):
        return ""
    if (
        not host or host.endswith(".") or host.startswith(".")
        or ".." in host or any(char.isspace() for char in host)
        or any(not (char.isalnum() or char in ".-") for char in host)
    ):
        return ""
    return host


def _trusted_origin(origin: Any) -> bool:
    host = _origin_host(origin)
    return ("https://%s" % host) in _TRUSTED_SESSION_ORIGINS


def _valid_auth_cookie(cookies: Any, origin: str = "") -> bool:
    """Require cookie facts that make a Google session assertion meaningful."""
    if not _trusted_origin(origin):
        return False
    host = _origin_host(origin)
    now = time.time()
    try:
        for cookie in cookies or []:
            if not isinstance(cookie, dict):
                continue
            name = str(cookie.get("name") or "")
            value = cookie.get("value")
            if name not in _AUTH_COOKIE_NAMES or not isinstance(value, str) or not value:
                continue
            if (
                value != value.strip()
                or any(ord(char) < 0x20 or ord(char) == 0x7f for char in value)
                or re.fullmatch(
                    r"(?i)(?:placeholder|dummy|fixture(?:[-_][a-z0-9]+)*|"
                    r"test(?:[-_][a-z0-9]+)*|null|undefined|none)",
                    value.strip(),
                )
            ):
                continue
            raw_domain = cookie.get("domain")
            if not isinstance(raw_domain, str) or not raw_domain:
                continue
            # Browser cookie domains are ASCII host names.  Do not silently
            # normalize extra dots/whitespace because that can turn malformed
            # data into a broader-than-intended scope.
            if raw_domain != raw_domain.strip() or any(
                ord(char) < 0x20 or ord(char) == 0x7f for char in raw_domain
            ):
                continue
            domain = raw_domain.lower()
            if domain.startswith("."):
                domain = domain[1:]
            if not domain or domain.startswith(".") or domain.endswith("."):
                continue
            if not (domain == "google.com" or domain.endswith(".google.com")):
                continue
            # A cookie scoped to accounts.google.com/evil.google.com cannot be
            # used as evidence for a page currently hosted at mail.google.com.
            if not (host == domain or host.endswith("." + domain)):
                continue
            if cookie.get("secure") is not True:
                continue
            expiry = cookie.get("expires", cookie.get("expiry"))
            if expiry not in (None, ""):
                try:
                    if isinstance(expiry, bool):
                        continue
                    expiry_value = float(expiry)
                    if not math.isfinite(expiry_value) or expiry_value <= now:
                        continue
                except (TypeError, ValueError):
                    continue
            return True
    except Exception:
        return False
    return False


def _looks_like_application_shell(text: str) -> bool:
    """Recognize a coarse app shell only when several independent markers exist."""
    try:
        lowered = str(text or "").lower()
    except Exception:
        lowered = ""
    markers = (
        "inbox", "compose", "search mail", "primary", "sent", "drafts",
    )
    return sum(token in lowered for token in markers) >= 2


def classify_session_auth(text: str = "", cookies: Any = None,
                          observed_email: Optional[str] = None,
                          expected_email: Optional[str] = None,
                          manifest: Optional[Dict[str, Any]] = None,
                          origin: str = "", application_shell: Optional[bool] = None) -> Dict[str, Any]:
    """Normalize browser-session authentication facts for every adapter.

    Warm and health paths must agree on the meaning of a login page, challenge,
    auth cookie, and reconstructed identity.  Keeping this decision in the
    profile kernel prevents one adapter from silently treating a stale cookie
    or an adopted profile as authenticated while the other reports a conflict.
    """
    signals = _semantic_signals(text)
    manifest_item = manifest if isinstance(manifest, dict) else {}
    cookie_names = _cookie_names(cookies)
    trusted = _trusted_origin(origin)
    auth_cookie = _valid_auth_cookie(cookies, origin)
    # Body text is only a diagnostic signal.  Authentication requires the
    # adapter's explicit selector result; deriving a shell from words such as
    # ``Inbox`` or ``Compose`` lets local/misleading pages impersonate Gmail.
    shell = application_shell is True
    expected = normalise_observed_email(expected_email)
    observed = normalise_observed_email(observed_email)
    manifest_email = _strict_manifest_email(manifest_item)
    durable_manifest = _manifest_has_durable_binding(manifest_item)
    native_identity = (
        manifest_item.get("identity_state") == "native"
        and (not durable_manifest or bool(manifest_email))
    )
    identity_match = bool(observed and (not expected or observed == expected))
    reconstructed_identity = bool(
        manifest_item and manifest_item.get("identity_state") in {
            "identity_reconstructed", "legacy_unbound", "reconstructed",
        }
    )
    manifest_identity_match = bool(
        observed and manifest_email and observed == manifest_email
    )
    fresh_identity = bool(
        observed and (not expected or observed == expected)
        and (not reconstructed_identity or manifest_identity_match)
    )
    if reconstructed_identity:
        # A fresh, matching mailbox observation is what establishes adoption
        # during the first probe.  A durable ``identity_verified`` bit is not
        # required yet (the warmer persists it immediately after this probe),
        # but it can never substitute for the current observation.
        identity_bound = bool(
            manifest_identity_match
            and (not expected or observed == expected)
        )
    else:
        identity_bound = bool(
            identity_match
            or native_identity
            or identity_is_verified(manifest_item)
        )
    authenticated = False
    identity_confidence = "verified" if identity_match else ("bound" if identity_bound else "unknown")
    code = ""
    if observed and expected and observed != expected:
        authenticated = False
        code = "account_mismatch"
    elif expected and (
        ("email" in manifest_item and (
            not manifest_email or manifest_email != expected
        ))
        or (durable_manifest and not manifest_email)
    ):
        authenticated = False
        code = "account_mismatch"
    elif signals["challenge"]:
        authenticated = False
        code = "challenge"
    elif signals["login_required"]:
        authenticated = False
        code = "login_required"
    elif reconstructed_identity and not identity_bound:
        # A reconstructed profile with an ordinary application page but no
        # current identity is unsafe to trust.  An explicit sign-in/challenge
        # page is handled above so the warmer can complete the required login
        # and establish the fresh identity in this same session.
        authenticated = False
        code = "account_mismatch"
    elif trusted and identity_bound and auth_cookie and shell:
        authenticated = True
    else:
        code = "login_required"
    return {
        "authenticated": authenticated,
        "status": code or ("authenticated" if authenticated else "login_required"),
        "code": code,
        "signals": signals,
        "cookie_names": cookie_names,
        "auth_cookie": auth_cookie,
        "trusted_origin": trusted,
        "application_shell": shell,
        # This flag is consumed only as private context by BrowserObservation
        # when it reprojects the same classifier result through ``.status``.
        # It is never accepted as a standalone adapter claim.
        "identity_bound": identity_bound,
        "identity_fresh": fresh_identity,
        "identity_confidence": identity_confidence,
        "observed_email": observed,
        # Private process-local proof used when this result is projected into
        # a ``BrowserObservation``.  It is never persisted or sent to callers
        # as a credential-bearing value.
        "_evidence_token": _AUTH_EVIDENCE_TOKEN if authenticated else None,
    }


class BrowserProfileKernel:
    """Browser protocol adapter shared by health checks and warm operations."""

    def __init__(self, runtime: Optional[ProfileRuntime] = None):
        self.runtime = runtime or default_profile_runtime()

    def _validate_browser_operation(self, handle: ProfileHandle, engine: str,
                                     expected_email: Optional[str],
                                     proxy: Optional[str]) -> Dict[str, Any]:
        """Validate the immutable binding immediately after taking a lease."""
        manifest = self.runtime.load(handle)
        if manifest.get("engine") != engine:
            raise ProfileConflictError("profile is bound to a different browser engine")
        if expected_email and str(manifest.get("email", "")).lower() != expected_email.lower():
            raise ProfileConflictError("profile is bound to a different account")
        if manifest.get("state") not in ("bound", "ready"):
            raise ProfileUnavailableError("profile is not ready for browser operation")
        self.runtime.validate_proxy(manifest, proxy)
        return manifest

    @staticmethod
    async def _maybe_await(value: Any) -> Any:
        if inspect.isawaitable(value):
            return await value
        return value

    @staticmethod
    async def _close_page_uncancelled(page: Any):
        """Close an identity page/tab to completion despite cancellation."""
        try:
            value = page.close()
        except BaseException:
            return False, False
        task = asyncio.ensure_future(BrowserProfileKernel._maybe_await(value))
        cancellation_seen = False
        while True:
            try:
                await asyncio.shield(task)
                break
            except asyncio.CancelledError:
                cancellation_seen = True
                continue
            except BaseException:
                return False, cancellation_seen
        try:
            task.result()
        except BaseException:
            return False, cancellation_seen
        return True, cancellation_seen

    @staticmethod
    async def _response_body(response: Any) -> bytes:
        """Read one complete browser response, rejecting oversized payloads."""
        reader = getattr(response, "body", None)
        if not callable(reader):
            raise IdentityProtocolError("identity_unavailable")
        try:
            body = await BrowserProfileKernel._maybe_await(reader())
        except BaseException:
            raise IdentityProtocolError("identity_unavailable")
        if isinstance(body, str):
            body = body.encode("utf-8")
        if not isinstance(body, (bytes, bytearray)):
            raise IdentityProtocolError("identity_unavailable")
        body = bytes(body)
        if len(body) > MAX_RESPONSE_BYTES:
            raise IdentityProtocolError("identity_unavailable")
        return body

    @staticmethod
    def _body_from_selenium(driver: Any) -> bytes:
        try:
            value = driver.execute_script(
                "return document.body ? document.body.innerText : '';"
            )
        except BaseException:
            raise IdentityProtocolError("identity_unavailable")
        if isinstance(value, str):
            body = value.encode("utf-8")
        elif isinstance(value, (bytes, bytearray)):
            body = bytes(value)
        else:
            raise IdentityProtocolError("identity_unavailable")
        if len(body) > MAX_RESPONSE_BYTES:
            raise IdentityProtocolError("identity_unavailable")
        return body

    @staticmethod
    def _identity_auth_facts(
        identity: IdentityObservation,
        *,
        expected_email: Optional[str],
        manifest: Optional[Dict[str, Any]],
        text: str,
        cookies: Any,
        origin: str,
        application_shell: Optional[bool],
    ) -> Dict[str, Any]:
        """Combine provider proof with the existing cookie/shell protocol."""
        if identity.status != "authenticated" or not _session_identity._proof_is_current(
            identity.proof
        ):
            return {
                "authenticated": False,
                "status": identity.status,
                "code": identity.error_code or identity.status,
                "signals": _semantic_signals(text),
                "cookie_names": _cookie_names(cookies),
                "auth_cookie": False,
                "trusted_origin": False,
                "application_shell": application_shell is True,
                "identity_bound": False,
                "identity_fresh": False,
                "identity_confidence": "unknown",
                "observed_email": None,
                "session_slot": None,
                "_evidence_token": None,
                "_provider_proof_valid": False,
            }

        proof = identity.proof
        facts = classify_session_auth(
            text=text,
            cookies=cookies,
            observed_email=proof.observed_email,
            expected_email=expected_email,
            manifest=manifest,
            origin=origin,
            application_shell=application_shell,
        )
        provider_valid = _session_identity._proof_is_current(proof)
        if not provider_valid:
            facts.update({
                "authenticated": False,
                "status": "identity_unavailable",
                "code": "identity_unavailable",
                "_evidence_token": None,
            })
        elif not facts.get("authenticated"):
            # Preserve the classifier's explicit cookie/shell/login/challenge
            # result.  The provider proof alone is never session auth.
            facts["_evidence_token"] = None
        facts["observed_email"] = proof.observed_email
        facts["session_slot"] = proof.session_slot
        facts["_provider_proof_valid"] = provider_valid
        return facts

    async def _fetch_identity_playwright(
        self,
        context: Any,
        business_page: Any,
        *,
        expected_email: Optional[str] = None,
        manifest_email: Optional[str] = None,
        timeout_ms: int = NAVIGATION_TIMEOUT_MS,
    ) -> IdentityObservation:
        """Fetch provider identity in a temporary page of the same context."""
        business_url = _safe_url(getattr(business_page, "url", ""))
        slot = parse_gmail_session_slot(business_url)
        if slot is None or context is None or not callable(getattr(context, "new_page", None)):
            return IdentityObservation("identity_unavailable", None, "identity_unavailable")
        temporary = None
        observation = IdentityObservation("identity_unavailable", None, "identity_unavailable")
        cancellation_seen = False
        try:
            temporary = await self._maybe_await(context.new_page())
            if temporary is None:
                return observation
            response = await self._maybe_await(
                temporary.goto(
                    IDENTITY_ENDPOINT,
                    timeout=NAVIGATION_TIMEOUT_MS,
                    wait_until="domcontentloaded",
                )
            )
            body = await self._response_body(response)
            final_url = _safe_url(
                getattr(response, "url", None) or getattr(temporary, "url", "")
            )
            records = parse_google_accounts_v1(body, final_url)
            observation = resolve_session_identity(
                records,
                session_slot=slot,
                expected_email=expected_email,
                manifest_email=manifest_email,
                final_origin=business_url,
            )
        except IdentityProtocolError as exc:
            observation = IdentityObservation(exc.status, None, exc.code)
        except asyncio.CancelledError:
            cancellation_seen = True
            observation = IdentityObservation("cleanup_failed", None, "cleanup_failed")
        except BaseException:
            observation = IdentityObservation(
                "identity_unavailable", None, "identity_unavailable"
            )
        finally:
            if temporary is not None:
                closed, close_cancelled = await self._close_page_uncancelled(temporary)
                cancellation_seen = cancellation_seen or close_cancelled
                if not closed:
                    observation = IdentityObservation(
                        "cleanup_failed", None, "cleanup_failed"
                    )
        if cancellation_seen:
            raise asyncio.CancelledError()
        return observation

    def _fetch_identity_selenium(
        self,
        driver: Any,
        *,
        expected_email: Optional[str] = None,
        manifest_email: Optional[str] = None,
        timeout_ms: int = NAVIGATION_TIMEOUT_MS,
    ) -> IdentityObservation:
        """Fetch provider identity in a temporary tab of the same driver."""
        business_url = _safe_url(getattr(driver, "current_url", ""))
        slot = parse_gmail_session_slot(business_url)
        if slot is None:
            return IdentityObservation("identity_unavailable", None, "identity_unavailable")
        try:
            original_handle = driver.current_window_handle
            original_handles = tuple(driver.window_handles)
        except BaseException:
            return IdentityObservation("identity_unavailable", None, "identity_unavailable")

        temporary_handle = None
        observation = IdentityObservation("identity_unavailable", None, "identity_unavailable")
        cleanup_failed = False
        try:
            switch_to = getattr(driver, "switch_to", None)
            new_window = getattr(switch_to, "new_window", None)
            if callable(new_window):
                new_window("tab")
            else:
                driver.execute_script("window.open('about:blank', '_blank');")
            handles_after_open = tuple(driver.window_handles)
            candidates = [item for item in handles_after_open if item not in original_handles]
            if len(candidates) != 1:
                raise IdentityProtocolError("identity_unavailable")
            temporary_handle = candidates[0]
            switch_to.window(temporary_handle)
            set_timeout = getattr(driver, "set_page_load_timeout", None)
            if callable(set_timeout):
                set_timeout(float(timeout_ms) / 1000.0)
            driver.get(IDENTITY_ENDPOINT)
            body = self._body_from_selenium(driver)
            final_url = _safe_url(getattr(driver, "current_url", ""))
            records = parse_google_accounts_v1(body, final_url)
            observation = resolve_session_identity(
                records,
                session_slot=slot,
                expected_email=expected_email,
                manifest_email=manifest_email,
                final_origin=business_url,
            )
        except IdentityProtocolError as exc:
            observation = IdentityObservation(exc.status, None, exc.code)
        except BaseException:
            observation = IdentityObservation(
                "identity_unavailable", None, "identity_unavailable"
            )
        finally:
            if temporary_handle is not None:
                try:
                    switch_to.window(temporary_handle)
                    driver.close()
                except BaseException:
                    cleanup_failed = True
            try:
                switch_to.window(original_handle)
            except BaseException:
                cleanup_failed = True
            try:
                final_handles = tuple(driver.window_handles)
                if set(final_handles) != set(original_handles):
                    cleanup_failed = True
                if driver.current_window_handle != original_handle:
                    cleanup_failed = True
            except BaseException:
                cleanup_failed = True
        if cleanup_failed:
            return IdentityObservation("cleanup_failed", None, "cleanup_failed")
        return observation

    @staticmethod
    async def _page_text(page: Any) -> str:
        try:
            return await page.content()
        except Exception:
            try:
                value = await page.evaluate("() => document.body ? document.body.innerText : ''")
                return str(value or "")
            except Exception:
                return ""

    @staticmethod
    async def _playwright_application_shell(page: Any) -> Optional[bool]:
        try:
            value = await page.evaluate("""() => Boolean(
                document.querySelector('[role="main"], [aria-label*="Inbox" i],
                    a[href*="#inbox"], [data-view-id], [data-mail-shell])
            )""")
            return value if isinstance(value, bool) else None
        except Exception:
            return None

    @staticmethod
    def _selenium_application_shell(driver: Any) -> Optional[bool]:
        try:
            value = driver.execute_script("""return Boolean(
                document.querySelector('[role="main"], [aria-label*="Inbox" i],
                    a[href*="#inbox"], [data-view-id], [data-mail-shell])
            );""")
            return value if isinstance(value, bool) else None
        except Exception:
            return None

    async def probe_playwright(self, handle: ProfileHandle, expected_email: Optional[str] = None,
                               proxy: Optional[str] = None, timeout: int = 30000) -> BrowserObservation:
        profile_lease = None
        manager = None
        adapter_launch_started = False
        observation = None
        try:
            # Adapter import, construction, probing, and close all happen
            # inside the same lease.  Closing in the inner ``finally`` is
            # important: Chromium may still hold its user-data lock when the
            # outer context manager starts releasing the OS lease.
            with self.runtime.lease(handle, "health") as profile_lease:
                stable_check = getattr(profile_lease, "assert_stable", None)
                try:
                    if callable(stable_check):
                        stable_check()
                except asyncio.CancelledError:
                    raise
                except BaseException as exc:
                    observation = _apply_probe_exception(
                        None, self.runtime, handle, "playwright", exc
                    )
                    return observation
                try:
                    manifest = self._validate_browser_operation(
                        handle, "playwright", expected_email, proxy
                    )
                    from core.stealth_browser import PlaywrightStealthManager
                    # Construction may spawn Chromium before returning (or
                    # raise after spawning it).  Remember that an adapter
                    # launch was attempted so a missing manager cannot be
                    # mistaken for a process-free path during cleanup.
                    adapter_launch_started = True
                    manager = PlaywrightStealthManager()
                    initialized = await self._maybe_await(manager.initialize(
                        proxy=proxy, profile_path=str(handle.path),
                        profile_manifest=manifest, lease_owned=True,
                        profile_lease=profile_lease,
                        purpose="health",
                    ))
                    if not initialized:
                        observation = BrowserObservation(
                            _expected_email=expected_email,
                            code="runtime_unavailable",
                            profile_id=handle.profile_id, engine="playwright",
                        )
                        return observation
                    # Validate the actual channel/version before the first
                    # page navigation.  A persisted profile must never be
                    # probed through a different runtime.
                    runtime_info_getter = getattr(manager, "get_runtime_info", None)
                    runtime_info = (
                        await self._maybe_await(runtime_info_getter())
                        if callable(runtime_info_getter) else {}
                    )
                    if runtime_info:
                        try:
                            self.runtime.record_runtime(handle, runtime_info)
                        except ProfileRuntimeMismatchError as exc:
                            observation = BrowserObservation(
                                _expected_email=expected_email,
                                code=exc.code, error=exc.code,
                                profile_id=handle.profile_id, engine="playwright",
                            )
                            return observation
                    page = manager.page
                    response = await self._maybe_await(page.goto(
                        "https://mail.google.com/", timeout=timeout,
                        wait_until="domcontentloaded"
                    ))
                    try:
                        await self._maybe_await(page.wait_for_timeout(500))
                    except Exception:
                        pass
                    url = _safe_url(getattr(page, "url", ""))
                    text = await self._page_text(page)
                    application_shell = await self._playwright_application_shell(page)
                    cookies = []
                    try:
                        cookies = await self._maybe_await(manager.context.cookies())
                    except Exception:
                        pass
                    identity = await self._fetch_identity_playwright(
                        manager.context,
                        page,
                        expected_email=expected_email,
                        manifest_email=manifest.get("email"),
                    )
                    facts = self._identity_auth_facts(
                        identity,
                        expected_email=expected_email,
                        manifest=manifest,
                        text=text,
                        cookies=cookies,
                        origin=url,
                        application_shell=application_shell,
                    )
                    signals = facts["signals"]
                    observation = BrowserObservation(
                        _expected_email=expected_email,
                        _identity_bound=facts.get("identity_bound"),
                        _evidence_token=facts.get("_evidence_token"),
                        final_url=url, origin=_origin(url),
                        response_status=getattr(response, "status", None),
                        redirect_chain=[], auth_cookie_names=facts["cookie_names"],
                        auth_cookie=facts["auth_cookie"], observed_email=facts.get("observed_email"),
                        application_shell=facts["application_shell"],
                        session_slot=facts.get("session_slot"),
                        identity_proof=facts.get("_provider_proof_valid") is True,
                        identity_state=manifest.get("identity_state", ""),
                        identity_verified=manifest.get("identity_verified") is True,
                        identity_confidence=facts["identity_confidence"],
                        authenticated=facts["authenticated"], code=facts["code"],
                        profile_id=handle.profile_id, engine="playwright",
                        **signals,
                    )
                    return observation
                except ProfileRuntimeError as exc:
                    observation = BrowserObservation(
                        _expected_email=expected_email,
                        code=exc.code, error=exc.code,
                        profile_id=getattr(handle, "profile_id", ""),
                        engine="playwright",
                    )
                    return observation
                except Exception as exc:
                    observation = BrowserObservation(
                        _expected_email=expected_email,
                        code="runtime_unavailable", error=type(exc).__name__,
                        profile_id=getattr(handle, "profile_id", ""),
                        engine="playwright",
                    )
                    return observation
                finally:
                    cancellation_seen = False
                    if manager is not None:
                        cleanup, cancellation_seen = await _close_async_manager_uncancelled(
                            manager
                        )
                        observation = _apply_probe_cleanup(
                            observation, self.runtime, handle, "playwright", cleanup
                        )
                        if cancellation_seen:
                            # Preserve cancellation semantics after the owned
                            # browser has been closed and its result recorded.
                            raise asyncio.CancelledError()
                    elif adapter_launch_started:
                        observation = _apply_probe_cleanup(
                            observation, self.runtime, handle, "playwright",
                            {"success": False, "browser_process_stopped": False},
                        )
                    elif observation is not None:
                        observation = _apply_probe_cleanup(
                            observation, self.runtime, handle, "playwright", None
                        )
                    try:
                        if callable(stable_check):
                            stable_check()
                    except BaseException as exc:
                        observation = _apply_probe_exception(
                            observation, self.runtime, handle, "playwright", exc
                        )
                    if cancellation_seen:
                        # Preserve cancellation only after cleanup and the
                        # post-cleanup lease integrity check have completed.
                        raise asyncio.CancelledError()
        except ProfileRuntimeError as exc:
            if observation is not None:
                observation = _apply_probe_exception(
                    observation, self.runtime, handle, "playwright",
                    RuntimeError("probe lease exit failed"),
                )
                return observation
            return BrowserObservation(_expected_email=expected_email,
                                      code=exc.code, error=exc.code,
                                      profile_id=getattr(handle, "profile_id", ""),
                                      engine="playwright")
        except asyncio.CancelledError:
            if observation is not None:
                _apply_probe_exception(
                    observation, self.runtime, handle, "playwright",
                    asyncio.CancelledError(),
                )
            raise
        except BaseException as exc:
            if observation is not None:
                observation = _apply_probe_exception(
                    observation, self.runtime, handle, "playwright", exc
                )
                return observation
            return BrowserObservation(_expected_email=expected_email,
                                      code="runtime_unavailable", error=type(exc).__name__,
                                      profile_id=getattr(handle, "profile_id", ""),
                                      engine="playwright")
        finally:
            _apply_probe_lease_release(
                observation, self.runtime, handle, profile_lease
            )

    def probe_selenium(self, handle: ProfileHandle, expected_email: Optional[str] = None,
                       proxy: Optional[str] = None, timeout: int = 30) -> BrowserObservation:
        profile_lease = None
        driver = None
        adapter_launch_started = False
        observation = None
        try:
            with self.runtime.lease(handle, "health") as profile_lease:
                stable_check = getattr(profile_lease, "assert_stable", None)
                try:
                    if callable(stable_check):
                        stable_check()
                except asyncio.CancelledError:
                    raise
                except BaseException as exc:
                    observation = _apply_probe_exception(
                        None, self.runtime, handle, "selenium", exc
                    )
                    return observation
                try:
                    manifest = self._validate_browser_operation(
                        handle, "selenium", expected_email, proxy
                    )
                    from core.selenium_runner import create_driver
                    # Driver creation can start a service process before it
                    # raises.  A failed factory therefore requires the same
                    # fail-closed cleanup result as a returned live driver.
                    adapter_launch_started = True
                    driver = create_driver(
                        proxy=proxy, profile_path=str(handle.path),
                        profile_manifest=manifest, lease_owned=True,
                        profile_lease=profile_lease,
                        purpose="health",
                    )
                    if not driver:
                        observation = BrowserObservation(
                            _expected_email=expected_email,
                            code="runtime_unavailable", error="driver unavailable",
                            profile_id=handle.profile_id, engine="selenium",
                        )
                        return observation
                    try:
                        from core.selenium_runner import get_runtime_info
                    except (ImportError, AttributeError):
                        get_runtime_info = lambda value: getattr(
                            value, "_profile_runtime_info", {}
                        )
                    runtime_info = get_runtime_info(driver) or {}
                    if runtime_info:
                        try:
                            self.runtime.record_runtime(handle, runtime_info)
                        except ProfileRuntimeMismatchError as exc:
                            observation = BrowserObservation(
                                _expected_email=expected_email,
                                code=exc.code, error=exc.code,
                                profile_id=handle.profile_id, engine="selenium",
                            )
                            return observation
                    driver.get("https://mail.google.com/")
                    url = _safe_url(getattr(driver, "current_url", ""))
                    source = str(getattr(driver, "page_source", "") or "")
                    application_shell = self._selenium_application_shell(driver)
                    cookies = []
                    try:
                        cookies = driver.get_cookies()
                    except Exception:
                        pass
                    identity = self._fetch_identity_selenium(
                        driver,
                        expected_email=expected_email,
                        manifest_email=manifest.get("email"),
                    )
                    facts = self._identity_auth_facts(
                        identity,
                        expected_email=expected_email,
                        manifest=manifest,
                        text=source,
                        cookies=cookies,
                        origin=url,
                        application_shell=application_shell,
                    )
                    signals = facts["signals"]
                    observation = BrowserObservation(
                        _expected_email=expected_email,
                        _identity_bound=facts.get("identity_bound"),
                        _evidence_token=facts.get("_evidence_token"),
                        final_url=url, origin=_origin(url), response_status=None,
                        redirect_chain=[], auth_cookie_names=facts["cookie_names"],
                        auth_cookie=facts["auth_cookie"], observed_email=facts.get("observed_email"),
                        application_shell=facts["application_shell"],
                        session_slot=facts.get("session_slot"),
                        identity_proof=facts.get("_provider_proof_valid") is True,
                        identity_state=manifest.get("identity_state", ""),
                        identity_verified=manifest.get("identity_verified") is True,
                        identity_confidence=facts["identity_confidence"],
                        authenticated=facts["authenticated"], code=facts["code"],
                        profile_id=handle.profile_id, engine="selenium", **signals,
                    )
                    return observation
                except ProfileRuntimeError as exc:
                    observation = BrowserObservation(
                        _expected_email=expected_email,
                        code=exc.code, error=exc.code,
                        profile_id=getattr(handle, "profile_id", ""),
                        engine="selenium",
                    )
                    return observation
                except Exception as exc:
                    observation = BrowserObservation(
                        _expected_email=expected_email,
                        code="runtime_unavailable", error=type(exc).__name__,
                        profile_id=getattr(handle, "profile_id", ""),
                        engine="selenium",
                    )
                    return observation
                finally:
                    if driver is not None:
                        cleanup = {"success": False, "browser_process_stopped": False}
                        try:
                            quit_result = driver.quit()
                        except BaseException:
                            # ``CancelledError`` and interpreter-level
                            # shutdown exceptions inherit directly from
                            # ``BaseException`` on supported Python versions.
                            # They still must become an explicit cleanup
                            # failure before the profile lease is released.
                            cleanup = {
                                "success": False,
                                "browser_process_stopped": False,
                            }
                        else:
                            # Selenium's quit result is advisory; the process
                            # inspection is authoritative.  A missing fact is
                            # unknown and must not become a successful probe.
                            quit_result_missing = quit_result is None
                            if isinstance(quit_result, dict):
                                cleanup["success"] = quit_result.get("success") is True
                                cleanup["browser_process_stopped"] = (
                                    quit_result.get("browser_process_stopped") is True
                                )
                            elif quit_result_missing:
                                # Selenium's normal quit() API returns None;
                                # process inspection below supplies the
                                # required success fact.
                                cleanup["success"] = False
                            observed = inspect_process_stopped(driver)
                            if observed is True:
                                cleanup["browser_process_stopped"] = True
                                if quit_result_missing:
                                    cleanup["success"] = True
                            else:
                                cleanup["browser_process_stopped"] = False
                                cleanup["success"] = False
                        observation = _apply_probe_cleanup(
                            observation, self.runtime, handle, "selenium", cleanup
                        )
                    elif adapter_launch_started:
                        observation = _apply_probe_cleanup(
                            observation, self.runtime, handle, "selenium",
                            {"success": False, "browser_process_stopped": False},
                        )
                    elif observation is not None:
                        observation = _apply_probe_cleanup(
                            observation, self.runtime, handle, "selenium", None
                        )
                    try:
                        if callable(stable_check):
                            stable_check()
                    except BaseException as exc:
                        observation = _apply_probe_exception(
                            observation, self.runtime, handle, "selenium", exc
                        )
        except ProfileRuntimeError as exc:
            if observation is not None:
                observation = _apply_probe_exception(
                    observation, self.runtime, handle, "selenium",
                    RuntimeError("probe lease exit failed"),
                )
                return observation
            return BrowserObservation(_expected_email=expected_email,
                                      code=exc.code, error=exc.code,
                                      profile_id=getattr(handle, "profile_id", ""),
                                      engine="selenium")
        except asyncio.CancelledError:
            if observation is not None:
                _apply_probe_exception(
                    observation, self.runtime, handle, "selenium",
                    asyncio.CancelledError(),
                )
            raise
        except BaseException as exc:
            if observation is not None:
                observation = _apply_probe_exception(
                    observation, self.runtime, handle, "selenium", exc
                )
                return observation
            return BrowserObservation(_expected_email=expected_email,
                                      code="runtime_unavailable", error=type(exc).__name__,
                                      profile_id=getattr(handle, "profile_id", ""),
                                      engine="selenium")
        finally:
            _apply_probe_lease_release(
                observation, self.runtime, handle, profile_lease
            )

    async def login_and_warm(self, handle: ProfileHandle, email: str, password: str,
                             duration_minutes: int = 3, proxy: Optional[str] = None,
                             expected_engine: Optional[str] = None):
        """Open the recorded engine, login only when needed, and warm it.

        The return value is a small mapping for callers that need diagnostics;
        ``success`` is deliberately separate from the normalized browser status.
        """
        manifest = self.runtime.load(handle)
        if manifest.get("email", "").lower() != email.lower():
            raise ProfileConflictError("profile is bound to a different account")
        if expected_engine is not None and manifest.get("engine") != expected_engine:
            raise ProfileConflictError("profile engine changed before warm dispatch")
        if manifest.get("state") not in ("bound", "ready"):
            raise ProfileUnavailableError("profile is not ready for warming")
        self.runtime.validate_proxy(manifest, proxy)
        engine = manifest["engine"]
        if engine not in ("playwright", "selenium"):
            raise ProfileConflictError("profile is not bound to a supported warmer engine")
        if engine == "playwright":
            from core.account_warmer import _warm_playwright_session
            return await _warm_playwright_session(
                self.runtime, handle, manifest, email, password,
                duration_minutes, proxy=proxy,
            )
        from core.account_warmer import _warm_selenium_session
        return _warm_selenium_session(
            self.runtime, handle, manifest, email, password,
            duration_minutes, proxy=proxy,
        )


__all__ = [
    "ProfileRuntime", "ProfileHandle", "ProfileLease", "ProfileRuntimeError",
    "ProfileConflictError", "ProfileUnavailableError", "ProfileRuntimeMismatchError",
    "ProfileBusyError", "ProxyUnavailableError", "ProxyMismatchError",
    "build_identity", "validate_profile_identity", "proxy_binding", "proxy_launch_config", "classify_browser_observation",
    "derive_overall_status", "normalise_observed_email", "identity_is_verified",
    "classify_session_auth", "ENGINES",
    "inspect_process_stopped", "registration_cleanup_verified",
    "release_registration_lease",
    "BROWSER_STATUSES", "MAILBOX_STATUSES",
    "default_profile_runtime", "BrowserObservation", "BrowserProfileKernel",
]
