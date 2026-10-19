"""Images for the tests, made by hand: real small ones, and headers that claim any size.

Nothing here needs GTK at import. The real images are 3 pixels wide and 2 high (a red PNG, JPEG and
WebP, the last in its lossy, lossless and animated forms; made with libwebp's tools and embedded),
so a decoder can be run on them; the header builders make just enough of a file for
`inspect_image`, which is all a hostile image has to be to claim a size.

`FakeResolver` is the resolver of the renderer's tests: an in-memory workspace (files, symlinks,
directories, FIFOs) that follows the contract in `tumbleweed.ui.images`. It can be made to wait,
so that a test controls when a worker's read returns.
"""

import base64
import threading
import zlib
from dataclasses import dataclass
from typing import TYPE_CHECKING, Final, final

from tumbleweed.core.types import ByteCount
from tumbleweed.ui.images import (
    ImageDecodeError,
    ImageNotAllowedError,
    ImageNotFoundError,
    ImageTooLargeError,
)
from tumbleweed.ui.spec import ImageRef

if TYPE_CHECKING:
    from tumbleweed.shell._gi import Gdk

SVG: Final = (
    b'<svg xmlns="http://www.w3.org/2000/svg" width="3" height="2">'
    b'<image href="file:///etc/passwd"/></svg>'
)

JPEG_3X2: Final = base64.b64decode(
    "/9j/4AAQSkZJRgABAQAAAQABAAD/2wBDAAYEBQYFBAYGBQYHBwYIChAKCgkJChQODwwQFxQYGBcUFhYaHSUfGhsj"
    "HBYWICwgIyYnKSopGR8tMC0oMCUoKSj/2wBDAQcHBwoIChMKChMoGhYaKCgoKCgoKCgoKCgoKCgoKCgoKCgoKCgo"
    "KCgoKCgoKCgoKCgoKCgoKCgoKCgoKCgoKCj/wAARCAACAAMDASIAAhEBAxEB/8QAHwAAAQUBAQEBAQEAAAAAAAAA"
    "AAECAwQFBgcICQoL/8QAtRAAAgEDAwIEAwUFBAQAAAF9AQIDAAQRBRIhMUEGE1FhByJxFDKBkaEII0KxwRVS0fAk"
    "M2JyggkKFhcYGRolJicoKSo0NTY3ODk6Q0RFRkdISUpTVFVWV1hZWmNkZWZnaGlqc3R1dnd4eXqDhIWGh4iJipKT"
    "lJWWl5iZmqKjpKWmp6ipqrKztLW2t7i5usLDxMXGx8jJytLT1NXW19jZ2uHi4+Tl5ufo6erx8vP09fb3+Pn6/8QA"
    "HwEAAwEBAQEBAQEBAQAAAAAAAAECAwQFBgcICQoL/8QAtREAAgECBAQDBAcFBAQAAQJ3AAECAxEEBSExBhJBUQdh"
    "cRMiMoEIFEKRobHBCSMzUvAVYnLRChYkNOEl8RcYGRomJygpKjU2Nzg5OkNERUZHSElKU1RVVldYWVpjZGVmZ2hp"
    "anN0dXZ3eHl6goOEhYaHiImKkpOUlZaXmJmaoqOkpaanqKmqsrO0tba3uLm6wsPExcbHyMnK0tPU1dbX2Nna4uPk"
    "5ebn6Onq8vP09fb3+Pn6/9oADAMBAAIRAxEAPwDgKKKK+eP2E//Z"
)

WEBP_LOSSY_3X2: Final = base64.b64decode(
    "UklGRjoAAABXRUJQVlA4IC4AAACwAQCdASoDAAIAAUAmJaACdLoABDAAAP7x3I/4DdfFtMv/vYL/3YL/3YL/WwAA"
)

WEBP_LOSSLESS_3X2: Final = base64.b64decode("UklGRh4AAABXRUJQVlA4TBEAAAAvAkAAAAdQjyLXo/+BiOh/AAA=")

WEBP_ANIMATED_3X2: Final = base64.b64decode(
    "UklGRrwAAABXRUJQVlA4WAoAAAACAAAAAgAAAQAAQU5JTQYAAAD/////AABBTk1GRAAAAAAAAAAAAAIAAAEAAGQA"
    "AAJWUDggLAAAAJABAJ0BKgMAAgACADQloAJ0ugADmAD+8Gyv9zUVsW9D/2ln/1LP/qWf4qAAQU5NRkQAAAAAAAAA"
    "AAACAAABAABkAAAAVlA4ICwAAACUAQCdASoDAAIAAAA0JaACdLoAA5gA/va+B/7xn/9xn/9xn/1u/oTY9pgAAA=="
)


def _chunk(kind: bytes, data: bytes) -> bytes:
    return len(data).to_bytes(4, "big") + kind + data + zlib.crc32(kind + data).to_bytes(4, "big")


def png_header(width: int, height: int) -> bytes:
    """The signature and the IHDR chunk: enough to claim a size, with no pixels behind it."""
    ihdr = width.to_bytes(4, "big") + height.to_bytes(4, "big") + bytes([8, 2, 0, 0, 0])
    return b"\x89PNG\r\n\x1a\n" + _chunk(b"IHDR", ihdr)


def png(width: int, height: int, rgb: tuple[int, int, int] = (200, 30, 30)) -> bytes:
    """A whole, valid PNG of one colour."""
    rows = b"".join(b"\x00" + bytes(rgb) * width for _ in range(height))
    return png_header(width, height) + _chunk(b"IDAT", zlib.compress(rows)) + _chunk(b"IEND", b"")


