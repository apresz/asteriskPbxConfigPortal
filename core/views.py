import json

from django.contrib.auth import get_user_model
from django.contrib.auth.decorators import login_required
from django.core.exceptions import PermissionDenied, ValidationError
from django.db import IntegrityError, transaction
from django.http import Http404, JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.views.decorators.http import require_GET, require_http_methods, require_POST

from .access import assign_role, get_user_role, permission_required, user_has_permission
from .audit import record_audit
from .forms import LocationForm
from .models import APIKey, AuditAction, AuditOutcome, Location, PortalPermission, PortalRole, ServiceIdentity
from .navigation import PORTAL_AREAS, visible_portal_areas


User = get_user_model()


def health(request):
    return JsonResponse({"status": "ok"})


@permission_required(PortalPermission.VIEW)
def home(request):
    context = {"areas": visible_portal_areas(request.user)}
    return render(request, _template(request, "core/home.html", "core/partials/home_content.html"), context)


@login_required
def portal_area(request, slug: str):
    area = PORTAL_AREAS.get(slug)
    if area is None:
        raise Http404("Unknown portal area")
    if not user_has_permission(request.user, area["permission"]):
        raise PermissionDenied

    context = {"area": area, "slug": slug, "areas": visible_portal_areas(request.user)}
    return render(request, _template(request, "core/area.html", "core/partials/area_content.html"), context)


@permission_required(PortalPermission.ADMINISTER)
@require_GET
def admin_roles(request):
    roles = [
        {
            "id": role.value,
            "label": role.label,
            "permissions": [permission.value for permission in PortalPermission if permission in _role_permissions(role)],
        }
        for role in PortalRole
    ]
    return JsonResponse({"roles": roles})


@permission_required(PortalPermission.ADMINISTER)
@require_http_methods(["GET", "POST"])
def admin_users(request):
    if request.method == "GET":
        users = User.objects.select_related("portal_profile").order_by("username", "id")
        return JsonResponse({"users": [_serialize_user(user) for user in users]})

    payload, error = _json_payload(request)
    if error is not None:
        return error

    username = str(payload.get("username", "")).strip()
    if not username:
        return _json_error("username is required")

    try:
        is_active = _payload_bool(payload, "is_active", True)
        role = _payload_role(payload)
    except ValueError as exc:
        return _json_error(str(exc))

    user = User(
        username=username,
        email=str(payload.get("email", "")).strip(),
        first_name=str(payload.get("first_name", "")).strip(),
        last_name=str(payload.get("last_name", "")).strip(),
        is_active=is_active,
    )
    password = payload.get("password")
    if password:
        user.set_password(str(password))
    else:
        user.set_unusable_password()

    try:
        user.full_clean()
        with transaction.atomic():
            user.save()
            assign_role(user, role)
    except (IntegrityError, ValidationError, ValueError) as exc:
        return _json_error(str(exc))

    return JsonResponse({"user": _serialize_user(user)}, status=201)


@permission_required(PortalPermission.ADMINISTER)
@require_http_methods(["PATCH"])
def admin_user_detail(request, user_id: int):
    user = get_object_or_404(User, pk=user_id)
    payload, error = _json_payload(request)
    if error is not None:
        return error

    for field in ("username", "email", "first_name", "last_name"):
        if field in payload:
            setattr(user, field, str(payload[field]).strip())

    try:
        if "is_active" in payload:
            user.is_active = _payload_bool(payload, "is_active", user.is_active)
        if payload.get("password"):
            user.set_password(str(payload["password"]))
        role = _payload_role(payload, required=False)
        user.full_clean()
        with transaction.atomic():
            user.save()
            if role is not None:
                assign_role(user, role)
    except (IntegrityError, ValidationError, ValueError) as exc:
        return _json_error(str(exc))

    return JsonResponse({"user": _serialize_user(user)})


@permission_required(PortalPermission.ADMINISTER)
@require_http_methods(["GET", "POST"])
def admin_service_identities(request):
    if request.method == "GET":
        identities = ServiceIdentity.objects.order_by("name", "id")
        return JsonResponse({"service_identities": [_serialize_service_identity(identity) for identity in identities]})

    payload, error = _json_payload(request)
    if error is not None:
        return error

    try:
        is_active = _payload_bool(payload, "is_active", True)
    except ValueError as exc:
        return _json_error(str(exc))

    identity = ServiceIdentity(
        name=str(payload.get("name", "")).strip(),
        slug=str(payload.get("slug", "")).strip(),
        description=str(payload.get("description", "")).strip(),
        is_active=is_active,
        created_by=request.user,
    )
    if not identity.name or not identity.slug:
        return _json_error("name and slug are required")

    try:
        identity.full_clean()
        identity.save()
    except (IntegrityError, ValidationError) as exc:
        return _json_error(str(exc))

    return JsonResponse({"service_identity": _serialize_service_identity(identity)}, status=201)


