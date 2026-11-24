"""Archive cold tier: S3 client signing and multipart flow, offload/restore, downloads, model hooks, command."""

import datetime
import hashlib
import io
import os
import shutil
import tempfile
import typing
from unittest import mock
from urllib.parse import parse_qs, urlsplit

import pytest
import requests
from django.conf import settings
from django.contrib.auth.models import User
from django.core.management import call_command
from django.test import Client, TestCase, override_settings
from django.urls import reverse
from pydantic import ValidationError

from core.base.config_models import ColdStorageSettings
from core.base.s3 import (
    MAX_PARTS,
    MIN_PART_SIZE,
    S3Client,
    S3Error,
    S3ObjectInfo,
    choose_part_size,
    multipart_etag,
)
from viewer.models import Archive, EventLog
from viewer.services import cold_storage
from viewer.services.cold_storage import ColdStorageError, ColdStorageService

AWS_KEY = "AKIAIOSFODNN7EXAMPLE"
AWS_SECRET = "wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY"
EMPTY_SHA = "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855"
AWS_DATE = "20130524T000000Z"


def aws_example_client() -> S3Client:
    return S3Client(
        "https://s3.amazonaws.com", "examplebucket", AWS_KEY, AWS_SECRET, region="us-east-1", addressing_style="virtual"
    )


# ---------------------------------------------------------------------------- S3 client


class TestS3Signing:
    """Examples from the AWS Signature Version 4 documentation for S3."""

    def test_presigned_url(self) -> None:
        url = aws_example_client().presigned_get_url(
            "test.txt", expires=86400, now=datetime.datetime(2013, 5, 24, tzinfo=datetime.timezone.utc)
        )
        assert url.startswith("https://examplebucket.s3.amazonaws.com/test.txt?")
        assert url.endswith("X-Amz-Signature=aeeed9bbccd4d02ee5c0109b86d86835f995330da4c265957d157751f604d404")

    def test_get_with_range(self) -> None:
        headers = {
            "host": "examplebucket.s3.amazonaws.com",
            "range": "bytes=0-9",
            "x-amz-content-sha256": EMPTY_SHA,
            "x-amz-date": AWS_DATE,
        }
        signature, signed_headers, scope = aws_example_client()._signature(
            "GET", "/test.txt", "", headers, EMPTY_SHA, AWS_DATE
        )
        assert signature == "f0e8bdb87c964420e857bd35b5d6ed310bd44f0170aba48dd91039c6036bdb41"
        assert signed_headers == "host;range;x-amz-content-sha256;x-amz-date"
        assert scope == "20130524/us-east-1/s3/aws4_request"

    def test_put_object(self) -> None:
        payload_hash = hashlib.sha256(b"Welcome to Amazon S3.").hexdigest()
        client = aws_example_client()
        host, path = client._host_and_path("test$file.text")
        headers = {
            "host": host,
            "date": "Fri, 24 May 2013 00:00:00 GMT",
            "x-amz-storage-class": "REDUCED_REDUNDANCY",
            "x-amz-content-sha256": payload_hash,
            "x-amz-date": AWS_DATE,
        }
        signature, _, _ = client._signature("PUT", path, "", headers, payload_hash, AWS_DATE)
        assert signature == "98ad721746da40c64f1a55b78f14c238d841ea1380cd77a1b5971af0ece108bd"

    def test_subresource_query(self) -> None:
        client = aws_example_client()
        headers = {"host": "examplebucket.s3.amazonaws.com", "x-amz-content-sha256": EMPTY_SHA, "x-amz-date": AWS_DATE}
        query = client._canonical_query({"lifecycle": ""})
        signature, _, _ = client._signature("GET", "/", query, headers, EMPTY_SHA, AWS_DATE)
        assert signature == "fea454ca298b7da1c68078a5d1bdbfbbe0d65c699e0f91ac7a200a0136783543"

    def test_path_style_encodes_key(self) -> None:
        client = S3Client("https://s3.gra.io.cloud.ovh.net", "my-bucket", "k", "s", region="gra")
        host, path = client._host_and_path("galleries/[C99] テスト 100% +1.zip")
        assert host == "s3.gra.io.cloud.ovh.net"
        assert path == "/my-bucket/galleries/%5BC99%5D%20%E3%83%86%E3%82%B9%E3%83%88%20100%25%20%2B1.zip"

    def test_presign_response_headers_and_expiry_cap(self) -> None:
        client = S3Client("https://s3.example.com:8443", "b", "k", "s")
        url = client.presigned_get_url(
            "a b.zip", expires=10**9, response_headers={"response-content-disposition": "attachment; filename*=UTF-8''a%20b.zip"}
        )
        parts = urlsplit(url)
        assert parts.netloc == "s3.example.com:8443"
        assert parts.path == "/b/a%20b.zip"
        query = parse_qs(parts.query)
        assert query["X-Amz-Expires"] == ["604800"]
        assert query["response-content-disposition"] == ["attachment; filename*=UTF-8''a%20b.zip"]

    def test_invalid_configuration(self) -> None:
        with pytest.raises(ValueError):
            S3Client("", "b", "k", "s")
        with pytest.raises(ValueError):
            S3Client("https://s3.example.com", "", "k", "s")
        with pytest.raises(ValueError):
            S3Client("https://s3.example.com", "b", "k", "s", addressing_style="weird")


