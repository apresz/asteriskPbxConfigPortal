from .models import AuditAction, AuditLog, AuditOutcome


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
