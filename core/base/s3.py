"""Minimal client for S3-compatible object storage (OVH, Backblaze B2, MinIO, Hetzner, AWS...).

Only the operations needed by the archive cold storage are implemented: presigned GET URLs, HEAD, single and multipart
uploads, (ranged) downloads and deletes. Requests are signed with AWS Signature Version 4 and sent with ``requests``,
which avoids pulling in boto3 (botocore pins ``python-dateutil<3``, which conflicts with this project's pin).

Uploads sign the SHA-256 of every body and send its Content-MD5, so the server rejects any part that was corrupted in
transit.
"""

from __future__ import annotations

import base64
import datetime
import hashlib
import hmac
import logging
import os
import typing
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from urllib.parse import quote, urlsplit

import requests

logger = logging.getLogger(__name__)

ALGORITHM = "AWS4-HMAC-SHA256"
UNSIGNED_PAYLOAD = "UNSIGNED-PAYLOAD"
EMPTY_SHA256 = hashlib.sha256(b"").hexdigest()

# S3 limits: parts are 5 MiB..5 GiB (except the last one), at most 10000 parts, presigned URLs last 7 days at most.
MIN_PART_SIZE = 5 * 1024 * 1024
MAX_PART_SIZE = 5 * 1024 * 1024 * 1024
MAX_PARTS = 10000
MAX_PRESIGN_EXPIRY = 7 * 24 * 3600

ProgressCallback = typing.Callable[[int], None]


class S3Error(Exception):
    """An S3 request failed. ``status`` is the HTTP status (0 when no response was received)."""

    def __init__(self, message: str, status: int = 0, code: str = "") -> None:
        super().__init__(message)
        self.status = status
        self.code = code


@dataclass
class S3ObjectInfo:
    key: str
    size: int
    etag: str
    last_modified: str = ""
    content_type: str = ""
    metadata: dict[str, str] = field(default_factory=dict)


def _uri_encode(value: str, safe: str = "") -> str:
    """RFC 3986 encoding as SigV4 expects it: only ``A-Za-z0-9-._~`` (plus ``safe``) are left as is."""
    return quote(value, safe=safe)


def _hmac(key: bytes, msg: str) -> bytes:
    return hmac.new(key, msg.encode("utf-8"), hashlib.sha256).digest()


