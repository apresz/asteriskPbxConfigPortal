from .access import user_has_permission
from .models import PortalPermission
from .portal_area_access import AREA_PERMISSIONS, SENSITIVE_AREA_AUDIT_ACTIONS


PORTAL_AREAS = {
    "locations": {
        "label": "Locations",
        "summary": "Site network, deployment, emergency, and PBX service settings.",
        "status": "Manage location records",
        "permission": PortalPermission(AREA_PERMISSIONS["locations"]),
    },
    "extensions": {
        "label": "Extensions",
        "summary": "Internal users, devices, and extension assignments.",
        "status": "Manage extension records",
        "permission": PortalPermission(AREA_PERMISSIONS["extensions"]),
    },
    "phone-book": {
        "label": "Phone Book",
        "summary": "Directory view of extensions, DIDs, assigned phones, and active contact details.",
        "status": "Inspect dialable contacts",
        "permission": PortalPermission(AREA_PERMISSIONS["phone-book"]),
    },
    "phones": {
        "label": "Phones",
        "summary": "Cisco phone inventory, line appearances, and speed-dial assignments.",
        "status": "Manage Cisco phone provisioning data",
        "permission": PortalPermission(AREA_PERMISSIONS["phones"]),
    },
    "inbound-destinations": {
        "label": "Destinations",
        "summary": "Reusable inbound targets for DIDs, IVRs, queues, and feature codes.",
        "status": "Manage inbound routing targets",
        "permission": PortalPermission(AREA_PERMISSIONS["inbound-destinations"]),
    },
    "dids": {
        "label": "DIDs",
        "summary": "Direct inbound DID routing and default destination fallback.",
        "status": "Manage inbound numbers",
        "permission": PortalPermission(AREA_PERMISSIONS["dids"]),
    },
    "ivrs": {
        "label": "IVRs",
        "summary": "Business-hours, after-hours, timeout, invalid-input, and menu routing.",
        "status": "Manage IVR menus",
        "permission": PortalPermission(AREA_PERMISSIONS["ivrs"]),
    },
    "ring-groups": {
        "label": "Ring Groups",
        "summary": "Static ring groups with member extensions, strategy, and timeout.",
        "status": "Manage ring groups",
        "permission": PortalPermission(AREA_PERMISSIONS["ring-groups"]),
    },
    "queues": {
        "label": "Queues",
        "summary": "Static queues with strategy, retry, timeout, MOH, and overflow.",
        "status": "Manage queues",
        "permission": PortalPermission(AREA_PERMISSIONS["queues"]),
    },
    "paging-groups": {
        "label": "Paging Groups",
        "summary": "Static paging groups with dialable page codes and members.",
        "status": "Manage paging groups",
        "permission": PortalPermission(AREA_PERMISSIONS["paging-groups"]),
    },
    "feature-codes": {
        "label": "Feature Codes",
        "summary": "PBX feature-code assignments for voicemail, pickup, park, paging, and custom actions.",
        "status": "Manage feature codes",
        "permission": PortalPermission(AREA_PERMISSIONS["feature-codes"]),
    },
    "trunks": {
        "label": "Trunks",
        "summary": "Carrier trunks, SIP endpoints, and upstream routing.",
        "status": "Manage provider trunks and credentials",
        "permission": PortalPermission(AREA_PERMISSIONS["trunks"]),
    },
    "dial-plan": {
        "label": "Dial Plan",
        "summary": "Inbound, outbound, and emergency call routing.",
        "status": "Manage outbound route fallback",
        "permission": PortalPermission(AREA_PERMISSIONS["dial-plan"]),
    },
    "recordings": {
        "label": "Recordings",
        "summary": "Location recording metadata, retention status, and audited playback access.",
        "status": "Review call recording availability",
        "permission": PortalPermission(AREA_PERMISSIONS["recordings"]),
        "audit_events": SENSITIVE_AREA_AUDIT_ACTIONS["recordings"],
    },
    "audit-log": {
        "label": "Audit Log",
        "summary": "Recent portal, API, deployment, backup, and recording activity.",
        "status": "Inspect sensitive action history",
        "permission": PortalPermission(AREA_PERMISSIONS["audit-log"]),
        "audit_events": SENSITIVE_AREA_AUDIT_ACTIONS["audit-log"],
    },
    "users-roles": {
        "label": "Users/Roles",
        "summary": "Named portal users, assigned roles, and role permission coverage.",
        "status": "Manage portal role assignments",
        "permission": PortalPermission(AREA_PERMISSIONS["users-roles"]),
        "audit_events": SENSITIVE_AREA_AUDIT_ACTIONS["users-roles"],
    },
    "api-keys": {
        "label": "API Keys",
        "summary": "User and service-identity API keys with audited create, rotate, and revoke actions.",
        "status": "Manage automation credentials",
        "permission": PortalPermission(AREA_PERMISSIONS["api-keys"]),
        "audit_events": SENSITIVE_AREA_AUDIT_ACTIONS["api-keys"],
    },
    "backups": {
        "label": "Backups",
        "summary": "Admin backup archives for off-host storage and audited download handling.",
        "status": "Generate off-host backup archives",
        "permission": PortalPermission(AREA_PERMISSIONS["backups"]),
        "audit_events": SENSITIVE_AREA_AUDIT_ACTIONS["backups"],
    },
    "settings": {
        "label": "Settings",
        "summary": "Portal administration index for protected operator and admin areas.",
        "status": "Review protected portal areas",
        "permission": PortalPermission(AREA_PERMISSIONS["settings"]),
    },
}


def visible_portal_areas(user):
    return {
        slug: area
        for slug, area in PORTAL_AREAS.items()
        if user_has_permission(user, area["permission"])
    }
