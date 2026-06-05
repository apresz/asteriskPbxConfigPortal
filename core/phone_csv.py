import csv
from io import StringIO

from django.db import transaction

from .audit import record_audit, record_config_change
from .audit_helpers import audit_model_summary
from .models import (
    AuditAction,
    AuditOutcome,
    DID,
    Extension,
    InboundDestination,
    Location,
    Phone,
    PhoneLineAppearance,
    PhoneSpeedDial,
    Provider,
    Trunk,
)
from .phone_csv_import import (
    CSVImportLookups,
    DID_CSV_HEADERS,
    PHONE_CSV_HEADERS,
    SPEED_DIAL_CSV_HEADERS,
    parse_did_import_csv,
    parse_phone_import_csv,
    parse_speed_dial_import_csv,
)


class PhoneCSVImportError(Exception):
    def __init__(self, result):
        self.result = result
        self.errors = result.error_messages()
        super().__init__("Phone CSV import failed.")


def phone_template_csv() -> str:
    return _template_csv(PHONE_CSV_HEADERS)


def did_template_csv() -> str:
    return _template_csv(DID_CSV_HEADERS)


def speed_dial_template_csv() -> str:
    return _template_csv(SPEED_DIAL_CSV_HEADERS)


def export_phones_csv(phones=None) -> str:
    if phones is None:
        phones = Phone.objects.all()
    phones = phones.select_related("location").prefetch_related(
        "line_appearances__extension",
        "speed_dials",
    )
    output = StringIO()
    writer = csv.DictWriter(output, fieldnames=PHONE_CSV_HEADERS)
    writer.writeheader()
    for phone in phones:
        writer.writerow(
            {
                "location_slug": phone.location.slug,
                "mac_address": phone.mac_address,
                "model": phone.model,
                "label": phone.label,
                "is_active": _format_bool(phone.is_active),
                "line_appearances": _line_appearance_values(phone),
                "speed_dials": _speed_dial_values(phone),
            }
        )
    return output.getvalue()


def export_speed_dials_csv(speed_dials=None) -> str:
    if speed_dials is None:
        speed_dials = PhoneSpeedDial.objects.all()
    speed_dials = speed_dials.select_related("phone")
    output = StringIO()
    writer = csv.DictWriter(output, fieldnames=SPEED_DIAL_CSV_HEADERS)
    writer.writeheader()
    for speed_dial in speed_dials:
        writer.writerow(
            {
                "phone_mac_address": speed_dial.phone.mac_address,
                "position": speed_dial.position,
                "label": speed_dial.label,
                "destination": speed_dial.destination,
            }
        )
    return output.getvalue()


def import_phones_csv(content, *, actor=None, dry_run=True):
    result = parse_phone_import_csv(
        content,
        lookups=_csv_import_lookups(),
        dry_run=dry_run,
        allowed_phone_models=set(Phone.PhoneModel.values),
    )
    if result.errors:
        _record_import_rejection(actor, "phones/csv_import", result)
        raise PhoneCSVImportError(result)
    if result.dry_run:
        return result

    with transaction.atomic():
        for row in result.rows:
            phone = Phone.objects.filter(mac_address=row.identifier).first()
            operation = "update" if phone else "create"
            if phone is None:
                phone = Phone(mac_address=row.identifier)
                before = None
            else:
                before = audit_model_summary(phone, redact=False)

            phone.location = Location.objects.get(slug=row.values["location_slug"])
            phone.mac_address = row.values["mac_address"]
            phone.model = row.values["model"]
            phone.label = row.values["label"]
            phone.is_active = row.values["is_active"]
            phone.full_clean()
            phone.save()

            _replace_phone_line_appearances(phone, row.line_appearances)
            _replace_phone_speed_dials(phone, row.speed_dials)
            record_config_change(
                actor=actor,
                operation=operation,
                instance=phone,
                before=before,
                source="csv_import",
                extra_details={"row": row.row_number, "import_type": "phones"},
            )
    return result


def import_dids_csv(content, *, actor=None, dry_run=True):
    result = parse_did_import_csv(content, lookups=_csv_import_lookups(), dry_run=dry_run)
    if result.errors:
        _record_import_rejection(actor, "dids/csv_import", result)
        raise PhoneCSVImportError(result)
    if result.dry_run:
        return result

    with transaction.atomic():
        for row in result.rows:
            did = DID.objects.filter(number=row.identifier).first()
            operation = "update" if did else "create"
            if did is None:
                did = DID(number=row.identifier)
                before = None
            else:
                before = audit_model_summary(did, redact=False)

            location = Location.objects.get(slug=row.values["location_slug"])
            did.location = location
            did.number = row.values["number"]
            did.provider = _provider_for_slug(row.values["provider_slug"])
            did.trunk = _trunk_for_name(location, row.values["trunk_name"])
            did.direct_extension = _extension_for_number(location, row.values["direct_extension"])
            did.default_destination = _destination_for_name(location, row.values["default_destination"])
            did.label = row.values["label"]
            did.is_active = row.values["is_active"]
            did.full_clean()
            did.save()
            record_config_change(
                actor=actor,
                operation=operation,
                instance=did,
                before=before,
                source="csv_import",
                extra_details={"row": row.row_number, "import_type": "dids"},
            )
    return result


