"""`inspect_image`: what may be decoded is decided from the bytes alone, before any decoder runs.

Format by magic number (an SVG is never one, whatever it is called), size from the header, and
the caps (`MAX_IMAGE_BYTES`, `MAX_IMAGE_SIDE`, `MAX_DECODED_BYTES`) each tested at the limit and one
past it. No GTK here: this is the part that has to hold before a decoder is even loaded.
"""

import contextlib
from collections.abc import Callable

import pytest
from hypothesis import given
from hypothesis import strategies as st

from tests.ui.image_support import (
    JPEG_3X2,
    SVG,
    WEBP_ANIMATED_3X2,
    WEBP_LOSSLESS_3X2,
    WEBP_LOSSY_3X2,
    jpeg_header,
    png,
    png_header,
    webp_header,
)
from tumbleweed.ui.images import (
    BYTES_PER_PIXEL,
    MAX_DECODED_BYTES,
    MAX_IMAGE_BYTES,
    MAX_IMAGE_SIDE,
    MAX_JPEG_SCANS,
    MAX_WEBP_CHUNKS,
    ImageError,
    ImageFormat,
    ImageTooLargeError,
    NoImageSource,
    UnsupportedImageError,
    inspect_image,
)
from tumbleweed.ui.spec import ImageRef

SIDE = int(MAX_IMAGE_SIDE)
PIXELS = int(MAX_DECODED_BYTES) // BYTES_PER_PIXEL  # the most pixels an image may have


def sizes(data: bytes) -> tuple[ImageFormat, int, int]:
    info = inspect_image(data)
    return info.format, int(info.width), int(info.height)


class TestWhatIsAllowed:
    def test_a_png_a_jpeg_and_every_still_webp_are_recognised_with_their_sizes(self) -> None:
        assert sizes(png(3, 2)) == (ImageFormat.PNG, 3, 2)
        assert sizes(JPEG_3X2) == (ImageFormat.JPEG, 3, 2)
        assert sizes(WEBP_LOSSY_3X2) == (ImageFormat.WEBP, 3, 2)
        assert sizes(WEBP_LOSSLESS_3X2) == (ImageFormat.WEBP, 3, 2)

    def test_the_headers_the_tests_build_read_back_the_size_they_claim(self) -> None:
        assert sizes(png_header(640, 480)) == (ImageFormat.PNG, 640, 480)
        assert sizes(jpeg_header(640, 480)) == (ImageFormat.JPEG, 640, 480)
        assert sizes(webp_header("lossy", 640, 480)) == (ImageFormat.WEBP, 640, 480)
        assert sizes(webp_header("lossless", 640, 480)) == (ImageFormat.WEBP, 640, 480)
        assert sizes(webp_header("extended", 640, 480)) == (ImageFormat.WEBP, 640, 480)

    def test_a_jpeg_with_other_segments_before_its_frame_is_read_past_them(self) -> None:
        # An APP1 segment whose data holds the bytes of a frame marker: skipped by its length.
        exif = b"\xff\xe1" + (12).to_bytes(2, "big") + b"Exif\x00\x00" + b"\xff\xc0\x00\x08"
        comment = b"\xff\xfe" + (7).to_bytes(2, "big") + b"hello"
        filled = b"\xff\xff\xff"  # fill bytes before a marker are allowed

        assert sizes(jpeg_header(7, 9, before=exif + comment)) == (ImageFormat.JPEG, 7, 9)
        data = jpeg_header(7, 9, before=comment)
        assert sizes(data[:-13] + filled + data[-13:]) == (ImageFormat.JPEG, 7, 9)

    def test_a_progressive_jpeg_frame_has_a_size_too(self) -> None:
        data = bytearray(jpeg_header(10, 20))
        data[data.index(b"\xff\xc0") + 1] = 0xC2  # SOF2: progressive

        assert sizes(bytes(data)) == (ImageFormat.JPEG, 10, 20)


