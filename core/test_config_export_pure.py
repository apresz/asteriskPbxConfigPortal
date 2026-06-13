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
    emergency_enabled_extensions,
    emergency_route_validation_issues,
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
from .runtime_images import (
    ASTERISK_20_CISCO_IMAGE,
    RUNTIME_IMAGE_TAG_POLICY_BLOCK,
    RUNTIME_IMAGE_TAG_POLICY_WARN,
    RuntimeImage,
    configured_runtime_images,
    parse_image_reference,
    runtime_image_metadata,
    runtime_image_validation_issues,
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


class FakeRelatedManager:
    def __init__(self, items):
        self.items = list(items)

    def all(self):
        return list(self.items)


def fake_extension(number, *, active=True, emergency_calling_enabled=True):
    return SimpleNamespace(number=number, is_active=active, emergency_calling_enabled=emergency_calling_enabled)


def fake_emergency_route(
    name="Emergency",
    *,
    caller_id_source="emergency",
    trunks=(),
):
    return SimpleNamespace(
        name=name,
        caller_id_source=caller_id_source,
        route_trunks=FakeRelatedManager([fake_route_trunk(trunk, index + 1) for index, trunk in enumerate(trunks)]),
    )


class RuntimeImageReferenceTests(unittest.TestCase):
    def test_parse_image_reference_splits_registry_tag_and_digest(self):
        digest = "sha256:" + "a" * 64
        reference = f"{ASTERISK_20_CISCO_IMAGE}@{digest}"

        parsed = parse_image_reference(reference)

        self.assertIsNone(parsed.registry)
        self.assertEqual(parsed.repository, "pbx-asterisk")
        self.assertEqual(parsed.tag, "20.19.0-cisco")
        self.assertEqual(parsed.digest, digest)
        self.assertTrue(parsed.digest_pinned)
        self.assertFalse(parsed.tag_only)

    def test_parse_image_reference_handles_registry_port_without_tag(self):
        digest = "sha256:" + "b" * 64

        parsed = parse_image_reference(f"localhost:5000/pbx/asterisk@{digest}")

        self.assertEqual(parsed.registry, "localhost:5000")
        self.assertEqual(parsed.repository, "localhost:5000/pbx/asterisk")
        self.assertIsNone(parsed.tag)
        self.assertEqual(parsed.digest, digest)

    def test_invalid_digest_is_rejected(self):
        with self.assertRaises(ValueError):
            parse_image_reference(f"{ASTERISK_20_CISCO_IMAGE}@sha256:not-a-digest")


class RuntimeImageValidationTests(unittest.TestCase):
    def test_custom_tag_only_images_warn_or_block_by_policy(self):
        image = RuntimeImage(
            service="asterisk",
            env_var="PBX_ASTERISK_IMAGE",
            reference=ASTERISK_20_CISCO_IMAGE,
            custom=True,
        )

        warning_issues = runtime_image_validation_issues([image], tag_policy=RUNTIME_IMAGE_TAG_POLICY_WARN)
        blocking_issues = runtime_image_validation_issues([image], tag_policy=RUNTIME_IMAGE_TAG_POLICY_BLOCK)

        self.assertEqual(warning_issues["errors"], [])
        self.assertEqual(warning_issues["warnings"][0]["code"], "runtime_image_tag_only")
        self.assertEqual(blocking_issues["warnings"], [])
        self.assertEqual(blocking_issues["errors"][0]["code"], "runtime_image_tag_only")

    def test_non_custom_tag_only_image_is_allowed(self):
        image = RuntimeImage(
            service="provisioning-http",
            env_var="PBX_HTTP_IMAGE",
            reference="docker.io/nginx:1.27-alpine",
            custom=False,
        )

        self.assertEqual(
            runtime_image_validation_issues([image], tag_policy=RUNTIME_IMAGE_TAG_POLICY_BLOCK),
            {"warnings": [], "errors": []},
        )

    def test_resolved_digest_metadata_makes_custom_image_immutable(self):
        digest = "sha256:" + "c" * 64
        images = configured_runtime_images(
            {
                "asterisk": {
                    "reference": ASTERISK_20_CISCO_IMAGE,
                    "resolved_digest": digest,
                    "digest_source": "release-lock",
                }
            }
        )
        asterisk = next(image for image in images if image.service == "asterisk")

        self.assertEqual(asterisk.compose_default, f"{ASTERISK_20_CISCO_IMAGE}@{digest}")
        self.assertEqual(
            runtime_image_validation_issues([asterisk], tag_policy=RUNTIME_IMAGE_TAG_POLICY_BLOCK),
            {"warnings": [], "errors": []},
        )
        self.assertIn(
            {
                "service": "asterisk",
                "env_var": "PBX_ASTERISK_IMAGE",
                "reference": ASTERISK_20_CISCO_IMAGE,
                "compose_reference": f"{ASTERISK_20_CISCO_IMAGE}@{digest}",
                "repository": "pbx-asterisk",
                "registry": None,
                "tag": "20.19.0-cisco",
                "digest": digest,
                "digest_source": "release-lock",
                "custom": True,
                "immutable": True,
                "requires_env_override": False,
                "built_from_source": True,
            },
            runtime_image_metadata([asterisk]),
        )


class RuntimeBundleImageGoldenTests(unittest.TestCase):
    def test_compose_golden_requires_digest_for_custom_runtime_images(self):
        fixture_dir = PROJECT_ROOT / "core" / "testdata" / "runtime_bundle"
        compose = (fixture_dir / "docker-compose.yml").read_text(encoding="utf-8")
        env_example = (fixture_dir / ".env.example").read_text(encoding="utf-8")

        self.assertIn("build:\n      context: ./runtime/asterisk", compose)
        self.assertIn("image: ${PBX_ASTERISK_IMAGE:-pbx-asterisk:20.19.0-cisco}", compose)
        self.assertIn(
            "image: ${PBX_TFTP_IMAGE:?PBX_TFTP_IMAGE must include an immutable digest}",
            compose,
        )
        self.assertIn(
            "image: ${PBX_AGENT_IMAGE:?PBX_AGENT_IMAGE must include an immutable digest}",
            compose,
        )
        self.assertIn("image: ${PBX_HTTP_IMAGE:-docker.io/nginx:1.27-alpine}", compose)
        self.assertNotIn("22-lts", compose)
        self.assertIn("ASTERISK_VERSION=20.19.0", env_example)
        self.assertIn(
            "CISCO_PATCH_SHA256=adcb88c17a1cf8eb6242c7d9f1959f06afc64ae9af210268abaf767d388b83d2",
            env_example,
        )
        self.assertIn("PBX_ASTERISK_IMAGE=pbx-asterisk:20.19.0-cisco\n", env_example)
        self.assertIn("PBX_TFTP_IMAGE=\n", env_example)
        self.assertIn("PBX_AGENT_IMAGE=\n", env_example)

    def test_runtime_image_manifest_golden_exposes_digest_fields(self):
        manifest = json.loads(
            (PROJECT_ROOT / "core" / "testdata" / "runtime_bundle" / "runtime-images.json").read_text(
                encoding="utf-8"
            )
        )
        images = {image["service"]: image for image in manifest["images"]}

        self.assertEqual(manifest["format"], "pbx-runtime-images/v1")
        self.assertEqual(manifest["tag_policy"], "warn")
        self.assertEqual(set(images), {"asterisk", "tftp", "provisioning-http", "pbx-agent"})
        self.assertTrue(images["asterisk"]["custom"])
        self.assertFalse(images["asterisk"]["immutable"])
        self.assertTrue(images["asterisk"]["built_from_source"])
        self.assertFalse(images["asterisk"]["requires_env_override"])
        self.assertIn("digest", images["asterisk"])
        self.assertIsNone(images["asterisk"]["digest"])
        self.assertEqual(images["asterisk"]["reference"], ASTERISK_20_CISCO_IMAGE)
        self.assertEqual(
            manifest["source_builds"]["asterisk"]["cisco_patch_sha256"],
            "adcb88c17a1cf8eb6242c7d9f1959f06afc64ae9af210268abaf767d388b83d2",
        )
        self.assertEqual(images["provisioning-http"]["compose_reference"], "docker.io/nginx:1.27-alpine")


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


class EmergencyValidationPolicyTests(unittest.TestCase):
    def test_missing_emergency_route_is_a_hard_block_for_911_enabled_extensions(self):
        issues = emergency_route_validation_issues(
            require_emergency=True,
            emergency_allowed_extensions=[fake_extension("3000")],
            emergency_routes=[],
            emergency_caller_id="+15551201000",
            warning_trunks={},
        )

        self.assertEqual([error["code"] for error in issues["errors"]], ["missing_emergency_route"])
        self.assertEqual(issues["errors"][0]["affected_extensions"], ["3000"])
        self.assertEqual(issues["warnings"], [])

    def test_missing_emergency_caller_id_is_a_hard_block_for_911_enabled_extensions(self):
        trunk = fake_trunk("Emergency SIP", "sip", emergency_capable=True)
        route = fake_emergency_route(trunks=[trunk])
        issues = emergency_route_validation_issues(
            require_emergency=True,
            emergency_allowed_extensions=[fake_extension("3000")],
            emergency_routes=[route],
            emergency_caller_id="",
            warning_trunks={},
        )

        self.assertEqual([error["code"] for error in issues["errors"]], ["missing_emergency_caller_id"])
        self.assertEqual(issues["errors"][0]["affected_extensions"], ["3000"])
        self.assertEqual(issues["warnings"], [])

    def test_route_credential_and_capability_issues_are_warnings_when_route_and_caller_id_exist(self):
        non_capable_trunk = fake_trunk("Standard SIP", "sip", emergency_capable=False)
        incomplete_emergency_trunk = fake_trunk(
            "Emergency SIP",
            "sip",
            password="",
            emergency_capable=True,
        )
        warning = provider_credential_warning(incomplete_emergency_trunk)
        issues = emergency_route_validation_issues(
            require_emergency=True,
            emergency_allowed_extensions=[fake_extension("3000")],
            emergency_routes=[
                fake_emergency_route("Bad Caller ID", caller_id_source="location_default", trunks=[non_capable_trunk]),
                fake_emergency_route("Missing Credentials", trunks=[incomplete_emergency_trunk]),
            ],
            emergency_caller_id="+15551201000",
            warning_trunks={warning["trunk"]: warning},
        )

        self.assertEqual(issues["errors"], [])
        self.assertEqual(
            {warning["code"] for warning in issues["warnings"]},
            {
                "emergency_route_caller_id_source",
                "missing_emergency_capable_trunk",
                "emergency_trunk_missing_credentials",
            },
        )

    def test_911_disabled_extensions_are_excluded_from_required_hard_blocks(self):
        allowed_extensions = emergency_enabled_extensions(
            [
                fake_extension("3000", emergency_calling_enabled=False),
                fake_extension("3001", active=False, emergency_calling_enabled=True),
            ]
        )
        issues = emergency_route_validation_issues(
            require_emergency=True,
            emergency_allowed_extensions=allowed_extensions,
            emergency_routes=[],
            emergency_caller_id="",
            warning_trunks={},
        )

        self.assertEqual(allowed_extensions, [])
        self.assertEqual(issues, {"warnings": [], "errors": []})

    def test_django_export_adapter_routes_emergency_policy_warnings_without_importing_django(self):
        source = (PROJECT_ROOT / "core" / "config_export.py").read_text(encoding="utf-8")

        self.assertIn("emergency_issues = emergency_route_validation_issues(", source)
        self.assertIn('warnings.extend(emergency_issues["warnings"])', source)
        self.assertIn('errors.extend(emergency_issues["errors"])', source)
        self.assertNotIn("errors.extend(emergency_trunk_missing_credential_errors", source)

    def test_dial_plan_validation_template_labels_blocking_errors_and_warnings(self):
        source = (PROJECT_ROOT / "templates" / "core" / "partials" / "dial_plan" / "list_content.html").read_text(
            encoding="utf-8"
        )

        self.assertIn("dial_plan_validation_has_errors", source)
        self.assertIn("Export-blocking errors", source)
        self.assertIn("Warnings", source)


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
        self.assertEqual(manifest["runtime_images"]["path"], "runtime-images.json")
        self.assertEqual(manifest["runtime_images"]["tag_policy"], "warn")
        self.assertEqual(
            manifest["runtime_images"]["source_builds"]["asterisk"]["asterisk_version"],
            "20.19.0",
        )
        self.assertEqual(
            manifest["runtime_images"]["source_builds"]["asterisk"]["cisco_patch_sha256"],
            "adcb88c17a1cf8eb6242c7d9f1959f06afc64ae9af210268abaf767d388b83d2",
        )
        self.assertIn("runtime-images.json", manifest_paths)
        self.assertIn("runtime/asterisk/Dockerfile", manifest_paths)
        self.assertIn("runtime/asterisk/docker-entrypoint.sh", manifest_paths)
        self.assertIn("runtime_image_tag_only", manifest["emergency_status"]["warning_codes"])
        self.assertEqual(manifest["deployment"]["runtime_images"]["tag_policy"], "warn")
        self.assertIn("  asterisk/pbx-active-config.json", (fixture_dir / "SHA256SUMS").read_text(encoding="utf-8"))
        self.assertIn("  runtime-images.json", (fixture_dir / "SHA256SUMS").read_text(encoding="utf-8"))
        self.assertIn("  runtime/asterisk/Dockerfile", (fixture_dir / "SHA256SUMS").read_text(encoding="utf-8"))
        self.assertIn("asterisk/pbx-active-config.json|", (fixture_dir / "zip-layout.txt").read_text(encoding="utf-8"))
        self.assertIn("runtime-images.json|", (fixture_dir / "zip-layout.txt").read_text(encoding="utf-8"))
        self.assertIn("runtime/asterisk/Dockerfile|", (fixture_dir / "zip-layout.txt").read_text(encoding="utf-8"))
        self.assertIn(
            "asterisk/pbx-active-config.json|",
            (fixture_dir / "staging-layout.txt").read_text(encoding="utf-8"),
        )
        self.assertIn(
            "runtime-images.json|",
            (fixture_dir / "staging-layout.txt").read_text(encoding="utf-8"),
        )
        self.assertIn(
            "runtime/asterisk/Dockerfile|",
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