@permission_required(PortalPermission.ADMINISTER)
@require_http_methods(["PATCH"])
def admin_service_identity_detail(request, service_identity_id: int):
    identity = get_object_or_404(ServiceIdentity, pk=service_identity_id)
    payload, error = _json_payload(request)
    if error is not None:
        return error

    for field in ("name", "slug", "description"):
        if field in payload:
            setattr(identity, field, str(payload[field]).strip())

    try:
        if "is_active" in payload:
            identity.is_active = _payload_bool(payload, "is_active", identity.is_active)
        identity.full_clean()
        identity.save()
    except (IntegrityError, ValidationError) as exc:
        return _json_error(str(exc))

    return JsonResponse({"service_identity": _serialize_service_identity(identity)})


@permission_required(PortalPermission.ADMINISTER)
@require_http_methods(["GET", "POST"])
def admin_api_keys(request):
    if request.method == "GET":
        api_keys = APIKey.objects.select_related("user", "service_identity").order_by("name", "id")
        return JsonResponse({"api_keys": [_serialize_api_key(api_key) for api_key in api_keys]})

    payload, error = _json_payload(request)
    if error is not None:
        return error

    name = str(payload.get("name", "")).strip()
    if not name:
        return _json_error("name is required")

    user, service_identity, scope_error = _payload_api_key_scope(payload)
    if scope_error is not None:
        return scope_error

    try:
        with transaction.atomic():
            api_key, raw_secret = APIKey.issue(
                name=name,
                created_by=request.user,
                user=user,
                service_identity=service_identity,
            )
            _record_api_key_audit(request.user, AuditAction.API_KEY_CREATE, api_key)
    except (IntegrityError, ValidationError, ValueError) as exc:
        return _json_error(str(exc))

    return JsonResponse({"api_key": _serialize_api_key(api_key), "secret": raw_secret}, status=201)


@permission_required(PortalPermission.ADMINISTER)
@require_POST
def admin_api_key_rotate(request, api_key_id: int):
    api_key = get_object_or_404(APIKey.objects.select_related("user", "service_identity"), pk=api_key_id)

    try:
        with transaction.atomic():
            old_prefix = api_key.prefix
            raw_secret = api_key.rotate(request.user)
            _record_api_key_audit(
                request.user,
                AuditAction.API_KEY_ROTATE,
                api_key,
                details={"old_prefix": old_prefix},
            )
    except ValidationError as exc:
        return _json_error(str(exc))

    return JsonResponse({"api_key": _serialize_api_key(api_key), "secret": raw_secret})


@permission_required(PortalPermission.ADMINISTER)
@require_POST
def admin_api_key_revoke(request, api_key_id: int):
    api_key = get_object_or_404(APIKey.objects.select_related("user", "service_identity"), pk=api_key_id)

    try:
        with transaction.atomic():
            api_key.revoke(request.user)
            _record_api_key_audit(request.user, AuditAction.API_KEY_REVOKE, api_key)
    except ValidationError as exc:
        return _json_error(str(exc))

    return JsonResponse({"api_key": _serialize_api_key(api_key)})


@permission_required(PortalPermission.VIEW)
def location_list(request):
    locations = Location.objects.all()
    context = _location_context(request, {"locations": locations})
    return render(request, _template(request, "core/locations/list.html", "core/partials/location_list.html"), context)


@permission_required(PortalPermission.VIEW)
def location_detail(request, slug: str):
    location = get_object_or_404(Location, slug=slug)
    context = _location_context(request, {"location": location})
    return render(request, _template(request, "core/locations/detail.html", "core/partials/location_detail.html"), context)


@permission_required(PortalPermission.EDIT_CONFIG)
def location_create(request):
    include_sensitive_fields = _can_manage_location_secrets(request)
    if request.method == "POST":
        form = LocationForm(request.POST, include_sensitive_fields=include_sensitive_fields)
        if form.is_valid():
            location = form.save()
            return redirect("location-detail", slug=location.slug)
    else:
        form = LocationForm(include_sensitive_fields=include_sensitive_fields)

    context = _location_context(
        request,
        {
            "form": form,
            "form_title": "New Location",
            "form_action": "Create",
            "location": None,
        },
    )
    return render(request, _template(request, "core/locations/form.html", "core/partials/location_form.html"), context)


@permission_required(PortalPermission.EDIT_CONFIG)
def location_update(request, slug: str):
    location = get_object_or_404(Location, slug=slug)
    include_sensitive_fields = _can_manage_location_secrets(request)
    if request.method == "POST":
        form = LocationForm(
            request.POST,
            instance=location,
            include_sensitive_fields=include_sensitive_fields,
        )
        if form.is_valid():
            location = form.save()
            return redirect("location-detail", slug=location.slug)
    else:
        form = LocationForm(instance=location, include_sensitive_fields=include_sensitive_fields)

    context = _location_context(
        request,
        {
            "form": form,
            "form_title": f"Edit {location.name}",
            "form_action": "Save",
            "location": location,
        },
    )
    return render(request, _template(request, "core/locations/form.html", "core/partials/location_form.html"), context)


