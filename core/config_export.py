from __future__ import annotations

from dataclasses import dataclass
import hashlib
import ipaddress
import json
from pathlib import Path
import re
from io import BytesIO
from typing import Any
import xml.etree.ElementTree as ET
import zipfile

from django.db import transaction
from django.db.models import Max
from django.utils import timezone

from .models import (
    AudioPrompt,
    ConfigVersion,
    Extension,
    FeatureCode,
    Location,
    OutboundRoute,
    Phone,
    Trunk,
    normalize_mac_address,
)

PHONE_APPEARANCE_WARNING_LIMIT = 5

ASTERISK_CONFIG_FILENAMES = (
    "pjsip.conf",
    "iax.conf",
    "extensions.conf",
    "voicemail.conf",
    "queues.conf",
    "manager.conf",
    "features.conf",
    "musiconhold.conf",
    "cdr.conf",
    "cel.conf",
    "recording.conf",
    "retention.conf",
)

CISCO_DIRECTORY_FILENAME = "company-directory.xml"
CISCO_FIRMWARE_CHECKLIST_FILENAME = "firmware/CISCO-FIRMWARE-CHECKLIST.txt"
CISCO_FIRMWARE_PLACEHOLDER_FILENAME = "firmware/README-no-firmware-bundled.txt"
CISCO_TRANSPORT_LAYER_PROTOCOL = "TCP"

CISCO_PHONE_MODELS = {
    Phone.PhoneModel.CISCO_9971,
    Phone.PhoneModel.CISCO_9951,
    Phone.PhoneModel.CISCO_8961,
}

CISCO_MODEL_PRODUCTS = {
    Phone.PhoneModel.CISCO_9971: "Cisco CP-9971",
    Phone.PhoneModel.CISCO_9951: "Cisco CP-9951",
    Phone.PhoneModel.CISCO_8961: "Cisco CP-8961",
}

ZIP_TIMESTAMP = (1980, 1, 1, 0, 0, 0)


@dataclass(frozen=True)
class ConfigExportArchive:
    archive_bytes: bytes
    checksum: str
    file_manifest: list[dict[str, Any]]
    manifest: dict[str, Any]


class ConfigExportValidationError(Exception):
    def __init__(self, validation: dict[str, list[dict[str, Any]]]):
        self.validation = validation
        super().__init__("Export blocked by validation errors.")


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
        "asterisk_configs": build_asterisk_config_files(location),
        "tftp": build_cisco_tftp_output(location),
        "helper_scripts": {
            "recording_retention_days": location.recording_retention_days,
        },
    }


def build_asterisk_config_files(location: Location) -> dict[str, str]:
    """Return rendered Asterisk config files for a location export."""
    renderers = {
        "pjsip.conf": _render_pjsip_conf,
        "iax.conf": _render_iax_conf,
        "extensions.conf": _render_extensions_conf,
        "voicemail.conf": _render_voicemail_conf,
        "queues.conf": _render_queues_conf,
        "manager.conf": _render_manager_conf,
        "features.conf": _render_features_conf,
        "musiconhold.conf": _render_musiconhold_conf,
        "cdr.conf": _render_cdr_conf,
        "cel.conf": _render_cel_conf,
        "recording.conf": _render_recording_conf,
        "retention.conf": _render_retention_conf,
    }
    return {filename: renderers[filename](location) for filename in ASTERISK_CONFIG_FILENAMES}


def build_route_generation_choices(location: Location) -> dict[str, list[dict[str, Any]]]:
    """Expose dialplan routing choices for tests and export previews."""
    local_extensions = list(
        location.extensions.filter(is_active=True).order_by("number")
    )
    remote_extensions = list(
        Extension.objects.select_related("location")
        .filter(is_active=True, location__is_active=True)
        .exclude(location=location)
        .order_by("number")
    )
    emergency_patterns = [
        _asterisk_pattern(route.dial_pattern)
        for route in _active_outbound_routes(location, emergency=True)
    ] or ["911"]

    return {
        "local_extensions": [
            {
                "number": extension.number,
                "target": f"PJSIP/{extension.number}",
                "context": "local-extensions",
            }
            for extension in local_extensions
        ],
        "remote_extensions": [
            {
                "number": extension.number,
                "owner_location": extension.location.slug,
                "transport": "iax2",
                "peer": _iax_peer_name(extension.location),
                "target": f"IAX2/{_iax_peer_name(extension.location)}/${{EXTEN}}",
            }
            for extension in remote_extensions
        ],
        "emergency_blocks": [
            {
                "extension": extension.number,
                "patterns": emergency_patterns,
            }
            for extension in local_extensions
            if not extension.emergency_calling_enabled
        ],
    }


def mac_to_sep_filename(mac_address: str) -> str:
    return f"SEP{normalize_mac_address(mac_address)}.cnf.xml"


