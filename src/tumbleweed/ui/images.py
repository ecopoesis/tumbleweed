"""Images a spec may show: what is allowed, how it is checked before decoding, and the resolver.

An `image` entry holds an `ImageRef`, a validated workspace-relative file name and nothing else
(I4, I11). Showing it takes three steps, and the order is the security property:

1. **Resolve.** An `ImageResolver` turns the ref into bytes, at most `MAX_IMAGE_BYTES` of them.
   The resolver is trusted code that holds the workspace (S19: the broker's); the renderer never
   opens a path itself. Its contract is below.
2. **Inspect.** `inspect_image` looks at the bytes, as bytes: the magic number says PNG, JPEG or
   WebP (anything else is refused, an SVG renamed `x.png` included: it starts with `<`), and the
   header says how many pixels wide and high. An animated WebP is refused. Over `MAX_IMAGE_SIDE`
   a side, or `MAX_DECODED_BYTES` of decoded pixels (width * height * 4), is refused. This reads a
   few dozen bytes and allocates nothing.
3. **Decode.** Only bytes that passed step 2 reach a decoder (`tumbleweed.ui.gtk.imageview`), which
   is pinned to the format step 2 found and checks the size again as the decoder reports it.

Each step raises a subclass of `ImageError`, which carries the fixed line the renderer shows
(`shown`): nothing from the file, the path or the exception message reaches the screen.

**The resolver contract** (`tests/ui/image_contract.py` is the test every implementation passes):

- `read(ref, limit)` returns the whole content of the file `ref` names in the resolver's
  workspace, if that is a regular file of at most `limit` bytes. It never returns more than
  `limit` bytes and never reads more than `limit + 1` from the file (a file that grows while it
  is read cannot get past the cap), and a file of more than `limit` bytes raises
  `ImageTooLargeError`.
- A ref is a file name and nothing else. The file is opened component by component from a
  directory file descriptor, never following a symlink and never leaving the workspace
  (`openat2` with `RESOLVE_BENEATH | RESOLVE_NO_SYMLINKS`, or an `O_NOFOLLOW` walk). A symlink
  anywhere on the path (the last component included), a file that is not a regular file (a
  directory, a FIFO, a device) and a path that leaves the workspace raise `ImageNotAllowedError`;
  a file that is not there raises `ImageNotFoundError`.
- The ref never goes through `expanduser`, a URI builder, a percent decoder,
  `g_file_parse_name`, a glob or a shell, and never into an argument list except after `--` or
  with `./` in front. So `-rf.png`, `~root/x.png` and `%2e%2e/x.png` (all valid refs: legal file
  names) name the file or directory that is literally called that, and are neither an option, a
  home directory nor `..`.
- It is called on a worker thread, never the GTK main thread, and may block, but within a time
  of its own: every wait has a timeout.
- It raises `ImageError` (the subclasses above) for what it refuses. Anything else it raises is a
  bug, which the renderer logs and shows as the generic line.
"""

from dataclasses import dataclass
from enum import Enum
from typing import ClassVar, Final, Protocol, final

from tumbleweed.core.errors import TumbleweedError
from tumbleweed.core.types import ByteCount, PixelCount
from tumbleweed.ui.spec import ImageRef

MAX_IMAGE_BYTES: Final = ByteCount(8 * 1024 * 1024)  # the file, before anything looks inside
MAX_IMAGE_SIDE: Final = PixelCount(8_192)  # width and height, each
MAX_DECODED_BYTES: Final = ByteCount(64 * 1024 * 1024)  # width * height * 4: 16 million pixels
MAX_JPEG_SCANS: Final = 64  # start-of-scan markers: a progressive JPEG has a dozen or so
MAX_WEBP_CHUNKS: Final = 64  # chunks looked at in a WebP, for the ones that mean animation
BYTES_PER_PIXEL: Final = 4  # what a decoder may allocate per pixel (RGBA, 8 bits)

