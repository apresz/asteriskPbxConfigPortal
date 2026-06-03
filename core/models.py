import ipaddress

from django.conf import settings
from django.core.exceptions import ValidationError
from django.core.validators import MaxValueValidator, MinValueValidator, RegexValidator
from django.db import models


def cidr_network_validator(value):
    try:
        ipaddress.ip_network(value, strict=False)
    except ValueError as exc:
        raise ValidationError("Enter a valid IPv4 or IPv6 CIDR network.") from exc


def ip_address_validator(value):
    try:
        ipaddress.ip_address(value)
    except ValueError as exc:
        raise ValidationError("Enter a valid IPv4 or IPv6 address.") from exc


port_validators = [MinValueValidator(1), MaxValueValidator(65535)]


extension_number_validator = RegexValidator(
    regex=r"^\d{4}$",
    message="Extension numbers must be exactly four digits.",
)

voicemail_pin_validator = RegexValidator(
    regex=r"^\d{4,12}$",
    message="Voicemail PINs must be 4 to 12 digits.",
)

did_number_validator = RegexValidator(
    regex=r"^\+?[1-9]\d{6,14}$",
    message="DIDs must be 7 to 15 digits, optionally prefixed with '+'.",
)

mac_address_validator = RegexValidator(
    regex=r"^[0-9A-F]{12}$",
    message="MAC addresses must be 12 uppercase hexadecimal characters.",
)


class TimestampedModel(models.Model):
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        abstract = True


class PortalRole(models.TextChoices):
    VIEWER = "viewer", "Viewer"
    EDITOR = "editor", "Editor"
    OPERATOR = "operator", "Operator"
    ADMIN = "admin", "Admin"


class PortalPermission(models.TextChoices):
    VIEW = "view", "View portal"
    EDIT_CONFIG = "edit_config", "Edit configuration"
    RUN_LIVE_OPERATIONS = "run_live_operations", "Run live operations"
    ADMINISTER = "administer", "Administer portal"


class AuditAction(models.TextChoices):
    CONFIG_CHANGE = "config_change", "Config change"
    CONFIG_EXPORT = "config_export", "Config export"
    DEPLOYMENT = "deployment", "Deployment"
    LIVE_PBX_ACTION = "live_pbx_action", "Live PBX action"


class AuditOutcome(models.TextChoices):
    SUCCESS = "success", "Success"
    FAILURE = "failure", "Failure"
    DENIED = "denied", "Denied"


class PortalUserProfile(TimestampedModel):
    user = models.OneToOneField(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="portal_profile",
    )
    role = models.CharField(
        max_length=16,
        choices=PortalRole.choices,
        default=PortalRole.VIEWER,
    )

    def __str__(self) -> str:
        return f"{self.user.get_username()} ({self.get_role_display()})"


class AuditLog(models.Model):
    actor = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="audit_logs",
    )
    action = models.CharField(max_length=32, choices=AuditAction.choices)
    target = models.CharField(max_length=255)
    timestamp = models.DateTimeField(auto_now_add=True, db_index=True)
    outcome = models.CharField(max_length=16, choices=AuditOutcome.choices)
    details = models.JSONField(default=dict, blank=True)

    class Meta:
        ordering = ["-timestamp", "-id"]

    def __str__(self) -> str:
        actor = self.actor.get_username() if self.actor_id else "system"
        return f"{self.get_action_display()} on {self.target} by {actor}: {self.get_outcome_display()}"