def build_cisco_tftp_output(location: Location) -> dict[str, Any]:
    """Return Cisco TFTP artifacts for the location export snapshot."""
    directory_url = _cisco_directory_url(location)
    phones = list(
        location.phones.filter(is_active=True, model__in=CISCO_PHONE_MODELS)
        .select_related("location")
        .prefetch_related("line_appearances__extension", "speed_dials")
        .order_by("mac_address")
    )
    phone_files = [
        _tftp_file(
            mac_to_sep_filename(phone.mac_address),
            _cisco_phone_config_xml(phone, directory_url),
            "application/xml",
        )
        for phone in phones
    ]
    directory_file = _tftp_file(
        CISCO_DIRECTORY_FILENAME,
        _company_directory_xml(),
        "application/xml",
    )
    firmware_files = _firmware_files(phones)
    files = [*phone_files, directory_file, *firmware_files]

    return {
        "directory_url": directory_url,
        "files": sorted(files, key=lambda file: file["path"]),
        "phone_files": [
            {
                "mac_address": phone.mac_address,
                "model": phone.model,
                "filename": mac_to_sep_filename(phone.mac_address),
                "firmware_load_name": phone.firmware_load_name,
                "line_count": len(_active_line_appearances(phone)),
                "speed_dial_count": len(list(phone.speed_dials.all())),
            }
            for phone in phones
        ],
        "firmware": {
            "checklist": CISCO_FIRMWARE_CHECKLIST_FILENAME,
            "placeholder": CISCO_FIRMWARE_PLACEHOLDER_FILENAME,
            "bundled": False,
        },
    }


def create_config_version(
    location: Location,
    *,
    exported_by=None,
    require_emergency: bool = True,
    rollback_of: ConfigVersion | None = None,
) -> ConfigVersion:
    validation = validate_location_routing(location, require_emergency=require_emergency)
    if validation["errors"]:
        raise ConfigExportValidationError(validation)

    with transaction.atomic():
        locked_location = Location.objects.select_for_update().get(pk=location.pk)
        version_number = (
            ConfigVersion.objects.filter(location=locked_location).aggregate(last=Max("version_number"))["last"] or 0
        ) + 1
        exported_at = timezone.now()
        archive = build_config_export_archive(
            locked_location,
            version_number=version_number,
            exported_at=exported_at,
            exported_by=exported_by,
            validation=validation,
            require_emergency=require_emergency,
        )
        return ConfigVersion.objects.create(
            location=locked_location,
            version_number=version_number,
            exported_by=exported_by if getattr(exported_by, "is_authenticated", False) else None,
            exported_at=exported_at,
            checksum=archive.checksum,
            warnings=validation["warnings"],
            emergency_status=_emergency_status(validation, require_emergency=require_emergency),
            file_manifest=archive.file_manifest,
            deployment_snapshot=_deployment_snapshot(locked_location),
            archive=archive.archive_bytes,
            archive_size_bytes=len(archive.archive_bytes),
            rollback_of=rollback_of,
        )


def build_config_export_archive(
    location: Location,
    *,
    version_number: int,
    exported_at,
    exported_by=None,
    validation: dict[str, list[dict[str, Any]]] | None = None,
    require_emergency: bool = True,
) -> ConfigExportArchive:
    validation = validation or validate_location_routing(location, require_emergency=require_emergency)
    config = build_location_config(location, require_emergency=require_emergency, validation=validation)
    exported_by_username = exported_by.get_username() if getattr(exported_by, "is_authenticated", False) else "system"
    files = _export_payload_files(location, config)
    payload_manifest = [_manifest_entry(path, content, content_type) for path, content, content_type in files]
    manifest = {
        "format": "pbx-config-export/v1",
        "version": {
            "number": version_number,
            "exported_at": exported_at.isoformat(),
            "exported_by": exported_by_username,
        },
        "location": {
            "id": location.id,
            "slug": location.slug,
            "name": location.name,
            "timezone": location.timezone,
        },
        "emergency_status": _emergency_status(validation, require_emergency=require_emergency),
        "warnings": validation["warnings"],
        "deployment": _deployment_snapshot(location),
        "files": payload_manifest,
    }
    manifest_content = _json_bytes(manifest)
    archive_files = [
        *files,
        ("manifest.json", manifest_content, "application/json"),
    ]
    checksum_lines = [
        f"{_sha256(content)}  {path}"
        for path, content, _content_type in archive_files
    ]
    checksum_content = ("\n".join(checksum_lines) + "\n").encode("utf-8")
    archive_files.append(("SHA256SUMS", checksum_content, "text/plain"))

    archive_bytes = _zip_archive(archive_files)
    file_manifest = [
        _manifest_entry(path, content, content_type)
        for path, content, content_type in archive_files
    ]
    return ConfigExportArchive(
        archive_bytes=archive_bytes,
        checksum=_sha256(archive_bytes),
        file_manifest=file_manifest,
        manifest=manifest,
    )


def write_config_version_directory(version: ConfigVersion, output_dir: str | Path) -> None:
    target = Path(output_dir)
    target.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(BytesIO(bytes(version.archive))) as archive:
        for zip_info in archive.infolist():
            destination = (target / zip_info.filename).resolve()
            try:
                destination.relative_to(target.resolve())
            except ValueError as exc:
                raise ValueError(f"Unsafe ZIP member path: {zip_info.filename}") from exc
            destination.parent.mkdir(parents=True, exist_ok=True)
            destination.write_bytes(archive.read(zip_info.filename))


def _export_payload_files(location: Location, config: dict[str, Any]) -> list[tuple[str, bytes, str]]:
    files: list[tuple[str, bytes, str]] = [
        ("docker-compose.yml", _docker_compose_yml(location).encode("utf-8"), "application/x-yaml"),
        (".env.example", _env_example(location).encode("utf-8"), "text/plain"),
    ]
    files.extend(
        (
            f"asterisk/{filename}",
            config["asterisk_configs"][filename].encode("utf-8"),
            "text/plain",
        )
        for filename in ASTERISK_CONFIG_FILENAMES
    )
    files.extend(
        (
            f"tftp/{file['path'].lstrip('/')}",
            file["content"].encode("utf-8"),
            file["content_type"],
        )
        for file in config["tftp"]["files"]
    )
    return files