def test_choose_part_size() -> None:
    assert choose_part_size(10, 1) == MIN_PART_SIZE
    assert choose_part_size(10 * 1024**3, 64 * 1024**2) == 64 * 1024**2
    huge = 64 * 1024**2 * MAX_PARTS * 3
    assert choose_part_size(huge, 64 * 1024**2) * MAX_PARTS >= huge


def test_multipart_etag() -> None:
    parts = [hashlib.md5(b"a").digest(), hashlib.md5(b"b").digest()]
    assert multipart_etag(parts) == '"{}-2"'.format(hashlib.md5(b"".join(parts)).hexdigest())


class FakeResponse:
    def __init__(self, status: int = 200, content: bytes = b"", headers: dict[str, str] | None = None) -> None:
        self.status_code = status
        self.content = content
        self.headers = headers or {}

    def close(self) -> None:
        pass


class RecordingSession:
    """requests.Session stand-in that answers from a list of responses and records the requests."""

    def __init__(self, responses: list[FakeResponse]) -> None:
        self.responses = list(responses)
        self.calls: list[tuple[str, str, dict[str, str], bytes | None]] = []

    def request(self, method: str, url: str, headers: dict[str, str], data: bytes | None, **kwargs: typing.Any) -> FakeResponse:
        self.calls.append((method, url, headers, data))
        return self.responses.pop(0)


class TestS3Multipart:
    def _file(self, tmp_path: typing.Any, size: int) -> str:
        path = os.path.join(str(tmp_path), "upload.bin")
        with open(path, "wb") as f:
            f.write(os.urandom(size))
        return path

    def test_multipart_upload(self, tmp_path: typing.Any) -> None:
        path = self._file(tmp_path, MIN_PART_SIZE * 2 + 10)
        with open(path, "rb") as f:
            expected_etag = multipart_etag([hashlib.md5(chunk).digest() for chunk in iter(lambda: f.read(MIN_PART_SIZE), b"")])
        session = RecordingSession(
            [
                FakeResponse(200, b"<InitiateMultipartUploadResult><UploadId>up1</UploadId></InitiateMultipartUploadResult>"),
                FakeResponse(200, headers={"ETag": '"e1"'}),
                FakeResponse(200, headers={"ETag": '"e2"'}),
                FakeResponse(200, headers={"ETag": '"e3"'}),
                FakeResponse(
                    200,
                    "<CompleteMultipartUploadResult><ETag>{}</ETag></CompleteMultipartUploadResult>".format(
                        expected_etag
                    ).encode(),
                ),
            ]
        )
        client = S3Client("https://s3.example.com", "b", "k", "s", session=typing.cast(requests.Session, session))
        progress: list[int] = []
        etag = client.upload_file(
            path, "dir/f.zip", metadata={"crc32": "ABCD"}, part_size=MIN_PART_SIZE, multipart_threshold=MIN_PART_SIZE,
            progress=progress.append,
        )
        assert etag == expected_etag
        assert sum(progress) == os.path.getsize(path)
        methods = [(m, urlsplit(u).query) for m, u, _, _ in session.calls]
        assert methods[0] == ("POST", "uploads=")
        assert methods[1] == ("PUT", "partNumber=1&uploadId=up1")
        assert methods[3] == ("PUT", "partNumber=3&uploadId=up1")
        assert methods[4] == ("POST", "uploadId=up1")
        assert session.calls[0][2]["x-amz-meta-crc32"] == "ABCD"
        # Each part carries its Content-MD5 and its signed SHA-256.
        part_headers = session.calls[1][2]
        part_body = session.calls[1][3] or b""
        assert part_headers["x-amz-content-sha256"] == hashlib.sha256(part_body).hexdigest()
        assert "content-md5" in part_headers
        complete_body = (session.calls[4][3] or b"").decode()
        assert '<PartNumber>3</PartNumber><ETag>"e3"</ETag>' in complete_body

    def test_complete_error_in_200_aborts(self, tmp_path: typing.Any) -> None:
        path = self._file(tmp_path, MIN_PART_SIZE + 1)
        session = RecordingSession(
            [
                FakeResponse(200, b"<InitiateMultipartUploadResult><UploadId>up1</UploadId></InitiateMultipartUploadResult>"),
                FakeResponse(200, headers={"ETag": '"e1"'}),
                FakeResponse(200, headers={"ETag": '"e2"'}),
                FakeResponse(200, b"<Error><Code>InternalError</Code><Message>oops</Message></Error>"),
                FakeResponse(204),
            ]
        )
        client = S3Client("https://s3.example.com", "b", "k", "s", session=typing.cast(requests.Session, session))
        with pytest.raises(S3Error) as error:
            client.upload_file(path, "f.zip", part_size=MIN_PART_SIZE, multipart_threshold=MIN_PART_SIZE)
        assert error.value.code == "InternalError"
        method, url, _, _ = session.calls[-1]
        assert (method, urlsplit(url).query) == ("DELETE", "uploadId=up1")

    def test_error_response(self) -> None:
        session = RecordingSession(
            [FakeResponse(403, b"<Error><Code>SignatureDoesNotMatch</Code><Message>bad</Message></Error>")]
        )
        client = S3Client("https://s3.example.com", "b", "k", "s", session=typing.cast(requests.Session, session))
        with pytest.raises(S3Error) as error:
            client.put_object("f", b"data")
        assert error.value.status == 403
        assert error.value.code == "SignatureDoesNotMatch"

    def test_head_missing(self) -> None:
        session = RecordingSession([FakeResponse(404)])
        client = S3Client("https://s3.example.com", "b", "k", "s", session=typing.cast(requests.Session, session))
        assert client.head_object("f") is None


