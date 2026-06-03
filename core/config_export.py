from __future__ import annotations

from typing import Any

from .models import Extension, Location, OutboundRoute


def build_location_config(location: Location) -> dict[str, Any]:
    """Return the PBX configuration data needed by generators and helper scripts."""
    smtp_settings = build_smtp_settings(location)
    routing_validation = validate_location_routing(location)
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
        "helper_scripts": {
            "recording_retention_days": location.recording_retention_days,
        },
    }


def validate_location_routing(location: Location, *, require_emergency: bool = False) -> dict[str, list[dict[str, Any]]]:
    """Return routing validation issues without blocking normal config export."""
    warnings = provider_credential_warnings(location)
    errors: list[dict[str, Any]] = []
    active_routes = list(
        location.outbound_routes.prefetch_related("route_trunks__trunk")
        .filter(is_active=True)
        .order_by("priority", "name")
    )
    emergency_routes = [route for route in active_routes if route.is_emergency_route]

    if require_emergency and not location.emergency_caller_id:
        errors.append(
            {
                "code": "missing_emergency_caller_id",
                "message": "Location emergency caller ID is required for emergency validation.",
            }
        )
    if require_emergency and not emergency_routes:
        errors.append(
            {
                "code": "missing_emergency_route",
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