class Location(TimestampedModel):
    class DeploymentStatus(models.TextChoices):
        NOT_DEPLOYED = "not_deployed", "Not deployed"
        READY = "ready", "Ready"
        DEPLOYED = "deployed", "Deployed"
        FAILED = "failed", "Failed"

    name = models.CharField(max_length=120, unique=True)
    slug = models.SlugField(max_length=80, unique=True)
    description = models.TextField(blank=True)
    timezone = models.CharField(max_length=64, default="UTC")
    lan_subnet = models.CharField(
        "LAN subnet",
        max_length=43,
        validators=[cidr_network_validator],
    )
    pbx_lan_ip = models.CharField(
        "PBX LAN IP",
        max_length=39,
        validators=[ip_address_validator],
    )
    pbx_warp_ip = models.CharField(
        "PBX WARP IP",
        max_length=39,
        validators=[ip_address_validator],
    )
    deployment_ssh_host = models.CharField("deployment SSH host", max_length=255, blank=True)
    deployment_ssh_port = models.PositiveIntegerField(
        "deployment SSH port",
        default=22,
        validators=port_validators,
    )
    deployment_ssh_username = models.CharField(
        "deployment SSH username",
        max_length=80,
        blank=True,
    )
    deployment_ssh_private_key = models.TextField(
        "deployment SSH private key",
        blank=True,
    )
    deployment_ssh_known_hosts = models.TextField(
        "deployment SSH known hosts",
        blank=True,
    )
    sip_bind_ip = models.CharField(
        "SIP bind IP",
        max_length=39,
        validators=[ip_address_validator],
    )
    sip_port = models.PositiveIntegerField(
        "SIP port",
        default=5060,
        validators=port_validators,
    )
    rtp_port_start = models.PositiveIntegerField(
        "RTP port start",
        default=10000,
        validators=port_validators,
    )
    rtp_port_end = models.PositiveIntegerField(
        "RTP port end",
        default=20000,
        validators=port_validators,
    )
    iax_bind_ip = models.CharField(
        "IAX bind IP",
        max_length=39,
        validators=[ip_address_validator],
    )
    iax_port = models.PositiveIntegerField(
        "IAX port",
        default=4569,
        validators=port_validators,
    )
    default_did = models.CharField(
        "default DID",
        max_length=16,
        validators=[did_number_validator],
    )
    emergency_caller_id = models.CharField(
        "emergency caller ID",
        max_length=16,
        validators=[did_number_validator],
    )
    emergency_trunk = models.CharField("emergency trunk", max_length=120)
    recording_retention_days = models.PositiveIntegerField(
        "recording retention days",
        default=365,
        validators=[MinValueValidator(1)],
    )
    smtp_host = models.CharField("SMTP host", max_length=255)
    smtp_port = models.PositiveIntegerField(
        "SMTP port",
        default=587,
        validators=port_validators,
    )
    smtp_from_email = models.EmailField("SMTP from email")
    smtp_use_tls = models.BooleanField("SMTP use TLS", default=True)
    smtp_use_ssl = models.BooleanField("SMTP use SSL", default=False)
    smtp_username = models.CharField("SMTP username", max_length=120, blank=True)
    smtp_password = models.CharField("SMTP password", max_length=255, blank=True)
    ami_host = models.CharField("AMI host", max_length=255)
    ami_port = models.PositiveIntegerField(
        "AMI port",
        default=5038,
        validators=port_validators,
    )
    ami_username = models.CharField("AMI username", max_length=120, blank=True)
    ami_secret = models.CharField("AMI secret", max_length=255, blank=True)
    agent_secret = models.CharField("agent secret", max_length=255, blank=True)
    is_active = models.BooleanField(default=True)
    last_deployed_at = models.DateTimeField("last deployed", null=True, blank=True)
    deployment_status = models.CharField(
        max_length=24,
        choices=DeploymentStatus.choices,
        default=DeploymentStatus.NOT_DEPLOYED,
    )

    class Meta:
        ordering = ["name"]

    def clean(self):
        super().clean()
        errors = {}
        network = None
        if self.lan_subnet:
            try:
                network = ipaddress.ip_network(self.lan_subnet, strict=False)
            except ValueError:
                errors["lan_subnet"] = "Enter a valid IPv4 or IPv6 CIDR network."

        if network and self.pbx_lan_ip:
            try:
                pbx_lan_ip = ipaddress.ip_address(self.pbx_lan_ip)
            except ValueError:
                pass
            else:
                if pbx_lan_ip not in network:
                    errors["pbx_lan_ip"] = "PBX LAN IP must be inside the LAN subnet."

        if self.rtp_port_start and self.rtp_port_end and self.rtp_port_start > self.rtp_port_end:
            errors["rtp_port_end"] = "RTP port end must be greater than or equal to RTP port start."

        if self.smtp_use_tls and self.smtp_use_ssl:
            errors["smtp_use_ssl"] = "SMTP TLS and SSL cannot both be enabled."

        if errors:
            raise ValidationError(errors)

    def __str__(self):
        return self.name


