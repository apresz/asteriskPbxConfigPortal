import base64
import csv
import asyncio
from datetime import datetime, timedelta, timezone as datetime_timezone
import hashlib
from io import BytesIO, StringIO
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest import mock
import zipfile

try:
    from django.contrib.auth import get_user_model
    from django.core.files.base import ContentFile
    from django.core.exceptions import ValidationError
    from django.core.files.uploadedfile import SimpleUploadedFile
    from django.core.management import call_command
    from django.core.management.base import CommandError
    from django.db import IntegrityError, transaction
    from django.test import Client, SimpleTestCase, TestCase, TransactionTestCase, override_settings
    from django.urls import reverse
    from django.utils import timezone
except ModuleNotFoundError as exc:
    if exc.name == "django":
        raise unittest.SkipTest("Django is not installed in this pure-Python validation environment.") from exc
    raise

from .access import (
    assign_role,
    get_user_role,
    role_has_permission,
    user_has_permission,
)
from .audit import record_audit
from .audio_prompts import AudioPromptConversionError, create_audio_prompt_from_upload
from .agent_client import (
    AgentConfig,
    execute_live_ami_command,
    handle_agent_control_message,
    portal_url_to_websocket_url,
    read_active_config_marker,
    report_active_config_once,
    report_telemetry_once,
    run_telemetry_loop,
)
from .agent_ws import update_active_config_report, update_agent_telemetry_report
from .ami_telemetry import (
    parse_active_calls,
    parse_ami_messages,
    parse_cdr_csv,
    parse_cel_csv,
    parse_location_health,
    parse_phone_registrations,
    parse_queue_status,
    parse_trunk_status,
    recording_id_for_path,
    scan_recording_metadata,
)
from .config_export import (
    ASTERISK_CONFIG_FILENAMES,
    build_asterisk_config_files,
    build_config_export_archive,
    create_config_version,
    build_location_config,
    build_route_generation_choices,
    mac_to_sep_filename,
    select_route_caller_id,
    validate_location_routing,
    write_config_version_directory,
)
from .deployments import (
    SSHDeploymentRunner,
    DeploymentCommandResult,
    DeploymentError,
    deploy_config_version,
    extract_deployment_bundle,
)
from .default_feature_codes import default_feature_code_specs
from .extension_csv import ExtensionCSVError, export_extensions_csv, extension_template_csv, import_extensions_csv
from .extension_management import sync_extension_relationships
from .forms import ExtensionForm, IVRForm, LocationForm, PhoneForm
from .phone_csv import (
    did_template_csv,
    export_phones_csv,
    export_speed_dials_csv,
    phone_template_csv,
    speed_dial_template_csv,
)
from .models import (
    APIKey,
    AdminBackup,
    AudioPrompt,
    AuditAction,
    AuditLog,
    AuditOutcome,
    DID,
    DeploymentRecord,
    FeatureCode,
    IVR,
    IVRMenuOption,
    CallQueue,
    ConfigVersion,
    Extension,
    InboundDestination,
    Location,
    OutboundRoute,
    OutboundRouteTrunk,
    PagingGroup,
    PagingGroupMember,
    Phone,
    PhoneLineAppearance,
    PhoneSpeedDial,
    PortalPermission,
    PortalRole,
    Provider,
    QueueMember,
    RingGroup,
    RingGroupMember,
    ServiceIdentity,
    Trunk,
)


User = get_user_model()


class FrontendAssetTests(SimpleTestCase):
    def test_base_template_uses_local_htmx_asset(self):
        template = (Path(__file__).resolve().parent.parent / "templates" / "base.html").read_text(encoding="utf-8")
        asset_path = Path(__file__).resolve().parent.parent / "static" / "js" / "htmx.min.js"

        self.assertIn("{% static 'js/htmx.min.js' %}", template)
        self.assertNotIn("unpkg.com/htmx", template)
        self.assertTrue(asset_path.exists())

    def test_base_template_uses_local_portal_navigation_asset(self):
        template = (Path(__file__).resolve().parent.parent / "templates" / "base.html").read_text(encoding="utf-8")
        asset_path = Path(__file__).resolve().parent.parent / "static" / "js" / "portal.js"
        script = asset_path.read_text(encoding="utf-8")

        self.assertIn("{% static 'js/portal.js' %}", template)
        self.assertIn("htmx:afterSwap", script)
        self.assertIn("ant-menu-item-selected", script)
        self.assertTrue(asset_path.exists())

    def test_routing_formset_template_exposes_dynamic_add_controls(self):
        template = (
            Path(__file__).resolve().parent.parent
            / "templates"
            / "core"
            / "partials"
            / "routing"
            / "form_content.html"
        ).read_text(encoding="utf-8")
        script = (Path(__file__).resolve().parent.parent / "static" / "js" / "portal.js").read_text(encoding="utf-8")

        self.assertIn('data-formset-prefix="{{ formset.prefix }}"', template)
        self.assertIn("data-formset-template", template)
        self.assertIn("data-formset-add", template)
        self.assertIn("data-formset-delete", template)
        self.assertIn("formset-delete-input", template)
        self.assertIn("field.as_hidden", template)
        self.assertIn("formset.empty_form", template)
        self.assertIn("initDynamicFormsets", script)
        self.assertIn("showFormsetDeleteConfirmation", script)
        self.assertIn("removeFormsetRow", script)
        self.assertIn('deleteInput.value = "on"', script)
        self.assertIn("TOTAL_FORMS", script)
        self.assertIn("__prefix__", script)


def location_form_data(**overrides):
    data = {
        "name": "Branch Office",
        "slug": "branch-office",
        "description": "Branch PBX",
        "timezone": "America/Los_Angeles",
        "lan_subnet": "10.30.0.0/24",
        "pbx_lan_ip": "10.30.0.10",
        "pbx_warp_ip": "100.64.30.10",
        "deployment_ssh_host": "pbx-branch.example.test",
        "deployment_ssh_port": "22",
        "deployment_ssh_username": "deploy",
        "deployment_ssh_private_key": "branch-private-key",
        "deployment_ssh_known_hosts": "pbx-branch.example.test ssh-ed25519 fixture",
        "deployment_staging_path": "/srv/pbx/staging",
        "deployment_asterisk_path": "/srv/pbx/asterisk",
        "deployment_tftp_path": "/srv/pbx/tftp",
        "deployment_reload_command": "asterisk -rx 'core reload'",
        "sip_bind_ip": "10.30.0.10",
        "sip_port": "5060",
        "rtp_port_start": "10000",
        "rtp_port_end": "20000",
        "iax_bind_ip": "10.30.0.10",
        "iax_port": "4569",
        "default_did": "+15551203000",
        "emergency_caller_id": "+15551203999",
        "emergency_trunk": "Branch Emergency SIP",
        "recording_retention_days": "90",
        "smtp_host": "smtp-branch.example.test",
        "smtp_port": "587",
        "smtp_from_email": "pbx-branch@example.test",
        "smtp_use_tls": "on",
        "smtp_username": "pbx-branch",
        "smtp_password": "smtp-secret",
        "ami_host": "127.0.0.1",
        "ami_port": "5038",
        "ami_username": "ami-branch",
        "ami_secret": "ami-secret",
        "agent_secret": "agent-secret",
        "is_active": "on",
        "deployment_status": Location.DeploymentStatus.READY,
    }
    data.update(overrides)
    return data


def location_model_data(**overrides):
    data = location_form_data()
    for field_name in (
        "deployment_ssh_port",
        "sip_port",
        "rtp_port_start",
        "rtp_port_end",
        "iax_port",
        "recording_retention_days",
        "smtp_port",
        "ami_port",
    ):
        data[field_name] = int(data[field_name])
    data["smtp_use_tls"] = True
    data["smtp_use_ssl"] = False
    data["is_active"] = True
    data.update(overrides)
    return data


def dashboard_config_version(location, version_number, **overrides):
    checksum = hashlib.sha256(f"{location.slug}-{version_number}".encode("utf-8")).hexdigest()
    data = {
        "location": location,
        "version_number": version_number,
        "checksum": checksum,
        "warnings": [],
        "emergency_status": {},
        "file_manifest": [{"path": "pjsip.conf", "sha256": checksum}],
        "deployment_snapshot": {"location_deployment_status": location.deployment_status},
        "archive": b"dashboard-test",
        "archive_size_bytes": len(b"dashboard-test"),
    }
    data.update(overrides)
    return ConfigVersion.objects.create(**data)


def extension_form_data(location, **overrides):
    data = {
        "location": str(location.id),
        "number": "3000",
        "display_name": "Branch Desk",
        "email": "desk@example.test",
        "sip_username": "3000",
        "sip_password": "sip-secret",
        "direct_dids": [],
        "voicemail_enabled": "on",
        "voicemail_pin": "1234",
        "caller_id_name": "Branch Desk",
        "caller_id_number": "+15551203000",
        "recording_policy": Extension.RecordingPolicy.NEVER,
        "emergency_calling_enabled": "on",
        "is_active": "on",
        "ring_groups": [],
        "queues": [],
        "paging_groups": [],
    }
    data.update(overrides)
    return data


def phone_form_data(location, **overrides):
    data = {
        "location": str(location.id),
        "mac_address": "SEP001122334455",
        "model": Phone.PhoneModel.CISCO_9971,
        "label": "Reception Phone",
        "is_active": "on",
    }
    data.update(overrides)
    return data


def phone_inline_formset_data(*, line_rows=None, speed_dial_rows=None):
    line_rows = line_rows or []
    speed_dial_rows = speed_dial_rows or []
    data = {
        "lines-TOTAL_FORMS": str(len(line_rows)),
        "lines-INITIAL_FORMS": "0",
        "lines-MIN_NUM_FORMS": "0",
        "lines-MAX_NUM_FORMS": "1000",
        "speed_dials-TOTAL_FORMS": str(len(speed_dial_rows)),
        "speed_dials-INITIAL_FORMS": "0",
        "speed_dials-MIN_NUM_FORMS": "0",
        "speed_dials-MAX_NUM_FORMS": "1000",
    }
    for index, row in enumerate(line_rows):
        data.update(
            {
                f"lines-{index}-id": "",
                f"lines-{index}-line_index": str(row["line_index"]),
                f"lines-{index}-extension": str(row["extension"].id),
                f"lines-{index}-label": row.get("label", ""),
            }
        )
    for index, row in enumerate(speed_dial_rows):
        data.update(
            {
                f"speed_dials-{index}-id": "",
                f"speed_dials-{index}-position": str(row["position"]),
                f"speed_dials-{index}-label": row["label"],
                f"speed_dials-{index}-destination": row["destination"],
            }
        )
    return data


def ivr_menu_formset_data(*, option_rows=None, total_forms=4):
    option_rows = option_rows or []
    data = {
        "menu_options-TOTAL_FORMS": str(total_forms),
        "menu_options-INITIAL_FORMS": "0",
        "menu_options-MIN_NUM_FORMS": "0",
        "menu_options-MAX_NUM_FORMS": "1000",
    }
    for index in range(total_forms):
        row = option_rows[index] if index < len(option_rows) else {}
        data.update(
            {
                f"menu_options-{index}-id": "",
                f"menu_options-{index}-digit": row.get("digit", ""),
                f"menu_options-{index}-label": row.get("label", ""),
                f"menu_options-{index}-destination": str(row["destination"].id) if row.get("destination") else "",
            }
        )
    return data


def provider_form_data(**overrides):
    data = {
        "name": "Carrier SIP",
        "slug": "carrier-sip",
        "provider_type": Provider.ProviderType.SIP,
        "notes": "Primary carrier",
        "is_active": "on",
    }
    data.update(overrides)
    return data


def trunk_form_data(location, provider, **overrides):
    data = {
        "location": str(location.id),
        "provider": str(provider.id),
        "name": "Primary SIP",
        "trunk_type": Trunk.TrunkType.SIP,
        "host": "sip.provider.example.test",
        "username": "branch-user",
        "password": "branch-secret",
        "is_emergency_capable": "",
        "is_active": "on",
    }
    data.update(overrides)
    return data


def outbound_route_form_data(location, **overrides):
    data = {
        "location": str(location.id),
        "name": "Local",
        "dial_pattern": "NXXNXXXXXX",
        "priority": "1",
        "caller_id_source": OutboundRoute.CallerIdSource.LOCATION_DEFAULT,
        "caller_id_number": "",
        "recording_policy": OutboundRoute.RecordingPolicy.NEVER,
        "is_active": "on",
        "is_emergency_route": "",
    }
    data.update(overrides)
    return data


def route_trunk_formset_data(*, trunk_rows=None):
    trunk_rows = trunk_rows or []
    data = {
        "route_trunks-TOTAL_FORMS": str(len(trunk_rows)),
        "route_trunks-INITIAL_FORMS": "0",
        "route_trunks-MIN_NUM_FORMS": "0",
        "route_trunks-MAX_NUM_FORMS": "1000",
    }
    for index, row in enumerate(trunk_rows):
        data.update(
            {
                f"route_trunks-{index}-id": "",
                f"route_trunks-{index}-priority": str(row["priority"]),
                f"route_trunks-{index}-trunk": str(row["trunk"].id),
            }
        )
    return data


def add_emergency_route(location):
    provider, _created = Provider.objects.get_or_create(
        slug=f"{location.slug}-emergency-sip",
        defaults={
            "name": f"{location.name} Emergency SIP",
            "provider_type": Provider.ProviderType.SIP,
        },
    )
    trunk = Trunk.objects.create(
        location=location,
        provider=provider,
        name="Emergency SIP",
        trunk_type=Trunk.TrunkType.SIP,
        host="sip.emergency.example.test",
        username="emergency",
        password="emergency-secret",
        is_emergency_capable=True,
    )
    route = OutboundRoute.objects.create(
        location=location,
        name="Emergency",
        dial_pattern="911",
        priority=99,
        is_emergency_route=True,
        caller_id_source=OutboundRoute.CallerIdSource.EMERGENCY,
    )
    OutboundRouteTrunk.objects.create(outbound_route=route, trunk=trunk, priority=1)
    return route


def did_default_destination(location, extension):
    return InboundDestination.objects.create(
        location=location,
        name=f"Default {extension.number}",
        destination_type=InboundDestination.DestinationType.EXTENSION,
        extension=extension,
    )


class PortalRouteTests(TestCase):
    def setUp(self):
        self.viewer = User.objects.create_user(username="viewer", password="portal-pass")
        self.admin = User.objects.create_user(username="admin", password="portal-pass")
        assign_role(self.viewer, PortalRole.VIEWER)
        assign_role(self.admin, PortalRole.ADMIN)

    def test_health_route_returns_ok(self):
        response = self.client.get(reverse("health"))

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), {"status": "ok"})

    def test_home_requires_named_user_login(self):
        response = self.client.get(reverse("home"))

        self.assertEqual(response.status_code, 302)
        self.assertIn(reverse("login"), response["Location"])

    def test_home_route_renders_base_shell(self):
        self.client.force_login(self.viewer)

        response = self.client.get(reverse("home"))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'id="portal-main"')
        self.assertContains(response, "hx-boost")
        self.assertContains(response, "Extensions")
        self.assertContains(response, "Phones")
        self.assertContains(response, "DIDs")
        self.assertContains(response, "IVRs")
        self.assertContains(response, "Ring Groups")
        self.assertContains(response, "Queues")
        self.assertContains(response, "Paging Groups")
        self.assertContains(response, "Feature Codes")
        self.assertContains(response, "Dial Plan")
        self.assertContains(response, "viewer - Viewer")
        self.assertNotContains(response, "Settings")

    def test_users_roles_route_renders_user_create_form(self):
        self.client.force_login(self.admin)

        response = self.client.get(reverse("users-roles"))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "New user")
        self.assertContains(response, 'name="username"')
        self.assertContains(response, 'name="password"')
        self.assertContains(response, 'name="role"')
        self.assertContains(response, reverse("user-delete", args=[self.viewer.id]))
        self.assertNotContains(response, reverse("user-delete", args=[self.admin.id]))

    def test_admin_can_create_user_with_password_and_role_from_users_roles_page(self):
        self.client.force_login(self.admin)

        response = self.client.post(
            reverse("users-roles"),
            {
                "username": "managed-ui",
                "email": "managed-ui@example.test",
                "password": "managed-secret",
                "role": PortalRole.OPERATOR,
                "is_active": "on",
            },
        )

        self.assertRedirects(response, reverse("users-roles"))
        user = User.objects.get(username="managed-ui")
        self.assertEqual(user.email, "managed-ui@example.test")
        self.assertTrue(user.check_password("managed-secret"))
        self.assertTrue(user.is_active)
        self.assertEqual(get_user_role(user), PortalRole.OPERATOR)
        audit = AuditLog.objects.get(action=AuditAction.API_USER_UPDATE, target=f"users/{user.id}")
        self.assertEqual(audit.actor, self.admin)
        self.assertEqual(audit.details["changed_fields"], ["email", "is_active", "password", "role", "username"])

    def test_duplicate_user_create_renders_inline_error(self):
        self.client.force_login(self.admin)

        response = self.client.post(
            reverse("users-roles"),
            {
                "username": self.viewer.username,
                "password": "managed-secret",
                "role": PortalRole.VIEWER,
                "is_active": "on",
            },
        )

        self.assertEqual(response.status_code, 400)
        self.assertContains(response, "A user with this username already exists.", status_code=400)
        self.assertEqual(User.objects.filter(username=self.viewer.username).count(), 1)

    def test_admin_can_delete_user_from_users_roles_page(self):
        target = User.objects.create_user(username="delete-me", password="portal-pass")
        assign_role(target, PortalRole.VIEWER)
        self.client.force_login(self.admin)

        response = self.client.post(reverse("user-delete", args=[target.id]))

        self.assertRedirects(response, reverse("users-roles"))
        self.assertFalse(User.objects.filter(pk=target.id).exists())
        audit = AuditLog.objects.get(action=AuditAction.API_USER_UPDATE, target=f"users/{target.id}")
        self.assertEqual(audit.actor, self.admin)
        self.assertEqual(audit.details["username"], "delete-me")
        self.assertEqual(audit.details["changed_fields"], ["deleted"])

    def test_admin_cannot_delete_own_user_from_users_roles_page(self):
        self.client.force_login(self.admin)

        response = self.client.post(reverse("user-delete", args=[self.admin.id]))

        self.assertEqual(response.status_code, 400)
        self.assertContains(response, "You cannot delete your own account.", status_code=400)
        self.assertTrue(User.objects.filter(pk=self.admin.id).exists())
        self.assertEqual(AuditLog.objects.filter(action=AuditAction.API_USER_UPDATE).count(), 0)

    def test_nav_links_expose_area_markers_for_active_sync(self):
        self.client.force_login(self.viewer)

        response = self.client.get(reverse("home"))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'href="' + reverse("home") + '" data-area="home"')
        self.assertContains(
            response,
            'href="' + reverse("inbound-destinations") + '" data-area="inbound-destinations"',
        )
        self.assertContains(response, '<section class="page-heading" data-area="home">')

    def test_initial_portal_area_routes_render(self):
        self.client.force_login(self.viewer)
        route_names = [
            "extensions",
            "phones",
            "inbound-destinations",
            "dids",
            "ivrs",
            "ring-groups",
            "queues",
            "paging-groups",
            "feature-codes",
            "trunks",
            "dial-plan",
        ]

        for route_name in route_names:
            with self.subTest(route_name=route_name):
                response = self.client.get(reverse(route_name))

                self.assertEqual(response.status_code, 200)
                self.assertContains(response, 'data-area="' + route_name + '"')

    def test_admin_only_settings_route_denies_viewer_and_allows_admin(self):
        self.client.force_login(self.viewer)

        viewer_response = self.client.get(reverse("settings"))

        self.assertEqual(viewer_response.status_code, 403)

        self.client.force_login(self.admin)

        admin_response = self.client.get(reverse("settings"))

        self.assertEqual(admin_response.status_code, 200)
        self.assertContains(admin_response, 'data-area="settings"')
        self.assertContains(admin_response, "plaintext telecom secret")
        self.assertContains(admin_response, "LAN/WARP")
        self.assertContains(admin_response, "emergency calling")
        self.assertContains(admin_response, "call recording consent")

    def test_htmx_request_returns_partial_content(self):
        self.client.force_login(self.viewer)

        response = self.client.get(reverse("extensions"), headers={"HX-Request": "true"})

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'data-area="extensions"')
        self.assertNotContains(response, "<html")


class DashboardViewTests(TestCase):
    def setUp(self):
        self.viewer = User.objects.create_user(username="dashboard-viewer", password="portal-pass")
        self.operator = User.objects.create_user(username="dashboard-operator", password="portal-pass")
        assign_role(self.viewer, PortalRole.VIEWER)
        assign_role(self.operator, PortalRole.OPERATOR)

    def test_home_dashboard_reflects_agent_telemetry_and_config_drift(self):
        location = Location.objects.create(**location_model_data(name="Dashboard HQ", slug="dashboard-hq"))
        deployed_version = dashboard_config_version(
            location,
            1,
            deployment_status=ConfigVersion.DeploymentStatus.ROLLED_BACK,
            deployed_at=timezone.now() - timedelta(minutes=2),
        )
        dashboard_config_version(location, 2)
        DeploymentRecord.objects.create(
            location=location,
            config_version=deployed_version,
            rollback_source_version=ConfigVersion.objects.get(location=location, version_number=2),
            target_host=location.deployment_ssh_host,
            target_username=location.deployment_ssh_username,
            staging_path=location.deployment_staging_path,
            asterisk_path=location.deployment_asterisk_path,
            tftp_path=location.deployment_tftp_path,
            reload_command=location.deployment_reload_command,
            action=DeploymentRecord.Action.ROLLBACK,
            status=DeploymentRecord.Status.SUCCESS,
            reload_result=DeploymentRecord.ReloadResult.SUCCESS,
        )
        reported_at = timezone.now()
        location.active_config_version_number = 1
        location.active_config_checksum = deployed_version.checksum
        location.active_config_timestamp = reported_at
        location.active_config_reported_at = reported_at
        location.agent_telemetry_reported_at = reported_at
        location.agent_telemetry_errors = []
        location.agent_telemetry = {
            "timestamp": reported_at.isoformat(),
            "location_health": {
                "ami_connected": True,
                "ami_response": "success",
                "core_current_calls": 1,
                "core_max_calls": 20,
            },
            "phone_registrations": [
                {"extension": "3000", "status": "reachable", "reachable": True},
                {"extension": "3001", "status": "unreachable", "reachable": False},
            ],
            "trunk_status": [
                {"name": "trunk-primary-sip", "status": "reachable", "available": True},
            ],
            "active_calls": [
                {"caller_id": "3000", "connected_line": "3001", "state": "Up"},
            ],
            "queue_status": [
                {"name": "support-queue", "calls_waiting": 2, "members": [{"name": "3000"}], "callers": []},
            ],
            "recent_calls": [
                {"source": "3000", "destination": "+15551230000", "disposition": "ANSWERED"},
            ],
            "recording_metadata": [
                {"filename": "call-123.wav", "size_bytes": 1200},
            ],
            "telemetry_errors": [],
        }
        location.save(
            update_fields=[
                "active_config_version_number",
                "active_config_checksum",
                "active_config_timestamp",
                "active_config_reported_at",
                "agent_telemetry",
                "agent_telemetry_errors",
                "agent_telemetry_reported_at",
                "updated_at",
            ]
        )
        self.client.force_login(self.viewer)

        response = self.client.get(reverse("home"))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Dashboard")
        self.assertContains(response, "Dashboard HQ")
        self.assertContains(response, "Agent Connection: Reporting")
        self.assertContains(response, "Location Health")
        self.assertContains(response, "Extension Registrations")
        self.assertContains(response, "3000 - reachable")
        self.assertContains(response, "Trunk Status")
        self.assertContains(response, "trunk-primary-sip - reachable")
        self.assertContains(response, "Active Calls")
        self.assertContains(response, "3000 to 3001 - Up")
        self.assertContains(response, "Queue Status")
        self.assertContains(response, "support-queue - 2 waiting")
        self.assertContains(response, "Recent Calls")
        self.assertContains(response, "3000 to +15551230000 - ANSWERED")
        self.assertContains(response, "Recording Availability")
        self.assertContains(response, "call-123.wav - 1200 bytes")
        self.assertContains(response, "Unavailable")
        self.assertContains(response, "Playback unavailable")
        self.assertNotContains(response, "Playback</a>")
        self.assertContains(response, "Config Drift Warning")
        self.assertContains(response, "Active v1 differs from latest exported v2.")
        self.assertContains(response, "Latest exported v2 differs from latest deployed v1.")
        self.assertContains(response, "Deployment History")
        self.assertContains(response, "Rollback")
        self.assertContains(response, "Rollback source v2")

    def test_dashboard_panel_htmx_partial_returns_refreshable_content(self):
        Location.objects.create(**location_model_data(name="Partial HQ", slug="partial-hq"))
        self.client.force_login(self.viewer)

        response = self.client.get(reverse("dashboard-panel"), headers={"HX-Request": "true"})

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'id="dashboard-panel"')
        self.assertContains(response, 'hx-get="' + reverse("dashboard-panel") + '"')
        self.assertContains(response, "Partial HQ")
        self.assertContains(response, "Agent Connection: Waiting")
        self.assertNotContains(response, "<html")

    def test_dashboard_shows_recording_playback_link_for_recording_role(self):
        location = Location.objects.create(**location_model_data(name="Recording HQ", slug="recording-hq"))
        reported_at = timezone.now()
        recording_id = recording_id_for_path("call-456.wav")
        location.agent_telemetry_reported_at = reported_at
        location.agent_telemetry_errors = []
        location.agent_telemetry = {
            "timestamp": reported_at.isoformat(),
            "location_health": {"ami_connected": True},
            "phone_registrations": [],
            "trunk_status": [],
            "active_calls": [],
            "queue_status": [],
            "recent_calls": [],
            "recording_metadata": [
                {
                    "recording_id": recording_id,
                    "relative_path": "call-456.wav",
                    "path": "/var/spool/asterisk/monitor/call-456.wav",
                    "filename": "call-456.wav",
                    "size_bytes": 4,
                    "modified_at": reported_at.isoformat(),
                    "retention_expires_at": (reported_at + timedelta(days=30)).isoformat(),
                    "available": True,
                }
            ],
            "telemetry_errors": [],
        }
        location.save(update_fields=["agent_telemetry", "agent_telemetry_errors", "agent_telemetry_reported_at", "updated_at"])
        self.client.force_login(self.operator)

        response = self.client.get(reverse("home"))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "call-456.wav - 4 bytes")
        self.assertContains(response, "Available")
        self.assertContains(response, reverse("location-recording-playback", args=[location.slug, recording_id]))


class LocationFormValidationTests(TestCase):
    def test_timezone_field_is_select_defaulting_to_los_angeles(self):
        form = LocationForm(include_sensitive_fields=True)
        timezone_values = [value for value, _label in form.fields["timezone"].choices]

        self.assertEqual(form.fields["timezone"].widget.input_type, "select")
        self.assertEqual(form["timezone"].value(), "America/Los_Angeles")
        self.assertEqual(Location._meta.get_field("timezone").default, "America/Los_Angeles")
        self.assertIn("America/Los_Angeles", timezone_values)
        self.assertIn("UTC", timezone_values)

    def test_listen_address_fields_default_to_all_interfaces(self):
        form = LocationForm(include_sensitive_fields=True)

        for field_name in ("sip_bind_ip", "iax_bind_ip"):
            with self.subTest(field_name=field_name):
                self.assertEqual(form[field_name].value(), "0.0.0.0")
                self.assertEqual(Location._meta.get_field(field_name).default, "0.0.0.0")

    def test_ami_fields_default_to_bind_all_and_generated_credentials(self):
        form = LocationForm(include_sensitive_fields=True)
        second_form = LocationForm(include_sensitive_fields=True)

        self.assertEqual(form["ami_host"].value(), "0.0.0.0")
        self.assertEqual(Location._meta.get_field("ami_host").default, "0.0.0.0")
        self.assertTrue(form["ami_username"].value().startswith("ami-location-"))
        self.assertNotEqual(form["ami_username"].value(), second_form["ami_username"].value())
        self.assertGreaterEqual(len(form["ami_secret"].value()), 40)
        self.assertNotEqual(form["ami_secret"].value(), second_form["ami_secret"].value())
        self.assertTrue(form.fields["ami_secret"].widget.render_value)

    def test_admin_form_requires_complete_location_fields(self):
        form = LocationForm(data={}, include_sensitive_fields=True)

        self.assertFalse(form.is_valid())
        for field_name in (
            "name",
            "slug",
            "lan_subnet",
            "pbx_lan_ip",
            "pbx_warp_ip",
            "deployment_ssh_host",
            "deployment_ssh_port",
            "deployment_ssh_username",
            "deployment_ssh_private_key",
            "deployment_staging_path",
            "deployment_asterisk_path",
            "deployment_tftp_path",
            "deployment_reload_command",
            "sip_bind_ip",
            "sip_port",
            "rtp_port_start",
            "rtp_port_end",
            "iax_bind_ip",
            "iax_port",
            "default_did",
            "emergency_caller_id",
            "emergency_trunk",
            "recording_retention_days",
            "smtp_port",
            "ami_host",
            "ami_port",
            "agent_secret",
        ):
            with self.subTest(field_name=field_name):
                self.assertIn(field_name, form.errors)

    def test_admin_form_accepts_complete_location_record(self):
        form = LocationForm(data=location_form_data(), include_sensitive_fields=True)

        self.assertTrue(form.is_valid(), form.errors)

    def test_emergency_trunk_record_must_be_emergency_capable_and_syncs_label(self):
        location = Location.objects.create(**location_model_data())
        provider = Provider.objects.create(name="Emergency Provider", slug="emergency-provider")
        emergency_trunk = Trunk.objects.create(
            location=location,
            provider=provider,
            name="Verified Emergency SIP",
            trunk_type=Trunk.TrunkType.SIP,
            is_emergency_capable=True,
        )
        normal_trunk = Trunk.objects.create(
            location=location,
            provider=provider,
            name="Normal SIP",
            trunk_type=Trunk.TrunkType.SIP,
            is_emergency_capable=False,
        )

        form = LocationForm(
            data=location_form_data(emergency_trunk_ref=str(emergency_trunk.id)),
            instance=location,
            include_sensitive_fields=True,
        )

        self.assertTrue(form.is_valid(), form.errors)
        updated = form.save()
        self.assertEqual(updated.emergency_trunk_ref, emergency_trunk)
        self.assertEqual(updated.emergency_trunk, "Verified Emergency SIP")

        invalid_form = LocationForm(
            data=location_form_data(emergency_trunk_ref=str(normal_trunk.id)),
            instance=location,
            include_sensitive_fields=True,
        )

        self.assertFalse(invalid_form.is_valid())
        self.assertIn("emergency_trunk_ref", invalid_form.errors)

    def test_location_accepts_voicemail_without_smtp_settings(self):
        form = LocationForm(
            data=location_form_data(
                smtp_host="",
                smtp_from_email="",
                smtp_username="",
                smtp_password="",
            ),
            include_sensitive_fields=True,
        )

        self.assertTrue(form.is_valid(), form.errors)

    def test_location_rejects_partial_smtp_settings(self):
        form = LocationForm(
            data=location_form_data(
                smtp_host="smtp-branch.example.test",
                smtp_from_email="",
                smtp_username="",
                smtp_password="",
            ),
            include_sensitive_fields=True,
        )

        self.assertFalse(form.is_valid())
        self.assertIn("smtp_from_email", form.errors)

    def test_form_validates_lan_membership_and_rtp_range(self):
        form = LocationForm(
            data=location_form_data(
                pbx_lan_ip="10.31.0.10",
                rtp_port_start="20000",
                rtp_port_end="10000",
            ),
            include_sensitive_fields=True,
        )

        self.assertFalse(form.is_valid())
        self.assertIn("pbx_lan_ip", form.errors)
        self.assertIn("rtp_port_end", form.errors)


