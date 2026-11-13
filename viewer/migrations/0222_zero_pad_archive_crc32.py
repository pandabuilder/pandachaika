from django.db import migrations
from django.db.models import Q, Value
from django.db.models.functions import Length, LPad, Upper


def zero_pad_crc32(apps, schema_editor):
    """Store every CRC32 with 8 uppercase hex digits.

    Older versions calculated it with "%X", which drops leading zeros (33AC069 instead of 033AC069), so exact lookups
    (duplicates, folder crawler, similar archives) missed archives with the new 8 digit values. Done in one UPDATE;
    values that aren't a CRC32 are left alone, and the history records keep what they recorded.
    """
    Archive = apps.get_model("viewer", "Archive")
    Archive.objects.filter(crc32__regex=r"^[0-9A-Fa-f]{1,8}$").annotate(crc32_length=Length("crc32")).filter(
        Q(crc32_length__lt=8) | ~Q(crc32=Upper("crc32"))
    ).update(crc32=LPad(Upper("crc32"), 8, Value("0")))


class Migration(migrations.Migration):

    dependencies = [
        ("viewer", "0221_archive_file_check_filter_scope"),
    ]

    operations = [
        # Not reversible: the padded values are equivalent, and the old ones can't be told apart.
        migrations.RunPython(zero_pad_crc32, migrations.RunPython.noop),
    ]