class Provider(TimestampedModel):
    class ProviderType(models.TextChoices):
        SIP = "sip", "SIP"
        IAX2 = "iax2", "IAX2"
        OTHER = "other", "Other"

    name = models.CharField(max_length=120, unique=True)
    slug = models.SlugField(max_length=80, unique=True)
    provider_type = models.CharField(
        max_length=16,
        choices=ProviderType.choices,
        default=ProviderType.SIP,
    )
    notes = models.TextField(blank=True)
    is_active = models.BooleanField(default=True)

    class Meta:
        ordering = ["name"]

    def __str__(self):
        return self.name


class Extension(TimestampedModel):
    class RecordingPolicy(models.TextChoices):
        INHERIT = "inherit", "Inherit location policy"
        ALWAYS = "always", "Always record"
        ON_DEMAND = "on_demand", "On demand"
        NEVER = "never", "Never record"

    location = models.ForeignKey(
        Location,
        on_delete=models.CASCADE,
        related_name="extensions",
    )
    number = models.CharField(
        max_length=4,
        unique=True,
        validators=[extension_number_validator],
    )
    display_name = models.CharField(max_length=120)
    email = models.EmailField(blank=True)
    sip_username = models.CharField(max_length=80, blank=True)
    sip_password = models.CharField(max_length=255, blank=True)
    voicemail_enabled = models.BooleanField(default=True)
    voicemail_pin = models.CharField(
        max_length=12,
        blank=True,
        validators=[voicemail_pin_validator],
    )
    caller_id_name = models.CharField(max_length=80, blank=True)
    caller_id_number = models.CharField(
        max_length=16,
        blank=True,
        validators=[did_number_validator],
    )
    recording_policy = models.CharField(
        max_length=16,
        choices=RecordingPolicy.choices,
        default=RecordingPolicy.INHERIT,
    )
    emergency_calling_enabled = models.BooleanField(default=True)
    is_active = models.BooleanField(default=True)

    class Meta:
        ordering = ["number"]
        constraints = [
            models.CheckConstraint(
                condition=models.Q(number__regex=r"^\d{4}$"),
                name="extension_number_four_digits",
            ),
        ]

    def __str__(self):
        return f"{self.number} - {self.display_name}"


class Phone(TimestampedModel):
    class PhoneModel(models.TextChoices):
        CISCO_9971 = "CP-9971", "Cisco CP-9971"
        CISCO_9951 = "CP-9951", "Cisco CP-9951"
        CISCO_8961 = "CP-8961", "Cisco CP-8961"
        OTHER = "other", "Other"

    location = models.ForeignKey(
        Location,
        on_delete=models.CASCADE,
        related_name="phones",
    )
    mac_address = models.CharField(
        max_length=12,
        unique=True,
        validators=[mac_address_validator],
    )
    model = models.CharField(
        max_length=24,
        choices=PhoneModel.choices,
        default=PhoneModel.CISCO_9971,
    )
    label = models.CharField(max_length=120, blank=True)
    is_active = models.BooleanField(default=True)

    class Meta:
        ordering = ["mac_address"]
        constraints = [
            models.CheckConstraint(
                condition=models.Q(mac_address__regex=r"^[0-9A-F]{12}$"),
                name="phone_mac_upper_hex_12",
            ),
        ]

    def __str__(self):
        label = f" ({self.label})" if self.label else ""
        return f"{self.mac_address}{label}"


