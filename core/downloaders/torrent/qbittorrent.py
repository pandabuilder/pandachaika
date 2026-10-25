import base64
import hashlib
import logging
import os
import re
from typing import Any
from urllib.parse import unquote, urlparse

import requests
from requests.exceptions import RequestException

from core.base.types import TorrentClient
from core.libs.bencoding import Decoder, Encoder

logger = logging.getLogger(__name__)


class qBittorrent(TorrentClient):

    name = "qbittorrent"
    convert_to_base64 = False
    send_url = False

    def __init__(
        self,
        address: str = "localhost",
        port: int = 8080,
        user: str = "",
        password: str = "",
        secure: bool = True,
    ) -> None:
        super().__init__(address=address, port=port, user=user, password=password, secure=secure)
        self.session: requests.Session | None = None
        self.base_url: str = ""
        self.error: str = ""

    def __str__(self) -> str:
        return self.name

    def _build_base_url(self) -> str:
        raw_address = self.address.strip()
        if not raw_address.startswith(("http://", "https://")):
            raw_address = f"http://{raw_address}"
        parsed = urlparse(raw_address)
        scheme = parsed.scheme or "http"
        hostname = parsed.hostname or "localhost"
        try:
            port = parsed.port if parsed.port is not None else int(self.port)
        except (ValueError, TypeError):
            port = 8080
        path = parsed.path.rstrip("/")
        if (scheme == "http" and port == 80 and parsed.port is None) or (
            scheme == "https" and port == 443 and parsed.port is None
        ):
            return f"{scheme}://{hostname}{path}"
        return f"{scheme}://{hostname}:{port}{path}"

    def connect(self) -> bool:
        self.error = ""
        try:
            self.base_url = self._build_base_url()
            session = requests.Session()
            session.verify = self.secure

            if self.user or self.password:
                login_url = f"{self.base_url}/api/v2/auth/login"
                login_data = {"username": self.user, "password": self.password}
                resp = session.post(login_url, data=login_data, timeout=25)
                if resp.status_code == 200 and resp.text.strip() == "Ok.":
                    self.session = session
                    return True
                elif resp.text.strip() == "Fails.":
                    self.error = "Authentication failed: invalid username or password"
                    return False
                elif resp.status_code == 403:
                    self.error = f"Authentication forbidden (HTTP 403): {resp.text}"
                    return False

            # Test connection if no credentials, or verify session
            test_url = f"{self.base_url}/api/v2/app/version"
            test_resp = session.get(test_url, timeout=25)
            if test_resp.status_code == 200:
                self.session = session
                return True
            elif test_resp.status_code in (401, 403):
                self.error = f"Authentication required or forbidden (HTTP {test_resp.status_code})"
                return False
            else:
                self.error = f"Unexpected response from qBittorrent: HTTP {test_resp.status_code}"
                return False
        except RequestException as e:
            self.error = str(e)
            logger.warning(f"Failed to connect to qBittorrent: {e}")
            return False

    def _extract_torrent_metadata(
        self, raw_bytes: bytes
    ) -> tuple[str | None, str, str, int]:
        """
        Parses bencoded torrent bytes.
        Returns (info_hash, torrent_name, extension, total_size).
        """
        info_hash: str | None = None
        torrent_name = ""
        torrent_ext = ""
        total_size = 0

        try:
            decoded = Decoder(raw_bytes).decode()
            if isinstance(decoded, dict) and b"info" in decoded:
                info_dict = decoded[b"info"]

                info_idx = raw_bytes.find(b"4:info")
                if info_idx != -1:
                    d = Decoder(raw_bytes[info_idx + 6 :])
                    d.decode()
                    info_raw = raw_bytes[info_idx + 6 : info_idx + 6 + d._index]
                    info_hash = hashlib.sha1(info_raw).hexdigest().lower()
                else:
                    info_raw = bytes(Encoder(info_dict).encode())
                    info_hash = hashlib.sha1(info_raw).hexdigest().lower()

                raw_name = info_dict.get(b"name", b"")
                if isinstance(raw_name, bytes):
                    torrent_name = raw_name.decode("utf-8", errors="replace")
                elif isinstance(raw_name, str):
                    torrent_name = raw_name

                if b"files" in info_dict and isinstance(info_dict[b"files"], list):
                    files_list = info_dict[b"files"]
                    total_size = sum(
                        int(f.get(b"length", 0)) for f in files_list if isinstance(f, dict)
                    )
                    if len(files_list) == 1:
                        single_file = files_list[0]
                        file_path_parts = single_file.get(b"path", [])
                        if file_path_parts and isinstance(file_path_parts[-1], bytes):
                            single_filename = file_path_parts[-1].decode("utf-8", errors="replace")
                            name_split = os.path.splitext(single_filename)
                            torrent_name = name_split[0]
                            torrent_ext = name_split[1]
                        else:
                            name_split = os.path.splitext(torrent_name)
                            torrent_name = name_split[0]
                            torrent_ext = name_split[1]
                    else:
                        torrent_ext = ""
                elif b"length" in info_dict:
                    total_size = int(info_dict.get(b"length", 0))
                    name_split = os.path.splitext(torrent_name)
                    torrent_name = name_split[0]
                    torrent_ext = name_split[1]
        except Exception as e:
            logger.debug(f"Failed to parse torrent bencode metadata: {e}")

        return info_hash, torrent_name, torrent_ext, total_size

    def add_torrent(
        self, torrent_data: str | bytes, download_dir: str | None = None
    ) -> tuple[bool, str | None]:
        if not self.session:
            if not self.connect():
                return False, None

        raw_bytes: bytes
        if isinstance(torrent_data, str):
            if os.path.isfile(torrent_data):
                try:
                    with open(torrent_data, "rb") as f:
                        raw_bytes = f.read()
                except OSError as e:
                    self.error = f"Failed to read torrent file: {e}"
                    return False, None
            else:
                try:
                    raw_bytes = base64.b64decode(torrent_data, validate=True)
                except Exception:
                    raw_bytes = torrent_data.encode("utf-8")
        elif isinstance(torrent_data, bytes):
            raw_bytes = torrent_data
        else:
            self.error = "Invalid torrent data type"
            return False, None

        info_hash, torrent_name, torrent_ext, total_size = self._extract_torrent_metadata(raw_bytes)

        if self.set_expected:
            self.total_size = total_size
            self.expected_torrent_name = torrent_name
            self.expected_torrent_extension = torrent_ext

        add_url = f"{self.base_url}/api/v2/torrents/add"
        filename = f"{torrent_name or 'torrent'}.torrent"
        files = {
            "torrents": (
                filename,
                raw_bytes,
                "application/x-bittorrent",
            )
        }
        data: dict[str, Any] = {}
        if download_dir:
            data["savepath"] = download_dir

        try:
            assert self.session is not None
            resp = self.session.post(add_url, files=files, data=data, timeout=25)
            if resp.status_code == 200 and resp.text.strip() == "Ok.":
                return True, info_hash
            elif resp.status_code == 409 or "duplicate" in resp.text.lower():
                return True, info_hash
            elif resp.status_code == 200 and resp.text.strip() == "Fails.":
                self.error = "qBittorrent rejected torrent (Fails.)"
                return False, None
            else:
                self.error = f"qBittorrent error adding torrent: HTTP {resp.status_code} {resp.text}"
                return False, None
        except RequestException as e:
            self.error = str(e)
            logger.warning(f"Error adding torrent to qBittorrent: {e}")
            return False, None

    def add_url(self, torrent_data: str, download_dir: str | None = None) -> tuple[bool, str | None]:
        if not self.session:
            if not self.connect():
                return False, None

        info_hash: str | None = None
        torrent_name = ""
        torrent_ext = ""

        if torrent_data.startswith("magnet:"):
            m = re.search(r"xt=urn:btih:([a-zA-Z0-9]+)", torrent_data, re.IGNORECASE)
            if m:
                raw_hash = m.group(1)
                if len(raw_hash) == 32:
                    try:
                        info_hash = base64.b32decode(raw_hash.upper()).hex().lower()
                    except Exception:
                        info_hash = raw_hash.lower()
                else:
                    info_hash = raw_hash.lower()

            name_m = re.search(r"dn=([^&]+)", torrent_data)
            if name_m:
                raw_name = unquote(name_m.group(1))
                name_split = os.path.splitext(raw_name)
                torrent_name = name_split[0]
                torrent_ext = name_split[1]

        if self.set_expected:
            self.total_size = 0
            self.expected_torrent_name = torrent_name
            self.expected_torrent_extension = torrent_ext

        add_url = f"{self.base_url}/api/v2/torrents/add"
        data: dict[str, Any] = {"urls": torrent_data}
        if download_dir:
            data["savepath"] = download_dir

        try:
            assert self.session is not None
            resp = self.session.post(add_url, data=data, timeout=25)
            if resp.status_code == 200 and resp.text.strip() == "Ok.":
                return True, info_hash
            elif resp.status_code == 409 or "duplicate" in resp.text.lower():
                return True, info_hash
            elif resp.status_code == 200 and resp.text.strip() == "Fails.":
                self.error = "qBittorrent rejected URL (Fails.)"
                return False, None
            else:
                self.error = f"qBittorrent error adding URL: HTTP {resp.status_code} {resp.text}"
                return False, None
        except RequestException as e:
            self.error = str(e)
            logger.warning(f"Error adding URL to qBittorrent: {e}")
            return False, None

    def get_download_progress[TorrentKey](
        self, download_list: list[tuple[str, TorrentKey]]
    ) -> list[tuple[TorrentKey, float]]:
        if not self.session:
            if not self.connect():
                return []

        hash_map: dict[str, TorrentKey] = {}
        for dl_id_str, key in download_list:
            if not dl_id_str or not isinstance(dl_id_str, str):
                continue
            cleaned = dl_id_str.strip().lower()
            if len(cleaned) in (40, 64) and all(c in "0123456789abcdef" for c in cleaned):
                hash_map[cleaned] = key

        if not hash_map:
            return []

        info_url = f"{self.base_url}/api/v2/torrents/info"
        params = {"hashes": "|".join(hash_map.keys())}
        try:
            assert self.session is not None
            resp = self.session.get(info_url, params=params, timeout=25)
            if resp.status_code != 200:
                logger.warning(f"Failed to get torrent progress from qBittorrent: HTTP {resp.status_code}")
                return []
            torrents = resp.json()
        except Exception as e:
            logger.warning(f"Failed to get torrent progress from qBittorrent: {e}")
            return []

        results: list[tuple[TorrentKey, float]] = []
        if isinstance(torrents, list):
            for torr in torrents:
                torr_hash = str(torr.get("hash", "")).lower()
                if torr_hash in hash_map:
                    try:
                        raw_progress = float(torr.get("progress", 0.0))
                    except (ValueError, TypeError):
                        raw_progress = 0.0
                    progress = min(max(raw_progress * 100.0, 0.0), 100.0)
                    results.append((hash_map[torr_hash], progress))

        return results
