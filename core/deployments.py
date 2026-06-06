from __future__ import annotations

from dataclasses import dataclass
from io import BytesIO
import hashlib
import json
from pathlib import Path, PurePosixPath
import posixpath
import shlex
import subprocess
import tempfile
from typing import Callable
from uuid import uuid4
import zipfile

from django.conf import settings
from django.db import transaction
from django.utils import timezone

from .audit import record_audit
from .config_archive import archive_member_mode
from .file_permissions import ensure_restricted_directory, write_restricted_bytes, write_restricted_text
from .models import AuditAction, AuditOutcome, ConfigVersion, DeploymentRecord, Location


MAX_CAPTURED_OUTPUT = 4000
RECORDING_RETENTION_SCRIPT_BUNDLE_PATH = "scripts/pbx-recording-retention"
RECORDING_RETENTION_SCRIPT_TARGET = "/usr/local/sbin/pbx-recording-retention"
DEFAULT_DEPLOYMENT_ALLOWED_ROOTS = (
    "/srv/pbx",
    "/opt/pbx",
    "/var/lib/pbx",
    "/etc/asterisk",
    "/srv/tftp",
    "/var/lib/tftpboot",
    "/tftpboot",
)


@dataclass(frozen=True)
class DeploymentCommandResult:
    command: str
    returncode: int = 0
    stdout: str = ""
    stderr: str = ""


@dataclass(frozen=True)
class ActiveConfigMarkerDeployment:
    bundle_path: str
    configured_path: str


class DeploymentError(Exception):
    pass


class DeploymentConfirmationError(DeploymentError):
    pass


class DeploymentConfigurationError(DeploymentError):
    pass


class DeploymentArchiveError(DeploymentError):
    pass


class DeploymentStepError(DeploymentError):
    def __init__(self, step: str, result: DeploymentCommandResult):
        self.step = step
        self.result = result
        message = result.stderr.strip() or result.stdout.strip() or f"{step} failed with exit code {result.returncode}."
        super().__init__(message)


class SSHDeploymentRunner:
    def __init__(
        self,
        *,
        host: str,
        port: int,
        username: str,
        key_path: Path,
        known_hosts_path: Path | None = None,
    ):
        self.host = host
        self.port = port
        self.username = username
        self.key_path = key_path
        self.known_hosts_path = known_hosts_path

    @classmethod
    def from_location(cls, location: Location, workspace: Path) -> "SSHDeploymentRunner":
        ensure_restricted_directory(workspace)
        key_path = workspace / "deployment_key"
        write_restricted_text(key_path, _trailing_newline(location.deployment_ssh_private_key))

        known_hosts_path = None
        if location.deployment_ssh_known_hosts.strip():
            known_hosts_path = workspace / "known_hosts"
            write_restricted_text(known_hosts_path, _trailing_newline(location.deployment_ssh_known_hosts))

        return cls(
            host=location.deployment_ssh_host,
            port=location.deployment_ssh_port,
            username=location.deployment_ssh_username,
            key_path=key_path,
            known_hosts_path=known_hosts_path,
        )

    def run(
        self,
        remote_command: str,
        *,
        input_bytes: bytes | None = None,
        command_label: str | None = None,
    ) -> DeploymentCommandResult:
        try:
            completed = subprocess.run(
                self._ssh_command(remote_command),
                input=input_bytes,
                capture_output=True,
                check=False,
            )
        except OSError as exc:
            return DeploymentCommandResult(
                command=command_label or remote_command,
                returncode=127,
                stderr=str(exc),
            )
        return DeploymentCommandResult(
            command=command_label or remote_command,
            returncode=completed.returncode,
            stdout=_decode(completed.stdout),
            stderr=_decode(completed.stderr),
        )

    def upload_bundle(self, bundle_dir: Path, staging_path: str) -> DeploymentCommandResult:
        bundle_roots = ["asterisk", "tftp"]
        if (bundle_dir / "active-config").exists():
            bundle_roots.append("active-config")
        try:
            tar_completed = subprocess.run(
                ["tar", "-C", str(bundle_dir), "-czf", "-", *bundle_roots],
                capture_output=True,
                check=False,
            )
        except OSError as exc:
            return DeploymentCommandResult(command="tar deploy bundle", returncode=127, stderr=str(exc))

        if tar_completed.returncode != 0:
            return DeploymentCommandResult(
                command="tar deploy bundle",
                returncode=tar_completed.returncode,
                stdout=_decode(tar_completed.stdout),
                stderr=_decode(tar_completed.stderr),
            )

        remote_command = "\n".join(
            [
                "set -eu",
                "umask 077",
                f"mkdir -p -m 700 {_q(staging_path)}",
                f"chmod 700 {_q(staging_path)}",
                f"tar -xzf - -C {_q(staging_path)}",
                f"chmod -R go-rwx {_q(staging_path)}",
            ]
        )
        return self.run(
            remote_command,
            input_bytes=tar_completed.stdout,
            command_label=f"upload bundle to {staging_path}",
        )

    def _ssh_command(self, remote_command: str) -> list[str]:
        host_key_options = []
        if self.known_hosts_path is not None:
            host_key_options = [
                "-o",
                "StrictHostKeyChecking=yes",
                "-o",
                f"UserKnownHostsFile={self.known_hosts_path}",
            ]
        else:
            host_key_options = [
                "-o",
                "StrictHostKeyChecking=no",
                "-o",
                "UserKnownHostsFile=/dev/null",
            ]
        return [
            "ssh",
            "-i",
            str(self.key_path),
            "-p",
            str(self.port),
            "-o",
            "IdentitiesOnly=yes",
            "-o",
            "BatchMode=yes",
            "-o",
            "ConnectTimeout=15",
            *host_key_options,
            f"{self.username}@{self.host}",
            remote_command,
        ]


