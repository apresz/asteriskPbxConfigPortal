from django.conf import settings
import django.core.validators
from django.db import migrations, models
import django.db.models.deletion
import django.utils.timezone


class Migration(migrations.Migration):

    dependencies = [
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
        ("core", "0012_alter_auditlog_action"),
    ]

    operations = [
        migrations.AddField(
            model_name="location",
            name="deployment_asterisk_path",
            field=models.CharField(
                default="/srv/pbx/asterisk",
                max_length=255,
                verbose_name="deployment Asterisk path",
            ),
        ),
        migrations.AddField(
            model_name="location",
            name="deployment_reload_command",
            field=models.CharField(
                default="asterisk -rx 'core reload'",
                max_length=255,
                verbose_name="deployment reload command",
            ),
        ),
        migrations.AddField(
            model_name="location",
            name="deployment_staging_path",
            field=models.CharField(
                default="/srv/pbx/staging",
                max_length=255,
                verbose_name="deployment staging path",
            ),
        ),
        migrations.AddField(
            model_name="location",
            name="deployment_tftp_path",
            field=models.CharField(
                default="/srv/pbx/tftp",
                max_length=255,
                verbose_name="deployment TFTP path",
            ),
        ),
        migrations.CreateModel(
            name="DeploymentRecord",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
                ("started_at", models.DateTimeField(db_index=True, default=django.utils.timezone.now)),
                ("completed_at", models.DateTimeField(blank=True, null=True)),
                ("target_host", models.CharField(max_length=255)),
                (
                    "target_port",
                    models.PositiveIntegerField(
                        default=22,
                        validators=[
                            django.core.validators.MinValueValidator(1),
                            django.core.validators.MaxValueValidator(65535),
                        ],
                    ),
                ),
                ("target_username", models.CharField(blank=True, max_length=80)),
                ("staging_path", models.CharField(max_length=255)),
                ("asterisk_path", models.CharField(max_length=255)),
                ("tftp_path", models.CharField(max_length=255)),
                ("reload_command", models.CharField(blank=True, max_length=255)),
                (
                    "action",
                    models.CharField(
                        choices=[("deploy", "Deploy"), ("rollback", "Rollback")],
                        default="deploy",
                        max_length=16,
                    ),
                ),
                (
                    "status",
                    models.CharField(
                        choices=[("pending", "Pending"), ("success", "Success"), ("failed", "Failed")],
                        default="pending",
                        max_length=16,
                    ),
                ),
                (
                    "reload_result",
                    models.CharField(
                        choices=[("not_run", "Not run"), ("success", "Success"), ("failed", "Failed")],
                        default="not_run",
                        max_length=16,
                    ),
                ),
                ("reload_output", models.TextField(blank=True)),
                ("error_message", models.TextField(blank=True)),
                ("details", models.JSONField(blank=True, default=dict)),
                (
                    "config_version",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.PROTECT,
                        related_name="deployment_records",
                        to="core.configversion",
                    ),
                ),
                (
                    "location",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="deployment_records",
                        to="core.location",
                    ),
                ),
                (
                    "operator",
                    models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.SET_NULL,
                        related_name="deployment_records",
                        to=settings.AUTH_USER_MODEL,
                    ),
                ),
                (
                    "rollback_source_version",
                    models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.PROTECT,
                        related_name="rollback_deployment_records",
                        to="core.configversion",
                    ),
                ),
            ],
            options={
                "ordering": ["-started_at", "-id"],
            },
        ),
    ]