# ---------------------------------------------------------------------------- settings


def test_settings_validation() -> None:
    assert ColdStorageSettings(local_prefix="/galleries/cold/").local_prefix == "galleries/cold"
    with pytest.raises(ValidationError):
        ColdStorageSettings(local_prefix="../outside")
    with pytest.raises(ValidationError):
        ColdStorageSettings(local_prefix="")
    with pytest.raises(ValidationError):
        ColdStorageSettings(addressing_style="other")
    assert not ColdStorageSettings(enable=True, endpoint_url="https://x", bucket="b").is_configured()
    assert ColdStorageSettings(
        enable=True, endpoint_url="https://x", bucket="b", access_key_id="k", secret_access_key="s"
    ).is_configured()


# ---------------------------------------------------------------------------- Django side


class FakeBucket(S3Client):
    """In-memory bucket with the S3Client interface used by the cold storage service."""

    def __init__(self) -> None:
        super().__init__("https://s3.test.invalid", "cold-bucket", "key", "secret", region="gra")
        self.objects: dict[str, tuple[bytes, dict[str, str]]] = {}
        self.fail_uploads = False

    def head_object(self, key: str) -> S3ObjectInfo | None:
        if key not in self.objects:
            return None
        data, metadata = self.objects[key]
        return S3ObjectInfo(key=key, size=len(data), etag='"{}"'.format(hashlib.md5(data).hexdigest()), metadata=metadata)

    def put_object(self, key: str, data: bytes, content_type: str = "", metadata: dict[str, str] | None = None) -> str:
        self.objects[key] = (data, dict(metadata or {}))
        return '"{}"'.format(hashlib.md5(data).hexdigest())

    def upload_file(  # type: ignore[override]
        self, path: str, key: str, content_type: str = "", metadata: dict[str, str] | None = None, **kwargs: typing.Any
    ) -> str:
        if self.fail_uploads:
            raise S3Error("PUT {} returned HTTP 500".format(key), status=500)
        with open(path, "rb") as f:
            return self.put_object(key, f.read(), metadata=metadata)

    def download_file(self, key: str, path: str, chunk_size: int = 0, progress: typing.Any = None) -> int:
        if key not in self.objects:
            raise S3Error("GET {} returned HTTP 404".format(key), status=404)
        with open(path, "wb") as f:
            f.write(self.objects[key][0])
        return len(self.objects[key][0])

    def delete_object(self, key: str) -> None:
        self.objects.pop(key, None)


class ColdStorageTestMixin:
    media_root: str
    bucket: FakeBucket

    def setUp(self) -> None:
        super().setUp()  # type: ignore[misc]
        self.media_root = tempfile.mkdtemp(prefix="cold-storage-test-")
        self._media_override = override_settings(MEDIA_ROOT=self.media_root)
        self._media_override.enable()
        conf = settings.CRAWLER_SETTINGS.cold_storage
        self._saved_conf = conf.model_dump()
        for field, value in {
            "enable": True,
            "endpoint_url": "https://s3.test.invalid",
            "bucket": "cold-bucket",
            "access_key_id": "key",
            "secret_access_key": "secret",
            "key_prefix": "archives/",
            "local_prefix": "galleries/cold",
            "redirect_downloads": True,
        }.items():
            setattr(conf, field, value)
        self.bucket = FakeBucket()
        self._client_patch = mock.patch.object(cold_storage, "get_client", side_effect=self._get_client)
        self._client_patch.start()

    def _get_client(self) -> FakeBucket:
        if not settings.CRAWLER_SETTINGS.cold_storage.is_configured():
            raise ColdStorageError("not configured")
        return self.bucket

    def tearDown(self) -> None:
        self._client_patch.stop()
        conf = settings.CRAWLER_SETTINGS.cold_storage
        for field, value in self._saved_conf.items():
            setattr(conf, field, value)
        self._media_override.disable()
        shutil.rmtree(self.media_root, ignore_errors=True)
        super().tearDown()  # type: ignore[misc]

    def make_archive(self, name: str = "galleries/hath_dls/[Group] Title (C99).zip", content: bytes = b"zipdata" * 100,
                     **fields: typing.Any) -> Archive:
        path = os.path.join(self.media_root, name)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "wb") as f:
            f.write(content)
        owner, _ = User.objects.get_or_create(username="cold-owner")
        values: dict[str, typing.Any] = {"title": "Title", "crc32": "0A0B0C0D", "public": True, "user": owner}
        values.update(fields)
        archive = Archive(**values)
        archive.zipped.name = name
        archive.save()
        return archive


