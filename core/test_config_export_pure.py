from types import SimpleNamespace
import unittest

from .asterisk_config_helpers import (
    REDACTED_VALUE,
    active_route_trunks,
    emergency_trunk_missing_credential_errors,
    iax2_provider_trunk_lines,
    provider_credential_warning,
    redact_sensitive_details,
    route_dial_targets,
)


def fake_trunk(
    name,
    trunk_type,
    *,
    host="provider.example.test",
    username="provider-user",
    password="provider-secret",
    provider_name="Example Provider",
    emergency_capable=False,
    active=True,
):
    return SimpleNamespace(
        name=name,
        trunk_type=trunk_type,
        host=host,
        username=username,
        password=password,
        provider=SimpleNamespace(name=provider_name),
        is_emergency_capable=emergency_capable,
        is_active=active,
    )


def fake_route_trunk(trunk, priority):
    return SimpleNamespace(trunk=trunk, priority=priority)


class Iax2ProviderConfigTests(unittest.TestCase):
    def test_provider_iax2_trunk_section_renders_plaintext_secret_for_asterisk(self):
        trunk = fake_trunk(
            "Backup IAX",
            "iax2",
            host="iax.provider.example.test",
            username="iax-user",
            password="iax-secret",
        )

        self.assertEqual(
            "\n".join(iax2_provider_trunk_lines(trunk)),
            "\n".join(
                [
                    "[trunk-backup-iax]",
                    "type=friend",
                    "host=iax.provider.example.test",
                    "username=iax-user",
                    "secret=iax-secret",
                    "context=inbound",
                    "trunk=yes",
                    "qualify=yes",
                    "",
                ]
            ),
        )

    def test_route_dial_targets_preserve_priority_fallback_for_sip_and_iax2(self):
        primary_sip = fake_trunk("Primary SIP", "sip")
        backup_iax = fake_trunk("Backup IAX", "iax2")
        disabled = fake_trunk("Disabled SIP", "sip", active=False)
        route_trunks = [
            fake_route_trunk(primary_sip, 2),
            fake_route_trunk(backup_iax, 1),
            fake_route_trunk(disabled, 3),
        ]

        self.assertEqual(
            route_dial_targets(route_trunks),
            [
                "IAX2/trunk-backup-iax/${EXTEN}",
                "PJSIP/${EXTEN}@trunk-primary-sip",
            ],
        )
        self.assertEqual([link.trunk.name for link in active_route_trunks(route_trunks)], ["Backup IAX", "Primary SIP"])

    def test_missing_iax2_provider_credentials_warn_and_error_without_django(self):
        trunk = fake_trunk(
            "Emergency IAX",
            "iax2",
            host="",
            username="",
            password="",
            emergency_capable=True,
        )
        warning = provider_credential_warning(trunk)

        self.assertEqual(warning["code"], "provider_trunk_missing_credentials")
        self.assertEqual(warning["trunk_type"], "iax2")
        self.assertEqual(warning["missing"], ["host", "username", "password"])
        self.assertTrue(warning["emergency_capable"])
        self.assertEqual(
            emergency_trunk_missing_credential_errors("Emergency", [trunk], {"Emergency IAX": warning}),
            [
                {
                    "code": "emergency_trunk_missing_credentials",
                    "route": "Emergency",
                    "trunk": "Emergency IAX",
                    "missing": ["host", "username", "password"],
                    "message": "Emergency-capable trunks need complete provider credentials.",
                }
            ],
        )

    def test_sensitive_provider_details_are_redacted_for_audit_payloads(self):
        details = {
            "trunk": "Backup IAX",
            "credentials": {
                "username": "iax-user",
                "password": "iax-secret",
            },
            "agent_secret": "agent-secret",
            "warnings": [{"missing": ["password"]}],
        }

        self.assertEqual(
            redact_sensitive_details(details),
            {
                "trunk": "Backup IAX",
                "credentials": REDACTED_VALUE,
                "agent_secret": REDACTED_VALUE,
                "warnings": [{"missing": ["password"]}],
            },
        )


if __name__ == "__main__":
    unittest.main()
