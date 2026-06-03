from __future__ import annotations

from typing import Any

from .models import Extension, Location, OutboundRoute


PHONE_APPEARANCE_WARNING_LIMIT = 5


def build_location_config(
    location: Location,
    *,
    require_emergency: bool = False,
    validation: dict[str, list[dict[str, Any]]] | None = None,
) -> dict[str, Any]:
    """Return the PBX configuration data needed by generators and helper scripts."""
    smtp_settings = build_smtp_settings(location)
    routing_validation = validation or validate_location_routing(location, require_emergency=require_emergency)
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
        "provider_trunks": [
            _trunk_payload(trunk)
            for trunk in location.trunks.select_related("provider").filter(is_active=True).order_by("name")
        ],
        "outbound_routes": [
            _outbound_route_payload(route)
            for route in location.outbound_routes.prefetch_related(
                "route_trunks__trunk__provider",
            )
            .filter(is_active=True)
            .order_by("priority", "name")
        ],
        "routing_validation": routing_validation,
        "dialplan_warnings": list(routing_validation["warnings"]),
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


def validate_location_routing(location: Location, *, require_emergency: bool = False) -> dict[str, list[dict[str, Any]]]:
    """Return export validation issues without blocking normal config export."""
    warnings = export_validation_warnings(location)
    errors: list[dict[str, Any]] = []
    emergency_allowed_extensions = list(
        location.extensions.filter(is_active=True, emergency_calling_enabled=True).order_by("number")
    )
    active_routes = list(
        location.outbound_routes.prefetch_related("route_trunks__trunk")
        .filter(is_active=True)
        .order_by("priority", "name")
    )
    emergency_routes = [route for route in active_routes if route.is_emergency_route]

    if require_emergency and emergency_allowed_extensions and not location.emergency_caller_id:
        errors.append(
            {
                "code": "missing_emergency_caller_id",
                "affected_extensions": [extension.number for extension in emergency_allowed_extensions],
                "message": "Location emergency caller ID is required for emergency validation.",
            }
        )
    if require_emergency and emergency_allowed_extensions and not emergency_routes:
        errors.append(
            {
                "code": "missing_emergency_route",
                "affected_extensions": [extension.number for extension in emergency_allowed_extensions],
                "message": "At least one active emergency outbound route is required.",
            }
        )

    warning_trunks = {
        warning["trunk"]: warning
        for warning in warnings
        if warning.get("emergency_capable")
    }
    for route in emergency_routes:
        route_trunks = [link.trunk for link in route.route_trunks.all() if link.trunk.is_active]
        if route.caller_id_source != OutboundRoute.CallerIdSource.EMERGENCY:
            errors.append(
                {
                    "code": "emergency_route_caller_id_source",
                    "route": route.name,
                    "message": "Emergency routes must select the location emergency caller ID.",
                }
            )
        if not any(trunk.is_emergency_capable for trunk in route_trunks):
            errors.append(
                {
                    "code": "missing_emergency_capable_trunk",
                    "route": route.name,
                    "message": "Emergency routes must include an emergency-capable trunk.",
                }
            )
        for trunk in route_trunks:
            warning = warning_trunks.get(trunk.name)
            if warning:
                errors.append(
                    {
                        "code": "emergency_trunk_missing_credentials",
                        "route": route.name,
                        "trunk": trunk.name,
                        "missing": warning["missing"],
                        "message": "Emergency-capable trunks need complete provider credentials.",
                    }
                )

    return {"warnings": warnings, "errors": errors}


def export_validation_warnings(location: Location) -> list[dict[str, Any]]:
    warnings: list[dict[str, Any]] = []
    warnings.extend(provider_credential_warnings(location))
    warnings.extend(suspicious_did_warnings(location))
    warnings.extend(phone_inventory_warnings(location))
    warnings.extend(extension_appearance_warnings(location))
    warnings.extend(smtp_warnings(location))
    warnings.extend(fallback_destination_warnings(location))
    warnings.extend(disabled_emergency_extension_warnings(location))
    return warnings


