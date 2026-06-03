import csv
from io import StringIO

from .models import Phone, PhoneSpeedDial


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
