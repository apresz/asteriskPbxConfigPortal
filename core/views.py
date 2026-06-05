import base64
import binascii
import json
from datetime import datetime, timedelta, timezone as datetime_timezone

from django.contrib.auth import get_user_model
from django.contrib.auth.decorators import login_required
from django.core.exceptions import PermissionDenied, ValidationError
from django.db import IntegrityError, transaction
from django.http import Http404, HttpResponse, JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import dateparse, timezone
from django.views.decorators.http import require_GET, require_http_methods, require_POST

from .access import (
    api_login_required,
    api_permission_required,
    assign_role,
    get_user_role,
    principal_has_permission,
    permission_required,
    user_has_permission,
)
from .audit import record_audit, record_config_change
from .audit_helpers import audit_model_summary
from .audio_prompts import AudioPromptConversionError, create_audio_prompt_from_upload
from .ami_telemetry import recording_id_for_path
from .backups import create_admin_backup
from .config_export import ConfigExportValidationError, create_config_version, validate_location_routing
from .deployments import DeploymentError, deploy_config_version
from .extension_csv import (
    ExtensionCSVError,
    export_extensions_csv,
    extension_template_csv,
    import_extensions_csv,
)
from .extension_management import clear_extension_relationships, is_911_disable_change
from .forms import (
    CallQueueForm,
    DIDForm,
    ExtensionForm,
    FeatureCodeForm,
    InboundDestinationForm,
    IVRForm,
    IVRMenuOptionFormSet,
    LocationForm,
    PagingGroupForm,
    RingGroupForm,
    OutboundRouteForm,
    OutboundRouteTrunkFormSet,
    ProviderForm,
    PhoneForm,
    PhoneLineAppearanceFormSet,
    PhoneSpeedDialFormSet,
    TrunkForm,
)
from .live_operations import (
    AgentCommandTimeoutError,
    AgentUnavailableError,
    UnsupportedLiveCommandError,
    build_live_operation_audit_details,
    canonical_live_command_name,
    run_location_live_command,
    run_location_recording_playback,
    supported_live_commands,
    validate_live_command_parameters,
)
from .models import (
    APIKey,
    AdminBackup,
    AuditAction,
    AuditLog,
    AuditOutcome,
    CallQueue,
    ConfigVersion,
    DID,
    Extension,
    FeatureCode,
    InboundDestination,
    IVR,
    Location,
    PagingGroup,
    OutboundRoute,
    Phone,
    PhoneSpeedDial,
    PortalPermission,
    PortalRole,
    PortalUserProfile,
    RingGroup,
    Provider,
    ServiceIdentity,
    Trunk,
)
from .navigation import PORTAL_AREAS, visible_portal_areas
from .service_principals import (
    ServicePermissionError,
    is_service_principal,
    normalize_service_permissions,
    service_identity_audit_details,
)
from .phone_csv import (
    PhoneCSVImportError,
    did_template_csv,
    export_phones_csv,
    export_speed_dials_csv,
    import_dids_csv,
    import_phones_csv,
    import_speed_dials_csv,
    phone_template_csv,
    speed_dial_template_csv,
)


User = get_user_model()


CONFIG_AUDIT_FORM_SOURCE = "portal_form"


def _snapshot_config_instance(instance) -> dict:
    return audit_model_summary(instance, redact=False)


def _save_config_form_with_audit(
    request,
    form,
    *,
    operation: str,
    before: dict | None = None,
    source: str = CONFIG_AUDIT_FORM_SOURCE,
    extra_details: dict | None = None,
):
    with transaction.atomic():
        instance = form.save()
        _record_config_change(
            request,
            operation,
            instance,
            before=before,
            source=source,
            extra_details=extra_details,
        )
    return instance


def _record_config_change(
    request,
    operation: str,
    instance,
    *,
    before: dict | None = None,
    after: dict | None = None,
    source: str = CONFIG_AUDIT_FORM_SOURCE,
    extra_details: dict | None = None,
):
    return record_config_change(
        actor=request.user,
        operation=operation,
        instance=instance,
        before=before,
        after=after,
        source=source,
        extra_details=extra_details,
    )


def _delete_config_object_with_audit(
    request,
    instance,
    *,
    source: str = CONFIG_AUDIT_FORM_SOURCE,
    extra_details: dict | None = None,
) -> None:
    before = _snapshot_config_instance(instance)
    with transaction.atomic():
        _record_config_change(
            request,
            "delete",
            instance,
            before=before,
            after=None,
            source=source,
            extra_details=extra_details,
        )
        instance.delete()


def health(request):
    return JsonResponse({"status": "ok"})


@permission_required(PortalPermission.VIEW)
def home(request):
    context = _dashboard_context(request)
    return render(request, _template(request, "core/home.html", "core/partials/home_content.html"), context)


@permission_required(PortalPermission.VIEW)
@require_GET
def dashboard_panel(request):
    return render(request, "core/partials/dashboard_panel.html", _dashboard_context(request))


@login_required
def portal_area(request, slug: str):
    area = PORTAL_AREAS.get(slug)
    if area is None:
        raise Http404("Unknown portal area")
    if not user_has_permission(request.user, area["permission"]):
        raise PermissionDenied

    context = {"area": area, "slug": slug, "areas": visible_portal_areas(request.user)}
    return render(request, _template(request, "core/area.html", "core/partials/area_content.html"), context)


@permission_required(PortalPermission.VIEW)
def trunk_list(request):
    providers = Provider.objects.prefetch_related("trunks").order_by("name")
    trunks = Trunk.objects.select_related("location", "provider").order_by("location__name", "name")
    context = _trunk_context(request, {"providers": providers, "trunks": trunks})
    return render(request, _template(request, "core/trunks/list.html", "core/partials/trunks/list_content.html"), context)


@permission_required(PortalPermission.EDIT_CONFIG)
def provider_create(request):
    if request.method == "POST":
        form = ProviderForm(request.POST)
        if form.is_valid():
            _save_config_form_with_audit(request, form, operation="create")
            return redirect("trunks")
    else:
        form = ProviderForm()

    context = _trunk_context(
        request,
        {
            "form": form,
            "form_title": "New Provider",
            "form_action": "Create",
            "provider": None,
        },
    )
    return render(request, _template(request, "core/trunks/provider_form.html", "core/partials/trunks/provider_form_content.html"), context)


@permission_required(PortalPermission.EDIT_CONFIG)
def provider_update(request, slug: str):
    provider = get_object_or_404(Provider, slug=slug)
    if request.method == "POST":
        before = _snapshot_config_instance(provider)
        form = ProviderForm(request.POST, instance=provider)
        if form.is_valid():
            _save_config_form_with_audit(request, form, operation="update", before=before)
            return redirect("trunks")
    else:
        form = ProviderForm(instance=provider)

    context = _trunk_context(
        request,
        {
            "form": form,
            "form_title": f"Edit {provider.name}",
            "form_action": "Save",
            "provider": provider,
        },
    )
    return render(request, _template(request, "core/trunks/provider_form.html", "core/partials/trunks/provider_form_content.html"), context)


@permission_required(PortalPermission.EDIT_CONFIG)
def provider_delete(request, slug: str):
    provider = get_object_or_404(Provider, slug=slug)
    if request.method == "POST":
        _delete_config_object_with_audit(request, provider)
        return redirect("trunks")

    context = _trunk_context(request, {"provider": provider})
    return render(
        request,
        _template(request, "core/trunks/provider_confirm_delete.html", "core/partials/trunks/provider_confirm_delete_content.html"),
        context,
    )


@permission_required(PortalPermission.EDIT_CONFIG)
def trunk_create(request):
    if request.method == "POST":
        form = TrunkForm(request.POST)
        if form.is_valid():
            _save_config_form_with_audit(request, form, operation="create")
            return redirect("trunks")
    else:
        form = TrunkForm()

    context = _trunk_context(
        request,
        {
            "form": form,
            "form_title": "New Provider Trunk",
            "form_action": "Create",
            "trunk": None,
        },
    )
    return render(request, _template(request, "core/trunks/trunk_form.html", "core/partials/trunks/trunk_form_content.html"), context)


@permission_required(PortalPermission.EDIT_CONFIG)
def trunk_update(request, trunk_id: int):
    trunk = get_object_or_404(Trunk.objects.select_related("location", "provider"), pk=trunk_id)
    if request.method == "POST":
        before = _snapshot_config_instance(trunk)
        form = TrunkForm(request.POST, instance=trunk)
        if form.is_valid():
            _save_config_form_with_audit(request, form, operation="update", before=before)
            return redirect("trunks")
    else:
        form = TrunkForm(instance=trunk)

    context = _trunk_context(
        request,
        {
            "form": form,
            "form_title": f"Edit {trunk.name}",
            "form_action": "Save",
            "trunk": trunk,
        },
    )
    return render(request, _template(request, "core/trunks/trunk_form.html", "core/partials/trunks/trunk_form_content.html"), context)


@permission_required(PortalPermission.EDIT_CONFIG)
def trunk_delete(request, trunk_id: int):
    trunk = get_object_or_404(Trunk.objects.select_related("location", "provider"), pk=trunk_id)
    if request.method == "POST":
        _delete_config_object_with_audit(request, trunk)
        return redirect("trunks")

    context = _trunk_context(request, {"trunk": trunk})
    return render(
        request,
        _template(request, "core/trunks/trunk_confirm_delete.html", "core/partials/trunks/trunk_confirm_delete_content.html"),
        context,
    )


@permission_required(PortalPermission.VIEW)
def outbound_route_list(request):
    routes = OutboundRoute.objects.select_related("location").prefetch_related(
        "route_trunks__trunk__provider",
    ).order_by("location__name", "priority", "name")
    dial_plan_validation = _dial_plan_validation()
    context = _dial_plan_context(
        request,
        {
            "routes": routes,
            "dial_plan_validation": dial_plan_validation,
            "dial_plan_validation_has_errors": any(result["errors"] for result in dial_plan_validation),
        },
    )
    return render(request, _template(request, "core/dial_plan/list.html", "core/partials/dial_plan/list_content.html"), context)


@permission_required(PortalPermission.EDIT_CONFIG)
def outbound_route_create(request):
    route = OutboundRoute()
    if request.method == "POST":
        form = OutboundRouteForm(request.POST, instance=route)
        location = _outbound_route_formset_location(request, route)
        form_is_valid = form.is_valid()
        formset = OutboundRouteTrunkFormSet(
            request.POST,
            instance=route,
            prefix="route_trunks",
            form_kwargs={"location": location},
        )
        if form_is_valid:
            route = form.save(commit=False)
            formset.instance = route
            if formset.is_valid():
                with transaction.atomic():
                    route.save()
                    formset.save()
                    _record_config_change(request, "create", route)
                return redirect("dial-plan")
    else:
        form = OutboundRouteForm(instance=route)
        formset = OutboundRouteTrunkFormSet(
            instance=route,
            prefix="route_trunks",
            form_kwargs={"location": None},
        )

    context = _dial_plan_context(
        request,
        {
            "form": form,
            "route_trunk_formset": formset,
            "form_title": "New Outbound Route",
            "form_action": "Create",
            "route": None,
        },
    )
    return render(request, _template(request, "core/dial_plan/outbound_form.html", "core/partials/dial_plan/outbound_form_content.html"), context)