@override_settings(COMPRESS_OFFLINE=False, COMPRESS_ENABLED=False)
class ColdStorageServiceTests(ColdStorageTestMixin, TestCase):
    def test_names(self) -> None:
        cold = cold_storage.cold_name_for("galleries/a/b.zip")
        assert cold == "galleries/cold/galleries/a/b.zip"
        assert cold_storage.is_cold_name(cold)
        assert not cold_storage.is_cold_name("galleries/a/b.zip")
        assert not cold_storage.is_cold_name("galleries/colder/b.zip")
        assert not cold_storage.is_cold_name(None)
        settings.CRAWLER_SETTINGS.cold_storage.enable = False
        assert not cold_storage.is_cold_name(cold)
        settings.CRAWLER_SETTINGS.cold_storage.enable = True
        assert cold_storage.local_name_for(cold) == "galleries/a/b.zip"
        assert cold_storage.object_key_for(cold) == "archives/galleries/a/b.zip"
        with pytest.raises(ColdStorageError):
            cold_storage.cold_name_for(cold)
        with pytest.raises(ColdStorageError):
            cold_storage.local_name_for("galleries/a/b.zip")

    def test_offload_and_restore(self) -> None:
        archive = self.make_archive()
        local_path = os.path.join(self.media_root, "galleries/hath_dls/[Group] Title (C99).zip")

        result = ColdStorageService.offload(archive)

        assert not result.skipped
        archive.refresh_from_db()
        assert archive.zipped.name == "galleries/cold/galleries/hath_dls/[Group] Title (C99).zip"
        assert archive.is_cold_stored
        assert not os.path.exists(local_path)
        data, metadata = self.bucket.objects["archives/galleries/hath_dls/[Group] Title (C99).zip"]
        assert data == b"zipdata" * 100
        assert metadata == {"archive-id": str(archive.pk), "crc32": "0A0B0C0D"}

        again = ColdStorageService.offload(archive)
        assert again.skipped and again.message == "already offloaded"

        result = ColdStorageService.restore(archive)

        assert not result.skipped
        archive.refresh_from_db()
        assert archive.zipped.name == "galleries/hath_dls/[Group] Title (C99).zip"
        with open(local_path, "rb") as f:
            assert f.read() == b"zipdata" * 100
        assert self.bucket.objects == {}
        assert not os.path.exists(local_path + ".cold-part")

    def test_offload_keep_local_and_dry_run(self) -> None:
        archive = self.make_archive()
        dry = ColdStorageService.offload(archive, dry_run=True)
        assert dry.message == "dry run" and dry.new_name.startswith("galleries/cold/")
        archive.refresh_from_db()
        assert not archive.is_cold_stored
        assert self.bucket.objects == {}

        ColdStorageService.offload(archive, keep_local=True)
        assert os.path.exists(os.path.join(self.media_root, "galleries/hath_dls/[Group] Title (C99).zip"))
        archive.refresh_from_db()
        assert archive.is_cold_stored

        # Restore finds the local copy with the same size and doesn't download it.
        with mock.patch.object(self.bucket, "download_file") as download:
            ColdStorageService.restore(archive, keep_remote=True)
        download.assert_not_called()
        archive.refresh_from_db()
        assert not archive.is_cold_stored
        assert len(self.bucket.objects) == 1

    def test_offload_skips(self) -> None:
        no_crc = self.make_archive(name="galleries/a/no_crc.zip", crc32="")
        assert ColdStorageService.offload(no_crc).message == "no CRC32 yet"
        deleted = self.make_archive(name="galleries/a/deleted.zip", file_deleted=True)
        assert ColdStorageService.offload(deleted).message == "file marked as deleted"
        missing = self.make_archive(name="galleries/a/missing.zip")
        os.remove(os.path.join(self.media_root, "galleries/a/missing.zip"))
        assert ColdStorageService.offload(missing).message == "local file missing"
        assert self.bucket.objects == {}

    def test_offload_upload_failure_keeps_archive_local(self) -> None:
        archive = self.make_archive()
        self.bucket.fail_uploads = True
        with pytest.raises(S3Error):
            ColdStorageService.offload(archive)
        archive.refresh_from_db()
        assert not archive.is_cold_stored
        assert os.path.exists(os.path.join(self.media_root, archive.zipped.name or ""))

    def test_offload_detects_concurrent_rename(self) -> None:
        archive = self.make_archive()
        original_upload = self.bucket.upload_file

        def upload_and_rename(*args: typing.Any, **kwargs: typing.Any) -> str:
            etag = original_upload(*args, **kwargs)
            Archive.objects.filter(pk=archive.pk).update(zipped="galleries/hath_dls/renamed.zip")
            return etag

        with mock.patch.object(self.bucket, "upload_file", side_effect=upload_and_rename):
            with pytest.raises(ColdStorageError):
                ColdStorageService.offload(archive)
        archive.refresh_from_db()
        assert archive.zipped.name == "galleries/hath_dls/renamed.zip"
        assert os.path.exists(os.path.join(self.media_root, "galleries/hath_dls/[Group] Title (C99).zip"))

    def test_offload_reuses_object_left_by_interrupted_run(self) -> None:
        archive = self.make_archive()
        self.bucket.put_object(
            "archives/" + (archive.zipped.name or ""), b"zipdata" * 100, metadata={"crc32": "0a0b0c0d"}
        )
        with mock.patch.object(self.bucket, "upload_file") as upload:
            ColdStorageService.offload(archive)
        upload.assert_not_called()
        archive.refresh_from_db()
        assert archive.is_cold_stored

    def test_restore_errors(self) -> None:
        archive = self.make_archive()
        ColdStorageService.offload(archive)
        archive.refresh_from_db()
        # A different file now sits at the original path.
        local_path = os.path.join(self.media_root, "galleries/hath_dls/[Group] Title (C99).zip")
        with open(local_path, "wb") as f:
            f.write(b"other")
        with pytest.raises(ColdStorageError):
            ColdStorageService.restore(archive)
        os.remove(local_path)
        self.bucket.objects.clear()
        with pytest.raises(ColdStorageError):
            ColdStorageService.restore(archive)
        archive.refresh_from_db()
        assert archive.is_cold_stored

    def test_restore_short_download(self) -> None:
        archive = self.make_archive()
        ColdStorageService.offload(archive)
        archive.refresh_from_db()
        with mock.patch.object(self.bucket, "download_file", return_value=3):
            with pytest.raises(ColdStorageError):
                ColdStorageService.restore(archive)
        archive.refresh_from_db()
        assert archive.is_cold_stored
        local_path = os.path.join(self.media_root, "galleries/hath_dls/[Group] Title (C99).zip")
        assert not os.path.exists(local_path)
        assert not os.path.exists(local_path + ".cold-part")


