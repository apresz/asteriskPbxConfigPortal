from django.contrib import admin

from .models import (
    AuditLog,
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
