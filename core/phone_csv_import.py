import csv
import re
from dataclasses import dataclass, field
from io import StringIO
from typing import Any, Mapping


PHONE_CSV_HEADERS = [
    "location_slug",
    "mac_address",
    "model",
    "label",
    "is_active",
    "line_appearances",
    "speed_dials",
]

DID_CSV_HEADERS = [
    "location_slug",
    "number",
    "provider_slug",
    "trunk_name",
    "direct_extension",
    "default_destination",
    "label",
    "is_active",
]

SPEED_DIAL_CSV_HEADERS = [
    "phone_mac_address",
    "position",
    "label",
    "destination",
]

DEFAULT_PHONE_MODELS = frozenset({"CP-9971", "CP-9951", "CP-8961", "other"})
TRUE_VALUES = frozenset({"1", "true", "t", "yes", "y", "enabled", "active"})
FALSE_VALUES = frozenset({"0", "false", "f", "no", "n", "disabled", "inactive"})
DID_PATTERN = re.compile(r"^\+?[1-9]\d{6,14}$")


@dataclass(frozen=True)
class CSVImportLookups:
    locations: frozenset[str] = frozenset()
    phones: Mapping[str, Mapping[str, Any]] = field(default_factory=dict)
    extensions_by_location: Mapping[str, frozenset[str]] = field(default_factory=dict)
    dids: Mapping[str, Mapping[str, Any]] = field(default_factory=dict)
    providers: frozenset[str] = frozenset()
    trunks_by_location: Mapping[str, frozenset[str]] = field(default_factory=dict)
    inbound_destinations_by_location: Mapping[str, frozenset[str]] = field(default_factory=dict)
    speed_dials_by_phone: Mapping[str, frozenset[int]] = field(default_factory=dict)


@dataclass(frozen=True)
class PhoneLinePlan:
    line_index: int
    extension_number: str
    label: str = ""


@dataclass(frozen=True)
class SpeedDialPlan:
    position: int
    label: str
    destination: str


@dataclass(frozen=True)
class CSVImportRowPlan:
    row_number: int
    kind: str
    identifier: str
    operation: str
    values: Mapping[str, Any]
    line_appearances: tuple[PhoneLinePlan, ...] = ()
    speed_dials: tuple[SpeedDialPlan, ...] = ()


@dataclass(frozen=True)
class CSVImportRowError:
    row_number: int
    messages: tuple[str, ...]

    def __str__(self) -> str:
        return f"Row {self.row_number}: {'; '.join(self.messages)}"


@dataclass(frozen=True)
class CSVImportAuditEvent:
    row_number: int
    target: str
    operation: str
    outcome: str
    details: Mapping[str, Any]


@dataclass(frozen=True)
class CSVImportResult:
    kind: str
    dry_run: bool
    rows: tuple[CSVImportRowPlan, ...] = ()
    errors: tuple[CSVImportRowError, ...] = ()
    audit_events: tuple[CSVImportAuditEvent, ...] = ()

    @property
    def valid(self) -> bool:
        return not self.errors

    @property
    def planned_count(self) -> int:
        return len(self.rows)

    def error_messages(self) -> list[str]:
        return [str(error) for error in self.errors]

    def change_messages(self) -> list[str]:
        return [
            f"Row {row.row_number}: {row.operation} {row.kind} {row.identifier}"
            for row in self.rows
        ]