class LocationManagementViewTests(TestCase):
    def setUp(self):
        self.viewer = User.objects.create_user(username="location-viewer", password="portal-pass")
        self.editor = User.objects.create_user(username="location-editor", password="portal-pass")
        self.admin = User.objects.create_user(username="location-admin", password="portal-pass")
        assign_role(self.viewer, PortalRole.VIEWER)
        assign_role(self.editor, PortalRole.EDITOR)
        assign_role(self.admin, PortalRole.ADMIN)

    def test_location_list_route_shows_status_fields(self):
        Location.objects.create(**location_model_data())
        self.client.force_login(self.viewer)

        response = self.client.get(reverse("locations"))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'data-area="locations"')
        self.assertContains(response, "Active")
        self.assertContains(response, "Last Deployed")
        self.assertContains(response, "Deployment Status")
        self.assertContains(response, "PBX Active Version")
        self.assertContains(response, "Not reported")
        self.assertContains(response, "Branch Office")

    def test_location_list_only_admin_sees_create_action(self):
        Location.objects.create(**location_model_data())

        self.client.force_login(self.editor)
        editor_response = self.client.get(reverse("locations"))
        self.assertEqual(editor_response.status_code, 200)
        self.assertNotContains(editor_response, "New Location")

        self.client.force_login(self.admin)
        admin_response = self.client.get(reverse("locations"))
        self.assertEqual(admin_response.status_code, 200)
        self.assertContains(admin_response, "New Location")

    def test_viewer_cannot_create_location(self):
        self.client.force_login(self.viewer)

        response = self.client.get(reverse("location-create"))

        self.assertEqual(response.status_code, 403)

    def test_editor_cannot_create_location(self):
        self.client.force_login(self.editor)

        response = self.client.get(reverse("location-create"))

        self.assertEqual(response.status_code, 403)

    def test_editor_edit_form_hides_sensitive_fields_and_shows_emergency_fields(self):
        location = Location.objects.create(**location_model_data())
        self.client.force_login(self.editor)

        response = self.client.get(reverse("location-edit", args=[location.slug]))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Emergency caller ID")
        self.assertContains(response, "Emergency trunk")
        self.assertContains(response, "Restricted Settings")
        self.assertNotContains(response, 'name="deployment_ssh_private_key"')
        self.assertNotContains(response, 'name="deployment_staging_path"')
        self.assertNotContains(response, 'name="deployment_reload_command"')
        self.assertNotContains(response, 'name="agent_secret"')
        self.assertNotContains(response, 'name="ami_secret"')

    def test_admin_form_shows_sensitive_deployment_and_agent_fields(self):
        self.client.force_login(self.admin)

        response = self.client.get(reverse("location-create"))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'name="deployment_ssh_private_key"')
        self.assertContains(response, 'name="deployment_ssh_host"')
        self.assertContains(response, 'name="deployment_staging_path"')
        self.assertContains(response, 'name="deployment_reload_command"')
        self.assertContains(response, 'name="agent_secret"')
        self.assertContains(response, 'name="ami_secret"')

    def test_admin_create_form_timezone_renders_los_angeles_select(self):
        self.client.force_login(self.admin)

        response = self.client.get(reverse("location-create"))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, '<select name="timezone"', html=False)
        self.assertContains(
            response,
            '<option value="America/Los_Angeles" selected>America/Los Angeles</option>',
            html=True,
        )

    def test_admin_create_form_defaults_sip_and_iax_to_all_interfaces(self):
        self.client.force_login(self.admin)

        response = self.client.get(reverse("location-create"))

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context["form"]["sip_bind_ip"].value(), "0.0.0.0")
        self.assertEqual(response.context["form"]["iax_bind_ip"].value(), "0.0.0.0")
        self.assertContains(response, 'name="sip_bind_ip" value="0.0.0.0"', html=False)
        self.assertContains(response, 'name="iax_bind_ip" value="0.0.0.0"', html=False)

    def test_admin_create_form_defaults_ami_host_and_credentials(self):
        self.client.force_login(self.admin)

        response = self.client.get(reverse("location-create"))

        form = response.context["form"]
        self.assertEqual(response.status_code, 200)
        self.assertEqual(form["ami_host"].value(), "0.0.0.0")
        self.assertTrue(form["ami_username"].value().startswith("ami-location-"))
        self.assertGreaterEqual(len(form["ami_secret"].value()), 40)
        self.assertTrue(form.fields["ami_secret"].widget.render_value)

    def test_admin_can_create_complete_location_record(self):
        self.client.force_login(self.admin)

        response = self.client.post(reverse("location-create"), location_form_data())

        location = Location.objects.get(slug="branch-office")
        default_codes = {spec.code for spec in default_feature_code_specs()}
        self.assertEqual(response.status_code, 302)
        self.assertEqual(location.lan_subnet, "10.30.0.0/24")
        self.assertEqual(location.default_did, "+15551203000")
        self.assertEqual(location.emergency_caller_id, "+15551203999")
        self.assertEqual(location.deployment_ssh_private_key, "branch-private-key")
        self.assertEqual(location.deployment_asterisk_path, "/srv/pbx/asterisk")
        self.assertEqual(location.deployment_reload_command, "asterisk -rx 'core reload'")
        self.assertEqual(location.deployment_status, Location.DeploymentStatus.READY)
        self.assertEqual(set(location.feature_codes.values_list("code", flat=True)), default_codes)

    def test_admin_can_create_location_without_manual_ami_credentials(self):
        self.client.force_login(self.admin)

        response = self.client.post(
            reverse("location-create"),
            location_form_data(
                slug="generated-ami",
                ami_username="",
                ami_secret="",
            ),
        )

        location = Location.objects.get(slug="generated-ami")
        self.assertEqual(response.status_code, 302)
        self.assertTrue(location.ami_username.startswith("ami-generated-ami-"))
        self.assertGreaterEqual(len(location.ami_secret), 40)
        audit_log = AuditLog.objects.get(action=AuditAction.CONFIG_CHANGE)
        audit_text = repr(audit_log.details)
        self.assertIn("ami_access", audit_log.details)
        self.assertNotIn(location.ami_secret, audit_text)

    def test_export_history_actions_follow_role_permissions(self):
        operator = User.objects.create_user(username="location-operator", password="portal-pass")
        assign_role(operator, PortalRole.OPERATOR)
        location = Location.objects.create(**location_model_data(name="Export HQ", slug="export-hq"))
        Extension.objects.create(location=location, number="3000", display_name="Export Desk")
        add_emergency_route(location)
        first = create_config_version(location, exported_by=self.admin)
        second = create_config_version(location, exported_by=self.admin)

        self.client.force_login(self.viewer)
        viewer_response = self.client.get(reverse("location-detail", args=[location.slug]))

        self.assertEqual(viewer_response.status_code, 200)
        self.assertContains(viewer_response, "Export history")
        self.assertContains(viewer_response, f"v{second.version_number}")
        self.assertContains(viewer_response, second.checksum)
        self.assertContains(viewer_response, "docker-compose.yml")
        self.assertNotContains(viewer_response, "Export ZIP")
        self.assertNotContains(viewer_response, "Download")
        self.assertNotContains(viewer_response, ">Deploy</button>")
        self.assertNotContains(viewer_response, ">Rollback</button>")
        self.assertNotContains(viewer_response, "Live Controls")
        self.assertNotContains(viewer_response, "Core reload")

        self.client.force_login(self.editor)
        editor_response = self.client.get(reverse("location-detail", args=[location.slug]))

        self.assertNotContains(editor_response, "Export ZIP")
        self.assertNotContains(editor_response, "Download")
        self.assertNotContains(editor_response, ">Deploy</button>")
        self.assertNotContains(editor_response, ">Rollback</button>")
        self.assertNotContains(editor_response, "Live Controls")
        self.assertNotContains(editor_response, "Core reload")

        self.client.force_login(operator)
        operator_response = self.client.get(reverse("location-detail", args=[location.slug]))

        self.assertNotContains(operator_response, "Export ZIP")
        self.assertNotContains(operator_response, "Download")
        self.assertNotContains(operator_response, ">Deploy</button>")
        self.assertNotContains(operator_response, ">Rollback</button>")
        self.assertContains(operator_response, "Live Controls")
        self.assertContains(operator_response, "Core reload")
        self.assertContains(operator_response, "PJSIP reload")
        self.assertContains(operator_response, "Queue reload")

        self.client.force_login(self.admin)
        admin_response = self.client.get(reverse("location-detail", args=[location.slug]))

        self.assertContains(admin_response, "Export ZIP")
        self.assertContains(admin_response, "Download")
        self.assertContains(admin_response, ">Deploy</button>")
        self.assertContains(admin_response, ">Rollback</button>")
        self.assertContains(admin_response, "Live Controls")

        self.client.force_login(self.viewer)
        self.assertEqual(
            self.client.post(reverse("location-config-export", args=[location.slug])).status_code,
            403,
        )
        self.assertEqual(
            self.client.get(reverse("location-config-export-download", args=[location.slug, first.version_number])).status_code,
            403,
        )
        self.client.force_login(self.editor)
        self.assertEqual(
            self.client.post(reverse("location-config-export", args=[location.slug])).status_code,
            403,
        )
        self.assertEqual(
            self.client.get(reverse("location-config-export-download", args=[location.slug, first.version_number])).status_code,
            403,
        )
        self.client.force_login(operator)
        self.assertEqual(
            self.client.post(
                reverse("location-config-export-deploy", args=[location.slug, first.version_number]),
                {"confirm_reload": "1"},
            ).status_code,
            403,
        )
        self.assertEqual(
            self.client.post(
                reverse("location-config-export-rollback", args=[location.slug, first.version_number]),
                {"confirm_reload": "1"},
            ).status_code,
            403,
        )

    def test_location_detail_surfaces_security_and_compliance_notes(self):
        location = Location.objects.create(**location_model_data(name="Compliance HQ", slug="compliance-hq"))
        Extension.objects.create(location=location, number="3000", display_name="Compliance Desk")
        add_emergency_route(location)

        self.client.force_login(self.editor)

        response = self.client.get(reverse("location-detail", args=[location.slug]))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Export and Deployment Warnings")
        self.assertContains(response, "plaintext SIP")
        self.assertContains(response, "LAN/WARP")
        self.assertContains(response, "emergency calling routes")
        self.assertContains(response, "recording consent")

    def test_export_download_deploy_and_rollback_actions_update_history(self):
        operator = User.objects.create_user(username="deploy-operator", password="portal-pass")
        assign_role(operator, PortalRole.OPERATOR)
        location = Location.objects.create(**location_model_data(name="Deploy HQ", slug="deploy-hq"))
        Extension.objects.create(location=location, number="3000", display_name="Deploy Desk")
        add_emergency_route(location)

        self.client.force_login(self.admin)
        export_response = self.client.post(reverse("location-config-export", args=[location.slug]))

        self.assertEqual(export_response.status_code, 302)
        version = ConfigVersion.objects.get(location=location)
        self.assertEqual(version.version_number, 1)

        download_response = self.client.get(
            reverse("location-config-export-download", args=[location.slug, version.version_number])
        )

        self.assertEqual(download_response.status_code, 200)
        self.assertEqual(download_response["Content-Type"], "application/zip")
        self.assertEqual(download_response.content, bytes(version.archive))

        def fake_deploy(selected_version, *, operator, reload_confirmed, rollback=False):
            self.assertTrue(reload_confirmed)
            selected_version.mark_deployed(operator, rolled_back=rollback)
            selected_location = selected_version.location
            selected_location.last_deployed_at = selected_version.deployed_at
            selected_location.deployment_status = Location.DeploymentStatus.DEPLOYED
            selected_location.save(update_fields=["last_deployed_at", "deployment_status", "updated_at"])
            return mock.Mock()

        self.client.force_login(self.admin)
        with mock.patch("core.views.deploy_config_version", side_effect=fake_deploy) as deploy_mock:
            deploy_response = self.client.post(
                reverse("location-config-export-deploy", args=[location.slug, version.version_number]),
                {"confirm_reload": "1"},
            )

        self.assertEqual(deploy_response.status_code, 302)
        deploy_mock.assert_called_once()
        version.refresh_from_db()
        location.refresh_from_db()
        self.assertEqual(version.deployment_status, ConfigVersion.DeploymentStatus.DEPLOYED)
        self.assertEqual(location.deployment_status, Location.DeploymentStatus.DEPLOYED)
        self.assertIsNotNone(location.last_deployed_at)

        with mock.patch("core.views.deploy_config_version", side_effect=fake_deploy) as rollback_mock:
            rollback_response = self.client.post(
                reverse("location-config-export-rollback", args=[location.slug, version.version_number]),
                {"confirm_reload": "1"},
            )

        self.assertEqual(rollback_response.status_code, 302)
        rollback_mock.assert_called_once()
        version.refresh_from_db()
        self.assertEqual(version.deployment_status, ConfigVersion.DeploymentStatus.ROLLED_BACK)

    def test_deploy_requires_reload_confirmation_and_audits_denial(self):
        location = Location.objects.create(**location_model_data(name="Confirm HQ", slug="confirm-hq"))
        Extension.objects.create(location=location, number="3000", display_name="Confirm Desk")
        add_emergency_route(location)
        version = create_config_version(location, exported_by=self.admin)
        self.client.force_login(self.admin)

        response = self.client.post(reverse("location-config-export-deploy", args=[location.slug, version.version_number]))

        self.assertEqual(response.status_code, 400)
        self.assertContains(response, "Reload confirmation is required", status_code=400)
        record = DeploymentRecord.objects.get(config_version=version)
        self.assertEqual(record.status, DeploymentRecord.Status.FAILED)
        self.assertEqual(record.reload_result, DeploymentRecord.ReloadResult.NOT_RUN)
        audit = AuditLog.objects.get(action=AuditAction.DEPLOYMENT)
        self.assertEqual(audit.outcome, AuditOutcome.DENIED)

    def test_editor_update_ignores_spoofed_sensitive_fields(self):
        location = Location.objects.create(
            **location_model_data(
                name="Spoof Target",
                slug="spoof-target",
                deployment_ssh_private_key="original-private-key",
                deployment_staging_path="/srv/pbx/original-staging",
                deployment_reload_command="asterisk -rx 'core reload'",
                smtp_password="original-smtp-password",
                ami_secret="original-ami-secret",
                agent_secret="original-agent-secret",
            )
        )
        self.client.force_login(self.editor)

        response = self.client.post(
            reverse("location-edit", args=[location.slug]),
            location_form_data(
                name="Spoof Target Updated",
                slug="spoof-target",
                deployment_ssh_private_key="spoofed-private-key",
                deployment_staging_path="/tmp/spoofed-staging",
                deployment_reload_command="rm -rf /",
                smtp_password="spoofed-smtp-password",
                ami_secret="spoofed-ami-secret",
                agent_secret="spoofed-agent-secret",
            ),
        )

        self.assertEqual(response.status_code, 302)
        location.refresh_from_db()
        self.assertEqual(location.name, "Spoof Target Updated")
        self.assertEqual(location.deployment_ssh_private_key, "original-private-key")
        self.assertEqual(location.deployment_staging_path, "/srv/pbx/original-staging")
        self.assertEqual(location.deployment_reload_command, "asterisk -rx 'core reload'")
        self.assertEqual(location.smtp_password, "original-smtp-password")
        self.assertEqual(location.ami_secret, "original-ami-secret")
        self.assertEqual(location.agent_secret, "original-agent-secret")


class LiveOperationViewTests(TestCase):
    def setUp(self):
        self.viewer = User.objects.create_user(username="live-viewer", password="portal-pass")
        self.operator = User.objects.create_user(username="live-operator", password="portal-pass")
        self.admin = User.objects.create_user(username="live-admin", password="portal-pass")
        assign_role(self.viewer, PortalRole.VIEWER)
        assign_role(self.operator, PortalRole.OPERATOR)
        assign_role(self.admin, PortalRole.ADMIN)
        self.location = Location.objects.create(
            **location_model_data(name="Live HQ", slug="live-hq", agent_secret="agent-secret")
        )

    def test_unauthorized_user_cannot_execute_live_operation_and_is_audited(self):
        self.client.force_login(self.viewer)

        with mock.patch("core.views.run_location_live_command") as dispatcher:
            response = self.client.post(
                reverse("location-live-operation", args=[self.location.slug]),
                data=json.dumps({"command": "core_reload"}),
                content_type="application/json",
            )

        self.assertEqual(response.status_code, 403)
        dispatcher.assert_not_called()
        payload = json.loads(response.content.decode("utf-8"))
        self.assertEqual(payload["status"], "denied")
        audit = AuditLog.objects.get(action=AuditAction.LIVE_PBX_ACTION)
        self.assertEqual(audit.actor, self.viewer)
        self.assertEqual(audit.outcome, AuditOutcome.DENIED)
        self.assertEqual(audit.details["command"], "core_reload")
        self.assertEqual(audit.details["actor_username"], "live-viewer")
        self.assertEqual(audit.details["location_slug"], "live-hq")

    def test_operator_live_operation_dispatches_and_audits_success(self):
        self.client.force_login(self.operator)
        result = {
            "type": "live_command_result",
            "command_id": "cmd-1",
            "command": "core_reload",
            "status": "success",
            "ami_action": "Command",
            "ami_response": [{"Response": "Success", "Message": "Command output follows"}],
        }

        with mock.patch("core.views.run_location_live_command", return_value=result) as dispatcher:
            response = self.client.post(
                reverse("location-live-operation", args=[self.location.slug]),
                data=json.dumps({"command": "core_reload"}),
                content_type="application/json",
            )

        self.assertEqual(response.status_code, 200)
        dispatcher.assert_called_once_with(self.location, "core_reload", {})
        payload = json.loads(response.content.decode("utf-8"))
        self.assertEqual(payload["status"], "success")
        self.assertEqual(payload["result"]["command_id"], "cmd-1")
        audit = AuditLog.objects.get(action=AuditAction.LIVE_PBX_ACTION)
        self.assertEqual(audit.actor, self.operator)
        self.assertEqual(audit.outcome, AuditOutcome.SUCCESS)
        self.assertEqual(audit.target, "locations/live-hq/live/core_reload")
        self.assertEqual(audit.details["result"]["result"]["status"], "success")

    def test_supported_command_without_connected_agent_returns_failure_and_audits(self):
        self.client.force_login(self.admin)

        response = self.client.post(
            reverse("location-live-operation", args=[self.location.slug]),
            data=json.dumps({"command": "pjsip_reload"}),
            content_type="application/json",
        )

        self.assertEqual(response.status_code, 503)
        payload = json.loads(response.content.decode("utf-8"))
        self.assertEqual(payload["status"], "failure")
        self.assertIn("not connected", payload["result"]["error"])
        audit = AuditLog.objects.get(action=AuditAction.LIVE_PBX_ACTION)
        self.assertEqual(audit.actor, self.admin)
        self.assertEqual(audit.outcome, AuditOutcome.FAILURE)
        self.assertEqual(audit.details["command"], "pjsip_reload")
        self.assertEqual(audit.details["location_slug"], "live-hq")


class RecordingPlaybackViewTests(TestCase):
    def setUp(self):
        self.viewer = User.objects.create_user(username="recording-viewer", password="portal-pass")
        self.operator = User.objects.create_user(username="recording-operator", password="portal-pass")
        assign_role(self.viewer, PortalRole.VIEWER)
        assign_role(self.operator, PortalRole.OPERATOR)
        self.location = Location.objects.create(
            **location_model_data(name="Recording HQ", slug="recording-hq", recording_retention_days=30)
        )
        now = timezone.now()
        self.available_id = recording_id_for_path("available.wav")
        self.expired_id = recording_id_for_path("expired.wav")
        self.unavailable_id = recording_id_for_path("missing.wav")
        self.location.agent_telemetry = {
            "timestamp": now.isoformat(),
            "location_health": {"ami_connected": True},
            "phone_registrations": [],
            "trunk_status": [],
            "active_calls": [],
            "queue_status": [],
            "recent_calls": [],
            "recording_metadata": [
                {
                    "recording_id": self.available_id,
                    "relative_path": "available.wav",
                    "path": "/var/spool/asterisk/monitor/available.wav",
                    "filename": "available.wav",
                    "size_bytes": 4,
                    "modified_at": (now - timedelta(days=1)).isoformat(),
                    "retention_expires_at": (now + timedelta(days=29)).isoformat(),
                    "available": True,
                },
                {
                    "recording_id": self.expired_id,
                    "relative_path": "expired.wav",
                    "path": "/var/spool/asterisk/monitor/expired.wav",
                    "filename": "expired.wav",
                    "size_bytes": 4,
                    "modified_at": (now - timedelta(days=45)).isoformat(),
                    "retention_expires_at": (now - timedelta(days=15)).isoformat(),
                    "available": True,
                },
                {
                    "recording_id": self.unavailable_id,
                    "relative_path": "missing.wav",
                    "filename": "missing.wav",
                    "size_bytes": 0,
                    "available": False,
                    "status": "unavailable",
                },
            ],
            "telemetry_errors": [],
        }
        self.location.save(update_fields=["agent_telemetry", "updated_at"])

    def test_viewer_cannot_access_playback_and_denial_is_audited(self):
        self.client.force_login(self.viewer)

        with mock.patch("core.views.run_location_recording_playback") as dispatcher:
            response = self.client.get(
                reverse("location-recording-playback", args=[self.location.slug, self.available_id])
            )

        self.assertEqual(response.status_code, 403)
        dispatcher.assert_not_called()
        audit = AuditLog.objects.get(action=AuditAction.RECORDING_PLAYBACK)
        self.assertEqual(audit.actor, self.viewer)
        self.assertEqual(audit.outcome, AuditOutcome.DENIED)
        self.assertEqual(audit.target, f"locations/{self.location.slug}/recordings/{self.available_id}")
        self.assertEqual(audit.details["actor_username"], "recording-viewer")
        self.assertEqual(audit.details["status"], "denied")

    def test_operator_playback_dispatches_via_agent_and_audits_success(self):
        self.client.force_login(self.operator)
        result = {
            "type": "recording_file_result",
            "request_id": "req-1",
            "status": "success",
            "filename": "available.wav",
            "content_type": "audio/wav",
            "content_base64": base64.b64encode(b"RIFF").decode("ascii"),
        }

        with mock.patch("core.views.run_location_recording_playback", return_value=result) as dispatcher:
            response = self.client.get(
                reverse("location-recording-playback", args=[self.location.slug, self.available_id])
            )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response["Content-Type"], "audio/wav")
        self.assertEqual(response.content, b"RIFF")
        dispatcher.assert_called_once_with(
            self.location,
            "/var/spool/asterisk/monitor/available.wav",
            retention_days=30,
        )
        audit = AuditLog.objects.get(action=AuditAction.RECORDING_PLAYBACK)
        self.assertEqual(audit.actor, self.operator)
        self.assertEqual(audit.outcome, AuditOutcome.SUCCESS)
        self.assertEqual(audit.details["filename"], "available.wav")
        self.assertEqual(audit.details["status"], "success")
        self.assertNotIn("content_base64", json.dumps(audit.details))

    def test_expired_recording_returns_gone_without_agent_dispatch_and_is_audited(self):
        self.client.force_login(self.operator)

        with mock.patch("core.views.run_location_recording_playback") as dispatcher:
            response = self.client.get(reverse("location-recording-playback", args=[self.location.slug, self.expired_id]))

        self.assertEqual(response.status_code, 410)
        dispatcher.assert_not_called()
        audit = AuditLog.objects.get(action=AuditAction.RECORDING_PLAYBACK)
        self.assertEqual(audit.outcome, AuditOutcome.FAILURE)
        self.assertEqual(audit.details["status"], "expired")

    def test_unavailable_recording_returns_not_found_without_agent_dispatch_and_is_audited(self):
        self.client.force_login(self.operator)

        with mock.patch("core.views.run_location_recording_playback") as dispatcher:
            response = self.client.get(
                reverse("location-recording-playback", args=[self.location.slug, self.unavailable_id])
            )

        self.assertEqual(response.status_code, 404)
        dispatcher.assert_not_called()
        audit = AuditLog.objects.get(action=AuditAction.RECORDING_PLAYBACK)
        self.assertEqual(audit.outcome, AuditOutcome.FAILURE)
        self.assertEqual(audit.details["status"], "unavailable")
class AuthenticatedChannelControlAPITests(TestCase):
    def setUp(self):
        self.viewer = User.objects.create_user(username="channel-viewer", password="portal-pass")
        self.operator = User.objects.create_user(username="channel-operator", password="portal-pass")
        self.admin = User.objects.create_user(username="channel-admin", password="portal-pass")
        assign_role(self.viewer, PortalRole.VIEWER)
        assign_role(self.operator, PortalRole.OPERATOR)
        assign_role(self.admin, PortalRole.ADMIN)
        self.location = Location.objects.create(
            **location_model_data(name="Channel HQ", slug="channel-hq", agent_secret="agent-secret")
        )
        _viewer_key, self.viewer_secret = APIKey.issue(name="viewer channel api", user=self.viewer, created_by=self.admin)
        _operator_key, self.operator_secret = APIKey.issue(
            name="operator channel api",
            user=self.operator,
            created_by=self.admin,
        )

    def test_viewer_channel_control_is_denied_and_audited_like_live_controls(self):
        with mock.patch("core.views.run_location_live_command") as dispatcher:
            response = self._post_channel_control(self.viewer_secret, {"command": "core_reload"})

        self.assertEqual(response.status_code, 403)
        dispatcher.assert_not_called()
        payload = response.json()
        self.assertEqual(payload["status"], "denied")
        audit = AuditLog.objects.get(action=AuditAction.LIVE_PBX_ACTION)
        self.assertEqual(audit.actor, self.viewer)
        self.assertEqual(audit.outcome, AuditOutcome.DENIED)
        self.assertEqual(audit.details["command"], "core_reload")
        self.assertEqual(audit.details["actor_username"], "channel-viewer")
        self.assertEqual(audit.details["location_slug"], "channel-hq")

    def test_operator_channel_control_dispatches_and_audits_success(self):
        result = {
            "type": "live_command_result",
            "command_id": "cmd-channel",
            "command": "core_reload",
            "status": "success",
            "ami_action": "Command",
            "ami_response": [{"Response": "Success", "Message": "Command output follows"}],
        }

        with mock.patch("core.views.run_location_live_command", return_value=result) as dispatcher:
            response = self._post_channel_control(self.operator_secret, {"command": "core_reload"})

        self.assertEqual(response.status_code, 200)
        dispatcher.assert_called_once_with(self.location, "core_reload", {})
        payload = response.json()
        self.assertEqual(payload["status"], "success")
        self.assertEqual(payload["result"]["command_id"], "cmd-channel")
        audit = AuditLog.objects.get(action=AuditAction.LIVE_PBX_ACTION)
        self.assertEqual(audit.actor, self.operator)
        self.assertEqual(audit.outcome, AuditOutcome.SUCCESS)
        self.assertEqual(audit.target, "locations/channel-hq/live/core_reload")

    def _post_channel_control(self, secret, payload):
        return self.client.post(
            reverse("api-channel-control", args=[self.location.slug]),
            data=json.dumps(payload),
            content_type="application/json",
            HTTP_AUTHORIZATION=f"Bearer {secret}",
        )


class ProviderTrunkValidationTests(TestCase):
    def setUp(self):
        self.location = Location.objects.create(**location_model_data(name="HQ", slug="hq"))
        self.sip_provider = Provider.objects.create(
            name="Carrier SIP",
            slug="carrier-sip",
            provider_type=Provider.ProviderType.SIP,
        )
        self.iax_provider = Provider.objects.create(
            name="Carrier IAX",
            slug="carrier-iax",
            provider_type=Provider.ProviderType.IAX2,
        )

    def test_trunk_accepts_plaintext_credentials(self):
        trunk = Trunk(
            location=self.location,
            provider=self.sip_provider,
            name="Primary SIP",
            trunk_type=Trunk.TrunkType.SIP,
            host="sip.provider.example.test",
            username="branch-user",
            password="branch-secret",
            is_emergency_capable=True,
        )

        trunk.full_clean()
        trunk.save()

        self.assertEqual(Trunk.objects.get(pk=trunk.pk).password, "branch-secret")

    def test_trunk_provider_type_must_match(self):
        trunk = Trunk(
            location=self.location,
            provider=self.iax_provider,
            name="Mismatched",
            trunk_type=Trunk.TrunkType.SIP,
        )

        with self.assertRaises(ValidationError) as context:
            trunk.full_clean()

        self.assertIn("trunk_type", context.exception.message_dict)

    def test_provider_credential_warnings_do_not_block_non_emergency_export(self):
        Trunk.objects.create(
            location=self.location,
            provider=self.sip_provider,
            name="Missing Secret",
            trunk_type=Trunk.TrunkType.SIP,
            host="sip.provider.example.test",
            username="branch-user",
            password="",
        )

        config = build_location_config(self.location)

        self.assertEqual(config["provider_trunks"][0]["credentials"]["password"], "")
        self.assertEqual(config["routing_validation"]["warnings"][0]["code"], "provider_trunk_missing_credentials")
        self.assertEqual(config["routing_validation"]["warnings"][0]["missing"], ["password"])
        self.assertEqual(config["routing_validation"]["errors"], [])

    def test_emergency_validation_flags_incomplete_emergency_trunk(self):
        trunk = Trunk.objects.create(
            location=self.location,
            provider=self.sip_provider,
            name="Emergency SIP",
            trunk_type=Trunk.TrunkType.SIP,
            host="sip.provider.example.test",
            username="branch-user",
            password="",
            is_emergency_capable=True,
        )
        route = OutboundRoute.objects.create(
            location=self.location,
            name="Emergency",
            dial_pattern="911",
            priority=1,
            is_emergency_route=True,
            caller_id_source=OutboundRoute.CallerIdSource.EMERGENCY,
        )
        OutboundRouteTrunk.objects.create(outbound_route=route, trunk=trunk, priority=1)

        validation = validate_location_routing(self.location, require_emergency=True)

        self.assertIn("provider_trunk_missing_credentials", {warning["code"] for warning in validation["warnings"]})
        self.assertIn("emergency_trunk_missing_credentials", {warning["code"] for warning in validation["warnings"]})
        self.assertEqual(validation["errors"], [])