def deploy_config_version(
    version: ConfigVersion,
    *,
    operator,
    reload_confirmed: bool,
    rollback: bool = False,
    runner=None,
) -> DeploymentRecord:
    location = Location.objects.get(pk=version.location_id)
    staging_path = _deployment_staging_path(location, version)
    record = DeploymentRecord.objects.create(
        location=location,
        config_version=version,
        rollback_source_version=version if rollback else None,
        operator=operator if getattr(operator, "is_authenticated", False) else None,
        target_host=location.deployment_ssh_host,
        target_port=location.deployment_ssh_port,
        target_username=location.deployment_ssh_username,
        staging_path=staging_path,
        asterisk_path=location.deployment_asterisk_path,
        tftp_path=location.deployment_tftp_path,
        reload_command=location.deployment_reload_command,
        action=DeploymentRecord.Action.ROLLBACK if rollback else DeploymentRecord.Action.DEPLOY,
        details={"steps": []},
    )

    try:
        if not reload_confirmed:
            raise DeploymentConfirmationError("Reload confirmation is required before deploying to Asterisk.")
        _validate_deployment_target(location)

        with tempfile.TemporaryDirectory() as temp_name:
            workspace = Path(temp_name)
            bundle_dir = workspace / "bundle"
            active_marker = deployment_bundle_active_marker(version)
            extract_deployment_bundle(version, bundle_dir)
            active_runner = runner or SSHDeploymentRunner.from_location(location, workspace)

            _run_step(record, "prepare_staging", lambda: active_runner.run(_prepare_staging_command(staging_path)))
            _run_step(record, "upload_bundle", lambda: active_runner.upload_bundle(bundle_dir, staging_path))
            _run_step(record, "verify_staging", lambda: active_runner.run(_verify_staging_command(staging_path)))
            _run_step(
                record,
                "swap_volumes",
                lambda: active_runner.run(
                    _swap_volumes_command(
                        staging_path=staging_path,
                        asterisk_path=location.deployment_asterisk_path,
                        tftp_path=location.deployment_tftp_path,
                        active_config_marker=active_marker,
                    )
                ),
            )
            reload_result = _run_step(
                record,
                "reload_asterisk",
                lambda: active_runner.run(location.deployment_reload_command),
            )

        _mark_deployment_success(record, version, location, operator, rollback=rollback, reload_result=reload_result)
        _record_deployment_audit(record, AuditOutcome.SUCCESS)
        return record
    except DeploymentError as exc:
        _mark_deployment_failed(record, location, exc)
        outcome = AuditOutcome.DENIED if isinstance(exc, DeploymentConfirmationError) else AuditOutcome.FAILURE
        _record_deployment_audit(record, outcome)
        raise
    except Exception as exc:
        deployment_error = DeploymentError(str(exc))
        _mark_deployment_failed(record, location, deployment_error)
        _record_deployment_audit(record, AuditOutcome.FAILURE)
        raise deployment_error from exc