def parse_phone_import_csv(
    content,
    *,
    lookups: CSVImportLookups | None = None,
    dry_run: bool = True,
    allowed_phone_models: set[str] | frozenset[str] | None = None,
) -> CSVImportResult:
    lookups = lookups or CSVImportLookups()
    allowed_phone_models = frozenset(allowed_phone_models or DEFAULT_PHONE_MODELS)
    reader, errors = _reader_for_content(content, PHONE_CSV_HEADERS)
    rows: list[CSVImportRowPlan] = []
    seen_macs: set[str] = set()

    if reader is None:
        return _result("phone", dry_run, rows, errors)

    for row_number, row in enumerate(reader, start=2):
        row_errors: list[str] = []
        location_slug = _text(row.get("location_slug"))
        if not location_slug:
            row_errors.append("location_slug is required")
        elif location_slug not in lookups.locations:
            row_errors.append(f"location_slug {location_slug} was not found")

        mac_address = _normalize_mac(row.get("mac_address"), row_errors, "mac_address")
        if mac_address:
            if mac_address in seen_macs:
                row_errors.append(f"duplicate phone MAC {mac_address} in CSV")
            seen_macs.add(mac_address)

        existing_phone = _existing(lookups.phones, mac_address)
        model = _text(row.get("model")) or _existing_value(existing_phone, "model", "CP-9971")
        if model not in allowed_phone_models:
            row_errors.append(f"model {model} is invalid")

        line_appearances = _parse_line_appearances(row.get("line_appearances"), location_slug, lookups, row_errors)
        speed_dials = _parse_embedded_speed_dials(row.get("speed_dials"), row_errors)
        is_active = _parse_bool(
            row.get("is_active"),
            _existing_value(existing_phone, "is_active", True),
            row_errors,
            "is_active",
        )

        if row_errors:
            errors.append(CSVImportRowError(row_number, tuple(row_errors)))
            continue

        rows.append(
            CSVImportRowPlan(
                row_number=row_number,
                kind="phone",
                identifier=mac_address,
                operation="update" if existing_phone is not None else "create",
                values={
                    "location_slug": location_slug,
                    "mac_address": mac_address,
                    "model": model,
                    "label": _text(row.get("label")),
                    "is_active": is_active,
                },
                line_appearances=line_appearances,
                speed_dials=speed_dials,
            )
        )

    return _result("phone", dry_run, rows, errors)


def parse_did_import_csv(
    content,
    *,
    lookups: CSVImportLookups | None = None,
    dry_run: bool = True,
) -> CSVImportResult:
    lookups = lookups or CSVImportLookups()
    reader, errors = _reader_for_content(content, DID_CSV_HEADERS)
    rows: list[CSVImportRowPlan] = []
    seen_numbers: set[str] = set()

    if reader is None:
        return _result("did", dry_run, rows, errors)

    for row_number, row in enumerate(reader, start=2):
        row_errors: list[str] = []
        location_slug = _text(row.get("location_slug"))
        if not location_slug:
            row_errors.append("location_slug is required")
        elif location_slug not in lookups.locations:
            row_errors.append(f"location_slug {location_slug} was not found")

        number = _text(row.get("number"))
        if not number:
            row_errors.append("number is required")
        elif not DID_PATTERN.fullmatch(number):
            row_errors.append("number must be 7 to 15 digits, optionally prefixed with '+'")
        elif number in seen_numbers:
            row_errors.append(f"duplicate DID {number} in CSV")
        else:
            seen_numbers.add(number)

        provider_slug = _text(row.get("provider_slug"))
        if provider_slug and provider_slug not in lookups.providers:
            row_errors.append(f"provider_slug {provider_slug} was not found")

        trunk_name = _text(row.get("trunk_name"))
        if trunk_name and location_slug and trunk_name not in _lookup_set(lookups.trunks_by_location, location_slug):
            row_errors.append(f"trunk_name {trunk_name} was not found in {location_slug}")

        direct_extension = _text(row.get("direct_extension"))
        if (
            direct_extension
            and location_slug
            and direct_extension not in _lookup_set(lookups.extensions_by_location, location_slug)
        ):
            row_errors.append(f"direct_extension {direct_extension} was not found in {location_slug}")

        default_destination = _text(row.get("default_destination"))
        if (
            default_destination
            and location_slug
            and default_destination not in _lookup_set(lookups.inbound_destinations_by_location, location_slug)
        ):
            row_errors.append(f"default_destination {default_destination} was not found in {location_slug}")

        existing_did = _existing(lookups.dids, number)
        is_active = _parse_bool(
            row.get("is_active"),
            _existing_value(existing_did, "is_active", True),
            row_errors,
            "is_active",
        )

        if row_errors:
            errors.append(CSVImportRowError(row_number, tuple(row_errors)))
            continue

        rows.append(
            CSVImportRowPlan(
                row_number=row_number,
                kind="did",
                identifier=number,
                operation="update" if existing_did is not None else "create",
                values={
                    "location_slug": location_slug,
                    "number": number,
                    "provider_slug": provider_slug,
                    "trunk_name": trunk_name,
                    "direct_extension": direct_extension,
                    "default_destination": default_destination,
                    "label": _text(row.get("label")),
                    "is_active": is_active,
                },
            )
        )

    return _result("did", dry_run, rows, errors)