class PhoneLineAppearance(TimestampedModel):
    phone = models.ForeignKey(
        Phone,
        on_delete=models.CASCADE,
        related_name="line_appearances",
    )
    extension = models.ForeignKey(
        Extension,
        on_delete=models.PROTECT,
        related_name="phone_appearances",
    )
    line_index = models.PositiveSmallIntegerField(validators=[MinValueValidator(1)])
    label = models.CharField(max_length=120, blank=True)

    class Meta:
        ordering = ["phone", "line_index"]
        constraints = [
            models.UniqueConstraint(
                fields=["phone", "line_index"],
                name="unique_phone_line_index",
            ),
            models.UniqueConstraint(
                fields=["phone", "extension"],
                name="unique_phone_extension_appearance",
            ),
        ]

    def clean(self):
        super().clean()
        if (
            self.phone_id
            and self.extension_id
            and self.phone.location_id != self.extension.location_id
        ):
            raise ValidationError(
                {"extension": "Phone line extensions must belong to the phone location."}
            )

    def __str__(self):
        return f"{self.phone} line {self.line_index}: {self.extension}"


class IVR(TimestampedModel):
    location = models.ForeignKey(
        Location,
        on_delete=models.CASCADE,
        related_name="ivrs",
    )
    name = models.CharField(max_length=120)
    prompt_name = models.CharField(max_length=160, blank=True)
    is_active = models.BooleanField(default=True)

    class Meta:
        ordering = ["location", "name"]
        constraints = [
            models.UniqueConstraint(fields=["location", "name"], name="unique_ivr_name_per_location"),
        ]

    def __str__(self):
        return f"{self.location}: {self.name}"


class RingGroup(TimestampedModel):
    class Strategy(models.TextChoices):
        RING_ALL = "ring_all", "Ring all"
        HUNT = "hunt", "Hunt"

    location = models.ForeignKey(
        Location,
        on_delete=models.CASCADE,
        related_name="ring_groups",
    )
    name = models.CharField(max_length=120)
    strategy = models.CharField(
        max_length=16,
        choices=Strategy.choices,
        default=Strategy.RING_ALL,
    )
    timeout_seconds = models.PositiveSmallIntegerField(default=20)
    is_active = models.BooleanField(default=True)

    class Meta:
        ordering = ["location", "name"]
        constraints = [
            models.UniqueConstraint(
                fields=["location", "name"],
                name="unique_ring_group_name_per_location",
            ),
        ]

    def __str__(self):
        return f"{self.location}: {self.name}"


class CallQueue(TimestampedModel):
    class Strategy(models.TextChoices):
        RING_ALL = "ring_all", "Ring all"
        LEAST_RECENT = "least_recent", "Least recent"
        FEWEST_CALLS = "fewest_calls", "Fewest calls"
        ROUND_ROBIN = "round_robin", "Round robin"

    location = models.ForeignKey(
        Location,
        on_delete=models.CASCADE,
        related_name="queues",
    )
    name = models.CharField(max_length=120)
    strategy = models.CharField(
        max_length=24,
        choices=Strategy.choices,
        default=Strategy.RING_ALL,
    )
    timeout_seconds = models.PositiveSmallIntegerField(default=30)
    retry_seconds = models.PositiveSmallIntegerField(default=5)
    music_on_hold = models.CharField(max_length=80, blank=True)
    is_active = models.BooleanField(default=True)

    class Meta:
        ordering = ["location", "name"]
        constraints = [
            models.UniqueConstraint(
                fields=["location", "name"],
                name="unique_queue_name_per_location",
            ),
        ]

    def __str__(self):
        return f"{self.location}: {self.name}"