def extract_deployment_bundle(version: ConfigVersion, target_dir: Path) -> list[str]:
    ensure_restricted_directory(target_dir)
    extracted_paths: list[str] = []
    with zipfile.ZipFile(BytesIO(bytes(version.archive))) as archive:
        _verify_archive_checksums(archive)
        active_marker = _archive_active_marker(archive)
        archive_names = set(archive.namelist())
        if active_marker and active_marker.bundle_path not in archive_names:
            raise DeploymentArchiveError(
                f"Export archive manifest references missing active marker: {active_marker.bundle_path}"
            )
        members = [
            info
            for info in archive.infolist()
            if info.filename.startswith("asterisk/")
            or info.filename.startswith("tftp/")
            or info.filename.startswith("scripts/")
            or (active_marker is not None and info.filename == active_marker.bundle_path)
        ]
        if not any(info.filename.startswith("asterisk/") and not info.is_dir() for info in members):
            raise DeploymentArchiveError("Export archive does not contain deployable asterisk files.")
        if not any(info.filename.startswith("tftp/") and not info.is_dir() for info in members):
            raise DeploymentArchiveError("Export archive does not contain deployable tftp files.")
        for info in members:
            destination = _safe_member_destination(target_dir, info.filename)
            if info.is_dir():
                ensure_restricted_directory(destination)
                continue
            ensure_restricted_directory(destination.parent)
            write_restricted_bytes(
                destination,
                archive.read(info.filename),
                mode=archive_member_mode(info.filename),
            )
            extracted_paths.append(info.filename)
    return extracted_paths


def deployment_bundle_active_marker(version: ConfigVersion) -> ActiveConfigMarkerDeployment | None:
    with zipfile.ZipFile(BytesIO(bytes(version.archive))) as archive:
        return _archive_active_marker(archive)


def _validate_deployment_target(location: Location) -> None:
    missing_fields = [
        label
        for label, value in (
            ("deployment SSH host", location.deployment_ssh_host),
            ("deployment SSH username", location.deployment_ssh_username),
            ("deployment SSH private key", location.deployment_ssh_private_key),
            ("deployment staging path", location.deployment_staging_path),
            ("deployment Asterisk path", location.deployment_asterisk_path),
            ("deployment TFTP path", location.deployment_tftp_path),
            ("deployment reload command", location.deployment_reload_command),
        )
        if not str(value or "").strip()
    ]
    if missing_fields:
        raise DeploymentConfigurationError(f"Missing deployment target settings: {', '.join(missing_fields)}.")

    normalized_paths = {
        "deployment staging path": _validate_remote_deployment_path(
            "deployment staging path",
            location.deployment_staging_path,
        ),
        "deployment Asterisk path": _validate_remote_deployment_path(
            "deployment Asterisk path",
            location.deployment_asterisk_path,
        ),
        "deployment TFTP path": _validate_remote_deployment_path(
            "deployment TFTP path",
            location.deployment_tftp_path,
        ),
    }
    active_paths = {
        normalized_paths["deployment Asterisk path"],
        normalized_paths["deployment TFTP path"],
    }
    if len(active_paths) != 2:
        raise DeploymentConfigurationError("Deployment Asterisk and TFTP paths must be distinct.")
    if normalized_paths["deployment staging path"] in active_paths:
        raise DeploymentConfigurationError("Deployment staging path must be distinct from active paths.")


def _run_step(
    record: DeploymentRecord,
    step: str,
    result_factory: Callable[[], DeploymentCommandResult],
) -> DeploymentCommandResult:
    result = result_factory()
    _append_step(record, step, result)
    if result.returncode != 0:
        raise DeploymentStepError(step, result)
    return result


