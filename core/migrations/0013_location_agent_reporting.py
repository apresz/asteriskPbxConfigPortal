import secrets

import core.models
from django.db import migrations, models


def populate_agent_tokens(apps, schema_editor):
    Location = apps.get_model("core", "Location")
    used_tokens = set(Location.objects.exclude(agent_token__isnull=True).values_list("agent_token", flat=True))
    for location in Location.objects.filter(agent_token__isnull=True):
        token = secrets.token_urlsafe(24)
        while token in used_tokens:
            token = secrets.token_urlsafe(24)
        used_tokens.add(token)
        location.agent_token = token
        location.save(update_fields=["agent_token"])


class Migration(migrations.Migration):
    dependencies = [
        ("core", "0012_alter_auditlog_action"),
    ]

    operations = [
        migrations.AddField(
            model_name="location",
            name="agent_token",
            field=models.CharField(
                editable=False,
                max_length=64,
                null=True,
                verbose_name="agent token",
            ),
        ),
        migrations.AddField(
            model_name="location",
            name="active_config_checksum",
            field=models.CharField(blank=True, max_length=64, verbose_name="PBX active config checksum"),
        ),
        migrations.AddField(
            model_name="location",
            name="active_config_reported_at",
            field=models.DateTimeField(blank=True, null=True, verbose_name="PBX active config reported at"),
        ),
        migrations.AddField(
            model_name="location",
            name="active_config_timestamp",
            field=models.DateTimeField(blank=True, null=True, verbose_name="PBX active config timestamp"),
        ),
        migrations.AddField(
            model_name="location",
            name="active_config_version_number",
            field=models.PositiveIntegerField(blank=True, null=True, verbose_name="PBX active config version"),
        ),
        migrations.RunPython(populate_agent_tokens, migrations.RunPython.noop),
        migrations.AlterField(
            model_name="location",
            name="agent_token",
            field=models.CharField(
                default=core.models.generate_agent_token,
                editable=False,
                max_length=64,
                unique=True,
                verbose_name="agent token",
            ),
        ),
    ]