@permission_required(PortalPermission.EDIT_CONFIG)
def outbound_route_update(request, route_id: int):
    route = get_object_or_404(OutboundRoute.objects.select_related("location"), pk=route_id)
    before = _snapshot_config_instance(route) if request.method == "POST" else None
    if request.method == "POST":
        form = OutboundRouteForm(request.POST, instance=route)
        location = _outbound_route_formset_location(request, route)
        form_is_valid = form.is_valid()
        formset = OutboundRouteTrunkFormSet(
            request.POST,
            instance=route,
            prefix="route_trunks",
            form_kwargs={"location": location},
        )
        if form_is_valid:
            route = form.save(commit=False)
            formset.instance = route
            if formset.is_valid():
                with transaction.atomic():
                    route.save()
                    formset.save()
                    _record_config_change(request, "update", route, before=before)
                return redirect("dial-plan")
    else:
        form = OutboundRouteForm(instance=route)
        formset = OutboundRouteTrunkFormSet(
            instance=route,
            prefix="route_trunks",
            form_kwargs={"location": route.location},
        )

    context = _dial_plan_context(
        request,
        {
            "form": form,
            "route_trunk_formset": formset,
            "form_title": f"Edit {route.name}",
            "form_action": "Save",
            "route": route,
        },
    )
    return render(request, _template(request, "core/dial_plan/outbound_form.html", "core/partials/dial_plan/outbound_form_content.html"), context)


@permission_required(PortalPermission.EDIT_CONFIG)
def outbound_route_delete(request, route_id: int):
    route = get_object_or_404(OutboundRoute.objects.select_related("location"), pk=route_id)
    if request.method == "POST":
        _delete_config_object_with_audit(request, route)
        return redirect("dial-plan")

    context = _dial_plan_context(request, {"route": route})
    return render(
        request,
        _template(request, "core/dial_plan/outbound_confirm_delete.html", "core/partials/dial_plan/outbound_confirm_delete_content.html"),
        context,
    )


@permission_required(PortalPermission.VIEW)
@require_GET
def phone_book(request):
    contacts = (
        Extension.objects.select_related("location")
        .prefetch_related("direct_dids", "phone_appearances__phone")
        .order_by("number")
    )
    context = _portal_area_context(request, "phone-book", {"phone_book_contacts": contacts})
    return render(request, _template(request, "core/phone_book.html", "core/partials/phone_book_content.html"), context)


@permission_required(PortalPermission.ACCESS_RECORDINGS)
@require_GET
def recordings(request):
    locations = [_recordings_location(location, request.user) for location in Location.objects.order_by("name")]
    context = _portal_area_context(
        request,
        "recordings",
        {
            "recording_locations": locations,
            "recording_count": sum(len(location["recordings"]) for location in locations),
        },
    )
    return render(request, _template(request, "core/recordings.html", "core/partials/recordings_content.html"), context)


@permission_required(PortalPermission.ADMINISTER)
@require_GET
def audit_log(request):
    entries = AuditLog.objects.select_related("actor").order_by("-timestamp", "-id")[:100]
    context = _portal_area_context(request, "audit-log", {"audit_entries": entries})
    return render(request, _template(request, "core/audit_log.html", "core/partials/audit_log_content.html"), context)


@permission_required(PortalPermission.ADMINISTER)
@require_GET
def users_roles(request):
    context = _portal_area_context(
        request,
        "users-roles",
        {
            "user_role_rows": _user_role_rows(),
            "portal_roles": _portal_role_rows(),
            "role_options": list(PortalRole),
        },
    )
    return render(request, _template(request, "core/users_roles.html", "core/partials/users_roles_content.html"), context)


@permission_required(PortalPermission.ADMINISTER)
@require_POST
def user_role_update(request, user_id: int):
    user = get_object_or_404(User, pk=user_id)
    try:
        role = PortalRole(str(request.POST.get("role") or ""))
    except ValueError:
        return HttpResponse("Invalid portal role.", status=400)

    changed_fields = []
    if _stored_user_role(user) != role:
        changed_fields.append("role")

    with transaction.atomic():
        user.portal_profile = assign_role(user, role)

    if changed_fields:
        _record_api_user_update_audit(request.user, user, changed_fields, request)
    return redirect("users-roles")


@permission_required(PortalPermission.ADMINISTER)
@require_http_methods(["GET", "POST"])
def api_keys(request):
    issued_secret = None
    issued_api_key = None
    error_message = ""
    status = 200

    if request.method == "POST":
        name = str(request.POST.get("name") or "").strip()
        user, service_identity, error_message = _api_key_scope_from_post(request)
        if not name:
            error_message = "API key name is required."
        if error_message:
            status = 400
        else:
            try:
                with transaction.atomic():
                    issued_api_key, issued_secret = APIKey.issue(
                        name=name,
                        created_by=request.user,
                        user=user,
                        service_identity=service_identity,
                    )
                    _record_api_key_audit(request.user, AuditAction.API_KEY_CREATE, issued_api_key, request=request)
                status = 201
            except (IntegrityError, ValidationError, ValueError) as exc:
                error_message = str(exc)
                status = 400

    return _api_keys_page_response(
        request,
        issued_secret=issued_secret,
        issued_api_key=issued_api_key,
        error_message=error_message,
        status=status,
    )


@permission_required(PortalPermission.ADMINISTER)
@require_POST
def api_key_rotate_ui(request, api_key_id: int):
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
                request=request,
            )
    except ValidationError as exc:
        return _api_keys_page_response(request, error_message=str(exc), status=400)

    return _api_keys_page_response(request, issued_secret=raw_secret, issued_api_key=api_key)


@permission_required(PortalPermission.ADMINISTER)
@require_POST
def api_key_revoke_ui(request, api_key_id: int):
    api_key = get_object_or_404(APIKey.objects.select_related("user", "service_identity"), pk=api_key_id)
    try:
        with transaction.atomic():
            api_key.revoke(request.user)
            _record_api_key_audit(request.user, AuditAction.API_KEY_REVOKE, api_key, request=request)
    except ValidationError as exc:
        return _api_keys_page_response(request, error_message=str(exc), status=400)

    return redirect("api-keys")


@api_permission_required(PortalPermission.ADMINISTER)
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


@api_permission_required(PortalPermission.ADMINISTER)
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


@api_permission_required(PortalPermission.ADMINISTER)
@require_http_methods(["PATCH"])
def admin_user_detail(request, user_id: int):
    user = get_object_or_404(User, pk=user_id)
    payload, error = _json_payload(request)
    if error is not None:
        return error

    return _update_user_info_response(request, user, payload, allow_admin_fields=True)


@api_permission_required(PortalPermission.VIEW)
@require_GET
def api_users(request):
    principal = _request_principal(request)
    users = User.objects.select_related("portal_profile")
    if principal_has_permission(principal, PortalPermission.ADMINISTER):
        users = users.order_by("username", "id")
    elif is_service_principal(principal):
        return _json_error("Service identity must have administer permission to access users.", status=403)
    else:
        users = users.filter(pk=request.user.pk)
    return JsonResponse({"users": [_serialize_user(user) for user in users]})


@api_permission_required(PortalPermission.VIEW)
@require_http_methods(["GET", "PATCH"])
def api_current_user(request):
    if is_service_principal(_request_principal(request)):
        return _json_error("User endpoint requires a user-scoped API key.", status=403)
    return _api_user_detail_response(request, request.user)


@api_permission_required(PortalPermission.VIEW)
@require_http_methods(["GET", "PATCH"])
def api_user_detail(request, user_id: int):
    user = get_object_or_404(User, pk=user_id)
    principal = _request_principal(request)
    if not principal_has_permission(principal, PortalPermission.ADMINISTER):
        if is_service_principal(principal) or user.pk != request.user.pk:
            return _json_error("Permission denied.", status=403)
    return _api_user_detail_response(request, user)


@api_permission_required(PortalPermission.ADMINISTER)
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
        permissions = _payload_service_permissions(payload.get("permissions", []))
    except (ServicePermissionError, ValueError) as exc:
        return _json_error(str(exc))

    identity = ServiceIdentity(
        name=str(payload.get("name", "")).strip(),
        slug=str(payload.get("slug", "")).strip(),
        description=str(payload.get("description", "")).strip(),
        permissions=list(permissions),
        is_active=is_active,
        created_by=_request_user_or_none(request),
    )
    if not identity.name or not identity.slug:
        return _json_error("name and slug are required")

    try:
        identity.full_clean()
        identity.save()
    except (IntegrityError, ValidationError) as exc:
        return _json_error(str(exc))

    return JsonResponse({"service_identity": _serialize_service_identity(identity)}, status=201)


@api_permission_required(PortalPermission.ADMINISTER)
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
        if "permissions" in payload:
            identity.permissions = list(_payload_service_permissions(payload["permissions"]))
        if "is_active" in payload:
            identity.is_active = _payload_bool(payload, "is_active", identity.is_active)
        identity.full_clean()
        identity.save()
    except (IntegrityError, ServicePermissionError, ValidationError) as exc:
        return _json_error(str(exc))

    return JsonResponse({"service_identity": _serialize_service_identity(identity)})


@api_permission_required(PortalPermission.ADMINISTER)
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
            _record_api_key_audit(request.user, AuditAction.API_KEY_CREATE, api_key, request=request)
    except (IntegrityError, ValidationError, ValueError) as exc:
        return _json_error(str(exc))

    return JsonResponse({"api_key": _serialize_api_key(api_key), "secret": raw_secret}, status=201)


@api_permission_required(PortalPermission.ADMINISTER)
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
                request=request,
            )
    except ValidationError as exc:
        return _json_error(str(exc))

    return JsonResponse({"api_key": _serialize_api_key(api_key), "secret": raw_secret})


@api_permission_required(PortalPermission.ADMINISTER)
@require_POST
def admin_api_key_revoke(request, api_key_id: int):
    api_key = get_object_or_404(APIKey.objects.select_related("user", "service_identity"), pk=api_key_id)

    try:
        with transaction.atomic():
            api_key.revoke(request.user)
            _record_api_key_audit(request.user, AuditAction.API_KEY_REVOKE, api_key, request=request)
    except ValidationError as exc:
        return _json_error(str(exc))

    return JsonResponse({"api_key": _serialize_api_key(api_key)})


@permission_required(PortalPermission.ADMINISTER)
@require_GET
def settings(request):
    context = _portal_area_context(
        request,
        "settings",
        {"settings_links": _settings_links(request.user)},
    )
    return render(request, _template(request, "core/settings.html", "core/partials/settings_content.html"), context)


@permission_required(PortalPermission.ADMINISTER)
@require_GET
def backups(request):
    backups = AdminBackup.objects.select_related("generated_by").order_by("-generated_at", "-id")[:20]
    context = _portal_area_context(request, "backups", {"backups": backups})
    return render(request, _template(request, "core/backups.html", "core/partials/backups_content.html"), context)


@permission_required(PortalPermission.ADMINISTER)
@require_POST
def admin_backup_create(request):
    backup = create_admin_backup(generated_by=request.user)
    _record_backup_audit(request.user, AuditAction.BACKUP_CREATE, backup)
    if request.headers.get("Accept") == "application/json":
        return JsonResponse({"backup": _serialize_admin_backup(backup)}, status=201)
    return redirect("backups")


@permission_required(PortalPermission.ADMINISTER)
@require_GET
def admin_backup_download(request, backup_id: int):
    backup = get_object_or_404(AdminBackup.objects.select_related("generated_by"), pk=backup_id)
    _record_backup_audit(request.user, AuditAction.BACKUP_DOWNLOAD, backup)
    response = HttpResponse(bytes(backup.archive), content_type="application/zip")
    response["Content-Disposition"] = f'attachment; filename="{backup.filename}"'
    response["Content-Length"] = str(backup.archive_size_bytes)
    response["X-Checksum-SHA256"] = backup.checksum
    return response


