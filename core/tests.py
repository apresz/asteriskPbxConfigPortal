from io import StringIO

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
from .extension_csv import EXTENSION_CSV_FIELDS, export_extensions_csv, extension_csv_template, import_extensions_csv
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


class ExtensionManagementViewTests(TestCase):
    def setUp(self):
        self.location = Location.objects.create(name="Headquarters", slug="headquarters")
        self.admin = User.objects.create_user(username="extension-admin", password="portal-pass")
        self.editor = User.objects.create_user(username="extension-editor", password="portal-pass")
        assign_role(self.admin, PortalRole.ADMIN)
        assign_role(self.editor, PortalRole.EDITOR)

    def test_extension_list_replaces_placeholder_with_management_actions(self):
        self.client.force_login(self.admin)

        response = self.client.get(reverse("extensions"))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Create")
        self.assertContains(response, "Import")
        self.assertContains(response, "Template")
        self.assertContains(response, "Export")
        self.assertNotContains(response, "Ready for provisioning model work")

    def test_extension_create_rejects_duplicate_number(self):
        Extension.objects.create(
            location=self.location,
            number="1000",
            display_name="Reception",
        )
        self.client.force_login(self.admin)

        response = self.client.post(
            reverse("extension_create"),
            data=self._extension_form_data(number="1000", display_name="Duplicate"),
        )

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Extension with this Number already exists.")
        self.assertEqual(Extension.objects.filter(number="1000").count(), 1)

    def test_editor_cannot_disable_911_and_denied_attempt_is_audited(self):
        self.client.force_login(self.editor)

        response = self.client.post(
            reverse("extension_create"),
            data=self._extension_form_data(number="1002", emergency_calling_enabled=False),
        )

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Only Admin users can disable 911 calling")
        self.assertFalse(Extension.objects.filter(number="1002").exists())
        audit = AuditLog.objects.get()
        self.assertEqual(audit.actor, self.editor)
        self.assertEqual(audit.outcome, AuditOutcome.DENIED)
        self.assertEqual(audit.target, "extensions/1002")
        self.assertEqual(audit.details["field"], "emergency_calling_enabled")

    def test_admin_can_disable_911_and_success_is_audited(self):
        self.client.force_login(self.admin)

        response = self.client.post(
            reverse("extension_create"),
            data=self._extension_form_data(number="1003", emergency_calling_enabled=False),
        )

        self.assertEqual(response.status_code, 302)
        extension = Extension.objects.get(number="1003")
        self.assertFalse(extension.emergency_calling_enabled)
        audit = AuditLog.objects.get()
        self.assertEqual(audit.actor, self.admin)
        self.assertEqual(audit.outcome, AuditOutcome.SUCCESS)
        self.assertEqual(audit.target, "extensions/1003")

    def _extension_form_data(self, **overrides):
        data = {
            "location": str(self.location.pk),
            "number": "1001",
            "display_name": "Sales",
            "email": "sales@example.test",
            "sip_username": "1001",
            "sip_password": "sip-secret",
            "voicemail_enabled": "on",
            "voicemail_pin": "1234",
            "caller_id_name": "Sales",
            "caller_id_number": "+15551201001",
            "recording_policy": Extension.RecordingPolicy.ON_DEMAND,
            "emergency_calling_enabled": "on",
            "is_active": "on",
        }
        data.update({key: value for key, value in overrides.items() if value is not False})
        for key, value in overrides.items():
            if value is False:
                data.pop(key, None)
        return data


