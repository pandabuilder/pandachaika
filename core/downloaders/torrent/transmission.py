import logging
from typing import Literal, cast, Any
from urllib.parse import urlparse

import transmission_rpc
import os

from core.base.types import TorrentClient

logger = logging.getLogger(__name__)


class Transmission(TorrentClient):

    name = "transmission"
    # v4 API needs bytes, not base64 string.
    convert_to_base64 = False
    send_url = False

    def __init__(
        self, address: str = "localhost", port: int = 9091, user: str = "", password: str = "", secure: bool = True
    ) -> None:
        super().__init__(address=address, port=port, user=user, password=password, secure=secure)
        self.trans: transmission_rpc.Client | None = None
        self.error = ""

    def __str__(self) -> str:
        return self.name

    def add_url(self, torrent_data: str, download_dir: str | None = None) -> tuple[bool, str | None]:
        result, torrent_id = self.add_torrent(torrent_data, download_dir=download_dir)
        if self.expected_torrent_name and not self.expected_torrent_extension:
            self.expected_torrent_name = os.path.splitext(self.expected_torrent_name)[0]
        return result, torrent_id

    def add_torrent(
        self, torrent_data: str | bytes, download_dir: str | None = None
    ) -> tuple[bool, str | None]:

        if not self.trans:
            return False, None
        self.total_size = 0

        if self.set_expected:
            self.expected_torrent_name = ""
            self.expected_torrent_extension = ""

        try:
            torr = self.trans.add_torrent(torrent_data, download_dir=download_dir, timeout=25)

            if self.set_expected:
                self.expected_torrent_name = torr.name

            for file_t in torr.get_files():
                self.total_size += file_t.size
                if self.set_expected and torr.name == file_t.name:
                    name_split = os.path.splitext(self.expected_torrent_name)
                    self.expected_torrent_name = name_split[0]
                    self.expected_torrent_extension = name_split[1]

            return True, str(torr.id)

        except transmission_rpc.TransmissionError as e:
            self.error = e.message
            if "invalid or corrupt torrent file" in e.message:
                return False, None
            elif "duplicate torrent" in e.message:
                return True, None
            else:
                return False, None

    def connect(self) -> bool:
        try:
            address_parts = urlparse(self.address)
            if address_parts.scheme not in ("http", "https"):
                return False
            address_scheme = cast(Literal["http", "https"], address_parts.scheme)
            extra_arguments: dict[str, Any] = {}
            if address_parts.hostname is not None:
                extra_arguments["host"] = address_parts.hostname
            extra_arguments["port"] = self.port
            if address_parts.path is not None:
                extra_arguments["path"] = address_parts.path
            self.trans = transmission_rpc.Client(
                protocol=address_scheme, username=self.user, password=self.password, timeout=25, **extra_arguments
            )
        except transmission_rpc.TransmissionError as e:
            self.error = e.message
            return False
        return True

    def get_download_progress[TorrentKey](
        self, download_list: list[tuple[str, TorrentKey]]
    ) -> list[tuple[TorrentKey, float]]:

        if not self.trans:
            return []

        torrent_id_map: dict[int, TorrentKey] = {}
        for dl_id_str, key in download_list:
            try:
                int_id = int(dl_id_str)
                torrent_id_map[int_id] = key
            except (ValueError, TypeError):
                continue

        if not torrent_id_map:
            return []

        try:
            torrents = self.trans.get_torrents(list(torrent_id_map.keys()), timeout=25)
        except Exception as e:
            logger.warning(f"Failed to get torrent progress from Transmission: {e}")
            return []

        results: list[tuple[TorrentKey, float]] = []
        for torr in torrents:
            if torr.id in torrent_id_map:
                results.append((torrent_id_map[torr.id], torr.progress))

        return results
