from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from typing import Any


SERVICE_PERMISSION_VALUES = (
    "view",
    "edit_config",
    "run_live_operations",
    "access_recordings",
    "administer",
)
SERVICE_PERMISSION_SET = frozenset(SERVICE_PERMISSION_VALUES)


class ServicePermissionError(ValueError):
    pass


@dataclass(frozen=True)
class ServicePrincipal:
    service_identity: Any
    permissions: frozenset[str]

    @property
    def is_service_principal(self) -> bool:
        return True

    @property
    def is_authenticated(self) -> bool:
        # Service principals are authenticated for API access, but are not Django users.
        return False

    @property
    def is_active(self) -> bool:
        return bool(getattr(self.service_identity, "is_active", False))

    @property
    def id(self):
        return getattr(self.service_identity, "id", None)

    @property
    def pk(self):
        return self.id

    def get_username(self) -> str:
        return service_identity_audit_label(self.service_identity)


def normalize_service_permissions(raw_permissions: object) -> tuple[str, ...]:
    if raw_permissions is None:
        return ()
    if isinstance(raw_permissions, (str, Mapping)) or not isinstance(raw_permissions, Iterable):
        raise ServicePermissionError("Service identity permissions must be a list of permission strings.")

    seen: set[str] = set()
    for raw_permission in raw_permissions:
        permission = _permission_value(raw_permission).strip()
        if permission not in SERVICE_PERMISSION_SET:
            raise ServicePermissionError(f"Unsupported service identity permission: {permission}")
        seen.add(permission)

    return tuple(permission for permission in SERVICE_PERMISSION_VALUES if permission in seen)


def service_principal_from_identity(service_identity) -> ServicePrincipal:
    if service_identity is None:
        raise ServicePermissionError("Service identity is required.")
    return ServicePrincipal(
        service_identity=service_identity,
        permissions=frozenset(normalize_service_permissions(getattr(service_identity, "permissions", ()))),
    )


def is_service_principal(principal) -> bool:
    return bool(getattr(principal, "is_service_principal", False))


def service_principal_has_permission(principal, permission: object) -> bool:
    if not is_service_principal(principal):
        return False
    return _permission_value(permission) in principal.permissions


def service_identity_audit_label(service_identity) -> str:
    slug = str(getattr(service_identity, "slug", "") or "").strip()
    name = str(getattr(service_identity, "name", "") or "").strip()
    identifier = slug or name or str(getattr(service_identity, "id", "") or "unknown")
    return f"service:{identifier}"


def service_identity_audit_details(service_identity, *, prefix: str = "service_identity") -> dict[str, object]:
    return {
        f"{prefix}_id": getattr(service_identity, "id", None),
        f"{prefix}_name": getattr(service_identity, "name", ""),
        f"{prefix}_slug": getattr(service_identity, "slug", ""),
    }


def _permission_value(permission: object) -> str:
    value = getattr(permission, "value", permission)
    if not isinstance(value, str):
        raise ServicePermissionError("Service identity permissions must be strings.")
    return value
