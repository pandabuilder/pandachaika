import io
import os
import shutil
import tempfile
import zipfile
from unittest.mock import MagicMock, patch

from django.test import TestCase

from core.base.setup import Settings
from core.base.setup_utilities import GeneralUtils
from core.base.types import GalleryData
from core.providers.panda.downloaders import ArchiveDownloader


def _response(body: bytes, content_type: str = "application/zip", status_code: int = 200) -> MagicMock:
    response = MagicMock()
    response.status_code = status_code
    response.headers = {"Content-Type": content_type, "Content-Length": str(len(body))}
    response.iter_content.return_value = [body]
    response.text = body.decode("utf-8", "ignore")
    return response


def _zip_bytes() -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as zf:
        zf.writestr("01.jpg", b"image")
    return buffer.getvalue()


class PandaDirectArchiveDownloadTests(TestCase):
    """The archive download done in the crawler's thread, when archive downloads aren't queued."""

    def setUp(self):
        media_root = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, media_root, ignore_errors=True)
        os.makedirs(os.path.join(media_root, "archives"))
        self.settings = Settings(load_from_disk=False)
        self.settings.MEDIA_ROOT = media_root
        self.settings.providers["panda"] = MagicMock(
            maximum_gp_cost_for_dls=None,
            maximum_gp_mb_ratio_for_dls=None,
            proceed_with_download_if_lowest_cost=False,
            archive_dl_folder="archives",
            queue_archive_downloads=False,
            wait_timer=0.01,
        )
        self.downloader = ArchiveDownloader(self.settings, GeneralUtils(self.settings))
        self.downloader.gallery = GalleryData(
            gid="1", token="abcdef", root="https://e-hentai.org", title="Gallery", provider="panda"
        )
        self.events: list[MagicMock] = []

        def create_event(*args, **kwargs):
            event = MagicMock()
            self.events.append(event)
            return event

        page = _response(b"<a href='https://h.example/archive/1/abc/'>", content_type="text/html")
        patches = [
            patch.object(self.downloader, "request_archive_download", return_value=page),
            patch.object(self.downloader, "should_queue_archive_download", return_value=False),
            patch.object(self.downloader, "create_download_event", side_effect=create_event),
            patch(
                "core.providers.panda.downloaders.get_archive_link_from_html_page",
                return_value="https://h.example/archive/1/abc/",
            ),
            patch("core.providers.panda.downloaders.construct_request_dict", return_value={}),
            patch("core.providers.panda.downloaders.calc_crc32", return_value="0000ABCD"),
            patch("core.providers.panda.downloaders.time.sleep"),
        ]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)

    def download(self, *responses):
        self.downloader.web_request_function = MagicMock(side_effect=list(responses))
        self.downloader.start_download()
        return self.downloader.web_request_function

    def test_non_zip_attempt_fails_its_download_event(self):
        self.download(_response(b"not a zip"), _response(_zip_bytes()))
        self.assertEqual(self.downloader.return_code, 1)
        self.assertEqual(len(self.events), 2)
        self.events[0].set_as_failed.assert_called_once()
        self.events[1].set_as_failed.assert_not_called()

    def test_failed_download_request_is_retried(self):
        request = self.download(_response(b"", status_code=503), None, _response(_zip_bytes()))
        self.assertEqual(request.call_count, 3)
        self.assertEqual(self.downloader.return_code, 1)
        self.assertEqual(len(self.events), 1)

    def test_still_processing_backs_off(self):
        processing = _response(b"The file is already being processed.", content_type="text/html")
        with patch("core.providers.panda.downloaders.time.sleep") as sleep:
            self.download(processing, processing, processing)
        self.assertEqual(self.downloader.return_code, 0)
        self.assertEqual([call.args[0] for call in sleep.call_args_list], [20, 40])
        self.assertEqual(self.events, [])
