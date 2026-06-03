from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):

    dependencies = [
        ("core", "0007_provider_trunk_outbound_route_management"),
        ("core", "0006_inbound_routing_configuration"),
    ]

    operations = [
        migrations.CreateModel(
            name="AudioPrompt",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
                ("name", models.CharField(max_length=120)),
                ("original_file", models.FileField(max_length=255, upload_to="audio_prompts/original/")),
                ("converted_file", models.FileField(max_length=255, upload_to="audio_prompts/converted/")),
                ("original_filename", models.CharField(max_length=255)),
                ("source_format", models.CharField(choices=[("wav", "WAV"), ("mp3", "MP3"), ("m4a", "M4A")], max_length=8)),
                ("content_type", models.CharField(blank=True, max_length=120)),
                ("size_bytes", models.PositiveBigIntegerField(default=0)),
                ("converted_format", models.CharField(default="wav", max_length=8)),
                ("sample_rate_hz", models.PositiveIntegerField(default=8000)),
                ("channels", models.PositiveSmallIntegerField(default=1)),
                ("asterisk_path", models.CharField(max_length=255)),
                (
                    "location",
                    models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name="audio_prompts", to="core.location"),
                ),
            ],
            options={
                "ordering": ["location", "name"],
            },
        ),
        migrations.AddField(
            model_name="ivr",
            name="prompt",
            field=models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name="ivrs", to="core.audioprompt"),
        ),
        migrations.AddConstraint(
            model_name="audioprompt",
            constraint=models.UniqueConstraint(fields=("location", "name"), name="unique_audio_prompt_name_per_location"),
        ),
    ]