class OutboundRouteCallerIdTests(TestCase):
    def setUp(self):
        self.location = Location.objects.create(**location_model_data(name="HQ", slug="hq"))
        self.extension = Extension.objects.create(
            location=self.location,
            number="3000",
            display_name="HQ Desk",
            caller_id_number="+15551203111",
        )
        self.did = DID.objects.create(
            location=self.location,
            number="+15551203001",
            default_destination=did_default_destination(self.location, self.extension),
            direct_extension=self.extension,
        )
        self.provider = Provider.objects.create(
            name="Carrier SIP",
            slug="carrier-sip",
            provider_type=Provider.ProviderType.SIP,
        )
        self.primary_trunk = Trunk.objects.create(
            location=self.location,
            provider=self.provider,
            name="Primary SIP",
            trunk_type=Trunk.TrunkType.SIP,
            host="sip.primary.example.test",
            username="primary",
            password="primary-secret",
            is_emergency_capable=True,
        )
        self.backup_trunk = Trunk.objects.create(
            location=self.location,
            provider=self.provider,
            name="Backup SIP",
            trunk_type=Trunk.TrunkType.SIP,
            host="sip.backup.example.test",
            username="backup",
            password="backup-secret",
            is_emergency_capable=True,
        )

    def test_extension_location_default_and_emergency_caller_id_selection(self):
        extension_route = OutboundRoute.objects.create(
            location=self.location,
            name="Extension DID",
            dial_pattern="NXXNXXXXXX",
            priority=1,
            caller_id_source=OutboundRoute.CallerIdSource.EXTENSION_DID,
        )
        default_route = OutboundRoute.objects.create(
            location=self.location,
            name="Location Default",
            dial_pattern="1NXXNXXXXXX",
            priority=2,
            caller_id_source=OutboundRoute.CallerIdSource.LOCATION_DEFAULT,
        )
        emergency_route = OutboundRoute.objects.create(
            location=self.location,
            name="Emergency",
            dial_pattern="911",
            priority=3,
            is_emergency_route=True,
            caller_id_source=OutboundRoute.CallerIdSource.EMERGENCY,
        )

        self.assertEqual(select_route_caller_id(extension_route, self.extension), self.did.number)
        self.assertEqual(select_route_caller_id(default_route, self.extension), self.location.default_did)
        self.assertEqual(select_route_caller_id(emergency_route, self.extension), self.location.emergency_caller_id)

    def test_route_export_orders_provider_fallback(self):
        route = OutboundRoute.objects.create(
            location=self.location,
            name="Local",
            dial_pattern="NXXNXXXXXX",
            priority=1,
            caller_id_source=OutboundRoute.CallerIdSource.LOCATION_DEFAULT,
        )
        OutboundRouteTrunk.objects.create(outbound_route=route, trunk=self.primary_trunk, priority=2)
        OutboundRouteTrunk.objects.create(outbound_route=route, trunk=self.backup_trunk, priority=1)

        route_payload = build_location_config(self.location)["outbound_routes"][0]

        self.assertEqual([trunk["name"] for trunk in route_payload["trunks"]], ["Backup SIP", "Primary SIP"])
        self.assertEqual(route_payload["caller_id"]["number"], self.location.default_did)

    def test_extension_did_route_generates_runtime_caller_id_lookup(self):
        route = OutboundRoute.objects.create(
            location=self.location,
            name="Extension DID",
            dial_pattern="NXXNXXXXXX",
            priority=1,
            caller_id_source=OutboundRoute.CallerIdSource.EXTENSION_DID,
        )
        OutboundRouteTrunk.objects.create(outbound_route=route, trunk=self.primary_trunk, priority=1)

        extensions_conf = build_asterisk_config_files(self.location)["extensions.conf"]

        self.assertIn("same => n,Set(ROUTE_CALLER_ID=+15551203000)", extensions_conf)
        self.assertIn("same => n,Gosub(route-caller-id-extension-did,s,1(${CALLERID(num)}))", extensions_conf)
        self.assertIn("[route-caller-id-extension-did]", extensions_conf)
        self.assertIn('same => n,GotoIf($["${ARG1}"="3000"]?set-3000)', extensions_conf)
        self.assertIn("same => n(set-3000),Set(ROUTE_CALLER_ID=+15551203001)", extensions_conf)
        self.assertIn("same => n,Set(CALLERID(num)=${ROUTE_CALLER_ID})", extensions_conf)

    def test_static_provider_trunk_generates_explicit_pjsip_acl(self):
        Trunk.objects.create(
            location=self.location,
            provider=self.provider,
            name="Static Provider SIP",
            trunk_type=Trunk.TrunkType.SIP,
            host="203.0.113.10",
            username="static",
            password="static-secret",
        )

        pjsip_conf = build_asterisk_config_files(self.location)["pjsip.conf"]

        self.assertIn("[trunk-static-provider-sip]", pjsip_conf)
        self.assertIn("from_domain=203.0.113.10", pjsip_conf)
        self.assertIn("deny=0.0.0.0/0.0.0.0", pjsip_conf)
        self.assertIn("permit=203.0.113.10/255.255.255.255", pjsip_conf)

    def test_emergency_route_rejects_non_emergency_caller_id_source(self):
        route = OutboundRoute(
            location=self.location,
            name="Bad Emergency",
            dial_pattern="911",
            priority=1,
            is_emergency_route=True,
            caller_id_source=OutboundRoute.CallerIdSource.LOCATION_DEFAULT,
        )

        with self.assertRaises(ValidationError) as context:
            route.full_clean()

        self.assertIn("caller_id_source", context.exception.message_dict)

    def test_emergency_route_trunk_rejects_non_emergency_capable_trunk(self):
        normal_trunk = Trunk.objects.create(
            location=self.location,
            provider=self.provider,
            name="Normal SIP",
            trunk_type=Trunk.TrunkType.SIP,
            host="sip.normal.example.test",
            username="normal",
            password="normal-secret",
            is_emergency_capable=False,
        )
        route = OutboundRoute.objects.create(
            location=self.location,
            name="Emergency",
            dial_pattern="911",
            priority=1,
            is_emergency_route=True,
            caller_id_source=OutboundRoute.CallerIdSource.EMERGENCY,
        )
        route_trunk = OutboundRouteTrunk(outbound_route=route, trunk=normal_trunk, priority=1)

        with self.assertRaises(ValidationError) as context:
            route_trunk.full_clean()

        self.assertIn("trunk", context.exception.message_dict)


class TrunkManagementViewTests(TestCase):
    def setUp(self):
        self.viewer = User.objects.create_user(username="trunk-viewer", password="portal-pass")
        self.editor = User.objects.create_user(username="trunk-editor", password="portal-pass")
        assign_role(self.viewer, PortalRole.VIEWER)
        assign_role(self.editor, PortalRole.EDITOR)
        self.location = Location.objects.create(**location_model_data(name="HQ", slug="hq"))
        self.provider = Provider.objects.create(
            name="Carrier SIP",
            slug="carrier-sip",
            provider_type=Provider.ProviderType.SIP,
        )

    def test_trunk_list_route_shows_management_surface(self):
        Trunk.objects.create(
            location=self.location,
            provider=self.provider,
            name="Primary SIP",
            trunk_type=Trunk.TrunkType.SIP,
            host="sip.provider.example.test",
            username="branch-user",
            password="branch-secret",
            is_emergency_capable=True,
        )
        self.client.force_login(self.viewer)

        response = self.client.get(reverse("trunks"))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'data-area="trunks"')
        self.assertContains(response, "Provider Trunks")
        self.assertContains(response, "Primary SIP")
        self.assertContains(response, "Password configured")

    def test_viewer_cannot_create_trunk(self):
        self.client.force_login(self.viewer)

        response = self.client.get(reverse("trunk-create"))

        self.assertEqual(response.status_code, 403)

    def test_editor_can_create_provider_and_trunk_with_plaintext_credentials(self):
        self.client.force_login(self.editor)

        provider_response = self.client.post(
            reverse("provider-create"),
            provider_form_data(name="Backup IAX", slug="backup-iax", provider_type=Provider.ProviderType.IAX2),
        )
        provider = Provider.objects.get(slug="backup-iax")
        trunk_response = self.client.post(
            reverse("trunk-create"),
            trunk_form_data(
                self.location,
                provider,
                name="Backup IAX",
                trunk_type=Trunk.TrunkType.IAX2,
                host="iax.provider.example.test",
                username="iax-user",
                password="iax-secret",
                is_emergency_capable="on",
            ),
        )

        self.assertEqual(provider_response.status_code, 302)
        self.assertEqual(trunk_response.status_code, 302)
        trunk = Trunk.objects.get(name="Backup IAX")
        self.assertEqual(trunk.password, "iax-secret")
        self.assertTrue(trunk.is_emergency_capable)


class OutboundRouteManagementViewTests(TestCase):
    def setUp(self):
        self.viewer = User.objects.create_user(username="route-viewer", password="portal-pass")
        self.editor = User.objects.create_user(username="route-editor", password="portal-pass")
        assign_role(self.viewer, PortalRole.VIEWER)
        assign_role(self.editor, PortalRole.EDITOR)
        self.location = Location.objects.create(**location_model_data(name="HQ", slug="hq"))
        self.provider = Provider.objects.create(
            name="Carrier SIP",
            slug="carrier-sip",
            provider_type=Provider.ProviderType.SIP,
        )
        self.primary_trunk = Trunk.objects.create(
            location=self.location,
            provider=self.provider,
            name="Primary SIP",
            trunk_type=Trunk.TrunkType.SIP,
            host="sip.primary.example.test",
            username="primary",
            password="primary-secret",
            is_emergency_capable=True,
        )
        self.backup_trunk = Trunk.objects.create(
            location=self.location,
            provider=self.provider,
            name="Backup SIP",
            trunk_type=Trunk.TrunkType.SIP,
            host="sip.backup.example.test",
            username="backup",
            password="backup-secret",
            is_emergency_capable=True,
        )

    def test_dial_plan_list_route_shows_fallback_order(self):
        route = OutboundRoute.objects.create(
            location=self.location,
            name="Local",
            dial_pattern="NXXNXXXXXX",
            priority=1,
            caller_id_source=OutboundRoute.CallerIdSource.LOCATION_DEFAULT,
        )
        OutboundRouteTrunk.objects.create(outbound_route=route, trunk=self.backup_trunk, priority=1)
        OutboundRouteTrunk.objects.create(outbound_route=route, trunk=self.primary_trunk, priority=2)
        self.client.force_login(self.viewer)

        response = self.client.get(reverse("dial-plan"))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'data-area="dial-plan"')
        self.assertContains(response, "Outbound Routes")
        self.assertContains(response, "1: Backup SIP")
        self.assertContains(response, "2: Primary SIP")
        self.assertContains(response, "Location default DID")

    def test_editor_can_create_outbound_route_with_provider_fallback_order(self):
        self.client.force_login(self.editor)
        post_data = outbound_route_form_data(self.location)
        post_data.update(
            route_trunk_formset_data(
                trunk_rows=[
                    {"priority": 2, "trunk": self.primary_trunk},
                    {"priority": 1, "trunk": self.backup_trunk},
                ],
            )
        )

        response = self.client.post(reverse("outbound-route-create"), post_data)

        self.assertEqual(response.status_code, 302)
        route = OutboundRoute.objects.get(name="Local")
        self.assertEqual(
            list(route.route_trunks.order_by("priority").values_list("priority", "trunk__name")),
            [(1, "Backup SIP"), (2, "Primary SIP")],
        )

    def test_emergency_route_form_rejects_non_emergency_trunk(self):
        normal_trunk = Trunk.objects.create(
            location=self.location,
            provider=self.provider,
            name="Normal SIP",
            trunk_type=Trunk.TrunkType.SIP,
            host="sip.normal.example.test",
            username="normal",
            password="normal-secret",
            is_emergency_capable=False,
        )
        self.client.force_login(self.editor)
        post_data = outbound_route_form_data(
            self.location,
            name="Emergency",
            dial_pattern="911",
            is_emergency_route="on",
            caller_id_source=OutboundRoute.CallerIdSource.EMERGENCY,
        )
        post_data.update(route_trunk_formset_data(trunk_rows=[{"priority": 1, "trunk": normal_trunk}]))

        response = self.client.post(reverse("outbound-route-create"), post_data)

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Emergency routes can only use emergency-capable trunks.")
        self.assertFalse(OutboundRoute.objects.filter(name="Emergency").exists())


class ExtensionFormValidationTests(TestCase):
    def setUp(self):
        self.hq = Location.objects.create(**location_model_data(name="HQ", slug="hq"))
        self.warehouse = Location.objects.create(**location_model_data(name="Warehouse", slug="warehouse"))

    def test_duplicate_extension_number_gets_form_error(self):
        Extension.objects.create(location=self.hq, number="3000", display_name="HQ Desk")

        form = ExtensionForm(
            data=extension_form_data(self.warehouse, number="3000"),
            can_disable_911=True,
        )

        self.assertFalse(form.is_valid())
        self.assertIn("Extension number already exists.", form.errors["number"])

    def test_non_admin_form_rejects_911_disable(self):
        extension = Extension.objects.create(
            location=self.hq,
            number="3000",
            display_name="HQ Desk",
            emergency_calling_enabled=True,
        )

        form = ExtensionForm(
            data=extension_form_data(self.hq, emergency_calling_enabled=""),
            instance=extension,
            can_disable_911=False,
        )

        self.assertFalse(form.is_valid())
        self.assertTrue(form.denied_911_disable)
        self.assertIn("Only admins can disable 911 calling for an extension.", form.errors["emergency_calling_enabled"])


class ExtensionManagementViewTests(TestCase):
    def setUp(self):
        self.viewer = User.objects.create_user(username="extension-viewer", password="portal-pass")
        self.editor = User.objects.create_user(username="extension-editor", password="portal-pass")
        self.admin = User.objects.create_user(username="extension-admin", password="portal-pass")
        assign_role(self.viewer, PortalRole.VIEWER)
        assign_role(self.editor, PortalRole.EDITOR)
        assign_role(self.admin, PortalRole.ADMIN)
        self.location = Location.objects.create(**location_model_data(name="HQ", slug="hq"))
        self.extension = Extension.objects.create(
            location=self.location,
            number="3000",
            display_name="HQ Desk",
            sip_username="3000",
            emergency_calling_enabled=True,
        )

    def test_extension_list_route_shows_management_surface(self):
        self.client.force_login(self.viewer)

        response = self.client.get(reverse("extensions"))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'data-area="extensions"')
        self.assertContains(response, "Extension Management")
        self.assertContains(response, "HQ Desk")
        self.assertContains(response, "CSV Template")

    def test_viewer_cannot_create_extension(self):
        self.client.force_login(self.viewer)

        response = self.client.get(reverse("extension-create"))

        self.assertEqual(response.status_code, 403)

    def test_admin_can_create_extension_with_memberships(self):
        fallback = Extension.objects.create(location=self.location, number="3999", display_name="Fallback")
        did = DID.objects.create(
            location=self.location,
            number="+15551203000",
            default_destination=did_default_destination(self.location, fallback),
        )
        ring_group = RingGroup.objects.create(location=self.location, name="Support Ring")
        queue = CallQueue.objects.create(location=self.location, name="Support Queue")
        paging_group = PagingGroup.objects.create(location=self.location, name="HQ Page", page_code="7000")
        self.client.force_login(self.admin)

        response = self.client.post(
            reverse("extension-create"),
            extension_form_data(
                self.location,
                number="3001",
                direct_dids=[str(did.id)],
                ring_groups=[str(ring_group.id)],
                queues=[str(queue.id)],
                paging_groups=[str(paging_group.id)],
            ),
        )

        self.assertEqual(response.status_code, 302)
        extension = Extension.objects.get(number="3001")
        did.refresh_from_db()
        self.assertEqual(did.direct_extension, extension)
        self.assertTrue(RingGroupMember.objects.filter(ring_group=ring_group, extension=extension).exists())
        self.assertTrue(QueueMember.objects.filter(queue=queue, extension=extension).exists())
        self.assertTrue(PagingGroupMember.objects.filter(paging_group=paging_group, extension=extension).exists())

    def test_editor_cannot_disable_911_and_denial_is_audited(self):
        self.client.force_login(self.editor)

        response = self.client.post(
            reverse("extension-edit", args=[self.extension.number]),
            extension_form_data(self.location, number=self.extension.number, emergency_calling_enabled=""),
        )

        self.assertEqual(response.status_code, 200)
        self.extension.refresh_from_db()
        self.assertTrue(self.extension.emergency_calling_enabled)
        audit_log = AuditLog.objects.get(target="extensions/3000/911")
        self.assertEqual(audit_log.outcome, AuditOutcome.DENIED)

    def test_admin_can_disable_911_and_success_is_audited(self):
        self.client.force_login(self.admin)

        response = self.client.post(
            reverse("extension-edit", args=[self.extension.number]),
            extension_form_data(self.location, number=self.extension.number, emergency_calling_enabled=""),
        )

        self.assertEqual(response.status_code, 302)
        self.extension.refresh_from_db()
        self.assertFalse(self.extension.emergency_calling_enabled)
        audit_log = AuditLog.objects.get(target="extensions/3000/911")
        self.assertEqual(audit_log.outcome, AuditOutcome.SUCCESS)


class ExtensionCSVTests(TestCase):
    def setUp(self):
        self.admin = User.objects.create_user(username="csv-admin", password="portal-pass")
        assign_role(self.admin, PortalRole.ADMIN)
        self.location = Location.objects.create(**location_model_data(name="HQ", slug="hq"))
        self.extension = Extension.objects.create(
            location=self.location,
            number="3000",
            display_name="HQ Desk",
            email="desk@example.test",
            sip_username="3000",
            sip_password="sip-secret",
            voicemail_enabled=True,
            voicemail_pin="1234",
            caller_id_name="HQ Desk",
            caller_id_number="+15551203000",
            recording_policy=Extension.RecordingPolicy.ALWAYS,
            emergency_calling_enabled=True,
        )
        self.did = DID.objects.create(
            location=self.location,
            number="+15551203000",
            default_destination=did_default_destination(self.location, self.extension),
            direct_extension=self.extension,
        )
        self.ring_group = RingGroup.objects.create(location=self.location, name="Support Ring")
        self.queue = CallQueue.objects.create(location=self.location, name="Support Queue")
        self.paging_group = PagingGroup.objects.create(location=self.location, name="HQ Page", page_code="7000")
        sync_extension_relationships(
            self.extension,
            direct_dids=[self.did],
            ring_groups=[self.ring_group],
            queues=[self.queue],
            paging_groups=[self.paging_group],
        )

    def test_extension_csv_template_contains_membership_headers(self):
        template = extension_template_csv()

        self.assertIn("direct_dids", template)
        self.assertIn("ring_groups", template)
        self.assertIn("queues", template)
        self.assertIn("paging_groups", template)

    def test_extension_csv_export_import_round_trips_attributes_and_memberships(self):
        exported_csv = export_extensions_csv(Extension.objects.filter(number="3000"))
        self.extension.display_name = "Mutated"
        self.extension.voicemail_enabled = False
        self.extension.recording_policy = Extension.RecordingPolicy.NEVER
        self.extension.save()
        sync_extension_relationships(
            self.extension,
            direct_dids=[],
            ring_groups=[],
            queues=[],
            paging_groups=[],
        )

        imported_count = import_extensions_csv(exported_csv, actor=self.admin, can_disable_911=True)

        self.assertEqual(imported_count, 1)
        self.extension.refresh_from_db()
        self.did.refresh_from_db()
        self.assertEqual(self.extension.display_name, "HQ Desk")
        self.assertTrue(self.extension.voicemail_enabled)
        self.assertEqual(self.extension.recording_policy, Extension.RecordingPolicy.ALWAYS)
        self.assertEqual(self.did.direct_extension, self.extension)
        self.assertTrue(RingGroupMember.objects.filter(ring_group=self.ring_group, extension=self.extension).exists())
        self.assertTrue(QueueMember.objects.filter(queue=self.queue, extension=self.extension).exists())
        self.assertTrue(PagingGroupMember.objects.filter(paging_group=self.paging_group, extension=self.extension).exists())

    def test_csv_import_rejects_duplicate_numbers_in_file(self):
        output = StringIO()
        writer = csv.DictWriter(
            output,
            fieldnames=[
                "location_slug",
                "number",
                "display_name",
                "email",
                "sip_username",
                "sip_password",
                "direct_dids",
                "voicemail_enabled",
                "voicemail_pin",
                "caller_id_name",
                "caller_id_number",
                "recording_policy",
                "emergency_calling_enabled",
                "is_active",
                "ring_groups",
                "queues",
                "paging_groups",
            ],
        )
        writer.writeheader()
        row = {
            "location_slug": self.location.slug,
            "number": "3002",
            "display_name": "Duplicate",
            "recording_policy": Extension.RecordingPolicy.NEVER,
            "emergency_calling_enabled": "true",
            "is_active": "true",
        }
        writer.writerow(row)
        writer.writerow(row)

        with self.assertRaises(ExtensionCSVError) as context:
            import_extensions_csv(output.getvalue(), actor=self.admin, can_disable_911=True)

        self.assertIn("duplicate extension number 3002 in CSV", context.exception.errors[0])


class VoicemailRecordingConfigTests(TestCase):
    def test_defaults_allow_phone_voicemail_without_smtp(self):
        location_data = location_model_data(
            smtp_host="",
            smtp_from_email="",
            smtp_username="",
            smtp_password="",
        )
        location_data.pop("recording_retention_days")
        location = Location(**location_data)
        location.full_clean()
        location.save()

        self.assertEqual(location.recording_retention_days, 90)
        self.assertIsNone(build_location_config(location)["voicemail"]["smtp"])

    def test_recording_policy_defaults_to_off_for_extension_queue_and_route(self):
        location = Location.objects.create(**location_model_data(name="HQ", slug="hq"))
        extension = Extension.objects.create(location=location, number="3000", display_name="HQ Desk")
        queue = CallQueue.objects.create(location=location, name="Support Queue")
        route = OutboundRoute.objects.create(
            location=location,
            name="Local",
            dial_pattern="NXXNXXXXXX",
            priority=1,
        )

        self.assertEqual(extension.recording_policy, Extension.RecordingPolicy.NEVER)
        self.assertEqual(queue.recording_policy, CallQueue.RecordingPolicy.NEVER)
        self.assertEqual(route.recording_policy, OutboundRoute.RecordingPolicy.NEVER)

    def test_recording_policy_and_retention_validation(self):
        location = Location.objects.create(**location_model_data(name="HQ", slug="hq"))
        invalid_retention = Location(**location_model_data(name="Bad Retention", slug="bad-retention", recording_retention_days=0))
        queue = CallQueue(location=location, name="Support Queue", recording_policy="invalid")
        route = OutboundRoute(
            location=location,
            name="Local",
            dial_pattern="NXXNXXXXXX",
            priority=1,
            recording_policy="invalid",
        )

        with self.assertRaises(ValidationError) as retention_context:
            invalid_retention.full_clean()
        self.assertIn("recording_retention_days", retention_context.exception.message_dict)

        with self.assertRaises(ValidationError) as queue_context:
            queue.full_clean()
        self.assertIn("recording_policy", queue_context.exception.message_dict)

        with self.assertRaises(ValidationError) as route_context:
            route.full_clean()
        self.assertIn("recording_policy", route_context.exception.message_dict)

    def test_config_export_exposes_voicemail_recording_and_retention(self):
        location = Location.objects.create(
            **location_model_data(
                name="No SMTP",
                slug="no-smtp",
                recording_retention_days=90,
                smtp_host="",
                smtp_from_email="",
                smtp_username="",
                smtp_password="",
            )
        )
        Extension.objects.create(
            location=location,
            number="3000",
            display_name="HQ Desk",
            email="desk@example.test",
            voicemail_enabled=True,
            voicemail_pin="4321",
            recording_policy=Extension.RecordingPolicy.ALWAYS,
        )
        CallQueue.objects.create(
            location=location,
            name="Support Queue",
            recording_policy=CallQueue.RecordingPolicy.ON_DEMAND,
        )
        OutboundRoute.objects.create(
            location=location,
            name="Local",
            dial_pattern="NXXNXXXXXX",
            priority=1,
            recording_policy=OutboundRoute.RecordingPolicy.NEVER,
        )

        config = build_location_config(location)

        self.assertIsNone(config["voicemail"]["smtp"])
        self.assertEqual(
            config["voicemail"]["mailboxes"][0],
            {
                "number": "3000",
                "name": "HQ Desk",
                "enabled": True,
                "pin": "4321",
                "email_enabled": False,
                "email": "",
            },
        )
        self.assertEqual(config["recording"]["retention_days"], 90)
        self.assertEqual(config["helper_scripts"]["recording_retention_days"], 90)
        self.assertEqual(config["recording"]["extensions"][0]["policy"], Extension.RecordingPolicy.ALWAYS)
        self.assertEqual(config["recording"]["queues"][0]["policy"], CallQueue.RecordingPolicy.ON_DEMAND)
        self.assertEqual(config["recording"]["routes"][0]["policy"], OutboundRoute.RecordingPolicy.NEVER)

        location.smtp_host = "smtp-hq.example.test"
        location.smtp_from_email = "pbx-hq@example.test"
        location.save()
        config = build_location_config(location)

        self.assertEqual(config["voicemail"]["smtp"]["host"], "smtp-hq.example.test")
        self.assertTrue(config["voicemail"]["mailboxes"][0]["email_enabled"])
        self.assertEqual(config["voicemail"]["mailboxes"][0]["email"], "desk@example.test")

    def test_export_command_outputs_helper_script_snapshot(self):
        location = Location.objects.create(
            **location_model_data(name="Command HQ", slug="command-hq", recording_retention_days=90)
        )
        Extension.objects.create(location=location, number="3000", display_name="HQ Desk")
        add_emergency_route(location)
        output = StringIO()

        call_command("export_pbx_config", location.slug, stdout=output)

        payload = json.loads(output.getvalue())
        self.assertEqual(payload["location"]["slug"], "command-hq")
        self.assertEqual(payload["helper_scripts"]["recording_retention_days"], 90)
        self.assertEqual(payload["voicemail"]["mailboxes"][0]["number"], "3000")


