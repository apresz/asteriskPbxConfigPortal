from .models import AuditAction, AuditLog, AuditOutcome
from .audit_helpers import (
    audit_model_label,
    audit_model_summary,
    audit_object_identity,
    audit_target,
    build_config_change_details,
)


def record_audit(
    *,
    actor,
    action: AuditAction | str,
    target: str,
    outcome: AuditOutcome | str,
    details: dict | None = None,
) -> AuditLog:
    audit_actor = actor if getattr(actor, "is_authenticated", False) else None
    return AuditLog.objects.create(
        actor=audit_actor,
        action=AuditAction(action),
        target=target,
        outcome=AuditOutcome(outcome),
        details=details or {},
    )


def record_config_change(
    *,
    actor,
    operation: str,
    instance=None,
    model: str | None = None,
    object_identity: str | None = None,
    target: str | None = None,
    outcome: AuditOutcome | str = AuditOutcome.SUCCESS,
    before: dict | None = None,
    after: dict | None = None,
    source: str = "portal",
    extra_details: dict | None = None,
) -> AuditLog:
    if instance is not None:
        model = model or audit_model_label(instance)
        object_identity = object_identity or audit_object_identity(instance)
        if after is None and operation != "delete":
            after = audit_model_summary(instance, redact=False)

    model = model or "unknown"
    object_identity = object_identity or "unknown"
    audit_outcome = AuditOutcome(outcome)
    audit_details = build_config_change_details(
        actor=actor,
        operation=operation,
        model=model,
        object_identity=object_identity,
        outcome=audit_outcome.value,
        before=before,
        after=after,
        source=source,
        extra_details=extra_details,
    )
    return record_audit(
        actor=actor,
        action=AuditAction.CONFIG_CHANGE,
        target=target or audit_target(model, object_identity),
        outcome=audit_outcome,
        details=audit_details,
    )
