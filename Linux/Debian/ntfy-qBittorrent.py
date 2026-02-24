#!/usr/bin/env python3

"""
This Python script is intended to be executed by qBittorrent-nox via the "Run external program / Run on torrent
finished" hook. When a torrent download completes, it sends an ntfy notification containing metadata provided
by qBittorrent. The parameter list below describes the placeholders supported by qBittorrent-nox (as of version
5.1.0), based on libtorrent (version 2.0.11.0):
    - %N: Torrent name
    - %L: Category
    - %G: Tags (separated by commas)
    - %F: Content path (same as root path for multi-file torrent)
    - %R: Root path (first torrent subdirectory path)
    - %D: Save path
    - %C: Number of files
    - %Z: Torrent size (bytes)
    - %T: Current tracker
    - %I: Info hash v1
    - %J: Info hash v2
    - %K: Torrent ID

Quote each placeholder argument to preserve whitespace (for example, "%N"). The script is intended to be
invoked as:

    python3 ./ntfy-qBittorrent.py "%N" "%Z" "%L"

By default, the script reads its ntfy configuration from:

    ~/.config/ntfy-config.ini

The script expects an ntfy configuration file in the following INI format:

    [general]
    server = -- ntfy server URL, e.g., https://ntfy.sh --
    topic  = -- ntfy topic, e.g., my-topic --

    [authentication]
    username = -- ntfy username, e.g., alice --
    password = -- ntfy password, e.g., s3cr3t --
"""

import logging
import sys
from configparser import ConfigParser, SectionProxy
from dataclasses import InitVar, dataclass, field
from datetime import datetime, tzinfo
from getpass import getuser
from pathlib import Path
from socket import gethostname
from time import sleep
from zoneinfo import ZoneInfo

from requests import post
from requests.auth import HTTPBasicAuth
from requests.exceptions import RequestException

logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")

# CONFIG CONSTANTS
POST_RETRY_DELAY: int = 3
POST_BACKOFF_EXP: int = 3
POST_TIMEOUT: int = 10

# FALLBACK CONSTANTS
FALLBACK_NTFY_CONF: Path = Path(Path.home(), ".config", "ntfy-config.ini")
FALLBACK_TIME_ZONE: ZoneInfo = ZoneInfo("Europe/Warsaw")


class ScriptError(Exception):
    pass


@dataclass(frozen=True, slots=True)
class TorrentInfo:
    raw_name: InitVar[str]
    raw_size: InitVar[str]
    raw_category: InitVar[str]

    name: str | None = field(init=False)  # torrent name or None if unknown
    size: int | None = field(init=False)  # torrent size or None if unknown
    category: str | None = field(init=False)  # torrent category if assigned

    def __post_init__(self, raw_name: str, raw_size: str, raw_category: str) -> None:
        object.__setattr__(self, "name", self._parse_name(raw_name))
        object.__setattr__(self, "size", self._parse_size(raw_size))
        object.__setattr__(self, "category", self._parse_category(raw_category))

    @staticmethod
    def _parse_name(value: str) -> str | None:
        if value == "":
            logging.debug("torrent name is empty")
            return None

        if not value.isprintable():
            logging.debug("torrent name has non-printable chars")
            value = "".join(ch if ch.isprintable() else "." for ch in value)

        if len(value) >= 256:
            logging.debug("torrent name too long; truncating")
            value = value[:254] + "…"

        return value

    @staticmethod
    def _parse_category(value: str) -> str | None:
        if value == "":
            logging.debug("torrent category is empty")
            return None

        return value

    @staticmethod
    def _parse_size(value: str) -> int | None:
        try:
            nbytes = int(value)
        except ValueError as exc:
            logging.debug("invalid torrent size value: %s", exc)
            return None

        if not (0 <= nbytes <= 18446744073709551615):  # 2^64 - 1
            logging.debug("torrent size out of range")
            return None

        return nbytes

    @property
    def size_human_readable(self) -> str:
        if self.size is None:
            return "unknown size"
        elif self.size == 0:
            return "0 B"
        else:
            iec_prefixes = ("B", "KiB", "MiB", "GiB", "TiB", "PiB", "EiB")
            iec_idx = (self.size.bit_length() - 1) // 10
            divisor = 1 << (iec_idx * 10)
            divided = (self.size * 100) // divisor
            int_part, dec_part = divmod(divided, 100)
            formatted = f"{int_part}.{dec_part:02d}".rstrip("0").rstrip(".")
            return f"{formatted} {iec_prefixes[iec_idx]}"


@dataclass(frozen=True, slots=True)
class LocalConfig:
    hostname: str = field(init=False)
    username: str = field(init=False)
    timezone: tzinfo = field(init=False)

    def __post_init__(self) -> None:
        object.__setattr__(self, "hostname", self._resolve_local_hostname())
        object.__setattr__(self, "username", self._resolve_local_username())
        object.__setattr__(self, "timezone", self._resolve_local_timezone())

    @staticmethod
    def _resolve_local_hostname() -> str:
        try:
            hostname = gethostname()
        except OSError as exc:
            logging.debug("could not resolve local hostname: %s", exc)
            hostname = "unknown-host"
        return hostname.split(".", 1)[0] or hostname

    @staticmethod
    def _resolve_local_username() -> str:
        try:
            user = getuser()
        except (KeyError, OSError, AttributeError) as exc:
            logging.debug("could not resolve local username: %s", exc)
            user = "unknown-user"
        return user

    @staticmethod
    def _resolve_local_timezone() -> tzinfo:
        try:
            tz = datetime.now().astimezone().tzinfo
        except OSError as exc:
            logging.debug("could not resolve local timezone: %s", exc)
            tz = FALLBACK_TIME_ZONE
        return tz or FALLBACK_TIME_ZONE