_PNG_SIGNATURE: Final = b"\x89PNG\r\n\x1a\n"
_JPEG_SIGNATURE: Final = b"\xff\xd8\xff"
# Start of frame: the markers that carry the size. C4 (huffman tables), C8 (reserved) and CC
# (arithmetic conditioning) sit in the same range and are not frames.
_JPEG_FRAMES: Final = frozenset(
    {0xC0, 0xC1, 0xC2, 0xC3, 0xC5, 0xC6, 0xC7, 0xC9, 0xCA, 0xCB, 0xCD, 0xCE, 0xCF}
)
_JPEG_STANDALONE: Final = frozenset({0x01, 0xD8, *range(0xD0, 0xD8)})  # markers with no length
_WEBP_ANIMATION: Final = 0x02  # the animation flag of the VP8X header


class ImageError(TumbleweedError):
    """An image could not be shown. `shown` is the line the renderer puts in its place."""

    shown: ClassVar[str] = "The image could not be shown."


class NoImageSourceError(ImageError):
    """No resolver is connected."""

    shown = "Images are not available here."


class ImageNotFoundError(ImageError):
    """The workspace has no such file."""

    shown = "The image was not found."


class ImageNotAllowedError(ImageError):
    """The ref leads through a symlink or out of the workspace, or is not a regular file."""

    shown = "The image is not allowed."


class ImageTooLargeError(ImageError):
    """The file has more bytes, or the image more pixels, than `MAX_IMAGE_*` allow."""

    shown = "The image is too large."


class UnsupportedImageError(ImageError):
    """The bytes are not a PNG, JPEG or WebP image (an SVG is never one), or are animated."""

    shown = "Only PNG, JPEG and WebP images can be shown."


class ImageDecodeError(ImageError):
    """The decoder refused the bytes, which had passed `inspect_image`."""

    shown = "The image could not be read."


class ImageFormat(Enum):
    PNG = "png"
    JPEG = "jpeg"
    WEBP = "webp"


@final
@dataclass(frozen=True)
class ImageInfo:
    """What the header of an allowed image says."""

    format: ImageFormat
    width: PixelCount
    height: PixelCount

    @property
    def decoded_bytes(self) -> ByteCount:
        return ByteCount(self.width * self.height * BYTES_PER_PIXEL)


class ImageResolver(Protocol):
    """Reads the file an `ImageRef` names. The contract is in the module docstring."""

    def read(self, ref: ImageRef, limit: ByteCount) -> bytes: ...


@final
class NoImageSource:
    """The resolver of a tool that has no workspace: every ref is refused."""

    def read(self, ref: ImageRef, limit: ByteCount) -> bytes:
        raise NoImageSourceError("no image source is connected")


def _be16(data: bytes, at: int) -> int:
    return int.from_bytes(data[at : at + 2], "big")


def _le24(data: bytes, at: int) -> int:
    return int.from_bytes(data[at : at + 3], "little")


def _png(data: bytes) -> tuple[int, int]:
    # The signature, then the IHDR chunk, which the format makes the first: length 13, "IHDR",
    # width and height as 32-bit big-endian numbers.
    if len(data) < 24 or data[8:16] != b"\x00\x00\x00\rIHDR":
        raise UnsupportedImageError("a PNG without an IHDR chunk first")
    return int.from_bytes(data[16:20], "big"), int.from_bytes(data[20:24], "big")


def _jpeg(data: bytes) -> tuple[int, int]:
    # Walk the marker segments by their lengths until the first frame header (never into the
    # entropy-coded data: a scan before a frame is a broken file).
    at = 2  # after the SOI; the signature has shown that a marker follows
    end = len(data)
    while at < end:
        if data[at] != 0xFF:
            break
        while at < end and data[at] == 0xFF:  # fill bytes
            at += 1
        if at >= end:
            break
        marker = data[at]
        at += 1
        if marker in _JPEG_STANDALONE:
            continue
        if marker in (0x00, 0xD9, 0xDA) or at + 2 > end:
            break
        length = _be16(data, at)
        if marker in _JPEG_FRAMES:
            if length < 8 or at + length > end:
                break
            # After the length and the precision: the height, then the width.
            return _be16(data, at + 5), _be16(data, at + 3)
        if length < 2:
            break
        at += length
    raise UnsupportedImageError("a JPEG with no usable frame header")