@permission_required(PortalPermission.VIEW)
def location_list(request):
    locations = Location.objects.all()
    context = _location_context(request, {"locations": locations})
    return render(request, _template(request, "core/locations/list.html", "core/partials/location_list.html"), context)


@permission_required(PortalPermission.VIEW)
def location_detail(request, slug: str):
    location = get_object_or_404(Location, slug=slug)
    context = _location_detail_context(request, location)
    return render(request, _template(request, "core/locations/detail.html", "core/partials/location_detail.html"), context)


@permission_required(PortalPermission.EDIT_CONFIG)
def location_create(request):
    include_sensitive_fields = _can_manage_location_secrets(request)
    if request.method == "POST":
        form = LocationForm(request.POST, include_sensitive_fields=include_sensitive_fields)
        if form.is_valid():
            location = _save_config_form_with_audit(request, form, operation="create")
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
        before = _snapshot_config_instance(location)
        form = LocationForm(
            request.POST,
            instance=location,
            include_sensitive_fields=include_sensitive_fields,
        )
        if form.is_valid():
            location = _save_config_form_with_audit(request, form, operation="update", before=before)
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
        _delete_config_object_with_audit(request, location)
        return redirect("locations")

    context = _location_context(request, {"location": location})
    return render(
        request,
        _template(request, "core/locations/confirm_delete.html", "core/partials/location_confirm_delete.html"),
        context,
    )


@permission_required(PortalPermission.EDIT_CONFIG)
@require_POST
def location_config_export(request, slug: str):
    location = get_object_or_404(Location, slug=slug)
    try:
        version = create_config_version(location, exported_by=request.user, require_emergency=True)
    except ConfigExportValidationError as exc:
        record_audit(
            actor=request.user,
            action=AuditAction.CONFIG_EXPORT,
            target=f"locations/{location.slug}/config",
            outcome=AuditOutcome.FAILURE,
            details={
                "location_id": location.id,
                "location_slug": location.slug,
                "validation": exc.validation,
            },
        )
        context = _location_detail_context(
            request,
            location,
            {
                "export_errors": [error["message"] for error in exc.validation["errors"]],
                "export_validation": exc.validation,
            },
        )
        return render(
            request,
            _template(request, "core/locations/detail.html", "core/partials/location_detail.html"),
            context,
            status=400,
        )

    record_audit(
        actor=request.user,
        action=AuditAction.CONFIG_EXPORT,
        target=f"locations/{location.slug}/config",
        outcome=AuditOutcome.SUCCESS,
        details={
            "location_id": location.id,
            "location_slug": location.slug,
            "config_version_id": version.id,
            "version_number": version.version_number,
            "checksum": version.checksum,
            "warnings": version.warnings,
        },
    )
    return redirect("location-detail", slug=location.slug)


@permission_required(PortalPermission.EDIT_CONFIG)
def location_config_export_download(request, slug: str, version_number: int):
    version = _config_version_or_404(slug, version_number)
    response = HttpResponse(bytes(version.archive), content_type="application/zip")
    response["Content-Disposition"] = (
        f'attachment; filename="{version.location.slug}-config-v{version.version_number}.zip"'
    )
    response["Content-Length"] = str(version.archive_size_bytes)
    return response


@permission_required(PortalPermission.RUN_LIVE_OPERATIONS)
@require_POST
def location_config_export_deploy(request, slug: str, version_number: int):
    version = _config_version_or_404(slug, version_number)
    try:
        deploy_config_version(
            version,
            operator=request.user,
            reload_confirmed=_reload_confirmed(request),
            rollback=False,
        )
    except DeploymentError as exc:
        context = _location_detail_context(
            request,
            version.location,
            {"deployment_errors": [str(exc)]},
        )
        return render(
            request,
            _template(request, "core/locations/detail.html", "core/partials/location_detail.html"),
            context,
            status=400,
        )
    return redirect("location-detail", slug=version.location.slug)


@permission_required(PortalPermission.RUN_LIVE_OPERATIONS)
@require_POST
def location_config_export_rollback(request, slug: str, version_number: int):
    version = _config_version_or_404(slug, version_number)
    try:
        deploy_config_version(
            version,
            operator=request.user,
            reload_confirmed=_reload_confirmed(request),
            rollback=True,
        )
    except DeploymentError as exc:
        context = _location_detail_context(
            request,
            version.location,
            {"deployment_errors": [str(exc)]},
        )
        return render(
            request,
            _template(request, "core/locations/detail.html", "core/partials/location_detail.html"),
            context,
            status=400,
        )
    return redirect("location-detail", slug=version.location.slug)


@login_required
@require_POST
def location_live_operation(request, slug: str):
    location = get_object_or_404(Location, slug=slug)
    payload, payload_error = _live_operation_payload(request)
    if payload_error is not None:
        return payload_error

    response_payload, status = _execute_live_operation(request, location, payload)
    return _live_operation_response(request, location, response_payload, status=status)


@api_login_required
@require_POST
def api_channel_control(request, slug: str):
    location = get_object_or_404(Location, slug=slug)
    payload, payload_error = _json_payload(request)
    if payload_error is not None:
        return payload_error

    response_payload, status = _execute_live_operation(request, location, payload)
    return JsonResponse(response_payload, status=status)


def _execute_live_operation(request, location: Location, payload: dict) -> tuple[dict, int]:
    command_name = str(payload.get("command") or "").strip()
    parameters = payload.get("parameters", {})
    principal = _request_principal(request)

    if not principal_has_permission(principal, PortalPermission.RUN_LIVE_OPERATIONS):
        response_payload = {
            "location": location.slug,
            "command": command_name,
            "status": "denied",
            "error": "Permission denied.",
        }
        _record_live_operation_audit(
            principal,
            location,
            command_name,
            AuditOutcome.DENIED,
            response_payload,
            request=request,
            parameters=parameters,
        )
        return response_payload, 403

    audit_command_name = command_name
    audit_parameters = parameters
    try:
        audit_command_name = canonical_live_command_name(command_name)
        audit_parameters = validate_live_command_parameters(audit_command_name, parameters)
        result = run_location_live_command(location, audit_command_name, audit_parameters)
    except UnsupportedLiveCommandError as exc:
        result = {"status": "failure", "error": str(exc)}
        outcome = AuditOutcome.FAILURE
        status = 400
    except AgentUnavailableError as exc:
        result = {"status": "failure", "error": str(exc)}
        outcome = AuditOutcome.FAILURE
        status = 503
    except AgentCommandTimeoutError as exc:
        result = {"status": "failure", "error": str(exc)}
        outcome = AuditOutcome.FAILURE
        status = 504
    except Exception as exc:
        result = {"status": "failure", "error": str(exc)}
        outcome = AuditOutcome.FAILURE
        status = 502
    else:
        outcome = AuditOutcome.SUCCESS if result.get("status") == "success" else AuditOutcome.FAILURE
        status = 200 if outcome == AuditOutcome.SUCCESS else 502

    response_payload = {
        "location": location.slug,
        "command": audit_command_name,
        "status": result.get("status", "failure"),
        "result": result,
    }
    _record_live_operation_audit(
        principal,
        location,
        audit_command_name,
        outcome,
        response_payload,
        request=request,
        parameters=audit_parameters,
    )
    return response_payload, status


@login_required
@require_GET
def location_recording_playback(request, slug: str, recording_id: str):
    location = get_object_or_404(Location, slug=slug)
    if not user_has_permission(request.user, PortalPermission.ACCESS_RECORDINGS):
        _record_recording_playback_audit(
            request.user,
            location,
            recording_id,
            AuditOutcome.DENIED,
            details={"status": "denied", "error": "Permission denied."},
        )
        raise PermissionDenied

    recording = _recording_metadata_for_id(location, recording_id)
    if recording is None:
        _record_recording_playback_audit(
            request.user,
            location,
            recording_id,
            AuditOutcome.FAILURE,
            details={"status": "missing", "error": "Recording metadata was not found."},
        )
        raise Http404("Recording not found.")

    recording_status = _recording_status(location, recording)
    if recording_status == "expired":
        _record_recording_playback_audit(
            request.user,
            location,
            recording_id,
            AuditOutcome.FAILURE,
            recording=recording,
            details={"status": "expired", "error": "Recording has expired."},
        )
        return HttpResponse("Recording has expired.", status=410)
    if recording_status != "available":
        _record_recording_playback_audit(
            request.user,
            location,
            recording_id,
            AuditOutcome.FAILURE,
            recording=recording,
            details={"status": "unavailable", "error": "Recording is unavailable."},
        )
        raise Http404("Recording not available.")

    try:
        result = run_location_recording_playback(
            location,
            _recording_path(recording),
            retention_days=location.recording_retention_days,
        )
    except AgentUnavailableError as exc:
        return _recording_playback_failure_response(
            request,
            location,
            recording_id,
            recording,
            "agent_unavailable",
            str(exc),
            503,
        )
    except AgentCommandTimeoutError as exc:
        return _recording_playback_failure_response(
            request,
            location,
            recording_id,
            recording,
            "agent_timeout",
            str(exc),
            504,
        )
    except Exception as exc:
        return _recording_playback_failure_response(
            request,
            location,
            recording_id,
            recording,
            "agent_error",
            str(exc),
            502,
        )

    if result.get("status") != "success":
        error_code = str(result.get("error_code") or "agent_failure")
        status_code = 410 if error_code == "expired" else 404 if error_code == "unavailable" else 502
        return _recording_playback_failure_response(
            request,
            location,
            recording_id,
            recording,
            error_code,
            str(result.get("error") or "Recording playback failed."),
            status_code,
            result=result,
        )

    try:
        content = base64.b64decode(str(result.get("content_base64") or ""), validate=True)
    except (binascii.Error, ValueError):
        return _recording_playback_failure_response(
            request,
            location,
            recording_id,
            recording,
            "invalid_agent_payload",
            "PBX agent returned invalid recording content.",
            502,
        )

    _record_recording_playback_audit(
        request.user,
        location,
        recording_id,
        AuditOutcome.SUCCESS,
        recording=recording,
        details={
            "status": "success",
            "content_type": result.get("content_type") or "application/octet-stream",
            "size_bytes": len(content),
        },
    )
    response = HttpResponse(content, content_type=result.get("content_type") or "application/octet-stream")
    filename = _recording_filename(result) or _recording_filename(recording) or "recording"
    response["Content-Disposition"] = f'inline; filename="{filename.replace(chr(34), "")}"'
    response["Content-Length"] = str(len(content))
    return response


@permission_required(PortalPermission.VIEW)
def extension_list(request):
    extensions = _extension_queryset()
    context = _extension_context(request, {"extensions": extensions})
    return render(request, _template(request, "core/extensions/list.html", "core/partials/extensions/list_content.html"), context)