@dataclass(frozen=True, slots=True)
class NtfyConfig:
    config_path: InitVar[Path]

    remote_username: str = field(init=False, repr=False)
    remote_password: str = field(init=False, repr=False)
    remote_server: str = field(init=False)
    remote_topic: str = field(init=False)

    def __post_init__(self, config_path: Path) -> None:
        parser = ConfigParser(interpolation=None)

        if not config_path.is_file():
            raise ScriptError(f"config file not found: {config_path}")

        try:
            with config_path.open(mode="rt", encoding="utf-8") as handle:
                parser.read_file(handle)
        except OSError as exc:
            raise ScriptError("could not read config file") from exc

        if not parser.has_section("general"):
            raise ScriptError("config missing [general] section")

        if not parser.has_section("authentication"):
            raise ScriptError("config missing [authentication] section")

        section1 = parser["general"]
        section2 = parser["authentication"]
        entries1 = ("server", "topic")
        entries2 = ("username", "password")

        self._require_section(section1, entries1)
        self._require_section(section2, entries2)

        object.__setattr__(self, "remote_username", section2["username"])
        object.__setattr__(self, "remote_password", section2["password"])
        object.__setattr__(self, "remote_server", section1["server"].rstrip("/"))
        object.__setattr__(self, "remote_topic", section1["topic"].strip("/"))

    @staticmethod
    def _require_section(section: SectionProxy, keys: tuple[str, ...]) -> None:
        missing = [key for key in keys if not section.get(key, "").strip()]
        if missing:
            raise ScriptError(
                f"config missing keys in [{section.name}]: {', '.join(missing)}"
            )

    @property
    def basic_auth(self) -> HTTPBasicAuth:
        return HTTPBasicAuth(self.remote_username, self.remote_password)

    @property
    def server_url(self) -> str:
        return f"{self.remote_server}/{self.remote_topic}"


@dataclass(slots=True)
class NtfyMessage:
    server_url: str
    basic_auth: HTTPBasicAuth
    msg_headers: dict[str, str]
    msg_content: str

    def __init__(
        self,
        torrent_info: TorrentInfo,
        local_config: LocalConfig,
        ntfy_config: NtfyConfig,
    ) -> None:
        self.server_url = ntfy_config.server_url
        self.basic_auth = ntfy_config.basic_auth
        self.msg_headers = self._render_headers(local_config)
        self.msg_content = self._render_message(torrent_info, local_config)

    @staticmethod
    def _render_headers(local_config: LocalConfig) -> dict[str, str]:
        headers = {
            "Title": "Download completed!",
            "Tags": f"pirate_flag,qBittorrent,{local_config.hostname}",
        }
        return headers

    @staticmethod
    def _render_message(torrent_info: TorrentInfo, local_config: LocalConfig) -> str:
        name_part = (
            f"‘{torrent_info.name}’"
            if torrent_info.name is not None
            else "unknown torrent"
        )
        size_part = (
            f"total of {torrent_info.size_human_readable}"
            if torrent_info.size is not None
            else "torrent of unknown size"
        )
        category_part = (
            f"labelled as {torrent_info.category}"
            if torrent_info.category is not None
            else "without assigned category"
        )
        sender_part = f"sent by {local_config.username}@{local_config.hostname}"
        time_part = f"on {datetime.now(local_config.timezone):%d/%m/%Y at %H:%M %Z}"
        full_message = "\n".join(
            [
                name_part,
                "",
                f"{size_part} {category_part}",
                "",
                "--------------------------",
                sender_part,
                time_part,
            ]
        )
        return full_message

    def push_message(self) -> None:
        for attempt in range(1, POST_BACKOFF_EXP + 1):
            try:
                response = post(
                    url=self.server_url,
                    auth=self.basic_auth,
                    headers=self.msg_headers,
                    data=self.msg_content,
                    timeout=POST_TIMEOUT,
                )
                response.raise_for_status()
                logging.debug(f"ntfy push ok: HTTP {response.status_code}")
                return
            except RequestException as exc:
                logging.debug(f"push failed (attempt {attempt}): %s", exc)
                if attempt < POST_BACKOFF_EXP:
                    sleep(POST_RETRY_DELAY**attempt)
                else:
                    raise ScriptError("notification failed after retries") from exc


def main(ntfy_conf: Path | None = None) -> int:
    logging.debug("hook start")

    if ntfy_conf is None:
        logging.debug(f"no config path provided; using fallback {FALLBACK_NTFY_CONF}")
        ntfy_conf = FALLBACK_NTFY_CONF

    if len(sys.argv) < 4:
        logging.debug("missing args; using empty defaults")
        args = sys.argv + [""] * (4 - len(sys.argv))
    else:
        args = sys.argv

    try:
        logging.debug("parse torrent args")
        torrentinfo = TorrentInfo(*args[1:4])

        logging.debug("read local context")
        localconfig = LocalConfig()

        logging.debug("read ntfy config")
        ntfyconfig = NtfyConfig(ntfy_conf)

        logging.debug("build notification")
        request = NtfyMessage(torrentinfo, localconfig, ntfyconfig)

        logging.debug("send notification")
        request.push_message()

        logging.debug("hook done")
        return 0
    except ScriptError as exc:
        logging.error("hook failed: %s", exc)
        return 1
    except Exception as exc:
        logging.exception("hook crashed: %s", exc)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
