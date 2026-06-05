from types import SimpleNamespace
import unittest

from .audit_helpers import REDACTED_VALUE
from .live_operations import (
    UnsupportedLiveCommandError,
    ami_action_for_live_command,
    build_live_operation_audit_details,
    canonical_live_command_name,
    supported_live_commands,
    validate_live_command_parameters,
)


class LiveCommandAllowListTests(unittest.TestCase):
    def test_supported_live_commands_default_to_unparameterized_ui_controls(self):
        self.assertEqual(
            [command["name"] for command in supported_live_commands()],
            ["core_reload", "pjsip_reload", "queue_reload"],
        )

    def test_supported_live_commands_can_include_parameterized_channel_actions(self):
        self.assertEqual(
            [command["name"] for command in supported_live_commands(include_parameterized=True)],
            [
                "core_reload",
                "pjsip_reload",
                "queue_reload",
                "channel_status",
                "channel_hangup",
                "channel_redirect",
                "channel_originate",
            ],
        )
        self.assertEqual(canonical_live_command_name("hangup"), "channel_hangup")
        self.assertEqual(canonical_live_command_name("redirect"), "channel_redirect")
        with self.assertRaisesRegex(UnsupportedLiveCommandError, "Unsupported live PBX command"):
            canonical_live_command_name("raw_ami_command")


class LiveCommandParameterValidationTests(unittest.TestCase):
    def test_reload_commands_reject_parameters(self):
        self.assertEqual(validate_live_command_parameters("core_reload", {}), {})
        with self.assertRaisesRegex(UnsupportedLiveCommandError, "does not accept parameters"):
            validate_live_command_parameters("core_reload", {"channel_id": "PJSIP/1000-0000001a"})

    def test_channel_redirect_normalizes_aliases_and_defaults_priority(self):
        self.assertEqual(
            validate_live_command_parameters(
                "redirect",
                {
                    "channel": "PJSIP/1000-0000001a",
                    "context": "from-internal",
                    "extension": "2000",
                },
            ),
            {
                "channel_id": "PJSIP/1000-0000001a",
                "context": "from-internal",
                "exten": "2000",
                "priority": 1,
            },
        )

    def test_channel_parameter_validation_rejects_bad_shapes_and_values(self):
        invalid_cases = [
            ("channel_hangup", "PJSIP/1000-0000001a", "parameters must be a JSON object"),
            ("channel_hangup", {"channel_id": 1000}, "parameter channel_id is invalid"),
            ("channel_hangup", {"channel_id": "PJSIP/1000 bad"}, "parameter channel_id is invalid"),
            ("channel_hangup", {"channel_id": "PJSIP/1000-0000001a", "raw": "Command"}, "raw"),
            (
                "channel_redirect",
                {"channel_id": "PJSIP/1000-0000001a", "context": "from internal", "exten": "2000"},
                "parameter context is invalid",
            ),
            (
                "channel_redirect",
                {"channel_id": "PJSIP/1000-0000001a", "context": "from-internal", "exten": "2000", "priority": 0},
                "between 1 and 999999",
            ),
            (
                "channel_originate",
                {"channel_id": "Local/2000@from-internal", "context": "from-internal", "exten": "2000", "async": "yes"},
                "parameter async must be a boolean",
            ),
        ]
        for command_name, parameters, expected_error in invalid_cases:
            with self.subTest(command=command_name, parameters=parameters):
                with self.assertRaisesRegex(UnsupportedLiveCommandError, expected_error):
                    validate_live_command_parameters(command_name, parameters)