def _no_animation_chunks(data: bytes) -> None:
    """Refuse a WebP that has an animation or a frame chunk, whatever its header flag says."""
    at = 12
    for _ in range(MAX_WEBP_CHUNKS):
        if at + 8 > len(data):
            return
        if data[at : at + 4] in (b"ANIM", b"ANMF"):
            raise UnsupportedImageError("an animated WebP")
        size = int.from_bytes(data[at + 4 : at + 8], "little")
        at += 8 + size + (size & 1)  # chunks are padded to an even length
    raise UnsupportedImageError("a WebP with too many chunks")


def _webp(data: bytes) -> tuple[int, int]:
    if len(data) < 30:
        raise UnsupportedImageError("a WebP too short for a header")
    _no_animation_chunks(data)
    kind = data[12:16]
    if kind == b"VP8X":  # extended: flags, then the canvas size less one, 24 bits each
        if data[20] & _WEBP_ANIMATION:
            raise UnsupportedImageError("an animated WebP")
        return _le24(data, 24) + 1, _le24(data, 27) + 1
    if kind == b"VP8L":  # lossless: a signature byte, then 14 bits each of width and height, less 1
        if data[20] != 0x2F:
            raise UnsupportedImageError("a lossless WebP without its signature")
        bits = int.from_bytes(data[21:25], "little")
        return (bits & 0x3FFF) + 1, ((bits >> 14) & 0x3FFF) + 1
    if kind == b"VP8 ":  # lossy: a 3-byte frame tag, a start code, then 14 bits each
        if data[23:26] != b"\x9d\x01\x2a":
            raise UnsupportedImageError("a lossy WebP without its start code")
        return (
            int.from_bytes(data[26:28], "little") & 0x3FFF,
            int.from_bytes(data[28:30], "little") & 0x3FFF,
        )
    raise UnsupportedImageError("a WebP of an unknown kind")


def inspect_image(data: bytes) -> ImageInfo:
    """What `data` is, from its first bytes, and whether it may be decoded.

    Raises `ImageTooLargeError` for more than `MAX_IMAGE_BYTES` bytes or a size over
    `MAX_IMAGE_SIDE` a side or `MAX_DECODED_BYTES` decoded, and `UnsupportedImageError` for
    anything that is not a whole-enough PNG, JPEG or still WebP. Reads at most the headers.
    """
    if len(data) > MAX_IMAGE_BYTES:
        raise ImageTooLargeError(f"{len(data)} bytes is more than {MAX_IMAGE_BYTES}")
    if data.startswith(_PNG_SIGNATURE):
        image_format, (width, height) = ImageFormat.PNG, _png(data)
    elif data.startswith(_JPEG_SIGNATURE):
        image_format, (width, height) = ImageFormat.JPEG, _jpeg(data)
    elif data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        image_format, (width, height) = ImageFormat.WEBP, _webp(data)
    else:
        raise UnsupportedImageError("not a PNG, JPEG or WebP image")
    if image_format is ImageFormat.JPEG and data.count(b"\xff\xda") > MAX_JPEG_SCANS:
        # Every scan is a pass of the decoder over the whole image, so a file that repeats one
        # thousands of times costs minutes of decoding for a few kilobytes. (An 0xFF in the
        # entropy-coded data is always escaped, so this never counts fewer scans than there are.)
        raise UnsupportedImageError("a JPEG with too many scans")
    if width < 1 or height < 1:
        raise UnsupportedImageError("an image with no pixels")
    if width > MAX_IMAGE_SIDE or height > MAX_IMAGE_SIDE:
        raise ImageTooLargeError(f"{width} by {height} pixels is over {MAX_IMAGE_SIDE} a side")
    if width * height * BYTES_PER_PIXEL > MAX_DECODED_BYTES:
        raise ImageTooLargeError(f"{width} by {height} pixels would decode to too many bytes")
    return ImageInfo(image_format, PixelCount(width), PixelCount(height))