def provider_credential_warnings(location: Location) -> list[dict[str, Any]]:
    warnings = []
    for trunk in location.trunks.select_related("provider").filter(is_active=True).order_by("name"):
        missing = []
        if not trunk.host:
            missing.append("host")
        if not trunk.username:
            missing.append("username")
        if not trunk.password:
            missing.append("password")
        if missing:
            warnings.append(
                {
                    "code": "provider_trunk_missing_credentials",
                    "provider": trunk.provider.name,
                    "trunk": trunk.name,
                    "trunk_type": trunk.trunk_type,
                    "missing": missing,
                    "emergency_capable": trunk.is_emergency_capable,
                    "message": f"{trunk.name} is missing {', '.join(missing)}.",
                }
            )
    return warnings


def suspicious_did_warnings(location: Location) -> list[dict[str, Any]]:
    warnings: list[dict[str, Any]] = []
    candidates = [
        ("location_default_did", location.default_did, location.name),
        ("location_emergency_caller_id", location.emergency_caller_id, location.name),
    ]
    candidates.extend(
        ("did", did.number, did.label or did.number)
        for did in location.dids.filter(is_active=True).order_by("number")
    )
    candidates.extend(
        ("extension_caller_id", extension.caller_id_number, extension.number)
        for extension in location.extensions.filter(is_active=True)
        .exclude(caller_id_number="")
        .order_by("number")
    )
    candidates.extend(
        ("route_custom_caller_id", route.caller_id_number, route.name)
        for route in location.outbound_routes.filter(
            is_active=True,
            caller_id_source=OutboundRoute.CallerIdSource.CUSTOM,
        ).order_by("priority", "name")
        if route.caller_id_number
    )

    for source, number, label in candidates:
        if not number or number.startswith("+"):
            continue
        warnings.append(
            {
                "code": "suspicious_did",
                "source": source,
                "label": label,
                "number": number,
                "reason": "missing_plus_prefix",
                "message": f"{number} is dialable but not E.164-style; verify DID/caller ID formatting.",
            }
        )
    return warnings


def phone_inventory_warnings(location: Location) -> list[dict[str, Any]]:
    warnings: list[dict[str, Any]] = []
    phones = (
        location.phones.filter(is_active=True)
        .prefetch_related("line_appearances__extension")
        .order_by("mac_address")
    )
    for phone in phones:
        active_line_numbers = [
            appearance.extension.number
            for appearance in phone.line_appearances.all()
            if appearance.extension.is_active
        ]
        if not active_line_numbers:
            warnings.append(
                {
                    "code": "phone_incomplete",
                    "phone": phone.mac_address,
                    "missing": ["line_appearances"],
                    "message": f"{phone.sep_identifier} has no active line appearances.",
                }
            )
        if not phone.firmware_load_name:
            warnings.append(
                {
                    "code": "phone_missing_firmware_load_name",
                    "phone": phone.mac_address,
                    "model": phone.model,
                    "message": f"{phone.sep_identifier} has no firmware/load name configured.",
                }
            )
    return warnings


def extension_appearance_warnings(location: Location) -> list[dict[str, Any]]:
    warnings: list[dict[str, Any]] = []
    extensions = (
        location.extensions.filter(is_active=True)
        .prefetch_related("phone_appearances__phone")
        .order_by("number")
    )
    for extension in extensions:
        appearance_count = sum(
            1
            for appearance in extension.phone_appearances.all()
            if appearance.phone.is_active
        )
        if appearance_count > PHONE_APPEARANCE_WARNING_LIMIT:
            warnings.append(
                {
                    "code": "extension_over_phone_appearance_limit",
                    "extension": extension.number,
                    "appearance_count": appearance_count,
                    "limit": PHONE_APPEARANCE_WARNING_LIMIT,
                    "message": (
                        f"Extension {extension.number} has {appearance_count} active phone appearances; "
                        f"recommended maximum is {PHONE_APPEARANCE_WARNING_LIMIT}."
                    ),
                }
            )
    return warnings


def smtp_warnings(location: Location) -> list[dict[str, Any]]:
    if build_smtp_settings(location):
        return []
    affected_extensions = list(
        location.extensions.filter(is_active=True, voicemail_enabled=True)
        .exclude(email="")
        .order_by("number")
        .values_list("number", flat=True)
    )
    if not affected_extensions:
        return []
    return [
        {
            "code": "smtp_not_configured",
            "affected_extensions": affected_extensions,
            "message": "SMTP is optional but not configured; voicemail email delivery is disabled.",
        }
    ]