@permission_required(PortalPermission.EDIT_CONFIG)
def extension_create(request):
    can_disable_911 = _can_disable_911(request)
    if request.method == "POST":
        form = ExtensionForm(request.POST, can_disable_911=can_disable_911)
        if form.is_valid():
            logs_911_disable = is_911_disable_change(None, form.cleaned_data["emergency_calling_enabled"])
            with transaction.atomic():
                extension = form.save()
                _record_config_change(request, "create", extension)
                if logs_911_disable:
                    _record_911_disable(request, extension, AuditOutcome.SUCCESS, "form")
            return redirect("extensions")
        _record_denied_911_if_needed(request, form, "new")
    else:
        form = ExtensionForm(can_disable_911=can_disable_911)

    context = _extension_context(
        request,
        {
            "form": form,
            "form_title": "New Extension",
            "form_action": "Create",
            "extension": None,
        },
    )
    return render(request, _template(request, "core/extensions/form.html", "core/partials/extensions/form_content.html"), context)


@permission_required(PortalPermission.EDIT_CONFIG)
def extension_update(request, number: str):
    extension = get_object_or_404(Extension, number=number)
    can_disable_911 = _can_disable_911(request)
    if request.method == "POST":
        before = _snapshot_config_instance(extension)
        original_911_enabled = extension.emergency_calling_enabled
        form = ExtensionForm(request.POST, instance=extension, can_disable_911=can_disable_911)
        if form.is_valid():
            logs_911_disable = original_911_enabled and not form.cleaned_data["emergency_calling_enabled"]
            with transaction.atomic():
                extension = form.save()
                _record_config_change(request, "update", extension, before=before)
                if logs_911_disable:
                    _record_911_disable(request, extension, AuditOutcome.SUCCESS, "form")
            return redirect("extensions")
        _record_denied_911_if_needed(request, form, extension.number)
    else:
        form = ExtensionForm(instance=extension, can_disable_911=can_disable_911)

    context = _extension_context(
        request,
        {
            "form": form,
            "form_title": f"Edit {extension.number}",
            "form_action": "Save",
            "extension": extension,
        },
    )
    return render(request, _template(request, "core/extensions/form.html", "core/partials/extensions/form_content.html"), context)


@permission_required(PortalPermission.EDIT_CONFIG)
def extension_delete(request, number: str):
    extension = get_object_or_404(Extension, number=number)
    if request.method == "POST":
        before = _snapshot_config_instance(extension)
        with transaction.atomic():
            clear_extension_relationships(extension)
            _record_config_change(request, "delete", extension, before=before, after=None)
            extension.delete()
        return redirect("extensions")

    context = _extension_context(request, {"extension": extension})
    return render(
        request,
        _template(request, "core/extensions/confirm_delete.html", "core/partials/extensions/confirm_delete_content.html"),
        context,
    )


@permission_required(PortalPermission.EDIT_CONFIG)
def extension_import(request):
    context = {}
    if request.method == "POST":
        upload = request.FILES.get("csv_file")
        if upload is None:
            context["import_errors"] = ["Choose an extension CSV file."]
        else:
            try:
                imported_count = import_extensions_csv(
                    upload,
                    actor=request.user,
                    can_disable_911=_can_disable_911(request),
                )
            except ExtensionCSVError as exc:
                context["import_errors"] = exc.errors
            else:
                context["import_result"] = f"Imported {imported_count} extension row(s)."

    return render(
        request,
        _template(request, "core/extensions/import.html", "core/partials/extensions/import_content.html"),
        _extension_context(request, context),
    )


@permission_required(PortalPermission.VIEW)
def extension_export(request):
    record_audit(
        actor=request.user,
        action=AuditAction.CONFIG_EXPORT,
        target="extensions/csv",
        outcome=AuditOutcome.SUCCESS,
    )
    return _csv_response(export_extensions_csv(_extension_queryset()), "extensions.csv")


@permission_required(PortalPermission.VIEW)
def extension_template(request):
    return _csv_response(extension_template_csv(), "extensions-template.csv")


@permission_required(PortalPermission.VIEW)
def phone_list(request):
    phones = _phone_queryset()
    context = _phone_context(request, {"phones": phones})
    return render(request, _template(request, "core/phones/list.html", "core/partials/phones/list_content.html"), context)


@permission_required(PortalPermission.EDIT_CONFIG)
def phone_import(request):
    return _csv_import_response(
        request,
        import_function=import_phones_csv,
        context_builder=_phone_context,
        area_slug="phones",
        eyebrow="Phones",
        title="Import Phones",
        back_url="phones",
        template_url="phone-template",
        result_label="phone",
    )


@permission_required(PortalPermission.EDIT_CONFIG)
def phone_create(request):
    phone = Phone()
    if request.method == "POST":
        form = PhoneForm(request.POST, instance=phone)
        location = _phone_formset_location(request, phone)
        line_formset = PhoneLineAppearanceFormSet(
            request.POST,
            instance=phone,
            prefix="lines",
            form_kwargs={"location": location},
        )
        speed_dial_formset = PhoneSpeedDialFormSet(request.POST, instance=phone, prefix="speed_dials")
        if form.is_valid() and line_formset.is_valid() and speed_dial_formset.is_valid():
            with transaction.atomic():
                phone = form.save()
                line_formset.instance = phone
                speed_dial_formset.instance = phone
                line_formset.save()
                speed_dial_formset.save()
                _record_config_change(request, "create", phone)
            return redirect("phones")
    else:
        form = PhoneForm(instance=phone)
        line_formset = PhoneLineAppearanceFormSet(
            instance=phone,
            prefix="lines",
            form_kwargs={"location": None},
        )
        speed_dial_formset = PhoneSpeedDialFormSet(instance=phone, prefix="speed_dials")

    context = _phone_context(
        request,
        {
            "form": form,
            "line_formset": line_formset,
            "speed_dial_formset": speed_dial_formset,
            "form_title": "New Phone",
            "form_action": "Create",
            "phone": None,
        },
    )
    return render(request, _template(request, "core/phones/form.html", "core/partials/phones/form_content.html"), context)


@permission_required(PortalPermission.EDIT_CONFIG)
def phone_update(request, mac_address: str):
    phone = get_object_or_404(Phone, mac_address=mac_address)
    before = _snapshot_config_instance(phone) if request.method == "POST" else None
    if request.method == "POST":
        form = PhoneForm(request.POST, instance=phone)
        location = _phone_formset_location(request, phone)
        line_formset = PhoneLineAppearanceFormSet(
            request.POST,
            instance=phone,
            prefix="lines",
            form_kwargs={"location": location},
        )
        speed_dial_formset = PhoneSpeedDialFormSet(request.POST, instance=phone, prefix="speed_dials")
        if form.is_valid() and line_formset.is_valid() and speed_dial_formset.is_valid():
            with transaction.atomic():
                phone = form.save()
                line_formset.instance = phone
                speed_dial_formset.instance = phone
                line_formset.save()
                speed_dial_formset.save()
                _record_config_change(request, "update", phone, before=before)
            return redirect("phones")
    else:
        form = PhoneForm(instance=phone)
        line_formset = PhoneLineAppearanceFormSet(
            instance=phone,
            prefix="lines",
            form_kwargs={"location": phone.location},
        )
        speed_dial_formset = PhoneSpeedDialFormSet(instance=phone, prefix="speed_dials")

    context = _phone_context(
        request,
        {
            "form": form,
            "line_formset": line_formset,
            "speed_dial_formset": speed_dial_formset,
            "form_title": f"Edit {phone.mac_address}",
            "form_action": "Save",
            "phone": phone,
        },
    )
    return render(request, _template(request, "core/phones/form.html", "core/partials/phones/form_content.html"), context)


@permission_required(PortalPermission.EDIT_CONFIG)
def phone_delete(request, mac_address: str):
    phone = get_object_or_404(Phone, mac_address=mac_address)
    if request.method == "POST":
        _delete_config_object_with_audit(request, phone)
        return redirect("phones")

    context = _phone_context(request, {"phone": phone})
    return render(
        request,
        _template(request, "core/phones/confirm_delete.html", "core/partials/phones/confirm_delete_content.html"),
        context,
    )


@permission_required(PortalPermission.VIEW)
def phone_export(request):
    record_audit(
        actor=request.user,
        action=AuditAction.CONFIG_EXPORT,
        target="phones/csv",
        outcome=AuditOutcome.SUCCESS,
    )
    return _csv_response(export_phones_csv(_phone_queryset()), "phones.csv")


@permission_required(PortalPermission.VIEW)
def phone_template(request):
    return _csv_response(phone_template_csv(), "phones-template.csv")


@permission_required(PortalPermission.VIEW)
def did_template(request):
    return _csv_response(did_template_csv(), "dids-template.csv")


@permission_required(PortalPermission.VIEW)
def speed_dial_export(request):
    record_audit(
        actor=request.user,
        action=AuditAction.CONFIG_EXPORT,
        target="speed-dials/csv",
        outcome=AuditOutcome.SUCCESS,
    )
    return _csv_response(export_speed_dials_csv(PhoneSpeedDial.objects.select_related("phone")), "speed-dials.csv")


@permission_required(PortalPermission.EDIT_CONFIG)
def speed_dial_import(request):
    return _csv_import_response(
        request,
        import_function=import_speed_dials_csv,
        context_builder=_phone_context,
        area_slug="phones",
        eyebrow="Speed Dials",
        title="Import Speed Dials",
        back_url="phones",
        template_url="speed-dial-template",
        result_label="speed dial",
    )


@permission_required(PortalPermission.VIEW)
def speed_dial_template(request):
    return _csv_response(speed_dial_template_csv(), "speed-dials-template.csv")


@permission_required(PortalPermission.VIEW)
def inbound_destination_list(request):
    destinations = InboundDestination.objects.select_related(
        "location",
        "extension",
        "ivr",
        "ring_group",
        "queue",
    )
    return _routing_list_response(
        request,
        kind="inbound-destinations",
        title="Inbound Destinations",
        description="Reusable targets for DID fallback, IVR, queue overflow, and feature-code routing.",
        records=destinations,
        create_url="inbound-destination-create",
        edit_url="inbound-destination-edit",
        empty_label="No inbound destinations configured",
    )


@permission_required(PortalPermission.EDIT_CONFIG)
def inbound_destination_create(request):
    form = InboundDestinationForm(request.POST or None)
    if request.method == "POST" and form.is_valid():
        _save_config_form_with_audit(request, form, operation="create")
        return redirect("inbound-destinations")
    return _routing_form_response(
        request,
        form=form,
        area_slug="inbound-destinations",
        eyebrow="Inbound Destination",
        title="New Inbound Destination",
        cancel_url="inbound-destinations",
        delete_url=None,
        object_instance=None,
    )


@permission_required(PortalPermission.EDIT_CONFIG)
def inbound_destination_update(request, destination_id: int):
    destination = get_object_or_404(InboundDestination, pk=destination_id)
    before = _snapshot_config_instance(destination) if request.method == "POST" else None
    form = InboundDestinationForm(request.POST or None, instance=destination)
    if request.method == "POST" and form.is_valid():
        _save_config_form_with_audit(request, form, operation="update", before=before)
        return redirect("inbound-destinations")
    return _routing_form_response(
        request,
        form=form,
        area_slug="inbound-destinations",
        eyebrow="Inbound Destination",
        title=f"Edit {destination.name}",
        cancel_url="inbound-destinations",
        delete_url="inbound-destination-delete",
        object_instance=destination,
    )


@permission_required(PortalPermission.EDIT_CONFIG)
def inbound_destination_delete(request, destination_id: int):
    destination = get_object_or_404(InboundDestination, pk=destination_id)
    return _routing_delete_response(
        request,
        record=destination,
        area_slug="inbound-destinations",
        eyebrow="Inbound Destination",
        title=f"Delete {destination.name}",
        cancel_url="inbound-destinations",
    )


