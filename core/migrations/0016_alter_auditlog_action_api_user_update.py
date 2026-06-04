from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("core", "0015_location_agent_telemetry"),
    ]

    operations = [
        migrations.AlterField(
            model_name="auditlog",
            name="action",
            field=models.CharField(
                choices=[
                    ("config_change", "Config change"),
                    ("config_export", "Config export"),
                    ("deployment", "Deployment"),
                    ("live_pbx_action", "Live PBX action"),
                    ("api_key_create", "API key create"),
                    ("api_key_rotate", "API key rotate"),
                    ("api_key_revoke", "API key revoke"),
                    ("api_user_update", "API user update"),
                    ("backup_create", "Backup create"),
                    ("backup_download", "Backup download"),
                ],
                max_length=32,
            ),
        ),
    ]
