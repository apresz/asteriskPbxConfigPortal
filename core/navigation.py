from .access import user_has_permission
from .models import PortalPermission


PORTAL_AREAS = {
    "extensions": {
        "label": "Extensions",
        "summary": "Internal users, devices, and extension assignments.",
        "status": "Ready for provisioning model work",
        "permission": PortalPermission.VIEW,
    },
    "trunks": {
        "label": "Trunks",
        "summary": "Carrier trunks, SIP endpoints, and upstream routing.",
        "status": "Ready for provider configuration work",
        "permission": PortalPermission.VIEW,
    },
    "dial-plan": {
        "label": "Dial Plan",
        "summary": "Inbound, outbound, and emergency call routing.",
        "status": "Ready for rule builder work",
        "permission": PortalPermission.VIEW,
    },
    "settings": {
        "label": "Settings",
        "summary": "Portal environment, deployment, and access controls.",
        "status": "Ready for administrative settings work",
        "permission": PortalPermission.ADMINISTER,
    },
}


def visible_portal_areas(user):
    return {
        slug: area
        for slug, area in PORTAL_AREAS.items()
        if user_has_permission(user, area["permission"])
    }
