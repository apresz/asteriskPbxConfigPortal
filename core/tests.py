import csv
import asyncio
import hashlib
from io import BytesIO, StringIO
import json
from pathlib import Path
import subprocess
import tempfile
from unittest import mock
import zipfile

from django.contrib.auth import get_user_model
from django.core.files.base import ContentFile
from django.core.exceptions import ValidationError
from django.core.files.uploadedfile import SimpleUploadedFile
from django.core.management import call_command
from django.core.management.base import CommandError
from django.db import IntegrityError, transaction
from django.test import Client, SimpleTestCase, TestCase, TransactionTestCase, override_settings
from django.urls import reverse

from .access import (
    assign_role,
    get_user_role,
    role_has_permission,
    user_has_permission,
)
from .audit import record_audit
from .audio_prompts import AudioPromptConversionError, create_audio_prompt_from_upload
from .agent_client import AgentConfig, portal_url_to_websocket_url, read_active_config_marker, report_active_config_once
from .config_export import (
    ASTERISK_CONFIG_FILENAMES,
    build_asterisk_config_files,
    create_config_version,
    build_location_config,
    build_route_generation_choices,
    mac_to_sep_filename,
    select_route_caller_id,
    validate_location_routing,
)
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

    def test_htmx_request_returns_partial_content(self):
        self.client.force_login(self.viewer)

        response = self.client.get(reverse("extensions"), headers={"HX-Request": "true"})

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'data-area="extensions"')
        self.assertNotContains(response, "<html")


