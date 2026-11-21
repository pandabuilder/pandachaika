from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("workers", "0006_remove_post_downloader_scheduler_state"),
    ]

    operations = [
        migrations.AddField(
            model_name="taskqueue",
            name="result",
            field=models.JSONField(
                blank=True,
                help_text="What the task did, as recorded by the worker when it completed (counts, created objects). "
                "Null when the task type records nothing.",
                null=True,
            ),
        ),
    ]
