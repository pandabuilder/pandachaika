import hashlib
import json

from django.db import migrations, models


def _options_hash(options, user_id):
    # Copy of TaskQueue.compute_options_hash at the time of this migration.
    payload = json.dumps(
        {"options": options or {}, "user": user_id},
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        default=str,
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def backfill_active_tasks(apps, schema_editor):
    """Hash the tasks that can still be deduplicated against (pending and processing). Finished ones keep ''."""
    TaskQueue = apps.get_model("workers", "TaskQueue")
    tasks = TaskQueue.objects.filter(status__in=["pending", "processing"]).only("pk", "options", "user_id")
    for task in tasks.iterator():
        task.options_hash = _options_hash(task.options, task.user_id)
        task.save(update_fields=["options_hash"])


class Migration(migrations.Migration):

    dependencies = [
        ("workers", "0010_taskqueue_taskqueue_finished_order_idx"),
    ]

    operations = [
        migrations.AddField(
            model_name="taskqueue",
            name="options_hash",
            field=models.CharField(
                blank=True,
                default="",
                help_text=(
                    "Hash of the options and user the task was queued with. Two tasks with the same type and args are "
                    "only duplicates when this matches too."
                ),
                max_length=64,
            ),
        ),
        migrations.RunPython(backfill_active_tasks, migrations.RunPython.noop),
    ]
