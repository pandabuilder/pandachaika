import importlib

from django.apps import apps
from django.test import TestCase

from core.base.utilities import normalize_crc32
from viewer.models import Archive
from viewer.tests.factories import ArchiveFactory

pad_migration = importlib.import_module("viewer.migrations.0222_zero_pad_archive_crc32")


class Crc32FormatTests(TestCase):
    def test_normalize_crc32(self):
        self.assertEqual(normalize_crc32("33AC069"), "033AC069")
        self.assertEqual(normalize_crc32(" c9dab27 "), "0C9DAB27")
        self.assertEqual(normalize_crc32("0C9DAB27"), "0C9DAB27")
        self.assertEqual(normalize_crc32("0"), "00000000")
        for value in ("", "not-a-crc", "123456789"):
            self.assertEqual(normalize_crc32(value), value)
        self.assertEqual(normalize_crc32(None), "")

    def test_saving_an_archive_stores_8_digits(self):
        archive = ArchiveFactory.create(gallery=None, crc32="33ac069")
        archive.refresh_from_db()
        self.assertEqual(archive.crc32, "033AC069")
        # Paths that skip Archive.save (e.g. recalc_fileinfo) still go through the pre_save signal.
        archive.crc32 = "abc"
        super(Archive, archive).save()
        archive.refresh_from_db()
        self.assertEqual(archive.crc32, "00000ABC")
        archive.crc32 = ""
        archive.save()
        archive.refresh_from_db()
        self.assertEqual(archive.crc32, "")

    def test_migration_pads_stored_values(self):
        short = ArchiveFactory.create(gallery=None)
        lower = ArchiveFactory.create(gallery=None)
        fine = ArchiveFactory.create(gallery=None)
        other = ArchiveFactory.create(gallery=None)
        empty = ArchiveFactory.create(gallery=None)
        # QuerySet.update skips the signal, like the data stored by older versions.
        for archive, value in ((short, "33AC069"), (lower, "0c9dab27"), (fine, "0C9DAB27"), (other, "nothex"), (empty, "")):
            Archive.objects.filter(pk=archive.pk).update(crc32=value)

        pad_migration.zero_pad_crc32(apps, None)

        values = dict(Archive.objects.values_list("pk", "crc32"))
        self.assertEqual(
            [values[a.pk] for a in (short, lower, fine, other, empty)],
            ["033AC069", "0C9DAB27", "0C9DAB27", "nothex", ""],
        )