class PagingGroup(TimestampedModel):
    location = models.ForeignKey(
        Location,
        on_delete=models.CASCADE,
        related_name="paging_groups",
    )
    name = models.CharField(max_length=120)
    page_code = models.CharField(
        max_length=4,
        unique=True,
        validators=[extension_number_validator],
    )
    is_active = models.BooleanField(default=True)

    class Meta:
        ordering = ["page_code"]
        constraints = [
            models.CheckConstraint(
                condition=models.Q(page_code__regex=r"^\d{4}$"),
                name="paging_group_page_code_four_digits",
            ),
            models.UniqueConstraint(
                fields=["location", "name"],
                name="unique_paging_group_name_per_location",
            ),
        ]

    def __str__(self):
        return f"{self.page_code} - {self.name}"


class InboundDestination(TimestampedModel):
    class DestinationType(models.TextChoices):
        EXTENSION = "extension", "Extension"
        IVR = "ivr", "IVR"
        RING_GROUP = "ring_group", "Ring group"
        QUEUE = "queue", "Queue"

    location = models.ForeignKey(
        Location,
        on_delete=models.CASCADE,
        related_name="inbound_destinations",
    )
    name = models.CharField(max_length=120)
    destination_type = models.CharField(max_length=24, choices=DestinationType.choices)
    extension = models.ForeignKey(
        Extension,
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="inbound_destinations",
    )
    ivr = models.ForeignKey(
        IVR,
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="inbound_destinations",
    )
    ring_group = models.ForeignKey(
        RingGroup,
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="inbound_destinations",
    )
    queue = models.ForeignKey(
        CallQueue,
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="inbound_destinations",
    )

    class Meta:
        ordering = ["location", "name"]
        constraints = [
            models.UniqueConstraint(
                fields=["location", "name"],
                name="unique_inbound_destination_name_per_location",
            ),
            models.CheckConstraint(
                condition=(
                    models.Q(
                        destination_type="extension",
                        extension__isnull=False,
                        ivr__isnull=True,
                        ring_group__isnull=True,
                        queue__isnull=True,
                    )
                    | models.Q(
                        destination_type="ivr",
                        extension__isnull=True,
                        ivr__isnull=False,
                        ring_group__isnull=True,
                        queue__isnull=True,
                    )
                    | models.Q(
                        destination_type="ring_group",
                        extension__isnull=True,
                        ivr__isnull=True,
                        ring_group__isnull=False,
                        queue__isnull=True,
                    )
                    | models.Q(
                        destination_type="queue",
                        extension__isnull=True,
                        ivr__isnull=True,
                        ring_group__isnull=True,
                        queue__isnull=False,
                    )
                ),
                name="inbound_destination_single_matching_target",
            ),
        ]

    @property
    def target(self):
        return {
            self.DestinationType.EXTENSION: self.extension,
            self.DestinationType.IVR: self.ivr,
            self.DestinationType.RING_GROUP: self.ring_group,
            self.DestinationType.QUEUE: self.queue,
        }.get(self.destination_type)

    def clean(self):
        super().clean()
        target = self.target
        if target and self.location_id and target.location_id != self.location_id:
            raise ValidationError(
                "Inbound destination targets must belong to the destination location."
            )

    def __str__(self):
        return f"{self.location}: {self.name}"


class IVRMenuOption(TimestampedModel):
    ivr = models.ForeignKey(
        IVR,
        on_delete=models.CASCADE,
        related_name="menu_options",
    )
    digit = models.CharField(
        max_length=1,
        validators=[
            RegexValidator(regex=r"^[0-9]$", message="IVR menu digits must be 0-9.")
        ],
    )
    destination = models.ForeignKey(
        InboundDestination,
        on_delete=models.PROTECT,
        related_name="ivr_menu_options",
    )
    label = models.CharField(max_length=120, blank=True)

    class Meta:
        ordering = ["ivr", "digit"]
        constraints = [
            models.UniqueConstraint(fields=["ivr", "digit"], name="unique_ivr_digit"),
            models.CheckConstraint(
                condition=models.Q(digit__regex=r"^[0-9]$"),
                name="ivr_menu_option_digit",
            ),
        ]

    def clean(self):
        super().clean()
        if (
            self.ivr_id
            and self.destination_id
            and self.ivr.location_id != self.destination.location_id
        ):
            raise ValidationError(
                {"destination": "IVR menu destinations must belong to the IVR location."}
            )

    def __str__(self):
        return f"{self.ivr} option {self.digit}"