def _mark_deployment_success(
    record: DeploymentRecord,
    version: ConfigVersion,
    location: Location,
    operator,
    *,
    rollback: bool,
    reload_result: DeploymentCommandResult,
) -> None:
    with transaction.atomic():
        version = ConfigVersion.objects.select_for_update().select_related("location").get(pk=version.pk)
        version.mark_deployed(operator, rolled_back=rollback)
        locked_location = Location.objects.select_for_update().get(pk=location.pk)
        locked_location.last_deployed_at = version.deployed_at
        locked_location.deployment_status = Location.DeploymentStatus.DEPLOYED
        locked_location.save(update_fields=["last_deployed_at", "deployment_status", "updated_at"])
        record.status = DeploymentRecord.Status.SUCCESS
        record.reload_result = DeploymentRecord.ReloadResult.SUCCESS
        record.reload_output = _captured_output(reload_result)
        record.completed_at = timezone.now()
        record.error_message = ""
        record.save(
            update_fields=[
                "status",
                "reload_result",
                "reload_output",
                "completed_at",
                "error_message",
                "updated_at",
            ]
        )


def _mark_deployment_failed(record: DeploymentRecord, location: Location, error: DeploymentError) -> None:
    if isinstance(error, DeploymentStepError) and error.step == "reload_asterisk":
        record.reload_result = DeploymentRecord.ReloadResult.FAILED
        record.reload_output = _captured_output(error.result)
    record.status = DeploymentRecord.Status.FAILED
    record.error_message = str(error)
    record.completed_at = timezone.now()
    record.save(
        update_fields=[
            "status",
            "reload_result",
            "reload_output",
            "error_message",
            "completed_at",
            "updated_at",
        ]
    )
    Location.objects.filter(pk=location.pk).update(
        deployment_status=Location.DeploymentStatus.FAILED,
        updated_at=timezone.now(),
    )


def _record_deployment_audit(record: DeploymentRecord, outcome: AuditOutcome) -> None:
    details = {
        "deployment_record_id": record.id,
        "location_id": record.location_id,
        "location_slug": record.location.slug,
        "config_version_id": record.config_version_id,
        "version_number": record.config_version.version_number,
        "checksum": record.config_version.checksum,
        "action": record.action,
        "status": record.status,
        "target_host": record.target_host,
        "target_port": record.target_port,
        "staging_path": record.staging_path,
        "asterisk_path": record.asterisk_path,
        "tftp_path": record.tftp_path,
        "reload_result": record.reload_result,
        "error_message": record.error_message,
    }
    if record.rollback_source_version_id:
        details["rollback_source_version_id"] = record.rollback_source_version_id
        details["rollback_source_version_number"] = record.rollback_source_version.version_number

    record_audit(
        actor=record.operator,
        action=AuditAction.DEPLOYMENT,
        target=f"locations/{record.location.slug}/config/v{record.config_version.version_number}",
        outcome=outcome,
        details=details,
    )


def _append_step(record: DeploymentRecord, step: str, result: DeploymentCommandResult) -> None:
    details = dict(record.details or {})
    steps = list(details.get("steps", []))
    steps.append(
        {
            "name": step,
            "command": result.command,
            "returncode": result.returncode,
            "stdout": result.stdout[:MAX_CAPTURED_OUTPUT],
            "stderr": result.stderr[:MAX_CAPTURED_OUTPUT],
        }
    )
    details["steps"] = steps
    record.details = details
    record.save(update_fields=["details", "updated_at"])


def _verify_archive_checksums(archive: zipfile.ZipFile) -> None:
    try:
        checksum_lines = archive.read("SHA256SUMS").decode("utf-8").splitlines()
    except KeyError as exc:
        raise DeploymentArchiveError("Export archive is missing SHA256SUMS.") from exc

    for line in checksum_lines:
        if not line.strip():
            continue
        try:
            expected_checksum, path = line.split(None, 1)
        except ValueError as exc:
            raise DeploymentArchiveError(f"Invalid checksum line: {line}") from exc
        path = path.strip()
        try:
            content = archive.read(path)
        except KeyError as exc:
            raise DeploymentArchiveError(f"Checksum references missing archive member: {path}") from exc
        actual_checksum = hashlib.sha256(content).hexdigest()
        if actual_checksum != expected_checksum:
            raise DeploymentArchiveError(f"Checksum mismatch for archive member: {path}")


