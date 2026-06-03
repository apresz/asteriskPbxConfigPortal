from __future__ import annotations

import csv
from dataclasses import dataclass
from io import StringIO

from django.core.exceptions import ValidationError
from django.db import transaction

from .extension_management import (
    record_911_disable_success,
    sync_extension_assignments,
    validate_911_disable_allowed,
    validate_local_assignments,
)
from .models import DID, CallQueue, Extension, Location, PagingGroup, RingGroup


EXTENSION_CSV_FIELDS = [
    "location_slug",
    "number",
    "display_name",
    "voicemail_email",
    "sip_username",
    "sip_password",
    "direct_dids",
    "voicemail_enabled",
    "voicemail_pin",
    "caller_id_name",
    "caller_id_number",
    "recording_policy",
    "emergency_calling_enabled",
    "is_active",
    "ring_groups",
    "queues",
    "paging_groups",
]


@dataclass(frozen=True)
class CsvImportResult:
    imported_count: int
    errors: list[str]


@dataclass(frozen=True)
class PreparedExtensionRow:
    extension: Extension
    before_911_enabled: bool
    dids: list[DID]
    ring_groups: list[RingGroup]
    queues: list[CallQueue]
    paging_groups: list[PagingGroup]


def extension_csv_template() -> str:
    return _write_rows([])


def export_extensions_csv(queryset=None) -> str:
    extensions = queryset if queryset is not None else Extension.objects.all()
    rows = []
    for extension in extensions.select_related("location").prefetch_related(
        "direct_dids",
        "ring_group_memberships__ring_group",
        "queue_memberships__queue",
        "paging_group_memberships__paging_group",
    ):
        rows.append(
            {
                "location_slug": extension.location.slug,
                "number": extension.number,
                "display_name": extension.display_name,
                "voicemail_email": extension.email,
                "sip_username": extension.sip_username,
                "sip_password": extension.sip_password,
                "direct_dids": _join_values(did.number for did in extension.direct_dids.all()),
                "voicemail_enabled": _format_bool(extension.voicemail_enabled),
                "voicemail_pin": extension.voicemail_pin,
                "caller_id_name": extension.caller_id_name,
                "caller_id_number": extension.caller_id_number,
                "recording_policy": extension.recording_policy,
                "emergency_calling_enabled": _format_bool(extension.emergency_calling_enabled),
                "is_active": _format_bool(extension.is_active),
                "ring_groups": _join_values(
                    member.ring_group.name for member in extension.ring_group_memberships.all()
                ),
                "queues": _join_values(
                    member.queue.name for member in extension.queue_memberships.all()
                ),
                "paging_groups": _join_values(
                    member.paging_group.name
                    for member in extension.paging_group_memberships.all()
                ),
            }
        )
    return _write_rows(rows)


def import_extensions_csv(file_obj, *, actor) -> CsvImportResult:
    text = _read_text(file_obj)
    reader = csv.DictReader(StringIO(text))
    if reader.fieldnames is None:
        return CsvImportResult(imported_count=0, errors=["CSV file is empty."])

    missing_fields = [field for field in EXTENSION_CSV_FIELDS if field not in reader.fieldnames]
    if missing_fields:
        return CsvImportResult(
            imported_count=0,
            errors=[f"CSV file is missing required columns: {', '.join(missing_fields)}."],
        )

    prepared_rows: list[PreparedExtensionRow] = []
    errors: list[str] = []
    seen_numbers: set[str] = set()
    for row_number, row in enumerate(reader, start=2):
        prepared = _prepare_row(row=row, row_number=row_number, actor=actor, seen_numbers=seen_numbers)
        if isinstance(prepared, PreparedExtensionRow):
            prepared_rows.append(prepared)
        else:
            errors.extend(prepared)

    if errors:
        return CsvImportResult(imported_count=0, errors=errors)

    with transaction.atomic():
        for prepared in prepared_rows:
            prepared.extension.save()
            sync_extension_assignments(
                prepared.extension,
                dids=prepared.dids,
                ring_groups=prepared.ring_groups,
                queues=prepared.queues,
                paging_groups=prepared.paging_groups,
            )
            record_911_disable_success(
                actor=actor,
                target_number=prepared.extension.number,
                before_enabled=prepared.before_911_enabled,
                after_enabled=prepared.extension.emergency_calling_enabled,
            )
    return CsvImportResult(imported_count=len(prepared_rows), errors=[])


