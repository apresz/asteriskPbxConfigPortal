from django.contrib import admin

from .models import AuditLog, PortalUserProfile


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