def parse_speed_dial_import_csv(
    content,
    *,
    lookups: CSVImportLookups | None = None,
    dry_run: bool = True,
) -> CSVImportResult:
    lookups = lookups or CSVImportLookups()
    reader, errors = _reader_for_content(content, SPEED_DIAL_CSV_HEADERS)
    rows: list[CSVImportRowPlan] = []
    seen_positions: set[tuple[str, int]] = set()

    if reader is None:
        return _result("speed_dial", dry_run, rows, errors)

    for row_number, row in enumerate(reader, start=2):
        row_errors: list[str] = []
        phone_mac = _normalize_mac(row.get("phone_mac_address"), row_errors, "phone_mac_address")
        if phone_mac and _existing(lookups.phones, phone_mac) is None:
            row_errors.append(f"phone_mac_address {phone_mac} was not found")

        position = _parse_positive_int(row.get("position"), row_errors, "position")
        if phone_mac and position is not None:
            key = (phone_mac, position)
            if key in seen_positions:
                row_errors.append(f"duplicate speed dial position {position} for phone {phone_mac} in CSV")
            seen_positions.add(key)

        label = _text(row.get("label"))
        if not label:
            row_errors.append("label is required")

        destination = _text(row.get("destination"))
        if not destination:
            row_errors.append("destination is required")

        if row_errors:
            errors.append(CSVImportRowError(row_number, tuple(row_errors)))
            continue

        existing_positions = _lookup_set(lookups.speed_dials_by_phone, phone_mac)
        rows.append(
            CSVImportRowPlan(
                row_number=row_number,
                kind="speed_dial",
                identifier=f"{phone_mac}:{position}",
                operation="update" if position in existing_positions else "create",
                values={
                    "phone_mac_address": phone_mac,
                    "position": position,
                    "label": label,
                    "destination": destination,
                },
            )
        )

    return _result("speed_dial", dry_run, rows, errors)


def normalize_mac_address(value) -> str:
    raw_value = str(value or "").strip().upper()
    if raw_value.startswith("SEP"):
        raw_value = raw_value[3:]
    normalized = re.sub(r"[\s:.-]", "", raw_value)
    if not re.fullmatch(r"[0-9A-F]{12}", normalized):
        raise ValueError("invalid MAC address")
    return normalized


def read_csv_content(content) -> str:
    if hasattr(content, "read"):
        content = content.read()
    if isinstance(content, bytes):
        return content.decode("utf-8-sig")
    return str(content)


def _reader_for_content(content, required_headers):
    csv_text = read_csv_content(content)
    reader = csv.DictReader(StringIO(csv_text))
    errors = _validate_headers(reader.fieldnames, required_headers)
    if errors:
        return None, [CSVImportRowError(1, tuple(errors))]
    return reader, []


def _validate_headers(fieldnames, required_headers) -> list[str]:
    if not fieldnames:
        return ["CSV must include a header row"]
    missing_headers = [header for header in required_headers if header not in fieldnames]
    if missing_headers:
        return [f"CSV is missing required headers: {', '.join(missing_headers)}"]
    return []


