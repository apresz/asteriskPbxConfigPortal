import csv
from dataclasses import dataclass
from io import StringIO

from django.core.exceptions import ValidationError
from django.db import transaction

from .audit import record_audit
from .extension_management import is_911_disable_change, membership_names, sync_extension_relationships
from .models import (
    AuditAction,
    AuditOutcome,
    CallQueue,
    DID,
    Extension,
    Location,
    PagingGroup,
    RingGroup,
)


EXTENSION_CSV_HEADERS = [
    "location_slug",
    "number",
    "display_name",
    "email",
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

TRUE_VALUES = {"1", "true", "t", "yes", "y", "enabled", "active"}
FALSE_VALUES = {"0", "false", "f", "no", "n", "disabled", "inactive"}


class ExtensionCSVError(Exception):
    def __init__(self, errors):
        self.errors = errors
        super().__init__("Extension CSV import failed.")


@dataclass
class PreparedExtensionRow:
    row_number: int
    extension: Extension
    field_values: dict
    direct_dids: list[DID]
    ring_groups: list[RingGroup]
    queues: list[CallQueue]
    paging_groups: list[PagingGroup]
    logs_911_disable: bool


def extension_template_csv() -> str:
    output = StringIO()
    writer = csv.DictWriter(output, fieldnames=EXTENSION_CSV_HEADERS)
    writer.writeheader()
    return output.getvalue()


def export_extensions_csv(extensions=None) -> str:
    if extensions is None:
        extensions = Extension.objects.all()
    extensions = extensions.select_related("location").prefetch_related(
        "direct_dids",
        "ring_group_memberships__ring_group",
        "queue_memberships__queue",
        "paging_group_memberships__paging_group",
    )
    output = StringIO()
    writer = csv.DictWriter(output, fieldnames=EXTENSION_CSV_HEADERS)
    writer.writeheader()
    for extension in extensions:
        memberships = membership_names(extension)
        writer.writerow(
            {
                "location_slug": extension.location.slug,
                "number": extension.number,
                "display_name": extension.display_name,
                "email": extension.email,
                "sip_username": extension.sip_username,
                "sip_password": extension.sip_password,
                "direct_dids": memberships["direct_dids"],
                "voicemail_enabled": _format_bool(extension.voicemail_enabled),
                "voicemail_pin": extension.voicemail_pin,
                "caller_id_name": extension.caller_id_name,
                "caller_id_number": extension.caller_id_number,
                "recording_policy": extension.recording_policy,
                "emergency_calling_enabled": _format_bool(extension.emergency_calling_enabled),
                "is_active": _format_bool(extension.is_active),
                "ring_groups": memberships["ring_groups"],
                "queues": memberships["queues"],
                "paging_groups": memberships["paging_groups"],
            }
        )
    return output.getvalue()


def import_extensions_csv(content, *, actor=None, can_disable_911=False) -> int:
    csv_text = _read_csv_content(content)
    reader = csv.DictReader(StringIO(csv_text))
    errors = _validate_headers(reader.fieldnames)
    seen_numbers = set()
    prepared_rows = []

    for row_number, row in enumerate(reader, start=2):
        row_errors = []
        number = (row.get("number") or "").strip()
        if not number:
            row_errors.append("number is required")
        elif number in seen_numbers:
            row_errors.append(f"duplicate extension number {number} in CSV")
        else:
            seen_numbers.add(number)

        location = _location_for_row(row, row_errors)
        extension = Extension.objects.filter(number=number).first() if number else None
        if extension is None:
            extension = Extension(number=number)

        field_values = _field_values_for_row(row, extension, location, row_errors)
        emergency_enabled = field_values.get("emergency_calling_enabled", True)
        logs_911_disable = is_911_disable_change(extension, emergency_enabled)
        if logs_911_disable and not can_disable_911:
            row_errors.append("Only admins can disable 911 calling for an extension")
            _record_911_audit(actor, number, AuditOutcome.DENIED, row_number)

        direct_dids = _lookup_dids(row.get("direct_dids"), location, extension, row_errors)
        ring_groups = _lookup_named_records(RingGroup, row.get("ring_groups"), location, row_errors, "ring group")
        queues = _lookup_named_records(CallQueue, row.get("queues"), location, row_errors, "queue")
        paging_groups = _lookup_paging_groups(row.get("paging_groups"), location, row_errors)

        if row_errors:
            errors.append(f"Row {row_number}: {'; '.join(row_errors)}")
            continue

        prepared_rows.append(
            PreparedExtensionRow(
                row_number=row_number,
                extension=extension,
                field_values=field_values,
                direct_dids=direct_dids,
                ring_groups=ring_groups,
                queues=queues,
                paging_groups=paging_groups,
                logs_911_disable=logs_911_disable,
            )
        )

    if errors:
        raise ExtensionCSVError(errors)

    with transaction.atomic():
        for prepared in prepared_rows:
            for field_name, value in prepared.field_values.items():
                setattr(prepared.extension, field_name, value)
            prepared.extension.full_clean()
            prepared.extension.save()
            sync_extension_relationships(
                prepared.extension,
                direct_dids=prepared.direct_dids,
                ring_groups=prepared.ring_groups,
                queues=prepared.queues,
                paging_groups=prepared.paging_groups,
            )
            if prepared.logs_911_disable:
                _record_911_audit(actor, prepared.extension.number, AuditOutcome.SUCCESS, prepared.row_number)

    return len(prepared_rows)


def _read_csv_content(content) -> str:
    if hasattr(content, "read"):
        content = content.read()
    if isinstance(content, bytes):
        return content.decode("utf-8-sig")
    return str(content)


def _validate_headers(fieldnames):
    if not fieldnames:
        return ["CSV must include a header row"]
    missing_headers = [header for header in EXTENSION_CSV_HEADERS if header not in fieldnames]
    if missing_headers:
        return [f"CSV is missing required headers: {', '.join(missing_headers)}"]
    return []


def _location_for_row(row, row_errors):
    location_slug = (row.get("location_slug") or "").strip()
    if not location_slug:
        row_errors.append("location_slug is required")
        return None
    try:
        return Location.objects.get(slug=location_slug)
    except Location.DoesNotExist:
        row_errors.append(f"location_slug {location_slug} was not found")
        return None


def _field_values_for_row(row, extension, location, row_errors):
    values = {
        "location": location,
        "number": (row.get("number") or "").strip(),
        "display_name": (row.get("display_name") or "").strip(),
        "email": (row.get("email") or "").strip(),
        "sip_username": (row.get("sip_username") or "").strip(),
        "sip_password": _text_or_existing(row.get("sip_password"), extension.sip_password),
        "voicemail_pin": _text_or_existing(row.get("voicemail_pin"), extension.voicemail_pin),
        "caller_id_name": (row.get("caller_id_name") or "").strip(),
        "caller_id_number": (row.get("caller_id_number") or "").strip(),
        "recording_policy": (row.get("recording_policy") or extension.recording_policy or Extension.RecordingPolicy.NEVER).strip(),
        "voicemail_enabled": _parse_bool(row.get("voicemail_enabled"), extension.voicemail_enabled if extension.pk else True, row_errors, "voicemail_enabled"),
        "emergency_calling_enabled": _parse_bool(
            row.get("emergency_calling_enabled"),
            extension.emergency_calling_enabled if extension.pk else True,
            row_errors,
            "emergency_calling_enabled",
        ),
        "is_active": _parse_bool(row.get("is_active"), extension.is_active if extension.pk else True, row_errors, "is_active"),
    }
    if not values["display_name"]:
        row_errors.append("display_name is required")
    if not values["sip_username"]:
        values["sip_username"] = values["number"]
    if values["recording_policy"] not in Extension.RecordingPolicy.values:
        row_errors.append(f"recording_policy {values['recording_policy']} is invalid")
    return values


def _lookup_dids(value, location, extension, row_errors):
    if location is None:
        return []
    dids = []
    for number in _split_values(value):
        try:
            did = DID.objects.get(location=location, number=number)
        except DID.DoesNotExist:
            row_errors.append(f"DID {number} was not found in {location.slug}")
        else:
            if did.direct_extension_id and did.direct_extension_id != extension.id:
                row_errors.append(f"DID {number} is already assigned to another extension")
            else:
                dids.append(did)
    return dids


def _lookup_named_records(model, value, location, row_errors, label):
    if location is None:
        return []
    records = []
    for name in _split_values(value):
        try:
            records.append(model.objects.get(location=location, name=name))
        except model.DoesNotExist:
            row_errors.append(f"{label} {name} was not found in {location.slug}")
    return records


def _lookup_paging_groups(value, location, row_errors):
    if location is None:
        return []
    records = []
    for identifier in _split_values(value):
        try:
            records.append(PagingGroup.objects.get(location=location, name=identifier))
        except PagingGroup.DoesNotExist:
            try:
                records.append(PagingGroup.objects.get(location=location, page_code=identifier))
            except PagingGroup.DoesNotExist:
                row_errors.append(f"paging group {identifier} was not found in {location.slug}")
    return records


def _split_values(value) -> list[str]:
    return [item.strip() for item in (value or "").split(";") if item.strip()]


def _text_or_existing(value, existing_value):
    text = (value or "").strip()
    return text if text else existing_value


def _parse_bool(value, default, row_errors, field_name):
    text = (value or "").strip().lower()
    if not text:
        return default
    if text in TRUE_VALUES:
        return True
    if text in FALSE_VALUES:
        return False
    row_errors.append(f"{field_name} must be true or false")
    return default


def _format_bool(value) -> str:
    return "true" if value else "false"


def _record_911_audit(actor, extension_number, outcome, row_number):
    record_audit(
        actor=actor,
        action=AuditAction.CONFIG_CHANGE,
        target=f"extensions/{extension_number}/911",
        outcome=outcome,
        details={"source": "csv_import", "row": row_number, "emergency_calling_enabled": False},
    )