class TestWhatIsRefused:
    @pytest.mark.parametrize(
        "data",
        [
            SVG,
            b"<?xml version='1.0'?>" + SVG,
            b"  \n" + SVG,
            b"GIF89a" + bytes(40),
            b"BM" + bytes(60),
            b"II*\x00" + bytes(40),
            b"",
            b"\x89PNG",
            b"RIFF",
            b"\xff\xd8",
            bytes(100),
        ],
        ids=[
            "svg",
            "xml-svg",
            "padded-svg",
            "gif",
            "bmp",
            "tiff",
            "empty",
            "stub",
            "riff",
            "soi",
            "zeros",
        ],
    )
    def test_anything_that_is_not_a_png_jpeg_or_webp_is_unsupported(self, data: bytes) -> None:
        with pytest.raises(UnsupportedImageError):
            inspect_image(data)

    def test_an_svg_is_refused_whatever_it_is_called_because_only_its_bytes_are_looked_at(
        self,
    ) -> None:
        # (The renderer is never told a name: a file called x.png that is an SVG has SVG bytes.)
        with pytest.raises(UnsupportedImageError, match="not a PNG, JPEG or WebP"):
            inspect_image(SVG)

    def test_an_svg_wrapped_in_a_png_signature_is_not_a_png_either(self) -> None:
        with pytest.raises(UnsupportedImageError):
            inspect_image(b"\x89PNG\r\n\x1a\n" + SVG)

    def test_a_png_without_its_header_chunk_first_is_refused(self) -> None:
        data = png(3, 2)
        with pytest.raises(UnsupportedImageError):
            inspect_image(data[:8] + b"\x00\x00\x00\x0dIDAT" + data[16:])
        with pytest.raises(UnsupportedImageError):
            inspect_image(data[:20])  # cut inside the size

    def test_a_jpeg_without_a_usable_frame_header_is_refused(self) -> None:
        good = jpeg_header(3, 2)
        scan_first = b"\xff\xd8\xff\xda\x00\x08\x01\x01\x00\x00\x3f\x00"  # SOS before any SOF
        for bad in (
            good[:-5],  # cut inside the frame header
            good[:-11],  # cut before it
            scan_first,
            b"\xff\xd8\xff\xd9",  # EOI at once
            b"\xff\xd8\xff\x00\x00\x00",  # a stuffed zero is no marker
            b"\xff\xd8\xff\xe0\x00\x01",  # a segment shorter than its own length field
            b"\xff\xd8\xff\xe0\xff\xff" + bytes(10),  # a segment that runs past the end
            good[:-13]
            + b"\xff\xc0"
            + (7).to_bytes(2, "big")
            + bytes(5),  # a frame header too short
        ):
            with pytest.raises(UnsupportedImageError):
                inspect_image(bad)

    def test_a_huffman_table_or_arithmetic_marker_is_no_frame_header(self) -> None:
        for marker in (0xC4, 0xC8, 0xCC):
            data = bytearray(jpeg_header(3, 2))
            data[data.index(b"\xff\xc0") + 1] = marker
            with pytest.raises(UnsupportedImageError):
                inspect_image(bytes(data))

    def test_an_animated_webp_is_refused_and_a_still_extended_one_is_not(self) -> None:
        with pytest.raises(UnsupportedImageError, match="animated"):
            inspect_image(WEBP_ANIMATED_3X2)
        with pytest.raises(UnsupportedImageError, match="animated"):
            inspect_image(webp_header("extended", 3, 2, animated=True))
        assert sizes(webp_header("extended", 3, 2)) == (ImageFormat.WEBP, 3, 2)

    def test_a_webp_that_is_cut_or_of_an_unknown_kind_is_refused(self) -> None:
        for bad in (
            WEBP_LOSSY_3X2[:20],
            WEBP_LOSSLESS_3X2[:25],
            WEBP_LOSSY_3X2[:12] + b"JUNK" + WEBP_LOSSY_3X2[16:],
            WEBP_LOSSY_3X2[:23] + b"\x00\x00\x00" + WEBP_LOSSY_3X2[26:],  # no start code
            WEBP_LOSSLESS_3X2[:20] + b"\x00" + WEBP_LOSSLESS_3X2[21:],  # no signature byte
            b"RIFF" + bytes(4) + b"WAVE" + bytes(40),
        ):
            with pytest.raises(UnsupportedImageError):
                inspect_image(bad)

    def test_a_size_of_zero_is_refused(self) -> None:
        for bad in (
            png_header(0, 5),
            png_header(5, 0),
            jpeg_header(0, 5),
            jpeg_header(5, 0),
            webp_header("lossy", 0, 5),
        ):
            with pytest.raises(UnsupportedImageError, match="no pixels"):
                inspect_image(bad)


