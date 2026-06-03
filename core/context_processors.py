from .access import get_user_role_label
from .navigation import visible_portal_areas


def portal_access(request):
    user = getattr(request, "user", None)
    return {
        "portal_nav_areas": visible_portal_areas(user),
        "portal_user_role_label": get_user_role_label(user),
    }
