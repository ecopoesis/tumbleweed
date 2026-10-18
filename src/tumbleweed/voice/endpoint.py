"""`SttEndpoint`: the validated address of a speech server.

The URL comes from the config file or the environment, and audio of the user's voice is sent to it,
so it is checked at every entry point (the config model and the backend both build one): `http` or
`https`, a host (a name or an IP address), an optional port and path prefix, **no credentials,
query or fragment**, short, and free of spaces and control characters. Any host is allowed on
purpose: a VM reaches whisper.cpp running natively on its host (README). Plain `http` leaves the
network in clear text, which is the user's choice to make and is logged as a warning when the host
is not the loopback.

Nothing else may name the URL: `display` is `scheme://host:port` and is the only form that is logged
or shown, so whatever else a URL held (a path, a token someone put there anyway) never reaches a
log. The error messages never echo the text they refuse.
"""

import ipaddress
import re
from typing import Final, final
from urllib.parse import urlsplit

from tumbleweed.core.errors import InvalidValueError

MAX_URL_CHARS: Final = 2048
INFERENCE_PATH: Final = "/inference"  # whisper-server's default; its `--inference-path` changes it
_PATH = re.compile(r"[A-Za-z0-9._~/-]*")
_HOSTNAME = re.compile(r"[A-Za-z0-9](?:[A-Za-z0-9._-]*[A-Za-z0-9])?")
_LOOPBACK_HOSTS = frozenset({"localhost", "127.0.0.1", "::1"})


def _is_host(host: str) -> bool:
    """A DNS name, an IPv4 address, or an IPv6 address without a zone: nothing a label would
    misread or a URL parser would reinterpret."""
    if _HOSTNAME.fullmatch(host) is not None:
        return True
    try:
        ipaddress.IPv6Address(host)
    except ValueError:
        return False
    return "%" not in host  # a zone ("fe80::1%en0") is a name for a local interface, not an address


@final
class SttEndpoint:
    """A speech server's base URL, validated. Immutable."""

    __slots__ = ("_display", "_inference_url", "_loopback", "_tls")

    def __init__(self, text: str) -> None:
        raw: object = text
        if type(raw) is not str:
            raise InvalidValueError("the speech server URL must be text")
        if not 1 <= len(text) <= MAX_URL_CHARS or not text.isprintable() or " " in text:
            raise InvalidValueError(
                "the speech server URL must be a short URL without spaces or control characters"
            )
        try:
            parts = urlsplit(text)
            host = parts.hostname
            port = parts.port  # raises ValueError for text and for anything over 65535
            if port == 0:
                raise ValueError("port 0")
        except ValueError:
            raise InvalidValueError(
                "the speech server URL is not a valid URL (check the host and the port)"
            ) from None
        if parts.scheme not in {"http", "https"}:
            raise InvalidValueError("the speech server URL must start with http:// or https://")
        if host is None or not host:
            raise InvalidValueError("the speech server URL needs a host")
        if not _is_host(host):
            raise InvalidValueError("the speech server URL's host has characters that are refused")
        if parts.username is not None or parts.password is not None or "@" in parts.netloc:
            raise InvalidValueError("the speech server URL must not hold credentials")
        if parts.query or parts.fragment or "?" in text or "#" in text:
            raise InvalidValueError("the speech server URL must have no query or fragment")
        if _PATH.fullmatch(parts.path) is None or ".." in parts.path.split("/"):
            raise InvalidValueError("the speech server URL's path has characters that are refused")
        shown_host = f"[{host}]" if ":" in host else host
        shown_port = port if port is not None else (443 if parts.scheme == "https" else 80)
        self._display = f"{parts.scheme}://{shown_host}:{shown_port}"
        netloc = f"{shown_host}:{port}" if port is not None else shown_host
        self._inference_url = f"{parts.scheme}://{netloc}{parts.path.rstrip('/')}{INFERENCE_PATH}"
        self._loopback = host in _LOOPBACK_HOSTS
        self._tls = parts.scheme == "https"

    @property
    def inference_url(self) -> str:
        """The URL the audio is posted to. Never log it: use `display`."""
        return self._inference_url

    @property
    def display(self) -> str:
        """`scheme://host:port`: safe to log and to show."""
        return self._display

    @property
    def sends_audio_in_clear(self) -> bool:
        """Whether audio crosses a network unencrypted: plain `http` to a host that is not local."""
        return not self._tls and not self._loopback

    def __repr__(self) -> str:
        return f"SttEndpoint({self._display})"

    __str__ = __repr__
