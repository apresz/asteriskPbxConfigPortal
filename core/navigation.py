from .access import user_has_permission
from .models import PortalPermission


PORTAL_AREAS = {
    "locations": {
        "label": "Locations",
        "summary": "Site network, deployment, emergency, and PBX service settings.",
        "status": "Manage location records",
        "permission": PortalPermission.VIEW,
    },
    "extensions": {
        "label": "Extensions",
        "summary": "Internal users, devices, and extension assignments.",
        "status": "Manage extension records",
        "permission": PortalPermission.VIEW,
    },
    "phones": {
        "label": "Phones",
        "summary": "Cisco phone inventory, line appearances, and speed-dial assignments.",
        "status": "Manage Cisco phone provisioning data",
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
