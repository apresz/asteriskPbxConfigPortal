from io import BytesIO
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
import zipfile

from .asterisk_config_helpers import (
    REDACTED_VALUE,
    active_route_trunks,
    emergency_trunk_missing_credential_errors,
    iax2_provider_trunk_lines,
    provider_credential_warning,
    redact_sensitive_details,
    route_dial_targets,
)
from .config_archive import (
    ACTIVE_CONFIG_MARKER_CONTENT_TYPE,
    CONFIG_PAYLOAD_CHECKSUM_TYPE,
    active_config_marker_bundle_path,
    active_config_marker_volume_mount,
    build_active_config_marker,
    json_bytes,
    manifest_entry,
    payload_files_checksum,
    sha256sums,
    zip_archive,
)


PROJECT_ROOT = Path(__file__).resolve().parents[1]


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


class ActiveConfigMarkerArchiveTests(unittest.TestCase):
    def test_marker_json_contains_active_version_location_checksum_and_deployment_metadata(self):
        checksum = "a" * 64
        marker = build_active_config_marker(
            location={
                "id": 42,
                "slug": "branch-hq",
                "name": "Branch HQ",
                "timezone": "America/Los_Angeles",
            },
            version_number=7,
            exported_at="2026-06-05T19:00:00+00:00",
            exported_by="operator",
            checksum=checksum,
            marker_path="/etc/asterisk/pbx-active-config.json",
            bundle_path="asterisk/pbx-active-config.json",
            deployment={
                "location_deployment_status": "ready",
                "asterisk_path": "/srv/pbx/current/asterisk",
                "tftp_path": "/srv/pbx/current/tftp",
            },
        )

        with tempfile.TemporaryDirectory() as temp_dir:
            marker_path = Path(temp_dir) / "pbx-active-config.json"
            marker_path.write_text(json.dumps(marker, sort_keys=True), encoding="utf-8")
            loaded_marker = json.loads(marker_path.read_text(encoding="utf-8"))

        self.assertEqual(loaded_marker["format"], "pbx-active-config/v1")
        self.assertEqual(loaded_marker["version"], 7)
        self.assertEqual(loaded_marker["version_number"], 7)
        self.assertEqual(loaded_marker["checksum"], checksum)
        self.assertEqual(loaded_marker["checksum_type"], CONFIG_PAYLOAD_CHECKSUM_TYPE)
        self.assertEqual(loaded_marker["timestamp"], "2026-06-05T19:00:00+00:00")
        self.assertEqual(loaded_marker["exported_at"], "2026-06-05T19:00:00+00:00")
        self.assertEqual(loaded_marker["config_version"]["exported_by"], "operator")
        self.assertEqual(loaded_marker["location"]["slug"], "branch-hq")
        self.assertEqual(loaded_marker["deployment"]["marker_path"], "/etc/asterisk/pbx-active-config.json")
        self.assertEqual(loaded_marker["deployment"]["bundle_path"], "asterisk/pbx-active-config.json")
        self.assertEqual(loaded_marker["deployment"]["asterisk_path"], "/srv/pbx/current/asterisk")

    def test_marker_bundle_path_and_runtime_mount_follow_configured_marker_path(self):
        self.assertEqual(
            active_config_marker_bundle_path("/etc/asterisk/pbx-active-config.json"),
            "asterisk/pbx-active-config.json",
        )
        self.assertEqual(
            active_config_marker_volume_mount(
                "/etc/asterisk/pbx-active-config.json",
                "asterisk/pbx-active-config.json",
            ),
            "./asterisk:/etc/asterisk:ro",
        )
        self.assertEqual(
            active_config_marker_bundle_path("/var/lib/pbx/active.json"),
            "active-config/var/lib/pbx/active.json",
        )
        self.assertEqual(
            active_config_marker_volume_mount(
                "/var/lib/pbx/active.json",
                "active-config/var/lib/pbx/active.json",
            ),
            "./active-config/var/lib/pbx:/var/lib/pbx:ro",
        )

        with self.assertRaises(ValueError):
            active_config_marker_bundle_path("../pbx-active-config.json")

    def test_marker_is_in_zip_manifest_and_checksum_lines(self):
        payload_files = [
            ("asterisk/pjsip.conf", b"[transport]\n", "text/plain"),
            ("tftp/company-directory.xml", b"<directory />\n", "application/xml"),
        ]
        bundle_path = active_config_marker_bundle_path("/etc/asterisk/pbx-active-config.json")
        marker = build_active_config_marker(
            location={"id": 42, "slug": "branch-hq", "name": "Branch HQ", "timezone": "UTC"},
            version_number=3,
            exported_at="2026-06-05T19:00:00+00:00",
            exported_by="operator",
            checksum=payload_files_checksum(payload_files),
            marker_path="/etc/asterisk/pbx-active-config.json",
            bundle_path=bundle_path,
            deployment={"location_deployment_status": "ready"},
        )
        archive_files = [
            *payload_files,
            (bundle_path, json_bytes(marker), ACTIVE_CONFIG_MARKER_CONTENT_TYPE),
        ]
        manifest = {
            "active_config_marker": {
                "path": bundle_path,
                "configured_path": "/etc/asterisk/pbx-active-config.json",
            },
            "files": [manifest_entry(path, content, content_type) for path, content, content_type in archive_files],
        }
        archive_files.append(("manifest.json", json_bytes(manifest), "application/json"))
        archive_files.append(("SHA256SUMS", sha256sums(archive_files), "text/plain"))

        with zipfile.ZipFile(BytesIO(zip_archive(archive_files))) as archive:
            names = set(archive.namelist())
            archived_manifest = json.loads(archive.read("manifest.json").decode("utf-8"))
            checksums = archive.read("SHA256SUMS").decode("utf-8")

        self.assertIn("asterisk/pbx-active-config.json", names)
        self.assertIn(
            {
                "path": "asterisk/pbx-active-config.json",
                "size": len(json_bytes(marker)),
                "sha256": manifest_entry(bundle_path, json_bytes(marker), ACTIVE_CONFIG_MARKER_CONTENT_TYPE)["sha256"],
                "content_type": ACTIVE_CONFIG_MARKER_CONTENT_TYPE,
            },
            archived_manifest["files"],
        )
        self.assertIn("  asterisk/pbx-active-config.json\n", checksums)

    def test_export_archive_golden_fixtures_include_active_marker(self):
        fixture_dir = PROJECT_ROOT / "core" / "testdata" / "export_archive"
        manifest = json.loads((fixture_dir / "manifest.json").read_text(encoding="utf-8"))
        manifest_paths = {file["path"] for file in manifest["files"]}

        self.assertEqual(manifest["active_config_marker"]["path"], "asterisk/pbx-active-config.json")
        self.assertEqual(
            manifest["active_config_marker"]["configured_path"],
            "/etc/asterisk/pbx-active-config.json",
        )
        self.assertIn("asterisk/pbx-active-config.json", manifest_paths)
        self.assertIn("  asterisk/pbx-active-config.json", (fixture_dir / "SHA256SUMS").read_text(encoding="utf-8"))
        self.assertIn("asterisk/pbx-active-config.json|", (fixture_dir / "zip-layout.txt").read_text(encoding="utf-8"))
        self.assertIn(
            "asterisk/pbx-active-config.json|",
            (fixture_dir / "staging-layout.txt").read_text(encoding="utf-8"),
        )

    def test_deployment_source_extracts_uploads_and_installs_configured_marker(self):
        source = (PROJECT_ROOT / "core" / "deployments.py").read_text(encoding="utf-8")

        self.assertIn("deployment_bundle_active_marker(version)", source)
        self.assertIn('"active-config"', source)
        self.assertIn("active_config_marker=active_marker", source)
        self.assertIn("Export archive manifest references missing active marker", source)
        self.assertIn("cp {_q(source_path)} {_q(target_path)}", source)


if __name__ == "__main__":
    unittest.main()