class ExtensionCsvTests(TestCase):
    def setUp(self):
        self.location = Location.objects.create(name="Headquarters", slug="headquarters")
        self.admin = User.objects.create_user(username="csv-admin", password="portal-pass")
        self.editor = User.objects.create_user(username="csv-editor", password="portal-pass")
        assign_role(self.admin, PortalRole.ADMIN)
        assign_role(self.editor, PortalRole.EDITOR)

    def test_extension_csv_template_includes_membership_columns(self):
        header = extension_csv_template().splitlines()[0].split(",")

        self.assertEqual(header, EXTENSION_CSV_FIELDS)
        self.assertIn("direct_dids", header)
        self.assertIn("ring_groups", header)
        self.assertIn("queues", header)
        self.assertIn("paging_groups", header)

    def test_extension_csv_export_import_round_trips_attributes_and_memberships(self):
        extension = Extension.objects.create(
            location=self.location,
            number="1100",
            display_name="Support",
            email="support@example.test",
            sip_username="1100",
            sip_password="sip-secret",
            voicemail_enabled=True,
            voicemail_pin="4321",
            caller_id_name="Support",
            caller_id_number="+15551201100",
            recording_policy=Extension.RecordingPolicy.ALWAYS,
        )
        destination = InboundDestination.objects.create(
            location=self.location,
            name="Support extension",
            destination_type=InboundDestination.DestinationType.EXTENSION,
            extension=extension,
        )
        did = DID.objects.create(
            location=self.location,
            number="+15551201100",
            default_destination=destination,
            direct_extension=extension,
        )
        ring_group = RingGroup.objects.create(location=self.location, name="Support Ring")
        queue = CallQueue.objects.create(location=self.location, name="Support Queue")
        paging_group = PagingGroup.objects.create(
            location=self.location,
            name="Support Page",
            page_code="7100",
        )
        RingGroupMember.objects.create(ring_group=ring_group, extension=extension)
        QueueMember.objects.create(queue=queue, extension=extension)
        PagingGroupMember.objects.create(paging_group=paging_group, extension=extension)

        csv_text = export_extensions_csv(Extension.objects.filter(pk=extension.pk))
        did.direct_extension = None
        did.save()
        RingGroupMember.objects.filter(extension=extension).delete()
        QueueMember.objects.filter(extension=extension).delete()
        PagingGroupMember.objects.filter(extension=extension).delete()
        extension.email = ""
        extension.sip_password = ""
        extension.recording_policy = Extension.RecordingPolicy.NEVER
        extension.save()

        result = import_extensions_csv(StringIO(csv_text), actor=self.admin)

        self.assertEqual(result.errors, [])
        self.assertEqual(result.imported_count, 1)
        extension.refresh_from_db()
        did.refresh_from_db()
        self.assertEqual(extension.email, "support@example.test")
        self.assertEqual(extension.sip_password, "sip-secret")
        self.assertEqual(extension.recording_policy, Extension.RecordingPolicy.ALWAYS)
        self.assertEqual(did.direct_extension, extension)
        self.assertTrue(RingGroupMember.objects.filter(ring_group=ring_group, extension=extension).exists())
        self.assertTrue(QueueMember.objects.filter(queue=queue, extension=extension).exists())
        self.assertTrue(PagingGroupMember.objects.filter(paging_group=paging_group, extension=extension).exists())

    def test_extension_csv_import_rejects_duplicate_numbers_in_file(self):
        csv_text = "\n".join(
            [
                ",".join(EXTENSION_CSV_FIELDS),
                "headquarters,1200,Desk,,,,,true,,Desk,+15551201200,inherit,true,true,,,",
                "headquarters,1200,Desk 2,,,,,true,,Desk,+15551201201,inherit,true,true,,,",
            ]
        )

        result = import_extensions_csv(StringIO(csv_text), actor=self.admin)

        self.assertEqual(result.imported_count, 0)
        self.assertIn("duplicate extension number 1200", result.errors[0])
        self.assertFalse(Extension.objects.filter(number="1200").exists())

    def test_extension_csv_import_requires_admin_for_911_disable(self):
        csv_text = "\n".join(
            [
                ",".join(EXTENSION_CSV_FIELDS),
                "headquarters,1300,Desk,,,,,true,,Desk,+15551201300,inherit,false,true,,,",
            ]
        )

        result = import_extensions_csv(StringIO(csv_text), actor=self.editor)

        self.assertEqual(result.imported_count, 0)
        self.assertIn("Only Admin users can disable 911 calling", result.errors[0])
        self.assertFalse(Extension.objects.filter(number="1300").exists())
        audit = AuditLog.objects.get()
        self.assertEqual(audit.outcome, AuditOutcome.DENIED)


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