def _docker_compose_yml(location: Location) -> str:
    return "\n".join(
        [
            "services:",
            "  asterisk:",
            "    image: asterisk:latest",
            f"    container_name: pbx-{location.slug}",
            "    restart: unless-stopped",
            "    env_file:",
            "      - .env",
            "    network_mode: host",
            "    volumes:",
            "      - ./asterisk:/etc/asterisk:ro",
            "      - ./tftp:/srv/tftp:ro",
            "",
        ]
    )


def _env_example(location: Location) -> str:
    return "\n".join(
        [
            f"PBX_LOCATION_SLUG={location.slug}",
            f"PBX_LAN_IP={location.pbx_lan_ip}",
            f"PBX_WARP_IP={location.pbx_warp_ip}",
            f"TZ={location.timezone}",
            "ASTERISK_UID=1000",
            "ASTERISK_GID=1000",
            "",
        ]
    )


def _deployment_snapshot(location: Location) -> dict[str, Any]:
    return {
        "location_deployment_status": location.deployment_status,
        "last_deployed_at": location.last_deployed_at.isoformat() if location.last_deployed_at else None,
        "ssh_host_configured": bool(location.deployment_ssh_host),
        "ssh_username_configured": bool(location.deployment_ssh_username),
        "ssh_private_key_configured": bool(location.deployment_ssh_private_key),
        "ssh_known_hosts_configured": bool(location.deployment_ssh_known_hosts),
    }


def _emergency_status(
    validation: dict[str, list[dict[str, Any]]],
    *,
    require_emergency: bool,
) -> dict[str, Any]:
    error_codes = [error["code"] for error in validation["errors"]]
    warning_codes = [warning["code"] for warning in validation["warnings"]]
    return {
        "required": require_emergency,
        "blocked": bool(validation["errors"]),
        "error_codes": error_codes,
        "warning_codes": warning_codes,
    }


def _manifest_entry(path: str, content: bytes, content_type: str) -> dict[str, Any]:
    return {
        "path": path,
        "size": len(content),
        "sha256": _sha256(content),
        "content_type": content_type,
    }


def _json_bytes(payload: dict[str, Any]) -> bytes:
    return (json.dumps(payload, indent=2, sort_keys=True) + "\n").encode("utf-8")


