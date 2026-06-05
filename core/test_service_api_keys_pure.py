from pathlib import Path
from types import SimpleNamespace
import unittest

from .live_operations import build_live_operation_audit_details
from .service_principals import (
    ServicePermissionError,
    is_service_principal,
    normalize_service_permissions,
    service_principal_from_identity,
    service_principal_has_permission,
)


ROOT = Path(__file__).resolve().parents[1]


class ServicePrincipalPermissionTests(unittest.TestCase):
    def test_normalize_service_permissions_validates_explicit_scope_list(self):
        self.assertEqual(
            normalize_service_permissions(["administer", "view", "view", "run_live_operations"]),
            ("view", "run_live_operations", "administer"),
        )

    def test_normalize_service_permissions_rejects_unusable_scope_values(self):
        with self.assertRaisesRegex(ServicePermissionError, "list of permission strings"):
            normalize_service_permissions("administer")

        with self.assertRaisesRegex(ServicePermissionError, "list of permission strings"):
            normalize_service_permissions({"administer": True})

        with self.assertRaisesRegex(ServicePermissionError, "Unsupported service identity permission"):
            normalize_service_permissions(["raw_ami"])

    def test_service_principal_enforces_permission_boundaries(self):
        identity = SimpleNamespace(
            id=7,
            name="Provisioning Service",
            slug="provisioning",
            is_active=True,
            permissions=["run_live_operations"],
        )

        principal = service_principal_from_identity(identity)

        self.assertTrue(is_service_principal(principal))
        self.assertTrue(principal.is_active)
        self.assertEqual(principal.get_username(), "service:provisioning")
        self.assertTrue(service_principal_has_permission(principal, "run_live_operations"))
        self.assertFalse(service_principal_has_permission(principal, "administer"))


class ServiceAPIKeySourceTests(unittest.TestCase):
    def test_service_identity_model_declares_explicit_permissions(self):
        source = _source("core/models.py")

        self.assertIn("permissions = models.JSONField(default=list, blank=True)", source)
        self.assertIn("normalize_service_permissions(self.permissions)", source)

    def test_middleware_authenticates_service_keys_as_principals(self):
        source = _source("core/middleware.py")

        self.assertIn("service_principal_from_identity(api_key.service_identity)", source)
        self.assertIn("request.api_service_identity = api_key.service_identity", source)
        self.assertNotIn("API key must be scoped to an active user.", source)

    def test_api_decorator_checks_principal_permissions(self):
        source = _source("core/access.py")

        self.assertIn("def principal_has_permission", source)
        self.assertIn("principal = getattr(request, \"api_principal\", None) or request.user", source)
        self.assertIn("service_principal_has_permission(principal", source)

    def test_service_identity_permissions_are_accepted_and_serialized(self):
        source = _source("core/views.py")

        self.assertIn("_payload_service_permissions(payload.get(\"permissions\", []))", source)
        self.assertIn("_payload_service_permissions(payload[\"permissions\"])", source)
        self.assertIn("\"permissions\": list(identity.permissions or [])", source)


class ServiceAPIKeyAuditTests(unittest.TestCase):
    def test_live_operation_audit_details_include_service_identity_use(self):
        identity = SimpleNamespace(
            id=7,
            name="Provisioning Service",
            slug="provisioning",
            is_active=True,
            permissions=["run_live_operations"],
        )
        principal = service_principal_from_identity(identity)
        api_key = SimpleNamespace(
            id=11,
            name="provisioning key",
            prefix="pbx_abc123",
            scope_type="service_identity",
            user_id=None,
            service_identity_id=identity.id,
            service_identity=identity,
        )

        details = build_live_operation_audit_details(
            actor=principal,
            location=SimpleNamespace(id=3, slug="hq"),
            command_name="core_reload",
            parameters={},
            result={"status": "success"},
            api_key=api_key,
        )

        self.assertEqual(details["actor_username"], "service:provisioning")
        self.assertEqual(details["api_key_scope_type"], "service_identity")
        self.assertEqual(details["api_key_service_identity_id"], identity.id)
        self.assertEqual(details["api_key_service_identity_slug"], "provisioning")


def _source(relative_path: str) -> str:
    return (ROOT / relative_path).read_text(encoding="utf-8")


if __name__ == "__main__":
    unittest.main()
