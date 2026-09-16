"""Declarative capability matrix for the supported browser adapters.

The matrix is intentionally separate from launch code.  It gives callers a
stable answer about what an engine can apply natively, what depends on the
underlying Chromium/driver, and what is refused.  Runtime smoke tests can add
verification evidence through :func:`capability_report` without mutating the
contract shared by workers.
"""

from copy import deepcopy
from typing import Any, Dict, Mapping, Optional


CAPABILITY_STATUSES = frozenset({
    "native", "best_effort", "unsupported", "not_verified",
})

CAPABILITY_KEYS = (
    "user_agent",
    "viewport",
    "locale",
    "accept_language",
    "timezone",
    "geolocation",
    "browser_channel_version",
    "persistent_storage",
    "proxy_endpoint",
    "proxy_credentials",
)


# Keep this table literal and reviewable.  A status describes the adapter
# contract, not whether a particular machine currently has Chrome installed.
_CAPABILITY_MATRIX = {
    "playwright": {
        "user_agent": "native",
        "viewport": "native",
        "locale": "native",
        "accept_language": "native",
        "timezone": "native",
        "geolocation": "native",
        "browser_channel_version": "best_effort",
        "persistent_storage": "native",
        "proxy_endpoint": "native",
        "proxy_credentials": "native",
    },
    "selenium": {
        "user_agent": "native",
        "viewport": "native",
        "locale": "native",
        "accept_language": "native",
        "timezone": "best_effort",
        "geolocation": "best_effort",
        "browser_channel_version": "not_verified",
        "persistent_storage": "native",
        "proxy_endpoint": "native",
        "proxy_credentials": "unsupported",
    },
}

_CAPABILITY_REASONS = {
    "playwright": {
        "user_agent": "Applied through the persistent browser context",
        "viewport": "Applied through the persistent browser context",
        "locale": "Applied through locale context options",
        "accept_language": "Derived from the persisted locale",
        "timezone": "Applied through timezone context options",
        "geolocation": "Applied through geolocation context options",
        "browser_channel_version": "Channel is selected natively; installed major is runtime-verified",
        "persistent_storage": "Uses the profile directory as a persistent context",
        "proxy_endpoint": "Applied through browser launch proxy options",
        "proxy_credentials": "Applied by the Playwright proxy credential bridge",
    },
    "selenium": {
        "user_agent": "Applied through Chrome command-line options",
        "viewport": "Applied to the content viewport through Chromium device metrics",
        "locale": "Applied through Chrome language preferences and Chromium locale override",
        "accept_language": "Derived from the persisted locale through Chrome language preferences",
        "timezone": "Best effort through CDP when the driver exposes it",
        "geolocation": "Best effort through CDP permission and override APIs",
        "browser_channel_version": "Requires a matching local Chrome/ChromeDriver pair",
        "persistent_storage": "Uses the profile directory as Chrome user-data-dir",
        "proxy_endpoint": "Applied through Chrome proxy-server options",
        "proxy_credentials": "Unsupported without a separate credential extension/bridge",
    },
}


def _engine_name(engine: str) -> str:
    value = str(engine or "").strip().lower()
    if value not in _CAPABILITY_MATRIX:
        raise ValueError("unsupported browser engine")
    return value


def get_capability_matrix() -> Dict[str, Dict[str, str]]:
    """Return a detached copy of the complete engine capability matrix."""
    return deepcopy(_CAPABILITY_MATRIX)


def get_engine_capabilities(engine: str) -> Dict[str, str]:
    """Return the capability statuses for one supported engine."""
    return deepcopy(_CAPABILITY_MATRIX[_engine_name(engine)])


def capability_status(engine: str, capability: str) -> str:
    """Return one status, failing closed for unknown dimensions."""
    engine_name = _engine_name(engine)
    key = str(capability or "").strip().lower()
    if key not in CAPABILITY_KEYS:
        raise ValueError("unknown browser capability")
    return _CAPABILITY_MATRIX[engine_name][key]


def capability_report(engine: str, observed: Optional[Mapping[str, Any]] = None
                      ) -> Dict[str, Dict[str, Any]]:
    """Combine static support with optional local smoke observations.

    ``observed`` is a mapping of capability names to truthy/falsey evidence.
    Missing keys are deliberately reported as ``verified=False`` rather than
    inferred from the static status.  This prevents a capability matrix from
    being mistaken for proof that a particular browser installation applied
    every option.
    """
    statuses = get_engine_capabilities(engine)
    observed = observed or {}
    if not isinstance(observed, Mapping):
        raise ValueError("observed capabilities must be a mapping")
    unknown = set(observed) - set(CAPABILITY_KEYS)
    if unknown:
        raise ValueError("unknown observed browser capability")
    if any(type(value) is not bool for value in observed.values()):
        raise ValueError("observed capability evidence must be boolean")
    engine_name = _engine_name(engine)
    return {
        key: {
            "status": status,
            "verified": (
                type(observed[key]) is bool and observed[key]
                and status != "unsupported"
            ) if key in observed else False,
            "reason": _CAPABILITY_REASONS[engine_name][key],
        }
        for key, status in statuses.items()
    }


# Readable aliases for integrations that prefer noun-oriented names.
ENGINE_CAPABILITIES = _CAPABILITY_MATRIX
browser_capability_matrix = get_capability_matrix
identity_capability_matrix = get_capability_matrix


__all__ = [
    "CAPABILITY_KEYS", "CAPABILITY_STATUSES", "ENGINE_CAPABILITIES",
    "get_capability_matrix", "get_engine_capabilities", "capability_status",
    "capability_report", "browser_capability_matrix",
    "identity_capability_matrix",
]
