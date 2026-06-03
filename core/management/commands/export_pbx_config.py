import json

from django.core.management.base import BaseCommand

from core.config_export import build_location_config
from core.models import Location


class Command(BaseCommand):
    help = "Export PBX configuration data for generators and helper scripts."

    def add_arguments(self, parser):
        parser.add_argument("location_slug")

    def handle(self, *args, **options):
        location = Location.objects.get(slug=options["location_slug"])
        self.stdout.write(json.dumps(build_location_config(location), indent=2, sort_keys=True))
