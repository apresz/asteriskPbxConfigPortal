from __future__ import annotations

from dataclasses import dataclass
from io import BytesIO, StringIO
import hashlib
import json
import mimetypes
import os
from pathlib import Path
import shutil
import subprocess
from typing import Any
import zipfile

from django.conf import settings
from django.core.management import call_command
from django.core.serializers.json import DjangoJSONEncoder
from django.db import connection
from django.utils import timezone

from .config_export import build_location_config
from .file_permissions import RESTRICTED_FILE_MODE
from .models import AdminBackup, AuditLog, ConfigVersion, Location


BACKUP_FORMAT = "pbx-admin-backup/v1"
ZIP_TIMESTAMP = (1980, 1, 1, 0, 0, 0)
ADMIN_BACKUP_TABLE = "core_adminbackup"
DATABASE_DUMP_EXCLUDES = ["contenttypes", "auth.permission", "core.adminbackup"]
CONFIG_MODEL_LABELS = [
    "core.Location",
    "core.Provider",
    "core.Trunk",
    "core.Extension",
    "core.Phone",
    "core.PhoneLineAppearance",
    "core.PhoneSpeedDial",
    "core.InboundDestination",
    "core.DID",
    "core.IVR",
    "core.IVRMenuOption",
    "core.RingGroup",
    "core.RingGroupMember",
    "core.CallQueue",
    "core.QueueMember",
    "core.PagingGroup",
    "core.PagingGroupMember",
    "core.FeatureCode",
    "core.OutboundRoute",
    "core.OutboundRouteTrunk",
    "core.AudioPrompt",
    "core.PortalUserProfile",
    "core.ServiceIdentity",
    "core.APIKey",
]


@dataclass(frozen=True)
class BackupArchive:
    archive_bytes: bytes
    checksum: str
    filename: str
    manifest: dict[str, Any]
    database_dump_method: str


def create_admin_backup(*, generated_by) -> AdminBackup:
    generated_at = timezone.now()
    archive = build_admin_backup_archive(generated_at=generated_at, generated_by=generated_by)
    return AdminBackup.objects.create(
        generated_by=generated_by if getattr(generated_by, "is_authenticated", False) else None,
        generated_at=generated_at,
        filename=archive.filename,
        checksum=archive.checksum,
        archive=archive.archive_bytes,
        archive_size_bytes=len(archive.archive_bytes),
        manifest=archive.manifest,
        database_dump_method=archive.database_dump_method,
    )


def build_admin_backup_archive(*, generated_at, generated_by=None) -> BackupArchive:
    generated_by_username = (
        generated_by.get_username() if getattr(generated_by, "is_authenticated", False) else "system"
    )
    timestamp = generated_at.strftime("%Y%m%dT%H%M%SZ")
    filename = f"pbx-admin-backup-{timestamp}.zip"

    database_path, database_content, database_info = _database_dump()
    media_files, media_manifest = _media_archive_files()
    export_metadata = _export_metadata()
    config_files, config_manifest = _config_archive_files()
    audit_logs = _audit_logs()
    backup_metadata = _admin_backup_metadata()

    files: list[tuple[str, bytes, str]] = [
        (database_path, database_content, database_info["content_type"]),
        ("media/manifest.json", _json_bytes(media_manifest), "application/json"),
        *media_files,
        ("exports/config_versions.json", _json_bytes(export_metadata), "application/json"),
        *config_files,
        ("audit/audit_logs.json", _json_bytes(audit_logs), "application/json"),
        ("backups/admin_backups.json", _json_bytes(backup_metadata), "application/json"),
        ("README.txt", _readme().encode("utf-8"), "text/plain"),
    ]

    manifest_files = [_manifest_entry(path, content, content_type) for path, content, content_type in files]
    manifest = {
        "format": BACKUP_FORMAT,
        "generated_at": generated_at.isoformat(),
        "generated_by": generated_by_username,
        "database": database_info,
        "contents": {
            "database_dump": database_path,
            "media": {
                "manifest": "media/manifest.json",
                "file_count": media_manifest["file_count"],
            },
            "export_metadata": "exports/config_versions.json",
            "configuration_data": config_manifest,
            "audit_logs": "audit/audit_logs.json",
            "backup_metadata": "backups/admin_backups.json",
        },
        "off_host_storage": (
            "This archive is designed for off-host storage. It contains PBX configuration, "
            "audit history, uploaded media, and secrets needed for recovery."
        ),
        "files": manifest_files,
    }
    files.append(("manifest.json", _json_bytes(manifest), "application/json"))
    files.append(("SHA256SUMS", _sha256sums(files), "text/plain"))

    archive_bytes = _zip_archive(files)
    return BackupArchive(
        archive_bytes=archive_bytes,
        checksum=_sha256(archive_bytes),
        filename=filename,
        manifest=manifest,
        database_dump_method=database_info["method"],
    )


