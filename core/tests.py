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
