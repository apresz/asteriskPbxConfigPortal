from __future__ import annotations

from io import BytesIO
import hashlib
import json
from pathlib import PurePosixPath
import posixpath
from typing import Any
import zipfile

from .file_permissions import RESTRICTED_FILE_MODE


ZIP_TIMESTAMP = (1980, 1, 1, 0, 0, 0)
DEFAULT_ACTIVE_CONFIG_MARKER_PATH = "/etc/asterisk/pbx-active-config.json"
ASTERISK_CONFIG_ROOT = "/etc/asterisk"
ACTIVE_CONFIG_MARKER_FORMAT = "pbx-active-config/v1"
ACTIVE_CONFIG_MARKER_CONTENT_TYPE = "application/json"
CONFIG_PAYLOAD_CHECKSUM_TYPE = "config-payload-sha256"

ArchiveFile = tuple[str, bytes, str]


def active_config_marker_bundle_path(
    marker_path: str | PurePosixPath | None,
    *,
    asterisk_root: str = ASTERISK_CONFIG_ROOT,
) -> str:
    marker = _posix_path(marker_path or DEFAULT_ACTIVE_CONFIG_MARKER_PATH)
    root = _posix_path(asterisk_root)

    if marker.is_absolute():
        try:
            relative = marker.relative_to(root)
        except ValueError:
            relative = _safe_relative_path(PurePosixPath(*marker.parts[1:]))
            return f"active-config/{relative.as_posix()}"
        return f"asterisk/{_safe_relative_path(relative).as_posix()}"

    return f"active-config/{_safe_relative_path(marker).as_posix()}"


def active_config_marker_volume_mount(marker_path: str | None, bundle_path: str) -> str:
    marker = _posix_path(marker_path or DEFAULT_ACTIVE_CONFIG_MARKER_PATH)
    if not marker.is_absolute():
        raise ValueError("Active config marker path must be absolute for runtime volume mounting.")

    if bundle_path.startswith("asterisk/"):
        return "./asterisk:/etc/asterisk:ro"

    source_parent = posixpath.dirname(bundle_path) or "."
    target_parent = posixpath.dirname(marker.as_posix()) or "/"
    return f"./{source_parent}:{target_parent}:ro"


def build_active_config_marker(
    *,
    location: dict[str, Any],
    version_number: int,
    exported_at: Any,
    exported_by: str,
    checksum: str,
    marker_path: str,
    bundle_path: str,
    deployment: dict[str, Any],
) -> dict[str, Any]:
    checksum = str(checksum).strip().lower()
    if len(checksum) != 64 or any(char not in "0123456789abcdef" for char in checksum):
        raise ValueError("Active config marker checksum must be a SHA-256 hex digest.")

    timestamp = _timestamp(exported_at)
    return {
        "checksum": checksum,
        "checksum_type": CONFIG_PAYLOAD_CHECKSUM_TYPE,
        "config_version": {
            "number": int(version_number),
            "exported_at": timestamp,
            "exported_by": exported_by,
        },
        "deployment": {
            **deployment,
            "marker_path": marker_path,
            "bundle_path": bundle_path,
        },
        "exported_at": timestamp,
        "format": ACTIVE_CONFIG_MARKER_FORMAT,
        "location": dict(location),
        "timestamp": timestamp,
        "version": int(version_number),
        "version_number": int(version_number),
    }


def payload_files_checksum(files: list[ArchiveFile]) -> str:
    payload_manifest = [manifest_entry(path, content, content_type) for path, content, content_type in files]
    return sha256(json_bytes(payload_manifest))


def manifest_entry(path: str, content: bytes, content_type: str) -> dict[str, Any]:
    return {
        "path": path,
        "size": len(content),
        "sha256": sha256(content),
        "content_type": content_type,
    }


def sha256sums(files: list[ArchiveFile]) -> bytes:
    lines = [f"{sha256(content)}  {path}" for path, content, _content_type in files]
    return ("\n".join(lines) + "\n").encode("utf-8")


def json_bytes(payload: Any) -> bytes:
    return (json.dumps(payload, indent=2, sort_keys=True) + "\n").encode("utf-8")


def sha256(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()


def zip_archive(files: list[ArchiveFile]) -> bytes:
    output = BytesIO()
    with zipfile.ZipFile(output, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for path, content, _content_type in files:
            zip_info = zipfile.ZipInfo(path, date_time=ZIP_TIMESTAMP)
            zip_info.compress_type = zipfile.ZIP_DEFLATED
            zip_info.external_attr = RESTRICTED_FILE_MODE << 16
            archive.writestr(zip_info, content)
    return output.getvalue()


def _posix_path(path: str | PurePosixPath) -> PurePosixPath:
    return PurePosixPath(str(path).strip() or DEFAULT_ACTIVE_CONFIG_MARKER_PATH)


def _safe_relative_path(path: PurePosixPath) -> PurePosixPath:
    parts = tuple(part for part in path.parts if part not in {"", "."})
    if not parts or any(part == ".." for part in parts):
        raise ValueError(f"Unsafe active config marker path: {path}")
    return PurePosixPath(*parts)


def _timestamp(value: Any) -> str:
    if hasattr(value, "isoformat"):
        return value.isoformat()
    return str(value)