def _safe_member_destination(target_dir: Path, member_name: str) -> Path:
    member_path = PurePosixPath(member_name)
    if member_path.is_absolute() or ".." in member_path.parts:
        raise DeploymentArchiveError(f"Unsafe archive member path: {member_name}")
    destination = (target_dir / Path(*member_path.parts)).resolve()
    try:
        destination.relative_to(target_dir.resolve())
    except ValueError as exc:
        raise DeploymentArchiveError(f"Unsafe archive member path: {member_name}") from exc
    return destination


def _deployment_staging_path(location: Location, version: ConfigVersion) -> str:
    base = location.deployment_staging_path.rstrip("/")
    return f"{base}/{location.slug}/v{version.version_number}-{version.checksum[:12]}-{uuid4().hex[:12]}"


def _prepare_staging_command(staging_path: str) -> str:
    return "\n".join(
        [
            "set -eu",
            "umask 077",
            f"rm -rf {_q(staging_path)}",
            f"mkdir -p -m 700 {_q(staging_path)}",
            f"chmod 700 {_q(staging_path)}",
        ]
    )


def _verify_staging_command(staging_path: str) -> str:
    staged_asterisk = posixpath.join(staging_path, "asterisk")
    staged_tftp = posixpath.join(staging_path, "tftp")
    return "\n".join(
        [
            "set -eu",
            f"test -d {_q(staged_asterisk)}",
            f"test -d {_q(staged_tftp)}",
            f"find {_q(staged_asterisk)} -type f | grep -q .",
            f"find {_q(staged_tftp)} -type f | grep -q .",
        ]
    )


def _swap_volumes_command(
    *,
    staging_path: str,
    asterisk_path: str,
    tftp_path: str,
    active_config_marker: ActiveConfigMarkerDeployment | None = None,
) -> str:
    commands = ["set -eu"]
    for staged_name, active_path in (("asterisk", asterisk_path), ("tftp", tftp_path)):
        staged_path = posixpath.join(staging_path, staged_name)
        parent = posixpath.dirname(active_path.rstrip("/")) or "/"
        backup_path = f"{active_path}.previous"
        commands.extend(
            [
                f"mkdir -p {_q(parent)}",
                f"rm -rf {_q(backup_path)}",
                f"if [ -e {_q(active_path)} ]; then mv {_q(active_path)} {_q(backup_path)}; fi",
                f"mv {_q(staged_path)} {_q(active_path)}",
            ]
        )
    commands.extend(_install_script_commands(staging_path))
    if active_config_marker and active_config_marker.configured_path.startswith("/"):
        source_path = _active_marker_source_path(
            staging_path=staging_path,
            asterisk_path=asterisk_path,
            tftp_path=tftp_path,
            bundle_path=active_config_marker.bundle_path,
        )
        target_path = posixpath.normpath(active_config_marker.configured_path)
        commands.extend(_install_active_marker_commands(source_path, target_path))
    return "\n".join(commands)


def _archive_active_marker(archive: zipfile.ZipFile) -> ActiveConfigMarkerDeployment | None:
    try:
        manifest = json.loads(archive.read("manifest.json").decode("utf-8"))
    except KeyError:
        return None
    marker = manifest.get("active_config_marker")
    if not isinstance(marker, dict):
        return None

    bundle_path = _safe_bundle_member_path(str(marker.get("path") or marker.get("bundle_path") or ""))
    configured_path = str(marker.get("configured_path") or marker.get("marker_path") or "").strip()
    if not configured_path:
        raise DeploymentArchiveError("Export archive active marker is missing configured_path.")
    return ActiveConfigMarkerDeployment(bundle_path=bundle_path, configured_path=configured_path)


