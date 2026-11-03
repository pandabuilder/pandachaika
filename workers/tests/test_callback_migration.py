# -*- coding: utf-8 -*-
import json
import uuid
from datetime import timedelta

import django.utils.timezone as django_tz
from django.contrib.auth import get_user_model
from django.test import TestCase

from core.base.parsers import BaseParser
from core.base.setup import Settings
from core.workers.webqueue import WebQueue
from viewer.models import Archive, EventLog, Gallery, GallerySubmitEntry, UserArchivePrefs
from workers import client
from workers.models import TaskQueue, WorkerProcess
from workers.runner import WorkerRunner
from workers.serialization import deserialize_settings_options, serialize_settings_options

User = get_user_model()




class CallbackMigrationTests(TestCase):

    def setUp(self):
        self.user = User.objects.create_user(username="callbackuser", password="password123")
        self.settings = Settings(load_from_disk=False)
        self.parser = BaseParser(self.settings)

    def test_notify_archive_without_callback(self):
        self.settings.archive_user = self.user
        self.settings.archive_reason = "Reason for archive"
        self.settings.event_action = "DOWNLOAD_ARCHIVE"

        # Call notify_archive without callback closure
        self.parser.notify_archive(None, "https://example.com/archive.zip", "success")

        log = EventLog.objects.filter(user=self.user, action="DOWNLOAD_ARCHIVE").first()
        self.assertIsNotNone(log)
        self.assertEqual(log.reason, "Reason for archive")
        self.assertEqual(log.data, "https://example.com/archive.zip")
        self.assertEqual(log.result, "success")

    def test_notify_gallery_without_callback(self):
        self.settings.archive_user = self.user
        self.settings.gallery_reason = "Reason for gallery"
        self.settings.event_action = "UPDATE_METADATA"

        self.parser.notify_gallery(None, "https://example.com/gallery/123", "success")

        log = EventLog.objects.filter(user=self.user, action="UPDATE_METADATA").first()
        self.assertIsNotNone(log)
        self.assertEqual(log.reason, "Reason for gallery")
        self.assertEqual(log.data, "https://example.com/gallery/123")
        self.assertEqual(log.result, "success")

    def test_notify_gallery_updates_submit_entry(self):
        group_uuid = uuid.uuid4()
        entry = GallerySubmitEntry.objects.create(
            submit_url="https://example.com/gallery/submit1",
            submit_group=group_uuid,
        )

        self.settings.submit_group_uuid = str(group_uuid)
        self.parser.notify_gallery(None, "https://example.com/gallery/submit1", "approved")

        entry.refresh_from_db()
        self.assertEqual(entry.submit_result, "approved")

    def test_legacy_callback_still_called_if_provided(self):
        called = []

        def my_callback(obj, url, result):
            called.append((url, result))

        self.parser.archive_callback = my_callback
        self.parser.notify_archive(None, "https://example.com/test", "success")
        self.assertEqual(len(called), 1)
        self.assertEqual(called[0], ("https://example.com/test", "success"))