class LocationFormValidationTests(TestCase):
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
            "ami_username",
            "ami_secret",
            "agent_secret",
        ):
            with self.subTest(field_name=field_name):
                self.assertIn(field_name, form.errors)

    def test_admin_form_accepts_complete_location_record(self):
        form = LocationForm(data=location_form_data(), include_sensitive_fields=True)

        self.assertTrue(form.is_valid(), form.errors)

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

    def test_viewer_cannot_create_location(self):
        self.client.force_login(self.viewer)

        response = self.client.get(reverse("location-create"))

        self.assertEqual(response.status_code, 403)

    def test_editor_form_hides_sensitive_fields_and_shows_emergency_fields(self):
        self.client.force_login(self.editor)

        response = self.client.get(reverse("location-create"))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Emergency caller ID")
        self.assertContains(response, "Emergency trunk")
        self.assertContains(response, "Restricted Settings")
        self.assertNotContains(response, 'name="deployment_ssh_private_key"')
        self.assertNotContains(response, 'name="agent_secret"')
        self.assertNotContains(response, 'name="ami_secret"')

    def test_admin_form_shows_sensitive_deployment_and_agent_fields(self):
        self.client.force_login(self.admin)

        response = self.client.get(reverse("location-create"))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'name="deployment_ssh_private_key"')
        self.assertContains(response, 'name="deployment_ssh_host"')
        self.assertContains(response, 'name="agent_secret"')
        self.assertContains(response, 'name="ami_secret"')

    def test_admin_can_create_complete_location_record(self):
        self.client.force_login(self.admin)

        response = self.client.post(reverse("location-create"), location_form_data())

        location = Location.objects.get(slug="branch-office")
        self.assertEqual(response.status_code, 302)
        self.assertEqual(location.lan_subnet, "10.30.0.0/24")
        self.assertEqual(location.default_did, "+15551203000")
        self.assertEqual(location.emergency_caller_id, "+15551203999")
        self.assertEqual(location.deployment_ssh_private_key, "branch-private-key")
        self.assertEqual(location.deployment_status, Location.DeploymentStatus.READY)

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

        self.client.force_login(self.editor)
        editor_response = self.client.get(reverse("location-detail", args=[location.slug]))

        self.assertContains(editor_response, "Export ZIP")
        self.assertContains(editor_response, "Download")
        self.assertNotContains(editor_response, ">Deploy</button>")
        self.assertNotContains(editor_response, ">Rollback</button>")

        self.client.force_login(operator)
        operator_response = self.client.get(reverse("location-detail", args=[location.slug]))

        self.assertNotContains(operator_response, "Export ZIP")
        self.assertNotContains(operator_response, "Download")
        self.assertContains(operator_response, ">Deploy</button>")
        self.assertContains(operator_response, ">Rollback</button>")

        self.client.force_login(self.viewer)
        self.assertEqual(
            self.client.post(reverse("location-config-export", args=[location.slug])).status_code,
            403,
        )
        self.assertEqual(
            self.client.get(reverse("location-config-export-download", args=[location.slug, first.version_number])).status_code,
            403,
        )

    def test_export_download_deploy_and_rollback_actions_update_history(self):
        operator = User.objects.create_user(username="deploy-operator", password="portal-pass")
        assign_role(operator, PortalRole.OPERATOR)
        location = Location.objects.create(**location_model_data(name="Deploy HQ", slug="deploy-hq"))
        Extension.objects.create(location=location, number="3000", display_name="Deploy Desk")
        add_emergency_route(location)

        self.client.force_login(self.editor)
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

        self.client.force_login(operator)
        deploy_response = self.client.post(
            reverse("location-config-export-deploy", args=[location.slug, version.version_number])
        )

        self.assertEqual(deploy_response.status_code, 302)
        version.refresh_from_db()
        location.refresh_from_db()
        self.assertEqual(version.deployment_status, ConfigVersion.DeploymentStatus.DEPLOYED)
        self.assertEqual(location.deployment_status, Location.DeploymentStatus.DEPLOYED)
        self.assertIsNotNone(location.last_deployed_at)

        rollback_response = self.client.post(
            reverse("location-config-export-rollback", args=[location.slug, version.version_number])
        )

        self.assertEqual(rollback_response.status_code, 302)
        version.refresh_from_db()
        self.assertEqual(version.deployment_status, ConfigVersion.DeploymentStatus.ROLLED_BACK)

    def test_editor_update_ignores_spoofed_sensitive_fields(self):
        location = Location.objects.create(
            **location_model_data(
                name="Spoof Target",
                slug="spoof-target",
                deployment_ssh_private_key="original-private-key",
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
                smtp_password="spoofed-smtp-password",
                ami_secret="spoofed-ami-secret",
                agent_secret="spoofed-agent-secret",
            ),
        )

        self.assertEqual(response.status_code, 302)
        location.refresh_from_db()
        self.assertEqual(location.name, "Spoof Target Updated")
        self.assertEqual(location.deployment_ssh_private_key, "original-private-key")
        self.assertEqual(location.smtp_password, "original-smtp-password")
        self.assertEqual(location.ami_secret, "original-ami-secret")
        self.assertEqual(location.agent_secret, "original-agent-secret")


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
        self.assertIn("emergency_trunk_missing_credentials", {error["code"] for error in validation["errors"]})


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
        self.assertIn("[transport-tcp]", configs["pjsip.conf"])
        self.assertIn("protocol=tcp", configs["pjsip.conf"])
        self.assertIn("hook=/usr/local/sbin/pbx-recording-retention", configs["retention.conf"])

    def test_pjsip_iax_dialplan_queue_and_voicemail_golden_files(self):
        configs = build_asterisk_config_files(self.hq)

        for filename in ("pjsip.conf", "iax.conf", "extensions.conf", "queues.conf", "voicemail.conf"):
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
                query_string=f"token={location.agent_token}&secret=agent-secret".encode("utf-8"),
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
                    "asterisk/voicemail.conf",
                    "docker-compose.yml",
                    "manifest.json",
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
        self.assertEqual(version.checksum, hashlib.sha256(bytes(version.archive)).hexdigest())
        self.assertEqual(
            {file["path"] for file in version.file_manifest},
            set(names),
        )

    def test_runtime_bundle_files_match_golden_templates(self):
        version = create_config_version(self.location, exported_by=self.user)

        with zipfile.ZipFile(BytesIO(bytes(version.archive))) as archive:
            docker_compose = archive.read("docker-compose.yml").decode("utf-8")
            env_example = archive.read(".env.example").decode("utf-8")

        self.assertEqual(docker_compose, self._runtime_golden("docker-compose.yml"))
        self.assertEqual(env_example, self._runtime_golden(".env.example"))

    def test_runtime_bundle_compose_services_and_volume_paths(self):
        version = create_config_version(self.location, exported_by=self.user)

        with zipfile.ZipFile(BytesIO(bytes(version.archive))) as archive:
            services = self._compose_service_blocks(archive.read("docker-compose.yml").decode("utf-8"))

        self.assertEqual(set(services), {"asterisk", "tftp", "provisioning-http", "pbx-agent"})
        self.assertIn("    image: ${PBX_ASTERISK_IMAGE:-ghcr.io/apresz/asterisk:22-lts}", services["asterisk"])
        self.assertIn("    network_mode: host", services["asterisk"])
        self.assertIn("      - ./asterisk:/etc/asterisk:ro", services["asterisk"])
        self.assertIn("      - ./tftp:/srv/tftp:ro", services["tftp"])
        self.assertIn('      - "${PROVISIONING_TFTP_PORT:-69}:69/udp"', services["tftp"])
        self.assertIn("      - ./tftp:/usr/share/nginx/html/cisco:ro", services["provisioning-http"])
        self.assertIn('      - "${PROVISIONING_HTTP_PORT:-80}:80/tcp"', services["provisioning-http"])
        self.assertIn("    network_mode: host", services["pbx-agent"])
        self.assertIn('      PBX_AGENT_WS_URL: "${PBX_AGENT_WS_URL:?PBX_AGENT_WS_URL is required}"', services["pbx-agent"])
        self.assertIn('      PBX_AGENT_TOKEN: "${PBX_AGENT_TOKEN:?PBX_AGENT_TOKEN is required}"', services["pbx-agent"])
        self.assertIn('      PBX_AGENT_SECRET: "${PBX_AGENT_SECRET:?PBX_AGENT_SECRET is required}"', services["pbx-agent"])
        self.assertIn("      PBX_ACTIVE_CONFIG_MARKER: ${PBX_ACTIVE_CONFIG_MARKER:-/etc/asterisk/pbx-active-config.json}", services["pbx-agent"])

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
        for model, product, mac_address, load_name, extension_number in model_cases:
            with self.subTest(model=model):
                filename = mac_to_sep_filename(mac_address)
                self.assertEqual(
                    files[filename]["content"],
                    self._expected_phone_xml(
                        model=model,
                        product=product,
                        label=f"{model} Phone",
                        load_name=load_name,
                        lines=[
                            {
                                "button": "1",
                                "label": "Primary",
                                "number": extension_number,
                                "display": f"Desk {extension_number}",
                                "auth": f"sip{extension_number}",
                                "password": f"secret{extension_number}",
                            }
                        ],
                    ),
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
            self._expected_phone_xml(
                model=Phone.PhoneModel.CISCO_9971,
                product="Cisco CP-9971",
                label="Reception Phone",
                load_name="sip9971.9-4-2",
                lines=[
                    {
                        "button": "1",
                        "label": "Primary",
                        "number": "5100",
                        "display": "Reception",
                        "auth": "sip5100",
                        "password": "secret5100",
                    },
                    {
                        "button": "2",
                        "label": "Sales",
                        "number": "5101",
                        "display": "Sales",
                        "auth": "5101",
                        "password": "secret5101",
                    },
                ],
                speed_dials=[
                    {
                        "button": "3",
                        "label": "Support",
                        "destination": "5101",
                    }
                ],
            ),
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
            """<?xml version='1.0' encoding='utf-8'?>
<CiscoIPPhoneDirectory>
  <Title>Company Directory</Title>
  <Prompt>Extensions grouped by location</Prompt>
  <DirectoryEntry>
    <Name>Provision HQ - Reception</Name>
    <Telephone>5100</Telephone>
    <Location>Provision HQ</Location>
  </DirectoryEntry>
  <DirectoryEntry>
    <Name>Provision HQ - Sales</Name>
    <Telephone>5101</Telephone>
    <Location>Provision HQ</Location>
  </DirectoryEntry>
  <DirectoryEntry>
    <Name>Provision Warehouse - Warehouse Desk</Name>
    <Telephone>6100</Telephone>
    <Location>Provision Warehouse</Location>
  </DirectoryEntry>
</CiscoIPPhoneDirectory>""",
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

    def _expected_phone_xml(self, *, model, product, label, load_name, lines, speed_dials=None):
        speed_dials = speed_dials or []
        line_xml = "\n".join(
            [
                f"""      <line button="{line['button']}">
        <featureID>9</featureID>
        <featureLabel>{line['label']}</featureLabel>
        <proxy>USECALLMANAGER</proxy>
        <port>5060</port>
        <name>{line['number']}</name>
        <displayName>{line['display']}</displayName>
        <authName>{line['auth']}</authName>
        <authPassword>{line['password']}</authPassword>
        <contact>{line['number']}</contact>
        <messagesNumber>*97</messagesNumber>
      </line>"""
                for line in lines
            ]
            + [
                f"""      <line button="{speed_dial['button']}">
        <featureID>2</featureID>
        <featureLabel>{speed_dial['label']}</featureLabel>
        <speedDialNumber>{speed_dial['destination']}</speedDialNumber>
      </line>"""
                for speed_dial in speed_dials
            ]
        )
        return f"""<?xml version='1.0' encoding='utf-8'?>
<device>
  <deviceProtocol>SIP</deviceProtocol>
  <product>{product}</product>
  <model>{model}</model>
  <phoneLabel>{label}</phoneLabel>
  <transportLayerProtocol>TCP</transportLayerProtocol>
  <directoryURL>http://10.50.0.10/cisco/company-directory.xml</directoryURL>
  <loadInformation>{load_name}</loadInformation>
  <devicePool>
    <dateTimeSetting>
      <dateTemplate>M/D/Ya</dateTemplate>
      <timeZone>America/Los_Angeles</timeZone>
    </dateTimeSetting>
    <callManagerGroup>
      <members>
        <member priority="0">
          <callManager>
            <ports>
              <ethernetPhonePort>2000</ethernetPhonePort>
              <sipPort>5060</sipPort>
              <securedSipPort>5061</securedSipPort>
            </ports>
            <processNodeName>10.50.0.10</processNodeName>
          </callManager>
        </member>
      </members>
    </callManagerGroup>
  </devicePool>
  <sipProfile>
    <sipProxies>
      <backupProxy />
      <backupProxyPort />
      <emergencyProxy />
      <emergencyProxyPort />
      <outboundProxy />
      <outboundProxyPort />
      <registerWithProxy>true</registerWithProxy>
    </sipProxies>
    <sipPort>5060</sipPort>
    <transportLayerProtocol>TCP</transportLayerProtocol>
    <phoneLabel>{label}</phoneLabel>
    <dialTemplate>dialplan.xml</dialTemplate>
    <sipLines>
{line_xml}
    </sipLines>
  </sipProfile>
  <phoneServices>
    <provisioning>0</provisioning>
    <phoneService type="1" category="0">
      <name>Company Directory</name>
      <url>http://10.50.0.10/cisco/company-directory.xml</url>
      <vendor>Local PBX</vendor>
      <version>1</version>
    </phoneService>
  </phoneServices>
</device>"""


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
                PortalPermission.ADMINISTER: False,
            },
            self.editor: {
                PortalPermission.VIEW: True,
                PortalPermission.EDIT_CONFIG: True,
                PortalPermission.RUN_LIVE_OPERATIONS: False,
                PortalPermission.ADMINISTER: False,
            },
            self.operator: {
                PortalPermission.VIEW: True,
                PortalPermission.EDIT_CONFIG: False,
                PortalPermission.RUN_LIVE_OPERATIONS: True,
                PortalPermission.ADMINISTER: False,
            },
            self.admin: {
                PortalPermission.VIEW: True,
                PortalPermission.EDIT_CONFIG: True,
                PortalPermission.RUN_LIVE_OPERATIONS: True,
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
            self.assertIn(b"suitable for off-host storage", archive.read("README.txt").lower())

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

    def _post_json(self, url, payload):
        return self.client.post(url, data=json.dumps(payload), content_type="application/json")


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
