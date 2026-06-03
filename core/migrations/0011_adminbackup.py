from django.conf import settings
from django.db import migrations, models
import django.db.models.deletion
import django.utils.timezone


class Migration(migrations.Migration):

    dependencies = [
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
        ("core", "0010_configversion"),
    ]

    operations = [
        migrations.CreateModel(
            name="AdminBackup",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
                ("generated_at", models.DateTimeField(db_index=True, default=django.utils.timezone.now)),
                ("filename", models.CharField(max_length=180)),
                ("checksum", models.CharField(db_index=True, max_length=64)),
                ("archive", models.BinaryField(editable=False)),
                ("archive_size_bytes", models.PositiveBigIntegerField(default=0)),
                ("manifest", models.JSONField(blank=True, default=dict)),
                ("database_dump_method", models.CharField(blank=True, max_length=80)),
                (
                    "generated_by",
                    models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.SET_NULL,
                        related_name="generated_admin_backups",
                        to=settings.AUTH_USER_MODEL,
                    ),
                ),
            ],
            options={
                "ordering": ["-generated_at", "-id"],
            },
        ),
    ]
