from __future__ import annotations

import os
from pathlib import Path
from typing import Any

from django.conf import settings


RESTRICTED_FILE_MODE = 0o600
RESTRICTED_DIR_MODE = 0o700


def apply_restricted_file_permissions(path: str | Path) -> None:
    _chmod(path, RESTRICTED_FILE_MODE)


def apply_restricted_directory_permissions(path: str | Path) -> None:
    _chmod(path, RESTRICTED_DIR_MODE)


def ensure_restricted_directory(path: str | Path) -> Path:
    directory = Path(path)
    directory.mkdir(parents=True, exist_ok=True)
    apply_restricted_directory_permissions(directory)
    return directory


def write_restricted_bytes(path: str | Path, content: bytes) -> None:
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    descriptor = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, RESTRICTED_FILE_MODE)
    with os.fdopen(descriptor, "wb") as destination:
        destination.write(content)
    apply_restricted_file_permissions(target)


def harden_local_storage_permissions(settings_obj: Any = settings) -> None:
    media_root = getattr(settings_obj, "MEDIA_ROOT", None)
    if media_root:
        media_path = Path(media_root)
        if media_path.exists():
            apply_restricted_directory_permissions(media_path)

    database = settings_obj.DATABASES.get("default", {})
    engine = database.get("ENGINE", "")
    database_name = database.get("NAME")
    if engine.endswith("sqlite3") and database_name and database_name != ":memory:":
        database_path = Path(database_name)
        if database_path.exists():
            apply_restricted_file_permissions(database_path)


def _chmod(path: str | Path, mode: int) -> None:
    try:
        Path(path).chmod(mode)
    except (NotImplementedError, OSError):
        return
