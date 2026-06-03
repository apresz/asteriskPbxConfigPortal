import json

from django.core.management.base import BaseCommand, CommandError

from core.audit import record_audit
from core.config_export import build_location_config, validate_location_routing
from core.models import AuditAction, AuditOutcome, Location


class Command(BaseCommand):
    help = "Export PBX configuration data for generators and helper scripts."

    def add_arguments(self, parser):
        parser.add_argument("location_slug")

    def handle(self, *args, **options):
        location = Location.objects.get(slug=options["location_slug"])
        validation = validate_location_routing(location, require_emergency=True)
        audit_details = {
            "location_id": location.id,
            "location_slug": location.slug,
            "validation": validation,
        }
        if validation["errors"]:
            record_audit(
                actor=None,
                action=AuditAction.CONFIG_EXPORT,
                target=f"locations/{location.slug}/config",
                outcome=AuditOutcome.FAILURE,
                details=audit_details,
            )
            error_codes = ", ".join(error["code"] for error in validation["errors"])
            raise CommandError(f"Export blocked by validation errors: {error_codes}")

        config = build_location_config(location, require_emergency=True, validation=validation)
        record_audit(
            actor=None,
            action=AuditAction.CONFIG_EXPORT,
            target=f"locations/{location.slug}/config",
            outcome=AuditOutcome.SUCCESS,
            details=audit_details,
        )
        self.stdout.write(json.dumps(config, indent=2, sort_keys=True))
