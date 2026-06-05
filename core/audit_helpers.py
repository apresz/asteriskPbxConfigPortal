from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import date, datetime, time
from decimal import Decimal
from pathlib import Path
from uuid import UUID


REDACTED_VALUE = "[redacted]"

SENSITIVE_FIELD_NAMES = frozenset(
    {
        "ami_secret",
        "ami_username",
        "agent_secret",
        "agent_token",
        "api_key",
        "api_key_id",
        "authorization",
        "deployment_asterisk_path",
        "deployment_reload_command",
        "deployment_ssh_host",
        "deployment_ssh_known_hosts",
        "deployment_ssh_port",
        "deployment_ssh_private_key",
        "deployment_ssh_username",
        "deployment_staging_path",
        "deployment_tftp_path",
        "key_hash",
        "password",
        "raw_secret",
        "secret",
        "sip_password",
        "sip_username",
        "smtp_password",
        "smtp_username",
        "token",
        "username",
        "voicemail_pin",
    }
)

SENSITIVE_FIELD_PARTS = (
    "api_key",
    "auth",
    "bearer",
    "credential",
    "key_hash",
    "password",
    "private_key",
    "raw_secret",
    "secret",
    "token",
)

USERNAME_FIELD_SUFFIXES = ("_username",)

SUMMARY_EXCLUDED_FIELDS = frozenset({"created_at", "updated_at"})


def is_sensitive_audit_field(field_name: object) -> bool:
    normalized = str(field_name or "").strip().lower()
    if not normalized:
        return False
    leaf_name = normalized.rsplit(".", 1)[-1]
    return (
        leaf_name in SENSITIVE_FIELD_NAMES
        or leaf_name.endswith(USERNAME_FIELD_SUFFIXES)
        or any(part in leaf_name for part in SENSITIVE_FIELD_PARTS)
    )


def redact_audit_value(field_name: object, value):
    if is_sensitive_audit_field(field_name):
        return REDACTED_VALUE
    if isinstance(value, Mapping):
        return {str(key): redact_audit_value(key, nested_value) for key, nested_value in value.items()}
    if _is_sequence(value):
        return [redact_audit_value(field_name, item) for item in value]
    return json_safe_audit_value(value)


def redact_audit_mapping(values: Mapping | None) -> dict:
    if not values:
        return {}
    return {str(key): redact_audit_value(key, value) for key, value in values.items()}


def audit_model_label(instance) -> str:
    meta = getattr(instance, "_meta", None)
    label = getattr(meta, "label_lower", None)
    if label:
        return str(label)
    return instance.__class__.__name__


def audit_object_identity(instance) -> str:
    pk = getattr(instance, "pk", None)
    if pk is None:
        pk = getattr(instance, "id", None)
    label = _safe_string(instance)
    if pk not in (None, ""):
        return f"{pk}:{label}" if label else str(pk)
    for field_name in ("slug", "number", "mac_address", "code", "name"):
        value = getattr(instance, field_name, None)
        if value not in (None, ""):
            return str(value)
    return label or instance.__class__.__name__


def audit_target(model: str | None, object_identity: str | None) -> str:
    target_model = str(model or "unknown")
    identity = str(object_identity or "unknown")
    target = f"{target_model}/{identity}"
    return target[:255]


def audit_model_summary(instance, *, extra: Mapping | None = None, redact: bool = True) -> dict:
    if instance is None:
        return {}
    values = _django_field_values(instance)
    if values is None:
        values = _public_attribute_values(instance)
    if extra:
        values.update(extra)
    if redact:
        return redact_audit_mapping(values)
    return json_safe_audit_mapping(values)


def changed_audit_fields(before: Mapping | None, after: Mapping | None) -> list[str]:
    before = before or {}
    after = after or {}
    return sorted(key for key in set(before) | set(after) if before.get(key) != after.get(key))


def build_config_change_details(
    *,
    actor,
    operation: str,
    model: str,
    object_identity: str,
    outcome: str,
    before: Mapping | None,
    after: Mapping | None,
    source: str,
    extra_details: Mapping | None = None,
) -> dict:
    raw_before = before or {}
    raw_after = after or {}
    changed_fields = changed_audit_fields(raw_before, raw_after)
    redacted_before = redact_audit_mapping(raw_before)
    redacted_after = redact_audit_mapping(raw_after)
    details = {
        "source": source,
        "operation": operation,
        "model": model,
        "object_identity": object_identity,
        "result": outcome,
        "actor_username": _actor_username(actor),
        "before": redacted_before,
        "after": redacted_after,
        "changed_fields": changed_fields,
    }
    if extra_details:
        details.update(redact_audit_mapping(extra_details))
    return details


def json_safe_audit_value(value):
    if value is None or isinstance(value, (bool, int, float, str)):
        return value
    if isinstance(value, (datetime, date, time)):
        return value.isoformat()
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, UUID):
        return str(value)
    if isinstance(value, bytes):
        return f"<bytes:{len(value)}>"
    if isinstance(value, Path):
        return str(value)
    pk = getattr(value, "pk", None)
    if pk is not None:
        return {"model": audit_model_label(value), "id": pk, "label": _safe_string(value)}
    return _safe_string(value)


def json_safe_audit_mapping(values: Mapping | None) -> dict:
    if not values:
        return {}
    return {str(key): _json_safe_with_nested_values(value) for key, value in values.items()}


def _django_field_values(instance) -> dict | None:
    meta = getattr(instance, "_meta", None)
    fields = getattr(meta, "fields", None)
    if not fields:
        return None
    values = {}
    for field in fields:
        field_name = getattr(field, "name", None)
        if not field_name or field_name in SUMMARY_EXCLUDED_FIELDS:
            continue
        value_attr = getattr(field, "attname", field_name)
        values[field_name] = getattr(instance, value_attr, None)
    return values


def _public_attribute_values(instance) -> dict:
    values = {}
    for field_name, value in vars(instance).items():
        if field_name.startswith("_") or field_name in SUMMARY_EXCLUDED_FIELDS:
            continue
        if callable(value):
            continue
        values[field_name] = value
    return values


def _actor_username(actor) -> str | None:
    if actor is None:
        return None
    get_username = getattr(actor, "get_username", None)
    if callable(get_username):
        username = get_username()
    else:
        username = getattr(actor, "username", None)
    if username in (None, ""):
        return None
    return str(username)


def _safe_string(value) -> str:
    try:
        return str(value)
    except Exception:
        return value.__class__.__name__


def _is_sequence(value) -> bool:
    return isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray))


def _json_safe_with_nested_values(value):
    if isinstance(value, Mapping):
        return {str(key): _json_safe_with_nested_values(nested_value) for key, nested_value in value.items()}
    if _is_sequence(value):
        return [_json_safe_with_nested_values(item) for item in value]
    return json_safe_audit_value(value)
