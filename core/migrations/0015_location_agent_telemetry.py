from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("core", "0014_merge_0013_deploymentrecord_location_deployment_targets_0013_location_agent_reporting"),
    ]

    operations = [
        migrations.AddField(
            model_name="location",
            name="agent_telemetry",
            field=models.JSONField(blank=True, default=dict, verbose_name="PBX agent telemetry"),
        ),
        migrations.AddField(
            model_name="location",
            name="agent_telemetry_errors",
            field=models.JSONField(blank=True, default=list, verbose_name="PBX agent telemetry errors"),
        ),
        migrations.AddField(
            model_name="location",
            name="agent_telemetry_reported_at",
            field=models.DateTimeField(blank=True, null=True, verbose_name="PBX agent telemetry reported at"),
        ),
    ]
