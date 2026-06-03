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
    "inbound-destinations": {
        "label": "Destinations",
        "summary": "Reusable inbound targets for DIDs, IVRs, queues, and feature codes.",
        "status": "Manage inbound routing targets",
        "permission": PortalPermission.VIEW,
    },
    "dids": {
        "label": "DIDs",
        "summary": "Direct inbound DID routing and default destination fallback.",
        "status": "Manage inbound numbers",
        "permission": PortalPermission.VIEW,
    },
    "ivrs": {
        "label": "IVRs",
        "summary": "Business-hours, after-hours, timeout, invalid-input, and menu routing.",
        "status": "Manage IVR menus",
        "permission": PortalPermission.VIEW,
    },
    "ring-groups": {
        "label": "Ring Groups",
        "summary": "Static ring groups with member extensions, strategy, and timeout.",
        "status": "Manage ring groups",
        "permission": PortalPermission.VIEW,
    },
    "queues": {
        "label": "Queues",
        "summary": "Static queues with strategy, retry, timeout, MOH, and overflow.",
        "status": "Manage queues",
        "permission": PortalPermission.VIEW,
    },
    "paging-groups": {
        "label": "Paging Groups",
        "summary": "Static paging groups with dialable page codes and members.",
        "status": "Manage paging groups",
        "permission": PortalPermission.VIEW,
    },
    "feature-codes": {
        "label": "Feature Codes",
        "summary": "PBX feature-code assignments for voicemail, pickup, park, paging, and custom actions.",
        "status": "Manage feature codes",
        "permission": PortalPermission.VIEW,
    },
    "trunks": {
        "label": "Trunks",
        "summary": "Carrier trunks, SIP endpoints, and upstream routing.",
        "status": "Manage provider trunks and credentials",
        "permission": PortalPermission.VIEW,
    },
    "dial-plan": {
        "label": "Dial Plan",
        "summary": "Inbound, outbound, and emergency call routing.",
        "status": "Manage outbound route fallback",
        "permission": PortalPermission.VIEW,
    },
    "settings": {
        "label": "Settings",
        "summary": "Admin backups, portal environment, deployment, and access controls.",
        "status": "Generate off-host backup archives",
        "permission": PortalPermission.ADMINISTER,
    },
}


def visible_portal_areas(user):
    return {
        slug: area
        for slug, area in PORTAL_AREAS.items()
        if user_has_permission(user, area["permission"])
    }
