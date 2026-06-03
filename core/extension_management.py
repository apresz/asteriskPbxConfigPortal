from __future__ import annotations

from collections.abc import Iterable

from django.core.exceptions import ValidationError

from .access import user_has_permission
from .audit import record_audit
from .models import (
    AuditAction,
    AuditOutcome,
    DID,
    CallQueue,
    Extension,
    PagingGroup,
    PagingGroupMember,
    PortalPermission,
    QueueMember,
    RingGroup,
    RingGroupMember,
)


def force_disables_911(before_enabled: bool, after_enabled: bool) -> bool:
    return bool(before_enabled) and not bool(after_enabled)


def validate_911_disable_allowed(
    *,
    actor,
    target_number: str,
    before_enabled: bool,
    after_enabled: bool,
    record_denied: bool = True,
) -> None:
    if not force_disables_911(before_enabled, after_enabled):
        return

    if user_has_permission(actor, PortalPermission.ADMINISTER):
        return

    if record_denied:
        record_911_disable_audit(
            actor=actor,
            target_number=target_number,
            outcome=AuditOutcome.DENIED,
            before_enabled=before_enabled,
            after_enabled=after_enabled,
            reason="admin_required",
        )
    raise ValidationError("Only Admin users can disable 911 calling for an extension.")


def record_911_disable_success(
    *,
    actor,
    target_number: str,
    before_enabled: bool,
    after_enabled: bool,
) -> None:
    if not force_disables_911(before_enabled, after_enabled):
        return
    record_911_disable_audit(
        actor=actor,
        target_number=target_number,
        outcome=AuditOutcome.SUCCESS,
        before_enabled=before_enabled,
        after_enabled=after_enabled,
    )


def record_911_disable_audit(
    *,
    actor,
    target_number: str,
    outcome: AuditOutcome,
    before_enabled: bool,
    after_enabled: bool,
    reason: str = "",
):
    details = {
        "field": "emergency_calling_enabled",
        "before": before_enabled,
        "after": after_enabled,
    }
    if reason:
        details["reason"] = reason
    return record_audit(
        actor=actor,
        action=AuditAction.CONFIG_CHANGE,
        target=f"extensions/{target_number or 'new'}",
        outcome=outcome,
        details=details,
    )


def sync_extension_assignments(
    extension: Extension,
    *,
    dids: Iterable[DID] = (),
    ring_groups: Iterable[RingGroup] = (),
    queues: Iterable[CallQueue] = (),
    paging_groups: Iterable[PagingGroup] = (),
) -> None:
    did_ids = _ids(dids)
    DID.objects.filter(direct_extension=extension).exclude(id__in=did_ids).update(
        direct_extension=None
    )
    DID.objects.filter(id__in=did_ids).update(direct_extension=extension)

    ring_group_ids = _ids(ring_groups)
    RingGroupMember.objects.filter(extension=extension).exclude(
        ring_group_id__in=ring_group_ids
    ).delete()
    for ring_group_id in ring_group_ids:
        RingGroupMember.objects.get_or_create(
            ring_group_id=ring_group_id,
            extension=extension,
        )

    queue_ids = _ids(queues)
    QueueMember.objects.filter(extension=extension).exclude(queue_id__in=queue_ids).delete()
    for queue_id in queue_ids:
        QueueMember.objects.get_or_create(queue_id=queue_id, extension=extension)

    paging_group_ids = _ids(paging_groups)
    PagingGroupMember.objects.filter(extension=extension).exclude(
        paging_group_id__in=paging_group_ids
    ).delete()
    for paging_group_id in paging_group_ids:
        PagingGroupMember.objects.get_or_create(
            paging_group_id=paging_group_id,
            extension=extension,
        )


def validate_local_assignments(
    *,
    location_id: int | None,
    dids: Iterable[DID],
    ring_groups: Iterable[RingGroup],
    queues: Iterable[CallQueue],
    paging_groups: Iterable[PagingGroup],
    extension: Extension | None = None,
) -> dict[str, str]:
    errors: dict[str, str] = {}
    if not location_id:
        return errors

    for did in dids:
        if did.location_id != location_id:
            errors["dids"] = "Assigned DIDs must belong to the extension location."
        if did.direct_extension_id and (
            extension is None or did.direct_extension_id != extension.pk
        ):
            errors["dids"] = "Assigned DIDs are already linked to another extension."

    if any(group.location_id != location_id for group in ring_groups):
        errors["ring_groups"] = "Ring groups must belong to the extension location."
    if any(queue.location_id != location_id for queue in queues):
        errors["queues"] = "Queues must belong to the extension location."
    if any(group.location_id != location_id for group in paging_groups):
        errors["paging_groups"] = "Paging groups must belong to the extension location."
    return errors


def _ids(objects: Iterable) -> list[int]:
    return [item.pk for item in objects if item.pk is not None]