@permission_required(PortalPermission.EDIT_CONFIG)
def location_delete(request, slug: str):
    location = get_object_or_404(Location, slug=slug)
    if request.method == "POST":
        location.delete()
        return redirect("locations")

    context = _location_context(request, {"location": location})
    return render(
        request,
        _template(request, "core/locations/confirm_delete.html", "core/partials/location_confirm_delete.html"),
        context,
    )


def _template(request, full_template: str, partial_template: str) -> str:
    if request.headers.get("HX-Request") == "true":
        return partial_template
    return full_template


def _role_permissions(role: PortalRole) -> frozenset[PortalPermission]:
    from .access import get_role_permissions

    return get_role_permissions(role)


def _json_payload(request):
    if not request.body:
        return {}, None
    try:
        payload = json.loads(request.body.decode("utf-8"))
    except json.JSONDecodeError:
        return None, _json_error("Request body must be valid JSON")
    if not isinstance(payload, dict):
        return None, _json_error("Request body must be a JSON object")
    return payload, None


def _json_error(message: str, *, status: int = 400) -> JsonResponse:
    return JsonResponse({"error": message}, status=status)


def _payload_bool(payload: dict, field: str, default: bool) -> bool:
    if field not in payload:
        return default
    value = payload[field]
    if isinstance(value, bool):
        return value
    raise ValueError(f"{field} must be a boolean")


def _payload_role(payload: dict, *, required: bool = True) -> PortalRole | None:
    if "role" not in payload:
        if required:
            return PortalRole.VIEWER
        return None
    return PortalRole(str(payload["role"]))


def _payload_api_key_scope(payload: dict):
    user_id = payload.get("user_id")
    service_identity_id = payload.get("service_identity_id")
    if bool(user_id) == bool(service_identity_id):
        return None, None, _json_error("Provide exactly one of user_id or service_identity_id")

    if user_id:
        return get_object_or_404(User, pk=user_id), None, None
    return None, get_object_or_404(ServiceIdentity, pk=service_identity_id), None


def _serialize_user(user) -> dict:
    role = get_user_role(user)
    return {
        "id": user.id,
        "username": user.get_username(),
        "email": user.email,
        "first_name": user.first_name,
        "last_name": user.last_name,
        "is_active": user.is_active,
        "role": role.value if role else None,
    }


def _serialize_service_identity(identity: ServiceIdentity) -> dict:
    return {
        "id": identity.id,
        "name": identity.name,
        "slug": identity.slug,
        "description": identity.description,
        "is_active": identity.is_active,
        "created_at": identity.created_at.isoformat(),
        "updated_at": identity.updated_at.isoformat(),
    }


def _serialize_api_key(api_key: APIKey) -> dict:
    return {
        "id": api_key.id,
        "name": api_key.name,
        "prefix": api_key.prefix,
        "scope_type": api_key.scope_type,
        "scope_id": api_key.user_id or api_key.service_identity_id,
        "scope_label": api_key.scope_label,
        "is_active": api_key.is_active,
        "created_at": api_key.created_at.isoformat(),
        "updated_at": api_key.updated_at.isoformat(),
        "last_rotated_at": api_key.last_rotated_at.isoformat() if api_key.last_rotated_at else None,
        "revoked_at": api_key.revoked_at.isoformat() if api_key.revoked_at else None,
        "last_used_at": api_key.last_used_at.isoformat() if api_key.last_used_at else None,
    }


def _record_api_key_audit(actor, action: AuditAction, api_key: APIKey, *, details: dict | None = None) -> None:
    audit_details = {
        "api_key_id": api_key.id,
        "api_key_name": api_key.name,
        "prefix": api_key.prefix,
        "scope_type": api_key.scope_type,
        "scope_id": api_key.user_id or api_key.service_identity_id,
    }
    if details:
        audit_details.update(details)
    record_audit(
        actor=actor,
        action=action,
        target=f"api_keys/{api_key.id}",
        outcome=AuditOutcome.SUCCESS,
        details=audit_details,
    )


def _location_context(request, context):
    context.update(
        {
            "areas": visible_portal_areas(request.user),
            "can_edit_locations": user_has_permission(request.user, PortalPermission.EDIT_CONFIG),
            "can_manage_location_secrets": _can_manage_location_secrets(request),
        }
    )
    return context


def _can_manage_location_secrets(request) -> bool:
    return user_has_permission(request.user, PortalPermission.ADMINISTER)