class LiveCommandAMIMappingTests(unittest.TestCase):
    def test_channel_actions_map_to_expected_ami_payloads(self):
        cases = [
            (
                "status",
                {"channel_id": "PJSIP/1000-0000001a"},
                "channel_status",
                "Status",
                {"Channel": "PJSIP/1000-0000001a"},
                "StatusComplete",
            ),
            (
                "hangup",
                {"channel": "PJSIP/1000-0000001a", "cause": "16"},
                "channel_hangup",
                "Hangup",
                {"Channel": "PJSIP/1000-0000001a", "Cause": "16"},
                None,
            ),
            (
                "redirect",
                {"channel_id": "PJSIP/1000-0000001a", "context": "from-internal", "exten": "2000", "priority": 2},
                "channel_redirect",
                "Redirect",
                {
                    "Channel": "PJSIP/1000-0000001a",
                    "Context": "from-internal",
                    "Exten": "2000",
                    "Priority": "2",
                },
                None,
            ),
            (
                "originate",
                {
                    "channel_id": "Local/2000@from-internal",
                    "context": "from-internal",
                    "exten": "3000",
                    "priority": "1",
                    "caller_id": "Help Desk <2000>",
                    "timeout_ms": 30000,
                    "async": False,
                },
                "channel_originate",
                "Originate",
                {
                    "Channel": "Local/2000@from-internal",
                    "Context": "from-internal",
                    "Exten": "3000",
                    "Priority": "1",
                    "Async": "false",
                    "CallerID": "Help Desk <2000>",
                    "Timeout": "30000",
                },
                None,
            ),
        ]
        for command_name, parameters, canonical, ami_action, ami_parameters, complete_event in cases:
            with self.subTest(command=command_name):
                action = ami_action_for_live_command(command_name, parameters)
                self.assertEqual(action.command_name, canonical)
                self.assertEqual(action.ami_action, ami_action)
                self.assertEqual(action.ami_parameters, ami_parameters)
                self.assertEqual(action.complete_event, complete_event)

    def test_originate_defaults_async_true_for_agent_dispatch(self):
        action = ami_action_for_live_command(
            "channel_originate",
            {"channel_id": "Local/2000@from-internal", "context": "from-internal", "exten": "3000"},
        )

        self.assertEqual(action.ami_parameters["Async"], "true")
        self.assertEqual(action.ami_parameters["Priority"], "1")


class LiveOperationAuditPayloadTests(unittest.TestCase):
    def test_accepted_channel_control_audit_includes_api_key_user_location_and_redacted_parameters(self):
        actor = _actor("api-operator")
        location = SimpleNamespace(id=7, slug="hq")
        api_key = SimpleNamespace(
            id=12,
            name="operator automation",
            prefix="pbx_live",
            scope_type="user",
            user_id=42,
            service_identity_id=None,
        )

        details = build_live_operation_audit_details(
            actor=actor,
            location=location,
            command_name="channel_hangup",
            parameters={
                "channel_id": "PJSIP/1000-0000001a",
                "cause": 16,
                "nested": {"api_key": "raw-secret"},
            },
            result={"status": "success"},
            api_key=api_key,
        )

        self.assertEqual(details["actor_username"], "api-operator")
        self.assertEqual(details["api_key_id"], 12)
        self.assertEqual(details["api_key_prefix"], "pbx_live")
        self.assertEqual(details["api_key_scope_type"], "user")
        self.assertEqual(details["api_key_scope_id"], 42)
        self.assertEqual(details["location_id"], 7)
        self.assertEqual(details["location_slug"], "hq")
        self.assertEqual(details["parameters"]["channel_id"], "PJSIP/1000-0000001a")
        self.assertEqual(details["parameters"]["nested"]["api_key"], REDACTED_VALUE)
        self.assertEqual(details["result"], {"status": "success"})

    def test_rejected_channel_control_audit_redacts_raw_submitted_parameters(self):
        details = build_live_operation_audit_details(
            actor=_actor("api-operator"),
            location=SimpleNamespace(id=7, slug="hq"),
            command_name="channel_redirect",
            parameters={
                "channel_id": "PJSIP/1000-0000001a",
                "context": "from internal",
                "exten": "2000",
                "api_key": "raw-secret",
                "password": "raw-password",
            },
            result={"status": "failure", "error": "parameter context is invalid"},
        )

        self.assertEqual(details["parameters"]["channel_id"], "PJSIP/1000-0000001a")
        self.assertEqual(details["parameters"]["api_key"], REDACTED_VALUE)
        self.assertEqual(details["parameters"]["password"], REDACTED_VALUE)
        self.assertEqual(details["result"]["status"], "failure")

    def test_rejected_non_mapping_parameters_are_sanitized_without_raw_value(self):
        details = build_live_operation_audit_details(
            actor=_actor("api-operator"),
            location=SimpleNamespace(id=7, slug="hq"),
            command_name="channel_hangup",
            parameters="raw-secret-token",
            result={"status": "failure"},
        )

        self.assertEqual(details["parameters"], {"_invalid_type": "str"})


def _actor(username):
    return SimpleNamespace(
        is_authenticated=True,
        get_username=lambda: username,
    )


if __name__ == "__main__":
    unittest.main()