def _database_dump() -> tuple[str, bytes, dict[str, Any]]:
    if connection.vendor == "postgresql" and shutil.which("pg_dump"):
        try:
            content = _postgres_dump()
            return (
                "database/postgresql-dump.sql",
                content,
                {
                    "path": "database/postgresql-dump.sql",
                    "method": "pg_dump",
                    "engine": connection.settings_dict.get("ENGINE", ""),
                    "vendor": connection.vendor,
                    "content_type": "application/sql",
                    "notes": [
                        f"{ADMIN_BACKUP_TABLE} table data is excluded to avoid nesting generated backup archives."
                    ],
                },
            )
        except (OSError, subprocess.SubprocessError) as exc:
            return _django_fixture_dump(note=f"pg_dump failed; used Django fixture fallback: {_safe_error(exc)}")

    note = "Non-PostgreSQL database or pg_dump unavailable; used Django fixture fallback."
    return _django_fixture_dump(note=note)


def _postgres_dump() -> bytes:
    db_settings = connection.settings_dict
    args = [
        "pg_dump",
        "--format=plain",
        "--no-owner",
        "--no-privileges",
        f"--exclude-table-data={ADMIN_BACKUP_TABLE}",
    ]
    if db_settings.get("HOST"):
        args.extend(["--host", str(db_settings["HOST"])])
    if db_settings.get("PORT"):
        args.extend(["--port", str(db_settings["PORT"])])
    if db_settings.get("USER"):
        args.extend(["--username", str(db_settings["USER"])])
    args.append(str(db_settings["NAME"]))

    env = os.environ.copy()
    if db_settings.get("PASSWORD"):
        env["PGPASSWORD"] = str(db_settings["PASSWORD"])

    completed = subprocess.run(
        args,
        check=True,
        capture_output=True,
        env=env,
        timeout=getattr(settings, "ADMIN_BACKUP_PG_DUMP_TIMEOUT_SECONDS", 60),
    )
    return completed.stdout


def _django_fixture_dump(*, note: str) -> tuple[str, bytes, dict[str, Any]]:
    content = _dumpdata(exclude=DATABASE_DUMP_EXCLUDES).encode("utf-8")
    return (
        "database/django-dumpdata.json",
        content,
        {
            "path": "database/django-dumpdata.json",
            "method": "django_dumpdata",
            "engine": connection.settings_dict.get("ENGINE", ""),
            "vendor": connection.vendor,
            "content_type": "application/json",
            "notes": [
                note,
                "core.AdminBackup is excluded to avoid nesting generated backup archives.",
            ],
        },
    )


def _media_archive_files() -> tuple[list[tuple[str, bytes, str]], dict[str, Any]]:
    media_root = Path(settings.MEDIA_ROOT)
    files: list[tuple[str, bytes, str]] = []
    entries: list[dict[str, Any]] = []
    if not media_root.exists():
        return files, {"root": str(media_root), "file_count": 0, "files": []}

    media_root_resolved = media_root.resolve()
    for path in sorted(candidate for candidate in media_root.rglob("*") if candidate.is_file()):
        resolved = path.resolve()
        try:
            relative = resolved.relative_to(media_root_resolved)
        except ValueError:
            continue
        archive_path = f"media/files/{relative.as_posix()}"
        content = path.read_bytes()
        content_type = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
        files.append((archive_path, content, content_type))
        entries.append(
            {
                "path": relative.as_posix(),
                "archive_path": archive_path,
                "size_bytes": len(content),
                "sha256": _sha256(content),
                "content_type": content_type,
            }
        )

    return files, {"root": str(media_root), "file_count": len(entries), "files": entries}


def _export_metadata() -> list[dict[str, Any]]:
    versions = ConfigVersion.objects.select_related("location", "exported_by", "deployed_by", "rollback_of")
    return [
        {
            "id": version.id,
            "location_id": version.location_id,
            "location_slug": version.location.slug,
            "version_number": version.version_number,
            "exported_at": version.exported_at,
            "exported_by": _user_ref(version.exported_by),
            "checksum": version.checksum,
            "warnings": version.warnings,
            "emergency_status": version.emergency_status,
            "file_manifest": version.file_manifest,
            "deployment_snapshot": version.deployment_snapshot,
            "archive_size_bytes": version.archive_size_bytes,
            "deployment_status": version.deployment_status,
            "deployed_at": version.deployed_at,
            "deployed_by": _user_ref(version.deployed_by),
            "rollback_of_id": version.rollback_of_id,
        }
        for version in versions.order_by("location__slug", "version_number")
    ]


