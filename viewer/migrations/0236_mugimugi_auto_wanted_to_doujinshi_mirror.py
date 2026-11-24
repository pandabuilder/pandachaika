# The doujinshi.org (mugimugi) API is gone. Its auto wanted sources run on the doujinshi_mirror provider instead,
# with their parameters translated to the mirror's JSON API.

import json

from django.db import migrations

from core.providers.doujinshi_mirror.legacy import params_from_legacy


def forwards(apps, schema_editor):
    AutoWantedSource = apps.get_model("viewer", "AutoWantedSource")
    for source in AutoWantedSource.objects.filter(provider="mugimugi"):
        old_params = source.params or {}
        params, dropped = params_from_legacy(old_params)
        note = "Converted from a mugimugi source, whose parameters were: {}.".format(
            json.dumps(old_params, ensure_ascii=False, sort_keys=True)
        )
        if dropped:
            note += " Left out (no equivalent on the mirror): {}.".format(", ".join(sorted(dropped)))
        source.provider = "doujinshi_mirror"
        source.params = params
        source.notes = "{}\n{}".format(source.notes, note).strip() if source.notes else note
        source.save(update_fields=["provider", "params", "notes"])


class Migration(migrations.Migration):

    dependencies = [
        ("viewer", "0235_auto_updater_schedule"),
    ]

    operations = [
        migrations.RunPython(forwards, migrations.RunPython.noop),
    ]
