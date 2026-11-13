from django.db import migrations


def remove_post_downloader_state(apps, schema_editor):
    # The post downloader scheduler was merged into download_progress_checker.
    SchedulerState = apps.get_model("workers", "SchedulerState")
    SchedulerState.objects.filter(name__in=["post_downloader", "timed_downloader"]).delete()


class Migration(migrations.Migration):

    dependencies = [
        ("workers", "0005_taskqueue_attempts_not_before"),
    ]

    operations = [
        migrations.RunPython(remove_post_downloader_state, migrations.RunPython.noop),
    ]
