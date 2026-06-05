"""Pure-Python portal access metadata for source-inspection tests."""

ROLE_PERMISSIONS = {
    "viewer": frozenset({"view"}),
    "editor": frozenset({"view", "edit_config"}),
    "operator": frozenset({"view", "run_live_operations", "access_recordings"}),
    "admin": frozenset(
        {
            "view",
            "edit_config",
            "run_live_operations",
            "access_recordings",
            "administer",
        }
    ),
}


AREA_PERMISSIONS = {
    "locations": "view",
    "extensions": "view",
    "phone-book": "view",
    "phones": "view",
    "inbound-destinations": "view",
    "dids": "view",
    "ivrs": "view",
    "ring-groups": "view",
    "queues": "view",
    "paging-groups": "view",
    "feature-codes": "view",
    "trunks": "view",
    "dial-plan": "view",
    "recordings": "access_recordings",
    "audit-log": "administer",
    "users-roles": "administer",
    "api-keys": "administer",
    "backups": "administer",
    "settings": "administer",
}


SENSITIVE_AREA_AUDIT_ACTIONS = {
    "recordings": ("recording_playback",),
    "audit-log": (),
    "users-roles": ("api_user_update",),
    "api-keys": ("api_key_create", "api_key_rotate", "api_key_revoke"),
    "backups": ("backup_create", "backup_download"),
}


REQUIRED_FIRST_CLASS_AREAS = (
    "phone-book",
    "recordings",
    "audit-log",
    "users-roles",
    "api-keys",
    "backups",
)


def area_visible_to_role(area_slug: str, role: str) -> bool:
    return AREA_PERMISSIONS[area_slug] in ROLE_PERMISSIONS[role]