@override_settings(COMPRESS_OFFLINE=False, COMPRESS_ENABLED=False)
class ColdStorageModelTests(ColdStorageTestMixin, TestCase):
    def test_delete_all_files_deletes_object(self) -> None:
        archive = self.make_archive()
        ColdStorageService.offload(archive)
        archive.refresh_from_db()
        archive.delete_all_files(create_mark=True)
        archive.refresh_from_db()
        assert archive.file_deleted
        assert self.bucket.objects == {}

    def test_delete_all_files_needs_configured_bucket(self) -> None:
        archive = self.make_archive()
        ColdStorageService.offload(archive)
        archive.refresh_from_db()
        settings.CRAWLER_SETTINGS.cold_storage.secret_access_key = ""
        with pytest.raises(ColdStorageError):
            archive.delete_all_files()
        archive.refresh_from_db()
        assert not archive.file_deleted
        assert len(self.bucket.objects) == 1

    def test_public_private_and_rename(self) -> None:
        archive = self.make_archive()
        ColdStorageService.offload(archive)
        archive.refresh_from_db()
        assert archive.has_zipped_file()
        archive.set_private()
        archive.refresh_from_db()
        assert not archive.public
        cold_name = archive.zipped.name
        assert not archive.rename_zipped_pathname("galleries/other.zip")
        assert not archive.rename_zipped_tail("other.zip")
        archive.refresh_from_db()
        assert archive.zipped.name == cold_name


