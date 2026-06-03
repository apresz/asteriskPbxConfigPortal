from django.contrib import admin

from .models import (
    AuditLog,
    APIKey,
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
    list_display = ("name", "slug", "timezone", "is_active")
    search_fields = ("name", "slug")


@admin.register(Extension)
class ExtensionAdmin(admin.ModelAdmin):
    list_display = ("number", "display_name", "location", "is_active")
    list_filter = ("location", "is_active")
    search_fields = ("number", "display_name")


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
    search_fields = ("name", "host", "username")


admin.site.register(Provider)
admin.site.register(OutboundRoute)
admin.site.register(OutboundRouteTrunk)
admin.site.register(InboundDestination)
admin.site.register(IVR)
admin.site.register(IVRMenuOption)
admin.site.register(RingGroup)
admin.site.register(RingGroupMember)
admin.site.register(CallQueue)
admin.site.register(QueueMember)
admin.site.register(PagingGroup)
admin.site.register(PagingGroupMember)
admin.site.register(PhoneLineAppearance)