class RingGroupMember(TimestampedModel):
    ring_group = models.ForeignKey(
        RingGroup,
        on_delete=models.CASCADE,
        related_name="members",
    )
    extension = models.ForeignKey(
        Extension,
        on_delete=models.PROTECT,
        related_name="ring_group_memberships",
    )
    priority = models.PositiveSmallIntegerField(default=1, validators=[MinValueValidator(1)])

    class Meta:
        ordering = ["ring_group", "priority", "extension"]
        constraints = [
            models.UniqueConstraint(
                fields=["ring_group", "extension"],
                name="unique_ring_group_member",
            ),
        ]

    def clean(self):
        super().clean()
        if (
            self.ring_group_id
            and self.extension_id
            and self.ring_group.location_id != self.extension.location_id
        ):
            raise ValidationError(
                {"extension": "Ring group members must belong to the ring group location."}
            )

    def __str__(self):
        return f"{self.ring_group}: {self.extension}"


class QueueMember(TimestampedModel):
    queue = models.ForeignKey(
        CallQueue,
        on_delete=models.CASCADE,
        related_name="members",
    )
    extension = models.ForeignKey(
        Extension,
        on_delete=models.PROTECT,
        related_name="queue_memberships",
    )
    penalty = models.PositiveSmallIntegerField(default=0)

    class Meta:
        ordering = ["queue", "penalty", "extension"]
        constraints = [
            models.UniqueConstraint(fields=["queue", "extension"], name="unique_queue_member"),
        ]

    def clean(self):
        super().clean()
        if (
            self.queue_id
            and self.extension_id
            and self.queue.location_id != self.extension.location_id
        ):
            raise ValidationError(
                {"extension": "Queue members must belong to the queue location."}
            )

    def __str__(self):
        return f"{self.queue}: {self.extension}"


class PagingGroupMember(TimestampedModel):
    paging_group = models.ForeignKey(
        PagingGroup,
        on_delete=models.CASCADE,
        related_name="members",
    )
    extension = models.ForeignKey(
        Extension,
        on_delete=models.PROTECT,
        related_name="paging_group_memberships",
    )

    class Meta:
        ordering = ["paging_group", "extension"]
        constraints = [
            models.UniqueConstraint(
                fields=["paging_group", "extension"],
                name="unique_paging_group_member",
            ),
        ]

    def clean(self):
        super().clean()
        if (
            self.paging_group_id
            and self.extension_id
            and self.paging_group.location_id != self.extension.location_id
        ):
            raise ValidationError(
                {"extension": "Paging group members must belong to the paging group location."}
            )

    def __str__(self):
        return f"{self.paging_group}: {self.extension}"


