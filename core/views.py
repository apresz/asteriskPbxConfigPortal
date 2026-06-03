from django.contrib.auth.decorators import login_required
from django.core.exceptions import PermissionDenied
from django.http import Http404, HttpResponse, JsonResponse
from django.shortcuts import get_object_or_404, redirect, render

from .access import permission_required, user_has_permission
from .audit import record_audit
from .extension_csv import (
    ExtensionCSVError,
    export_extensions_csv,
    extension_template_csv,
    import_extensions_csv,
)
from .extension_management import clear_extension_relationships, is_911_disable_change
from .forms import ExtensionForm, LocationForm
from .models import AuditAction, AuditOutcome, Extension, Location, PortalPermission
from .navigation import PORTAL_AREAS, visible_portal_areas


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
            extension = form.save()
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
        original_911_enabled = extension.emergency_calling_enabled
        form = ExtensionForm(request.POST, instance=extension, can_disable_911=can_disable_911)
        if form.is_valid():
            logs_911_disable = original_911_enabled and not form.cleaned_data["emergency_calling_enabled"]
            extension = form.save()
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
        clear_extension_relationships(extension)
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


def _template(request, full_template: str, partial_template: str) -> str:
    if request.headers.get("HX-Request") == "true":
        return partial_template
    return full_template


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
