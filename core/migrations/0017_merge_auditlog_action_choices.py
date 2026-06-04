from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("core", "0016_alter_auditlog_action"),
        ("core", "0016_alter_auditlog_action_api_user_update"),
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
                    ("recording_playback", "Recording playback"),
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