def jpeg_header(width: int, height: int, *, before: bytes = b"") -> bytes:
    """A JPEG up to its frame header (after the segments in `before`, if any)."""
    jfif = b"JFIF\x00\x01\x01\x00\x00\x01\x00\x01\x00\x00"
    app0 = b"\xff\xe0" + (len(jfif) + 2).to_bytes(2, "big") + jfif
    frame = b"\xff\xc0" + (11).to_bytes(2, "big") + b"\x08"
    frame += height.to_bytes(2, "big") + width.to_bytes(2, "big") + b"\x01\x01\x11\x00"
    return b"\xff\xd8" + app0 + before + frame


def webp_header(kind: str, width: int, height: int, *, animated: bool = False) -> bytes:
    """The RIFF header and the first chunk of a WebP: `lossy`, `lossless` or `extended`."""
    if kind == "lossy":
        payload = b"\x00\x00\x00\x9d\x01\x2a" + width.to_bytes(2, "little")
        payload += height.to_bytes(2, "little")
        fourcc = b"VP8 "
    elif kind == "lossless":
        bits = (width - 1) | ((height - 1) << 14)
        payload = b"\x2f" + bits.to_bytes(4, "little")
        fourcc = b"VP8L"
    elif kind == "extended":
        flags = 0x02 if animated else 0x00
        payload = bytes([flags, 0, 0, 0]) + (width - 1).to_bytes(3, "little")
        payload += (height - 1).to_bytes(3, "little")
        fourcc = b"VP8X"
    else:
        raise ValueError(kind)
    payload += bytes(max(0, 10 - len(payload)))
    chunk = fourcc + len(payload).to_bytes(4, "little") + payload
    return b"RIFF" + (4 + len(chunk)).to_bytes(4, "little") + b"WEBP" + chunk


@final
@dataclass(frozen=True)
class _File:
    data: bytes


@final
@dataclass(frozen=True)
class _Link:
    target: str


@final
class _Directory:
    pass


@final
class _Fifo:
    pass


type _Entry = _File | _Link | _Directory | _Fifo


@final
class FakeResolver:
    """An in-memory workspace that follows the resolver contract.

    `FakeResolver({"a.png": data})` has those files (and the directories above them). A path is
    only ever walked as literal names, one component at a time; a symlink anywhere on it is refused.
    `reads` records every call. With `gate` set, `read` waits for it first (up to `WAIT` seconds).
    """

    WAIT: Final = 10.0

    def __init__(self, files: dict[str, bytes] | None = None) -> None:
        self._entries: dict[str, _Entry] = {}
        self.outside: dict[str, bytes] = {}  # files next to the workspace root, not in it
        self.reads: list[tuple[str, int]] = []
        self.gate: threading.Event | None = None
        self.started = threading.Event()  # set when a read has begun
        self.threads: list[int] = []  # the thread of each read
        self.active = 0  # reads in progress now, and at most at once
        self.most_active = 0
        self._lock = threading.Lock()
        for path, data in (files or {}).items():
            self.add_file(path, data)

    def _parents(self, rel: str) -> None:
        parts = rel.split("/")
        for end in range(1, len(parts)):
            self._entries.setdefault("/".join(parts[:end]), _Directory())

    def add_file(self, rel: str, data: bytes) -> None:
        self._parents(rel)
        self._entries[rel] = _File(data)

    def add_symlink(self, rel: str, target: str) -> None:
        self._parents(rel)
        self._entries[rel] = _Link(target)

    def add_dir(self, rel: str) -> None:
        self._parents(rel)
        self._entries[rel] = _Directory()

    def add_fifo(self, rel: str) -> None:
        self._parents(rel)
        self._entries[rel] = _Fifo()

    def add_outside_file(self, name: str, data: bytes) -> None:
        self.outside[name] = data

    def resolver(self) -> "FakeResolver":
        return self

    def read(self, ref: ImageRef, limit: ByteCount) -> bytes:
        with self._lock:
            self.reads.append((str(ref.path), int(limit)))
            self.threads.append(threading.get_ident())
            self.active += 1
            self.most_active = max(self.most_active, self.active)
        self.started.set()
        try:
            return self._read(ref, limit)
        finally:
            with self._lock:
                self.active -= 1

    def _read(self, ref: ImageRef, limit: ByteCount) -> bytes:
        if self.gate is not None and not self.gate.wait(self.WAIT):
            raise ImageDecodeError("the test never opened the gate")
        parts = str(ref.path).split("/")
        for end in range(1, len(parts) + 1):
            entry = self._entries.get("/".join(parts[:end]))
            last = end == len(parts)
            if entry is None:
                raise ImageNotFoundError("no such file")
            if isinstance(entry, _Link):
                raise ImageNotAllowedError("a symlink is never followed")
            if not last and not isinstance(entry, _Directory):
                raise ImageNotFoundError("not a directory")
            if last:
                if not isinstance(entry, _File):
                    raise ImageNotAllowedError("not a regular file")
                if len(entry.data) > limit:
                    raise ImageTooLargeError("over the limit")
                return entry.data
        raise AssertionError("unreachable: the loop returns or raises")


@final
class RecordingDecoder:
    """A decoder that records what it is given and makes a texture of its own (or fails)."""

    def __init__(self, *, fail: bool = False) -> None:
        self.calls: list[bytes] = []
        self.threads: list[int] = []
        self.fail = fail

    def decode(self, data: bytes, info: object) -> "Gdk.Texture":
        from tumbleweed.shell._gi import Gdk, GLib

        self.calls.append(data)
        self.threads.append(threading.get_ident())
        if self.fail:
            raise ImageDecodeError("the fake decoder was told to fail")
        pixels = GLib.Bytes.new(bytes([200, 30, 30, 255]) * 6)
        return Gdk.MemoryTexture.new(3, 2, Gdk.MemoryFormat.R8G8B8A8, pixels, 12)
