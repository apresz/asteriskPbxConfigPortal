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
from .forms import LocationForm
from .models import (
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
        "recording_retention_days": "365",
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
            "smtp_host",
            "smtp_port",
            "smtp_from_email",
            "smtp_username",
            "smtp_password",
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
