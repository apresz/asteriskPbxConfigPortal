from django.conf import settings
from django.db import migrations, models
import django.db.models.deletion
import django.utils.timezone


class Migration(migrations.Migration):

    dependencies = [
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
        ("core", "0009_phone_firmware_load_name"),
    ]

    operations = [
        migrations.CreateModel(
            name="ConfigVersion",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
                ("version_number", models.PositiveIntegerField()),
                ("exported_at", models.DateTimeField(db_index=True, default=django.utils.timezone.now)),
                ("checksum", models.CharField(db_index=True, max_length=64)),
                ("warnings", models.JSONField(blank=True, default=list)),
                ("emergency_status", models.JSONField(blank=True, default=dict)),
                ("file_manifest", models.JSONField(blank=True, default=list)),
                ("deployment_snapshot", models.JSONField(blank=True, default=dict)),
                ("archive", models.BinaryField(editable=False)),
                ("archive_size_bytes", models.PositiveBigIntegerField(default=0)),
                (
                    "deployment_status",
                    models.CharField(
                        choices=[
                            ("not_deployed", "Not deployed"),
                            ("deployed", "Deployed"),
                            ("rolled_back", "Rolled back"),
                        ],
                        default="not_deployed",
                        max_length=24,
                    ),
                ),
                ("deployed_at", models.DateTimeField(blank=True, null=True)),
                (
                    "deployed_by",
                    models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.SET_NULL,
                        related_name="deployed_config_versions",
                        to=settings.AUTH_USER_MODEL,
                    ),
                ),
                (
                    "exported_by",
                    models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.SET_NULL,
                        related_name="exported_config_versions",
                        to=settings.AUTH_USER_MODEL,
                    ),
                ),
                (
                    "location",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="config_versions",
                        to="core.location",
                    ),
                ),
                (
                    "rollback_of",
                    models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.SET_NULL,
                        related_name="rollback_versions",
                        to="core.configversion",
                    ),
                ),
            ],
            options={
                "ordering": ["location", "-version_number"],
            },
        ),
        migrations.AddConstraint(
            model_name="configversion",
            constraint=models.UniqueConstraint(
                fields=("location", "version_number"),
                name="unique_config_version_per_location",
            ),
        ),
    ]
