from __future__ import annotations

import os
from pathlib import Path


RESTRICTED_FILE_MODE = 0o600
RESTRICTED_EXECUTABLE_FILE_MODE = 0o700
RESTRICTED_DIRECTORY_MODE = 0o700


def platform_supports_restrictive_permissions() -> bool:
    return os.name == "posix"


def restrict_file_permissions(path: str | Path, mode: int = RESTRICTED_FILE_MODE) -> None:
    if not platform_supports_restrictive_permissions():
        return
    target = Path(path)
    if target.is_symlink():
        return
    try:
        target.chmod(mode)
    except FileNotFoundError:
        return


def restrict_directory_permissions(path: str | Path, mode: int = RESTRICTED_DIRECTORY_MODE) -> None:
    if not platform_supports_restrictive_permissions():
        return
    target = Path(path)
    if target.is_symlink():
        return
    try:
        target.chmod(mode)
    except FileNotFoundError:
        return


def ensure_restricted_directory(path: str | Path) -> Path:
    target = Path(path)
    target.mkdir(parents=True, exist_ok=True)
    restrict_directory_permissions(target)
    return target


def write_restricted_bytes(path: str | Path, content: bytes, *, mode: int = RESTRICTED_FILE_MODE) -> Path:
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    if platform_supports_restrictive_permissions():
        flags = os.O_WRONLY | os.O_CREAT | os.O_TRUNC
        fd = os.open(target, flags, mode)
        with os.fdopen(fd, "wb") as handle:
            handle.write(content)
        restrict_file_permissions(target, mode)
    else:
        target.write_bytes(content)
    return target


def write_restricted_text(
    path: str | Path,
    content: str,
    *,
    encoding: str = "utf-8",
    mode: int = RESTRICTED_FILE_MODE,
) -> Path:
    return write_restricted_bytes(path, content.encode(encoding), mode=mode)


def restrict_tree_permissions(path: str | Path) -> None:
    if not platform_supports_restrictive_permissions():
        return
    root = Path(path)
    if not root.exists() or root.is_symlink():
        return
    if root.is_file():
        restrict_file_permissions(root)
        return
    restrict_directory_permissions(root)
    for child in root.rglob("*"):
        if child.is_symlink():
            continue
        if child.is_dir():
            restrict_directory_permissions(child)
        elif child.is_file():
            restrict_file_permissions(child)


def harden_runtime_storage_permissions(settings) -> None:
    media_root = getattr(settings, "MEDIA_ROOT", None)
    if media_root:
        restrict_directory_permissions(media_root)

    for database in getattr(settings, "DATABASES", {}).values():
        engine = str(database.get("ENGINE", ""))
        name = database.get("NAME")
        if "sqlite" not in engine or not name or str(name) == ":memory:":
            continue
        restrict_file_permissions(name)