def _config_archive_files() -> tuple[list[tuple[str, bytes, str]], dict[str, Any]]:
    files: list[tuple[str, bytes, str]] = [
        (
            "config/portal-config-data.json",
            _dumpdata(*CONFIG_MODEL_LABELS).encode("utf-8"),
            "application/json",
        )
    ]
    locations = Location.objects.order_by("slug")
    generated_locations: list[dict[str, Any]] = []
    errors: list[dict[str, Any]] = []

    for location in locations:
        archive_path = f"config/locations/{location.slug}.json"
        try:
            config = build_location_config(location, require_emergency=False)
        except Exception as exc:  # pragma: no cover - defensive backup resilience
            errors.append({"location_id": location.id, "location_slug": location.slug, "error": _safe_error(exc)})
            continue
        files.append((archive_path, _json_bytes(config), "application/json"))
        generated_locations.append(
            {
                "location_id": location.id,
                "location_slug": location.slug,
                "archive_path": archive_path,
            }
        )

    manifest = {
        "model_dump": "config/portal-config-data.json",
        "location_configs": generated_locations,
        "errors": errors,
    }
    files.append(("config/manifest.json", _json_bytes(manifest), "application/json"))
    return files, manifest


def _audit_logs() -> list[dict[str, Any]]:
    logs = AuditLog.objects.select_related("actor").order_by("timestamp", "id")
    return [
        {
            "id": log.id,
            "timestamp": log.timestamp,
            "actor": _user_ref(log.actor),
            "action": log.action,
            "target": log.target,
            "outcome": log.outcome,
            "details": log.details,
        }
        for log in logs
    ]


def _admin_backup_metadata() -> list[dict[str, Any]]:
    backups = AdminBackup.objects.select_related("generated_by").order_by("generated_at", "id")
    return [
        {
            "id": backup.id,
            "generated_at": backup.generated_at,
            "generated_by": _user_ref(backup.generated_by),
            "filename": backup.filename,
            "checksum": backup.checksum,
            "archive_size_bytes": backup.archive_size_bytes,
            "database_dump_method": backup.database_dump_method,
        }
        for backup in backups
    ]


def _dumpdata(*labels: str, exclude: list[str] | None = None) -> str:
    output = StringIO()
    call_command(
        "dumpdata",
        *labels,
        natural_foreign=True,
        natural_primary=True,
        indent=2,
        exclude=exclude or [],
        stdout=output,
        verbosity=0,
    )
    return output.getvalue()


def _readme() -> str:
    return "\n".join(
        [
            "PBX Config Portal admin backup",
            "",
            "This archive is suitable for off-host storage.",
            "It contains operational PBX configuration, uploaded media/audio, export metadata, audit logs, and database data.",
            "Treat it as sensitive because configuration records can include plaintext telecom credentials and deployment secrets.",
            "Store retained copies on encrypted storage outside the application host.",
            "",
        ]
    )


def _user_ref(user) -> dict[str, Any] | None:
    if user is None:
        return None
    return {
        "id": user.id,
        "username": user.get_username(),
        "email": user.email,
    }


def _manifest_entry(path: str, content: bytes, content_type: str) -> dict[str, Any]:
    return {
        "path": path,
        "size_bytes": len(content),
        "sha256": _sha256(content),
        "content_type": content_type,
    }


def _zip_archive(files: list[tuple[str, bytes, str]]) -> bytes:
    buffer = BytesIO()
    with zipfile.ZipFile(buffer, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for path, content, _content_type in files:
            zip_info = zipfile.ZipInfo(path, ZIP_TIMESTAMP)
            zip_info.compress_type = zipfile.ZIP_DEFLATED
            zip_info.external_attr = RESTRICTED_FILE_MODE << 16
            archive.writestr(zip_info, content)
    return buffer.getvalue()


def _sha256sums(files: list[tuple[str, bytes, str]]) -> bytes:
    lines = [f"{_sha256(content)}  {path}" for path, content, _content_type in files]
    return ("\n".join(lines) + "\n").encode("utf-8")


def _json_bytes(payload: Any) -> bytes:
    return (json.dumps(payload, cls=DjangoJSONEncoder, indent=2, sort_keys=True) + "\n").encode("utf-8")


def _sha256(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()


def _safe_error(exc: BaseException) -> str:
    return str(exc).replace("\n", " ")[:500]