@override_settings(COMPRESS_OFFLINE=False, COMPRESS_ENABLED=False)
class ColdStorageDownloadTests(ColdStorageTestMixin, TestCase):
    def setUp(self) -> None:
        super().setUp()
        self.user = User.objects.create_user(username="cold-user", password="password123")
        self.client = Client()

    def offloaded(self, **fields: typing.Any) -> Archive:
        archive = self.make_archive(**fields)
        ColdStorageService.offload(archive)
        archive.refresh_from_db()
        return archive

    def test_download_redirects_to_presigned_url(self) -> None:
        archive = self.offloaded()
        response = self.client.get(reverse("viewer:archive-download", args=[archive.pk]), HTTP_X_FORWARDED_HOST="x")
        assert response.status_code == 302
        assert response["Cache-Control"] == "private, no-store"
        parts = urlsplit(response["Location"])
        assert parts.netloc == "s3.test.invalid"
        assert parts.path == "/cold-bucket/archives/galleries/hath_dls/%5BGroup%5D%20Title%20%28C99%29.zip"
        query = parse_qs(parts.query)
        assert query["response-content-disposition"] == ["attachment; filename*=UTF-8''Title.zip"]
        assert query["response-content-type"] == ["application/vnd.comicbook+zip"]
        assert query["X-Amz-Expires"] == [str(settings.CRAWLER_SETTINGS.cold_storage.presigned_url_expiry)]

        original = self.client.get(reverse("viewer:archive-download", args=[archive.pk]) + "?original")
        query = parse_qs(urlsplit(original["Location"]).query)
        assert query["response-content-disposition"] == [
            "attachment; filename*=UTF-8''%5BGroup%5D%20Title%20%28C99%29.zip"
        ]

        ext = self.client.get(reverse("viewer:archive-ext-download", args=[archive.pk]))
        assert ext.status_code == 302
        assert urlsplit(ext["Location"]).netloc == "s3.test.invalid"

    def test_private_archive_needs_login(self) -> None:
        archive = self.offloaded(public=False)
        url = reverse("viewer:archive-download", args=[archive.pk])
        assert self.client.get(url).status_code == 404
        self.client.login(username="cold-user", password="password123")
        assert self.client.get(url).status_code == 302

    def test_local_archive_unchanged(self) -> None:
        archive = self.make_archive()
        response = self.client.get(reverse("viewer:archive-download", args=[archive.pk]), HTTP_X_FORWARDED_HOST="x")
        assert response.status_code == 200
        assert response["X-Accel-Redirect"] == "/download/galleries/hath_dls/%5BGroup%5D%20Title%20%28C99%29.zip"

    def test_redirect_disabled_uses_x_accel_through_mount(self) -> None:
        archive = self.offloaded()
        settings.CRAWLER_SETTINGS.cold_storage.redirect_downloads = False
        response = self.client.get(reverse("viewer:archive-download", args=[archive.pk]), HTTP_X_FORWARDED_HOST="x")
        assert response.status_code == 200
        assert response["X-Accel-Redirect"].startswith("/download/galleries/cold/galleries/hath_dls/")

    def test_deleted_file_not_redirected(self) -> None:
        archive = self.offloaded()
        Archive.objects.filter(pk=archive.pk).update(file_deleted=True)
        response = self.client.get(reverse("viewer:archive-download", args=[archive.pk]), HTTP_X_FORWARDED_HOST="x")
        assert "Location" not in response