def _strip_ns(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def _find_text(root: ET.Element, name: str) -> str:
    for element in root.iter():
        if _strip_ns(element.tag) == name:
            return element.text or ""
    return ""


def _parse_error(content: bytes) -> tuple[str, str]:
    """Return (code, message) of an S3 XML error body, or empty strings if it isn't one."""
    if not content:
        return "", ""
    try:
        root = ET.fromstring(content)
    except ET.ParseError:
        return "", ""
    if _strip_ns(root.tag) != "Error":
        return "", ""
    return _find_text(root, "Code"), _find_text(root, "Message")


def multipart_etag(md5_digests: list[bytes]) -> str:
    """ETag S3 gives a multipart upload: MD5 of the concatenated part MD5s, plus the part count."""
    return '"{}-{}"'.format(hashlib.md5(b"".join(md5_digests)).hexdigest(), len(md5_digests))


def choose_part_size(file_size: int, part_size: int) -> int:
    """Grow ``part_size`` (clamped to the S3 limits) until ``file_size`` fits in MAX_PARTS parts."""
    part_size = max(MIN_PART_SIZE, min(part_size, MAX_PART_SIZE))
    while part_size * MAX_PARTS < file_size and part_size < MAX_PART_SIZE:
        part_size = min(part_size * 2, MAX_PART_SIZE)
    return part_size


class S3Client:
    """Client for one bucket of an S3-compatible service.

    ``addressing_style`` is ``"path"`` (``https://endpoint/bucket/key``, supported by every provider) or
    ``"virtual"`` (``https://bucket.endpoint/key``).
    """

    def __init__(
        self,
        endpoint_url: str,
        bucket: str,
        access_key_id: str,
        secret_access_key: str,
        region: str = "us-east-1",
        addressing_style: str = "path",
        timeout: float = 60.0,
        session: requests.Session | None = None,
    ) -> None:
        if not endpoint_url:
            raise ValueError("An S3 endpoint URL is required")
        if not bucket:
            raise ValueError("An S3 bucket name is required")
        parts = urlsplit(endpoint_url if "://" in endpoint_url else "https://" + endpoint_url)
        if parts.scheme not in ("http", "https") or not parts.hostname:
            raise ValueError("Invalid S3 endpoint URL: {}".format(endpoint_url))
        if addressing_style not in ("path", "virtual"):
            raise ValueError("addressing_style must be 'path' or 'virtual'")
        self.scheme = parts.scheme
        self.endpoint_netloc = parts.netloc
        self.bucket = bucket
        self.access_key_id = access_key_id
        self.secret_access_key = secret_access_key
        self.region = region or "us-east-1"
        self.addressing_style = addressing_style
        self.timeout = timeout
        self.session = session or requests.Session()

    # ------------------------------------------------------------------ signing

    def _host_and_path(self, key: str) -> tuple[str, str]:
        encoded_key = _uri_encode(key, safe="/")
        if self.addressing_style == "virtual":
            return "{}.{}".format(self.bucket, self.endpoint_netloc), "/" + encoded_key
        path = "/" + _uri_encode(self.bucket)
        if key:
            path += "/" + encoded_key
        return self.endpoint_netloc, path

    def _signing_key(self, date_stamp: str) -> bytes:
        k_date = _hmac(("AWS4" + self.secret_access_key).encode("utf-8"), date_stamp)
        k_region = _hmac(k_date, self.region)
        k_service = _hmac(k_region, "s3")
        return _hmac(k_service, "aws4_request")

    @staticmethod
    def _canonical_query(params: dict[str, str]) -> str:
        return "&".join(
            "{}={}".format(_uri_encode(k), _uri_encode(v))
            for k, v in sorted(params.items(), key=lambda kv: (_uri_encode(kv[0]), _uri_encode(kv[1])))
        )

    def _signature(
        self,
        method: str,
        path: str,
        canonical_query: str,
        headers: dict[str, str],
        payload_hash: str,
        amz_date: str,
    ) -> tuple[str, str, str]:
        """Return (signature, signed_headers, credential_scope)."""
        date_stamp = amz_date[:8]
        lowered = {k.lower().strip(): " ".join(str(v).strip().split()) for k, v in headers.items()}
        signed_headers = ";".join(sorted(lowered))
        canonical_headers = "".join("{}:{}\n".format(k, lowered[k]) for k in sorted(lowered))
        canonical_request = "\n".join(
            [method, path, canonical_query, canonical_headers, signed_headers, payload_hash]
        )
        scope = "{}/{}/s3/aws4_request".format(date_stamp, self.region)
        string_to_sign = "\n".join(
            [ALGORITHM, amz_date, scope, hashlib.sha256(canonical_request.encode("utf-8")).hexdigest()]
        )
        signature = hmac.new(
            self._signing_key(date_stamp), string_to_sign.encode("utf-8"), hashlib.sha256
        ).hexdigest()
        return signature, signed_headers, scope

    @staticmethod
    def _now() -> datetime.datetime:
        return datetime.datetime.now(datetime.timezone.utc)

    def presigned_get_url(
        self,
        key: str,
        expires: int = 3600,
        response_headers: dict[str, str] | None = None,
        now: datetime.datetime | None = None,
    ) -> str:
        """URL that allows a GET (and Range GETs) of ``key`` without credentials for ``expires`` seconds.

        ``response_headers`` are S3 response overrides, e.g. ``{"response-content-disposition": "attachment"}``.
        """
        expires = max(1, min(int(expires), MAX_PRESIGN_EXPIRY))
        amz_date = (now or self._now()).strftime("%Y%m%dT%H%M%SZ")
        host, path = self._host_and_path(key)
        params = {
            "X-Amz-Algorithm": ALGORITHM,
            "X-Amz-Credential": "{}/{}/{}/s3/aws4_request".format(self.access_key_id, amz_date[:8], self.region),
            "X-Amz-Date": amz_date,
            "X-Amz-Expires": str(expires),
            "X-Amz-SignedHeaders": "host",
        }
        params.update(response_headers or {})
        canonical_query = self._canonical_query(params)
        signature, _, _ = self._signature("GET", path, canonical_query, {"host": host}, UNSIGNED_PAYLOAD, amz_date)
        return "{}://{}{}?{}&X-Amz-Signature={}".format(self.scheme, host, path, canonical_query, signature)

    # ------------------------------------------------------------------ requests

    def _request(
        self,
        method: str,
        key: str,
        params: dict[str, str] | None = None,
        headers: dict[str, str] | None = None,
        body: bytes = b"",
        stream: bool = False,
        expected: tuple[int, ...] = (200,),
    ) -> requests.Response:
        params = params or {}
        headers = dict(headers or {})
        host, path = self._host_and_path(key)
        payload_hash = hashlib.sha256(body).hexdigest() if body else EMPTY_SHA256
        amz_date = self._now().strftime("%Y%m%dT%H%M%SZ")
        headers["host"] = host
        headers["x-amz-date"] = amz_date
        headers["x-amz-content-sha256"] = payload_hash
        if body:
            headers["content-md5"] = base64.b64encode(hashlib.md5(body).digest()).decode("ascii")
        canonical_query = self._canonical_query(params)
        signature, signed_headers, scope = self._signature(
            method, path, canonical_query, headers, payload_hash, amz_date
        )
        headers["authorization"] = "{} Credential={}/{}, SignedHeaders={}, Signature={}".format(
            ALGORITHM, self.access_key_id, scope, signed_headers, signature
        )
        url = "{}://{}{}".format(self.scheme, host, path)
        if canonical_query:
            url += "?" + canonical_query
        try:
            response = self.session.request(
                method, url, headers=headers, data=body or None, stream=stream, timeout=self.timeout
            )
        except requests.RequestException as e:
            raise S3Error("{} {} failed: {}".format(method, key, e)) from e
        if response.status_code not in expected:
            content = b"" if method == "HEAD" else response.content
            code, message = _parse_error(content)
            response.close()
            raise S3Error(
                "{} {} returned HTTP {}{}".format(
                    method, key, response.status_code, ": {} {}".format(code, message).rstrip() if code else ""
                ),
                status=response.status_code,
                code=code,
            )
        return response

    def head_object(self, key: str) -> S3ObjectInfo | None:
        """Object info, or None if it doesn't exist."""
        try:
            response = self._request("HEAD", key)
        except S3Error as e:
            if e.status == 404:
                return None
            raise
        return S3ObjectInfo(
            key=key,
            size=int(response.headers.get("Content-Length", "0")),
            etag=response.headers.get("ETag", ""),
            last_modified=response.headers.get("Last-Modified", ""),
            content_type=response.headers.get("Content-Type", ""),
            metadata={
                k.lower().removeprefix("x-amz-meta-"): v
                for k, v in response.headers.items()
                if k.lower().startswith("x-amz-meta-")
            },
        )

    def delete_object(self, key: str) -> None:
        # Deleting a missing key isn't an error in S3 (204), some providers answer 404.
        self._request("DELETE", key, expected=(200, 204, 404))

    def put_object(
        self, key: str, data: bytes, content_type: str = "application/octet-stream", metadata: dict[str, str] | None = None
    ) -> str:
        headers = {"content-type": content_type}
        headers.update(self._metadata_headers(metadata))
        response = self._request("PUT", key, headers=headers, body=data)
        return response.headers.get("ETag", "")

    @staticmethod
    def _metadata_headers(metadata: dict[str, str] | None) -> dict[str, str]:
        # Metadata values must be ASCII in HTTP headers; callers pass ids and hashes.
        return {"x-amz-meta-{}".format(k.lower()): str(v) for k, v in (metadata or {}).items()}

    def upload_file(
        self,
        path: str,
        key: str,
        content_type: str = "application/octet-stream",
        metadata: dict[str, str] | None = None,
        part_size: int = 64 * 1024 * 1024,
        multipart_threshold: int = 64 * 1024 * 1024,
        progress: ProgressCallback | None = None,
    ) -> str:
        """Upload a local file, as a multipart upload when it's larger than ``multipart_threshold``.

        Returns the ETag the server reported. Each part is read into memory, so memory use is about ``part_size``.
        """
        file_size = os.path.getsize(path)
        if file_size <= max(multipart_threshold, 0) or file_size <= MIN_PART_SIZE:
            with open(path, "rb") as f:
                data = f.read()
            etag = self.put_object(key, data, content_type=content_type, metadata=metadata)
            if progress:
                progress(len(data))
            return etag

        part_size = choose_part_size(file_size, part_size)
        headers = {"content-type": content_type}
        headers.update(self._metadata_headers(metadata))
        response = self._request("POST", key, params={"uploads": ""}, headers=headers)
        upload_id = _find_text(ET.fromstring(response.content), "UploadId")
        if not upload_id:
            raise S3Error("CreateMultipartUpload for {} returned no UploadId".format(key))

        parts: list[tuple[int, str]] = []
        md5s: list[bytes] = []
        try:
            with open(path, "rb") as f:
                part_number = 1
                while chunk := f.read(part_size):
                    response = self._request(
                        "PUT",
                        key,
                        params={"partNumber": str(part_number), "uploadId": upload_id},
                        body=chunk,
                    )
                    parts.append((part_number, response.headers.get("ETag", "")))
                    md5s.append(hashlib.md5(chunk).digest())
                    if progress:
                        progress(len(chunk))
                    part_number += 1
            body = "<CompleteMultipartUpload>{}</CompleteMultipartUpload>".format(
                "".join(
                    "<Part><PartNumber>{}</PartNumber><ETag>{}</ETag></Part>".format(number, etag)
                    for number, etag in parts
                )
            ).encode("utf-8")
            response = self._request("POST", key, params={"uploadId": upload_id}, body=body)
            # CompleteMultipartUpload can fail after answering 200, with the error in the body.
            code, message = _parse_error(response.content)
            if code:
                raise S3Error("CompleteMultipartUpload for {} failed: {} {}".format(key, code, message), code=code)
            etag = _find_text(ET.fromstring(response.content), "ETag")
        except BaseException:
            try:
                self._request("DELETE", key, params={"uploadId": upload_id}, expected=(200, 204, 404))
            except S3Error as abort_error:
                logger.warning("Could not abort multipart upload %s of %s: %s", upload_id, key, abort_error)
            raise
        expected_etag = multipart_etag(md5s)
        if etag and etag.strip('"').lower() != expected_etag.strip('"').lower() and "-" in etag:
            logger.warning("Multipart ETag of %s is %s, expected %s", key, etag, expected_etag)
        return etag

    def get_object(self, key: str, byte_range: tuple[int, int] | None = None) -> requests.Response:
        """Streaming response for ``key`` (the caller must close it). ``byte_range`` is inclusive."""
        headers = {}
        if byte_range is not None:
            headers["range"] = "bytes={}-{}".format(byte_range[0], byte_range[1])
        return self._request("GET", key, headers=headers, stream=True, expected=(200, 206))

    def download_file(
        self, key: str, path: str, chunk_size: int = 1024 * 1024, progress: ProgressCallback | None = None
    ) -> int:
        """Download ``key`` to ``path``, returning the number of bytes written."""
        written = 0
        response = self.get_object(key)
        try:
            with open(path, "wb") as f:
                for chunk in response.iter_content(chunk_size=chunk_size):
                    if not chunk:
                        continue
                    f.write(chunk)
                    written += len(chunk)
                    if progress:
                        progress(len(chunk))
        except requests.RequestException as e:
            raise S3Error("GET {} failed while reading: {}".format(key, e)) from e
        finally:
            response.close()
        return written
