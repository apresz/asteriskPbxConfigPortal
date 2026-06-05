import ast
import unittest
from pathlib import Path

from core.audit_helpers import (
    REDACTED_VALUE,
    audit_model_summary,
    build_config_change_details,
    redact_audit_mapping,
)


ROOT = Path(__file__).resolve().parent


class AuditHelperTests(unittest.TestCase):
    def test_redacts_secret_credential_and_admin_sensitive_fields(self):
        redacted = redact_audit_mapping(
            {
                "name": "HQ",
                "username": "carrier-user",
                "sip_password": "sip-secret",
                "voicemail_pin": "1234",
                "deployment_ssh_private_key": "private-key",
                "deployment_staging_path": "/srv/pbx/staging",
                "nested": {"agent_token": "agent-token", "label": "visible"},
            }
        )

        self.assertEqual(redacted["name"], "HQ")
        self.assertEqual(redacted["username"], REDACTED_VALUE)
        self.assertEqual(redacted["sip_password"], REDACTED_VALUE)
        self.assertEqual(redacted["voicemail_pin"], REDACTED_VALUE)
        self.assertEqual(redacted["deployment_ssh_private_key"], REDACTED_VALUE)
        self.assertEqual(redacted["deployment_staging_path"], REDACTED_VALUE)
        self.assertEqual(redacted["nested"]["agent_token"], REDACTED_VALUE)
        self.assertEqual(redacted["nested"]["label"], "visible")

    def test_model_summary_and_change_details_preserve_changed_secret_field_names(self):
        class Field:
            def __init__(self, name, attname=None):
                self.name = name
                self.attname = attname or name

        class Meta:
            label_lower = "core.extension"
            fields = [
                Field("id"),
                Field("display_name"),
                Field("location", "location_id"),
                Field("sip_password"),
                Field("created_at"),
            ]

        class FakeExtension:
            _meta = Meta()
            pk = 7
            id = 7
            display_name = "Desk Phone"
            location_id = 3
            sip_password = "raw-secret"
            created_at = "ignored"

            def __str__(self):
                return "1000 - Desk Phone"

        raw_summary = audit_model_summary(FakeExtension(), redact=False)
        self.assertEqual(raw_summary["sip_password"], "raw-secret")
        self.assertNotIn("created_at", raw_summary)

        redacted_summary = audit_model_summary(FakeExtension())
        self.assertEqual(redacted_summary["sip_password"], REDACTED_VALUE)
        self.assertEqual(redacted_summary["location"], 3)

        actor = type("Actor", (), {"get_username": lambda self: "admin-user"})()
        details = build_config_change_details(
            actor=actor,
            operation="update",
            model="core.extension",
            object_identity="7:1000 - Desk Phone",
            outcome="success",
            before={"display_name": "Old", "sip_password": "old-secret"},
            after={"display_name": "New", "sip_password": "new-secret"},
            source="portal_form",
        )

        self.assertEqual(details["actor_username"], "admin-user")
        self.assertEqual(details["result"], "success")
        self.assertEqual(details["before"]["sip_password"], REDACTED_VALUE)
        self.assertEqual(details["after"]["sip_password"], REDACTED_VALUE)
        self.assertEqual(details["changed_fields"], ["display_name", "sip_password"])


class ConfigAuditSourceInspectionTests(unittest.TestCase):
    def test_central_config_change_recorder_uses_existing_audit_log_model(self):
        source = _source("core/audit.py")
        body = _function_source(source, "record_config_change")

        self.assertIn("build_config_change_details", body)
        self.assertIn("AuditAction.CONFIG_CHANGE", body)
        self.assertIn("record_audit(", body)

    def test_reported_view_paths_call_audit_helpers(self):
        source = _source("core/views.py")
        expected_helpers = {
            "provider_create": "_save_config_form_with_audit",
            "provider_update": "_save_config_form_with_audit",
            "location_update": "_save_config_form_with_audit",
            "extension_create": "_record_config_change",
            "extension_update": "_record_config_change",
            "extension_delete": "_record_config_change",
            "trunk_delete": "_delete_config_object_with_audit",
            "phone_delete": "_delete_config_object_with_audit",
            "_routing_delete_response": "_delete_config_object_with_audit",
        }
        for function_name, helper_name in expected_helpers.items():
            with self.subTest(function=function_name):
                self.assertIn(helper_name, _function_source(source, function_name))

    def test_normal_config_create_update_paths_call_audit_helpers(self):
        source = _source("core/views.py")
        expected_helpers = {
            "provider_create": "_save_config_form_with_audit",
            "provider_update": "_save_config_form_with_audit",
            "trunk_create": "_save_config_form_with_audit",
            "trunk_update": "_save_config_form_with_audit",
            "outbound_route_create": "_record_config_change",
            "outbound_route_update": "_record_config_change",
            "location_create": "_save_config_form_with_audit",
            "location_update": "_save_config_form_with_audit",
            "extension_create": "_record_config_change",
            "extension_update": "_record_config_change",
            "phone_create": "_record_config_change",
            "phone_update": "_record_config_change",
            "inbound_destination_create": "_save_config_form_with_audit",
            "inbound_destination_update": "_save_config_form_with_audit",
            "did_create": "_save_config_form_with_audit",
            "did_update": "_save_config_form_with_audit",
            "ivr_create": "_record_config_change",
            "ivr_update": "_record_config_change",
            "ring_group_create": "_save_config_form_with_audit",
            "ring_group_update": "_save_config_form_with_audit",
            "queue_create": "_save_config_form_with_audit",
            "queue_update": "_save_config_form_with_audit",
            "paging_group_create": "_save_config_form_with_audit",
            "paging_group_update": "_save_config_form_with_audit",
            "feature_code_create": "_save_config_form_with_audit",
            "feature_code_update": "_save_config_form_with_audit",
        }
        for function_name, helper_name in expected_helpers.items():
            with self.subTest(function=function_name):
                self.assertIn(helper_name, _function_source(source, function_name))

    def test_csv_import_records_config_change_per_successful_row(self):
        source = _source("core/extension_csv.py")
        body = _function_source(source, "import_extensions_csv")

        self.assertIn("record_config_change(", body)
        self.assertIn('source="csv_import"', body)
        self.assertIn('extra_details={"row": prepared.row_number}', body)
        self.assertIn("audit_model_summary(prepared.extension, redact=False)", body)


def _source(relative_path):
    return (ROOT / relative_path).read_text()


def _function_source(source, function_name):
    tree = ast.parse(source)
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name == function_name:
            return ast.get_source_segment(source, node)
    raise AssertionError(f"Function not found: {function_name}")


if __name__ == "__main__":
    unittest.main()
