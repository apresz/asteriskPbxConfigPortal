from django.http import Http404, JsonResponse
from django.shortcuts import render


PORTAL_AREAS = {
    "extensions": {
        "label": "Extensions",
        "summary": "Internal users, devices, and extension assignments.",
        "status": "Ready for provisioning model work",
    },
    "trunks": {
        "label": "Trunks",
        "summary": "Carrier trunks, SIP endpoints, and upstream routing.",
        "status": "Ready for provider configuration work",
    },
    "dial-plan": {
        "label": "Dial Plan",
        "summary": "Inbound, outbound, and emergency call routing.",
        "status": "Ready for rule builder work",
    },
    "settings": {
        "label": "Settings",
        "summary": "Portal environment, deployment, and access controls.",
        "status": "Ready for administrative settings work",
    },
}


def health(request):
    return JsonResponse({"status": "ok"})


def home(request):
    context = {"areas": PORTAL_AREAS}
    return render(request, _template(request, "core/home.html", "core/partials/home_content.html"), context)


def portal_area(request, slug: str):
    area = PORTAL_AREAS.get(slug)
    if area is None:
        raise Http404("Unknown portal area")

    context = {"area": area, "slug": slug, "areas": PORTAL_AREAS}
    return render(request, _template(request, "core/area.html", "core/partials/area_content.html"), context)


def _template(request, full_template: str, partial_template: str) -> str:
    if request.headers.get("HX-Request") == "true":
        return partial_template
    return full_template