@permission_required(PortalPermission.VIEW)
def did_list(request):
    dids = DID.objects.select_related(
        "location",
        "provider",
        "trunk",
        "direct_extension",
        "default_destination",
        "location__default_inbound_destination",
    )
    return _routing_list_response(
        request,
        kind="dids",
        title="DID Routing",
        description="Inbound DID routing with direct extension assignment and location default fallback.",
        records=dids,
        create_url="did-create",
        edit_url="did-edit",
        empty_label="No DIDs configured",
        import_url="did-import",
        template_url="did-template",
    )


@permission_required(PortalPermission.EDIT_CONFIG)
def did_import(request):
    return _csv_import_response(
        request,
        import_function=import_dids_csv,
        context_builder=_routing_context,
        area_slug="dids",
        eyebrow="DID Routing",
        title="Import DIDs",
        back_url="dids",
        template_url="did-template",
        result_label="DID",
    )


@permission_required(PortalPermission.EDIT_CONFIG)
def did_create(request):
    form = DIDForm(request.POST or None)
    if request.method == "POST" and form.is_valid():
        _save_config_form_with_audit(request, form, operation="create")
        return redirect("dids")
    return _routing_form_response(
        request,
        form=form,
        area_slug="dids",
        eyebrow="DID",
        title="New DID",
        cancel_url="dids",
        delete_url=None,
        object_instance=None,
    )


@permission_required(PortalPermission.EDIT_CONFIG)
def did_update(request, did_id: int):
    did = get_object_or_404(DID, pk=did_id)
    before = _snapshot_config_instance(did) if request.method == "POST" else None
    form = DIDForm(request.POST or None, instance=did)
    if request.method == "POST" and form.is_valid():
        _save_config_form_with_audit(request, form, operation="update", before=before)
        return redirect("dids")
    return _routing_form_response(
        request,
        form=form,
        area_slug="dids",
        eyebrow="DID",
        title=f"Edit {did.number}",
        cancel_url="dids",
        delete_url="did-delete",
        object_instance=did,
    )


@permission_required(PortalPermission.EDIT_CONFIG)
def did_delete(request, did_id: int):
    did = get_object_or_404(DID, pk=did_id)
    return _routing_delete_response(
        request,
        record=did,
        area_slug="dids",
        eyebrow="DID",
        title=f"Delete {did.number}",
        cancel_url="dids",
    )


@permission_required(PortalPermission.VIEW)
def ivr_list(request):
    ivrs = IVR.objects.select_related(
        "location",
        "business_hours_destination",
        "after_hours_destination",
        "timeout_destination",
        "invalid_destination",
    ).prefetch_related("menu_options__destination")
    return _routing_list_response(
        request,
        kind="ivrs",
        title="IVRs",
        description="Business-hours, after-hours, timeout, invalid-input, and menu-option routing.",
        records=ivrs,
        create_url="ivr-create",
        edit_url="ivr-edit",
        empty_label="No IVRs configured",
    )


@permission_required(PortalPermission.EDIT_CONFIG)
def ivr_create(request):
    ivr = IVR()
    form = IVRForm(request.POST or None, request.FILES or None, instance=ivr)
    formset = IVRMenuOptionFormSet(
        request.POST or None,
        instance=ivr,
        prefix="menu_options",
        form_kwargs={"location": _ivr_formset_location(request, ivr)},
    )
    if request.method == "POST" and form.is_valid() and formset.is_valid():
        try:
            with transaction.atomic():
                ivr = _save_ivr_with_prompt(form)
                formset.instance = ivr
                formset.save()
                _record_config_change(request, "create", ivr)
            return redirect("ivrs")
        except AudioPromptConversionError as exc:
            form.add_error("prompt_upload", str(exc))
    return _routing_form_response(
        request,
        form=form,
        area_slug="ivrs",
        eyebrow="IVR",
        title="New IVR",
        cancel_url="ivrs",
        delete_url=None,
        object_instance=None,
        formset=formset,
        formset_title="Menu Options",
    )


@permission_required(PortalPermission.EDIT_CONFIG)
def ivr_update(request, ivr_id: int):
    ivr = get_object_or_404(IVR, pk=ivr_id)
    before = _snapshot_config_instance(ivr) if request.method == "POST" else None
    form = IVRForm(request.POST or None, request.FILES or None, instance=ivr)
    formset = IVRMenuOptionFormSet(
        request.POST or None,
        instance=ivr,
        prefix="menu_options",
        form_kwargs={"location": _ivr_formset_location(request, ivr)},
    )
    if request.method == "POST" and form.is_valid() and formset.is_valid():
        try:
            with transaction.atomic():
                ivr = _save_ivr_with_prompt(form)
                formset.instance = ivr
                formset.save()
                _record_config_change(request, "update", ivr, before=before)
            return redirect("ivrs")
        except AudioPromptConversionError as exc:
            form.add_error("prompt_upload", str(exc))
    return _routing_form_response(
        request,
        form=form,
        area_slug="ivrs",
        eyebrow="IVR",
        title=f"Edit {ivr.name}",
        cancel_url="ivrs",
        delete_url="ivr-delete",
        object_instance=ivr,
        formset=formset,
        formset_title="Menu Options",
    )


@permission_required(PortalPermission.EDIT_CONFIG)
def ivr_delete(request, ivr_id: int):
    ivr = get_object_or_404(IVR, pk=ivr_id)
    return _routing_delete_response(
        request,
        record=ivr,
        area_slug="ivrs",
        eyebrow="IVR",
        title=f"Delete {ivr.name}",
        cancel_url="ivrs",
    )


@permission_required(PortalPermission.VIEW)
def ring_group_list(request):
    ring_groups = RingGroup.objects.select_related("location").prefetch_related("members__extension")
    return _routing_list_response(
        request,
        kind="ring-groups",
        title="Ring Groups",
        description="Static ring groups with strategy, timeout, and ordered member extensions.",
        records=ring_groups,
        create_url="ring-group-create",
        edit_url="ring-group-edit",
        empty_label="No ring groups configured",
    )


@permission_required(PortalPermission.EDIT_CONFIG)
def ring_group_create(request):
    form = RingGroupForm(request.POST or None)
    if request.method == "POST" and form.is_valid():
        _save_config_form_with_audit(request, form, operation="create")
        return redirect("ring-groups")
    return _routing_form_response(
        request,
        form=form,
        area_slug="ring-groups",
        eyebrow="Ring Group",
        title="New Ring Group",
        cancel_url="ring-groups",
        delete_url=None,
        object_instance=None,
    )


@permission_required(PortalPermission.EDIT_CONFIG)
def ring_group_update(request, ring_group_id: int):
    ring_group = get_object_or_404(RingGroup, pk=ring_group_id)
    before = _snapshot_config_instance(ring_group) if request.method == "POST" else None
    form = RingGroupForm(request.POST or None, instance=ring_group)
    if request.method == "POST" and form.is_valid():
        _save_config_form_with_audit(request, form, operation="update", before=before)
        return redirect("ring-groups")
    return _routing_form_response(
        request,
        form=form,
        area_slug="ring-groups",
        eyebrow="Ring Group",
        title=f"Edit {ring_group.name}",
        cancel_url="ring-groups",
        delete_url="ring-group-delete",
        object_instance=ring_group,
    )


@permission_required(PortalPermission.EDIT_CONFIG)
def ring_group_delete(request, ring_group_id: int):
    ring_group = get_object_or_404(RingGroup, pk=ring_group_id)
    return _routing_delete_response(
        request,
        record=ring_group,
        area_slug="ring-groups",
        eyebrow="Ring Group",
        title=f"Delete {ring_group.name}",
        cancel_url="ring-groups",
    )


@permission_required(PortalPermission.VIEW)
def queue_list(request):
    queues = CallQueue.objects.select_related("location", "overflow_destination").prefetch_related("members__extension")
    return _routing_list_response(
        request,
        kind="queues",
        title="Queues",
        description="Static queues with strategy, retry, timeout, music on hold, and overflow destination.",
        records=queues,
        create_url="queue-create",
        edit_url="queue-edit",
        empty_label="No queues configured",
    )


@permission_required(PortalPermission.EDIT_CONFIG)
def queue_create(request):
    form = CallQueueForm(request.POST or None)
    if request.method == "POST" and form.is_valid():
        _save_config_form_with_audit(request, form, operation="create")
        return redirect("queues")
    return _routing_form_response(
        request,
        form=form,
        area_slug="queues",
        eyebrow="Queue",
        title="New Queue",
        cancel_url="queues",
        delete_url=None,
        object_instance=None,
    )


@permission_required(PortalPermission.EDIT_CONFIG)
def queue_update(request, queue_id: int):
    queue = get_object_or_404(CallQueue, pk=queue_id)
    before = _snapshot_config_instance(queue) if request.method == "POST" else None
    form = CallQueueForm(request.POST or None, instance=queue)
    if request.method == "POST" and form.is_valid():
        _save_config_form_with_audit(request, form, operation="update", before=before)
        return redirect("queues")
    return _routing_form_response(
        request,
        form=form,
        area_slug="queues",
        eyebrow="Queue",
        title=f"Edit {queue.name}",
        cancel_url="queues",
        delete_url="queue-delete",
        object_instance=queue,
    )


@permission_required(PortalPermission.EDIT_CONFIG)
def queue_delete(request, queue_id: int):
    queue = get_object_or_404(CallQueue, pk=queue_id)
    return _routing_delete_response(
        request,
        record=queue,
        area_slug="queues",
        eyebrow="Queue",
        title=f"Delete {queue.name}",
        cancel_url="queues",
    )


@permission_required(PortalPermission.VIEW)
def paging_group_list(request):
    paging_groups = PagingGroup.objects.select_related("location").prefetch_related("members__extension")
    return _routing_list_response(
        request,
        kind="paging-groups",
        title="Paging Groups",
        description="Static paging groups with dialable page codes and member extensions.",
        records=paging_groups,
        create_url="paging-group-create",
        edit_url="paging-group-edit",
        empty_label="No paging groups configured",
    )


@permission_required(PortalPermission.EDIT_CONFIG)
def paging_group_create(request):
    form = PagingGroupForm(request.POST or None)
    if request.method == "POST" and form.is_valid():
        _save_config_form_with_audit(request, form, operation="create")
        return redirect("paging-groups")
    return _routing_form_response(
        request,
        form=form,
        area_slug="paging-groups",
        eyebrow="Paging Group",
        title="New Paging Group",
        cancel_url="paging-groups",
        delete_url=None,
        object_instance=None,
    )


@permission_required(PortalPermission.EDIT_CONFIG)
def paging_group_update(request, paging_group_id: int):
    paging_group = get_object_or_404(PagingGroup, pk=paging_group_id)
    before = _snapshot_config_instance(paging_group) if request.method == "POST" else None
    form = PagingGroupForm(request.POST or None, instance=paging_group)
    if request.method == "POST" and form.is_valid():
        _save_config_form_with_audit(request, form, operation="update", before=before)
        return redirect("paging-groups")
    return _routing_form_response(
        request,
        form=form,
        area_slug="paging-groups",
        eyebrow="Paging Group",
        title=f"Edit {paging_group.name}",
        cancel_url="paging-groups",
        delete_url="paging-group-delete",
        object_instance=paging_group,
    )