def _prepare_row(
    *,
    row: dict,
    row_number: int,
    actor,
    seen_numbers: set[str],
) -> PreparedExtensionRow | list[str]:
    errors: list[str] = []
    number = _clean(row, "number")
    location_slug = _clean(row, "location_slug")
    if not number:
        errors.append(f"Row {row_number}: number is required.")
    elif number in seen_numbers:
        errors.append(f"Row {row_number}: duplicate extension number {number} in import file.")
    else:
        seen_numbers.add(number)

    location = None
    if not location_slug:
        errors.append(f"Row {row_number}: location_slug is required.")
    else:
        try:
            location = Location.objects.get(slug=location_slug)
        except Location.DoesNotExist:
            errors.append(f"Row {row_number}: location_slug {location_slug} was not found.")

    extension = Extension.objects.filter(number=number).first() if number else None
    before_911_enabled = bool(extension.emergency_calling_enabled) if extension else True
    after_911_enabled = _parse_bool(
        _clean(row, "emergency_calling_enabled"),
        default=True,
        field="emergency_calling_enabled",
        row_number=row_number,
        errors=errors,
    )
    try:
        validate_911_disable_allowed(
            actor=actor,
            target_number=number,
            before_enabled=before_911_enabled,
            after_enabled=after_911_enabled,
        )
    except ValidationError as exc:
        errors.append(f"Row {row_number}: {exc.messages[0]}")

    if errors:
        return errors

    extension = extension or Extension(number=number)
    extension.location = location
    extension.display_name = _clean(row, "display_name")
    extension.email = _clean(row, "voicemail_email")
    extension.sip_username = _clean(row, "sip_username")
    extension.sip_password = _clean(row, "sip_password")
    extension.voicemail_enabled = _parse_bool(
        _clean(row, "voicemail_enabled"),
        default=True,
        field="voicemail_enabled",
        row_number=row_number,
        errors=errors,
    )
    extension.voicemail_pin = _clean(row, "voicemail_pin")
    extension.caller_id_name = _clean(row, "caller_id_name")
    extension.caller_id_number = _clean(row, "caller_id_number")
    extension.recording_policy = _clean(row, "recording_policy") or Extension.RecordingPolicy.INHERIT
    extension.emergency_calling_enabled = after_911_enabled
    extension.is_active = _parse_bool(
        _clean(row, "is_active"),
        default=True,
        field="is_active",
        row_number=row_number,
        errors=errors,
    )

    dids = _resolve_dids(
        values=_split_values(_clean(row, "direct_dids")),
        location=location,
        extension=extension,
        row_number=row_number,
        errors=errors,
    )
    ring_groups = _resolve_named_groups(
        model=RingGroup,
        values=_split_values(_clean(row, "ring_groups")),
        location=location,
        label="ring group",
        row_number=row_number,
        errors=errors,
    )
    queues = _resolve_named_groups(
        model=CallQueue,
        values=_split_values(_clean(row, "queues")),
        location=location,
        label="queue",
        row_number=row_number,
        errors=errors,
    )
    paging_groups = _resolve_named_groups(
        model=PagingGroup,
        values=_split_values(_clean(row, "paging_groups")),
        location=location,
        label="paging group",
        row_number=row_number,
        errors=errors,
    )
    for field, message in validate_local_assignments(
        location_id=location.pk,
        dids=dids,
        ring_groups=ring_groups,
        queues=queues,
        paging_groups=paging_groups,
        extension=extension if extension.pk else None,
    ).items():
        errors.append(f"Row {row_number}: {field}: {message}")

    try:
        extension.full_clean()
    except ValidationError as exc:
        for field, messages in exc.message_dict.items():
            errors.append(f"Row {row_number}: {field}: {'; '.join(messages)}")

    if errors:
        return errors
    return PreparedExtensionRow(
        extension=extension,
        before_911_enabled=before_911_enabled,
        dids=dids,
        ring_groups=ring_groups,
        queues=queues,
        paging_groups=paging_groups,
    )


def _resolve_dids(
    *,
    values: list[str],
    location: Location,
    extension: Extension,
    row_number: int,
    errors: list[str],
) -> list[DID]:
    dids = []
    for number in values:
        try:
            did = DID.objects.get(number=number)
        except DID.DoesNotExist:
            errors.append(f"Row {row_number}: DID {number} was not found.")
            continue
        if did.location_id != location.pk:
            errors.append(f"Row {row_number}: DID {number} belongs to another location.")
        if did.direct_extension_id and did.direct_extension_id != extension.pk:
            errors.append(f"Row {row_number}: DID {number} is already assigned.")
        dids.append(did)
    return dids


def _resolve_named_groups(
    *,
    model,
    values: list[str],
    location: Location,
    label: str,
    row_number: int,
    errors: list[str],
):
    groups = []
    for name in values:
        try:
            groups.append(model.objects.get(location=location, name=name))
        except model.DoesNotExist:
            errors.append(f"Row {row_number}: {label} {name} was not found.")
    return groups


def _read_text(file_obj) -> str:
    content = file_obj.read()
    if isinstance(content, bytes):
        return content.decode("utf-8-sig")
    return content


def _write_rows(rows: list[dict]) -> str:
    output = StringIO()
    writer = csv.DictWriter(output, fieldnames=EXTENSION_CSV_FIELDS)
    writer.writeheader()
    for row in rows:
        writer.writerow(row)
    return output.getvalue()


def _clean(row: dict, field: str) -> str:
    return (row.get(field) or "").strip()


def _split_values(value: str) -> list[str]:
    return [item.strip() for item in value.split(";") if item.strip()]


def _join_values(values) -> str:
    return ";".join(str(value) for value in values if value)


def _format_bool(value: bool) -> str:
    return "true" if value else "false"


def _parse_bool(
    value: str,
    *,
    default: bool,
    field: str,
    row_number: int,
    errors: list[str],
) -> bool:
    if value == "":
        return default
    normalized = value.lower()
    if normalized in {"1", "true", "yes", "y", "on"}:
        return True
    if normalized in {"0", "false", "no", "n", "off"}:
        return False
    errors.append(f"Row {row_number}: {field} must be true or false.")
    return default