class AsteriskConfigGenerationTests(TestCase):
    maxDiff = None

    def setUp(self):
        self.hq = Location.objects.create(
            **location_model_data(
                name="HQ",
                slug="hq",
                lan_subnet="10.30.0.0/24",
                pbx_lan_ip="10.30.0.10",
                pbx_warp_ip="100.64.30.10",
                sip_bind_ip="10.30.0.10",
                iax_bind_ip="10.30.0.10",
                default_did="+15551203000",
                emergency_caller_id="+15551203999",
                recording_retention_days=45,
                ami_username="ami-hq",
                ami_secret="ami-secret",
                agent_secret="hq-agent-secret",
            )
        )
        self.warehouse = Location.objects.create(
            **location_model_data(
                name="Warehouse",
                slug="warehouse",
                lan_subnet="10.40.0.0/24",
                pbx_lan_ip="10.40.0.10",
                pbx_warp_ip="100.64.40.10",
                sip_bind_ip="10.40.0.10",
                iax_bind_ip="10.40.0.10",
                default_did="+15551204000",
                emergency_caller_id="+15551204999",
                ami_username="ami-warehouse",
                agent_secret="warehouse-agent-secret",
            )
        )
        self.reception = Extension.objects.create(
            location=self.hq,
            number="3000",
            display_name="HQ Reception",
            email="reception@example.test",
            sip_username="3000",
            sip_password="sip-secret-3000",
            voicemail_pin="1234",
            caller_id_name="HQ Reception",
            caller_id_number="+15551203000",
            emergency_calling_enabled=True,
            recording_policy=Extension.RecordingPolicy.ALWAYS,
        )
        self.disabled_extension = Extension.objects.create(
            location=self.hq,
            number="3001",
            display_name="Lab Phone",
            sip_username="3001",
            sip_password="sip-secret-3001",
            voicemail_enabled=False,
            emergency_calling_enabled=False,
        )
        self.remote_extension = Extension.objects.create(
            location=self.warehouse,
            number="4000",
            display_name="Warehouse Desk",
            sip_username="4000",
            sip_password="sip-secret-4000",
            voicemail_pin="4321",
        )
        self.provider = Provider.objects.create(
            name="Example SIP",
            slug="example-sip",
            provider_type=Provider.ProviderType.SIP,
        )
        self.primary_trunk = Trunk.objects.create(
            location=self.hq,
            provider=self.provider,
            name="Primary SIP",
            trunk_type=Trunk.TrunkType.SIP,
            host="sip.primary.example.test",
            username="primary-user",
            password="primary-secret",
            is_emergency_capable=True,
        )
        local_route = OutboundRoute.objects.create(
            location=self.hq,
            name="Local Outbound",
            dial_pattern="NXXNXXXXXX",
            priority=1,
            caller_id_source=OutboundRoute.CallerIdSource.LOCATION_DEFAULT,
        )
        OutboundRouteTrunk.objects.create(outbound_route=local_route, trunk=self.primary_trunk, priority=1)
        add_emergency_route(self.hq)
        self.reception_destination = did_default_destination(self.hq, self.reception)
        DID.objects.create(
            location=self.hq,
            number="+15551203000",
            provider=self.provider,
            trunk=self.primary_trunk,
            direct_extension=self.reception,
            default_destination=self.reception_destination,
            label="HQ main",
        )
        queue = CallQueue.objects.create(
            location=self.hq,
            name="Support Queue",
            strategy=CallQueue.Strategy.ROUND_ROBIN,
            timeout_seconds=45,
            retry_seconds=7,
            music_on_hold="default",
            overflow_destination=self.reception_destination,
            recording_policy=CallQueue.RecordingPolicy.ON_DEMAND,
        )
        QueueMember.objects.create(queue=queue, extension=self.reception, penalty=0)
        paging_group = PagingGroup.objects.create(location=self.hq, name="HQ Page", page_code="7100")
        PagingGroupMember.objects.create(paging_group=paging_group, extension=self.reception)
        FeatureCode.objects.create(
            location=self.hq,
            code="*98",
            name="Voicemail",
            feature_type=FeatureCode.FeatureType.VOICEMAIL_MAIN,
            destination=self.reception_destination,
        )

    def test_generates_required_asterisk_files_for_location_export(self):
        configs = build_location_config(self.hq)["asterisk_configs"]

        self.assertEqual(set(configs), set(ASTERISK_CONFIG_FILENAMES))
        self.assertIn("rtpstart=10000", configs["rtp.conf"])
        self.assertIn("rtpend=20000", configs["rtp.conf"])
        self.assertIn("[transport-tcp]", configs["pjsip.conf"])
        self.assertIn("protocol=tcp", configs["pjsip.conf"])
        self.assertIn("hook=/usr/local/sbin/pbx-recording-retention", configs["retention.conf"])

    def test_export_archive_bundles_recording_retention_script(self):
        archive = build_config_export_archive(
            self.hq,
            version_number=1,
            exported_at=timezone.now(),
            require_emergency=True,
        )

        with zipfile.ZipFile(BytesIO(archive.archive_bytes)) as zip_file:
            script = zip_file.read("scripts/pbx-recording-retention").decode("utf-8")
            docker_compose = zip_file.read("docker-compose.yml").decode("utf-8")

        self.assertIn("#!/bin/sh", script)
        self.assertIn("find \"$RECORDING_ROOT\"", script)
        self.assertIn(
            "./scripts/pbx-recording-retention:/usr/local/sbin/pbx-recording-retention:ro",
            docker_compose,
        )

    def test_pjsip_iax_dialplan_queue_and_voicemail_golden_files(self):
        configs = build_asterisk_config_files(self.hq)

        for filename in ("pjsip.conf", "rtp.conf", "iax.conf", "extensions.conf", "queues.conf", "voicemail.conf"):
            with self.subTest(filename=filename):
                self.assertEqual(configs[filename], self._golden(filename))

    def test_route_generation_choices_use_iax2_for_remote_and_block_disabled_emergency(self):
        choices = build_route_generation_choices(self.hq)

        self.assertEqual(
            choices["remote_extensions"],
            [
                {
                    "number": "4000",
                    "owner_location": "warehouse",
                    "transport": "iax2",
                    "peer": "warehouse",
                    "target": "IAX2/warehouse/${EXTEN}",
                }
            ],
        )
        self.assertEqual(
            choices["emergency_blocks"],
            [
                {
                    "extension": "3001",
                    "patterns": ["911"],
                }
            ],
        )

    def test_export_command_writes_spec_export_structure_to_output_dir_and_zip(self):
        output = StringIO()
        with tempfile.TemporaryDirectory() as output_dir:
            zip_path = Path(output_dir) / "hq-config.zip"
            expanded_dir = Path(output_dir) / "expanded"
            call_command(
                "export_pbx_config",
                self.hq.slug,
                "--output-dir",
                expanded_dir,
                "--zip-output",
                zip_path,
                stdout=output,
            )
            payload = json.loads(output.getvalue())
            version = ConfigVersion.objects.get(location=self.hq)

            self.assertEqual(
                (expanded_dir / "asterisk" / "extensions.conf").read_text(encoding="utf-8"),
                payload["asterisk_configs"]["extensions.conf"],
            )
            self.assertEqual(
                (expanded_dir / "asterisk" / "pjsip.conf").read_text(encoding="utf-8"),
                self._golden("pjsip.conf"),
            )
            self.assertTrue((expanded_dir / "docker-compose.yml").exists())
            self.assertTrue((expanded_dir / ".env.example").exists())
            self.assertTrue((expanded_dir / "tftp" / "company-directory.xml").exists())
            self.assertTrue((expanded_dir / "manifest.json").exists())
            self.assertTrue((expanded_dir / "SHA256SUMS").exists())
            self.assertEqual(payload["config_version"]["version_number"], version.version_number)
            self.assertEqual(payload["config_version"]["checksum"], version.checksum)
            self.assertEqual(zip_path.read_bytes(), bytes(version.archive))
            if os.name == "posix":
                self.assertEqual(zip_path.stat().st_mode & 0o777, 0o600)
                self.assertEqual(expanded_dir.stat().st_mode & 0o777, 0o700)
                self.assertEqual((expanded_dir / "asterisk" / "extensions.conf").stat().st_mode & 0o777, 0o600)

    def _golden(self, filename):
        return (
            Path(__file__).with_name("testdata") / "asterisk_configs" / filename
        ).read_text(encoding="utf-8")


class AgentWebSocketTests(TransactionTestCase):
    def test_authenticated_agent_reports_active_config_version(self):
        location = Location.objects.create(
            **location_model_data(name="Agent HQ", slug="agent-hq", agent_secret="agent-secret")
        )
        checksum = "a" * 64

        events = asyncio.run(
            self._run_agent_websocket(
                headers=self._agent_headers(location),
                messages=[
                    {
                        "type": "websocket.receive",
                        "text": json.dumps(
                            {
                                "type": "active_config",
                                "version": 7,
                                "checksum": checksum,
                                "timestamp": "2026-06-03T20:00:00Z",
                            }
                        ),
                    }
                ],
            )
        )

        location.refresh_from_db()
        self.assertEqual(events[0], {"type": "websocket.accept"})
        self.assertEqual(json.loads(events[1]["text"]), {"type": "agent_authenticated", "location": "agent-hq"})
        self.assertEqual(json.loads(events[2]["text"])["type"], "active_config_ack")
        self.assertEqual(location.active_config_version_number, 7)
        self.assertEqual(location.active_config_checksum, checksum)
        self.assertEqual(location.active_config_timestamp.isoformat(), "2026-06-03T20:00:00+00:00")
        self.assertIsNotNone(location.active_config_reported_at)

    def test_invalid_agent_credentials_are_rejected_without_location_update(self):
        location = Location.objects.create(
            **location_model_data(name="Rejected Agent HQ", slug="rejected-agent-hq", agent_secret="agent-secret")
        )

        events = asyncio.run(
            self._run_agent_websocket(
                headers=[
                    (b"x-pbx-agent-token", location.agent_token.encode("ascii")),
                    (b"x-pbx-agent-secret", b"wrong-secret"),
                ],
                messages=[
                    {
                        "type": "websocket.receive",
                        "text": json.dumps(
                            {
                                "type": "active_config",
                                "version": 9,
                                "checksum": "b" * 64,
                                "timestamp": "2026-06-03T20:00:00Z",
                            }
                        ),
                    }
                ],
            )
        )

        location.refresh_from_db()
        self.assertEqual(events, [{"type": "websocket.close", "code": 4401}])
        self.assertIsNone(location.active_config_version_number)
        self.assertEqual(location.active_config_checksum, "")
        self.assertIsNone(location.active_config_timestamp)
        self.assertIsNone(location.active_config_reported_at)

    def test_authenticated_agent_reports_telemetry_snapshot(self):
        location = Location.objects.create(
            **location_model_data(name="Telemetry HQ", slug="telemetry-hq", agent_secret="agent-secret")
        )
        payload = {
            "type": "telemetry",
            "timestamp": "2026-06-04T03:45:00Z",
            "location_health": {
                "location_slug": "spoofed-slug",
                "ami_connected": True,
                "collected_at": "2026-06-04T03:45:00Z",
            },
            "phone_registrations": [{"extension": "3000", "status": "reachable"}],
            "trunk_status": [{"name": "trunk-primary-sip", "status": "not in use"}],
            "active_calls": [{"channel": "PJSIP/3000-00000001"}],
            "queue_status": [{"name": "support-queue"}],
            "recent_calls": [{"uniqueid": "1717460000.1"}],
            "call_events": [{"event_type": "ANSWER"}],
            "recording_metadata": [{"filename": "1717460000.1.wav"}],
            "telemetry_errors": [{"category": "recording_metadata", "message": "scan skipped"}],
        }

        events = asyncio.run(
            self._run_agent_websocket(
                headers=self._agent_headers(location),
                messages=[
                    {
                        "type": "websocket.receive",
                        "text": json.dumps(payload),
                    }
                ],
            )
        )

        location.refresh_from_db()
        ack = json.loads(events[2]["text"])
        self.assertEqual(ack["type"], "telemetry_ack")
        self.assertEqual(ack["location"], "telemetry-hq")
        self.assertEqual(ack["error_count"], 1)
        self.assertIsNotNone(location.agent_telemetry_reported_at)
        self.assertEqual(location.agent_telemetry["phone_registrations"][0]["extension"], "3000")
        self.assertEqual(location.agent_telemetry["trunk_status"][0]["name"], "trunk-primary-sip")
        self.assertEqual(location.agent_telemetry["active_calls"][0]["channel"], "PJSIP/3000-00000001")
        self.assertEqual(location.agent_telemetry["queue_status"][0]["name"], "support-queue")
        self.assertEqual(location.agent_telemetry["recent_calls"][0]["uniqueid"], "1717460000.1")
        self.assertEqual(location.agent_telemetry["call_events"][0]["event_type"], "ANSWER")
        self.assertEqual(location.agent_telemetry["recording_metadata"][0]["filename"], "1717460000.1.wav")
        self.assertEqual(location.agent_telemetry_errors, payload["telemetry_errors"])

    def test_query_string_agent_credentials_are_rejected(self):
        location = Location.objects.create(
            **location_model_data(name="Query Agent HQ", slug="query-agent-hq", agent_secret="agent-secret")
        )

        events = asyncio.run(
            self._run_agent_websocket(
                query_string=f"token={location.agent_token}&secret=agent-secret".encode("utf-8"),
                messages=[
                    {
                        "type": "websocket.receive",
                        "text": json.dumps({"type": "active_config", "version": 1, "checksum": "c" * 64}),
                    }
                ],
            )
        )

        location.refresh_from_db()
        self.assertEqual(events, [{"type": "websocket.close", "code": 4401}])
        self.assertIsNone(location.active_config_version_number)

    def test_portal_dispatches_live_command_to_authenticated_agent_websocket(self):
        location = Location.objects.create(
            **location_model_data(name="Live Agent HQ", slug="live-agent-hq", agent_secret="agent-secret")
        )

        events, result = asyncio.run(self._run_live_command_exchange(location))

        outbound_payloads = [
            json.loads(event["text"])
            for event in events
            if event["type"] == "websocket.send"
        ]
        live_command = next(payload for payload in outbound_payloads if payload["type"] == "live_command")
        self.assertEqual(live_command["command"], "core_reload")
        self.assertEqual(live_command["parameters"], {})
        self.assertEqual(result["type"], "live_command_result")
        self.assertEqual(result["command_id"], live_command["command_id"])
        self.assertEqual(result["status"], "success")
        self.assertIn(
            {"type": "live_command_result_ack", "command_id": live_command["command_id"]},
            outbound_payloads,
        )

    def test_portal_dispatches_recording_file_request_to_authenticated_agent_websocket(self):
        location = Location.objects.create(
            **location_model_data(name="Recording Agent HQ", slug="recording-agent-hq", agent_secret="agent-secret")
        )

        events, result = asyncio.run(self._run_recording_playback_exchange(location))

        outbound_payloads = [
            json.loads(event["text"])
            for event in events
            if event["type"] == "websocket.send"
        ]
        recording_request = next(payload for payload in outbound_payloads if payload["type"] == "recording_file_request")
        self.assertEqual(recording_request["path"], "/var/spool/asterisk/monitor/call.wav")
        self.assertEqual(recording_request["retention_days"], 30)
        self.assertEqual(result["type"], "recording_file_result")
        self.assertEqual(result["request_id"], recording_request["request_id"])
        self.assertEqual(result["status"], "success")
        self.assertIn(
            {"type": "recording_file_result_ack", "request_id": recording_request["request_id"]},
            outbound_payloads,
        )

    async def _run_agent_websocket(self, *, query_string=b"", headers=None, messages=None):
        from portal.asgi import application

        events = []
        inbound = [{"type": "websocket.connect"}, *(messages or []), {"type": "websocket.disconnect"}]

        async def receive():
            return inbound.pop(0)

        async def send(message):
            events.append(message)

        await application(
            {
                "type": "websocket",
                "path": "/api/agent/ws/",
                "query_string": query_string,
                "headers": headers or [],
            },
            receive,
            send,
        )
        return events

    def _agent_headers(self, location, secret="agent-secret"):
        return [
            (b"x-pbx-agent-token", location.agent_token.encode("ascii")),
            (b"x-pbx-agent-secret", secret.encode("ascii")),
        ]

    async def _run_live_command_exchange(self, location):
        from portal.asgi import application
        from .live_operations import run_location_live_command

        events = []
        inbound = asyncio.Queue()
        authenticated = asyncio.Event()
        await inbound.put({"type": "websocket.connect"})

        async def receive():
            return await inbound.get()

        async def send(message):
            events.append(message)
            if message["type"] != "websocket.send":
                return
            payload = json.loads(message["text"])
            if payload["type"] == "agent_authenticated":
                authenticated.set()
            if payload["type"] == "live_command":
                await inbound.put(
                    {
                        "type": "websocket.receive",
                        "text": json.dumps(
                            {
                                "type": "live_command_result",
                                "command_id": payload["command_id"],
                                "command": payload["command"],
                                "status": "success",
                                "ami_response": [{"Response": "Success"}],
                            }
                        ),
                    }
                )

        app_task = asyncio.create_task(
            application(
                {
                    "type": "websocket",
                    "path": "/api/agent/ws/",
                    "query_string": b"",
                    "headers": self._agent_headers(location),
                },
                receive,
                send,
            )
        )
        await asyncio.wait_for(authenticated.wait(), timeout=1)
        result = await asyncio.to_thread(run_location_live_command, location, "core_reload")
        await inbound.put({"type": "websocket.disconnect"})
        await asyncio.wait_for(app_task, timeout=1)
        return events, result

    async def _run_recording_playback_exchange(self, location):
        from portal.asgi import application
        from .live_operations import run_location_recording_playback

        events = []
        inbound = asyncio.Queue()
        authenticated = asyncio.Event()
        await inbound.put({"type": "websocket.connect"})

        async def receive():
            return await inbound.get()

        async def send(message):
            events.append(message)
            if message["type"] != "websocket.send":
                return
            payload = json.loads(message["text"])
            if payload["type"] == "agent_authenticated":
                authenticated.set()
            if payload["type"] == "recording_file_request":
                await inbound.put(
                    {
                        "type": "websocket.receive",
                        "text": json.dumps(
                            {
                                "type": "recording_file_result",
                                "request_id": payload["request_id"],
                                "status": "success",
                                "filename": "call.wav",
                                "content_base64": base64.b64encode(b"RIFF").decode("ascii"),
                            }
                        ),
                    }
                )

        app_task = asyncio.create_task(
            application(
                {
                    "type": "websocket",
                    "path": "/api/agent/ws/",
                    "query_string": b"",
                    "headers": self._agent_headers(location),
                },
                receive,
                send,
            )
        )
        await asyncio.wait_for(authenticated.wait(), timeout=1)
        result = await asyncio.to_thread(
            run_location_recording_playback,
            location,
            "/var/spool/asterisk/monitor/call.wav",
            retention_days=30,
        )
        await inbound.put({"type": "websocket.disconnect"})
        await asyncio.wait_for(app_task, timeout=1)
        return events, result


