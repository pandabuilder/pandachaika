from django.db import migrations


def cleanup_scheduler_states(apps, schema_editor):
    SchedulerState = apps.get_model("workers", "SchedulerState")
    SchedulerState.objects.filter(name__in=["worker_runner", "worker_process"]).update(
        schedule_type="control"
    )
    SchedulerState.objects.filter(name__in=["post_downloader", "timed_downloader"]).delete()


class Migration(migrations.Migration):

    dependencies = [
        ("workers", "0008_taskqueue_taskqueue_pending_order_idx_and_more"),
    ]

    operations = [
        migrations.RunPython(cleanup_scheduler_states, migrations.RunPython.noop),
    ]