class TestScansAndChunks:
    SCAN = b"\xff\xda\x00\x02"

    def test_a_jpeg_may_have_a_progressive_images_worth_of_scans_and_not_one_more(self) -> None:
        one_scan_in_the_header = jpeg_header(3, 2)  # (it has none: only the frame)

        assert sizes(one_scan_in_the_header + self.SCAN * MAX_JPEG_SCANS)[1:] == (3, 2)
        with pytest.raises(UnsupportedImageError, match="too many scans"):
            inspect_image(one_scan_in_the_header + self.SCAN * (MAX_JPEG_SCANS + 1))

    def test_a_real_jpeg_with_one_scan_repeated_a_thousand_times_is_refused_by_its_bytes(
        self,
    ) -> None:
        # Each repeated scan is another pass of the decoder over the image: minutes for 8 MiB.
        bomb = JPEG_3X2[:-2] + JPEG_3X2[JPEG_3X2.index(b"\xff\xda") :] * 1000

        with pytest.raises(UnsupportedImageError, match="too many scans"):
            inspect_image(bomb)

    def test_the_count_cannot_be_dodged_by_entropy_coded_bytes(self) -> None:
        # 0xFF in the entropy-coded data is escaped as FF 00, so FF DA there is no marker: a
        # scan marker is always seen, wherever it is.
        escaped = b"\xff\x00\xda" * 500
        assert sizes(jpeg_header(3, 2) + escaped)[1:] == (3, 2)

    def test_a_webp_with_an_animation_chunk_is_refused_whatever_its_flag_says(self) -> None:
        cleared = WEBP_ANIMATED_3X2[:20] + bytes([WEBP_ANIMATED_3X2[20] & ~0x02])
        cleared += WEBP_ANIMATED_3X2[21:]

        assert b"ANMF" in cleared
        with pytest.raises(UnsupportedImageError, match="animated"):
            inspect_image(cleared)

    def test_a_still_webp_with_other_chunks_is_fine_and_one_with_too_many_is_not(self) -> None:
        def chunk(kind: bytes, size: int) -> bytes:
            return kind + size.to_bytes(4, "little") + bytes(size + (size & 1))

        base = webp_header("extended", 3, 2)
        assert sizes(base + chunk(b"EXIF", 5) + chunk(b"XMP ", 4))[1:] == (3, 2)  # odd size: padded
        with pytest.raises(UnsupportedImageError, match="too many chunks"):
            inspect_image(base + chunk(b"JUNK", 0) * (MAX_WEBP_CHUNKS + 1))


class TestTheCapsAreCheckedFromTheHeader:
    def test_a_side_of_exactly_the_cap_is_allowed_and_one_more_is_not(self) -> None:
        assert sizes(png_header(SIDE, 1)) == (ImageFormat.PNG, SIDE, 1)
        assert sizes(png_header(1, SIDE)) == (ImageFormat.PNG, 1, SIDE)
        for bad in (png_header(SIDE + 1, 1), png_header(1, SIDE + 1)):
            with pytest.raises(ImageTooLargeError, match="a side"):
                inspect_image(bad)

    def test_the_decoded_size_is_capped_by_width_times_height_times_four(self) -> None:
        wide = PIXELS // SIDE  # a rectangle with the most pixels the cap allows
        assert sizes(png_header(SIDE, wide)) == (ImageFormat.PNG, SIDE, wide)
        with pytest.raises(ImageTooLargeError, match="decode"):
            inspect_image(png_header(SIDE, wide + 1))
        with pytest.raises(ImageTooLargeError, match="decode"):
            inspect_image(png_header(SIDE, SIDE))  # each side within the cap, the product not

    @pytest.mark.parametrize(
        "make",
        [
            lambda w, h: png_header(w, h),
            lambda w, h: jpeg_header(w, h),
            lambda w, h: webp_header("lossy", w, h),
            lambda w, h: webp_header("lossless", w, h),
            lambda w, h: webp_header("extended", w, h),
        ],
        ids=["png", "jpeg", "webp-lossy", "webp-lossless", "webp-extended"],
    )
    def test_every_format_is_capped_in_every_dimension_it_can_claim(
        self, make: Callable[[int, int], bytes]
    ) -> None:
        wide = PIXELS // SIDE
        with pytest.raises(ImageTooLargeError):
            inspect_image(make(SIDE, SIDE))
        with pytest.raises(ImageTooLargeError):
            inspect_image(make(SIDE, wide + 1))
        assert sizes(make(SIDE, wide))[1:] == (SIDE, wide)

    def test_a_png_that_claims_four_billion_pixels_a_side_is_refused_by_its_header(self) -> None:
        with pytest.raises(ImageTooLargeError):
            inspect_image(png_header(2**32 - 1, 2**32 - 1))

    def test_the_byte_cap_is_exact(self) -> None:
        header = png_header(3, 2)
        at_the_cap = header + bytes(int(MAX_IMAGE_BYTES) - len(header))
        assert sizes(at_the_cap)[1:] == (3, 2)
        with pytest.raises(ImageTooLargeError, match="bytes"):
            inspect_image(at_the_cap + b"\x00")


