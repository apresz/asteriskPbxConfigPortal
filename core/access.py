from collections.abc import Callable
from functools import wraps
from typing import Any

from django.contrib.auth.decorators import login_required
from django.core.exceptions import PermissionDenied
from django.http import HttpRequest, HttpResponse

from .models import PortalPermission, PortalRole, PortalUserProfile


ROLE_PERMISSIONS = {
    PortalRole.VIEWER: frozenset({PortalPermission.VIEW}),
    PortalRole.EDITOR: frozenset({PortalPermission.VIEW, PortalPermission.EDIT_CONFIG}),
    PortalRole.OPERATOR: frozenset(
        {PortalPermission.VIEW, PortalPermission.RUN_LIVE_OPERATIONS, PortalPermission.ACCESS_RECORDINGS}
    ),
    PortalRole.ADMIN: frozenset(
        {
            PortalPermission.VIEW,
            PortalPermission.EDIT_CONFIG,
            PortalPermission.RUN_LIVE_OPERATIONS,
            PortalPermission.ACCESS_RECORDINGS,
            PortalPermission.ADMINISTER,
        }
    ),
}


def assign_role(user, role: PortalRole | str) -> PortalUserProfile:
    portal_role = PortalRole(role)
    profile, _created = PortalUserProfile.objects.update_or_create(
        user=user,
        defaults={"role": portal_role},
    )
    return profile


def get_user_role(user) -> PortalRole | None:
    if not _is_active_user(user):
        return None
    if user.is_superuser:
        return PortalRole.ADMIN

    profile, _created = PortalUserProfile.objects.get_or_create(user=user)
    return PortalRole(profile.role)


def get_user_role_label(user) -> str:
    role = get_user_role(user)
    if role is None:
        return ""
    return role.label


def get_role_permissions(role: PortalRole | str | None) -> frozenset[PortalPermission]:
    if role is None:
        return frozenset()
    return ROLE_PERMISSIONS[PortalRole(role)]


def get_user_permissions(user) -> frozenset[PortalPermission]:
    return get_role_permissions(get_user_role(user))


def role_has_permission(role: PortalRole | str | None, permission: PortalPermission | str) -> bool:
    return PortalPermission(permission) in get_role_permissions(role)


def user_has_permission(user, permission: PortalPermission | str) -> bool:
    return PortalPermission(permission) in get_user_permissions(user)


def permission_required(permission: PortalPermission | str) -> Callable:
    required_permission = PortalPermission(permission)

    def decorator(view_func: Callable[..., HttpResponse]) -> Callable[..., HttpResponse]:
        @login_required
        @wraps(view_func)
        def wrapper(request: HttpRequest, *args: Any, **kwargs: Any) -> HttpResponse:
            if not user_has_permission(request.user, required_permission):
                raise PermissionDenied
            return view_func(request, *args, **kwargs)

        return wrapper

    return decorator


def _is_active_user(user) -> bool:
    return bool(user and user.is_authenticated and user.is_active)
