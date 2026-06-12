from .access import get_user_role_label
from .navigation import visible_portal_areas


ROUTE_AREA_PREFIXES = {
    "admin-backup": "backups",
    "api-key": "api-keys",
    "did": "dids",
    "extension": "extensions",
    "feature-code": "feature-codes",
    "inbound-destination": "inbound-destinations",
    "ivr": "ivrs",
    "location": "locations",
    "outbound-route": "dial-plan",
    "paging-group": "paging-groups",
    "phone": "phones",
    "provider": "trunks",
    "queue": "queues",
    "ring-group": "ring-groups",
    "speed-dial": "phones",
    "trunk": "trunks",
    "user-role": "users-roles",
}

ROUTE_AREA_NAMES = {
    "api-keys",
    "audit-log",
    "backups",
    "dial-plan",
    "dids",
    "extensions",
    "feature-codes",
    "home",
    "inbound-destinations",
    "ivrs",
    "locations",
    "paging-groups",
    "phone-book",
    "phones",
    "queues",
    "recordings",
    "ring-groups",
    "settings",
    "trunks",
    "users-roles",
}


def portal_access(request):
    user = getattr(request, "user", None)
    url_name = getattr(getattr(request, "resolver_match", None), "url_name", "") or ""
    return {
        "portal_nav_areas": visible_portal_areas(user),
        "portal_user_role_label": get_user_role_label(user),
        "portal_active_area": _active_area(url_name),
    }


def _active_area(url_name: str) -> str:
    if url_name in ROUTE_AREA_NAMES:
        return url_name
    for prefix, area in ROUTE_AREA_PREFIXES.items():
        if url_name.startswith(prefix):
            return area
    return ""