@override_settings(COMPRESS_OFFLINE=False, COMPRESS_ENABLED=False)
class ColdStorageCommandTests(ColdStorageTestMixin, TestCase):
    def run_command(self, *args: str) -> str:
        out = io.StringIO()
        call_command("archive_cold_storage", *args, stdout=out, stderr=out)
        return out.getvalue()

    def test_offload_selection_and_restore(self) -> None:
        hath = self.make_archive(name="galleries/hath_dls/a.zip")
        torrent = self.make_archive(name="galleries/torrent_dls/b.zip")
        Archive.objects.filter(pk=torrent.pk).update(
            create_date=datetime.datetime(2020, 1, 1, tzinfo=datetime.timezone.utc)
        )

        output = self.run_command("offload", "--path-prefix", "galleries/hath_dls/", "--dry-run")
        assert "Would offloaded" not in output
        assert "Would offload" in output and "a.zip" in output and "b.zip" not in output
        assert self.bucket.objects == {}

        output = self.run_command("offload", "--older-than-days", "30")
        assert "Offloaded: 1 archives" in output
        torrent.refresh_from_db()
        hath.refresh_from_db()
        assert torrent.is_cold_stored and not hath.is_cold_stored
        assert EventLog.objects.filter(action="COLD_STORAGE_OFFLOAD", object_id=torrent.pk).exists()

        output = self.run_command("offload", "--limit", "5")
        assert "Offloaded: 1 archives" in output
        hath.refresh_from_db()
        assert hath.is_cold_stored

        output = self.run_command("check")
        assert "2 in the bucket" in output
        self.bucket.objects.pop("archives/galleries/hath_dls/a.zip")
        output = self.run_command("check")
        assert "1 missing" in output

        output = self.run_command("restore", "--pk", str(torrent.pk))
        assert "Restored: 1 archives" in output
        torrent.refresh_from_db()
        assert not torrent.is_cold_stored
        assert os.path.isfile(os.path.join(self.media_root, "galleries/torrent_dls/b.zip"))

    def test_pk_file_and_status(self) -> None:
        first = self.make_archive(name="galleries/x/1.zip")
        second = self.make_archive(name="galleries/x/2.zip")
        pk_file = os.path.join(self.media_root, "pks.txt")
        with open(pk_file, "w") as f:
            f.write("# cold candidates\n{}\n".format(second.pk))
        self.run_command("offload", "--pk-file", pk_file)
        first.refresh_from_db()
        second.refresh_from_db()
        assert second.is_cold_stored and not first.is_cold_stored
        output = self.run_command("status")
        assert "Offloaded archives: 1 of 2" in output

    def test_status_verify(self) -> None:
        def presigned_get(url: str, timeout: float) -> mock.Mock:
            key = next(k for k in self.bucket.objects if k.split("/")[-1] in url)
            return mock.Mock(status_code=200, content=self.bucket.objects[key][0], text="")

        with mock.patch("requests.get", side_effect=presigned_get) as get:
            output = self.run_command("status", "--verify")
        assert "Bucket test passed" in output
        assert get.call_count == 1
        assert self.bucket.objects == {}  # the test object is deleted

        with mock.patch("requests.get", return_value=mock.Mock(status_code=403, content=b"", text="denied")):
            from django.core.management.base import CommandError

            with pytest.raises(CommandError, match="HTTP 403"):
                self.run_command("status", "--verify")
        assert self.bucket.objects == {}

    def test_status_verify_flag_does_not_look_like_a_test_run(self) -> None:
        # pandabackup/settings.py switches to test settings when any argument contains "test" (on a DEBUG install
        # that breaks the URL checks before the command runs), so the documented flag must not.
        from viewer.management.commands.archive_cold_storage import Command

        parser = Command().create_parser("manage.py", "archive_cold_storage")
        subcommands = [action.choices for action in parser._actions if isinstance(action.choices, dict)]
        status = subcommands[0]["status"]
        assert status.parse_args(["--verify"]).verify
        flags = [flag for action in status._actions for flag in action.option_strings]
        assert "--verify" in flags
        assert not [flag for flag in flags if "test" in flag]

    def test_not_configured(self) -> None:
        from django.core.management.base import CommandError

        settings.CRAWLER_SETTINGS.cold_storage.enable = False
        with pytest.raises(CommandError):
            self.run_command("offload")
        with pytest.raises(CommandError):
            self.run_command("restore")


def make_manager(username: str, staff: bool = False) -> User:
    from django.contrib.auth.models import Permission

    user = User.objects.create_user(username=username, password="password123", is_staff=staff)
    user.user_permissions.add(Permission.objects.get(codename="manage_archive", content_type__app_label="viewer"))
    return User.objects.get(pk=user.pk)