@permission_required(PortalPermission.EDIT_CONFIG)
def paging_group_delete(request, paging_group_id: int):
    paging_group = get_object_or_404(PagingGroup, pk=paging_group_id)
    return _routing_delete_response(
        request,
        record=paging_group,
        area_slug="paging-groups",
        eyebrow="Paging Group",
        title=f"Delete {paging_group.name}",
        cancel_url="paging-groups",
    )


@permission_required(PortalPermission.VIEW)
def feature_code_list(request):
    feature_codes = FeatureCode.objects.select_related("location", "destination")
    return _routing_list_response(
        request,
        kind="feature-codes",
        title="Feature Codes",
        description="Dialable PBX feature codes for voicemail, pickup, park, paging, and custom actions.",
        records=feature_codes,
        create_url="feature-code-create",
        edit_url="feature-code-edit",
        empty_label="No feature codes configured",
    )


@permission_required(PortalPermission.EDIT_CONFIG)
def feature_code_create(request):
    form = FeatureCodeForm(request.POST or None)
    if request.method == "POST" and form.is_valid():
        _save_config_form_with_audit(request, form, operation="create")
        return redirect("feature-codes")
    return _routing_form_response(
        request,
        form=form,
        area_slug="feature-codes",
        eyebrow="Feature Code",
        title="New Feature Code",
        cancel_url="feature-codes",
        delete_url=None,
        object_instance=None,
    )


@permission_required(PortalPermission.EDIT_CONFIG)
def feature_code_update(request, feature_code_id: int):
    feature_code = get_object_or_404(FeatureCode, pk=feature_code_id)
    before = _snapshot_config_instance(feature_code) if request.method == "POST" else None
    form = FeatureCodeForm(request.POST or None, instance=feature_code)
    if request.method == "POST" and form.is_valid():
        _save_config_form_with_audit(request, form, operation="update", before=before)
        return redirect("feature-codes")
    return _routing_form_response(
        request,
        form=form,
        area_slug="feature-codes",
        eyebrow="Feature Code",
        title=f"Edit {feature_code.code}",
        cancel_url="feature-codes",
        delete_url="feature-code-delete",
        object_instance=feature_code,
    )


@permission_required(PortalPermission.EDIT_CONFIG)
def feature_code_delete(request, feature_code_id: int):
    feature_code = get_object_or_404(FeatureCode, pk=feature_code_id)
    return _routing_delete_response(
        request,
        record=feature_code,
        area_slug="feature-codes",
        eyebrow="Feature Code",
        title=f"Delete {feature_code.code}",
        cancel_url="feature-codes",
    )


def _routing_list_response(
    request,
    *,
    kind,
    title,
    description,
    records,
    create_url,
    edit_url,
    empty_label,
    import_url=None,
    template_url=None,
):
    context = _routing_context(
        request,
        {
            "kind": kind,
            "page_title": title,
            "page_description": description,
            "records": records,
            "create_url": create_url,
            "edit_url": edit_url,
            "empty_label": empty_label,
            "import_url": import_url,
            "template_url": template_url,
        },
    )
    return render(request, _template(request, "core/routing/list.html", "core/partials/routing/list_content.html"), context)


def _routing_form_response(
    request,
    *,
    form,
    area_slug,
    eyebrow,
    title,
    cancel_url,
    delete_url,
    object_instance,
    formset=None,
    formset_title="",
):
    context = _routing_context(
        request,
        {
            "form": form,
            "area_slug": area_slug,
            "eyebrow": eyebrow,
            "form_title": title,
            "cancel_url": cancel_url,
            "delete_url": delete_url,
            "object_instance": object_instance,
            "formset": formset,
            "formset_title": formset_title,
        },
    )
    return render(request, _template(request, "core/routing/form.html", "core/partials/routing/form_content.html"), context)


def _routing_delete_response(request, *, record, area_slug, eyebrow, title, cancel_url):
    if request.method == "POST":
        _delete_config_object_with_audit(request, record)
        return redirect(cancel_url)

    context = _routing_context(
        request,
        {
            "record": record,
            "area_slug": area_slug,
            "eyebrow": eyebrow,
            "confirm_title": title,
            "cancel_url": cancel_url,
        },
    )
    return render(
        request,
        _template(request, "core/routing/confirm_delete.html", "core/partials/routing/confirm_delete_content.html"),
        context,
    )


def _routing_context(request, context):
    context.update(
        {
            "areas": visible_portal_areas(request.user),
            "can_edit_routing": user_has_permission(request.user, PortalPermission.EDIT_CONFIG),
        }
    )
    return context


def _ivr_formset_location(request, ivr):
    if request.method == "POST":
        location_id = request.POST.get("location")
        if location_id:
            try:
                return Location.objects.get(pk=location_id)
            except (Location.DoesNotExist, ValueError):
                return None
        return None
    if ivr and ivr.pk:
        return ivr.location
    return None


def _save_ivr_with_prompt(form):
    ivr = form.save(commit=False)
    upload = form.cleaned_data.get("prompt_upload")
    if upload:
        prompt = create_audio_prompt_from_upload(location=ivr.location, uploaded_file=upload)
        ivr.prompt = prompt
        ivr.prompt_name = prompt.playback_name
    elif ivr.prompt_id:
        ivr.prompt_name = ivr.prompt.playback_name
    ivr.save()
    return ivr


def _template(request, full_template: str, partial_template: str) -> str:
    if request.headers.get("HX-Request") == "true":
        return partial_template
    return full_template


def _portal_area_context(request, slug: str, extra: dict | None = None) -> dict:
    context = {
        "area": PORTAL_AREAS[slug],
        "slug": slug,
        "areas": visible_portal_areas(request.user),
    }
    if extra:
        context.update(extra)
    return context


def _settings_links(user) -> list[dict]:
    visible_areas = visible_portal_areas(user)
    return [
        {"slug": slug, "area": PORTAL_AREAS[slug]}
        for slug in ("users-roles", "api-keys", "audit-log", "backups")
        if slug in visible_areas
    ]


def _recordings_location(location: Location, user) -> dict:
    telemetry = location.agent_telemetry if isinstance(location.agent_telemetry, dict) else {}
    can_access_recordings = user_has_permission(user, PortalPermission.ACCESS_RECORDINGS)
    recordings = [
        _recording_context(location, recording, can_access_recordings=can_access_recordings)
        for recording in _telemetry_list(telemetry, "recording_metadata")
    ]
    return {
        "location": location,
        "recordings": recordings,
        "available": sum(1 for recording in recordings if recording["status"] == "available"),
        "expired": sum(1 for recording in recordings if recording["status"] == "expired"),
        "unavailable": sum(1 for recording in recordings if recording["status"] == "unavailable"),
    }


def _user_role_rows() -> list[dict]:
    rows = []
    for user in User.objects.select_related("portal_profile").order_by("username", "id"):
        role = get_user_role(user)
        rows.append(
            {
                "user": user,
                "role": role,
                "role_label": role.label if role else "No portal role",
                "is_superuser": user.is_superuser,
            }
        )
    return rows


def _portal_role_rows() -> list[dict]:
    return [{"role": role, "permissions": _role_permissions(role)} for role in PortalRole]


def _api_keys_page_response(
    request,
    *,
    issued_secret: str | None = None,
    issued_api_key: APIKey | None = None,
    error_message: str = "",
    status: int = 200,
):
    context = _portal_area_context(
        request,
        "api-keys",
        {
            "api_keys": APIKey.objects.select_related("user", "service_identity").order_by("name", "id"),
            "service_identities": ServiceIdentity.objects.order_by("name", "id"),
            "api_key_users": User.objects.order_by("username", "id"),
            "issued_secret": issued_secret,
            "issued_api_key": issued_api_key,
            "error_message": error_message,
        },
    )
    return render(
        request,
        _template(request, "core/api_keys.html", "core/partials/api_keys_content.html"),
        context,
        status=status,
    )


def _api_key_scope_from_post(request):
    raw_scope = str(request.POST.get("scope") or "").strip()
    if ":" not in raw_scope:
        return None, None, "Choose a user or service identity scope."
    scope_type, scope_id = raw_scope.split(":", 1)
    if scope_type not in {"user", "service_identity"}:
        return None, None, "Choose a user or service identity scope."
    try:
        scope_pk = int(scope_id)
    except (TypeError, ValueError):
        return None, None, "Choose a valid API key scope."

    if scope_type == "user":
        user = User.objects.filter(pk=scope_pk).first()
        if user is None:
            return None, None, "Selected user was not found."
        return user, None, ""

    service_identity = ServiceIdentity.objects.filter(pk=scope_pk).first()
    if service_identity is None:
        return None, None, "Selected service identity was not found."
    return None, service_identity, ""


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


def _request_principal(request):
    return getattr(request, "api_principal", None) or request.user


def _request_user_or_none(request):
    user = getattr(request, "user", None)
    return user if getattr(user, "is_authenticated", False) else None


SELF_USER_UPDATE_FIELDS = frozenset({"email", "first_name", "last_name", "password"})
ADMIN_USER_UPDATE_FIELDS = frozenset({"username", "email", "first_name", "last_name", "is_active", "password", "role"})


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


def _payload_service_permissions(raw_permissions) -> tuple[str, ...]:
    return normalize_service_permissions(raw_permissions)


def _payload_api_key_scope(payload: dict):
    user_id = payload.get("user_id")
    service_identity_id = payload.get("service_identity_id")
    if bool(user_id) == bool(service_identity_id):
        return None, None, _json_error("Provide exactly one of user_id or service_identity_id")

    if user_id:
        return get_object_or_404(User, pk=user_id), None, None
    return None, get_object_or_404(ServiceIdentity, pk=service_identity_id), None


def _api_user_detail_response(request, user) -> JsonResponse:
    if request.method == "GET":
        return JsonResponse({"user": _serialize_user(user)})

    payload, error = _json_payload(request)
    if error is not None:
        return error

    return _update_user_info_response(
        request,
        user,
        payload,
        allow_admin_fields=principal_has_permission(_request_principal(request), PortalPermission.ADMINISTER),
    )


def _update_user_info_response(request, user, payload: dict, *, allow_admin_fields: bool) -> JsonResponse:
    allowed_fields = ADMIN_USER_UPDATE_FIELDS if allow_admin_fields else SELF_USER_UPDATE_FIELDS
    known_fields = ADMIN_USER_UPDATE_FIELDS
    unknown_fields = sorted(set(payload) - known_fields)
    if unknown_fields:
        return _json_error(f"Unsupported field: {unknown_fields[0]}")

    disallowed_fields = sorted(set(payload) - allowed_fields)
    if disallowed_fields:
        return _json_error(f"Field is not permitted: {disallowed_fields[0]}", status=403)

    changed_fields: list[str] = []
    for field in ("username", "email", "first_name", "last_name"):
        if field in payload:
            value = str(payload[field]).strip()
            if getattr(user, field) != value:
                setattr(user, field, value)
                changed_fields.append(field)

    role = None
    try:
        if "is_active" in payload:
            is_active = _payload_bool(payload, "is_active", user.is_active)
            if user.is_active != is_active:
                user.is_active = is_active
                changed_fields.append("is_active")
        if payload.get("password"):
            user.set_password(str(payload["password"]))
            changed_fields.append("password")
        if "role" in payload:
            role = _payload_role(payload, required=False)
            if role is not None and _stored_user_role(user) != role:
                changed_fields.append("role")

        user.full_clean()
        with transaction.atomic():
            user.save()
            if role is not None:
                user.portal_profile = assign_role(user, role)
    except (IntegrityError, ValidationError, ValueError) as exc:
        return _json_error(str(exc))

    if changed_fields:
        _record_api_user_update_audit(request.user, user, changed_fields, request)

    return JsonResponse({"user": _serialize_user(user)})