def _result(kind, dry_run, rows, errors) -> CSVImportResult:
    row_plans = tuple(rows)
    row_errors = tuple(errors)
    audit_events = [
        CSVImportAuditEvent(
            row_number=error.row_number,
            target=f"{kind}s/csv_import",
            operation="reject",
            outcome="denied",
            details={"source": "csv_import", "errors": list(error.messages), "dry_run": dry_run},
        )
        for error in row_errors
    ]
    if not row_errors:
        outcome = "planned" if dry_run else "success"
        audit_events.extend(
            CSVImportAuditEvent(
                row_number=row.row_number,
                target=f"{kind}s/{row.identifier}",
                operation=row.operation,
                outcome=outcome,
                details={"source": "csv_import", "dry_run": dry_run},
            )
            for row in row_plans
        )
    return CSVImportResult(kind=kind, dry_run=dry_run, rows=row_plans, errors=row_errors, audit_events=tuple(audit_events))


def _parse_line_appearances(value, location_slug, lookups, row_errors) -> tuple[PhoneLinePlan, ...]:
    line_appearances = []
    seen_line_indexes = set()
    for entry in _split_entries(value):
        parts = [part.strip() for part in entry.split(":", 2)]
        if len(parts) < 2 or not parts[0] or not parts[1]:
            row_errors.append(f"line_appearances entry {entry!r} must use line:extension[:label]")
            continue
        line_index = _parse_positive_int(parts[0], row_errors, "line_appearances line")
        extension_number = parts[1]
        if line_index is None:
            continue
        if line_index in seen_line_indexes:
            row_errors.append(f"duplicate line appearance {line_index}")
        seen_line_indexes.add(line_index)
        if location_slug and extension_number not in _lookup_set(lookups.extensions_by_location, location_slug):
            row_errors.append(f"line extension {extension_number} was not found in {location_slug}")
        line_appearances.append(
            PhoneLinePlan(
                line_index=line_index,
                extension_number=extension_number,
                label=parts[2] if len(parts) > 2 else "",
            )
        )
    return tuple(line_appearances)


def _parse_embedded_speed_dials(value, row_errors) -> tuple[SpeedDialPlan, ...]:
    speed_dials = []
    seen_positions = set()
    for entry in _split_entries(value):
        parts = [part.strip() for part in entry.split(":", 2)]
        if len(parts) != 3 or not parts[0] or not parts[1] or not parts[2]:
            row_errors.append(f"speed_dials entry {entry!r} must use position:label:destination")
            continue
        position = _parse_positive_int(parts[0], row_errors, "speed_dials position")
        if position is None:
            continue
        if position in seen_positions:
            row_errors.append(f"duplicate speed dial position {position}")
        seen_positions.add(position)
        speed_dials.append(SpeedDialPlan(position=position, label=parts[1], destination=parts[2]))
    return tuple(speed_dials)


def _split_entries(value) -> list[str]:
    return [entry.strip() for entry in str(value or "").split("|") if entry.strip()]


def _parse_bool(value, default, row_errors, field_name):
    text = _text(value).lower()
    if not text:
        return default
    if text in TRUE_VALUES:
        return True
    if text in FALSE_VALUES:
        return False
    row_errors.append(f"{field_name} must be true or false")
    return default


def _parse_positive_int(value, row_errors, field_name):
    text = _text(value)
    if not text:
        row_errors.append(f"{field_name} is required")
        return None
    try:
        parsed = int(text)
    except ValueError:
        row_errors.append(f"{field_name} must be a positive integer")
        return None
    if parsed < 1:
        row_errors.append(f"{field_name} must be a positive integer")
        return None
    return parsed


def _normalize_mac(value, row_errors, field_name):
    text = _text(value)
    if not text:
        row_errors.append(f"{field_name} is required")
        return ""
    try:
        return normalize_mac_address(text)
    except ValueError:
        row_errors.append(f"{field_name} must be 12 hexadecimal characters")
        return ""


def _text(value) -> str:
    return str(value or "").strip()


def _existing(mapping, key):
    if not key:
        return None
    return mapping.get(key)


def _existing_value(existing, key, default):
    if existing is None:
        return default
    return existing.get(key, default)


def _lookup_set(mapping, key) -> frozenset:
    return frozenset(mapping.get(key, frozenset()))
