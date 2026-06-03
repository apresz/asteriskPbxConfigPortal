import csv
from io import StringIO
import json

from django.contrib.auth import get_user_model
from django.core.exceptions import ValidationError
from django.core.management import call_command
from django.db import IntegrityError, transaction
from django.test import Client, SimpleTestCase, TestCase, override_settings
from django.urls import reverse

from .access import (
    assign_role,
    get_user_role,
    role_has_permission,
    user_has_permission,
)
from .audit import record_audit
from .config_export import build_location_config
from .extension_csv import ExtensionCSVError, export_extensions_csv, extension_template_csv, import_extensions_csv
from .extension_management import sync_extension_relationships
from .forms import ExtensionForm, LocationForm
from .models import (
    APIKey,
    AuditAction,
    AuditLog,
    AuditOutcome,
    DID,
    IVR,
    IVRMenuOption,
    CallQueue,
    Extension,
    InboundDestination,
    Location,
    OutboundRoute,
    OutboundRouteTrunk,
    PagingGroup,
    PagingGroupMember,
    Phone,
    PhoneLineAppearance,
    PortalPermission,
    PortalRole,
    QueueMember,
    RingGroup,
    RingGroupMember,
    ServiceIdentity,
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
        self.assertContains(response, "Dial Plan")
        self.assertContains(response, "viewer - Viewer")
        self.assertNotContains(response, "Settings")

    def test_initial_portal_area_routes_render(self):
        self.client.force_login(self.viewer)
        route_names = ["extensions", "trunks", "dial-plan"]

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
        output = StringIO()

        call_command("export_pbx_config", location.slug, stdout=output)

        payload = json.loads(output.getvalue())
        self.assertEqual(payload["location"]["slug"], "command-hq")
        self.assertEqual(payload["helper_scripts"]["recording_retention_days"], 90)
        self.assertEqual(payload["voicemail"]["mailboxes"][0]["number"], "3000")


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