def _stored_user_role(user) -> PortalRole | None:
    try:
        return PortalRole(user.portal_profile.role)
    except (PortalUserProfile.DoesNotExist, ValueError):
        return None


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
        "permissions": list(identity.permissions or []),
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


def _serialize_admin_backup(backup: AdminBackup) -> dict:
    return {
        "id": backup.id,
        "filename": backup.filename,
        "checksum": backup.checksum,
        "archive_size_bytes": backup.archive_size_bytes,
        "generated_at": backup.generated_at.isoformat(),
        "generated_by": backup.generated_by.get_username() if backup.generated_by_id else None,
        "database_dump_method": backup.database_dump_method,
    }


def _record_api_key_audit(
    actor,
    action: AuditAction,
    api_key: APIKey,
    *,
    details: dict | None = None,
    request=None,
) -> None:
    audit_details = {
        "api_key_id": api_key.id,
        "api_key_name": api_key.name,
        "prefix": api_key.prefix,
        "scope_type": api_key.scope_type,
        "scope_id": api_key.user_id or api_key.service_identity_id,
    }
    if details:
        audit_details.update(details)
    audit_details.update(_api_auth_audit_details(request))
    record_audit(
        actor=actor,
        action=action,
        target=f"api_keys/{api_key.id}",
        outcome=AuditOutcome.SUCCESS,
        details=audit_details,
    )


def _record_api_user_update_audit(actor, user, changed_fields: list[str], request) -> None:
    details = {
        "user_id": user.id,
        "username": user.get_username(),
        "changed_fields": sorted(changed_fields),
        "actor_username": _audit_actor_username(actor, request),
    }
    details.update(_api_auth_audit_details(request))

    record_audit(
        actor=actor,
        action=AuditAction.API_USER_UPDATE,
        target=f"users/{user.id}",
        outcome=AuditOutcome.SUCCESS,
        details=details,
    )


def _api_auth_audit_details(request) -> dict:
    if request is None:
        return {}
    api_key = getattr(request, "api_key", None)
    if api_key is None:
        return {}

    details = {
        "auth_api_key_id": api_key.id,
        "auth_api_key_name": api_key.name,
        "auth_api_key_prefix": api_key.prefix,
        "auth_api_key_scope_type": api_key.scope_type,
        "auth_api_key_scope_id": api_key.user_id or api_key.service_identity_id,
        "auth_actor_username": _audit_actor_username(getattr(request, "user", None), request),
    }
    service_identity = getattr(api_key, "service_identity", None)
    if service_identity is not None:
        details.update(service_identity_audit_details(service_identity, prefix="auth_service_identity"))
    return details


def _audit_actor_username(actor, request=None) -> str:
    if request is not None:
        principal = getattr(request, "api_principal", None)
        if is_service_principal(principal):
            return principal.get_username()
    if getattr(actor, "is_authenticated", False):
        return actor.get_username()
    return "anonymous"


def _record_backup_audit(actor, action: AuditAction, backup: AdminBackup) -> None:
    record_audit(
        actor=actor,
        action=action,
        target=f"admin_backups/{backup.id}",
        outcome=AuditOutcome.SUCCESS,
        details={
            "backup_id": backup.id,
            "filename": backup.filename,
            "checksum": backup.checksum,
            "archive_size_bytes": backup.archive_size_bytes,
            "database_dump_method": backup.database_dump_method,
        },
    )


def _dashboard_context(request):
    dashboard_locations = [_dashboard_location(location, request.user) for location in Location.objects.order_by("name")]
    dashboard_totals = {
        "locations": len(dashboard_locations),
        "reporting_locations": sum(1 for item in dashboard_locations if item["agent_state"]["is_reporting"]),
        "drift_locations": sum(1 for item in dashboard_locations if item["config_drift"]["has_drift"]),
        "active_calls": sum(item["call_summary"]["total"] for item in dashboard_locations),
        "queued_calls": sum(item["queue_summary"]["waiting"] for item in dashboard_locations),
    }
    return {
        "areas": visible_portal_areas(request.user),
        "dashboard_generated_at": timezone.now(),
        "dashboard_locations": dashboard_locations,
        "dashboard_totals": dashboard_totals,
    }


def _dashboard_location(location: Location, user) -> dict:
    telemetry = location.agent_telemetry if isinstance(location.agent_telemetry, dict) else {}
    phone_registrations = _telemetry_list(telemetry, "phone_registrations")
    trunk_status = _telemetry_list(telemetry, "trunk_status")
    active_calls = _telemetry_list(telemetry, "active_calls")
    queue_status = _telemetry_list(telemetry, "queue_status")
    recent_calls = _telemetry_list(telemetry, "recent_calls")
    can_access_recordings = user_has_permission(user, PortalPermission.ACCESS_RECORDINGS)
    recording_metadata = [
        _recording_context(location, recording, can_access_recordings=can_access_recordings)
        for recording in _telemetry_list(telemetry, "recording_metadata")
    ]
    telemetry_errors = location.agent_telemetry_errors if isinstance(location.agent_telemetry_errors, list) else []

    latest_exported = location.config_versions.order_by("-version_number").first()
    deployed_versions = location.config_versions.filter(
        deployment_status__in=(
            ConfigVersion.DeploymentStatus.DEPLOYED,
            ConfigVersion.DeploymentStatus.ROLLED_BACK,
        )
    )
    latest_deployed = (
        deployed_versions.filter(deployed_at__isnull=False)
        .order_by("-deployed_at", "-version_number")
        .first()
    )
    if latest_deployed is None:
        latest_deployed = deployed_versions.order_by("-version_number").first()

    return {
        "location": location,
        "agent_state": _agent_state(location, telemetry_errors),
        "health": _telemetry_dict(telemetry, "location_health"),
        "phone_registrations": phone_registrations[:8],
        "trunk_status": trunk_status[:8],
        "active_calls": active_calls[:8],
        "queue_status": queue_status[:6],
        "recent_calls": recent_calls[:6],
        "recording_metadata": recording_metadata[:6],
        "telemetry_errors": telemetry_errors[:6],
        "registration_summary": _registration_summary(phone_registrations),
        "trunk_summary": _trunk_summary(trunk_status),
        "call_summary": {"total": len(active_calls), "recent": len(recent_calls)},
        "queue_summary": _queue_summary(queue_status),
        "recording_summary": {
            "total": len(recording_metadata),
            "available": any(recording["status"] == "available" for recording in recording_metadata),
            "expired": sum(1 for recording in recording_metadata if recording["status"] == "expired"),
            "unavailable": sum(1 for recording in recording_metadata if recording["status"] == "unavailable"),
        },
        "config_drift": _config_drift(location, latest_exported, latest_deployed),
        "deployment_records": location.deployment_records.select_related(
            "operator",
            "config_version",
            "rollback_source_version",
        ).order_by("-started_at", "-id")[:5],
    }


def _agent_state(location: Location, telemetry_errors: list) -> dict:
    reported_at = location.agent_telemetry_reported_at
    if reported_at is None:
        return {
            "label": "Waiting",
            "detail": "No telemetry received",
            "badge_class": "status-badge--muted",
            "is_reporting": False,
        }

    if reported_at < timezone.now() - timedelta(minutes=5):
        return {
            "label": "Stale",
            "detail": "Telemetry is older than 5 minutes",
            "badge_class": "status-badge--warning",
            "is_reporting": False,
        }

    if telemetry_errors:
        return {
            "label": "Degraded",
            "detail": f"{len(telemetry_errors)} telemetry error(s)",
            "badge_class": "status-badge--warning",
            "is_reporting": True,
        }

    return {
        "label": "Reporting",
        "detail": "Telemetry is current",
        "badge_class": "",
        "is_reporting": True,
    }


def _config_drift(location: Location, latest_exported: ConfigVersion | None, latest_deployed: ConfigVersion | None) -> dict:
    active_version = location.active_config_version_number
    exported_version = latest_exported.version_number if latest_exported else None
    deployed_version = latest_deployed.version_number if latest_deployed else None
    warnings = []

    if (exported_version or deployed_version) and active_version is None:
        warnings.append("PBX active version has not been reported.")
    if active_version and exported_version and active_version != exported_version:
        warnings.append(f"Active v{active_version} differs from latest exported v{exported_version}.")
    if active_version and deployed_version and active_version != deployed_version:
        warnings.append(f"Active v{active_version} differs from latest deployed v{deployed_version}.")
    if exported_version and deployed_version and exported_version != deployed_version:
        warnings.append(f"Latest exported v{exported_version} differs from latest deployed v{deployed_version}.")

    if warnings:
        label = "Drift warning"
        badge_class = "status-badge--warning"
    elif active_version or exported_version or deployed_version:
        label = "Aligned"
        badge_class = ""
    else:
        label = "No versions"
        badge_class = "status-badge--muted"

    return {
        "active_version": active_version,
        "exported_version": exported_version,
        "deployed_version": deployed_version,
        "latest_exported": latest_exported,
        "latest_deployed": latest_deployed,
        "warnings": warnings,
        "has_drift": bool(warnings),
        "label": label,
        "badge_class": badge_class,
    }


def _registration_summary(registrations: list[dict]) -> dict:
    reachable = sum(
        1
        for item in registrations
        if item.get("reachable") or str(item.get("status") or "").lower() == "reachable"
    )
    return {
        "total": len(registrations),
        "reachable": reachable,
        "unreachable": max(len(registrations) - reachable, 0),
    }


def _trunk_summary(trunks: list[dict]) -> dict:
    available = sum(1 for item in trunks if item.get("available"))
    return {
        "total": len(trunks),
        "available": available,
        "unavailable": max(len(trunks) - available, 0),
    }


def _queue_summary(queues: list[dict]) -> dict:
    return {
        "total": len(queues),
        "waiting": sum(_int_or_zero(item.get("calls_waiting")) for item in queues),
        "members": sum(len(item.get("members") or []) for item in queues),
    }


def _recording_metadata_for_id(location: Location, recording_id: str) -> dict | None:
    telemetry = location.agent_telemetry if isinstance(location.agent_telemetry, dict) else {}
    for recording in _telemetry_list(telemetry, "recording_metadata"):
        normalized = _recording_context(location, recording, can_access_recordings=True)
        if recording_id in {
            str(normalized.get("recording_id") or ""),
            str(normalized.get("filename") or ""),
            str(normalized.get("relative_path") or ""),
        }:
            return normalized
    return None


def _recording_context(location: Location, recording: dict, *, can_access_recordings: bool) -> dict:
    normalized = dict(recording)
    recording_id = str(normalized.get("recording_id") or "").strip()
    if not recording_id:
        raw_path = str(normalized.get("relative_path") or normalized.get("filename") or "").strip()
        recording_id = recording_id_for_path(raw_path) if raw_path else ""
    normalized["recording_id"] = recording_id
    status = _recording_status(location, normalized)
    normalized["status"] = status
    normalized["status_label"] = {
        "available": "Available",
        "expired": "Expired",
        "unavailable": "Unavailable",
    }[status]
    normalized["status_badge_class"] = {
        "available": "",
        "expired": "status-badge--warning",
        "unavailable": "status-badge--muted",
    }[status]
    normalized["can_playback"] = bool(can_access_recordings and status == "available" and recording_id)
    return normalized


