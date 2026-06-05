import ast
import unittest
from pathlib import Path

from core.portal_area_access import AREA_PERMISSIONS, REQUIRED_FIRST_CLASS_AREAS, area_visible_to_role


ROOT = Path(__file__).resolve().parent

EXPECTED_AREAS = {
    "phone-book": {
        "label": "Phone Book",
        "permission": "view",
        "template": "templates/core/partials/phone_book_content.html",
        "tokens": ("data-area=\"phone-book\"", "Phone Book", "{% url 'extensions' %}", "{% url 'phones' %}"),
    },
    "recordings": {
        "label": "Recordings",
        "permission": "access_recordings",
        "template": "templates/core/partials/recordings_content.html",
        "tokens": ("data-area=\"recordings\"", "Recordings", "location-recording-playback", "Recording playback"),
    },
    "audit-log": {
        "label": "Audit Log",
        "permission": "administer",
        "template": "templates/core/partials/audit_log_content.html",
        "tokens": ("data-area=\"audit-log\"", "Audit Log", "audit_entries", "entry.details.items"),
    },
    "users-roles": {
        "label": "Users/Roles",
        "permission": "administer",
        "template": "templates/core/partials/users_roles_content.html",
        "tokens": ("data-area=\"users-roles\"", "Users/Roles", "user-role-update", "Role permissions"),
    },
    "api-keys": {
        "label": "API Keys",
        "permission": "administer",
        "template": "templates/core/partials/api_keys_content.html",
        "tokens": ("data-area=\"api-keys\"", "API Keys", "api-key-rotate-ui", "api-key-revoke-ui"),
    },
    "backups": {
        "label": "Backups",
        "permission": "administer",
        "template": "templates/core/partials/backups_content.html",
        "tokens": ("data-area=\"backups\"", "Backups", "admin-backup-create", "admin-backup-download"),
    },
}


class PortalNavigationSourceTests(unittest.TestCase):
    def test_required_portal_areas_are_declared_in_navigation(self):
        source = _source("core/navigation.py")
        portal_areas = _portal_area_nodes(source)

        self.assertEqual(tuple(EXPECTED_AREAS), REQUIRED_FIRST_CLASS_AREAS)
        for slug, expected in EXPECTED_AREAS.items():
            with self.subTest(slug=slug):
                self.assertIn(slug, portal_areas)
                self.assertEqual(_literal_field(portal_areas[slug], "label"), expected["label"])

    def test_navigation_uses_permission_manifest_for_required_areas(self):
        source = _source("core/navigation.py")
        for slug in EXPECTED_AREAS:
            with self.subTest(slug=slug):
                self.assertIn(f'AREA_PERMISSIONS["{slug}"]', source)


class PortalTemplateTests(unittest.TestCase):
    def test_required_htmx_partials_exist_and_expose_expected_labels_and_anchors(self):
        for slug, expected in EXPECTED_AREAS.items():
            with self.subTest(slug=slug):
                template = ROOT / expected["template"]
                self.assertTrue(template.exists(), f"{template} should exist")
                content = template.read_text(encoding="utf-8")
                for token in expected["tokens"]:
                    self.assertIn(token, content)

    def test_full_templates_include_required_htmx_partials(self):
        wrappers = {
            "phone-book": ("templates/core/phone_book.html", "core/partials/phone_book_content.html"),
            "recordings": ("templates/core/recordings.html", "core/partials/recordings_content.html"),
            "audit-log": ("templates/core/audit_log.html", "core/partials/audit_log_content.html"),
            "users-roles": ("templates/core/users_roles.html", "core/partials/users_roles_content.html"),
            "api-keys": ("templates/core/api_keys.html", "core/partials/api_keys_content.html"),
            "backups": ("templates/core/backups.html", "core/partials/backups_content.html"),
        }
        for slug, (wrapper_path, partial_name) in wrappers.items():
            with self.subTest(slug=slug):
                content = _source(wrapper_path)
                self.assertIn(partial_name, content)


class PortalPermissionManifestTests(unittest.TestCase):
    def test_required_area_permissions_are_role_gated(self):
        expected_permissions = {slug: data["permission"] for slug, data in EXPECTED_AREAS.items()}
        for slug, permission in expected_permissions.items():
            with self.subTest(slug=slug):
                self.assertEqual(AREA_PERMISSIONS[slug], permission)

    def test_role_visibility_for_required_areas(self):
        self.assertTrue(area_visible_to_role("phone-book", "viewer"))
        self.assertTrue(area_visible_to_role("phone-book", "editor"))
        self.assertTrue(area_visible_to_role("phone-book", "operator"))
        self.assertTrue(area_visible_to_role("phone-book", "admin"))

        self.assertFalse(area_visible_to_role("recordings", "viewer"))
        self.assertFalse(area_visible_to_role("recordings", "editor"))
        self.assertTrue(area_visible_to_role("recordings", "operator"))
        self.assertTrue(area_visible_to_role("recordings", "admin"))

        for slug in ("audit-log", "users-roles", "api-keys", "backups"):
            with self.subTest(slug=slug):
                self.assertFalse(area_visible_to_role(slug, "viewer"))
                self.assertFalse(area_visible_to_role(slug, "editor"))
                self.assertFalse(area_visible_to_role(slug, "operator"))
                self.assertTrue(area_visible_to_role(slug, "admin"))


def _source(relative_path):
    return (ROOT / relative_path).read_text(encoding="utf-8")


def _portal_area_nodes(source):
    tree = ast.parse(source)
    for node in tree.body:
        if isinstance(node, ast.Assign) and any(getattr(target, "id", "") == "PORTAL_AREAS" for target in node.targets):
            return {
                key.value: value
                for key, value in zip(node.value.keys, node.value.values)
                if isinstance(key, ast.Constant)
            }
    raise AssertionError("PORTAL_AREAS assignment was not found")


def _literal_field(area_node, field_name):
    for key, value in zip(area_node.keys, area_node.values):
        if isinstance(key, ast.Constant) and key.value == field_name:
            return ast.literal_eval(value)
    raise AssertionError(f"{field_name} field was not found")


if __name__ == "__main__":
    unittest.main()
