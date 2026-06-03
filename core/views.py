from django.contrib.auth.decorators import login_required
from django.core.exceptions import PermissionDenied
from django.http import Http404, JsonResponse
from django.shortcuts import render

from .access import permission_required, user_has_permission
from .models import PortalPermission
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


def _template(request, full_template: str, partial_template: str) -> str:
    if request.headers.get("HX-Request") == "true":
        return partial_template
    return full_template
