from django.conf import settings
from django.db import models


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


class PortalUserProfile(models.Model):
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
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

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
