from django.contrib import admin

from .models import (
    AuditLog,
    APIKey,
    AudioPrompt,
    DID,
    IVR,
    IVRMenuOption,
    CallQueue,
    Extension,
    FeatureCode,
    InboundDestination,
    Location,
    OutboundRoute,
    OutboundRouteTrunk,
    PagingGroup,
    PagingGroupMember,
    Phone,
    PhoneLineAppearance,
    PhoneSpeedDial,
    PortalUserProfile,
    Provider,
    QueueMember,
    RingGroup,
    RingGroupMember,
    ServiceIdentity,
    Trunk,
)


@admin.register(PortalUserProfile)
class PortalUserProfileAdmin(admin.ModelAdmin):
    list_display = ("user", "role", "updated_at")
    list_filter = ("role",)
    search_fields = ("user__username", "user__email", "user__first_name", "user__last_name")


@admin.register(AuditLog)
class AuditLogAdmin(admin.ModelAdmin):
    list_display = ("timestamp", "actor", "action", "target", "outcome")
    list_filter = ("action", "outcome")
    readonly_fields = ("actor", "action", "target", "timestamp", "outcome", "details")
    search_fields = ("actor__username", "target")


@admin.register(ServiceIdentity)
class ServiceIdentityAdmin(admin.ModelAdmin):
    list_display = ("name", "slug", "is_active", "created_by", "updated_at")
    list_filter = ("is_active",)
    search_fields = ("name", "slug", "description")


@admin.register(APIKey)
class APIKeyAdmin(admin.ModelAdmin):
    list_display = ("name", "scope_label", "prefix", "is_active", "created_by", "created_at", "revoked_at")
    list_filter = ("revoked_at", "service_identity")
    readonly_fields = (
        "prefix",
        "key_hash",
        "created_by",
        "last_rotated_at",
        "last_rotated_by",
        "revoked_at",
        "revoked_by",
        "last_used_at",
        "created_at",
        "updated_at",
    )
    search_fields = ("name", "prefix", "user__username", "service_identity__name", "service_identity__slug")

    @admin.display(boolean=True)
    def is_active(self, obj):
        return obj.is_active


@admin.register(Location)
class LocationAdmin(admin.ModelAdmin):
    list_display = ("name", "slug", "timezone", "is_active", "deployment_status", "last_deployed_at")
    list_filter = ("is_active", "deployment_status", "timezone")
    search_fields = ("name", "slug")
    fieldsets = (
        ("Identity", {"fields": ("name", "slug", "description", "timezone", "is_active")}),
        ("Network", {"fields": ("lan_subnet", "pbx_lan_ip", "pbx_warp_ip")}),
        (
            "Deployment SSH",
            {
                "fields": (
                    "deployment_ssh_host",
                    "deployment_ssh_port",
                    "deployment_ssh_username",
                    "deployment_ssh_private_key",
                    "deployment_ssh_known_hosts",
                )
            },
        ),
        (
            "SIP / RTP / IAX",
            {"fields": ("sip_bind_ip", "sip_port", "rtp_port_start", "rtp_port_end", "iax_bind_ip", "iax_port")},
        ),
        ("Emergency", {"fields": ("default_did", "emergency_caller_id", "emergency_trunk")}),
        ("Inbound Routing", {"fields": ("default_inbound_destination",)}),
        ("Recording", {"fields": ("recording_retention_days",)}),
        (
            "SMTP",
            {"fields": ("smtp_host", "smtp_port", "smtp_from_email", "smtp_use_tls", "smtp_use_ssl", "smtp_username", "smtp_password")},
        ),
        ("AMI", {"fields": ("ami_host", "ami_port", "ami_username", "ami_secret")}),
        ("Agent", {"fields": ("agent_secret",)}),
        ("Deployment Status", {"fields": ("deployment_status", "last_deployed_at")}),
    )


@admin.register(Extension)
class ExtensionAdmin(admin.ModelAdmin):
    list_display = (
        "number",
        "display_name",
        "location",
        "voicemail_enabled",
        "recording_policy",
        "emergency_calling_enabled",
        "is_active",
    )
    list_filter = ("location", "recording_policy", "voicemail_enabled", "emergency_calling_enabled", "is_active")
    search_fields = ("number", "display_name", "sip_username", "caller_id_number")


@admin.register(CallQueue)
class CallQueueAdmin(admin.ModelAdmin):
    list_display = ("name", "location", "strategy", "recording_policy", "overflow_destination", "is_active")
    list_filter = ("location", "strategy", "recording_policy", "is_active")
    search_fields = ("name",)


@admin.register(Phone)
class PhoneAdmin(admin.ModelAdmin):
    list_display = ("mac_address", "model", "location", "label", "is_active")
    list_filter = ("model", "location", "is_active")
    search_fields = ("mac_address", "label")


@admin.register(DID)
class DIDAdmin(admin.ModelAdmin):
    list_display = ("number", "location", "provider", "direct_extension", "default_destination")
    list_filter = ("location", "provider", "is_active")
    search_fields = ("number", "label")


@admin.register(Trunk)
class TrunkAdmin(admin.ModelAdmin):
    list_display = ("name", "location", "provider", "trunk_type", "is_emergency_capable", "is_active")
    list_filter = ("location", "provider", "trunk_type", "is_emergency_capable", "is_active")
    search_fields = ("name", "host", "username", "password")


admin.site.register(Provider)
@admin.register(OutboundRoute)
class OutboundRouteAdmin(admin.ModelAdmin):
    list_display = (
        "name",
        "location",
        "dial_pattern",
        "priority",
        "caller_id_source",
        "recording_policy",
        "is_emergency_route",
        "is_active",
    )
    list_filter = ("location", "caller_id_source", "recording_policy", "is_emergency_route", "is_active")
    search_fields = ("name", "dial_pattern", "caller_id_number")


admin.site.register(OutboundRouteTrunk)
admin.site.register(InboundDestination)
admin.site.register(AudioPrompt)
admin.site.register(IVR)
admin.site.register(IVRMenuOption)
admin.site.register(FeatureCode)
admin.site.register(RingGroup)
admin.site.register(RingGroupMember)
admin.site.register(QueueMember)
admin.site.register(PagingGroup)
admin.site.register(PagingGroupMember)
admin.site.register(PhoneLineAppearance)
admin.site.register(PhoneSpeedDial)