def _sha256(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()


def _zip_archive(files: list[tuple[str, bytes, str]]) -> bytes:
    output = BytesIO()
    with zipfile.ZipFile(output, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for path, content, _content_type in files:
            zip_info = zipfile.ZipInfo(path, date_time=ZIP_TIMESTAMP)
            zip_info.compress_type = zipfile.ZIP_DEFLATED
            zip_info.external_attr = 0o644 << 16
            archive.writestr(zip_info, content)
    return output.getvalue()


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


def _cisco_directory_url(location: Location) -> str:
    host = f"[{location.pbx_lan_ip}]" if ":" in location.pbx_lan_ip else location.pbx_lan_ip
    return f"http://{host}/cisco/{CISCO_DIRECTORY_FILENAME}"


def _cisco_phone_config_xml(phone: Phone, directory_url: str) -> str:
    root = ET.Element("device")
    _xml_text(root, "deviceProtocol", "SIP")
    _xml_text(root, "product", CISCO_MODEL_PRODUCTS[phone.model])
    _xml_text(root, "model", phone.model)
    _xml_text(root, "phoneLabel", phone.label or phone.sep_identifier)
    _xml_text(root, "transportLayerProtocol", CISCO_TRANSPORT_LAYER_PROTOCOL)
    _xml_text(root, "directoryURL", directory_url)
    _xml_text(root, "loadInformation", phone.firmware_load_name)

    device_pool = ET.SubElement(root, "devicePool")
    date_time = ET.SubElement(device_pool, "dateTimeSetting")
    _xml_text(date_time, "dateTemplate", "M/D/Ya")
    _xml_text(date_time, "timeZone", phone.location.timezone)
    call_manager_group = ET.SubElement(device_pool, "callManagerGroup")
    members = ET.SubElement(call_manager_group, "members")
    member = ET.SubElement(members, "member", {"priority": "0"})
    call_manager = ET.SubElement(member, "callManager")
    ports = ET.SubElement(call_manager, "ports")
    _xml_text(ports, "ethernetPhonePort", "2000")
    _xml_text(ports, "sipPort", str(phone.location.sip_port))
    _xml_text(ports, "securedSipPort", "5061")
    _xml_text(call_manager, "processNodeName", phone.location.pbx_lan_ip)

    sip_profile = ET.SubElement(root, "sipProfile")
    sip_proxies = ET.SubElement(sip_profile, "sipProxies")
    _xml_text(sip_proxies, "backupProxy", "")
    _xml_text(sip_proxies, "backupProxyPort", "")
    _xml_text(sip_proxies, "emergencyProxy", "")
    _xml_text(sip_proxies, "emergencyProxyPort", "")
    _xml_text(sip_proxies, "outboundProxy", "")
    _xml_text(sip_proxies, "outboundProxyPort", "")
    _xml_text(sip_proxies, "registerWithProxy", "true")
    _xml_text(sip_profile, "sipPort", str(phone.location.sip_port))
    _xml_text(sip_profile, "transportLayerProtocol", CISCO_TRANSPORT_LAYER_PROTOCOL)
    _xml_text(sip_profile, "phoneLabel", phone.label or phone.sep_identifier)
    _xml_text(sip_profile, "dialTemplate", "dialplan.xml")

    sip_lines = ET.SubElement(sip_profile, "sipLines")
    active_lines = _active_line_appearances(phone)
    for appearance in active_lines:
        _cisco_line_xml(sip_lines, appearance)
    line_button_offset = max((appearance.line_index for appearance in active_lines), default=0)
    for speed_dial in phone.speed_dials.all():
        _cisco_speed_dial_xml(sip_lines, speed_dial, line_button_offset + speed_dial.position)

    services = ET.SubElement(root, "phoneServices")
    _xml_text(services, "provisioning", "0")
    service = ET.SubElement(services, "phoneService", {"type": "1", "category": "0"})
    _xml_text(service, "name", "Company Directory")
    _xml_text(service, "url", directory_url)
    _xml_text(service, "vendor", "Local PBX")
    _xml_text(service, "version", "1")
    return _xml_string(root)


def _cisco_line_xml(parent: ET.Element, appearance) -> None:
    extension = appearance.extension
    line = ET.SubElement(parent, "line", {"button": str(appearance.line_index)})
    label = appearance.label or extension.display_name or extension.number
    auth_name = extension.sip_username or extension.number
    _xml_text(line, "featureID", "9")
    _xml_text(line, "featureLabel", label)
    _xml_text(line, "proxy", "USECALLMANAGER")
    _xml_text(line, "port", str(extension.location.sip_port))
    _xml_text(line, "name", extension.number)
    _xml_text(line, "displayName", extension.display_name)
    _xml_text(line, "authName", auth_name)
    _xml_text(line, "authPassword", extension.sip_password)
    _xml_text(line, "contact", extension.number)
    _xml_text(line, "messagesNumber", "*97")


def _cisco_speed_dial_xml(parent: ET.Element, speed_dial, button: int) -> None:
    line = ET.SubElement(parent, "line", {"button": str(button)})
    _xml_text(line, "featureID", "2")
    _xml_text(line, "featureLabel", speed_dial.label)
    _xml_text(line, "speedDialNumber", speed_dial.destination)


def _company_directory_xml() -> str:
    root = ET.Element("CiscoIPPhoneDirectory")
    _xml_text(root, "Title", "Company Directory")
    _xml_text(root, "Prompt", "Extensions grouped by location")
    locations = (
        Location.objects.filter(is_active=True)
        .prefetch_related("extensions")
        .order_by("name")
    )
    for location in locations:
        active_extensions = [
            extension
            for extension in location.extensions.all()
            if extension.is_active
        ]
        for extension in sorted(active_extensions, key=lambda item: item.number):
            entry = ET.SubElement(root, "DirectoryEntry")
            _xml_text(entry, "Name", f"{location.name} - {extension.display_name}")
            _xml_text(entry, "Telephone", extension.number)
            _xml_text(entry, "Location", location.name)
    return _xml_string(root)


def _firmware_files(phones: list[Phone]) -> list[dict[str, str]]:
    phone_rows = [
        f"- {phone.sep_identifier} ({phone.model}): {phone.firmware_load_name or 'MISSING firmware_load_name'}"
        for phone in phones
    ]
    models = sorted({phone.model for phone in phones})
    model_rows = [
        f"- {model}: confirm the matching Cisco SIP load files are staged in the TFTP root."
        for model in models
    ]
    checklist = "\n".join(
        [
            "Cisco firmware/load checklist",
            "",
            "This export does not bundle Cisco firmware.",
            "Before provisioning phones:",
            "- Obtain licensed Cisco SIP firmware from the authorized source.",
            "- Stage the referenced load files in the TFTP root.",
            "- Confirm each phone has the expected loadInformation value.",
            "- Reboot or reset phones only after XML and firmware files are present.",
            "",
            "Phones:",
            *(phone_rows or ["- No active Cisco phones in this location."]),
            "",
            "Models:",
            *(model_rows or ["- No Cisco model loads required."]),
            "",
        ]
    )
    placeholder = "\n".join(
        [
            "No firmware binaries are included in this export.",
            "Place Cisco SIP load files beside the generated SEP<MAC>.cnf.xml files when firmware updates are required.",
            "",
        ]
    )
    return [
        _tftp_file(CISCO_FIRMWARE_CHECKLIST_FILENAME, checklist, "text/plain"),
        _tftp_file(CISCO_FIRMWARE_PLACEHOLDER_FILENAME, placeholder, "text/plain"),
    ]


def _active_line_appearances(phone: Phone):
    return [
        appearance
        for appearance in phone.line_appearances.all()
        if appearance.extension.is_active
    ]


def _tftp_file(path: str, content: str, content_type: str) -> dict[str, str]:
    return {
        "path": path,
        "content_type": content_type,
        "content": content,
    }


def _xml_text(parent: ET.Element, tag: str, text: Any) -> ET.Element:
    child = ET.SubElement(parent, tag)
    child.text = "" if text is None else str(text)
    return child


def _xml_string(root: ET.Element) -> str:
    tree = ET.ElementTree(root)
    ET.indent(tree, space="  ")
    output = BytesIO()
    tree.write(output, encoding="utf-8", xml_declaration=True, short_empty_elements=True)
    return output.getvalue().decode("utf-8")


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
                "prompt",
                "business_hours_destination",
                "after_hours_destination",
                "timeout_destination",
                "invalid_destination",
            )
            .prefetch_related("menu_options__destination")
            .order_by("name")
        ],
        "audio_prompts": [
            _audio_prompt_config(prompt)
            for prompt in location.audio_prompts.order_by("name")
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
    prompt = _audio_prompt_ref(ivr.prompt) if ivr.prompt_id else None
    return {
        "name": ivr.name,
        "prompt_name": prompt["playback_name"] if prompt else ivr.prompt_name,
        "prompt": prompt,
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


def _audio_prompt_config(prompt: AudioPrompt) -> dict[str, Any]:
    return {
        "id": prompt.id,
        "name": prompt.name,
        "original_filename": prompt.original_filename,
        "source_format": prompt.source_format,
        "converted_format": prompt.converted_format,
        "sample_rate_hz": prompt.sample_rate_hz,
        "channels": prompt.channels,
        "converted_file": prompt.converted_file.name,
        "asterisk_path": prompt.asterisk_path,
        "playback_name": prompt.playback_name,
    }


def _audio_prompt_ref(prompt: AudioPrompt) -> dict[str, Any]:
    return {
        "id": prompt.id,
        "name": prompt.name,
        "asterisk_path": prompt.asterisk_path,
        "playback_name": prompt.playback_name,
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


def _render_pjsip_conf(location: Location) -> str:
    lines = _header(location, "PJSIP TCP endpoints and provider trunks")
    lines.extend(
        [
            "[global]",
            "type=global",
            f"user_agent=PBXConfigPortal-{_slug(location.slug)}",
            "",
            "[transport-tcp]",
            "type=transport",
            "protocol=tcp",
            f"bind={location.sip_bind_ip}:{location.sip_port}",
            f"local_net={location.lan_subnet}",
            "",
        ]
    )

    for extension in location.extensions.filter(is_active=True).order_by("number"):
        endpoint = extension.number
        auth = f"auth-{extension.number}"
        aor = f"aor-{extension.number}"
        caller_id_name = _quote(extension.caller_id_name or extension.display_name)
        username = extension.sip_username or extension.number

        lines.extend(
            [
                f"[{endpoint}]",
                "type=endpoint",
                "transport=transport-tcp",
                "context=from-pjsip",
                "disallow=all",
                "allow=ulaw,alaw",
                f"auth={auth}",
                f"aors={aor}",
                f"callerid=\"{caller_id_name}\" <{extension.number}>",
                "direct_media=no",
                "force_rport=yes",
                "rewrite_contact=yes",
                "deny=0.0.0.0/0.0.0.0",
                f"permit={_asterisk_acl(location.lan_subnet)}",
            ]
        )
        if extension.voicemail_enabled:
            lines.append(f"mailboxes={extension.number}@default")
        lines.extend(
            [
                "",
                f"[{auth}]",
                "type=auth",
                "auth_type=userpass",
                f"username={username}",
                f"password={extension.sip_password}",
                "",
                f"[{aor}]",
                "type=aor",
                "max_contacts=5",
                "remove_existing=yes",
                "",
            ]
        )

    for trunk in _active_sip_trunks(location):
        section = _trunk_section_name(trunk)
        auth = f"auth-{section}"
        aor = f"aor-{section}"
        identify = f"identify-{section}"
        lines.extend(
            [
                f"[{section}]",
                "type=endpoint",
                "transport=transport-tcp",
                "context=inbound",
                "disallow=all",
                "allow=ulaw,alaw",
                f"outbound_auth={auth}",
                f"aors={aor}",
                "direct_media=no",
                "from_domain=" + trunk.host,
                "",
                f"[{auth}]",
                "type=auth",
                "auth_type=userpass",
                f"username={trunk.username}",
                f"password={trunk.password}",
                "",
                f"[{aor}]",
                "type=aor",
                "max_contacts=1",
                f"contact=sip:{trunk.host}",
                "",
                f"[{identify}]",
                "type=identify",
                f"endpoint={section}",
                f"match={trunk.host}",
                "",
            ]
        )

    return _render_lines(lines)


def _render_iax_conf(location: Location) -> str:
    lines = _header(location, "IAX2 inter-location trunks over WARP")
    lines.extend(
        [
            "[general]",
            f"bindaddr={location.pbx_warp_ip}",
            f"bindport={location.iax_port}",
            "autokill=yes",
            "requirecalltoken=yes",
            "disallow=all",
            "allow=ulaw",
            "jitterbuffer=yes",
            "",
        ]
    )

    for remote_location in _remote_locations(location):
        peer = _iax_peer_name(remote_location)
        lines.extend(
            [
                f"[{peer}]",
                "type=friend",
                f"host={remote_location.pbx_warp_ip}",
                f"port={remote_location.iax_port}",
                f"username={location.slug}",
                f"secret={_iax_shared_secret(location, remote_location)}",
                "context=from-iax2",
                "trunk=yes",
                "qualify=yes",
                "deny=0.0.0.0/0.0.0.0",
                f"permit={remote_location.pbx_warp_ip}/255.255.255.255",
                "",
            ]
        )
    return _render_lines(lines)


def _render_extensions_conf(location: Location) -> str:
    inbound_config = build_inbound_config(location)
    route_choices = build_route_generation_choices(location)
    lines = _header(location, "Dialplan")
    lines.extend(
        [
            "[globals]",
            f"LOCAL_PBX={location.slug}",
            f"RECORDING_RETENTION_DAYS={location.recording_retention_days}",
            "",
            "[from-pjsip]",
            "include => emergency",
            "include => local-extensions",
            "include => remote-extensions",
            "include => feature-codes",
            "include => voicemail",
            "include => paging",
            "include => outbound",
            "",
            "[from-iax2]",
            "include => local-extensions",
            "include => inbound",
            "",
            "[local-extensions]",
        ]
    )
    for choice in route_choices["local_extensions"]:
        extension = location.extensions.get(number=choice["number"])
        lines.extend(
            [
                f"exten => {extension.number},1,NoOp(Local extension {extension.number} {extension.display_name})",
                f" same => n,Dial({choice['target']},30)",
            ]
        )
        if extension.voicemail_enabled:
            lines.append(f" same => n,VoiceMail({extension.number}@default,u)")
        lines.extend([" same => n,Hangup()", ""])

    lines.append("[remote-extensions]")
    for choice in route_choices["remote_extensions"]:
        lines.extend(
            [
                (
                    f"exten => {choice['number']},1,NoOp(Remote extension {choice['number']} "
                    f"owned by {choice['owner_location']})"
                ),
                f" same => n,Dial({choice['target']},30)",
                " same => n,Hangup()",
                "",
            ]
        )

    lines.append("[inbound]")
    for did in inbound_config["dids"]:
        lines.extend(
            [
                f"exten => {did['number']},1,NoOp(Inbound DID {did['number']})",
                f" same => n,{_destination_app(did['effective_destination'])}",
                "",
            ]
        )
    if inbound_config["default_destination"]:
        lines.extend(
            [
                "exten => s,1,NoOp(Default inbound destination)",
                f" same => n,{_destination_app(inbound_config['default_destination'])}",
                "",
            ]
        )

    lines.append("[outbound]")
    for route in _active_outbound_routes(location, emergency=False):
        _append_route_lines(lines, route, "Outbound")

    lines.append("[emergency]")
    for block in route_choices["emergency_blocks"]:
        for pattern in block["patterns"]:
            lines.extend(
                [
                    f"exten => {pattern}/{block['extension']},1,NoOp(Emergency calling disabled for extension {block['extension']})",
                    " same => n,Playback(ss-noservice)",
                    " same => n,Hangup(21)",
                    "",
                ]
            )
    for route in _active_outbound_routes(location, emergency=True):
        _append_route_lines(lines, route, "Emergency")

    lines.extend(["[voicemail]", "exten => *97,1,VoiceMailMain(${CALLERID(num)}@default)", " same => n,Hangup()"])
    lines.extend(["exten => *98,1,VoiceMailMain(@default)", " same => n,Hangup()", ""])

    lines.append("[paging]")
    for paging_group in inbound_config["paging_groups"]:
        members = "&".join(f"PJSIP/{number}" for number in paging_group["members"])
        if not members:
            members = "Local/s@invalid"
        lines.extend(
            [
                f"exten => {paging_group['page_code']},1,NoOp(Page {paging_group['name']})",
                f" same => n,Page({members})",
                " same => n,Hangup()",
                "",
            ]
        )

    lines.append("[feature-codes]")
    for feature_code in inbound_config["feature_codes"]:
        _append_feature_code_lines(lines, feature_code)

    _append_destination_contexts(lines, inbound_config)
    return _render_lines(lines)


def _render_voicemail_conf(location: Location) -> str:
    smtp_settings = build_smtp_settings(location)
    lines = _header(location, "Voicemail")
    lines.extend(
        [
            "[general]",
            "format=wav49|gsm|wav",
            "attach=yes",
            "maxmsg=100",
            f"serveremail={smtp_settings['from_email'] if smtp_settings else ''}",
            "",
            "[default]",
        ]
    )
    for extension in location.extensions.filter(is_active=True, voicemail_enabled=True).order_by("number"):
        email = extension.email if smtp_settings and extension.email else ""
        pin = extension.voicemail_pin or "0000"
        lines.append(f"{extension.number} => {pin},{extension.display_name},{email},,attach=yes")
    return _render_lines(lines)


def _render_queues_conf(location: Location) -> str:
    inbound_config = build_inbound_config(location)
    lines = _header(location, "Queues")
    lines.extend(["[general]", "persistentmembers=yes", "autofill=yes", ""])
    for queue in inbound_config["queues"]:
        queue_name = _queue_name(queue["name"])
        lines.extend(
            [
                f"[{queue_name}]",
                f"strategy={_queue_strategy(queue['strategy'])}",
                f"timeout={queue['timeout_seconds']}",
                f"retry={queue['retry_seconds']}",
                f"musicclass={queue['music_on_hold'] or 'default'}",
                "setinterfacevar=yes",
            ]
        )
        for member in queue["members"]:
            lines.append(f"member => PJSIP/{member['extension']},{member['penalty']},{member['extension']}")
        lines.append("")
    return _render_lines(lines)


def _render_manager_conf(location: Location) -> str:
    username = location.ami_username or f"ami-{location.slug}"
    lines = _header(location, "Asterisk Manager Interface")
    lines.extend(
        [
            "[general]",
            "enabled=yes",
            f"port={location.ami_port}",
            f"bindaddr={location.ami_host}",
            "webenabled=no",
            "",
            f"[{username}]",
            f"secret={location.ami_secret}",
            "read=system,call,log,verbose,command,agent,user,config,dtmf,reporting,cdr,dialplan",
            "write=system,call,agent,user,config,command,reporting,originate",
            "permit=127.0.0.1/255.255.255.255",
        ]
    )
    return _render_lines(lines)


def _render_features_conf(location: Location) -> str:
    lines = _header(location, "Call features")
    lines.extend(
        [
            "[general]",
            "parkext => 700",
            "parkpos => 701-720",
            "context => parkedcalls",
            "",
            "[featuremap]",
            "blindxfer => #1",
            "atxfer => *2",
            "automon => *1",
        ]
    )
    return _render_lines(lines)


def _render_musiconhold_conf(location: Location) -> str:
    classes = {
        queue.music_on_hold or "default"
        for queue in location.queues.filter(is_active=True).order_by("music_on_hold", "name")
    }
    classes.add("default")
    lines = _header(location, "Music on hold")
    for moh_class in sorted(classes):
        lines.extend(
            [
                f"[{_slug(moh_class)}]",
                "mode=files",
                f"directory=/var/lib/asterisk/moh/{_slug(moh_class)}",
                "",
            ]
        )
    return _render_lines(lines)


def _render_cdr_conf(location: Location) -> str:
    lines = _header(location, "Call detail records")
    lines.extend(["[general]", "enable=yes", "unanswered=yes", "congestion=yes"])
    return _render_lines(lines)


def _render_cel_conf(location: Location) -> str:
    lines = _header(location, "Channel event logging")
    lines.extend(
        [
            "[general]",
            "enable=yes",
            "apps=dial,park,queue,voicemail",
            "events=CHAN_START,CHAN_END,ANSWER,HANGUP,BRIDGE_ENTER,BRIDGE_EXIT,APP_START,APP_END",
        ]
    )
    return _render_lines(lines)


def _render_recording_conf(location: Location) -> str:
    lines = _header(location, "Recording policy references")
    lines.extend(
        [
            "[general]",
            "recording_root=/var/spool/asterisk/monitor",
            f"retention_days={location.recording_retention_days}",
            "",
        ]
    )
    for extension in location.extensions.filter(is_active=True).order_by("number"):
        lines.extend([f"[extension-{extension.number}]", f"policy={extension.recording_policy}", ""])
    for queue in location.queues.filter(is_active=True).order_by("name"):
        lines.extend([f"[queue-{_queue_name(queue.name)}]", f"policy={queue.recording_policy}", ""])
    for route in _active_outbound_routes(location):
        lines.extend([f"[route-{_slug(route.name)}]", f"policy={route.recording_policy}", ""])
    return _render_lines(lines)


def _render_retention_conf(location: Location) -> str:
    lines = _header(location, "Recording retention hook")
    lines.extend(
        [
            "[recordings]",
            f"retention_days={location.recording_retention_days}",
            "hook=/usr/local/sbin/pbx-recording-retention",
            "spool=/var/spool/asterisk/monitor",
        ]
    )
    return _render_lines(lines)


def _append_route_lines(lines: list[str], route: OutboundRoute, label: str) -> None:
    pattern = _asterisk_pattern(route.dial_pattern)
    lines.extend([f"exten => {pattern},1,NoOp({label} route {route.name})"])
    caller_id = select_route_caller_id(route)
    if caller_id:
        lines.append(f" same => n,Set(CALLERID(num)={caller_id})")
    route_trunks = [route_trunk for route_trunk in route.route_trunks.all() if route_trunk.trunk.is_active]
    if not route_trunks:
        lines.extend([" same => n,Playback(all-circuits-busy-now)", " same => n,Hangup(34)", ""])
        return
    for route_trunk in route_trunks:
        lines.append(f" same => n,Dial({_dial_target(route_trunk.trunk)},60)")
    lines.extend([" same => n,Hangup()", ""])


def _append_feature_code_lines(lines: list[str], feature_code: dict[str, Any]) -> None:
    code = feature_code["code"]
    feature_type = feature_code["feature_type"]
    lines.append(f"exten => {code},1,NoOp(Feature code {code} {feature_code['name']})")
    if feature_type == FeatureCode.FeatureType.VOICEMAIL_MAIN:
        lines.append(" same => n,VoiceMailMain(@default)")
    elif feature_type == FeatureCode.FeatureType.VOICEMAIL_DIRECT:
        lines.append(f" same => n,VoiceMail(${{EXTEN:{len(code)}}}@default,u)")
    elif feature_type == FeatureCode.FeatureType.CALL_PICKUP:
        lines.append(" same => n,Pickup()")
    elif feature_type == FeatureCode.FeatureType.DIRECTED_PICKUP:
        lines.append(f" same => n,Pickup(${{EXTEN:{len(code)}}})")
    elif feature_type == FeatureCode.FeatureType.PARK:
        lines.append(" same => n,Park()")
    elif feature_type == FeatureCode.FeatureType.PAGING_PREFIX:
        lines.append(f" same => n,Goto(paging,${{EXTEN:{len(code)}}},1)")
    elif feature_code["destination"]:
        lines.append(f" same => n,{_destination_app(feature_code['destination'])}")
    else:
        lines.append(" same => n,Playback(feature-not-available)")
    lines.extend([" same => n,Hangup()", ""])


def _append_destination_contexts(lines: list[str], inbound_config: dict[str, Any]) -> None:
    lines.append("[ivrs]")
    for ivr in inbound_config["ivrs"]:
        ivr_name = _slug(ivr["name"])
        prompt = ivr["prompt_name"] or "silence/1"
        lines.extend(
            [
                f"exten => {ivr_name},1,NoOp(IVR {ivr['name']})",
                f" same => n,Background({prompt})",
                f" same => n,WaitExten({ivr['timeout_seconds']})",
            ]
        )
        if ivr["timeout_destination"]:
            lines.append(f" same => n,{_destination_app(ivr['timeout_destination'])}")
        lines.append("")
        for option in ivr["menu_options"]:
            lines.extend(
                [
                    f"exten => {option['digit']},1,NoOp(IVR option {option['digit']} {option['label']})",
                    f" same => n,{_destination_app(option['destination'])}",
                    "",
                ]
            )

    lines.append("[ring-groups]")
    for ring_group in inbound_config["ring_groups"]:
        members = "&".join(f"PJSIP/{member['extension']}" for member in ring_group["members"])
        if not members:
            members = "Local/s@invalid"
        lines.extend(
            [
                f"exten => {_slug(ring_group['name'])},1,NoOp(Ring group {ring_group['name']})",
                f" same => n,Dial({members},{ring_group['timeout_seconds']})",
                " same => n,Hangup()",
                "",
            ]
        )

    lines.append("[queues]")
    for queue in inbound_config["queues"]:
        queue_name = _queue_name(queue["name"])
        lines.extend(
            [
                f"exten => {queue_name},1,NoOp(Queue {queue['name']})",
                f" same => n,Queue({queue_name},t,,,{queue['timeout_seconds']})",
            ]
        )
        if queue["overflow_destination"]:
            lines.append(f" same => n,{_destination_app(queue['overflow_destination'])}")
        lines.extend([" same => n,Hangup()", ""])


def _active_sip_trunks(location: Location) -> list[Trunk]:
    return list(
        location.trunks.select_related("provider")
        .filter(is_active=True, trunk_type=Trunk.TrunkType.SIP)
        .order_by("name")
    )


def _active_outbound_routes(location: Location, emergency: bool | None = None) -> list[OutboundRoute]:
    routes = (
        location.outbound_routes.prefetch_related("route_trunks__trunk__provider")
        .filter(is_active=True)
        .order_by("priority", "name")
    )
    if emergency is not None:
        routes = routes.filter(is_emergency_route=emergency)
    return list(routes)


def _remote_locations(location: Location) -> list[Location]:
    return list(Location.objects.filter(is_active=True).exclude(pk=location.pk).order_by("slug"))


def _iax_peer_name(location: Location) -> str:
    return _slug(location.slug)


def _iax_shared_secret(location: Location, remote_location: Location) -> str:
    pair = sorted(
        [
            f"{location.slug}:{location.agent_secret}",
            f"{remote_location.slug}:{remote_location.agent_secret}",
        ]
    )
    return hashlib.sha256("|".join(pair).encode("utf-8")).hexdigest()[:32]


def _trunk_section_name(trunk: Trunk) -> str:
    return f"trunk-{_slug(trunk.name)}"


def _queue_name(name: str) -> str:
    return _slug(name)


def _dial_target(trunk: Trunk) -> str:
    if trunk.trunk_type == Trunk.TrunkType.IAX2:
        return f"IAX2/{_trunk_section_name(trunk)}/${{EXTEN}}"
    return f"PJSIP/${{EXTEN}}@{_trunk_section_name(trunk)}"


def _destination_app(destination: dict[str, Any] | None) -> str:
    if not destination:
        return "Playback(invalid)"
    destination_type = destination["type"]
    target = destination.get("target")
    if destination_type == "extension":
        if "number" in destination:
            return f"Goto(local-extensions,{destination['number']},1)"
        if not target:
            return "Playback(invalid)"
        return f"Goto(local-extensions,{target['number']},1)"
    if not target:
        return "Playback(invalid)"
    if destination_type == "ivr":
        return f"Goto(ivrs,{_slug(target['name'])},1)"
    if destination_type == "ring_group":
        return f"Goto(ring-groups,{_slug(target['name'])},1)"
    if destination_type == "queue":
        return f"Goto(queues,{_queue_name(target['name'])},1)"
    return "Playback(invalid)"


def _asterisk_pattern(dial_pattern: str) -> str:
    if re.fullmatch(r"[0-9*#+]+", dial_pattern):
        return dial_pattern
    if dial_pattern.startswith("_"):
        return dial_pattern
    return f"_{dial_pattern}"


def _asterisk_acl(cidr: str) -> str:
    network = ipaddress.ip_network(cidr, strict=False)
    if network.version == 4:
        return f"{network.network_address}/{network.netmask}"
    return f"{network.network_address}/{network.prefixlen}"


def _queue_strategy(strategy: str) -> str:
    return {
        "ring_all": "ringall",
        "least_recent": "leastrecent",
        "fewest_calls": "fewestcalls",
        "round_robin": "rrmemory",
    }.get(strategy, "ringall")


def _header(location: Location, description: str) -> list[str]:
    return [
        f"; Generated by PBX Config Portal for {location.name}",
        f"; {description}",
        "",
    ]


def _render_lines(lines: list[str]) -> str:
    return "\n".join(lines).rstrip() + "\n"


def _slug(value: str) -> str:
    slug = re.sub(r"[^a-zA-Z0-9_-]+", "-", str(value).strip().lower()).strip("-")
    return slug or "default"


def _quote(value: str) -> str:
    return str(value).replace('"', '\\"')