class TestItNeverRaisesAnythingElse:
    @given(st.binary(max_size=200))
    def test_any_bytes_are_an_image_info_or_an_image_error(self, data: bytes) -> None:
        with contextlib.suppress(ImageError):  # a refusal is fine; any other exception is not
            inspect_image(data)

    @given(
        st.sampled_from(
            [png(3, 2), JPEG_3X2, WEBP_LOSSY_3X2, WEBP_LOSSLESS_3X2, WEBP_ANIMATED_3X2]
        ),
        st.integers(min_value=0, max_value=700),
        st.integers(min_value=0, max_value=255),
    )
    def test_a_real_image_cut_or_with_a_byte_changed_is_an_image_info_or_an_image_error(
        self, data: bytes, at: int, value: int
    ) -> None:
        mutated = bytearray(data)
        if at < len(mutated):
            mutated[at] = value
        for candidate in (bytes(mutated), bytes(mutated[:at])):
            with contextlib.suppress(ImageError):
                inspect_image(candidate)


def test_the_default_source_refuses_every_ref_and_says_so() -> None:
    from tumbleweed.core.types import ByteCount
    from tumbleweed.ui.images import NoImageSourceError

    ref = ImageRef.model_validate({"path": "a.png"})

    with pytest.raises(NoImageSourceError):
        NoImageSource().read(ref, ByteCount(10))


def test_every_error_has_a_fixed_line_of_its_own_to_show() -> None:
    from tumbleweed.ui import images

    errors = [
        cls
        for cls in vars(images).values()
        if isinstance(cls, type) and issubclass(cls, ImageError) and cls is not ImageError
    ]
    shown = {cls.shown for cls in errors} | {ImageError.shown}

    assert len(errors) == 6
    assert len(shown) == 7  # none shares another's line
    assert all(len(line) < 60 and line.endswith(".") for line in shown)


PROBE = """
import sys
sys.path[:0] = [sys.argv[1]]
from tumbleweed.ui.images import inspect_image
from tumbleweed.ui.spec import ImageRef
loaded = sorted(
    name
    for name in sys.modules
    if name == "gi" or name.startswith(("gi.", "tumbleweed.ui.gtk", "tumbleweed.shell"))
)
print(",".join(loaded))
"""


def test_importing_the_image_rules_loads_no_gtk_and_no_shell() -> None:
    import subprocess
    import sys
    import tomllib
    from pathlib import Path

    root = Path(__file__).resolve().parents[2]
    result = subprocess.run(  # noqa: S603  (argv list, no shell; our own interpreter)
        [sys.executable, "-I", "-c", PROBE, str(root / "src")],
        capture_output=True,
        text=True,
        check=True,
        timeout=60,
    )

    assert result.stdout.strip() == ""
    contracts = tomllib.loads((root / "pyproject.toml").read_text(encoding="utf-8"))
    names = [c["name"] for c in contracts["tool"]["importlinter"]["contracts"]]
    assert "ui.images never imports gi, the GTK adapter or the shell (D21)" in names
