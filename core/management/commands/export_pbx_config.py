import json
from pathlib import Path

from django.core.management.base import BaseCommand, CommandError

from core.audit import record_audit
from core.config_export import (
    ConfigExportValidationError,
    build_location_config,
    create_config_version,
    write_config_version_directory,
)
from core.models import AuditAction, AuditOutcome, Location


class Command(BaseCommand):
    help = "Export PBX configuration data for generators and helper scripts."

    def add_arguments(self, parser):
        parser.add_argument("location_slug")
        parser.add_argument(
            "--output-dir",
            help="Write the expanded export ZIP structure to this directory in addition to JSON stdout.",
        )
        parser.add_argument(
            "--zip-output",
            help="Write the immutable export ZIP archive to this path in addition to JSON stdout.",
        )

    def handle(self, *args, **options):
        location = Location.objects.get(slug=options["location_slug"])
        try:
            version = create_config_version(location, require_emergency=True)
        except ConfigExportValidationError as exc:
            audit_details = {
                "location_id": location.id,
                "location_slug": location.slug,
                "validation": exc.validation,
            }
            record_audit(
                actor=None,
                action=AuditAction.CONFIG_EXPORT,
                target=f"locations/{location.slug}/config",
                outcome=AuditOutcome.FAILURE,
                details=audit_details,
            )
            error_codes = ", ".join(error["code"] for error in exc.validation["errors"])
            raise CommandError(f"Export blocked by validation errors: {error_codes}")

        audit_details = {
            "location_id": location.id,
            "location_slug": location.slug,
            "config_version_id": version.id,
            "version_number": version.version_number,
            "checksum": version.checksum,
            "validation": {
                "errors": [],
                "warnings": version.warnings,
            },
        }
        if options["output_dir"]:
            write_config_version_directory(version, Path(options["output_dir"]))
        if options["zip_output"]:
            Path(options["zip_output"]).write_bytes(bytes(version.archive))

        record_audit(
            actor=None,
            action=AuditAction.CONFIG_EXPORT,
            target=f"locations/{location.slug}/config",
            outcome=AuditOutcome.SUCCESS,
            details=audit_details,
        )
        config = build_location_config(
            location,
            require_emergency=True,
            validation={
                "errors": [],
                "warnings": version.warnings,
            },
        )
        config["config_version"] = {
            "id": version.id,
            "version_number": version.version_number,
            "checksum": version.checksum,
            "archive_size_bytes": version.archive_size_bytes,
            "file_manifest": version.file_manifest,
        }
        self.stdout.write(json.dumps(config, indent=2, sort_keys=True))