def _safe_bundle_member_path(member_name: str) -> str:
    member_path = PurePosixPath(member_name)
    if member_path.is_absolute() or not member_path.parts or ".." in member_path.parts:
        raise DeploymentArchiveError(f"Unsafe active marker bundle path: {member_name}")
    return member_path.as_posix()


def _active_marker_source_path(*, staging_path: str, asterisk_path: str, tftp_path: str, bundle_path: str) -> str:
    if bundle_path.startswith("asterisk/"):
        return posixpath.join(asterisk_path, bundle_path.removeprefix("asterisk/"))
    if bundle_path.startswith("tftp/"):
        return posixpath.join(tftp_path, bundle_path.removeprefix("tftp/"))
    return posixpath.join(staging_path, bundle_path)


def _install_active_marker_commands(source_path: str, target_path: str) -> list[str]:
    if posixpath.normpath(source_path) == target_path:
        return [f"chmod go-rwx {_q(target_path)}"]

    parent = posixpath.dirname(target_path.rstrip("/")) or "/"
    backup_path = f"{target_path}.previous"
    return [
        f"mkdir -p {_q(parent)}",
        f"rm -rf {_q(backup_path)}",
        f"if [ -e {_q(target_path)} ]; then mv {_q(target_path)} {_q(backup_path)}; fi",
        f"cp {_q(source_path)} {_q(target_path)}",
        f"chmod go-rwx {_q(target_path)}",
    ]


def _install_script_commands(staging_path: str) -> list[str]:
    source_path = posixpath.join(staging_path, RECORDING_RETENTION_SCRIPT_BUNDLE_PATH)
    return [
        f"if [ -f {_q(source_path)} ]; then mkdir -p {_q(posixpath.dirname(RECORDING_RETENTION_SCRIPT_TARGET))}; fi",
        f"if [ -f {_q(source_path)} ]; then cp {_q(source_path)} {_q(RECORDING_RETENTION_SCRIPT_TARGET)}; fi",
        f"if [ -f {_q(RECORDING_RETENTION_SCRIPT_TARGET)} ]; then chmod 700 {_q(RECORDING_RETENTION_SCRIPT_TARGET)}; fi",
    ]


def _validate_remote_deployment_path(label: str, value: str) -> str:
    raw_value = str(value or "").strip()
    if not raw_value.startswith("/"):
        raise DeploymentConfigurationError(f"{label} must be an absolute remote path.")
    if ".." in PurePosixPath(raw_value).parts:
        raise DeploymentConfigurationError(f"{label} must not contain parent directory references.")

    normalized = posixpath.normpath(raw_value)
    if normalized == "/":
        raise DeploymentConfigurationError(f"{label} must not be the filesystem root.")
    allowed_roots = _deployment_allowed_roots()
    if allowed_roots and not any(_path_is_same_or_child(normalized, root) for root in allowed_roots):
        roots = ", ".join(allowed_roots)
        raise DeploymentConfigurationError(f"{label} must be under an allowed deployment root: {roots}.")
    return normalized


def _deployment_allowed_roots() -> tuple[str, ...]:
    configured = getattr(settings, "PBX_DEPLOYMENT_ALLOWED_ROOTS", DEFAULT_DEPLOYMENT_ALLOWED_ROOTS)
    if isinstance(configured, str):
        candidates = configured.split(",")
    else:
        candidates = configured
    roots = []
    for root in candidates:
        normalized = posixpath.normpath(str(root or "").strip())
        if normalized.startswith("/") and normalized != "/":
            roots.append(normalized.rstrip("/"))
    return tuple(dict.fromkeys(roots))


def _path_is_same_or_child(path: str, root: str) -> bool:
    root = root.rstrip("/")
    return path == root or path.startswith(f"{root}/")


def _captured_output(result: DeploymentCommandResult) -> str:
    output = "\n".join(part for part in (result.stdout.strip(), result.stderr.strip()) if part)
    return output[:MAX_CAPTURED_OUTPUT]


def _q(value: str) -> str:
    return shlex.quote(str(value))


def _decode(value: bytes | str | None) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    return value.decode("utf-8", errors="replace")


def _trailing_newline(value: str) -> str:
    return value if value.endswith("\n") else f"{value}\n"