def _recording_status(location: Location, recording: dict) -> str:
    if recording.get("expired") is True or str(recording.get("status") or "").lower() == "expired":
        return "expired"
    if recording.get("available") is False or not _recording_path(recording):
        return "unavailable"
    if _recording_expired_by_retention(location, recording):
        return "expired"
    return "available"


def _recording_expired_by_retention(location: Location, recording: dict) -> bool:
    expires_at = _parse_aware_datetime(recording.get("retention_expires_at"))
    if expires_at is None:
        modified_at = _parse_aware_datetime(recording.get("modified_at"))
        if modified_at is None:
            return False
        expires_at = modified_at + timedelta(days=location.recording_retention_days)
    return expires_at <= timezone.now()


def _parse_aware_datetime(value) -> datetime | None:
    if not value:
        return None
    parsed = dateparse.parse_datetime(str(value))
    if parsed is None:
        return None
    if timezone.is_naive(parsed):
        parsed = timezone.make_aware(parsed, datetime_timezone.utc)
    return parsed


def _recording_path(recording: dict) -> str:
    return str(recording.get("path") or recording.get("relative_path") or "").strip()


def _recording_filename(recording: dict) -> str:
    return str(recording.get("filename") or "").strip()


def _telemetry_list(telemetry: dict, key: str) -> list:
    value = telemetry.get(key)
    if not isinstance(value, list):
        return []
    return [item for item in value if isinstance(item, dict)]


def _telemetry_dict(telemetry: dict, key: str) -> dict:
    value = telemetry.get(key)
    return value if isinstance(value, dict) else {}


def _int_or_zero(value) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


def _location_context(request, context):
    context.update(
        {
            "areas": visible_portal_areas(request.user),
            "can_edit_locations": user_has_permission(request.user, PortalPermission.EDIT_CONFIG),
            "can_manage_location_secrets": _can_manage_location_secrets(request),
            "can_export_config": user_has_permission(request.user, PortalPermission.EDIT_CONFIG),
            "can_deploy_config": user_has_permission(request.user, PortalPermission.RUN_LIVE_OPERATIONS),
            "can_run_live_operations": user_has_permission(request.user, PortalPermission.RUN_LIVE_OPERATIONS),
            "live_commands": supported_live_commands(),
        }
    )
    return context


def _location_detail_context(request, location: Location, extra: dict | None = None):
    context = {
        "location": location,
        "config_versions": location.config_versions.select_related(
            "exported_by",
            "deployed_by",
            "rollback_of",
        ).order_by("-version_number"),
        "deployment_records": location.deployment_records.select_related(
            "operator",
            "config_version",
            "rollback_source_version",
        ).order_by("-started_at", "-id")[:10],
    }
    if extra:
        context.update(extra)
    return _location_context(request, context)


def _config_version_or_404(slug: str, version_number: int) -> ConfigVersion:
    return get_object_or_404(
        ConfigVersion.objects.select_related("location", "exported_by", "deployed_by"),
        location__slug=slug,
        version_number=version_number,
    )


def _reload_confirmed(request) -> bool:
    return request.POST.get("confirm_reload") in {"1", "on", "true", "yes"}


def _live_operation_payload(request):
    if _request_is_json(request):
        return _json_payload(request)
    return {"command": request.POST.get("command", ""), "parameters": {}}, None


def _request_is_json(request) -> bool:
    return str(getattr(request, "content_type", "") or "").split(";", 1)[0] == "application/json"


def _wants_json(request) -> bool:
    return _request_is_json(request) or "application/json" in request.headers.get("Accept", "")


def _live_operation_response(request, location: Location, payload: dict, *, status: int):
    if _wants_json(request):
        return JsonResponse(payload, status=status)
    context = _location_detail_context(request, location, {"live_operation_result": payload})
    return render(
        request,
        _template(request, "core/locations/detail.html", "core/partials/location_detail.html"),
        context,
        status=status,
    )


def _record_live_operation_audit(
    actor,
    location: Location,
    command_name: str,
    outcome: AuditOutcome,
    result: dict,
    *,
    request=None,
    parameters: dict | None = None,
) -> None:
    record_audit(
        actor=actor,
        action=AuditAction.LIVE_PBX_ACTION,
        target=f"locations/{location.slug}/live/{command_name or 'missing'}",
        outcome=outcome,
        details=build_live_operation_audit_details(
            actor=actor,
            location=location,
            command_name=command_name,
            parameters=parameters,
            result=result,
            api_key=getattr(request, "api_key", None),
        ),
    )


def _recording_playback_failure_response(
    request,
    location: Location,
    recording_id: str,
    recording: dict,
    status_name: str,
    error: str,
    status_code: int,
    *,
    result: dict | None = None,
) -> HttpResponse:
    details = {"status": status_name, "error": error}
    if result:
        details["agent_result"] = {
            key: value
            for key, value in result.items()
            if key not in {"content_base64"}
        }
    _record_recording_playback_audit(
        request.user,
        location,
        recording_id,
        AuditOutcome.FAILURE,
        recording=recording,
        details=details,
    )
    return HttpResponse(error, status=status_code)


def _record_recording_playback_audit(
    actor,
    location: Location,
    recording_id: str,
    outcome: AuditOutcome,
    *,
    recording: dict | None = None,
    details: dict | None = None,
) -> None:
    actor_username = actor.get_username() if getattr(actor, "is_authenticated", False) else "anonymous"
    audit_details = {
        "actor_username": actor_username,
        "location_id": location.id,
        "location_slug": location.slug,
        "recording_id": recording_id,
    }
    if recording:
        audit_details.update(
            {
                "filename": _recording_filename(recording),
                "relative_path": recording.get("relative_path") or "",
                "uniqueid": recording.get("uniqueid") or "",
                "retention_days": location.recording_retention_days,
                "retention_expires_at": recording.get("retention_expires_at"),
                "metadata_status": recording.get("status") or "",
            }
        )
    if details:
        audit_details.update(details)

    record_audit(
        actor=actor,
        action=AuditAction.RECORDING_PLAYBACK,
        target=f"locations/{location.slug}/recordings/{recording_id}",
        outcome=outcome,
        details=audit_details,
    )


def _mark_config_version_deployed(version: ConfigVersion, user, *, rolled_back: bool):
    with transaction.atomic():
        version = ConfigVersion.objects.select_for_update().select_related("location").get(pk=version.pk)
        version.mark_deployed(user, rolled_back=rolled_back)
        version.location.last_deployed_at = version.deployed_at
        version.location.deployment_status = Location.DeploymentStatus.DEPLOYED
        version.location.save(update_fields=["last_deployed_at", "deployment_status", "updated_at"])
    record_audit(
        actor=user,
        action=AuditAction.DEPLOYMENT,
        target=f"locations/{version.location.slug}/config/v{version.version_number}",
        outcome=AuditOutcome.SUCCESS,
        details={
            "location_id": version.location_id,
            "location_slug": version.location.slug,
            "config_version_id": version.id,
            "version_number": version.version_number,
            "checksum": version.checksum,
            "rolled_back": rolled_back,
        },
    )


def _can_manage_location_secrets(request) -> bool:
    return user_has_permission(request.user, PortalPermission.ADMINISTER)


def _extension_queryset():
    return Extension.objects.select_related("location").prefetch_related(
        "direct_dids",
        "ring_group_memberships__ring_group",
        "queue_memberships__queue",
        "paging_group_memberships__paging_group",
    )


def _extension_context(request, context):
    context.update(
        {
            "areas": visible_portal_areas(request.user),
            "can_edit_extensions": user_has_permission(request.user, PortalPermission.EDIT_CONFIG),
            "can_disable_911": _can_disable_911(request),
        }
    )
    return context


def _can_disable_911(request) -> bool:
    return user_has_permission(request.user, PortalPermission.ADMINISTER)


def _phone_queryset():
    return Phone.objects.select_related("location").prefetch_related(
        "line_appearances__extension",
        "speed_dials",
    )


def _phone_context(request, context):
    context.update(
        {
            "areas": visible_portal_areas(request.user),
            "can_edit_phones": user_has_permission(request.user, PortalPermission.EDIT_CONFIG),
        }
    )
    return context


def _trunk_context(request, context):
    context.update(
        {
            "areas": visible_portal_areas(request.user),
            "can_edit_trunks": user_has_permission(request.user, PortalPermission.EDIT_CONFIG),
        }
    )
    return context


def _dial_plan_context(request, context):
    context.update(
        {
            "areas": visible_portal_areas(request.user),
            "can_edit_dial_plan": user_has_permission(request.user, PortalPermission.EDIT_CONFIG),
        }
    )
    return context


def _dial_plan_validation():
    validation_by_location = []
    for location in Location.objects.filter(is_active=True).order_by("name"):
        validation = validate_location_routing(location, require_emergency=True)
        if validation["warnings"] or validation["errors"]:
            validation_by_location.append(
                {
                    "location": location,
                    "warnings": validation["warnings"],
                    "errors": validation["errors"],
                    "has_errors": bool(validation["errors"]),
                }
            )
    return validation_by_location


def _phone_formset_location(request, phone):
    if request.method == "POST":
        location_id = request.POST.get("location")
        if location_id:
            try:
                return Location.objects.get(pk=location_id)
            except (Location.DoesNotExist, ValueError):
                return None
        return None
    if phone and phone.pk:
        return phone.location
    return None


def _outbound_route_formset_location(request, route):
    if request.method == "POST":
        location_id = request.POST.get("location")
        if location_id:
            try:
                return Location.objects.get(pk=location_id)
            except (Location.DoesNotExist, ValueError):
                return None
        return None
    if route and route.pk:
        return route.location
    return None


def _record_denied_911_if_needed(request, form, extension_number):
    if form.denied_911_disable:
        _record_911_disable(request, extension_number, AuditOutcome.DENIED, "form")


def _record_911_disable(request, extension_or_number, outcome, source):
    if isinstance(extension_or_number, Extension):
        extension_number = extension_or_number.number
    else:
        extension_number = extension_or_number
    record_audit(
        actor=request.user,
        action=AuditAction.CONFIG_CHANGE,
        target=f"extensions/{extension_number}/911",
        outcome=outcome,
        details={"source": source, "emergency_calling_enabled": False},
    )


def _csv_response(csv_text, filename):
    response = HttpResponse(csv_text, content_type="text/csv")
    response["Content-Disposition"] = f'attachment; filename="{filename}"'
    return response


def _csv_import_response(
    request,
    *,
    import_function,
    context_builder,
    area_slug,
    eyebrow,
    title,
    back_url,
    template_url,
    result_label,
):
    dry_run = request.method != "POST" or request.POST.get("dry_run") == "on"
    context = {
        "area_slug": area_slug,
        "eyebrow": eyebrow,
        "import_title": title,
        "back_url": back_url,
        "template_url": template_url,
        "dry_run": dry_run,
    }
    if request.method == "POST":
        upload = request.FILES.get("csv_file")
        if upload is None:
            context["import_errors"] = [f"Choose a {result_label} CSV file."]
        else:
            try:
                result = import_function(upload, actor=request.user, dry_run=dry_run)
            except PhoneCSVImportError as exc:
                context["import_errors"] = exc.errors
            else:
                verb = "Validated" if result.dry_run else "Imported"
                context["import_result"] = f"{verb} {result.planned_count} {result_label} row(s)."
                context["import_changes"] = result.change_messages()

    return render(
        request,
        _template(request, "core/csv_import.html", "core/partials/csv_import_content.html"),
        context_builder(request, context),
    )