class DID(TimestampedModel):
    location = models.ForeignKey(Location, on_delete=models.CASCADE, related_name="dids")
    number = models.CharField(
        max_length=16,
        unique=True,
        validators=[did_number_validator],
    )
    provider = models.ForeignKey(
        Provider,
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="dids",
    )
    trunk = models.ForeignKey(
        "Trunk",
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="dids",
    )
    direct_extension = models.ForeignKey(
        Extension,
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="direct_dids",
    )
    default_destination = models.ForeignKey(
        InboundDestination,
        on_delete=models.PROTECT,
        related_name="dids",
    )
    label = models.CharField(max_length=120, blank=True)
    is_active = models.BooleanField(default=True)

    class Meta:
        ordering = ["number"]
        constraints = [
            models.CheckConstraint(
                condition=models.Q(number__regex=r"^\+?[1-9]\d{6,14}$"),
                name="did_number_e164ish",
            ),
        ]

    def clean(self):
        super().clean()
        errors = {}
        if (
            self.direct_extension_id
            and self.location_id
            and self.direct_extension.location_id != self.location_id
        ):
            errors["direct_extension"] = "Direct extension must belong to the DID location."
        if (
            self.default_destination_id
            and self.location_id
            and self.default_destination.location_id != self.location_id
        ):
            errors["default_destination"] = "Default destination must belong to the DID location."
        if self.trunk_id and self.location_id and self.trunk.location_id != self.location_id:
            errors["trunk"] = "Trunk must belong to the DID location."
        if errors:
            raise ValidationError(errors)

    def __str__(self):
        return self.number


class Trunk(TimestampedModel):
    class TrunkType(models.TextChoices):
        SIP = "sip", "SIP"
        IAX2 = "iax2", "IAX2"

    location = models.ForeignKey(Location, on_delete=models.CASCADE, related_name="trunks")
    provider = models.ForeignKey(
        Provider,
        on_delete=models.PROTECT,
        related_name="trunks",
    )
    name = models.CharField(max_length=120)
    trunk_type = models.CharField(
        max_length=16,
        choices=TrunkType.choices,
        default=TrunkType.SIP,
    )
    host = models.CharField(max_length=255, blank=True)
    username = models.CharField(max_length=120, blank=True)
    is_emergency_capable = models.BooleanField(default=False)
    is_active = models.BooleanField(default=True)

    class Meta:
        ordering = ["location", "name"]
        constraints = [
            models.UniqueConstraint(fields=["location", "name"], name="unique_trunk_name_per_location"),
        ]

    def __str__(self):
        return f"{self.location}: {self.name}"


class OutboundRoute(TimestampedModel):
    location = models.ForeignKey(
        Location,
        on_delete=models.CASCADE,
        related_name="outbound_routes",
    )
    name = models.CharField(max_length=120)
    dial_pattern = models.CharField(max_length=80)
    priority = models.PositiveSmallIntegerField(validators=[MinValueValidator(1)])
    is_emergency_route = models.BooleanField(default=False)
    caller_id_number = models.CharField(max_length=32, blank=True)
    is_active = models.BooleanField(default=True)

    class Meta:
        ordering = ["location", "priority", "name"]
        constraints = [
            models.UniqueConstraint(
                fields=["location", "name"],
                name="unique_outbound_route_name_per_location",
            ),
            models.UniqueConstraint(
                fields=["location", "priority"],
                name="unique_outbound_route_priority_per_location",
            ),
        ]

    def __str__(self):
        return f"{self.location}: {self.name}"


class OutboundRouteTrunk(TimestampedModel):
    outbound_route = models.ForeignKey(
        OutboundRoute,
        on_delete=models.CASCADE,
        related_name="route_trunks",
    )
    trunk = models.ForeignKey(
        Trunk,
        on_delete=models.PROTECT,
        related_name="outbound_route_links",
    )
    priority = models.PositiveSmallIntegerField(validators=[MinValueValidator(1)])

    class Meta:
        ordering = ["outbound_route", "priority"]
        constraints = [
            models.UniqueConstraint(
                fields=["outbound_route", "trunk"],
                name="unique_outbound_route_trunk",
            ),
            models.UniqueConstraint(
                fields=["outbound_route", "priority"],
                name="unique_outbound_route_trunk_priority",
            ),
        ]

    def clean(self):
        super().clean()
        if (
            self.outbound_route_id
            and self.trunk_id
            and self.outbound_route.location_id != self.trunk.location_id
        ):
            raise ValidationError(
                {"trunk": "Outbound route trunks must belong to the route location."}
            )

    def __str__(self):
        return f"{self.outbound_route} via {self.trunk}"
