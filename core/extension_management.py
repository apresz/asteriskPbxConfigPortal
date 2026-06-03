from django.core.exceptions import ValidationError

from .models import (
    CallQueue,
    DID,
    Extension,
    PagingGroup,
    PagingGroupMember,
    QueueMember,
    RingGroup,
    RingGroupMember,
)


def is_911_disable_change(extension: Extension | None, emergency_calling_enabled: bool) -> bool:
    original_enabled = True
    if extension and extension.pk:
        original_enabled = extension.emergency_calling_enabled
    return original_enabled and not emergency_calling_enabled


def sync_extension_relationships(
    extension: Extension,
    *,
    direct_dids,
    ring_groups,
    queues,
    paging_groups,
) -> None:
    _validate_same_location(extension, direct_dids, "DIDs")
    _validate_same_location(extension, ring_groups, "Ring groups")
    _validate_same_location(extension, queues, "Queues")
    _validate_same_location(extension, paging_groups, "Paging groups")

    direct_did_ids = [did.id for did in direct_dids]
    DID.objects.filter(direct_extension=extension).exclude(id__in=direct_did_ids).update(direct_extension=None)
    DID.objects.filter(id__in=direct_did_ids).update(direct_extension=extension)

    _sync_members(
        extension=extension,
        selected_records=ring_groups,
        model=RingGroupMember,
        parent_field="ring_group",
        defaults={"priority": 1},
    )
    _sync_members(
        extension=extension,
        selected_records=queues,
        model=QueueMember,
        parent_field="queue",
        defaults={"penalty": 0},
    )
    _sync_members(
        extension=extension,
        selected_records=paging_groups,
        model=PagingGroupMember,
        parent_field="paging_group",
        defaults={},
    )


def clear_extension_relationships(extension: Extension) -> None:
    DID.objects.filter(direct_extension=extension).update(direct_extension=None)
    RingGroupMember.objects.filter(extension=extension).delete()
    QueueMember.objects.filter(extension=extension).delete()
    PagingGroupMember.objects.filter(extension=extension).delete()


def membership_names(extension: Extension) -> dict[str, str]:
    return {
        "direct_dids": _join_values(extension.direct_dids.order_by("number").values_list("number", flat=True)),
        "ring_groups": _join_values(
            RingGroup.objects.filter(members__extension=extension).order_by("name").values_list("name", flat=True)
        ),
        "queues": _join_values(
            CallQueue.objects.filter(members__extension=extension).order_by("name").values_list("name", flat=True)
        ),
        "paging_groups": _join_values(
            PagingGroup.objects.filter(members__extension=extension)
            .order_by("page_code")
            .values_list("name", flat=True)
        ),
    }


def _sync_members(*, extension, selected_records, model, parent_field, defaults):
    selected_ids = [record.id for record in selected_records]
    model.objects.filter(extension=extension).exclude(**{f"{parent_field}_id__in": selected_ids}).delete()
    for record in selected_records:
        model.objects.get_or_create(
            extension=extension,
            **{parent_field: record},
            defaults=defaults,
        )


def _validate_same_location(extension, records, label):
    wrong_location = [str(record) for record in records if record.location_id != extension.location_id]
    if wrong_location:
        raise ValidationError(f"{label} must belong to {extension.location}.")


def _join_values(values) -> str:
    return ";".join(str(value) for value in values)
