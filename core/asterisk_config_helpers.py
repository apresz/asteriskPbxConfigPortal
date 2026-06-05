from __future__ import annotations

import re
from typing import Any, Iterable

SIP_TRUNK_TYPE = "sip"
IAX2_TRUNK_TYPE = "iax2"
REDACTED_VALUE = "[redacted]"
SENSITIVE_DETAIL_KEYS = frozenset(
    {
        "api_key",
        "apikey",
        "authorization",
        "credential",
        "credentials",
        "key_hash",
        "password",
        "private_key",
        "secret",
        "token",
    }
)


def slug(value: str) -> str:
    normalized = re.sub(r"[^a-zA-Z0-9_-]+", "-", str(value).strip().lower()).strip("-")
    return normalized or "default"


def trunk_section_name(trunk: Any) -> str:
    return f"trunk-{slug(getattr(trunk, 'name', 'default'))}"


def trunk_type_value(trunk: Any) -> str:
    raw_value = getattr(trunk, "trunk_type", "")
    return str(getattr(raw_value, "value", raw_value)).lower()


def is_iax2_trunk(trunk: Any) -> bool:
    return trunk_type_value(trunk) == IAX2_TRUNK_TYPE


def dial_target(trunk: Any) -> str:
    section = trunk_section_name(trunk)
    if is_iax2_trunk(trunk):
        return f"IAX2/{section}/${{EXTEN}}"
    return f"PJSIP/${{EXTEN}}@{section}"


def active_route_trunks(route_trunk_links: Iterable[Any]) -> list[Any]:
    links = [link for link in route_trunk_links if getattr(getattr(link, "trunk", None), "is_active", False)]
    return sorted(links, key=lambda link: (getattr(link, "priority", 0), getattr(link.trunk, "name", "")))


def route_dial_targets(route_trunk_links: Iterable[Any]) -> list[str]:
    return [dial_target(link.trunk) for link in active_route_trunks(route_trunk_links)]


def iax2_provider_trunk_lines(trunk: Any) -> list[str]:
    section = trunk_section_name(trunk)
    return [
        f"[{section}]",
        "type=friend",
        f"host={getattr(trunk, 'host', '')}",
        f"username={getattr(trunk, 'username', '')}",
        f"secret={getattr(trunk, 'password', '')}",
        "context=inbound",
        "trunk=yes",
        "qualify=yes",
        "",
    ]


def provider_credential_warning(trunk: Any) -> dict[str, Any] | None:
    missing = [field for field in ("host", "username", "password") if not getattr(trunk, field, "")]
    if not missing:
        return None
    provider = getattr(trunk, "provider", None)
    return {
        "code": "provider_trunk_missing_credentials",
        "provider": getattr(provider, "name", ""),
        "trunk": getattr(trunk, "name", ""),
        "trunk_type": trunk_type_value(trunk),
        "missing": missing,
        "emergency_capable": bool(getattr(trunk, "is_emergency_capable", False)),
        "message": f"{getattr(trunk, 'name', '')} is missing {', '.join(missing)}.",
    }


def provider_credential_warnings_for_trunks(trunks: Iterable[Any]) -> list[dict[str, Any]]:
    return [
        warning
        for warning in (provider_credential_warning(trunk) for trunk in trunks)
        if warning is not None
    ]


def emergency_trunk_missing_credential_errors(
    route_name: str,
    route_trunks: Iterable[Any],
    warning_trunks: dict[str, dict[str, Any]],
) -> list[dict[str, Any]]:
    errors = []
    for trunk in route_trunks:
        warning = warning_trunks.get(getattr(trunk, "name", ""))
        if warning:
            errors.append(
                {
                    "code": "emergency_trunk_missing_credentials",
                    "route": route_name,
                    "trunk": getattr(trunk, "name", ""),
                    "missing": warning["missing"],
                    "message": "Emergency-capable trunks need complete provider credentials.",
                }
            )
    return errors


def redact_sensitive_details(value: Any) -> Any:
    if isinstance(value, dict):
        redacted = {}
        for key, nested_value in value.items():
            if _is_sensitive_key(key):
                redacted[key] = REDACTED_VALUE
            else:
                redacted[key] = redact_sensitive_details(nested_value)
        return redacted
    if isinstance(value, list):
        return [redact_sensitive_details(item) for item in value]
    if isinstance(value, tuple):
        return tuple(redact_sensitive_details(item) for item in value)
    return value


def _is_sensitive_key(key: Any) -> bool:
    normalized = str(key).lower().replace("-", "_")
    return normalized in SENSITIVE_DETAIL_KEYS or normalized.endswith("_password") or normalized.endswith("_secret")
