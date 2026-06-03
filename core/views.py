from django.contrib.auth.decorators import login_required
from django.core.exceptions import PermissionDenied
from django.http import Http404, JsonResponse
from django.shortcuts import get_object_or_404, redirect, render

from .access import permission_required, user_has_permission
from .forms import LocationForm
from .models import Location, PortalPermission
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
