from __future__ import annotations

from typing import Any

from .models import Location


def build_location_config(location: Location) -> dict[str, Any]:
    """Return the PBX configuration data needed by generators and helper scripts."""
    smtp_settings = build_smtp_settings(location)
    return {
        "location": {
            "id": location.id,
            "slug": location.slug,
            "name": location.name,
            "timezone": location.timezone,
        },
        "voicemail": {
            "smtp": smtp_settings,
            "mailboxes": [
                _voicemail_mailbox(extension, smtp_settings)
                for extension in location.extensions.filter(is_active=True).order_by("number")
            ],
        },
        "recording": {
            "retention_days": location.recording_retention_days,
            "extensions": [
                {
                    "number": extension.number,
                    "policy": extension.recording_policy,
                }
                for extension in location.extensions.filter(is_active=True).order_by("number")
            ],
            "queues": [
                {
                    "name": queue.name,
                    "policy": queue.recording_policy,
                }
                for queue in location.queues.filter(is_active=True).order_by("name")
            ],
            "routes": [
                {
                    "name": route.name,
                    "dial_pattern": route.dial_pattern,
                    "priority": route.priority,
                    "policy": route.recording_policy,
                }
                for route in location.outbound_routes.filter(is_active=True).order_by("priority", "name")
            ],
        },
        "inbound": build_inbound_config(location),
        "helper_scripts": {
            "recording_retention_days": location.recording_retention_days,
        },
    }


def build_smtp_settings(location: Location) -> dict[str, Any] | None:
    if not (location.smtp_host and location.smtp_from_email):
        return None
    return {
        "host": location.smtp_host,
        "port": location.smtp_port,
        "from_email": location.smtp_from_email,
        "use_tls": location.smtp_use_tls,
        "use_ssl": location.smtp_use_ssl,
        "username": location.smtp_username,
        "password": location.smtp_password,
    }


def build_inbound_config(location: Location) -> dict[str, Any]:
    return {
        "default_destination": _destination_ref(location.default_inbound_destination),
        "dids": [
            _did_route(did)
            for did in location.dids.filter(is_active=True)
            .select_related(
                "direct_extension",
                "default_destination",
                "location__default_inbound_destination",
            )
            .order_by("number")
        ],
        "ivrs": [
            _ivr_config(ivr)
            for ivr in location.ivrs.filter(is_active=True)
            .select_related(
                "business_hours_destination",
                "after_hours_destination",
                "timeout_destination",
                "invalid_destination",
            )
            .prefetch_related("menu_options__destination")
            .order_by("name")
        ],
        "ring_groups": [
            {
                "name": ring_group.name,
                "strategy": ring_group.strategy,
                "timeout_seconds": ring_group.timeout_seconds,
                "members": [
                    {
                        "extension": member.extension.number,
                        "priority": member.priority,
                    }
                    for member in ring_group.members.select_related("extension").order_by("priority", "extension__number")
                ],
            }
            for ring_group in location.ring_groups.filter(is_active=True).order_by("name")
        ],
        "queues": [
            {
                "name": queue.name,
                "strategy": queue.strategy,
                "timeout_seconds": queue.timeout_seconds,
                "retry_seconds": queue.retry_seconds,
                "music_on_hold": queue.music_on_hold,
                "overflow_destination": _destination_ref(queue.overflow_destination),
                "members": [
                    {
                        "extension": member.extension.number,
                        "penalty": member.penalty,
                    }
                    for member in queue.members.select_related("extension").order_by("penalty", "extension__number")
                ],
            }
            for queue in location.queues.filter(is_active=True).select_related("overflow_destination").order_by("name")
        ],
        "paging_groups": [
            {
                "name": paging_group.name,
                "page_code": paging_group.page_code,
                "members": [
                    member.extension.number
                    for member in paging_group.members.select_related("extension").order_by("extension__number")
                ],
            }
            for paging_group in location.paging_groups.filter(is_active=True).order_by("page_code")
        ],
        "feature_codes": [
            {
                "code": feature_code.code,
                "name": feature_code.name,
                "feature_type": feature_code.feature_type,
                "destination": _destination_ref(feature_code.destination),
            }
            for feature_code in location.feature_codes.filter(is_active=True).select_related("destination").order_by("code")
        ],
    }


def _did_route(did) -> dict[str, Any]:
    if did.direct_extension_id:
        route_source = "direct_extension"
    elif did.location_default_destination:
        route_source = "location_default"
    else:
        route_source = "did_default"

    return {
        "number": did.number,
        "label": did.label,
        "direct_extension": did.direct_extension.number if did.direct_extension_id else "",
        "default_destination": _destination_ref(did.default_destination),
        "route_source": route_source,
        "effective_destination": _effective_destination_ref(did),
    }


def _ivr_config(ivr) -> dict[str, Any]:
    return {
        "name": ivr.name,
        "prompt_name": ivr.prompt_name,
        "business_hours_destination": _destination_ref(ivr.business_hours_destination),
        "after_hours_destination": _destination_ref(ivr.after_hours_destination),
        "timeout_seconds": ivr.timeout_seconds,
        "timeout_destination": _destination_ref(ivr.timeout_destination),
        "invalid_destination": _destination_ref(ivr.invalid_destination),
        "menu_options": [
            {
                "digit": option.digit,
                "label": option.label,
                "destination": _destination_ref(option.destination),
            }
            for option in ivr.menu_options.all()
        ],
    }


def _effective_destination_ref(did) -> dict[str, Any] | None:
    if did.direct_extension_id:
        return {
            "type": "extension",
            "number": did.direct_extension.number,
            "name": did.direct_extension.display_name,
        }
    return _destination_ref(did.location_default_destination or did.default_destination)


def _destination_ref(destination) -> dict[str, Any] | None:
    if destination is None:
        return None
    return {
        "name": destination.name,
        "type": destination.destination_type,
        "target": _target_ref(destination.destination_type, destination.target),
    }


def _target_ref(destination_type: str, target) -> dict[str, Any] | None:
    if target is None:
        return None
    if destination_type == "extension":
        return {
            "number": target.number,
            "name": target.display_name,
        }
    return {
        "id": target.id,
        "name": target.name,
    }


def _voicemail_mailbox(extension, smtp_settings: dict[str, Any] | None) -> dict[str, Any]:
    email_enabled = bool(extension.voicemail_enabled and extension.email and smtp_settings)
    return {
        "number": extension.number,
        "name": extension.display_name,
        "enabled": extension.voicemail_enabled,
        "pin": extension.voicemail_pin,
        "email_enabled": email_enabled,
        "email": extension.email if email_enabled else "",
    }
