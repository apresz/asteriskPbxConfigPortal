from collections.abc import Callable
from functools import wraps
from typing import Any

from django.contrib.auth.decorators import login_required
from django.core.exceptions import PermissionDenied
from django.http import HttpRequest, HttpResponse, JsonResponse

from .models import PortalPermission, PortalRole, PortalUserProfile
from .portal_area_access import ROLE_PERMISSIONS as ROLE_PERMISSION_VALUES


ROLE_PERMISSIONS = {
    PortalRole(role): frozenset(PortalPermission(permission) for permission in permissions)
    for role, permissions in ROLE_PERMISSION_VALUES.items()
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


def api_login_required(view_func: Callable[..., HttpResponse]) -> Callable[..., HttpResponse]:
    @wraps(view_func)
    def wrapper(request: HttpRequest, *args: Any, **kwargs: Any) -> HttpResponse:
        auth_error = getattr(request, "api_key_auth_error", "")
        if auth_error:
            return JsonResponse(
                {"error": auth_error},
                status=getattr(request, "api_key_auth_error_status", 401),
            )

        api_user = getattr(request, "api_user", None)
        if _is_active_user(api_user):
            request.user = api_user
        elif not _is_active_user(request.user):
            return JsonResponse({"error": "Authentication required."}, status=401)

        return view_func(request, *args, **kwargs)

    return wrapper


def api_permission_required(permission: PortalPermission | str) -> Callable:
    required_permission = PortalPermission(permission)

    def decorator(view_func: Callable[..., HttpResponse]) -> Callable[..., HttpResponse]:
        @api_login_required
        @wraps(view_func)
        def wrapper(request: HttpRequest, *args: Any, **kwargs: Any) -> HttpResponse:
            if not user_has_permission(request.user, required_permission):
                return JsonResponse({"error": "Permission denied."}, status=403)
            return view_func(request, *args, **kwargs)

        return wrapper

    return decorator


def _is_active_user(user) -> bool:
    return bool(user and user.is_authenticated and user.is_active)
