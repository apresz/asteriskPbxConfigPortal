from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("core", "0017_merge_auditlog_action_choices"),
    ]

    operations = [
        migrations.AddField(
            model_name="serviceidentity",
            name="permissions",
            field=models.JSONField(blank=True, default=list),
        ),
    ]