class ColdStorageArchiveManagerTests(ColdStorageTestMixin, TestCase):
    """Offload/restore actions, storage filter and info in the archive manager: staff only, cold storage enabled."""

    def setUp(self) -> None:
        super().setUp()
        self.staff = make_manager("cold-staff", staff=True)
        self.manager = make_manager("cold-manager")
        self.local = self.make_archive("galleries/a.zip", title="Local")
        self.cold = self.make_archive("galleries/b.zip", title="Cold")
        ColdStorageService.offload(self.cold)
        self.url = reverse("viewer:archive-manager")

    def listing(self, user: User, **params: str) -> dict[str, typing.Any]:
        self.client.force_login(user)
        response = self.client.get(self.url, {"json": "1", **params})
        assert response.status_code == 200
        return response.json()

    def post_action(self, user: User, action: str, *archives: Archive, **extra: str) -> typing.Any:
        self.client.force_login(user)
        data = {action: "1", **{"sel-{}".format(a.pk): str(a.pk) for a in archives}, **extra}
        return self.client.post(self.url + "?json=1", data)

    def test_listing_info_and_storage_filter(self) -> None:
        data = self.listing(self.staff)
        assert data["cold_storage"] == {"enabled": True}
        assert {r["title"]: r["cold_stored"] for r in data["results"]} == {"Local": False, "Cold": True}
        assert [r["title"] for r in self.listing(self.staff, storage="cold")["results"]] == ["Cold"]
        assert [r["title"] for r in self.listing(self.staff, storage="local")["results"]] == ["Local"]

        # Not for non-staff: no tools, and the filter is ignored.
        assert self.listing(self.manager)["cold_storage"] == {"enabled": False}
        assert len(self.listing(self.manager, storage="cold")["results"]) == 2

        # Nor when the cold storage isn't configured.
        settings.CRAWLER_SETTINGS.cold_storage.secret_access_key = ""
        assert self.listing(self.staff)["cold_storage"] == {"enabled": False}

    def test_actions_queue_one_task_per_eligible_archive(self) -> None:
        from workers.models import TaskQueue

        deleted = self.make_archive("galleries/c.zip", title="Deleted", file_deleted=True)
        response = self.post_action(self.staff, "cold_storage_offload", self.local, self.cold, deleted, reason="old")
        task = TaskQueue.objects.get(task_type=cold_storage.OFFLOAD_TASK)
        assert task.args == [self.local.pk]
        assert task.reason == "old"
        assert task.user == self.staff
        assert task.event_action == cold_storage.OFFLOAD_EVENT
        # The queued tasks are returned so the frontend can follow them.
        assert response.json()["task_ids"] == [task.pk]
        assert "status_url" in response.json()

        # Asking again while the task is pending returns the same task.
        response = self.post_action(self.staff, "cold_storage_offload", self.local)
        assert response.json()["task_ids"] == [task.pk]
        assert TaskQueue.objects.filter(task_type=cold_storage.OFFLOAD_TASK).count() == 1

        self.post_action(self.staff, "cold_storage_restore", self.local, self.cold)
        assert TaskQueue.objects.get(task_type=cold_storage.RESTORE_TASK).args == [self.cold.pk]

        # Run for all follows the listing filters.
        TaskQueue.objects.all().delete()
        self.client.post(self.url + "?json=1&storage=cold", {"cold_storage_restore": "1", "run_for_all": "1"})
        assert list(TaskQueue.objects.values_list("args", flat=True)) == [[self.cold.pk]]

    def test_actions_need_staff_and_configured_storage(self) -> None:
        from workers.models import TaskQueue

        self.post_action(self.manager, "cold_storage_offload", self.local)
        assert not TaskQueue.objects.exists()

        settings.CRAWLER_SETTINGS.cold_storage.enable = False
        response = self.post_action(self.staff, "cold_storage_offload", self.local)
        assert response.status_code == 400
        assert not TaskQueue.objects.exists()

    def test_worker_task_transfers_and_logs(self) -> None:
        result = cold_storage.run_transfer_task(self.local.pk, restore=False, user=self.staff, reason="old")
        assert result is not None and not result.skipped
        self.local.refresh_from_db()
        assert self.local.is_cold_stored
        event = EventLog.objects.get(action=cold_storage.OFFLOAD_EVENT, object_id=self.local.pk)
        assert event.user == self.staff
        assert event.reason == "old"

        result = cold_storage.run_transfer_task(self.cold.pk, restore=True, user=self.staff)
        assert result is not None and not result.skipped
        self.cold.refresh_from_db()
        assert not self.cold.is_cold_stored
        assert os.path.isfile(os.path.join(self.media_root, "galleries/b.zip"))
        assert EventLog.objects.filter(action=cold_storage.RESTORE_EVENT, object_id=self.cold.pk).exists()

        # Nothing to do: skipped, not logged.
        result = cold_storage.run_transfer_task(self.cold.pk, restore=True, user=self.staff)
        assert result is not None and result.skipped
        assert EventLog.objects.filter(action=cold_storage.RESTORE_EVENT).count() == 1
        assert cold_storage.run_transfer_task(999999, restore=False) is None

    def test_worker_task_failure_raises(self) -> None:
        self.bucket.fail_uploads = True
        with pytest.raises(ColdStorageError):
            cold_storage.run_transfer_task(self.local.pk, restore=False)
        self.local.refresh_from_db()
        assert not self.local.is_cold_stored

    def test_runner_dispatches_transfer_tasks(self) -> None:
        from core.base.setup import Settings
        from workers import client
        from workers.models import TaskQueue
        from workers.runner import WorkerRunner

        client.enqueue(args=[self.local.pk], task_type=cold_storage.OFFLOAD_TASK, user=self.staff)
        runner = WorkerRunner(Settings(load_from_disk=False), enable_schedulers=False)
        claimed = runner.claim_next_job()
        assert claimed is not None
        with (
            mock.patch("django_db_logger.db_log_handler.DatabaseLogHandler.emit"),
            mock.patch.object(cold_storage, "run_transfer_task") as transfer,
        ):
            runner.execute_job(claimed)
        claimed.refresh_from_db()
        assert claimed.status == TaskQueue.Status.COMPLETED
        transfer.assert_called_once()
        assert transfer.call_args.args == (self.local.pk,)
        assert transfer.call_args.kwargs["restore"] is False
        assert transfer.call_args.kwargs["user"] == self.staff

    def test_activity_progress_is_throttled(self) -> None:
        calls: list[int] = []
        progress = cold_storage._activity_progress(lambda: calls.append(1), interval=3600)
        assert progress is not None
        for _ in range(5):
            progress(1024)
        assert len(calls) == 1
        assert cold_storage._activity_progress(None) is None

    def test_clean_filter_storage(self) -> None:
        from viewer.services.archive_selection import clean_filter

        assert clean_filter({"storage": "cold"}, self.staff) == {"storage": "cold"}
        assert clean_filter({"storage": "cold"}, self.manager) == {}
        with pytest.raises(ValueError):
            clean_filter({"storage": "elsewhere"}, self.staff)