def import_speed_dials_csv(content, *, actor=None, dry_run=True):
    result = parse_speed_dial_import_csv(content, lookups=_csv_import_lookups(), dry_run=dry_run)
    if result.errors:
        _record_import_rejection(actor, "speed-dials/csv_import", result)
        raise PhoneCSVImportError(result)
    if result.dry_run:
        return result

    with transaction.atomic():
        for row in result.rows:
            phone = Phone.objects.get(mac_address=row.values["phone_mac_address"])
            speed_dial = PhoneSpeedDial.objects.filter(phone=phone, position=row.values["position"]).first()
            operation = "update" if speed_dial else "create"
            if speed_dial is None:
                speed_dial = PhoneSpeedDial(phone=phone, position=row.values["position"])
                before = None
            else:
                before = audit_model_summary(speed_dial, redact=False)

            speed_dial.label = row.values["label"]
            speed_dial.destination = row.values["destination"]
            speed_dial.full_clean()
            speed_dial.save()
            record_config_change(
                actor=actor,
                operation=operation,
                instance=speed_dial,
                before=before,
                source="csv_import",
                extra_details={"row": row.row_number, "import_type": "speed_dials"},
            )
    return result


def _template_csv(headers) -> str:
    output = StringIO()
    writer = csv.DictWriter(output, fieldnames=headers)
    writer.writeheader()
    return output.getvalue()


def _format_bool(value) -> str:
    return "true" if value else "false"


def _line_appearance_values(phone) -> str:
    return "|".join(
        f"{appearance.line_index}:{appearance.extension.number}:{appearance.label}"
        for appearance in phone.line_appearances.all()
    )


def _speed_dial_values(phone) -> str:
    return "|".join(
        f"{speed_dial.position}:{speed_dial.label}:{speed_dial.destination}"
        for speed_dial in phone.speed_dials.all()
    )


def _csv_import_lookups() -> CSVImportLookups:
    return CSVImportLookups(
        locations=frozenset(Location.objects.values_list("slug", flat=True)),
        phones={
            mac_address: {
                "location_slug": location_slug,
                "model": model,
                "is_active": is_active,
            }
            for mac_address, location_slug, model, is_active in Phone.objects.select_related("location").values_list(
                "mac_address",
                "location__slug",
                "model",
                "is_active",
            )
        },
        extensions_by_location=_group_values(
            Extension.objects.select_related("location").values_list("location__slug", "number")
        ),
        dids={
            number: {
                "location_slug": location_slug,
                "is_active": is_active,
            }
            for number, location_slug, is_active in DID.objects.select_related("location").values_list(
                "number",
                "location__slug",
                "is_active",
            )
        },
        providers=frozenset(Provider.objects.values_list("slug", flat=True)),
        trunks_by_location=_group_values(Trunk.objects.select_related("location").values_list("location__slug", "name")),
        inbound_destinations_by_location=_group_values(
            InboundDestination.objects.select_related("location").values_list("location__slug", "name")
        ),
        speed_dials_by_phone=_group_int_values(
            PhoneSpeedDial.objects.select_related("phone").values_list("phone__mac_address", "position")
        ),
    )


def _replace_phone_line_appearances(phone, line_appearances) -> None:
    PhoneLineAppearance.objects.filter(phone=phone).delete()
    for line in line_appearances:
        appearance = PhoneLineAppearance(
            phone=phone,
            extension=Extension.objects.get(location=phone.location, number=line.extension_number),
            line_index=line.line_index,
            label=line.label,
        )
        appearance.full_clean()
        appearance.save()


def _replace_phone_speed_dials(phone, speed_dials) -> None:
    PhoneSpeedDial.objects.filter(phone=phone).delete()
    for speed_dial in speed_dials:
        record = PhoneSpeedDial(
            phone=phone,
            position=speed_dial.position,
            label=speed_dial.label,
            destination=speed_dial.destination,
        )
        record.full_clean()
        record.save()


def _provider_for_slug(slug):
    return Provider.objects.get(slug=slug) if slug else None


def _trunk_for_name(location, name):
    return Trunk.objects.get(location=location, name=name) if name else None


def _extension_for_number(location, number):
    return Extension.objects.get(location=location, number=number) if number else None


def _destination_for_name(location, name):
    return InboundDestination.objects.get(location=location, name=name) if name else None


def _group_values(rows):
    groups = {}
    for key, value in rows:
        groups.setdefault(key, set()).add(value)
    return {key: frozenset(values) for key, values in groups.items()}


def _group_int_values(rows):
    groups = {}
    for key, value in rows:
        groups.setdefault(key, set()).add(int(value))
    return {key: frozenset(values) for key, values in groups.items()}


def _record_import_rejection(actor, target, result) -> None:
    record_audit(
        actor=actor,
        action=AuditAction.CONFIG_CHANGE,
        target=target,
        outcome=AuditOutcome.DENIED,
        details={
            "source": "csv_import",
            "dry_run": result.dry_run,
            "import_type": result.kind,
            "error_count": len(result.errors),
            "errors": result.error_messages(),
        },
    )