def fallback_destination_warnings(location: Location) -> list[dict[str, Any]]:
    warnings: list[dict[str, Any]] = []
    for did in location.dids.filter(is_active=True).order_by("number"):
        if did.direct_extension_id or did.default_destination_id or location.default_inbound_destination_id:
            continue
        warnings.append(
            {
                "code": "did_missing_fallback_destination",
                "did": did.number,
                "message": f"DID {did.number} has no direct extension, DID fallback, or location fallback.",
            }
        )

    for ivr in location.ivrs.filter(is_active=True).order_by("name"):
        missing = [
            field_name
            for field_name in (
                "business_hours_destination",
                "after_hours_destination",
                "timeout_destination",
                "invalid_destination",
            )
            if getattr(ivr, f"{field_name}_id") is None
        ]
        if missing:
            warnings.append(
                {
                    "code": "ivr_incomplete_fallback_destinations",
                    "ivr": ivr.name,
                    "missing": missing,
                    "message": f"IVR {ivr.name} has incomplete fallback destinations.",
                }
            )

    for queue in location.queues.filter(is_active=True).order_by("name"):
        if queue.overflow_destination_id:
            continue
        warnings.append(
            {
                "code": "queue_missing_overflow_destination",
                "queue": queue.name,
                "message": f"Queue {queue.name} has no overflow destination.",
            }
        )

    for feature_code in location.feature_codes.filter(is_active=True).order_by("code"):
        if feature_code.destination_id:
            continue
        warnings.append(
            {
                "code": "feature_code_missing_destination",
                "feature_code": feature_code.code,
                "message": f"Feature code {feature_code.code} has no destination.",
            }
        )
    return warnings


def disabled_emergency_extension_warnings(location: Location) -> list[dict[str, Any]]:
    warnings: list[dict[str, Any]] = []
    for extension in location.extensions.filter(is_active=True, emergency_calling_enabled=False).order_by("number"):
        warnings.append(
            {
                "code": "extension_911_disabled",
                "extension": extension.number,
                "message": (
                    f"911 calling is disabled for extension {extension.number} by Admin override; "
                    "emergency export hard-block excludes this extension."
                ),
            }
        )
    return warnings


def select_route_caller_id(route: OutboundRoute, extension: Extension | None = None) -> str:
    if route.caller_id_source == OutboundRoute.CallerIdSource.EMERGENCY:
        return route.location.emergency_caller_id
    if route.caller_id_source == OutboundRoute.CallerIdSource.CUSTOM:
        return route.caller_id_number
    if route.caller_id_source == OutboundRoute.CallerIdSource.EXTENSION_DID:
        if extension is not None:
            direct_did = (
                extension.direct_dids.filter(location=route.location, is_active=True)
                .order_by("number")
                .first()
            )
            if direct_did:
                return direct_did.number
            if extension.caller_id_number:
                return extension.caller_id_number
        return route.location.default_did
    return route.location.default_did


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


def _trunk_payload(trunk) -> dict[str, Any]:
    return {
        "name": trunk.name,
        "provider": trunk.provider.slug,
        "provider_name": trunk.provider.name,
        "type": trunk.trunk_type,
        "host": trunk.host,
        "credentials": {
            "username": trunk.username,
            "password": trunk.password,
        },
        "emergency_capable": trunk.is_emergency_capable,
    }


def _outbound_route_payload(route: OutboundRoute) -> dict[str, Any]:
    return {
        "name": route.name,
        "dial_pattern": route.dial_pattern,
        "priority": route.priority,
        "emergency": route.is_emergency_route,
        "caller_id": {
            "source": route.caller_id_source,
            "number": select_route_caller_id(route),
        },
        "trunks": [
            {
                "priority": route_trunk.priority,
                "name": route_trunk.trunk.name,
                "provider": route_trunk.trunk.provider.slug,
                "type": route_trunk.trunk.trunk_type,
                "emergency_capable": route_trunk.trunk.is_emergency_capable,
            }
            for route_trunk in route.route_trunks.all()
            if route_trunk.trunk.is_active
        ],
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