class AgentTelemetryParsingTests(SimpleTestCase):
    def test_parses_representative_ami_cdr_cel_and_recording_fixtures(self):
        contacts = parse_phone_registrations(parse_ami_messages(self._telemetry_fixture("ami_contacts.txt")))
        trunks = parse_trunk_status(parse_ami_messages(self._telemetry_fixture("ami_endpoints.txt")))
        calls = parse_active_calls(parse_ami_messages(self._telemetry_fixture("ami_channels.txt")))
        queues = parse_queue_status(parse_ami_messages(self._telemetry_fixture("ami_queues.txt")))
        health = parse_location_health(parse_ami_messages(self._telemetry_fixture("ami_core_status.txt")))
        recent_calls = parse_cdr_csv(self._telemetry_fixture("cdr_master.csv"))
        call_events = parse_cel_csv(self._telemetry_fixture("cel_master.csv"))

        self.assertEqual(contacts[0]["extension"], "3000")
        self.assertTrue(contacts[0]["reachable"])
        self.assertEqual(contacts[1]["extension"], "3001")
        self.assertFalse(contacts[1]["reachable"])
        self.assertEqual(trunks, [
            {
                "name": "trunk-primary-sip",
                "technology": "PJSIP",
                "status": "not in use",
                "available": True,
                "active_contacts": 1,
                "configured_contacts": 1,
                "aors": ["aor-trunk-primary-sip"],
                "transport": "transport-tcp",
                "outbound_auths": ["auth-trunk-primary-sip"],
            }
        ])
        self.assertEqual(calls[0]["uniqueid"], "1717460000.1")
        self.assertEqual(calls[0]["application"], "Dial")
        self.assertEqual(queues[0]["name"], "support-queue")
        self.assertEqual(queues[0]["members"][0]["location"], "PJSIP/3000")
        self.assertEqual(queues[0]["callers"][0]["caller_id"], "15557654321")
        self.assertEqual(health["core_current_calls"], 1)
        self.assertEqual(recent_calls[0]["uniqueid"], "1717460200.2")
        self.assertEqual(recent_calls[0]["disposition"], "ANSWERED")
        self.assertEqual(call_events[0]["event_type"], "ANSWER")

    def test_scans_recording_metadata(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            recording = Path(temp_dir) / "1717460000.1-in.wav"
            recording.write_bytes(b"RIFF")

            recordings = scan_recording_metadata(temp_dir)

        self.assertEqual(recordings[0]["filename"], "1717460000.1-in.wav")
        self.assertEqual(recordings[0]["size_bytes"], 4)
        self.assertEqual(recordings[0]["uniqueid"], "1717460000.1")

    def test_scans_recording_metadata_with_retention_status(self):
        now = datetime(2026, 6, 4, 12, 0, tzinfo=datetime_timezone.utc)
        with tempfile.TemporaryDirectory() as temp_dir:
            fresh = Path(temp_dir) / "fresh.wav"
            fresh.write_bytes(b"RIFF")
            fresh_mtime = (now - timedelta(days=1)).timestamp()
            os.utime(fresh, (fresh_mtime, fresh_mtime))

            expired = Path(temp_dir) / "expired.wav"
            expired.write_bytes(b"RIF")
            expired_mtime = (now - timedelta(days=45)).timestamp()
            os.utime(expired, (expired_mtime, expired_mtime))

            recordings = scan_recording_metadata(temp_dir, retention_days=30, now=now)

        by_filename = {recording["filename"]: recording for recording in recordings}
        self.assertEqual(by_filename["fresh.wav"]["recording_id"], recording_id_for_path("fresh.wav"))
        self.assertEqual(by_filename["fresh.wav"]["relative_path"], "fresh.wav")
        self.assertEqual(by_filename["fresh.wav"]["retention_days"], 30)
        self.assertFalse(by_filename["fresh.wav"]["expired"])
        self.assertTrue(by_filename["fresh.wav"]["available"])
        self.assertEqual(by_filename["fresh.wav"]["status"], "available")
        self.assertEqual(by_filename["expired.wav"]["status"], "expired")
        self.assertTrue(by_filename["expired.wav"]["expired"])
        self.assertFalse(by_filename["expired.wav"]["available"])

    def _telemetry_fixture(self, filename):
        return (Path(__file__).with_name("testdata") / "telemetry" / filename).read_text(encoding="utf-8")


class AgentRecordingFileRequestTests(SimpleTestCase):
    def test_agent_control_message_returns_recording_file_content(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            recording = Path(temp_dir) / "call.wav"
            recording.write_bytes(b"RIFF")
            config = AgentConfig(
                websocket_url="ws://portal.example.test/api/agent/ws/",
                token="token",
                secret="secret",
                marker_path=Path(temp_dir) / "marker.json",
                recording_root=temp_dir,
                recording_retention_days=30,
            )

            result = asyncio.run(
                handle_agent_control_message(
                    config,
                    {
                        "type": "recording_file_request",
                        "request_id": "req-1",
                        "path": "call.wav",
                    },
                )
            )

        self.assertEqual(result["type"], "recording_file_result")
        self.assertEqual(result["request_id"], "req-1")
        self.assertEqual(result["status"], "success")
        self.assertEqual(result["filename"], "call.wav")
        self.assertEqual(base64.b64decode(result["content_base64"]), b"RIFF")

    def test_agent_control_message_rejects_expired_recording_file(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            recording = Path(temp_dir) / "expired.wav"
            recording.write_bytes(b"RIFF")
            old_mtime = (datetime.now(tz=datetime_timezone.utc) - timedelta(days=2)).timestamp()
            os.utime(recording, (old_mtime, old_mtime))
            config = AgentConfig(
                websocket_url="ws://portal.example.test/api/agent/ws/",
                token="token",
                secret="secret",
                marker_path=Path(temp_dir) / "marker.json",
                recording_root=temp_dir,
                recording_retention_days=1,
            )

            result = asyncio.run(
                handle_agent_control_message(
                    config,
                    {
                        "type": "recording_file_request",
                        "request_id": "req-expired",
                        "path": "expired.wav",
                    },
                )
            )

        self.assertEqual(result["type"], "recording_file_result")
        self.assertEqual(result["request_id"], "req-expired")
        self.assertEqual(result["status"], "failure")
        self.assertEqual(result["error_code"], "expired")


class AgentActiveConfigMarkerTests(TestCase):
    def test_agent_reads_active_marker_and_sends_outbound_websocket_report(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            marker_path = Path(temp_dir) / "pbx-active-config.json"
            marker_path.write_text(
                json.dumps(
                    {
                        "version": 3,
                        "checksum": "c" * 64,
                        "timestamp": "2026-06-03T20:00:00Z",
                    }
                ),
                encoding="utf-8",
            )

            marker = read_active_config_marker(marker_path)
            self.assertEqual(marker.version, 3)
            self.assertEqual(marker.checksum, "c" * 64)
            self.assertEqual(marker.timestamp, "2026-06-03T20:00:00Z")

            websocket_exchange = mock.AsyncMock(return_value={"type": "active_config_ack"})
            with mock.patch("core.agent_client.websocket_json_exchange", websocket_exchange):
                response = asyncio.run(
                    report_active_config_once(
                        AgentConfig(
                            websocket_url="wss://portal.warp.test/api/agent/ws/",
                            token="agent-token",
                            secret="agent-secret",
                            marker_path=marker_path,
                        )
                    )
                )

        self.assertEqual(response, {"type": "active_config_ack"})
        websocket_exchange.assert_awaited_once_with(
            "wss://portal.warp.test/api/agent/ws/",
            {
                "type": "active_config",
                "version": 3,
                "checksum": "c" * 64,
                "timestamp": "2026-06-03T20:00:00Z",
            },
            {
                "X-PBX-Agent-Token": "agent-token",
                "X-PBX-Agent-Secret": "agent-secret",
            },
        )

    def test_agent_websocket_url_defaults_to_warp_reachable_portal_path(self):
        self.assertEqual(
            portal_url_to_websocket_url("https://portal.warp.test"),
            "wss://portal.warp.test/api/agent/ws/",
        )

    def test_agent_reports_collector_failure_as_telemetry_payload(self):
        async def failing_collector(_config):
            raise RuntimeError("AMI unavailable")

        websocket_exchange = mock.AsyncMock(return_value={"type": "telemetry_ack"})

        with mock.patch("core.agent_client.websocket_json_exchange", websocket_exchange):
            response = asyncio.run(
                report_telemetry_once(
                    AgentConfig(
                        websocket_url="wss://portal.warp.test/api/agent/ws/",
                        token="agent-token",
                        secret="agent-secret",
                        marker_path=Path("/tmp/unused-marker.json"),
                        location_slug="agent-hq",
                    ),
                    collector=failing_collector,
                )
            )

        self.assertEqual(response, {"type": "telemetry_ack"})
        payload = websocket_exchange.await_args.args[1]
        self.assertEqual(payload["telemetry_errors"], [{"category": "telemetry", "message": "AMI unavailable"}])
        self.assertFalse(payload["location_health"]["ami_connected"])

    def test_agent_reconnect_loop_preserves_location_identity(self):
        calls = []

        async def collector(config):
            return {
                "type": "telemetry",
                "timestamp": "2026-06-04T03:45:00Z",
                "location_health": {"location_slug": config.location_slug, "ami_connected": True},
                "phone_registrations": [],
                "trunk_status": [],
                "active_calls": [],
                "queue_status": [],
                "recent_calls": [],
                "call_events": [],
                "recording_metadata": [],
                "telemetry_errors": [],
            }

        async def flaky_exchange(url, payload, headers):
            calls.append((url, payload, headers))
            if len(calls) == 1:
                raise ConnectionError("portal dropped connection")
            return {"type": "telemetry_ack", "location": "agent-hq"}

        async def no_sleep(_seconds):
            return None

        asyncio.run(
            run_telemetry_loop(
                AgentConfig(
                    websocket_url="wss://portal.warp.test/api/agent/ws/",
                    token="agent-token",
                    secret="agent-secret",
                    marker_path=Path("/tmp/unused-marker.json"),
                    location_slug="agent-hq",
                    telemetry_interval_seconds=0,
                ),
                collector=collector,
                websocket_exchange=flaky_exchange,
                sleep=no_sleep,
                reconnect_delay_seconds=0,
                iterations=2,
            )
        )

        self.assertEqual(len(calls), 2)
        self.assertEqual(calls[0][2], calls[1][2])
        self.assertEqual(calls[1][2]["X-PBX-Agent-Token"], "agent-token")
        self.assertEqual(calls[1][2]["X-PBX-Agent-Secret"], "agent-secret")
        self.assertEqual(calls[1][1]["location_health"]["location_slug"], "agent-hq")


class AgentLiveCommandExecutionTests(SimpleTestCase):
    def test_agent_executes_supported_live_command_through_ami(self):
        calls = []
        init_kwargs = []

        class FakeAMIClient:
            def __init__(self, **kwargs):
                init_kwargs.append(kwargs)

            async def __aenter__(self):
                return self

            async def __aexit__(self, exc_type, exc, traceback):
                return None

            async def action(self, action, parameters=None, *, complete_event=None):
                calls.append((action, parameters, complete_event))
                return [{"Response": "Success", "Message": "Command output follows"}]

        result = asyncio.run(
            execute_live_ami_command(
                AgentConfig(
                    websocket_url="wss://portal.warp.test/api/agent/ws/",
                    token="agent-token",
                    secret="agent-secret",
                    marker_path=Path("/tmp/unused-marker.json"),
                    ami_host="127.0.0.1",
                    ami_port=5038,
                    ami_username="ami-user",
                    ami_secret="ami-secret",
                ),
                "pjsip_reload",
                ami_client_factory=FakeAMIClient,
            )
        )

        self.assertEqual(result["status"], "success")
        self.assertEqual(result["ami_action"], "Command")
        self.assertEqual(result["ami_parameters"], {"Command": "pjsip reload"})
        self.assertEqual(calls, [("Command", {"Command": "pjsip reload"}, None)])
        self.assertEqual(init_kwargs[0]["username"], "ami-user")
        self.assertEqual(init_kwargs[0]["secret"], "ami-secret")

    def test_agent_rejects_unsupported_live_command_before_ami(self):
        calls = []

        class FakeAMIClient:
            def __init__(self, **_kwargs):
                calls.append("init")

        result = asyncio.run(
            execute_live_ami_command(
                AgentConfig(
                    websocket_url="wss://portal.warp.test/api/agent/ws/",
                    token="agent-token",
                    secret="agent-secret",
                    marker_path=Path("/tmp/unused-marker.json"),
                    ami_username="ami-user",
                    ami_secret="ami-secret",
                ),
                "raw_ami_command",
                ami_client_factory=FakeAMIClient,
            )
        )

        self.assertEqual(result["status"], "failure")
        self.assertIn("Unsupported live PBX command", result["error"])
        self.assertEqual(calls, [])

    def test_agent_live_command_message_returns_command_result_payload(self):
        async def fake_executor(config, command_name, parameters):
            return {
                "status": "success",
                "ami_action": "QueueReload",
                "location_slug": config.location_slug,
                "parameters": parameters,
                "command_name": command_name,
            }

        response = asyncio.run(
            handle_agent_control_message(
                AgentConfig(
                    websocket_url="wss://portal.warp.test/api/agent/ws/",
                    token="agent-token",
                    secret="agent-secret",
                    marker_path=Path("/tmp/unused-marker.json"),
                    location_slug="agent-hq",
                ),
                {
                    "type": "live_command",
                    "command_id": "cmd-2",
                    "command": "queue_reload",
                    "parameters": {},
                },
                command_executor=fake_executor,
            )
        )

        self.assertEqual(response["type"], "live_command_result")
        self.assertEqual(response["command_id"], "cmd-2")
        self.assertEqual(response["command"], "queue_reload")
        self.assertEqual(response["status"], "success")
        self.assertEqual(response["ami_action"], "QueueReload")
        self.assertEqual(response["location_slug"], "agent-hq")


class ConfigVersionExportTests(TestCase):
    maxDiff = None

    def setUp(self):
        self.user = User.objects.create_user(username="exporter", password="portal-pass")
        self.location = Location.objects.create(
            **location_model_data(name="Version HQ", slug="version-hq", agent_token="agent-token")
        )
        self.extension = Extension.objects.create(
            location=self.location,
            number="3000",
            display_name="Version Desk",
            sip_username="3000",
            sip_password="sip-secret",
            emergency_calling_enabled=True,
        )
        add_emergency_route(self.location)

    def test_every_export_creates_new_immutable_version_record(self):
        first = create_config_version(self.location, exported_by=self.user)
        second = create_config_version(self.location, exported_by=self.user)

        self.assertEqual(first.version_number, 1)
        self.assertEqual(second.version_number, 2)
        self.assertEqual(ConfigVersion.objects.filter(location=self.location).count(), 2)
        self.assertEqual(first.exported_by, self.user)
        self.assertEqual(first.archive_size_bytes, len(bytes(first.archive)))
        self.assertEqual(first.checksum, hashlib.sha256(bytes(first.archive)).hexdigest())
        first_checksum = first.checksum
        first.checksum = "0" * 64
        with self.assertRaises(ValidationError):
            first.save()
        first.refresh_from_db()
        self.assertEqual(first.checksum, first_checksum)

    @override_settings(
        PBX_AGENT_PORTAL_URL="https://portal.warp.test",
        PBX_ACTIVE_CONFIG_MARKER="/var/lib/pbx/active.json",
    )
    def test_export_zip_structure_manifest_and_checksum_snapshot(self):
        version = create_config_version(self.location, exported_by=self.user)

        with zipfile.ZipFile(BytesIO(bytes(version.archive))) as archive:
            names = sorted(archive.namelist())
            env_example = archive.read(".env.example").decode("utf-8")
            manifest = json.loads(archive.read("manifest.json").decode("utf-8"))
            checksums = archive.read("SHA256SUMS").decode("utf-8").splitlines()
            modes = {
                (info.external_attr >> 16) & 0o777
                for info in archive.infolist()
                if not info.is_dir()
            }

        self.assertEqual(
            names,
            sorted(
                [
                    ".env.example",
                    "SHA256SUMS",
                    "asterisk/cdr.conf",
                    "asterisk/cel.conf",
                    "asterisk/extensions.conf",
                    "asterisk/features.conf",
                    "asterisk/iax.conf",
                    "asterisk/manager.conf",
                    "asterisk/musiconhold.conf",
                    "asterisk/pjsip.conf",
                    "asterisk/queues.conf",
                    "asterisk/recording.conf",
                    "asterisk/retention.conf",
                    "asterisk/rtp.conf",
                    "asterisk/voicemail.conf",
                    "active-config/var/lib/pbx/active.json",
                    "docker-compose.yml",
                    "manifest.json",
                    "runtime-images.json",
                    "runtime/asterisk/Dockerfile",
                    "runtime/asterisk/docker-entrypoint.sh",
                    "scripts/pbx-recording-retention",
                    "tftp/company-directory.xml",
                    "tftp/firmware/CISCO-FIRMWARE-CHECKLIST.txt",
                    "tftp/firmware/README-no-firmware-bundled.txt",
                ]
            ),
        )
        self.assertEqual(manifest["format"], "pbx-config-export/v1")
        self.assertEqual(manifest["location"]["slug"], "version-hq")
        self.assertEqual(manifest["version"]["number"], 1)
        self.assertEqual(manifest["version"]["exported_by"], "exporter")
        self.assertFalse(manifest["emergency_status"]["blocked"])
        self.assertIn("PBX_AGENT_WS_URL=wss://portal.warp.test/api/agent/ws/", env_example)
        self.assertIn(f"PBX_AGENT_TOKEN={self.location.agent_token}", env_example)
        self.assertIn("PBX_AGENT_SECRET=agent-secret", env_example)
        self.assertIn("PBX_ACTIVE_CONFIG_MARKER=/var/lib/pbx/active.json", env_example)
        self.assertEqual(manifest["active_config_marker"]["path"], "active-config/var/lib/pbx/active.json")
        self.assertEqual(manifest["active_config_marker"]["configured_path"], "/var/lib/pbx/active.json")
        self.assertEqual(manifest["runtime_images"]["path"], "runtime-images.json")
        self.assertEqual(manifest["runtime_images"]["tag_policy"], "warn")
        self.assertIn("active-config/var/lib/pbx/active.json", names)
        self.assertIn("runtime-images.json", names)
        self.assertIn(
            {
                "path": "docker-compose.yml",
                "content_type": "application/x-yaml",
                "size": next(file["size"] for file in version.file_manifest if file["path"] == "docker-compose.yml"),
                "sha256": next(file["sha256"] for file in version.file_manifest if file["path"] == "docker-compose.yml"),
            },
            manifest["files"],
        )
        self.assertTrue(any(line.endswith("  manifest.json") for line in checksums))
        self.assertTrue(any(line.endswith("  asterisk/pjsip.conf") for line in checksums))
        self.assertTrue(any(line.endswith("  asterisk/rtp.conf") for line in checksums))
        self.assertTrue(any(line.endswith("  active-config/var/lib/pbx/active.json") for line in checksums))
        self.assertTrue(any(line.endswith("  runtime-images.json") for line in checksums))
        self.assertTrue(any(line.endswith("  runtime/asterisk/Dockerfile") for line in checksums))
        self.assertTrue(any(line.endswith("  runtime/asterisk/docker-entrypoint.sh") for line in checksums))
        self.assertTrue(any(line.endswith("  scripts/pbx-recording-retention") for line in checksums))
        self.assertEqual(version.checksum, hashlib.sha256(bytes(version.archive)).hexdigest())
        self.assertEqual(
            {file["path"] for file in version.file_manifest},
            set(names),
        )
        self.assertEqual(modes, {0o600, 0o700})

    def test_runtime_bundle_files_match_golden_templates(self):
        version = create_config_version(self.location, exported_by=self.user)

        with zipfile.ZipFile(BytesIO(bytes(version.archive))) as archive:
            docker_compose = archive.read("docker-compose.yml").decode("utf-8")
            env_example = archive.read(".env.example").decode("utf-8")
            runtime_images = archive.read("runtime-images.json").decode("utf-8")
            asterisk_dockerfile = archive.read("runtime/asterisk/Dockerfile").decode("utf-8")
            asterisk_entrypoint = archive.read("runtime/asterisk/docker-entrypoint.sh").decode("utf-8")

        self.assertEqual(docker_compose, self._runtime_golden("docker-compose.yml"))
        self.assertEqual(env_example, self._runtime_golden(".env.example"))
        self.assertEqual(runtime_images, self._runtime_golden("runtime-images.json"))
        self.assertEqual(asterisk_dockerfile, self._runtime_golden("runtime/asterisk/Dockerfile"))
        self.assertEqual(asterisk_entrypoint, self._runtime_golden("runtime/asterisk/docker-entrypoint.sh"))

    def test_runtime_bundle_compose_services_and_volume_paths(self):
        version = create_config_version(self.location, exported_by=self.user)

        with zipfile.ZipFile(BytesIO(bytes(version.archive))) as archive:
            services = self._compose_service_blocks(archive.read("docker-compose.yml").decode("utf-8"))

        self.assertEqual(set(services), {"asterisk", "tftp", "provisioning-http", "pbx-agent"})
        self.assertIn("    build:", services["asterisk"])
        self.assertIn("      context: ./runtime/asterisk", services["asterisk"])
        self.assertIn("        ASTERISK_VERSION: \"${ASTERISK_VERSION:-20.19.0}\"", services["asterisk"])
        self.assertIn("    image: ${PBX_ASTERISK_IMAGE:-pbx-asterisk:20.19.0-cisco}", services["asterisk"])
        self.assertIn("    network_mode: host", services["asterisk"])
        self.assertIn("      - ./asterisk:/etc/asterisk:ro", services["asterisk"])
        self.assertIn(
            "    image: ${PBX_TFTP_IMAGE:?PBX_TFTP_IMAGE must include an immutable digest}",
            services["tftp"],
        )
        self.assertIn("      - ./asterisk/sounds:/var/lib/asterisk/sounds:ro", services["asterisk"])
        self.assertIn("      - ./tftp:/srv/tftp:ro", services["tftp"])
        self.assertIn('      - "${PROVISIONING_TFTP_PORT:-69}:69/udp"', services["tftp"])
        self.assertIn("      - ./tftp:/usr/share/nginx/html/cisco:ro", services["provisioning-http"])
        self.assertIn('      - "${PROVISIONING_HTTP_PORT:-80}:80/tcp"', services["provisioning-http"])
        self.assertIn(
            "    image: ${PBX_AGENT_IMAGE:?PBX_AGENT_IMAGE must include an immutable digest}",
            services["pbx-agent"],
        )
        self.assertIn("    network_mode: host", services["pbx-agent"])
        self.assertIn('      PBX_AGENT_WS_URL: "${PBX_AGENT_WS_URL:?PBX_AGENT_WS_URL is required}"', services["pbx-agent"])
        self.assertIn('      PBX_AGENT_TOKEN: "${PBX_AGENT_TOKEN:?PBX_AGENT_TOKEN is required}"', services["pbx-agent"])
        self.assertIn('      PBX_AGENT_SECRET: "${PBX_AGENT_SECRET:?PBX_AGENT_SECRET is required}"', services["pbx-agent"])
        self.assertIn("      PBX_ACTIVE_CONFIG_MARKER: ${PBX_ACTIVE_CONFIG_MARKER:-/etc/asterisk/pbx-active-config.json}", services["pbx-agent"])
        self.assertIn("      - ./asterisk:/etc/asterisk:ro", services["pbx-agent"])

    def _runtime_golden(self, filename):
        return (Path(__file__).with_name("testdata") / "runtime_bundle" / filename).read_text(encoding="utf-8")

    def _compose_service_blocks(self, compose_text):
        services = {}
        current_service = None
        in_services = False
        for line in compose_text.splitlines():
            if line == "services:":
                in_services = True
                continue
            if not in_services:
                continue
            if line and not line.startswith(" "):
                break
            if line.startswith("  ") and not line.startswith("    ") and line.endswith(":"):
                current_service = line.strip()[:-1]
                services[current_service] = []
                continue
            if current_service:
                services[current_service].append(line)
        return {service: "\n".join(lines) for service, lines in services.items()}


class FakeDeploymentRunner:
    def __init__(self, *, fail_step=None):
        self.fail_step = fail_step
        self.remote_commands = []
        self.uploads = []

    def run(self, command, **_kwargs):
        self.remote_commands.append(command)
        if self.fail_step == "reload_asterisk" and "core reload" in command:
            return DeploymentCommandResult(command=command, returncode=1, stderr="reload failed")
        stdout = "Reload OK" if "core reload" in command else ""
        return DeploymentCommandResult(command=command, stdout=stdout)

    def upload_bundle(self, bundle_dir, staging_path):
        asterisk_files = sorted(path.relative_to(bundle_dir).as_posix() for path in (bundle_dir / "asterisk").rglob("*") if path.is_file())
        tftp_files = sorted(path.relative_to(bundle_dir).as_posix() for path in (bundle_dir / "tftp").rglob("*") if path.is_file())
        scripts_files = sorted(
            path.relative_to(bundle_dir).as_posix()
            for path in (bundle_dir / "scripts").rglob("*")
            if path.is_file()
        ) if (bundle_dir / "scripts").exists() else []
        self.uploads.append(
            {
                "staging_path": staging_path,
                "asterisk_files": asterisk_files,
                "tftp_files": tftp_files,
                "scripts_files": scripts_files,
            }
        )
        return DeploymentCommandResult(command=f"upload bundle to {staging_path}")


class PBXWorkflowIntegrationTests(TestCase):
    maxDiff = None

    def setUp(self):
        self.operator = User.objects.create_user(username="pbx-integration-operator", password="portal-pass")
        self.hq = Location.objects.create(
            **location_model_data(
                name="Integration HQ",
                slug="integration-hq",
                lan_subnet="10.50.0.0/24",
                pbx_lan_ip="10.50.0.10",
                pbx_warp_ip="100.64.50.10",
                sip_bind_ip="10.50.0.10",
                iax_bind_ip="10.50.0.10",
                default_did="+15551205000",
                emergency_caller_id="+15551205999",
                recording_retention_days=120,
                deployment_staging_path="/srv/pbx/releases",
                deployment_asterisk_path="/srv/pbx/current/asterisk",
                deployment_tftp_path="/srv/pbx/current/tftp",
                deployment_reload_command="asterisk -rx 'core reload'",
                agent_secret="hq-agent-secret",
            )
        )
        self.warehouse = Location.objects.create(
            **location_model_data(
                name="Integration Warehouse",
                slug="warehouse-integration",
                lan_subnet="10.60.0.0/24",
                pbx_lan_ip="10.60.0.10",
                pbx_warp_ip="100.64.60.10",
                sip_bind_ip="10.60.0.10",
                iax_bind_ip="10.60.0.10",
                default_did="+15551206000",
                emergency_caller_id="+15551206999",
                agent_secret="warehouse-agent-secret",
            )
        )
        self.reception = Extension.objects.create(
            location=self.hq,
            number="3000",
            display_name="HQ Reception",
            email="reception.integration@example.test",
            sip_username="3000",
            sip_password="sip-secret-3000",
            voicemail_pin="1234",
            caller_id_name="HQ Reception",
            caller_id_number="+15551205000",
            emergency_calling_enabled=True,
            recording_policy=Extension.RecordingPolicy.ALWAYS,
        )
        self.support_agent = Extension.objects.create(
            location=self.hq,
            number="3001",
            display_name="Support Agent",
            sip_username="3001",
            sip_password="sip-secret-3001",
            emergency_calling_enabled=True,
        )
        self.remote_extension = Extension.objects.create(
            location=self.warehouse,
            number="4000",
            display_name="Warehouse Desk",
            sip_username="4000",
            sip_password="sip-secret-4000",
        )
        provider = Provider.objects.create(
            name="Integration SIP",
            slug="integration-sip",
            provider_type=Provider.ProviderType.SIP,
        )
        primary_trunk = Trunk.objects.create(
            location=self.hq,
            provider=provider,
            name="Primary SIP",
            trunk_type=Trunk.TrunkType.SIP,
            host="sip.primary.integration.test",
            username="primary-user",
            password="primary-secret",
            is_emergency_capable=True,
        )
        outbound_route = OutboundRoute.objects.create(
            location=self.hq,
            name="National",
            dial_pattern="NXXNXXXXXX",
            priority=1,
            caller_id_source=OutboundRoute.CallerIdSource.LOCATION_DEFAULT,
            recording_policy=OutboundRoute.RecordingPolicy.ALWAYS,
        )
        OutboundRouteTrunk.objects.create(outbound_route=outbound_route, trunk=primary_trunk, priority=1)
        add_emergency_route(self.hq)

        self.reception_destination = InboundDestination.objects.create(
            location=self.hq,
            name="Reception",
            destination_type=InboundDestination.DestinationType.EXTENSION,
            extension=self.reception,
        )
        self.hq.default_inbound_destination = self.reception_destination
        self.hq.save(update_fields=["default_inbound_destination", "updated_at"])
        self.queue = CallQueue.objects.create(
            location=self.hq,
            name="Support Queue",
            strategy=CallQueue.Strategy.ROUND_ROBIN,
            timeout_seconds=45,
            retry_seconds=7,
            music_on_hold="support-hold",
            overflow_destination=self.reception_destination,
            recording_policy=CallQueue.RecordingPolicy.ON_DEMAND,
        )
        QueueMember.objects.create(queue=self.queue, extension=self.reception, penalty=0)
        QueueMember.objects.create(queue=self.queue, extension=self.support_agent, penalty=1)
        self.queue_destination = InboundDestination.objects.create(
            location=self.hq,
            name="Support Queue",
            destination_type=InboundDestination.DestinationType.QUEUE,
            queue=self.queue,
        )
        self.ivr = IVR.objects.create(
            location=self.hq,
            name="Main IVR",
            prompt_name="custom/main-menu",
            timeout_seconds=6,
            business_hours_destination=self.queue_destination,
            after_hours_destination=self.reception_destination,
            timeout_destination=self.reception_destination,
            invalid_destination=self.reception_destination,
        )
        self.ivr_destination = InboundDestination.objects.create(
            location=self.hq,
            name="Main IVR",
            destination_type=InboundDestination.DestinationType.IVR,
            ivr=self.ivr,
        )
        IVRMenuOption.objects.create(
            ivr=self.ivr,
            digit="1",
            label="Support",
            destination=self.queue_destination,
        )
        DID.objects.create(
            location=self.hq,
            number="+15551205000",
            provider=provider,
            trunk=primary_trunk,
            direct_extension=self.reception,
            default_destination=self.reception_destination,
            label="Reception DID",
        )
        DID.objects.create(
            location=self.hq,
            number="+15551205001",
            provider=provider,
            trunk=primary_trunk,
            label="Fallback DID",
        )
        self.paging_group = PagingGroup.objects.create(location=self.hq, name="HQ Page", page_code="7100")
        PagingGroupMember.objects.create(paging_group=self.paging_group, extension=self.reception)
        PagingGroupMember.objects.create(paging_group=self.paging_group, extension=self.support_agent)

    def test_generated_configs_cover_registration_routing_trunks_did_ivr_queue_paging_and_recording(self):
        location_config = build_location_config(self.hq)
        configs = location_config["asterisk_configs"]
        inbound = location_config["inbound"]

        with self.subTest("local TCP PJSIP registration"):
            self.assertIn("[transport-tcp]", configs["pjsip.conf"])
            self.assertIn("protocol=tcp", configs["pjsip.conf"])
            self.assertIn("bind=10.50.0.10:5060", configs["pjsip.conf"])
            self.assertIn("[3000]\ntype=endpoint\ntransport=transport-tcp\ncontext=from-pjsip", configs["pjsip.conf"])
            self.assertIn("max_contacts=5", configs["pjsip.conf"])
            self.assertIn("permit=10.50.0.0/255.255.255.0", configs["pjsip.conf"])

        with self.subTest("remote IAX2 extension routing"):
            self.assertIn("[warehouse-integration]", configs["iax.conf"])
            self.assertIn("host=100.64.60.10", configs["iax.conf"])
            self.assertIn("trunk=yes", configs["iax.conf"])
            self.assertIn("exten => 4000,1,NoOp(Remote extension 4000 owned by warehouse-integration)", configs["extensions.conf"])
            self.assertIn(" same => n,Dial(IAX2/warehouse-integration/${EXTEN},30)", configs["extensions.conf"])

        with self.subTest("provider trunk generation"):
            self.assertIn("[trunk-primary-sip]", configs["pjsip.conf"])
            self.assertIn("outbound_auth=auth-trunk-primary-sip", configs["pjsip.conf"])
            self.assertIn("from_domain=sip.primary.integration.test", configs["pjsip.conf"])
            self.assertIn("contact=sip:sip.primary.integration.test", configs["pjsip.conf"])

        with self.subTest("DID fallback routing"):
            fallback_did = next(route for route in inbound["dids"] if route["number"] == "+15551205001")
            self.assertEqual(fallback_did["route_source"], "location_default")
            self.assertEqual(fallback_did["effective_destination"]["target"]["number"], "3000")
            self.assertIn("exten => +15551205001,1,NoOp(Inbound DID +15551205001)", configs["extensions.conf"])
            self.assertIn(" same => n,Goto(local-extensions,3000,1)", configs["extensions.conf"])

        with self.subTest("IVR hours destinations"):
            ivr_config = next(ivr for ivr in inbound["ivrs"] if ivr["name"] == "Main IVR")
            self.assertEqual(ivr_config["business_hours_destination"]["target"]["name"], "Support Queue")
            self.assertEqual(ivr_config["after_hours_destination"]["target"]["number"], "3000")
            self.assertEqual(ivr_config["business_hours_schedule"]["times"], "09:00-17:00")
            self.assertIn("exten => main-ivr,1,Goto(ivr-main-ivr,s,1)", configs["extensions.conf"])
            self.assertIn(" same => n,GotoIfTime(09:00-17:00,mon-fri,*,*?business-hours,1)", configs["extensions.conf"])
            self.assertIn("exten => business-hours,1,NoOp(IVR Main IVR business hours)", configs["extensions.conf"])
            self.assertIn(" same => n,Goto(queues,support-queue,1)", configs["extensions.conf"])
            self.assertIn("exten => after-hours,1,NoOp(IVR Main IVR after hours)", configs["extensions.conf"])
            self.assertIn("exten => 1,1,NoOp(IVR option 1 Support)", configs["extensions.conf"])

        with self.subTest("queue overflow"):
            self.assertIn("[support-queue]", configs["queues.conf"])
            self.assertIn("strategy=rrmemory", configs["queues.conf"])
            self.assertIn("member => PJSIP/3000,0,3000", configs["queues.conf"])
            self.assertIn("member => PJSIP/3001,1,3001", configs["queues.conf"])
            self.assertIn("exten => support-queue,1,NoOp(Queue Support Queue)", configs["extensions.conf"])
            self.assertIn(" same => n,Queue(support-queue,t,,,45)", configs["extensions.conf"])
            self.assertIn(" same => n,Goto(local-extensions,3000,1)", configs["extensions.conf"])

        with self.subTest("paging"):
            self.assertIn("exten => 7100,1,NoOp(Page HQ Page)", configs["extensions.conf"])
            self.assertIn(" same => n,Page(PJSIP/3000&PJSIP/3001)", configs["extensions.conf"])

        with self.subTest("recording policies"):
            self.assertIn("retention_days=120", configs["recording.conf"])
            self.assertIn("[extension-3000]\npolicy=always", configs["recording.conf"])
            self.assertIn("[queue-support-queue]\npolicy=on_demand", configs["recording.conf"])
            self.assertIn("[route-national]\npolicy=always", configs["recording.conf"])

    def test_agent_harness_covers_recording_metadata_active_version_and_reconnect(self):
        checksum = "d" * 64
        update_active_config_report(
            self.hq.id,
            {
                "type": "active_config",
                "version": 9,
                "checksum": checksum,
                "timestamp": "2026-06-04T03:45:00Z",
            },
        )
        update_agent_telemetry_report(
            self.hq.id,
            {
                "type": "telemetry",
                "timestamp": "2026-06-04T03:46:00Z",
                "location_health": {"location_slug": self.hq.slug, "ami_connected": True},
                "phone_registrations": [{"extension": "3000", "transport": "tcp", "status": "reachable"}],
                "trunk_status": [{"name": "trunk-primary-sip", "status": "available"}],
                "active_calls": [],
                "queue_status": [{"name": "support-queue", "calls_waiting": 0}],
                "recent_calls": [],
                "call_events": [],
                "recording_metadata": [
                    {
                        "recording_id": "call-3000",
                        "filename": "call-3000.wav",
                        "path": "/var/spool/asterisk/monitor/call-3000.wav",
                        "size_bytes": 4096,
                    }
                ],
                "telemetry_errors": [],
            },
        )

        self.hq.refresh_from_db()
        self.assertEqual(self.hq.active_config_version_number, 9)
        self.assertEqual(self.hq.active_config_checksum, checksum)
        self.assertEqual(self.hq.agent_telemetry["recording_metadata"][0]["filename"], "call-3000.wav")
        self.assertEqual(self.hq.agent_telemetry["phone_registrations"][0]["transport"], "tcp")

        calls = []

        async def collector(config):
            return {
                "type": "telemetry",
                "timestamp": "2026-06-04T03:47:00Z",
                "location_health": {"location_slug": config.location_slug, "ami_connected": True},
                "phone_registrations": [],
                "trunk_status": [],
                "active_calls": [],
                "queue_status": [],
                "recent_calls": [],
                "call_events": [],
                "recording_metadata": [],
                "telemetry_errors": [],
            }

        async def flaky_exchange(url, payload, headers):
            calls.append((url, payload, headers))
            if len(calls) == 1:
                raise ConnectionError("portal dropped connection")
            return {"type": "telemetry_ack", "location": self.hq.slug}

        async def no_sleep(_seconds):
            return None

        asyncio.run(
            run_telemetry_loop(
                AgentConfig(
                    websocket_url="wss://portal.warp.test/api/agent/ws/",
                    token=self.hq.agent_token,
                    secret=self.hq.agent_secret,
                    marker_path=Path("/tmp/unused-marker.json"),
                    location_slug=self.hq.slug,
                    telemetry_interval_seconds=0,
                ),
                collector=collector,
                websocket_exchange=flaky_exchange,
                sleep=no_sleep,
                iterations=2,
                reconnect_delay_seconds=0,
            )
        )

        self.assertEqual(len(calls), 2)
        self.assertEqual(calls[0][0], "wss://portal.warp.test/api/agent/ws/")
        self.assertEqual(calls[1][1]["location_health"]["location_slug"], self.hq.slug)
        self.assertEqual(calls[1][2]["X-PBX-Agent-Token"], self.hq.agent_token)

    def test_deployment_harness_covers_ssh_reload_and_rollback(self):
        previous_version = create_config_version(self.hq, exported_by=self.operator)
        current_version = create_config_version(self.hq, exported_by=self.operator)
        deploy_runner = FakeDeploymentRunner()
        rollback_runner = FakeDeploymentRunner()

        deploy_record = deploy_config_version(
            current_version,
            operator=self.operator,
            reload_confirmed=True,
            runner=deploy_runner,
        )
        rollback_record = deploy_config_version(
            previous_version,
            operator=self.operator,
            reload_confirmed=True,
            rollback=True,
            runner=rollback_runner,
        )

        deploy_record.refresh_from_db()
        rollback_record.refresh_from_db()
        previous_version.refresh_from_db()
        current_version.refresh_from_db()
        self.hq.refresh_from_db()
        self.assertEqual(deploy_record.action, DeploymentRecord.Action.DEPLOY)
        self.assertEqual(deploy_record.status, DeploymentRecord.Status.SUCCESS)
        self.assertEqual(deploy_record.target_host, self.hq.deployment_ssh_host)
        self.assertEqual(deploy_record.reload_result, DeploymentRecord.ReloadResult.SUCCESS)
        self.assertEqual(deploy_record.reload_output, "Reload OK")
        self.assertEqual(
            [step["name"] for step in deploy_record.details["steps"]],
            ["prepare_staging", "upload_bundle", "verify_staging", "swap_volumes", "reload_asterisk"],
        )
        self.assertIn("asterisk/pjsip.conf", deploy_runner.uploads[0]["asterisk_files"])
        self.assertIn("asterisk/pbx-active-config.json", deploy_runner.uploads[0]["asterisk_files"])
        self.assertIn("tftp/company-directory.xml", deploy_runner.uploads[0]["tftp_files"])
        self.assertIn("/srv/pbx/current/asterisk", deploy_runner.remote_commands[2])
        self.assertIn("/srv/pbx/current/tftp", deploy_runner.remote_commands[2])
        self.assertEqual(current_version.deployment_status, ConfigVersion.DeploymentStatus.DEPLOYED)
        self.assertEqual(rollback_record.action, DeploymentRecord.Action.ROLLBACK)
        self.assertEqual(rollback_record.config_version, previous_version)
        self.assertEqual(rollback_record.rollback_source_version, previous_version)
        self.assertEqual(previous_version.deployment_status, ConfigVersion.DeploymentStatus.ROLLED_BACK)
        self.assertEqual(self.hq.deployment_status, Location.DeploymentStatus.DEPLOYED)

    def test_audio_conversion_harness_covers_wav_mp3_and_m4a(self):
        commands = []

        def fake_ffmpeg(command, *, capture_output, text, check):
            commands.append(command)
            Path(command[-1]).write_bytes(b"RIFFasterisk-wav")
            return subprocess.CompletedProcess(command, 0, stdout="", stderr="")

        with tempfile.TemporaryDirectory() as media_root, override_settings(MEDIA_ROOT=media_root):
            prompts = [
                create_audio_prompt_from_upload(
                    location=self.hq,
                    uploaded_file=SimpleUploadedFile(filename, b"source-audio", content_type=content_type),
                    runner=fake_ffmpeg,
                )
                for filename, content_type in (
                    ("integration-menu.wav", "audio/wav"),
                    ("integration-menu.mp3", "audio/mpeg"),
                    ("integration-menu.m4a", "audio/mp4"),
                )
            ]

        self.assertEqual([prompt.source_format for prompt in prompts], ["wav", "mp3", "m4a"])
        self.assertEqual([prompt.converted_format for prompt in prompts], ["wav", "wav", "wav"])
        for prompt in prompts:
            self.assertEqual(prompt.sample_rate_hz, 8000)
            self.assertEqual(prompt.channels, 1)
            self.assertTrue(prompt.converted_file.name.endswith(".wav"))
        for command in commands:
            self.assertIn("pcm_s16le", command)
            self.assertIn("-ar", command)
            self.assertIn("8000", command)
            self.assertIn("-ac", command)
            self.assertIn("1", command)


class ConfigDeploymentServiceTests(TestCase):
    def setUp(self):
        self.operator = User.objects.create_user(username="deploy-service", password="portal-pass")
        self.location = Location.objects.create(
            **location_model_data(
                name="Deploy Service HQ",
                slug="deploy-service-hq",
                deployment_staging_path="/srv/pbx/releases",
                deployment_asterisk_path="/srv/pbx/current/asterisk",
                deployment_tftp_path="/srv/pbx/current/tftp",
            )
        )
        Extension.objects.create(
            location=self.location,
            number="3000",
            display_name="Deploy Service Desk",
            sip_username="3000",
            sip_password="sip-secret",
            emergency_calling_enabled=True,
        )
        add_emergency_route(self.location)

    def test_ssh_staging_layout_reload_and_audit_record(self):
        version = create_config_version(self.location, exported_by=self.operator)
        runner = FakeDeploymentRunner()

        record = deploy_config_version(
            version,
            operator=self.operator,
            reload_confirmed=True,
            runner=runner,
        )

        record.refresh_from_db()
        version.refresh_from_db()
        self.location.refresh_from_db()
        self.assertEqual(record.operator, self.operator)
        self.assertEqual(record.target_host, self.location.deployment_ssh_host)
        self.assertEqual(record.config_version, version)
        self.assertEqual(record.status, DeploymentRecord.Status.SUCCESS)
        self.assertEqual(record.reload_result, DeploymentRecord.ReloadResult.SUCCESS)
        self.assertEqual(record.reload_output, "Reload OK")
        self.assertTrue(record.staging_path.startswith("/srv/pbx/releases/deploy-service-hq/v1-"))
        self.assertEqual(record.asterisk_path, "/srv/pbx/current/asterisk")
        self.assertEqual(record.tftp_path, "/srv/pbx/current/tftp")
        self.assertEqual(version.deployment_status, ConfigVersion.DeploymentStatus.DEPLOYED)
        self.assertEqual(self.location.deployment_status, Location.DeploymentStatus.DEPLOYED)
        self.assertIsNotNone(self.location.last_deployed_at)
        self.assertEqual(
            [step["name"] for step in record.details["steps"]],
            ["prepare_staging", "upload_bundle", "verify_staging", "swap_volumes", "reload_asterisk"],
        )
        self.assertEqual(len(runner.uploads), 1)
        self.assertIn("asterisk/pjsip.conf", runner.uploads[0]["asterisk_files"])
        self.assertIn("asterisk/pbx-active-config.json", runner.uploads[0]["asterisk_files"])
        self.assertIn("tftp/company-directory.xml", runner.uploads[0]["tftp_files"])
        self.assertIn("scripts/pbx-recording-retention", runner.uploads[0]["scripts_files"])
        self.assertIn("/srv/pbx/current/asterisk", runner.remote_commands[2])
        self.assertIn("/srv/pbx/current/tftp", runner.remote_commands[2])
        self.assertIn("/usr/local/sbin/pbx-recording-retention", runner.remote_commands[2])
        self.assertIn("umask 077", runner.remote_commands[0])
        self.assertIn("mkdir -p -m 700", runner.remote_commands[0])
        self.assertIn("chmod 700", runner.remote_commands[0])

        audit = AuditLog.objects.get(action=AuditAction.DEPLOYMENT)
        self.assertEqual(audit.actor, self.operator)
        self.assertEqual(audit.outcome, AuditOutcome.SUCCESS)
        self.assertEqual(audit.details["deployment_record_id"], record.id)
        self.assertEqual(audit.details["target_host"], self.location.deployment_ssh_host)
        self.assertEqual(audit.details["reload_result"], DeploymentRecord.ReloadResult.SUCCESS)

    def test_reload_failure_records_failed_deployment_and_audit(self):
        version = create_config_version(self.location, exported_by=self.operator)
        runner = FakeDeploymentRunner(fail_step="reload_asterisk")

        with self.assertRaises(DeploymentError):
            deploy_config_version(
                version,
                operator=self.operator,
                reload_confirmed=True,
                runner=runner,
            )

        record = DeploymentRecord.objects.get(config_version=version)
        self.location.refresh_from_db()
        version.refresh_from_db()
        self.assertEqual(record.status, DeploymentRecord.Status.FAILED)
        self.assertEqual(record.reload_result, DeploymentRecord.ReloadResult.FAILED)
        self.assertEqual(record.reload_output, "reload failed")
        self.assertEqual(self.location.deployment_status, Location.DeploymentStatus.FAILED)
        self.assertEqual(version.deployment_status, ConfigVersion.DeploymentStatus.NOT_DEPLOYED)
        audit = AuditLog.objects.get(action=AuditAction.DEPLOYMENT)
        self.assertEqual(audit.outcome, AuditOutcome.FAILURE)
        self.assertEqual(audit.details["status"], DeploymentRecord.Status.FAILED)

    def test_rollback_redeploys_previous_version_and_links_source_version(self):
        previous_version = create_config_version(self.location, exported_by=self.operator)
        current_version = create_config_version(self.location, exported_by=self.operator)
        self.assertEqual(current_version.version_number, 2)
        runner = FakeDeploymentRunner()

        record = deploy_config_version(
            previous_version,
            operator=self.operator,
            reload_confirmed=True,
            rollback=True,
            runner=runner,
        )

        record.refresh_from_db()
        previous_version.refresh_from_db()
        self.assertEqual(record.action, DeploymentRecord.Action.ROLLBACK)
        self.assertEqual(record.config_version, previous_version)
        self.assertEqual(record.rollback_source_version, previous_version)
        self.assertEqual(previous_version.deployment_status, ConfigVersion.DeploymentStatus.ROLLED_BACK)
        audit = AuditLog.objects.get(action=AuditAction.DEPLOYMENT)
        self.assertEqual(audit.outcome, AuditOutcome.SUCCESS)
        self.assertEqual(audit.details["action"], DeploymentRecord.Action.ROLLBACK)
        self.assertEqual(audit.details["rollback_source_version_id"], previous_version.id)

    def test_deployment_sensitive_files_and_staging_are_restricted(self):
        version = create_config_version(self.location, exported_by=self.operator)

        with tempfile.TemporaryDirectory() as temp_dir:
            workspace = Path(temp_dir)
            runner = SSHDeploymentRunner.from_location(self.location, workspace)
            extracted = extract_deployment_bundle(version, workspace / "bundle")

            self.assertTrue(runner.key_path.exists())
            self.assertTrue(runner.known_hosts_path.exists())
            self.assertIn("asterisk/pjsip.conf", extracted)
            self.assertIn("asterisk/pbx-active-config.json", extracted)
            self.assertIn("tftp/company-directory.xml", extracted)
            self.assertIn("scripts/pbx-recording-retention", extracted)
            if os.name == "posix":
                self.assertEqual(runner.key_path.stat().st_mode & 0o777, 0o600)
                self.assertEqual(runner.known_hosts_path.stat().st_mode & 0o777, 0o600)
                self.assertEqual((workspace / "bundle").stat().st_mode & 0o777, 0o700)
                self.assertEqual((workspace / "bundle" / "asterisk").stat().st_mode & 0o777, 0o700)
                self.assertEqual((workspace / "bundle" / "asterisk" / "pjsip.conf").stat().st_mode & 0o777, 0o600)
                self.assertEqual((workspace / "bundle" / "scripts" / "pbx-recording-retention").stat().st_mode & 0o777, 0o700)

    def test_deployment_rejects_unsafe_remote_roots_before_commands_run(self):
        version = create_config_version(self.location, exported_by=self.operator)
        self.location.deployment_staging_path = "/tmp/pbx/releases"
        self.location.save(update_fields=["deployment_staging_path", "updated_at"])
        runner = FakeDeploymentRunner()

        with self.assertRaises(DeploymentError) as context:
            deploy_config_version(
                version,
                operator=self.operator,
                reload_confirmed=True,
                runner=runner,
            )

        self.assertIn("allowed deployment root", str(context.exception))
        self.assertEqual(runner.remote_commands, [])
        record = DeploymentRecord.objects.get(config_version=version)
        self.assertEqual(record.status, DeploymentRecord.Status.FAILED)

    def test_upload_bundle_restricts_remote_staging_permissions(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            bundle_dir = Path(temp_dir)
            (bundle_dir / "asterisk").mkdir()
            (bundle_dir / "tftp").mkdir()
            (bundle_dir / "asterisk" / "pjsip.conf").write_text("[transport]\n", encoding="utf-8")
            (bundle_dir / "tftp" / "company-directory.xml").write_text("<directory />\n", encoding="utf-8")
            runner = SSHDeploymentRunner(
                host="pbx.example.test",
                port=22,
                username="deploy",
                key_path=bundle_dir / "deployment_key",
            )
            calls = []

            def fake_run(command, **kwargs):
                calls.append((command, kwargs))
                if command[0] == "tar":
                    return subprocess.CompletedProcess(command, 0, stdout=b"tarball", stderr=b"")
                return subprocess.CompletedProcess(command, 0, stdout=b"", stderr=b"")

            with mock.patch("core.deployments.subprocess.run", side_effect=fake_run):
                result = runner.upload_bundle(bundle_dir, "/srv/pbx/staging/site/v1")

            self.assertEqual(result.returncode, 0)
            remote_command = calls[1][0][-1]
            self.assertIn("umask 077", remote_command)
            self.assertIn("mkdir -p -m 700 /srv/pbx/staging/site/v1", remote_command)
            self.assertIn("tar -xzf - -C /srv/pbx/staging/site/v1", remote_command)
            self.assertIn("chmod -R go-rwx /srv/pbx/staging/site/v1", remote_command)


class ConfigExportGoldenFileTests(TestCase):
    maxDiff = None

    def test_export_archive_manifest_checksum_zip_and_staging_golden_files(self):
        archive = self._build_golden_archive()

        with zipfile.ZipFile(BytesIO(archive.archive_bytes)) as zip_archive:
            manifest_content = zip_archive.read("manifest.json").decode("utf-8")
            checksum_content = zip_archive.read("SHA256SUMS").decode("utf-8")

        self.assertEqual(manifest_content, self._golden("manifest.json"))
        self.assertEqual(json.loads(manifest_content), archive.manifest)
        self.assertEqual(checksum_content, self._golden("SHA256SUMS"))
        self.assertEqual(self._zip_layout(archive.archive_bytes), self._golden("zip-layout.txt"))

        with tempfile.TemporaryDirectory() as output_dir:
            write_config_version_directory(ConfigVersion(archive=archive.archive_bytes), output_dir)

            self.assertEqual(self._staging_layout(output_dir), self._golden("staging-layout.txt"))

        self.assertEqual(archive.checksum, hashlib.sha256(archive.archive_bytes).hexdigest())

    def _build_golden_archive(self):
        user = User.objects.create_user(username="exporter", password="portal-pass")
        location = Location.objects.create(
            id=101,
            **location_model_data(
                name="Golden HQ",
                slug="golden-hq",
                lan_subnet="10.60.0.0/24",
                pbx_lan_ip="10.60.0.10",
                pbx_warp_ip="100.64.60.10",
                sip_bind_ip="10.60.0.10",
                iax_bind_ip="10.60.0.10",
                default_did="+15551206000",
                emergency_caller_id="+15551206999",
                recording_retention_days=30,
                ami_username="ami-golden",
                ami_secret="ami-golden-secret",
                agent_token="agent-token",
                agent_secret="golden-agent-secret",
            ),
        )
        Extension.objects.create(
            location=location,
            number="6200",
            display_name="Golden Desk",
            sip_username="6200",
            sip_password="sip-secret-6200",
            voicemail_pin="2468",
            emergency_calling_enabled=True,
        )
        add_emergency_route(location)
        return build_config_export_archive(
            location,
            version_number=1,
            exported_at=datetime(2026, 1, 2, 3, 4, 5, tzinfo=datetime_timezone.utc),
            exported_by=user,
            require_emergency=True,
        )

    def _zip_layout(self, archive_bytes):
        lines = []
        with zipfile.ZipFile(BytesIO(archive_bytes)) as zip_archive:
            for zip_info in zip_archive.infolist():
                content = zip_archive.read(zip_info.filename)
                lines.append(
                    (
                        f"{zip_info.filename}|size={len(content)}|"
                        f"sha256={hashlib.sha256(content).hexdigest()}|"
                        f"date={zip_info.date_time}|mode={oct(zip_info.external_attr >> 16)}"
                    )
                )
        return "\n".join(lines) + "\n"

    def _staging_layout(self, output_dir):
        lines = []
        root = Path(output_dir)
        files = sorted(
            (path for path in root.rglob("*") if path.is_file()),
            key=lambda path: path.relative_to(root).as_posix(),
        )
        for path in files:
            content = path.read_bytes()
            mode = path.stat().st_mode & 0o777
            relative_path = path.relative_to(root).as_posix()
            if relative_path.startswith("scripts/"):
                mode = 0o700
            elif os.name != "posix":
                mode = 0o600
            lines.append(
                (
                    f"{relative_path}|size={len(content)}|"
                    f"sha256={hashlib.sha256(content).hexdigest()}|mode={oct(mode)}"
                )
            )
        return "\n".join(lines) + "\n"

    def _golden(self, filename):
        return (Path(__file__).with_name("testdata") / "export_archive" / filename).read_text(encoding="utf-8")


class ExportValidationEngineTests(TestCase):
    def test_export_command_hard_blocks_missing_emergency_route_and_caller_id(self):
        location = Location.objects.create(
            **location_model_data(name="Blocked HQ", slug="blocked-hq", emergency_caller_id="")
        )
        Extension.objects.create(location=location, number="3000", display_name="HQ Desk")

        with self.assertRaises(CommandError) as context:
            call_command("export_pbx_config", location.slug, stdout=StringIO())

        self.assertIn("missing_emergency_caller_id", str(context.exception))
        self.assertIn("missing_emergency_route", str(context.exception))
        audit_log = AuditLog.objects.get(target="locations/blocked-hq/config")
        self.assertEqual(audit_log.action, AuditAction.CONFIG_EXPORT)
        self.assertEqual(audit_log.outcome, AuditOutcome.FAILURE)
        self.assertEqual(
            {error["code"] for error in audit_log.details["validation"]["errors"]},
            {"missing_emergency_caller_id", "missing_emergency_route"},
        )
        self.assertEqual(
            audit_log.details["validation"]["errors"][0]["affected_extensions"],
            ["3000"],
        )

    def test_disabled_911_override_excludes_extension_and_surfaces_dialplan_warning(self):
        location = Location.objects.create(
            **location_model_data(name="Disabled HQ", slug="disabled-hq", emergency_caller_id="")
        )
        Extension.objects.create(
            location=location,
            number="3000",
            display_name="Disabled Desk",
            emergency_calling_enabled=False,
        )
        output = StringIO()

        call_command("export_pbx_config", location.slug, stdout=output)

        payload = json.loads(output.getvalue())
        self.assertEqual(payload["routing_validation"]["errors"], [])
        self.assertEqual(payload["dialplan_warnings"][0]["code"], "extension_911_disabled")
        audit_log = AuditLog.objects.get(target="locations/disabled-hq/config")
        self.assertEqual(audit_log.outcome, AuditOutcome.SUCCESS)
        self.assertEqual(
            audit_log.details["validation"]["warnings"][0]["code"],
            "extension_911_disabled",
        )

        viewer = User.objects.create_user(username="disabled-viewer", password="portal-pass")
        assign_role(viewer, PortalRole.VIEWER)
        self.client.force_login(viewer)
        response = self.client.get(reverse("dial-plan"))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "911 calling is disabled for extension 3000 by Admin override")

    def test_over_five_registration_warning(self):
        location = Location.objects.create(**location_model_data(name="Phone HQ", slug="phone-hq"))
        extension = Extension.objects.create(location=location, number="3000", display_name="HQ Desk")
        for index in range(6):
            phone = Phone.objects.create(
                location=location,
                mac_address=f"0011223344{index:02X}",
                model=Phone.PhoneModel.CISCO_9971,
                firmware_load_name="sip9971.9-4-2",
            )
            PhoneLineAppearance.objects.create(phone=phone, extension=extension, line_index=1)

        validation = validate_location_routing(location)

        warning = next(
            warning
            for warning in validation["warnings"]
            if warning["code"] == "extension_over_phone_appearance_limit"
        )
        self.assertEqual(warning["extension"], "3000")
        self.assertEqual(warning["appearance_count"], 6)
        self.assertEqual(warning["limit"], 5)

    def test_validation_warning_snapshot_covers_export_warning_categories(self):
        location = Location.objects.create(
            **location_model_data(
                name="Warning HQ",
                slug="warning-hq",
                smtp_host="",
                smtp_from_email="",
                smtp_username="",
                smtp_password="",
            )
        )
        Extension.objects.create(
            location=location,
            number="3000",
            display_name="Voicemail Desk",
            email="desk@example.test",
            voicemail_enabled=True,
        )
        disabled_extension = Extension.objects.create(
            location=location,
            number="3001",
            display_name="No 911 Desk",
            emergency_calling_enabled=False,
        )
        over_limit_extension = Extension.objects.create(
            location=location,
            number="3002",
            display_name="Busy Desk",
        )
        provider = Provider.objects.create(
            name="Warning SIP",
            slug="warning-sip",
            provider_type=Provider.ProviderType.SIP,
        )
        Trunk.objects.create(
            location=location,
            provider=provider,
            name="Missing Secret",
            trunk_type=Trunk.TrunkType.SIP,
            host="sip.warning.example.test",
            username="warning",
            password="",
        )
        DID.objects.create(location=location, number="15551230001")
        Phone.objects.create(
            location=location,
            mac_address="001122000000",
            model=Phone.PhoneModel.CISCO_9971,
        )
        for index in range(6):
            phone = Phone.objects.create(
                location=location,
                mac_address=f"0011224455{index:02X}",
                model=Phone.PhoneModel.CISCO_9971,
                firmware_load_name="sip9971.9-4-2",
            )
            PhoneLineAppearance.objects.create(phone=phone, extension=over_limit_extension, line_index=1)
        IVR.objects.create(location=location, name="Main IVR")
        CallQueue.objects.create(location=location, name="Support Queue")
        FeatureCode.objects.create(
            location=location,
            code="*8",
            name="Pickup",
            feature_type=FeatureCode.FeatureType.CALL_PICKUP,
        )

        config = build_location_config(location)

        self.assertEqual(config["routing_validation"]["errors"], [])
        self.assertEqual(
            [warning["code"] for warning in config["routing_validation"]["warnings"]],
            [
                "provider_trunk_missing_credentials",
                "suspicious_did",
                "phone_incomplete",
                "phone_missing_firmware_load_name",
                "extension_over_phone_appearance_limit",
                "smtp_not_configured",
                "did_missing_fallback_destination",
                "ivr_incomplete_fallback_destinations",
                "queue_missing_overflow_destination",
                "feature_code_missing_destination",
                "extension_911_disabled",
            ],
        )
        self.assertEqual(config["routing_validation"]["warnings"][0]["missing"], ["password"])
        self.assertEqual(config["routing_validation"]["warnings"][1]["reason"], "missing_plus_prefix")
        self.assertEqual(config["routing_validation"]["warnings"][5]["affected_extensions"], ["3000"])
        self.assertEqual(config["routing_validation"]["warnings"][-1]["extension"], disabled_extension.number)
        self.assertEqual(config["dialplan_warnings"], config["routing_validation"]["warnings"])


class AudioPromptConversionTests(TestCase):
    def setUp(self):
        self.location = Location.objects.create(**location_model_data(name="HQ", slug="hq"))
        self.temp_dir = tempfile.TemporaryDirectory()
        self.settings_override = override_settings(MEDIA_ROOT=self.temp_dir.name)
        self.settings_override.enable()
        self.addCleanup(self.settings_override.disable)
        self.addCleanup(self.temp_dir.cleanup)
        self.commands = []

    def _upload(self, filename, content_type):
        return SimpleUploadedFile(filename, b"source-audio", content_type=content_type)

    def _fake_ffmpeg(self, command, *, capture_output, text, check):
        self.commands.append(command)
        Path(command[-1]).write_bytes(b"RIFFasterisk-wav")
        return subprocess.CompletedProcess(command, 0, stdout="", stderr="")

    def test_wav_mp3_and_m4a_uploads_are_converted(self):
        uploads = [
            ("main-menu.wav", "audio/wav", "wav"),
            ("main-menu.mp3", "audio/mpeg", "mp3"),
            ("main-menu.m4a", "audio/mp4", "m4a"),
        ]

        with mock.patch("core.audio_prompts.subprocess.run", side_effect=self._fake_ffmpeg):
            prompts = [
                create_audio_prompt_from_upload(location=self.location, uploaded_file=self._upload(filename, content_type))
                for filename, content_type, _source_format in uploads
            ]

        self.assertEqual([prompt.source_format for prompt in prompts], ["wav", "mp3", "m4a"])
        for prompt in prompts:
            self.assertEqual(prompt.converted_format, "wav")
            self.assertEqual(prompt.sample_rate_hz, 8000)
            self.assertEqual(prompt.channels, 1)
            self.assertTrue(prompt.converted_file.name.endswith(".wav"))
            self.assertTrue(prompt.asterisk_path.endswith(".wav"))
            self.assertFalse(prompt.playback_name.endswith(".wav"))
            if os.name == "posix":
                self.assertEqual(Path(prompt.original_file.path).stat().st_mode & 0o777, 0o600)
                self.assertEqual(Path(prompt.converted_file.path).stat().st_mode & 0o777, 0o600)
        for command in self.commands:
            self.assertIn("-ar", command)
            self.assertIn("8000", command)
            self.assertIn("-ac", command)
            self.assertIn("1", command)
            self.assertIn("pcm_s16le", command)

    def test_failed_conversion_returns_actionable_error(self):
        def failing_ffmpeg(command, *, capture_output, text, check):
            return subprocess.CompletedProcess(command, 1, stdout="", stderr="unsupported codec in fixture")

        with mock.patch("core.audio_prompts.subprocess.run", side_effect=failing_ffmpeg):
            with self.assertRaises(AudioPromptConversionError) as context:
                create_audio_prompt_from_upload(
                    location=self.location,
                    uploaded_file=self._upload("broken.mp3", "audio/mpeg"),
                )

        self.assertIn("Could not convert audio prompt", str(context.exception))
        self.assertIn("unsupported codec", str(context.exception))

    def test_invalid_prompt_upload_is_rejected_by_ivr_form(self):
        form = IVRForm(
            data={
                "location": str(self.location.id),
                "name": "Main IVR",
                "prompt": "",
                "prompt_name": "",
                "business_hours_destination": "",
                "after_hours_destination": "",
                "timeout_seconds": "10",
                "timeout_destination": "",
                "invalid_destination": "",
                "is_active": "on",
            },
            files={"prompt_upload": SimpleUploadedFile("notes.txt", b"not audio", content_type="text/plain")},
        )

        self.assertFalse(form.is_valid())
        self.assertIn("WAV, MP3, or M4A", form.errors["prompt_upload"][0])


class AudioPromptIVRIntegrationTests(TestCase):
    def setUp(self):
        self.editor = User.objects.create_user(username="prompt-editor", password="portal-pass")
        assign_role(self.editor, PortalRole.EDITOR)
        self.location = Location.objects.create(**location_model_data(name="HQ", slug="hq"))
        self.temp_dir = tempfile.TemporaryDirectory()
        self.settings_override = override_settings(MEDIA_ROOT=self.temp_dir.name)
        self.settings_override.enable()
        self.addCleanup(self.settings_override.disable)
        self.addCleanup(self.temp_dir.cleanup)

    def _fake_ffmpeg(self, command, *, capture_output, text, check):
        Path(command[-1]).write_bytes(b"RIFFasterisk-wav")
        return subprocess.CompletedProcess(command, 0, stdout="", stderr="")

    def _ivr_post_data(self, upload):
        data = {
            "location": str(self.location.id),
            "name": "Main IVR",
            "prompt": "",
            "prompt_name": "",
            "business_hours_destination": "",
            "after_hours_destination": "",
            "timeout_seconds": "12",
            "timeout_destination": "",
            "invalid_destination": "",
            "is_active": "on",
            "prompt_upload": upload,
        }
        data.update(ivr_menu_formset_data(total_forms=1))
        return data

    def test_ivr_upload_references_converted_prompt_in_export(self):
        self.client.force_login(self.editor)
        upload = SimpleUploadedFile("main-menu.wav", b"source-audio", content_type="audio/wav")

        with mock.patch("core.audio_prompts.subprocess.run", side_effect=self._fake_ffmpeg):
            response = self.client.post(reverse("ivr-create"), self._ivr_post_data(upload))

        self.assertEqual(response.status_code, 302)
        ivr = IVR.objects.select_related("prompt").get(name="Main IVR")
        self.assertIsNotNone(ivr.prompt)
        self.assertEqual(ivr.prompt_name, ivr.prompt.playback_name)

        inbound_config = build_location_config(self.location)["inbound"]
        ivr_config = inbound_config["ivrs"][0]
        prompt_config = inbound_config["audio_prompts"][0]
        self.assertEqual(ivr_config["prompt"]["id"], ivr.prompt_id)
        self.assertEqual(ivr_config["prompt_name"], ivr.prompt.playback_name)
        self.assertEqual(prompt_config["asterisk_path"], ivr.prompt.asterisk_path)
        self.assertEqual(prompt_config["playback_name"], ivr.prompt.playback_name)

    def test_ivr_upload_conversion_failure_returns_form_error(self):
        self.client.force_login(self.editor)
        upload = SimpleUploadedFile("broken.m4a", b"source-audio", content_type="audio/mp4")

        def failing_ffmpeg(command, *, capture_output, text, check):
            return subprocess.CompletedProcess(command, 1, stdout="", stderr="unsupported codec in fixture")

        with mock.patch("core.audio_prompts.subprocess.run", side_effect=failing_ffmpeg):
            response = self.client.post(reverse("ivr-create"), self._ivr_post_data(upload))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "unsupported codec")
        self.assertEqual(AudioPrompt.objects.count(), 0)


class CiscoTFTPProvisioningTests(TestCase):
    maxDiff = None

    def test_mac_to_sep_filename_normalizes_supported_formats(self):
        self.assertEqual(mac_to_sep_filename("sep00:11:22:aa:bb:cc"), "SEP001122AABBCC.cnf.xml")
        self.assertEqual(mac_to_sep_filename("0011.2233.4455"), "SEP001122334455.cnf.xml")

    def test_generates_golden_sep_files_for_each_supported_cisco_model(self):
        location = self._create_location()
        model_cases = [
            (Phone.PhoneModel.CISCO_9971, "Cisco CP-9971", "001122334401", "sip9971.9-4-2", "5101"),
            (Phone.PhoneModel.CISCO_9951, "Cisco CP-9951", "001122334402", "sip9951.9-4-2", "5102"),
            (Phone.PhoneModel.CISCO_8961, "Cisco CP-8961", "001122334403", "sip8961.9-4-2", "5103"),
        ]
        for model, _product, mac_address, load_name, extension_number in model_cases:
            extension = Extension.objects.create(
                location=location,
                number=extension_number,
                display_name=f"Desk {extension_number}",
                sip_username=f"sip{extension_number}",
                sip_password=f"secret{extension_number}",
            )
            phone = Phone.objects.create(
                location=location,
                mac_address=mac_address,
                model=model,
                firmware_load_name=load_name,
                label=f"{model} Phone",
            )
            PhoneLineAppearance.objects.create(phone=phone, extension=extension, line_index=1, label="Primary")

        inactive_phone = Phone.objects.create(
            location=location,
            mac_address="001122334499",
            model=Phone.PhoneModel.CISCO_9971,
            firmware_load_name="sip9971.9-4-2",
            label="Inactive",
            is_active=False,
        )
        PhoneLineAppearance.objects.create(
            phone=inactive_phone,
            extension=Extension.objects.create(location=location, number="5199", display_name="Inactive Desk"),
            line_index=1,
        )

        tftp = build_location_config(location)["tftp"]
        files = self._file_map(tftp)

        self.assertEqual(
            [phone_file["filename"] for phone_file in tftp["phone_files"]],
            [
                "SEP001122334401.cnf.xml",
                "SEP001122334402.cnf.xml",
                "SEP001122334403.cnf.xml",
            ],
        )
        for model, _product, mac_address, _load_name, _extension_number in model_cases:
            with self.subTest(model=model):
                filename = mac_to_sep_filename(mac_address)
                self.assertEqual(
                    files[filename]["content"],
                    self._cisco_golden("supported_models", filename),
                )

    def test_multiline_and_speed_dial_golden_config(self):
        location = self._create_location()
        reception = Extension.objects.create(
            location=location,
            number="5100",
            display_name="Reception",
            sip_username="sip5100",
            sip_password="secret5100",
        )
        sales = Extension.objects.create(
            location=location,
            number="5101",
            display_name="Sales",
            sip_password="secret5101",
        )
        phone = Phone.objects.create(
            location=location,
            mac_address="001122334455",
            model=Phone.PhoneModel.CISCO_9971,
            firmware_load_name="sip9971.9-4-2",
            label="Reception Phone",
        )
        PhoneLineAppearance.objects.create(phone=phone, extension=reception, line_index=1, label="Primary")
        PhoneLineAppearance.objects.create(phone=phone, extension=sales, line_index=2, label="Sales")
        PhoneSpeedDial.objects.create(phone=phone, position=1, label="Support", destination="5101")

        files = self._file_map(build_location_config(location)["tftp"])

        self.assertEqual(
            files["SEP001122334455.cnf.xml"]["content"],
            self._cisco_golden("multiline_speed_dial.xml"),
        )

    def test_company_directory_golden_xml_groups_active_extensions_by_location(self):
        hq = self._create_location(name="Provision HQ", slug="provision-hq", pbx_lan_ip="10.50.0.10")
        warehouse = self._create_location(
            name="Provision Warehouse",
            slug="provision-warehouse",
            pbx_lan_ip="10.51.0.10",
            pbx_warp_ip="100.64.51.10",
            lan_subnet="10.51.0.0/24",
        )
        Extension.objects.create(location=hq, number="5100", display_name="Reception")
        Extension.objects.create(location=hq, number="5101", display_name="Sales")
        Extension.objects.create(location=warehouse, number="6100", display_name="Warehouse Desk")
        Extension.objects.create(location=warehouse, number="6101", display_name="Inactive Desk", is_active=False)

        files = self._file_map(build_location_config(hq)["tftp"])

        self.assertEqual(
            files["company-directory.xml"]["content"],
            self._cisco_golden("company-directory.xml"),
        )

    def test_firmware_placeholder_and_checklist_files_are_in_tftp_output(self):
        location = self._create_location()
        extension = Extension.objects.create(location=location, number="5100", display_name="Reception")
        phone = Phone.objects.create(
            location=location,
            mac_address="001122334455",
            model=Phone.PhoneModel.CISCO_9971,
            firmware_load_name="sip9971.9-4-2",
        )
        PhoneLineAppearance.objects.create(phone=phone, extension=extension, line_index=1)

        tftp = build_location_config(location)["tftp"]
        files = self._file_map(tftp)

        self.assertFalse(tftp["firmware"]["bundled"])
        self.assertIn("firmware/CISCO-FIRMWARE-CHECKLIST.txt", files)
        self.assertIn("firmware/README-no-firmware-bundled.txt", files)
        self.assertIn("This export does not bundle Cisco firmware.", files["firmware/CISCO-FIRMWARE-CHECKLIST.txt"]["content"])
        self.assertIn("SEP001122334455 (CP-9971): sip9971.9-4-2", files["firmware/CISCO-FIRMWARE-CHECKLIST.txt"]["content"])
        self.assertIn("No firmware binaries are included", files["firmware/README-no-firmware-bundled.txt"]["content"])

    def _create_location(self, **overrides):
        data = location_model_data(
            name="Provision HQ",
            slug="provision-hq",
            pbx_lan_ip="10.50.0.10",
            pbx_warp_ip="100.64.50.10",
            lan_subnet="10.50.0.0/24",
            sip_bind_ip="10.50.0.10",
            iax_bind_ip="10.50.0.10",
            default_did="+15551205000",
            emergency_caller_id="+15551205999",
            emergency_trunk="Provision SIP",
        )
        data.update(overrides)
        data.setdefault("sip_bind_ip", data["pbx_lan_ip"])
        data.setdefault("iax_bind_ip", data["pbx_lan_ip"])
        return Location.objects.create(**data)

    def _file_map(self, tftp):
        return {file["path"]: file for file in tftp["files"]}

    def _cisco_golden(self, *parts):
        return (
            Path(__file__).with_name("testdata") / "cisco_tftp" / Path(*parts)
        ).read_text(encoding="utf-8").removesuffix("\n")


class PhoneMACValidationTests(TestCase):
    def setUp(self):
        self.location = Location.objects.create(**location_model_data(name="HQ", slug="hq"))

    def test_phone_save_normalizes_mac_for_sep_provisioning(self):
        phone = Phone.objects.create(
            location=self.location,
            mac_address="sep00:11:22:aa:bb:cc",
            model=Phone.PhoneModel.CISCO_9951,
            label="Reception",
        )

        self.assertEqual(phone.mac_address, "001122AABBCC")
        self.assertEqual(phone.sep_identifier, "SEP001122AABBCC")

    def test_phone_rejects_invalid_mac_values(self):
        with self.assertRaises(ValidationError):
            Phone.objects.create(location=self.location, mac_address="00:11:22:33:44:ZZ")

    def test_phone_form_normalizes_mac_and_limits_cisco_models(self):
        form = PhoneForm(
            data=phone_form_data(
                self.location,
                mac_address="0011.2233.4455",
                model=Phone.PhoneModel.CISCO_8961,
            )
        )

        self.assertTrue(form.is_valid(), form.errors)
        self.assertEqual(form.cleaned_data["mac_address"], "001122334455")
        self.assertEqual(
            {choice[0] for choice in form.fields["model"].choices},
            {
                Phone.PhoneModel.CISCO_9971,
                Phone.PhoneModel.CISCO_9951,
                Phone.PhoneModel.CISCO_8961,
            },
        )


class PhoneManagementViewTests(TestCase):
    def setUp(self):
        self.viewer = User.objects.create_user(username="phone-viewer", password="portal-pass")
        self.editor = User.objects.create_user(username="phone-editor", password="portal-pass")
        assign_role(self.viewer, PortalRole.VIEWER)
        assign_role(self.editor, PortalRole.EDITOR)
        self.location = Location.objects.create(**location_model_data(name="HQ", slug="hq"))
        self.reception = Extension.objects.create(
            location=self.location,
            number="3000",
            display_name="Reception",
        )
        self.sales = Extension.objects.create(
            location=self.location,
            number="3001",
            display_name="Sales",
        )

    def test_phone_list_route_shows_inventory_and_csv_tooling(self):
        phone = Phone.objects.create(
            location=self.location,
            mac_address="001122334455",
            model=Phone.PhoneModel.CISCO_9971,
            label="Reception Phone",
        )
        PhoneLineAppearance.objects.create(phone=phone, extension=self.reception, line_index=1, label="Primary")
        PhoneSpeedDial.objects.create(phone=phone, position=1, label="Support", destination="3001")
        self.client.force_login(self.viewer)

        response = self.client.get(reverse("phones"))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'data-area="phones"')
        self.assertContains(response, "Phone Inventory")
        self.assertContains(response, "SEP001122334455")
        self.assertContains(response, "Line 1: 3000")
        self.assertContains(response, "Support -> 3001")
        self.assertContains(response, "Phones Template")
        self.assertContains(response, "DIDs Template")
        self.assertContains(response, "Speed Dials Template")

    def test_viewer_cannot_create_phone(self):
        self.client.force_login(self.viewer)

        response = self.client.get(reverse("phone-create"))

        self.assertEqual(response.status_code, 403)

    def test_editor_can_create_phone_with_multiple_lines_and_speed_dials(self):
        self.client.force_login(self.editor)
        post_data = phone_form_data(
            self.location,
            mac_address="SEP00-11-22-33-44-55",
            model=Phone.PhoneModel.CISCO_9951,
            label="Lobby",
        )
        post_data.update(
            phone_inline_formset_data(
                line_rows=[
                    {"line_index": 1, "extension": self.reception, "label": "Primary"},
                    {"line_index": 2, "extension": self.sales, "label": "Sales"},
                ],
                speed_dial_rows=[
                    {"position": 1, "label": "Support", "destination": "3001"},
                    {"position": 2, "label": "Emergency", "destination": "911"},
                ],
            )
        )

        response = self.client.post(reverse("phone-create"), post_data)

        self.assertEqual(response.status_code, 302)
        phone = Phone.objects.get(mac_address="001122334455")
        self.assertEqual(phone.model, Phone.PhoneModel.CISCO_9951)
        self.assertEqual(phone.line_appearances.count(), 2)
        self.assertTrue(
            PhoneLineAppearance.objects.filter(phone=phone, line_index=2, extension=self.sales, label="Sales").exists()
        )
        self.assertEqual(phone.speed_dials.count(), 2)
        self.assertTrue(
            PhoneSpeedDial.objects.filter(phone=phone, position=1, label="Support", destination="3001").exists()
        )

    def test_line_appearance_formset_rejects_duplicate_line_numbers(self):
        self.client.force_login(self.editor)
        post_data = phone_form_data(self.location)
        post_data.update(
            phone_inline_formset_data(
                line_rows=[
                    {"line_index": 1, "extension": self.reception},
                    {"line_index": 1, "extension": self.sales},
                ],
                speed_dial_rows=[],
            )
        )

        response = self.client.post(reverse("phone-create"), post_data)

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Please correct the duplicate data for line_index.")
        self.assertFalse(Phone.objects.filter(mac_address="001122334455").exists())


class PhoneCSVTests(TestCase):
    def setUp(self):
        self.location = Location.objects.create(**location_model_data(name="HQ", slug="hq"))
        self.extension = Extension.objects.create(
            location=self.location,
            number="3000",
            display_name="Reception",
        )
        self.phone = Phone.objects.create(
            location=self.location,
            mac_address="001122334455",
            model=Phone.PhoneModel.CISCO_9971,
            label="Reception Phone",
        )
        PhoneLineAppearance.objects.create(phone=self.phone, extension=self.extension, line_index=1, label="Primary")
        self.speed_dial = PhoneSpeedDial.objects.create(
            phone=self.phone,
            position=1,
            label="Support",
            destination="3001",
        )

    def test_csv_templates_include_phone_did_and_speed_dial_headers(self):
        self.assertEqual(
            next(csv.reader(StringIO(phone_template_csv()))),
            [
                "location_slug",
                "mac_address",
                "model",
                "label",
                "is_active",
                "line_appearances",
                "speed_dials",
            ],
        )
        self.assertEqual(
            next(csv.reader(StringIO(did_template_csv()))),
            [
                "location_slug",
                "number",
                "provider_slug",
                "trunk_name",
                "direct_extension",
                "default_destination",
                "label",
                "is_active",
            ],
        )
        self.assertEqual(
            next(csv.reader(StringIO(speed_dial_template_csv()))),
            ["phone_mac_address", "position", "label", "destination"],
        )

    def test_phone_and_speed_dial_exports_include_provisioning_data(self):
        phone_rows = list(csv.DictReader(StringIO(export_phones_csv(Phone.objects.filter(id=self.phone.id)))))
        speed_dial_rows = list(
            csv.DictReader(StringIO(export_speed_dials_csv(PhoneSpeedDial.objects.filter(id=self.speed_dial.id))))
        )

        self.assertEqual(phone_rows[0]["mac_address"], "001122334455")
        self.assertEqual(phone_rows[0]["line_appearances"], "1:3000:Primary")
        self.assertEqual(phone_rows[0]["speed_dials"], "1:Support:3001")
        self.assertEqual(speed_dial_rows[0]["phone_mac_address"], "001122334455")
        self.assertEqual(speed_dial_rows[0]["destination"], "3001")


class InboundRoutingManagementViewTests(TestCase):
    def setUp(self):
        self.viewer = User.objects.create_user(username="routing-viewer", password="portal-pass")
        self.editor = User.objects.create_user(username="routing-editor", password="portal-pass")
        assign_role(self.viewer, PortalRole.VIEWER)
        assign_role(self.editor, PortalRole.EDITOR)
        self.location = Location.objects.create(**location_model_data(name="HQ", slug="hq"))
        self.reception = Extension.objects.create(
            location=self.location,
            number="3000",
            display_name="Reception",
        )

    def test_editor_configures_inbound_routing_surfaces(self):
        self.client.force_login(self.editor)

        destination_response = self.client.post(
            reverse("inbound-destination-create"),
            {
                "location": str(self.location.id),
                "name": "Reception Destination",
                "destination_type": InboundDestination.DestinationType.EXTENSION,
                "extension": str(self.reception.id),
                "ivr": "",
                "ring_group": "",
                "queue": "",
            },
        )
        self.assertEqual(destination_response.status_code, 302)
        destination = InboundDestination.objects.get(name="Reception Destination")

        did_response = self.client.post(
            reverse("did-create"),
            {
                "location": str(self.location.id),
                "number": "+15551203000",
                "label": "Main line",
                "provider": "",
                "trunk": "",
                "direct_extension": str(self.reception.id),
                "default_destination": str(destination.id),
                "is_active": "on",
            },
        )
        self.assertEqual(did_response.status_code, 302)

        ring_response = self.client.post(
            reverse("ring-group-create"),
            {
                "location": str(self.location.id),
                "name": "Reception Ring",
                "strategy": RingGroup.Strategy.RING_ALL,
                "timeout_seconds": "25",
                "is_active": "on",
                "members": [str(self.reception.id)],
            },
        )
        self.assertEqual(ring_response.status_code, 302)

        queue_response = self.client.post(
            reverse("queue-create"),
            {
                "location": str(self.location.id),
                "name": "Reception Queue",
                "strategy": CallQueue.Strategy.ROUND_ROBIN,
                "timeout_seconds": "45",
                "retry_seconds": "7",
                "music_on_hold": "default",
                "overflow_destination": str(destination.id),
                "recording_policy": CallQueue.RecordingPolicy.NEVER,
                "is_active": "on",
                "members": [str(self.reception.id)],
            },
        )
        self.assertEqual(queue_response.status_code, 302)

        paging_response = self.client.post(
            reverse("paging-group-create"),
            {
                "location": str(self.location.id),
                "name": "Reception Page",
                "page_code": "7100",
                "is_active": "on",
                "members": [str(self.reception.id)],
            },
        )
        self.assertEqual(paging_response.status_code, 302)

        ivr_post = {
            "location": str(self.location.id),
            "name": "Main IVR",
            "prompt_name": "main-menu",
            "business_hours_destination": str(destination.id),
            "after_hours_destination": str(destination.id),
            "timeout_seconds": "12",
            "timeout_destination": str(destination.id),
            "invalid_destination": str(destination.id),
            "is_active": "on",
        }
        ivr_post.update(
            ivr_menu_formset_data(
                option_rows=[
                    {
                        "digit": "1",
                        "label": "Reception",
                        "destination": destination,
                    }
                ]
            )
        )
        ivr_response = self.client.post(reverse("ivr-create"), ivr_post)
        self.assertEqual(ivr_response.status_code, 302)

        feature_response = self.client.post(
            reverse("feature-code-create"),
            {
                "location": str(self.location.id),
                "code": "*98",
                "name": "Voicemail",
                "feature_type": FeatureCode.FeatureType.VOICEMAIL_MAIN,
                "destination": str(destination.id),
                "notes": "Main voicemail access",
                "is_active": "on",
            },
        )
        self.assertEqual(feature_response.status_code, 302)

        self.assertEqual(DID.objects.get(number="+15551203000").direct_extension, self.reception)
        self.assertTrue(RingGroup.objects.filter(name="Reception Ring", members__extension=self.reception).exists())
        self.assertTrue(CallQueue.objects.filter(name="Reception Queue", members__extension=self.reception).exists())
        self.assertTrue(PagingGroup.objects.filter(name="Reception Page", members__extension=self.reception).exists())
        self.assertTrue(IVRMenuOption.objects.filter(ivr__name="Main IVR", digit="1", destination=destination).exists())
        self.assertTrue(FeatureCode.objects.filter(code="*98", destination=destination).exists())

    def test_viewer_sees_routing_lists(self):
        InboundDestination.objects.create(
            location=self.location,
            name="Reception Destination",
            destination_type=InboundDestination.DestinationType.EXTENSION,
            extension=self.reception,
        )
        self.client.force_login(self.viewer)

        expectations = {
            "inbound-destinations": "Reception Destination",
            "dids": "DID Routing",
            "ivrs": "IVRs",
            "ring-groups": "Ring Groups",
            "queues": "Queues",
            "paging-groups": "Paging Groups",
            "feature-codes": "Feature Codes",
        }
        for route_name, expected_text in expectations.items():
            with self.subTest(route_name=route_name):
                response = self.client.get(reverse(route_name))
                self.assertEqual(response.status_code, 200)
                self.assertContains(response, expected_text)

    def test_ivr_create_renders_dynamic_menu_option_add_controls(self):
        self.client.force_login(self.editor)

        response = self.client.get(reverse("ivr-create"))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'data-formset-prefix="menu_options"')
        self.assertContains(response, "<template data-formset-template>", html=False)
        self.assertContains(response, 'name="menu_options-__prefix__-digit"', html=False)
        self.assertContains(response, 'name="menu_options-__prefix__-DELETE"', html=False)
        self.assertContains(response, 'type="hidden" name="menu_options-0-DELETE"', html=False)
        self.assertNotContains(response, 'type="checkbox" name="menu_options-0-DELETE"', html=False)
        self.assertContains(response, 'name="menu_options-TOTAL_FORMS"', html=False)
        self.assertContains(response, 'data-formset-delete')
        self.assertContains(response, "formset-delete-input")
        self.assertContains(response, "Add option")

    def test_editor_can_create_ivr_with_more_than_initial_menu_options(self):
        destination = InboundDestination.objects.create(
            location=self.location,
            name="Reception Destination",
            destination_type=InboundDestination.DestinationType.EXTENSION,
            extension=self.reception,
        )
        self.client.force_login(self.editor)

        ivr_post = {
            "location": str(self.location.id),
            "name": "Expanded IVR",
            "prompt_name": "expanded-menu",
            "business_hours_destination": str(destination.id),
            "after_hours_destination": str(destination.id),
            "timeout_seconds": "12",
            "timeout_destination": str(destination.id),
            "invalid_destination": str(destination.id),
            "is_active": "on",
        }
        ivr_post.update(
            ivr_menu_formset_data(
                total_forms=5,
                option_rows=[
                    {"digit": str(digit), "label": f"Option {digit}", "destination": destination}
                    for digit in range(1, 6)
                ],
            )
        )

        response = self.client.post(reverse("ivr-create"), ivr_post)

        self.assertRedirects(response, reverse("ivrs"))
        ivr = IVR.objects.get(name="Expanded IVR")
        self.assertEqual(list(ivr.menu_options.values_list("digit", flat=True)), ["1", "2", "3", "4", "5"])

    def test_editor_can_delete_existing_ivr_menu_option_from_formset(self):
        destination = InboundDestination.objects.create(
            location=self.location,
            name="Reception Destination",
            destination_type=InboundDestination.DestinationType.EXTENSION,
            extension=self.reception,
        )
        ivr = IVR.objects.create(
            location=self.location,
            name="Existing IVR",
            prompt_name="existing-menu",
            business_hours_destination=destination,
            after_hours_destination=destination,
            timeout_seconds=12,
            timeout_destination=destination,
            invalid_destination=destination,
        )
        keep_option = IVRMenuOption.objects.create(
            ivr=ivr,
            digit="1",
            label="Keep",
            destination=destination,
        )
        delete_option = IVRMenuOption.objects.create(
            ivr=ivr,
            digit="2",
            label="Remove",
            destination=destination,
        )
        self.client.force_login(self.editor)

        response = self.client.post(
            reverse("ivr-edit", args=[ivr.id]),
            {
                "location": str(self.location.id),
                "name": "Existing IVR",
                "prompt_name": "existing-menu",
                "business_hours_destination": str(destination.id),
                "after_hours_destination": str(destination.id),
                "timeout_seconds": "12",
                "timeout_destination": str(destination.id),
                "invalid_destination": str(destination.id),
                "is_active": "on",
                "menu_options-TOTAL_FORMS": "2",
                "menu_options-INITIAL_FORMS": "2",
                "menu_options-MIN_NUM_FORMS": "0",
                "menu_options-MAX_NUM_FORMS": "1000",
                "menu_options-0-id": str(keep_option.id),
                "menu_options-0-digit": "1",
                "menu_options-0-label": "Keep",
                "menu_options-0-destination": str(destination.id),
                "menu_options-1-id": str(delete_option.id),
                "menu_options-1-digit": "2",
                "menu_options-1-label": "Remove",
                "menu_options-1-destination": str(destination.id),
                "menu_options-1-DELETE": "on",
            },
        )

        self.assertRedirects(response, reverse("ivrs"))
        self.assertTrue(IVRMenuOption.objects.filter(pk=keep_option.pk).exists())
        self.assertFalse(IVRMenuOption.objects.filter(pk=delete_option.pk).exists())

    def test_editor_can_seed_default_feature_codes_and_edit_seeded_rows(self):
        self.client.force_login(self.editor)

        list_response = self.client.get(reverse("feature-codes"))

        self.assertEqual(list_response.status_code, 200)
        self.assertContains(list_response, "Defaults")
        self.assertEqual(FeatureCode.objects.filter(location=self.location).count(), 0)

        seed_response = self.client.post(reverse("feature-code-seed-defaults"))

        self.assertRedirects(seed_response, reverse("feature-codes"))
        default_codes = {spec.code for spec in default_feature_code_specs()}
        self.assertEqual(set(self.location.feature_codes.values_list("code", flat=True)), default_codes)

        seeded = FeatureCode.objects.get(location=self.location, code="*98")
        seeded_list_response = self.client.get(reverse("feature-codes"))
        self.assertContains(seeded_list_response, reverse("feature-code-edit", args=[seeded.id]))
        self.assertNotContains(seeded_list_response, "No feature codes configured")

        edit_response = self.client.post(
            reverse("feature-code-edit", args=[seeded.id]),
            {
                "location": str(self.location.id),
                "code": "*97",
                "name": "Voicemail portal",
                "feature_type": FeatureCode.FeatureType.VOICEMAIL_MAIN,
                "destination": "",
                "notes": "Updated default code",
                "is_active": "on",
            },
        )

        self.assertRedirects(edit_response, reverse("feature-codes"))
        seeded.refresh_from_db()
        self.assertEqual(seeded.code, "*97")
        self.assertEqual(seeded.name, "Voicemail portal")

    def test_seed_defaults_preserves_existing_default_codes(self):
        existing = FeatureCode.objects.create(
            location=self.location,
            code="*98",
            name="Custom voicemail",
            feature_type=FeatureCode.FeatureType.VOICEMAIL_MAIN,
            notes="Keep this label",
        )
        self.client.force_login(self.editor)

        response = self.client.post(reverse("feature-code-seed-defaults"))

        self.assertRedirects(response, reverse("feature-codes"))
        existing.refresh_from_db()
        self.assertEqual(existing.name, "Custom voicemail")
        self.assertEqual(existing.notes, "Keep this label")
        self.assertEqual(self.location.feature_codes.count(), len(default_feature_code_specs()))


class PortalPermissionTests(TestCase):
    def setUp(self):
        self.viewer = User.objects.create_user(username="viewer", password="portal-pass")
        self.editor = User.objects.create_user(username="editor", password="portal-pass")
        self.operator = User.objects.create_user(username="operator", password="portal-pass")
        self.admin = User.objects.create_user(username="role-admin", password="portal-pass")
        assign_role(self.viewer, PortalRole.VIEWER)
        assign_role(self.editor, PortalRole.EDITOR)
        assign_role(self.operator, PortalRole.OPERATOR)
        assign_role(self.admin, PortalRole.ADMIN)

    def test_roles_exist_and_can_be_assigned(self):
        self.assertEqual({choice.value for choice in PortalRole}, {"viewer", "editor", "operator", "admin"})
        self.assertEqual(assign_role(self.viewer, PortalRole.ADMIN).role, PortalRole.ADMIN)
        self.assertEqual(get_user_role(self.viewer), PortalRole.ADMIN)

    def test_role_permission_boundaries(self):
        expectations = {
            self.viewer: {
                PortalPermission.VIEW: True,
                PortalPermission.EDIT_CONFIG: False,
                PortalPermission.RUN_LIVE_OPERATIONS: False,
                PortalPermission.ACCESS_RECORDINGS: False,
                PortalPermission.ADMINISTER: False,
            },
            self.editor: {
                PortalPermission.VIEW: True,
                PortalPermission.EDIT_CONFIG: True,
                PortalPermission.RUN_LIVE_OPERATIONS: False,
                PortalPermission.ACCESS_RECORDINGS: False,
                PortalPermission.ADMINISTER: False,
            },
            self.operator: {
                PortalPermission.VIEW: True,
                PortalPermission.EDIT_CONFIG: False,
                PortalPermission.RUN_LIVE_OPERATIONS: True,
                PortalPermission.ACCESS_RECORDINGS: True,
                PortalPermission.ADMINISTER: False,
            },
            self.admin: {
                PortalPermission.VIEW: True,
                PortalPermission.EDIT_CONFIG: True,
                PortalPermission.RUN_LIVE_OPERATIONS: True,
                PortalPermission.ACCESS_RECORDINGS: True,
                PortalPermission.ADMINISTER: True,
            },
        }

        for user, permissions in expectations.items():
            for permission, expected in permissions.items():
                with self.subTest(user=user.username, permission=permission):
                    self.assertEqual(user_has_permission(user, permission), expected)

    def test_role_permission_helper_handles_unassigned_and_inactive_users(self):
        unassigned = User.objects.create_user(username="unassigned", password="portal-pass")
        inactive = User.objects.create_user(username="inactive", password="portal-pass", is_active=False)

        self.assertTrue(role_has_permission(PortalRole.VIEWER, PortalPermission.VIEW))
        self.assertEqual(get_user_role(unassigned), PortalRole.VIEWER)
        self.assertFalse(user_has_permission(inactive, PortalPermission.VIEW))

    def test_superuser_gets_admin_permissions(self):
        superuser = User.objects.create_superuser(
            username="superuser",
            password="portal-pass",
            email="superuser@example.com",
        )

        self.assertEqual(get_user_role(superuser), PortalRole.ADMIN)
        self.assertTrue(user_has_permission(superuser, PortalPermission.ADMINISTER))


class AuditLogTests(TestCase):
    def setUp(self):
        self.actor = User.objects.create_user(username="auditor", password="portal-pass")

    def test_record_audit_captures_required_fields(self):
        log = record_audit(
            actor=self.actor,
            action=AuditAction.CONFIG_CHANGE,
            target="extensions/1001",
            outcome=AuditOutcome.SUCCESS,
            details={"field": "voicemail_enabled"},
        )

        self.assertEqual(log.actor, self.actor)
        self.assertEqual(log.action, AuditAction.CONFIG_CHANGE)
        self.assertEqual(log.target, "extensions/1001")
        self.assertIsNotNone(log.timestamp)
        self.assertEqual(log.outcome, AuditOutcome.SUCCESS)
        self.assertEqual(log.details, {"field": "voicemail_enabled"})

    def test_representative_audit_actions_can_be_recorded(self):
        representative_actions = [
            AuditAction.CONFIG_CHANGE,
            AuditAction.CONFIG_EXPORT,
            AuditAction.DEPLOYMENT,
            AuditAction.LIVE_PBX_ACTION,
            AuditAction.RECORDING_PLAYBACK,
        ]

        for action in representative_actions:
            with self.subTest(action=action):
                record_audit(
                    actor=self.actor,
                    action=action,
                    target=f"target/{action}",
                    outcome=AuditOutcome.DENIED,
                )

        self.assertEqual(AuditLog.objects.count(), len(representative_actions))


class AdminManagementAPITests(TestCase):
    def setUp(self):
        self.admin = User.objects.create_user(username="admin-api", password="portal-pass")
        self.viewer = User.objects.create_user(username="viewer-api", password="portal-pass")
        assign_role(self.admin, PortalRole.ADMIN)
        assign_role(self.viewer, PortalRole.VIEWER)

    def test_roles_endpoint_requires_admin_and_lists_permissions(self):
        self.client.force_login(self.viewer)

        viewer_response = self.client.get(reverse("admin-roles"))

        self.assertEqual(viewer_response.status_code, 403)

        self.client.force_login(self.admin)

        admin_response = self.client.get(reverse("admin-roles"))

        self.assertEqual(admin_response.status_code, 200)
        roles = {role["id"]: role for role in admin_response.json()["roles"]}
        self.assertIn(PortalRole.ADMIN, roles)
        self.assertIn(PortalPermission.ADMINISTER, roles[PortalRole.ADMIN]["permissions"])
        self.assertNotIn(PortalPermission.ADMINISTER, roles[PortalRole.VIEWER]["permissions"])

    def test_admin_user_list_requires_admin_and_serializes_user_info(self):
        self.client.force_login(self.viewer)

        viewer_response = self.client.get(reverse("admin-users"))

        self.assertEqual(viewer_response.status_code, 403)

        self.client.force_login(self.admin)

        admin_response = self.client.get(reverse("admin-users"))
        users = {user["username"]: user for user in admin_response.json()["users"]}

        self.assertEqual(admin_response.status_code, 200)
        self.assertEqual(users["admin-api"]["role"], PortalRole.ADMIN)
        self.assertEqual(users["viewer-api"]["role"], PortalRole.VIEWER)
        self.assertTrue(users["admin-api"]["is_active"])
        self.assertEqual(users["viewer-api"]["email"], "")
        self.assertNotIn("password", users["admin-api"])

    def test_admin_can_create_and_update_users_with_roles(self):
        self.client.force_login(self.admin)

        create_response = self._post_json(
            reverse("admin-users"),
            {
                "username": "managed-user",
                "email": "managed@example.com",
                "role": PortalRole.EDITOR,
            },
        )

        self.assertEqual(create_response.status_code, 201)
        user = User.objects.get(username="managed-user")
        self.assertEqual(user.email, "managed@example.com")
        self.assertEqual(get_user_role(user), PortalRole.EDITOR)

        update_response = self._patch_json(
            reverse("admin-user-detail", args=[user.id]),
            {"role": PortalRole.OPERATOR, "is_active": False},
        )

        self.assertEqual(update_response.status_code, 200)
        user.refresh_from_db()
        user.portal_profile.refresh_from_db()
        self.assertFalse(user.is_active)
        self.assertEqual(user.portal_profile.role, PortalRole.OPERATOR)
        self.assertIsNone(get_user_role(user))

    def test_admin_can_create_and_update_service_identities(self):
        self.client.force_login(self.admin)

        create_response = self._post_json(
            reverse("service-identity-list"),
            {
                "name": "Provisioner",
                "slug": "provisioner",
                "description": "Phone provisioning job",
            },
        )

        self.assertEqual(create_response.status_code, 201)
        identity = ServiceIdentity.objects.get(slug="provisioner")
        self.assertEqual(identity.created_by, self.admin)
        self.assertTrue(identity.is_active)

        update_response = self._patch_json(
            reverse("service-identity-detail", args=[identity.id]),
            {"is_active": False, "description": "Disabled"},
        )

        self.assertEqual(update_response.status_code, 200)
        identity.refresh_from_db()
        self.assertFalse(identity.is_active)
        self.assertEqual(identity.description, "Disabled")

    def _post_json(self, url, payload):
        return self.client.post(url, data=json.dumps(payload), content_type="application/json")

    def _patch_json(self, url, payload):
        return self.client.patch(url, data=json.dumps(payload), content_type="application/json")


class AuthenticatedUserInfoAPITests(TestCase):
    def setUp(self):
        self.admin = User.objects.create_user(username="info-admin", password="portal-pass")
        self.viewer = User.objects.create_user(username="info-viewer", password="portal-pass")
        self.other = User.objects.create_user(username="info-other", password="portal-pass")
        assign_role(self.admin, PortalRole.ADMIN)
        assign_role(self.viewer, PortalRole.VIEWER)
        assign_role(self.other, PortalRole.VIEWER)
        _admin_key, self.admin_secret = APIKey.issue(name="admin info api", user=self.admin, created_by=self.admin)
        _viewer_key, self.viewer_secret = APIKey.issue(name="viewer info api", user=self.viewer, created_by=self.admin)

    def test_api_key_user_can_read_and_update_own_info_with_audit(self):
        get_response = self.client.get(
            reverse("api-current-user"),
            HTTP_AUTHORIZATION=f"Bearer {self.viewer_secret}",
        )

        self.assertEqual(get_response.status_code, 200)
        self.assertEqual(get_response.json()["user"]["username"], "info-viewer")
        self.assertEqual(get_response.json()["user"]["role"], PortalRole.VIEWER)

        update_response = self._patch_json(
            reverse("api-current-user"),
            {"email": "viewer@example.test", "first_name": "Viewer"},
            self.viewer_secret,
        )

        self.assertEqual(update_response.status_code, 200)
        self.viewer.refresh_from_db()
        self.assertEqual(self.viewer.email, "viewer@example.test")
        self.assertEqual(self.viewer.first_name, "Viewer")
        audit = AuditLog.objects.get(action=AuditAction.API_USER_UPDATE)
        self.assertEqual(audit.actor, self.viewer)
        self.assertEqual(audit.target, f"users/{self.viewer.id}")
        self.assertEqual(audit.outcome, AuditOutcome.SUCCESS)
        self.assertEqual(audit.details["changed_fields"], ["email", "first_name"])
        self.assertEqual(audit.details["api_key_id"], APIKey.objects.get(name="viewer info api").id)
        self.assertNotIn("viewer@example.test", json.dumps(audit.details))

    def test_non_admin_user_can_only_retrieve_self(self):
        list_response = self.client.get(
            reverse("api-users"),
            HTTP_AUTHORIZATION=f"Bearer {self.viewer_secret}",
        )
        other_response = self.client.get(
            reverse("api-user-detail", args=[self.other.id]),
            HTTP_AUTHORIZATION=f"Bearer {self.viewer_secret}",
        )

        self.assertEqual(list_response.status_code, 200)
        self.assertEqual([user["username"] for user in list_response.json()["users"]], ["info-viewer"])
        self.assertEqual(other_response.status_code, 403)

    def test_non_admin_user_cannot_update_admin_only_fields(self):
        response = self._patch_json(
            reverse("api-current-user"),
            {"role": PortalRole.ADMIN, "is_active": False},
            self.viewer_secret,
        )

        self.assertEqual(response.status_code, 403)
        self.viewer.refresh_from_db()
        self.viewer.portal_profile.refresh_from_db()
        self.assertTrue(self.viewer.is_active)
        self.assertEqual(self.viewer.portal_profile.role, PortalRole.VIEWER)
        self.assertEqual(AuditLog.objects.count(), 0)

    def test_admin_api_key_can_retrieve_and_update_other_users_with_audit(self):
        get_response = self.client.get(
            reverse("api-user-detail", args=[self.other.id]),
            HTTP_AUTHORIZATION=f"Bearer {self.admin_secret}",
        )

        self.assertEqual(get_response.status_code, 200)
        self.assertEqual(get_response.json()["user"]["username"], "info-other")

        update_response = self._patch_json(
            reverse("api-user-detail", args=[self.other.id]),
            {"username": "renamed-other", "role": PortalRole.OPERATOR, "is_active": False},
            self.admin_secret,
        )

        self.assertEqual(update_response.status_code, 200)
        self.other.refresh_from_db()
        self.other.portal_profile.refresh_from_db()
        self.assertEqual(self.other.username, "renamed-other")
        self.assertFalse(self.other.is_active)
        self.assertEqual(self.other.portal_profile.role, PortalRole.OPERATOR)
        audit = AuditLog.objects.get(action=AuditAction.API_USER_UPDATE)
        self.assertEqual(audit.actor, self.admin)
        self.assertEqual(audit.target, f"users/{self.other.id}")
        self.assertEqual(audit.details["changed_fields"], ["is_active", "role", "username"])

    def test_invalid_bearer_token_is_rejected(self):
        response = self.client.get(reverse("api-current-user"), HTTP_AUTHORIZATION="Bearer missing")

        self.assertEqual(response.status_code, 401)
        self.assertEqual(response.json()["error"], "Invalid API key.")

    def _patch_json(self, url, payload, secret):
        return self.client.patch(
            url,
            data=json.dumps(payload),
            content_type="application/json",
            HTTP_AUTHORIZATION=f"Bearer {secret}",
        )


class AdminBackupWorkflowTests(TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.settings_override = override_settings(MEDIA_ROOT=self.temp_dir.name)
        self.settings_override.enable()
        self.addCleanup(self.settings_override.disable)
        self.addCleanup(self.temp_dir.cleanup)

        self.admin = User.objects.create_user(username="backup-admin", password="portal-pass")
        self.viewer = User.objects.create_user(username="backup-viewer", password="portal-pass")
        assign_role(self.admin, PortalRole.ADMIN)
        assign_role(self.viewer, PortalRole.VIEWER)
        self.location = Location.objects.create(**location_model_data(name="Backup HQ", slug="backup-hq"))
        self.extension = Extension.objects.create(
            location=self.location,
            number="3000",
            display_name="Backup Desk",
            emergency_calling_enabled=True,
        )
        add_emergency_route(self.location)
        self.prompt = self._create_audio_prompt()
        self.config_version = create_config_version(self.location, exported_by=self.admin)
        record_audit(
            actor=self.admin,
            action=AuditAction.CONFIG_CHANGE,
            target="locations/backup-hq",
            outcome=AuditOutcome.SUCCESS,
            details={"field": "name"},
        )

    def test_admin_can_generate_backup_archive_with_required_categories(self):
        self.client.force_login(self.admin)

        settings_response = self.client.get(reverse("settings"))
        create_response = self.client.post(reverse("admin-backup-create"))

        self.assertEqual(settings_response.status_code, 200)
        self.assertContains(settings_response, 'data-area="settings"')
        self.assertContains(settings_response, "Generate backup")
        self.assertContains(settings_response, "Suitable for off-host storage")
        self.assertEqual(create_response.status_code, 302)

        backup = AdminBackup.objects.get()
        download_response = self.client.get(reverse("admin-backup-download", args=[backup.id]))

        self.assertEqual(download_response.status_code, 200)
        self.assertEqual(download_response["Content-Type"], "application/zip")
        self.assertEqual(download_response["X-Checksum-SHA256"], backup.checksum)

        with zipfile.ZipFile(BytesIO(download_response.content)) as archive:
            names = set(archive.namelist())
            media_archive_path = f"media/files/{self.prompt.original_file.name}"

            self.assertIn("manifest.json", names)
            self.assertIn("README.txt", names)
            self.assertIn("database/django-dumpdata.json", names)
            self.assertIn("media/manifest.json", names)
            self.assertIn(media_archive_path, names)
            self.assertIn("exports/config_versions.json", names)
            self.assertIn("config/portal-config-data.json", names)
            self.assertIn("config/locations/backup-hq.json", names)
            self.assertIn("audit/audit_logs.json", names)
            self.assertIn("backups/admin_backups.json", names)
            self.assertIn("SHA256SUMS", names)

            manifest = json.loads(archive.read("manifest.json"))
            self.assertEqual(manifest["format"], "pbx-admin-backup/v1")
            self.assertEqual(manifest["contents"]["database_dump"], "database/django-dumpdata.json")
            self.assertEqual(manifest["contents"]["media"]["file_count"], 2)
            self.assertIn("off-host storage", manifest["off_host_storage"])
            self.assertTrue(any("plaintext telecom secrets" in note for note in manifest["security_notes"]))
            self.assertEqual(
                {
                    (info.external_attr >> 16) & 0o777
                    for info in archive.infolist()
                    if not info.is_dir()
                },
                {0o600},
            )

            export_metadata = json.loads(archive.read("exports/config_versions.json"))
            self.assertEqual(export_metadata[0]["checksum"], self.config_version.checksum)
            self.assertEqual(export_metadata[0]["location_slug"], "backup-hq")

            location_config = json.loads(archive.read("config/locations/backup-hq.json"))
            self.assertEqual(location_config["location"]["slug"], "backup-hq")
            self.assertIn("asterisk_configs", location_config)

            config_dump = json.loads(archive.read("config/portal-config-data.json"))
            self.assertIn("core.location", {row["model"] for row in config_dump})
            self.assertIn("core.audioprompt", {row["model"] for row in config_dump})

            audit_logs = json.loads(archive.read("audit/audit_logs.json"))
            self.assertIn("config_change", {row["action"] for row in audit_logs})
            backup_readme = archive.read("README.txt").lower()
            self.assertIn(b"suitable for off-host storage", backup_readme)
            self.assertIn(b"plaintext telecom secrets", backup_readme)

    def test_backup_generate_and_download_require_admin(self):
        self.client.force_login(self.viewer)

        viewer_settings_response = self.client.get(reverse("settings"))
        viewer_create_response = self.client.post(reverse("admin-backup-create"))

        self.assertEqual(viewer_settings_response.status_code, 403)
        self.assertEqual(viewer_create_response.status_code, 403)
        self.assertEqual(AdminBackup.objects.count(), 0)

        self.client.force_login(self.admin)
        self.client.post(reverse("admin-backup-create"))
        backup = AdminBackup.objects.get()

        self.client.force_login(self.viewer)
        viewer_download_response = self.client.get(reverse("admin-backup-download", args=[backup.id]))

        self.assertEqual(viewer_download_response.status_code, 403)
        self.assertEqual(AuditLog.objects.filter(action=AuditAction.BACKUP_DOWNLOAD).count(), 0)

    def test_backup_generation_and_download_are_audit_logged(self):
        self.client.force_login(self.admin)

        self.client.post(reverse("admin-backup-create"))
        backup = AdminBackup.objects.get()

        create_audit = AuditLog.objects.get(action=AuditAction.BACKUP_CREATE)
        self.assertEqual(create_audit.actor, self.admin)
        self.assertEqual(create_audit.target, f"admin_backups/{backup.id}")
        self.assertEqual(create_audit.outcome, AuditOutcome.SUCCESS)
        self.assertEqual(create_audit.details["checksum"], backup.checksum)
        self.assertEqual(create_audit.details["database_dump_method"], "django_dumpdata")

        download_response = self.client.get(reverse("admin-backup-download", args=[backup.id]))

        self.assertEqual(download_response.status_code, 200)
        download_audit = AuditLog.objects.get(action=AuditAction.BACKUP_DOWNLOAD)
        self.assertEqual(download_audit.actor, self.admin)
        self.assertEqual(download_audit.target, f"admin_backups/{backup.id}")
        self.assertEqual(download_audit.outcome, AuditOutcome.SUCCESS)
        self.assertEqual(download_audit.details["archive_size_bytes"], backup.archive_size_bytes)

    def _create_audio_prompt(self):
        prompt = AudioPrompt(
            location=self.location,
            name="Main Menu",
            original_filename="main-menu.wav",
            source_format=AudioPrompt.SourceFormat.WAV,
            content_type="audio/wav",
            size_bytes=len(b"source-audio"),
            asterisk_path="/var/lib/asterisk/sounds/custom/ivr/main-menu.wav",
        )
        prompt.original_file.save("main-menu.wav", ContentFile(b"source-audio"), save=False)
        prompt.converted_file.save("main-menu-converted.wav", ContentFile(b"converted-audio"), save=False)
        prompt.save()
        return prompt


class APIKeyLifecycleAPITests(TestCase):
    def setUp(self):
        self.admin = User.objects.create_user(username="key-admin", password="portal-pass")
        self.viewer = User.objects.create_user(username="key-viewer", password="portal-pass")
        self.scoped_user = User.objects.create_user(username="scoped-user", password="portal-pass")
        assign_role(self.admin, PortalRole.ADMIN)
        assign_role(self.viewer, PortalRole.VIEWER)
        assign_role(self.scoped_user, PortalRole.VIEWER)
        self.service_identity = ServiceIdentity.objects.create(
            name="Provisioning Service",
            slug="provisioning-service",
            created_by=self.admin,
        )

    def test_api_key_lifecycle_operations_require_admin(self):
        api_key, _secret = APIKey.issue(name="existing", user=self.scoped_user, created_by=self.admin)
        self.client.force_login(self.viewer)

        create_response = self._post_json(
            reverse("api-key-create"),
            {"name": "viewer key", "user_id": self.scoped_user.id},
        )
        rotate_response = self.client.post(reverse("api-key-rotate", args=[api_key.id]))
        revoke_response = self.client.post(reverse("api-key-revoke", args=[api_key.id]))

        self.assertEqual(create_response.status_code, 403)
        self.assertEqual(rotate_response.status_code, 403)
        self.assertEqual(revoke_response.status_code, 403)
        api_key.refresh_from_db()
        self.assertTrue(api_key.is_active)
        self.assertEqual(AuditLog.objects.count(), 0)

    def test_non_admin_bearer_cannot_create_api_keys(self):
        _viewer_key, viewer_secret = APIKey.issue(name="viewer automation", user=self.viewer, created_by=self.admin)

        response = self._post_json(
            reverse("api-key-create"),
            {"name": "viewer key", "user_id": self.scoped_user.id},
            HTTP_AUTHORIZATION=f"Bearer {viewer_secret}",
        )

        self.assertEqual(response.status_code, 403)
        self.assertFalse(APIKey.objects.filter(name="viewer key").exists())
        self.assertEqual(AuditLog.objects.count(), 0)

    def test_admin_bearer_can_create_user_scoped_api_key_and_audits(self):
        _admin_key, admin_secret = APIKey.issue(name="admin automation", user=self.admin, created_by=self.admin)

        response = self._post_json(
            reverse("api-key-create"),
            {"name": "API-created user key", "user_id": self.scoped_user.id},
            HTTP_AUTHORIZATION=f"Bearer {admin_secret}",
        )

        self.assertEqual(response.status_code, 201)
        api_key = APIKey.objects.get(name="API-created user key")
        self.assertEqual(api_key.user, self.scoped_user)
        self.assertEqual(api_key.created_by, self.admin)
        audit = AuditLog.objects.get(action=AuditAction.API_KEY_CREATE)
        self.assertEqual(audit.actor, self.admin)
        self.assertEqual(audit.target, f"api_keys/{api_key.id}")
        self.assertEqual(audit.details["scope_id"], self.scoped_user.id)

    def test_admin_creates_user_scoped_api_key_and_audits_without_raw_secret(self):
        self.client.force_login(self.admin)

        response = self._post_json(
            reverse("api-key-create"),
            {"name": "User automation", "user_id": self.scoped_user.id},
        )

        self.assertEqual(response.status_code, 201)
        body = response.json()
        raw_secret = body["secret"]
        api_key = APIKey.objects.get()
        self.assertTrue(raw_secret.startswith("pbx_"))
        self.assertEqual(api_key.user, self.scoped_user)
        self.assertIsNone(api_key.service_identity)
        self.assertNotEqual(api_key.key_hash, raw_secret)
        self.assertEqual(APIKey.find_by_secret(raw_secret), api_key)

        audit = AuditLog.objects.get(action=AuditAction.API_KEY_CREATE)
        self.assertEqual(audit.actor, self.admin)
        self.assertEqual(audit.target, f"api_keys/{api_key.id}")
        self.assertEqual(audit.outcome, AuditOutcome.SUCCESS)
        self.assertEqual(audit.details["scope_type"], "user")
        self.assertEqual(audit.details["scope_id"], self.scoped_user.id)
        self.assertNotIn(raw_secret, json.dumps(audit.details))

    def test_api_key_scope_requires_exactly_one_user_or_service_identity(self):
        self.client.force_login(self.admin)

        missing_scope_response = self._post_json(reverse("api-key-create"), {"name": "missing"})
        double_scope_response = self._post_json(
            reverse("api-key-create"),
            {
                "name": "double",
                "user_id": self.scoped_user.id,
                "service_identity_id": self.service_identity.id,
            },
        )
        service_scope_response = self._post_json(
            reverse("api-key-create"),
            {
                "name": "service",
                "service_identity_id": self.service_identity.id,
            },
        )

        self.assertEqual(missing_scope_response.status_code, 400)
        self.assertEqual(double_scope_response.status_code, 400)
        self.assertEqual(service_scope_response.status_code, 201)
        api_key = APIKey.objects.get()
        self.assertIsNone(api_key.user)
        self.assertEqual(api_key.service_identity, self.service_identity)

    def test_rotation_invalidates_old_secret_and_audits_new_prefix(self):
        self.client.force_login(self.admin)
        create_response = self._post_json(
            reverse("api-key-create"),
            {"name": "Rotating key", "user_id": self.scoped_user.id},
        )
        api_key = APIKey.objects.get()
        old_secret = create_response.json()["secret"]
        old_prefix = api_key.prefix

        rotate_response = self.client.post(reverse("api-key-rotate", args=[api_key.id]))

        self.assertEqual(rotate_response.status_code, 200)
        new_secret = rotate_response.json()["secret"]
        self.assertNotEqual(new_secret, old_secret)
        self.assertIsNone(APIKey.find_by_secret(old_secret))
        self.assertEqual(APIKey.find_by_secret(new_secret), api_key)
        api_key.refresh_from_db()
        self.assertEqual(api_key.last_rotated_by, self.admin)
        self.assertIsNotNone(api_key.last_rotated_at)
        self.assertNotEqual(api_key.prefix, old_prefix)

        audit = AuditLog.objects.get(action=AuditAction.API_KEY_ROTATE)
        self.assertEqual(audit.details["old_prefix"], old_prefix)
        self.assertEqual(audit.details["prefix"], api_key.prefix)
        self.assertNotIn(new_secret, json.dumps(audit.details))

    def test_revocation_disables_secret_and_blocks_future_rotation(self):
        self.client.force_login(self.admin)
        create_response = self._post_json(
            reverse("api-key-create"),
            {"name": "Revoked key", "user_id": self.scoped_user.id},
        )
        api_key = APIKey.objects.get()
        raw_secret = create_response.json()["secret"]

        revoke_response = self.client.post(reverse("api-key-revoke", args=[api_key.id]))

        self.assertEqual(revoke_response.status_code, 200)
        api_key.refresh_from_db()
        self.assertFalse(api_key.is_active)
        self.assertEqual(api_key.revoked_by, self.admin)
        self.assertIsNotNone(api_key.revoked_at)
        self.assertIsNone(APIKey.find_by_secret(raw_secret))

        rotate_response = self.client.post(reverse("api-key-rotate", args=[api_key.id]))

        self.assertEqual(rotate_response.status_code, 400)
        self.assertEqual(AuditLog.objects.filter(action=AuditAction.API_KEY_REVOKE).count(), 1)

    def _post_json(self, url, payload, **extra):
        return self.client.post(url, data=json.dumps(payload), content_type="application/json", **extra)


class LANWarpOnlyMiddlewareTests(SimpleTestCase):
    @override_settings(
        PORTAL_ENFORCE_CLIENT_CIDR=True,
        PORTAL_ALLOWED_CLIENT_CIDRS=["10.0.0.0/8"],
        PORTAL_TRUSTED_PROXY_CIDRS=["127.0.0.0/8"],
    )
    def test_blocks_public_client_ip(self):
        client = Client(REMOTE_ADDR="203.0.113.10")

        response = client.get(reverse("home"))

        self.assertEqual(response.status_code, 403)

    @override_settings(
        PORTAL_ENFORCE_CLIENT_CIDR=True,
        PORTAL_ALLOWED_CLIENT_CIDRS=["10.0.0.0/8"],
        PORTAL_TRUSTED_PROXY_CIDRS=["127.0.0.0/8"],
    )
    def test_allows_configured_private_client_ip(self):
        client = Client(REMOTE_ADDR="10.10.10.10")

        response = client.get(reverse("home"))

        self.assertEqual(response.status_code, 302)

    @override_settings(
        PORTAL_ENFORCE_CLIENT_CIDR=True,
        PORTAL_ALLOWED_CLIENT_CIDRS=["10.0.0.0/8"],
        PORTAL_TRUSTED_PROXY_CIDRS=["127.0.0.0/8"],
    )
    def test_uses_forwarded_for_from_trusted_proxy(self):
        client = Client(REMOTE_ADDR="127.0.0.1")

        response = client.get(reverse("home"), headers={"X-Forwarded-For": "10.20.30.40"})

        self.assertEqual(response.status_code, 302)


class InboundRoutingModelTests(TestCase):
    def setUp(self):
        self.hq = Location.objects.create(**location_model_data(name="HQ", slug="hq"))
        self.warehouse = Location.objects.create(**location_model_data(name="Warehouse", slug="warehouse"))
        self.reception = Extension.objects.create(
            location=self.hq,
            number="3000",
            display_name="Reception",
        )
        self.fallback = Extension.objects.create(
            location=self.hq,
            number="3999",
            display_name="Fallback",
        )
        self.warehouse_extension = Extension.objects.create(
            location=self.warehouse,
            number="4000",
            display_name="Warehouse",
        )
        self.fallback_destination = InboundDestination.objects.create(
            location=self.hq,
            name="HQ Fallback",
            destination_type=InboundDestination.DestinationType.EXTENSION,
            extension=self.fallback,
        )
        self.warehouse_destination = InboundDestination.objects.create(
            location=self.warehouse,
            name="Warehouse Fallback",
            destination_type=InboundDestination.DestinationType.EXTENSION,
            extension=self.warehouse_extension,
        )
        self.hq.default_inbound_destination = self.fallback_destination
        self.hq.save(update_fields=["default_inbound_destination", "updated_at"])

    def test_did_routes_direct_extension_before_location_default(self):
        did = DID(
            location=self.hq,
            number="+15551203000",
            direct_extension=self.reception,
        )

        did.full_clean()
        did.save()

        self.assertEqual(did.effective_destination, self.reception)

        did.direct_extension = None
        did.full_clean()
        did.save(update_fields=["direct_extension", "updated_at"])

        did.refresh_from_db()
        self.assertEqual(did.effective_destination, self.fallback_destination)
        self.assertEqual(build_location_config(self.hq)["inbound"]["dids"][0]["route_source"], "location_default")

    def test_did_rejects_duplicate_number_and_missing_fallback(self):
        DID.objects.create(
            location=self.hq,
            number="+15551203000",
            default_destination=self.fallback_destination,
        )
        duplicate = DID(
            location=self.hq,
            number="+15551203000",
            default_destination=self.fallback_destination,
        )
        no_fallback_location = Location.objects.create(**location_model_data(name="No Fallback", slug="no-fallback"))
        missing_fallback = DID(location=no_fallback_location, number="+15551203001")

        with self.assertRaises(ValidationError) as duplicate_context:
            duplicate.full_clean()
        self.assertIn("number", duplicate_context.exception.message_dict)

        with self.assertRaises(ValidationError) as missing_context:
            missing_fallback.full_clean()
        self.assertIn("default_destination", missing_context.exception.message_dict)

    def test_ivr_timeout_invalid_and_hours_destinations_are_local(self):
        ivr = IVR(
            location=self.hq,
            name="Main IVR",
            prompt_name="main-menu",
            business_hours_destination=self.fallback_destination,
            after_hours_destination=self.fallback_destination,
            timeout_seconds=15,
            timeout_destination=self.fallback_destination,
            invalid_destination=self.fallback_destination,
        )

        ivr.full_clean()
        ivr.save()
        option = IVRMenuOption(ivr=ivr, digit="1", label="Reception", destination=self.fallback_destination)
        option.full_clean()
        option.save()

        ivr.invalid_destination = self.warehouse_destination
        with self.assertRaises(ValidationError) as context:
            ivr.full_clean()
        self.assertIn("invalid_destination", context.exception.message_dict)

    def test_queue_static_members_strategy_timing_moh_and_overflow(self):
        queue = CallQueue(
            location=self.hq,
            name="Support Queue",
            strategy=CallQueue.Strategy.ROUND_ROBIN,
            timeout_seconds=45,
            retry_seconds=7,
            music_on_hold="default",
            overflow_destination=self.fallback_destination,
        )

        queue.full_clean()
        queue.save()
        member = QueueMember(queue=queue, extension=self.reception)
        member.full_clean()
        member.save()

        self.assertEqual(queue.strategy, CallQueue.Strategy.ROUND_ROBIN)
        self.assertEqual(queue.timeout_seconds, 45)
        self.assertEqual(queue.retry_seconds, 7)
        self.assertEqual(queue.music_on_hold, "default")
        self.assertEqual(queue.overflow_destination, self.fallback_destination)

        queue.overflow_destination = self.warehouse_destination
        with self.assertRaises(ValidationError) as context:
            queue.full_clean()
        self.assertIn("overflow_destination", context.exception.message_dict)

    def test_ring_group_paging_group_and_feature_code_models(self):
        ring_group = RingGroup.objects.create(
            location=self.hq,
            name="Reception Ring",
            strategy=RingGroup.Strategy.HUNT,
            timeout_seconds=22,
        )
        ring_member = RingGroupMember(ring_group=ring_group, extension=self.reception, priority=1)
        ring_member.full_clean()
        ring_member.save()

        paging_group = PagingGroup.objects.create(location=self.hq, name="Reception Page", page_code="7100")
        paging_member = PagingGroupMember(paging_group=paging_group, extension=self.reception)
        paging_member.full_clean()
        paging_member.save()

        feature_code = FeatureCode(
            location=self.hq,
            code="*98",
            name="Voicemail",
            feature_type=FeatureCode.FeatureType.VOICEMAIL_MAIN,
            destination=self.fallback_destination,
        )
        feature_code.full_clean()
        feature_code.save()

        cross_location_feature = FeatureCode(
            location=self.hq,
            code="*99",
            name="Bad Forward",
            feature_type=FeatureCode.FeatureType.CUSTOM,
            destination=self.warehouse_destination,
        )
        invalid_code = FeatureCode(
            location=self.hq,
            code="transfer-now",
            name="Invalid",
            feature_type=FeatureCode.FeatureType.CUSTOM,
        )

        with self.assertRaises(ValidationError) as destination_context:
            cross_location_feature.full_clean()
        self.assertIn("destination", destination_context.exception.message_dict)

        with self.assertRaises(ValidationError) as code_context:
            invalid_code.full_clean()
        self.assertIn("code", code_context.exception.message_dict)

        inbound_config = build_location_config(self.hq)["inbound"]
        self.assertEqual(inbound_config["ring_groups"][0]["members"][0]["extension"], "3000")
        self.assertEqual(inbound_config["paging_groups"][0]["members"], ["3000"])
        self.assertEqual(inbound_config["feature_codes"][0]["code"], "*98")


class PBXDomainModelTests(TestCase):
    def setUp(self):
        self.hq = Location.objects.create(name="Headquarters", slug="headquarters")
        self.warehouse = Location.objects.create(name="Warehouse", slug="warehouse")

    def test_extension_numbers_are_global_four_digit_unique(self):
        Extension.objects.create(
            location=self.hq,
            number="1000",
            display_name="HQ Reception",
        )

        with self.assertRaises(IntegrityError):
            with transaction.atomic():
                Extension.objects.create(
                    location=self.warehouse,
                    number="1000",
                    display_name="Warehouse Desk",
                )

        invalid_extension = Extension(
            location=self.hq,
            number="100",
            display_name="Too short",
        )
        with self.assertRaises(ValidationError):
            invalid_extension.full_clean()

    def test_required_location_relationships_are_database_enforced(self):
        with self.assertRaises(IntegrityError):
            with transaction.atomic():
                Extension.objects.create(number="1001", display_name="No Location")

    def test_inbound_destination_requires_one_matching_local_target(self):
        extension = Extension.objects.create(
            location=self.hq,
            number="1000",
            display_name="HQ Reception",
        )
        destination = InboundDestination(
            location=self.hq,
            name="Reception",
            destination_type=InboundDestination.DestinationType.EXTENSION,
            extension=extension,
        )

        destination.full_clean()
        destination.save()

        missing_target = InboundDestination(
            location=self.hq,
            name="Missing target",
            destination_type=InboundDestination.DestinationType.EXTENSION,
        )
        with self.assertRaises(ValidationError):
            missing_target.full_clean()

        with self.assertRaises(IntegrityError):
            with transaction.atomic():
                InboundDestination.objects.create(
                    location=self.hq,
                    name="Database blocked missing target",
                    destination_type=InboundDestination.DestinationType.EXTENSION,
                )

        wrong_location_extension = Extension.objects.create(
            location=self.warehouse,
            number="2000",
            display_name="Warehouse Desk",
        )
        cross_location_destination = InboundDestination(
            location=self.hq,
            name="Cross location",
            destination_type=InboundDestination.DestinationType.EXTENSION,
            extension=wrong_location_extension,
        )
        with self.assertRaises(ValidationError):
            cross_location_destination.full_clean()

    def test_routing_and_memberships_keep_location_ownership(self):
        reception = Extension.objects.create(
            location=self.hq,
            number="1000",
            display_name="HQ Reception",
        )
        sales = Extension.objects.create(
            location=self.hq,
            number="1001",
            display_name="HQ Sales",
        )
        warehouse = Extension.objects.create(
            location=self.warehouse,
            number="2000",
            display_name="Warehouse Desk",
        )
        phone = Phone.objects.create(
            location=self.hq,
            mac_address="001122334455",
            label="Reception Phone",
        )
        line = PhoneLineAppearance(phone=phone, extension=reception, line_index=1)
        line.full_clean()
        line.save()

        ring_group = RingGroup.objects.create(location=self.hq, name="HQ Ring")
        ring_member = RingGroupMember(ring_group=ring_group, extension=reception)
        ring_member.full_clean()
        ring_member.save()

        queue = CallQueue.objects.create(location=self.hq, name="Sales Queue")
        queue_member = QueueMember(queue=queue, extension=sales)
        queue_member.full_clean()
        queue_member.save()

        paging_group = PagingGroup.objects.create(
            location=self.hq,
            name="HQ Page",
            page_code="7000",
        )
        paging_member = PagingGroupMember(paging_group=paging_group, extension=sales)
        paging_member.full_clean()
        paging_member.save()

        extension_destination = InboundDestination.objects.create(
            location=self.hq,
            name="Reception",
            destination_type=InboundDestination.DestinationType.EXTENSION,
            extension=reception,
        )
        ivr = IVR.objects.create(location=self.hq, name="Main IVR")
        ivr_destination = InboundDestination.objects.create(
            location=self.hq,
            name="Main IVR",
            destination_type=InboundDestination.DestinationType.IVR,
            ivr=ivr,
        )
        ring_destination = InboundDestination.objects.create(
            location=self.hq,
            name="Ring Group",
            destination_type=InboundDestination.DestinationType.RING_GROUP,
            ring_group=ring_group,
        )
        queue_destination = InboundDestination.objects.create(
            location=self.hq,
            name="Queue",
            destination_type=InboundDestination.DestinationType.QUEUE,
            queue=queue,
        )
        option = IVRMenuOption(ivr=ivr, digit="1", destination=ring_destination)
        option.full_clean()
        option.save()

        self.assertEqual(
            {
                extension_destination.destination_type,
                ivr_destination.destination_type,
                ring_destination.destination_type,
                queue_destination.destination_type,
            },
            {
                InboundDestination.DestinationType.EXTENSION,
                InboundDestination.DestinationType.IVR,
                InboundDestination.DestinationType.RING_GROUP,
                InboundDestination.DestinationType.QUEUE,
            },
        )

        cross_location_member = RingGroupMember(
            ring_group=ring_group,
            extension=warehouse,
        )
        with self.assertRaises(ValidationError):
            cross_location_member.full_clean()

    def test_minimal_fixture_loads_multi_location_topology(self):
        call_command("loaddata", "minimal_pbx_topology", verbosity=0)

        self.assertEqual(Location.objects.count(), 2)
        self.assertEqual(DID.objects.count(), 2)
        self.assertEqual(OutboundRouteTrunk.objects.count(), 2)
        self.assertEqual(
            list(Extension.objects.order_by("number").values_list("number", flat=True)),
            ["1000", "1001", "2000"],
        )
        self.assertEqual(
            set(InboundDestination.objects.values_list("destination_type", flat=True)),
            {
                InboundDestination.DestinationType.EXTENSION,
                InboundDestination.DestinationType.IVR,
                InboundDestination.DestinationType.RING_GROUP,
                InboundDestination.DestinationType.QUEUE,
            },
        )
