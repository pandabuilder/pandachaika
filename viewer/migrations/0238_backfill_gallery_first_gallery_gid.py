# The backfill for Gallery.first_gallery_gid runs in its own migration: it sets first_gallery links, a deferred
# foreign key, and Postgres can't create an index on the table in the same transaction while those checks are pending.
# It skips the galleries that already have the gid, so running it again is harmless.

import json

from django.db import migrations


def backfill_first_gallery_gid(apps, schema_editor):
    """Copy the provider's first gallery gid out of the stored metadata, and link the first gallery when it is here."""
    Gallery = apps.get_model("viewer", "Gallery")

    to_update = []
    candidates = (
        Gallery.objects.filter(provider_metadata__contains='"first_gid"', first_gallery_gid__isnull=True)
        .only("pk", "gid", "provider", "provider_metadata", "first_gallery_id")
        .iterator(chunk_size=2000)
    )
    for gallery in candidates:
        try:
            metadata = json.loads(gallery.provider_metadata)
        except ValueError:
            continue
        if not isinstance(metadata, dict) or metadata.get("first_gid") in (None, ""):
            continue
        first_gid = str(metadata["first_gid"])
        if first_gid == gallery.gid:
            continue
        gallery.first_gallery_gid = first_gid
        to_update.append(gallery)

    first_ids = {
        (provider, gid): pk
        for pk, gid, provider in Gallery.objects.filter(
            gid__in={g.first_gallery_gid for g in to_update}
        ).values_list("pk", "gid", "provider")
    }
    for gallery in to_update:
        if gallery.first_gallery_id is None:
            gallery.first_gallery_id = first_ids.get((gallery.provider, gallery.first_gallery_gid))

    Gallery.objects.bulk_update(to_update, ["first_gallery_gid", "first_gallery"], batch_size=1000)


class Migration(migrations.Migration):

    dependencies = [
        ("viewer", "0237_gallery_first_gallery_gid"),
    ]

    operations = [
        migrations.RunPython(backfill_first_gallery_gid, migrations.RunPython.noop),
    ]
