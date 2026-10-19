"""The `image` widget: a ref is resolved, inspected, then decoded, all off the main thread (I4, I9).

What matters here is the order and who sees what. The real decoder is used for the real images;
a recording decoder stands in to prove that a refused image never reaches one. Every hostile input
ends as a fixed line of ours in the image's place, never as a decode.
"""

import functools
import logging
import threading
from typing import TYPE_CHECKING
from unittest import mock

import pytest

from tests.shell.gtk_support import pump, pump_until
from tests.ui.gtk.support import of_type, spec_of
from tests.ui.image_support import (
    JPEG_3X2,
    SVG,
    WEBP_ANIMATED_3X2,
    WEBP_LOSSLESS_3X2,
    WEBP_LOSSY_3X2,
    FakeResolver,
    RecordingDecoder,
    jpeg_header,
    png,
    png_header,
    webp_header,
)
from tumbleweed.ui.images import MAX_IMAGE_BYTES, MAX_IMAGE_SIDE, ImageError

if TYPE_CHECKING:
    from tumbleweed.shell._gi import Gdk
    from tumbleweed.ui.gtk import RenderedTool
    from tumbleweed.ui.gtk.imageview import ImageDecoder, ImageView
    from tumbleweed.ui.images import ImageResolver

pytestmark = pytest.mark.gtk

SIDE = int(MAX_IMAGE_SIDE)


def image_spec(alt: str = "a photo") -> object:
    return spec_of(
        {"kind": "box", "children": [{"kind": "image", "bind": "img", "alt": alt}]},
        [{"kind": "image", "key": "img"}],
    )


def render_with(
    resolver: "ImageResolver", decoder: "ImageDecoder | None" = None, alt: str = "a photo"
) -> "RenderedTool":
    from tumbleweed.ui.gtk import RenderedTool
    from tumbleweed.ui.spec import UiSpec

    spec = image_spec(alt)
    assert isinstance(spec, UiSpec)
    return RenderedTool(spec, images=resolver, decoder=decoder)


def view_of(tool: "RenderedTool") -> "ImageView":
    from tumbleweed.ui.gtk.imageview import ImageView

    (view,) = of_type(tool, ImageView)
    return view


def point_at(tool: "RenderedTool", path: str | None) -> None:
    tool.runtime.patch_state({"img": None if path is None else {"path": path}})


def settled(view: "ImageView") -> bool:
    from tumbleweed.ui.gtk.imageview import LOADING

    return view.texture is not None or view.notice not in ("", LOADING)


def drain() -> None:
    """Wait until every job queued so far has been run by a worker and its result handled.

    Each worker is made to wait at a barrier behind everything that was queued, so when both
    are there nothing earlier is still running, and the results they posted are then delivered
    by the main loop. (A test that asserts that something did NOT happen waits with this, not
    for a time.)"""
    from tumbleweed.ui.gtk.imageview import WORKERS, submit

    barrier = threading.Barrier(WORKERS, timeout=10)
    done = [threading.Event() for _ in range(WORKERS)]

    def sentinel(event: threading.Event) -> None:
        barrier.wait()
        event.set()

    for event in done:
        submit(functools.partial(sentinel, event))
    assert pump_until(lambda: all(event.is_set() for event in done), timeout=15)
    pump(0.05)  # the results are posted already: let the main loop take them


def wait_for(view: "ImageView") -> None:
    assert pump_until(lambda: settled(view)), f"nothing arrived (notice {view.notice!r})"


def size(texture: "Gdk.Texture | None") -> tuple[int, int]:
    assert texture is not None
    return texture.get_width(), texture.get_height()


class TestShowingAnImage:
    @pytest.mark.parametrize(
        ("name", "data"),
        [("a.png", png(3, 2)), ("b.jpg", JPEG_3X2)],
        ids=["png", "jpeg"],
    )
    def test_a_png_and_a_jpeg_are_decoded_and_shown(self, name: str, data: bytes) -> None:
        tool = render_with(FakeResolver({name: data}))
        view = view_of(tool)

        point_at(tool, name)
        wait_for(view)

        assert size(view.texture) == (3, 2)
        assert view.notice == ""

    @pytest.mark.parametrize("data", [WEBP_LOSSY_3X2, WEBP_LOSSLESS_3X2], ids=["lossy", "lossless"])
    def test_a_webp_is_decoded_where_the_system_has_its_loader_and_refused_with_a_line_where_not(
        self, data: bytes
    ) -> None:
        from tumbleweed.ui.gtk.imageview import installed_loaders

        tool = render_with(FakeResolver({"a.webp": data}))
        view = view_of(tool)

        point_at(tool, "a.webp")
        wait_for(view)

        if "webp" in installed_loaders():
            assert size(view.texture) == (3, 2)
            assert view.notice == ""
        else:  # (the loader is a package of its own: a system without it says so)
            assert view.texture is None
            assert view.notice == "Only PNG, JPEG and WebP images can be shown."

    def test_a_picture_shows_the_texture_scaled_down_to_fit_and_never_up(self) -> None:
        from tumbleweed.ui.gtk.imageview import MAX_DISPLAY, fit

        assert fit(3, 2) == (3, 2)
        assert fit(MAX_DISPLAY * 4, MAX_DISPLAY * 2) == (MAX_DISPLAY, MAX_DISPLAY // 2)
        assert fit(1, SIDE) == (1, MAX_DISPLAY)
        assert fit(SIDE, 1) == (MAX_DISPLAY, 1)  # never a size of zero

    def test_an_empty_entry_shows_nothing_at_all(self) -> None:
        tool = render_with(FakeResolver())
        view = view_of(tool)

        assert view.texture is None
        assert view.notice == ""
        assert not view.get_first_child().get_visible()  # type: ignore[union-attr]  # the picture

    def test_the_alt_text_is_for_assistive_technology_and_is_never_displayed(self) -> None:
        from tumbleweed.shell._gi import Gtk

        tool = render_with(FakeResolver({"a.png": png(3, 2)}), alt="<b>a red</b> _picture")
        view = view_of(tool)
        point_at(tool, "a.png")
        wait_for(view)

        (picture,) = of_type(tool, Gtk.Picture)
        assert picture.get_alternative_text() == "<b>a red</b> _picture"
        assert "a red" not in view.notice
        assert picture.get_tooltip_text() is None

    def test_replacing_the_image_with_another_shows_the_new_one(self) -> None:
        resolver = FakeResolver({"a.png": png(3, 2), "b.png": png(5, 4)})
        tool = render_with(resolver)
        view = view_of(tool)
        point_at(tool, "a.png")
        wait_for(view)

        point_at(tool, "b.png")
        assert view.texture is None  # never an image the state no longer holds
        wait_for(view)

        assert size(view.texture) == (5, 4)

    def test_assigning_the_same_ref_again_reads_the_file_again(self) -> None:
        # A handler that rewrites the file in place assigns an equal ref: the widget refreshes.
        resolver = FakeResolver({"a.png": png(3, 2)})
        tool = render_with(resolver)
        view = view_of(tool)
        point_at(tool, "a.png")
        wait_for(view)

        resolver.add_file("a.png", png(6, 4))
        point_at(tool, "a.png")
        wait_for(view)

        assert size(view.texture) == (6, 4)
        assert [path for path, _limit in resolver.reads] == ["a.png", "a.png"]

    def test_clearing_the_entry_takes_the_image_away(self) -> None:
        tool = render_with(FakeResolver({"a.png": png(3, 2)}))
        view = view_of(tool)
        point_at(tool, "a.png")
        wait_for(view)

        point_at(tool, None)

        assert view.texture is None
        assert view.notice == ""

    def test_a_stale_entry_says_so_instead_of_showing_the_old_image(self) -> None:
        tool = render_with(FakeResolver({"a.png": png(3, 2)}))
        view = view_of(tool)
        point_at(tool, "a.png")
        wait_for(view)

        view.set_ref(None, stale=True)

        assert view.texture is None
        assert view.notice == "Out of date"

    def test_a_loaded_image_claims_no_text_and_a_big_one_is_shown_scaled_to_fit(self) -> None:
        from tumbleweed.shell._gi import Gdk, GLib, Gtk
        from tumbleweed.ui.gtk.imageview import MAX_DISPLAY, fit

        class Big:
            def decode(self, data: bytes, info: object) -> "Gdk.Texture":
                pixels = GLib.Bytes.new(bytes(1000 * 500 * 4))
                return Gdk.MemoryTexture.new(1000, 500, Gdk.MemoryFormat.R8G8B8A8, pixels, 4000)

        tool = render_with(FakeResolver({"a.png": png(3, 2)}), Big())
        view = view_of(tool)
        point_at(tool, "a.png")
        assert tool.shown_text_total == len("Loading the image...")  # the line is counted ...

        wait_for(view)

        assert tool.shown_text_total == 0  # ... and given back when the image is there
        (picture,) = of_type(tool, Gtk.Picture)
        assert picture.get_size_request() == fit(1000, 500) == (MAX_DISPLAY, MAX_DISPLAY // 2)

    def test_the_budget_counts_the_lines_an_image_shows(self) -> None:
        tool = render_with(FakeResolver())
        view = view_of(tool)
        before = tool.shown_text_total

        view.set_ref(None, stale=True)

        assert tool.shown_text_total == before + len("Out of date")


class TestItNeverBlocksTheMainThread:
    def test_the_file_is_read_and_decoded_on_workers_never_on_the_main_thread(self) -> None:
        resolver, decoder = FakeResolver({"a.png": png(3, 2)}), RecordingDecoder()
        tool = render_with(resolver, decoder)
        view = view_of(tool)

        point_at(tool, "a.png")
        wait_for(view)

        main = threading.get_ident()
        assert resolver.threads
        assert decoder.threads
        assert main not in resolver.threads + decoder.threads

    def test_a_read_that_has_not_returned_leaves_the_interface_running_and_says_loading(
        self,
    ) -> None:
        resolver = FakeResolver({"a.png": png(3, 2)})
        resolver.gate = threading.Event()  # the read waits for the test
        tool = render_with(resolver)
        view = view_of(tool)

        point_at(tool, "a.png")
        assert resolver.started.wait(5)
        pump(0.1)  # the main loop turns while the worker is stuck in the resolver

        assert view.notice == "Loading the image..."
        assert view.texture is None
        resolver.gate.set()
        wait_for(view)
        assert size(view.texture) == (3, 2)

    def test_a_new_ref_replaces_the_one_waiting_and_a_result_nobody_wants_is_dropped(self) -> None:
        resolver = FakeResolver(
            {n: png(w, 2) for n, w in (("a.png", 3), ("b.png", 4), ("c.png", 5))}
        )
        resolver.gate = threading.Event()
        tool = render_with(resolver)
        view = view_of(tool)
        point_at(tool, "a.png")
        assert resolver.started.wait(5)

        point_at(tool, "b.png")  # waits behind a.png, which is being read
        point_at(tool, "c.png")  # replaces b.png: it is never read
        resolver.gate.set()
        wait_for(view)
        drain()

        assert [path for path, _limit in resolver.reads] == ["a.png", "c.png"]
        assert size(view.texture) == (5, 2)  # c.png's; a.png's result was dropped

    def test_one_widget_has_one_read_in_flight_at_a_time(self) -> None:
        resolver = FakeResolver({"a.png": png(3, 2), "b.png": png(4, 2)})
        resolver.gate = threading.Event()
        tool = render_with(resolver)
        view = view_of(tool)

        point_at(tool, "a.png")
        assert resolver.started.wait(5)
        point_at(tool, "b.png")

        # (b.png must not be read while a.png is: were it, a second read would begin at once)
        assert not pump_until(lambda: resolver.most_active > 1, timeout=0.5)
        assert resolver.most_active == 1
        resolver.gate.set()
        wait_for(view)

    def test_many_images_share_two_workers(self) -> None:
        from tumbleweed.ui.gtk.imageview import WORKERS, ImageView

        resolver = FakeResolver({"a.png": png(3, 2)})
        resolver.gate = threading.Event()
        spec = spec_of(
            {
                "kind": "box",
                "children": [
                    {"kind": "box", "children": [{"kind": "image", "bind": "img"}] * 20}
                    for _ in range(5)
                ],
            },
            [{"kind": "image", "key": "img"}],
        )
        from tumbleweed.ui.gtk import RenderedTool

        tool = RenderedTool(spec, images=resolver)
        tool.runtime.patch_state({"img": {"path": "a.png"}})
        assert pump_until(lambda: resolver.most_active == WORKERS)  # both workers are in a read

        workers = [t for t in threading.enumerate() if t.name.startswith("tumbleweed-image-")]
        assert 1 <= len(workers) <= WORKERS
        assert all(t.daemon for t in workers)  # none keeps the process alive
        assert resolver.most_active <= WORKERS  # a hundred images, two reads at a time
        resolver.gate.set()
        views = of_type(tool, ImageView)
        assert pump_until(lambda: all(v.texture is not None for v in views), timeout=30)

    def test_a_result_that_arrives_after_the_tool_is_closed_is_dropped(self) -> None:
        resolver = FakeResolver({"a.png": png(3, 2)})
        resolver.gate = threading.Event()
        tool = render_with(resolver)
        view = view_of(tool)
        point_at(tool, "a.png")
        assert resolver.started.wait(5)

        tool.close()
        resolver.gate.set()
        drain()  # the worker has finished the read and posted what it found

        assert view.closed
        assert view.texture is None

    def test_jobs_in_the_queue_or_running_do_not_keep_a_closed_tool_alive(self) -> None:
        import gc
        import weakref

        from tumbleweed.ui.gtk import RenderedTool
        from tumbleweed.ui.gtk.imageview import WORKERS, ImageView

        resolver = FakeResolver({"a.png": png(3, 2)})
        resolver.gate = threading.Event()  # every read waits: the workers are busy
        count = WORKERS + 2
        spec = spec_of(
            {"kind": "box", "children": [{"kind": "image", "bind": "img"}] * count},
            [{"kind": "image", "key": "img"}],
        )
        tool = RenderedTool(spec, images=resolver)
        tool.runtime.patch_state({"img": {"path": "a.png"}})
        assert resolver.started.wait(5)  # some are being read, the rest wait for a worker
        views = [weakref.ref(v) for v in of_type(tool, ImageView)]
        assert len(views) == count

        tool.close()
        del tool
        gc.collect()

        assert [ref() for ref in views] == [None] * count
        resolver.gate.set()  # let the workers go on to the next tool's jobs

    def test_a_result_for_an_entry_that_was_cleared_meanwhile_is_dropped(self) -> None:
        resolver = FakeResolver({"a.png": png(3, 2)})
        resolver.gate = threading.Event()
        tool = render_with(resolver)
        view = view_of(tool)
        point_at(tool, "a.png")
        assert resolver.started.wait(5)

        point_at(tool, None)
        resolver.gate.set()
        drain()

        assert view.texture is None
        assert view.notice == ""


HOSTILE: list[tuple[str, bytes, str]] = [
    ("an SVG called x.png", SVG, "Only PNG, JPEG and WebP images can be shown."),
    (
        "an SVG after a PNG signature",
        b"\x89PNG\r\n\x1a\n" + SVG,
        "Only PNG, JPEG and WebP images can be shown.",
    ),
    (
        "an XML prolog and an SVG",
        b"<?xml version='1.0'?>" + SVG,
        "Only PNG, JPEG and WebP images can be shown.",
    ),
    ("a GIF", b"GIF89a" + bytes(30), "Only PNG, JPEG and WebP images can be shown."),
    ("an animated WebP", WEBP_ANIMATED_3X2, "Only PNG, JPEG and WebP images can be shown."),
    (
        "an animated WebP with its flag cleared",
        WEBP_ANIMATED_3X2[:20] + bytes([WEBP_ANIMATED_3X2[20] & ~0x02]) + WEBP_ANIMATED_3X2[21:],
        "Only PNG, JPEG and WebP images can be shown.",
    ),
    (
        "a JPEG of 200 scans",
        jpeg_header(3, 2) + b"\xff\xda\x00\x02" * 200,
        "Only PNG, JPEG and WebP images can be shown.",
    ),
    ("nothing at all", b"", "Only PNG, JPEG and WebP images can be shown."),
    ("a PNG too wide", png_header(SIDE + 1, 1), "The image is too large."),
    ("a PNG too tall", png_header(1, SIDE + 1), "The image is too large."),
    ("a PNG with too many pixels", png_header(SIDE, SIDE), "The image is too large."),
    ("a PNG of four billion pixels", png_header(2**32 - 1, 2**32 - 1), "The image is too large."),
    ("a JPEG with too many pixels", jpeg_header(SIDE, SIDE), "The image is too large."),
    (
        "a lossy WebP with too many pixels",
        webp_header("lossy", SIDE, SIDE),
        "The image is too large.",
    ),
    ("a lossless WebP too wide", webp_header("lossless", SIDE + 1, 1), "The image is too large."),
    (
        "an extended WebP with too many pixels",
        webp_header("extended", SIDE, SIDE),
        "The image is too large.",
    ),
]


class TestHostileImagesNeverReachADecoder:
    @pytest.mark.parametrize(("what", "data", "line"), HOSTILE, ids=[h[0] for h in HOSTILE])
    def test_it_is_refused_with_a_line_and_the_decoder_is_never_called(
        self, what: str, data: bytes, line: str
    ) -> None:
        decoder = RecordingDecoder()
        tool = render_with(FakeResolver({"x.png": data}), decoder)
        view = view_of(tool)

        point_at(tool, "x.png")
        wait_for(view)

        assert decoder.calls == []
        assert view.texture is None
        assert view.notice == line

    @pytest.mark.parametrize(("what", "data", "line"), HOSTILE, ids=[h[0] for h in HOSTILE])
    def test_and_the_real_decoder_refuses_it_the_same_way(
        self, what: str, data: bytes, line: str
    ) -> None:
        tool = render_with(FakeResolver({"x.png": data}))
        view = view_of(tool)

        point_at(tool, "x.png")
        wait_for(view)

        assert view.texture is None
        assert view.notice == line

    def test_a_file_of_one_byte_over_the_cap_is_refused_before_it_is_looked_into(self) -> None:
        decoder = RecordingDecoder()
        big = png(3, 2) + bytes(int(MAX_IMAGE_BYTES))
        tool = render_with(FakeResolver({"x.png": big}), decoder)
        view = view_of(tool)

        point_at(tool, "x.png")
        wait_for(view)

        assert decoder.calls == []
        assert view.notice == "The image is too large."

    def test_a_resolver_that_ignores_the_limit_is_not_trusted(self) -> None:
        class Greedy:
            def read(self, ref: object, limit: object) -> bytes:
                return png(3, 2) + bytes(int(MAX_IMAGE_BYTES))

        decoder = RecordingDecoder()
        tool = render_with(Greedy(), decoder)
        view = view_of(tool)

        point_at(tool, "x.png")
        wait_for(view)

        assert decoder.calls == []
        assert view.texture is None
        assert view.notice == "The image could not be read."

    def test_the_resolver_is_asked_for_no_more_than_the_byte_cap(self) -> None:
        resolver = FakeResolver({"x.png": png(3, 2)})
        tool = render_with(resolver, RecordingDecoder())
        view = view_of(tool)

        point_at(tool, "x.png")
        wait_for(view)

        assert resolver.reads == [("x.png", int(MAX_IMAGE_BYTES))]

    def test_a_png_cut_short_is_a_decode_failure_with_a_line_not_a_crash(self) -> None:
        data = png(3, 2)  # its header is whole, so it passes inspection; its pixels are not there
        tool = render_with(FakeResolver({"x.png": data[:40]}))
        view = view_of(tool)

        point_at(tool, "x.png")
        wait_for(view)

        assert view.texture is None
        assert view.notice == "The image could not be read."


class TestWhatTheResolverRefusesIsShownAsALine:
    def test_each_refusal_has_its_own_line_which_says_nothing_of_the_file(self) -> None:
        resolver = FakeResolver({"real.png": png(3, 2)})
        resolver.add_symlink("link.png", "real.png")
        resolver.add_dir("folder.png")
        lines = {}
        for name in ("missing.png", "link.png", "folder.png"):
            tool = render_with(resolver)
            view = view_of(tool)
            point_at(tool, name)
            wait_for(view)
            lines[name] = view.notice

        assert lines == {
            "missing.png": "The image was not found.",
            "link.png": "The image is not allowed.",
            "folder.png": "The image is not allowed.",
        }

    def test_without_a_resolver_every_image_is_refused_and_says_so(self) -> None:
        from tumbleweed.ui.gtk import RenderedTool
        from tumbleweed.ui.spec import UiSpec

        spec = image_spec()
        assert isinstance(spec, UiSpec)
        tool = RenderedTool(spec)
        view = view_of(tool)

        point_at(tool, "a.png")
        wait_for(view)

        assert view.notice == "Images are not available here."

    def test_a_failure_nobody_expected_is_the_generic_line_and_is_logged(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        class Broken:
            def read(self, ref: object, limit: object) -> bytes:
                raise RuntimeError("secret detail of the failure")

        tool = render_with(Broken())
        view = view_of(tool)

        with caplog.at_level(logging.ERROR):
            point_at(tool, "a.png")
            wait_for(view)

        assert view.notice == ImageError.shown
        assert "secret detail" not in view.notice
        assert any("secret detail" in (r.exc_text or "") for r in caplog.records)

    def test_a_decoder_that_raises_is_the_generic_line(self) -> None:
        class Broken:
            def decode(self, data: bytes, info: object) -> "Gdk.Texture":
                raise ValueError("not an ImageError")

        tool = render_with(FakeResolver({"a.png": png(3, 2)}), Broken())
        view = view_of(tool)

        point_at(tool, "a.png")
        wait_for(view)

        assert view.notice == ImageError.shown

    def test_a_failure_can_be_followed_by_a_good_image(self) -> None:
        tool = render_with(FakeResolver({"good.png": png(3, 2)}))
        view = view_of(tool)
        point_at(tool, "missing.png")
        wait_for(view)

        point_at(tool, "good.png")
        wait_for(view)

        assert size(view.texture) == (3, 2)
        assert view.notice == ""


class TestThePinnedDecoder:
    def test_a_loader_is_made_for_the_format_that_was_inspected_and_never_sniffs(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # gdk-pixbuf, left to sniff, would pick its SVG loader for these bytes and make an image
        # of an SVG (which can reference other files). Pinned to PNG, the PNG loader refuses
        # them. `inspect_image` is made to lie, as a bug in it would, to put the SVG in front of
        # the decoder: the decoder's own pin is what stops it.
        from tumbleweed.core.types import PixelCount
        from tumbleweed.shell._gi import GdkPixbuf, GLib
        from tumbleweed.ui.gtk import imageview
        from tumbleweed.ui.images import ImageDecodeError, ImageFormat, ImageInfo

        info = ImageInfo(ImageFormat.PNG, PixelCount(3), PixelCount(2))
        monkeypatch.setattr(imageview, "inspect_image", lambda data: info)

        with pytest.raises(ImageDecodeError):
            imageview.PixbufDecoder().decode(SVG, info)

        if "svg" in imageview.installed_loaders():  # the control: the same bytes, left to sniff
            loader = GdkPixbuf.PixbufLoader()
            loader.write_bytes(GLib.Bytes.new(SVG))
            loader.close()
            assert loader.get_pixbuf() is not None  # it would have been decoded

    @pytest.mark.parametrize(
        ("data", "name"), [(png(3, 2), "png"), (JPEG_3X2, "jpeg")], ids=["png", "jpeg"]
    )
    def test_the_loader_is_asked_for_by_the_name_of_the_inspected_format(
        self, data: bytes, name: str
    ) -> None:
        from tumbleweed.shell._gi import GdkPixbuf
        from tumbleweed.ui.gtk.imageview import PixbufDecoder
        from tumbleweed.ui.images import inspect_image

        real = GdkPixbuf.PixbufLoader.new_with_type
        with mock.patch.object(GdkPixbuf.PixbufLoader, "new_with_type", wraps=real) as pinned:
            texture = PixbufDecoder().decode(data, inspect_image(data))

        pinned.assert_called_once_with(name)  # (an unpinned `PixbufLoader()` is what sniffs)
        assert texture.get_width() == 3

    def test_the_decoder_looks_at_the_bytes_again_and_refuses_a_size_nobody_checked(self) -> None:
        from tumbleweed.core.types import PixelCount
        from tumbleweed.ui.gtk.imageview import PixbufDecoder
        from tumbleweed.ui.images import ImageDecodeError, ImageFormat, ImageInfo

        wrong = ImageInfo(ImageFormat.PNG, PixelCount(2), PixelCount(2))

        with pytest.raises(ImageDecodeError, match="inspected"):
            PixbufDecoder().decode(png(3, 2), wrong)

    def test_a_loader_that_sees_another_size_than_the_header_is_refused(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # The header and the loader disagree about the size: inspect is made to say 2 by 2 for
        # bytes that are 3 by 2, as if the header were read differently by the two.
        from tumbleweed.core.types import PixelCount
        from tumbleweed.ui.gtk import imageview
        from tumbleweed.ui.images import ImageDecodeError, ImageFormat, ImageInfo

        info = ImageInfo(ImageFormat.PNG, PixelCount(2), PixelCount(2))
        monkeypatch.setattr(imageview, "inspect_image", lambda data: info)

        with pytest.raises(ImageDecodeError, match="another size"):
            imageview.PixbufDecoder().decode(png(3, 2), info)

    def test_a_format_whose_loader_is_not_installed_is_refused_without_a_loader(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from tumbleweed.ui.gtk import imageview
        from tumbleweed.ui.images import UnsupportedImageError, inspect_image

        monkeypatch.setattr(imageview, "installed_loaders", lambda: frozenset({"png"}))

        with pytest.raises(UnsupportedImageError, match="no jpeg loader"):
            imageview.PixbufDecoder().decode(JPEG_3X2, inspect_image(JPEG_3X2))

    def test_the_texture_has_the_size_of_the_image(self) -> None:
        from tumbleweed.ui.gtk.imageview import PixbufDecoder
        from tumbleweed.ui.images import inspect_image

        data = png(7, 5)

        texture = PixbufDecoder().decode(data, inspect_image(data))

        assert (texture.get_width(), texture.get_height()) == (7, 5)


class TestWhatAToolsImagesCost:
    """Decoded pixels are bounded per image (display size) and per tool (a budget)."""

    def test_an_image_is_kept_at_the_display_size_and_a_small_one_exactly_as_it_is(self) -> None:
        from tumbleweed.ui.gtk.imageview import MAX_DISPLAY, PixbufDecoder
        from tumbleweed.ui.images import inspect_image

        decoder = PixbufDecoder()

        def decoded(width: int, height: int) -> tuple[int, int]:
            data = png(width, height)
            return size(decoder.decode(data, inspect_image(data)))

        assert decoded(1000, 600) == (MAX_DISPLAY, 288)  # scaled down, aspect kept
        assert decoded(400, 3000) == (64, MAX_DISPLAY)
        assert decoded(3, 2) == (3, 2)  # unchanged
        assert decoded(MAX_DISPLAY, MAX_DISPLAY) == (MAX_DISPLAY, MAX_DISPLAY)  # at the limit
        assert decoded(MAX_DISPLAY + 1, 1) == (MAX_DISPLAY, 1)

    def test_an_image_at_or_under_the_display_size_has_the_pixels_it_was_given(self) -> None:
        from tumbleweed.shell._gi import Gdk
        from tumbleweed.ui.gtk.imageview import PixbufDecoder
        from tumbleweed.ui.images import inspect_image

        data = png(5, 4, (10, 200, 30))

        texture = PixbufDecoder().decode(data, inspect_image(data))
        downloader = Gdk.TextureDownloader.new(texture)
        downloader.set_format(Gdk.MemoryFormat.R8G8B8A8)
        pixels, stride = downloader.download_bytes()
        raw = pixels.get_data()

        assert stride == 5 * 4
        assert raw == bytes([10, 200, 30, 255]) * (5 * 4)  # every pixel, as it was

    def test_the_tools_images_together_hold_at_most_the_pixel_budget(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from tumbleweed.ui.gtk import RenderedTool, imageview
        from tumbleweed.ui.gtk.imageview import MAX_DISPLAY, ImageView

        each = MAX_DISPLAY * (MAX_DISPLAY // 2) * 4  # an image of 2000 x 1000 is kept at 480 x 240
        monkeypatch.setattr(imageview, "MAX_TOOL_PIXEL_BYTES", each * 3 + 10)
        spec = spec_of(
            {"kind": "box", "children": [{"kind": "image", "bind": "img"}] * 8},
            [{"kind": "image", "key": "img"}],
        )
        tool = RenderedTool(spec, images=FakeResolver({"big.png": png(2000, 1000)}))
        views = of_type(tool, ImageView)

        tool.runtime.patch_state({"img": {"path": "big.png"}})
        assert pump_until(lambda: all(settled(v) for v in views), timeout=30)

        shown = [v for v in views if v.texture is not None]
        refused = [v for v in views if v.texture is None]
        assert len(shown) == 3  # what the budget holds
        assert {v.notice for v in refused} == {"The image is too large."}
        held = sum(v.texture.get_width() * v.texture.get_height() * 4 for v in shown if v.texture)
        assert held <= each * 3 + 10
        assert tool._context.pixels.total == held  # noqa: SLF001  (what the budget says it holds)

    def test_pixels_come_back_when_an_image_is_replaced_cleared_or_the_tool_closed(self) -> None:
        resolver = FakeResolver({"a.png": png(3, 2), "b.png": png(5, 4)})
        tool = render_with(resolver)
        view = view_of(tool)
        pixels = tool._context.pixels  # noqa: SLF001
        point_at(tool, "a.png")
        wait_for(view)
        assert pixels.total == 3 * 2 * 4

        point_at(tool, "b.png")  # the old image goes at once, and so does its claim
        assert pixels.total == 0
        wait_for(view)
        assert pixels.total == 5 * 4 * 4
        point_at(tool, None)
        assert pixels.total == 0
        point_at(tool, "a.png")
        wait_for(view)
        assert pixels.total == 3 * 2 * 4
        tool.close()

        assert pixels.total == 0

    def test_a_result_that_is_dropped_or_failed_holds_nothing(self) -> None:
        resolver = FakeResolver({"a.png": png(3, 2), "bad.png": png(3, 2)[:40]})
        resolver.gate = threading.Event()
        tool = render_with(resolver)
        view = view_of(tool)
        pixels = tool._context.pixels  # noqa: SLF001
        point_at(tool, "a.png")
        assert resolver.started.wait(5)
        point_at(tool, "bad.png")  # a.png's result is no longer wanted
        resolver.gate.set()
        wait_for(view)
        drain()

        assert view.notice == "The image could not be read."  # bad.png was refused by the decoder
        assert pixels.total == 0

    def test_the_budget_itself_replaces_a_reservation_and_refuses_what_does_not_fit(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from tumbleweed.ui.gtk import imageview
        from tumbleweed.ui.gtk.imageview import PixelBudget

        monkeypatch.setattr(imageview, "MAX_TOOL_PIXEL_BYTES", 100)
        budget = PixelBudget()
        one, two = object(), object()

        assert budget.reserve(one, 60) is True
        assert budget.reserve(two, 41) is False  # 101
        assert budget.reserve(two, 40) is True  # exactly the budget
        assert budget.reserve(one, 60) is True  # the same owner again replaces its claim
        assert budget.total == 100
        budget.release(one)
        budget.release(one)  # twice is harmless
        assert budget.total == 40


class TestSettingTheSameRefAgain:
    def test_putting_the_widgets_back_does_not_read_a_shown_image_again(self) -> None:
        resolver = FakeResolver({"a.png": png(3, 2)})
        tool = render_with(resolver)
        view = view_of(tool)
        point_at(tool, "a.png")
        wait_for(view)
        before = view.texture

        tool._context.resync()  # noqa: SLF001  (what a failed handler does)
        drain()

        assert view.texture is before  # not cleared, not decoded again
        assert len(resolver.reads) == 1

    def test_but_a_ref_assigned_again_is_still_read_again(self) -> None:
        resolver = FakeResolver({"a.png": png(3, 2)})
        tool = render_with(resolver)
        view = view_of(tool)
        point_at(tool, "a.png")
        wait_for(view)

        point_at(tool, "a.png")
        wait_for(view)

        assert len(resolver.reads) == 2


class TestAClosedToolsQueuedJobsDoNothing:
    def test_a_job_that_waited_for_a_worker_never_calls_the_resolver_once_its_tool_is_closed(
        self,
    ) -> None:
        from tumbleweed.ui.gtk import RenderedTool
        from tumbleweed.ui.gtk.imageview import WORKERS

        resolver = FakeResolver({"busy.png": png(3, 2), "waiting.png": png(3, 2)})
        resolver.gate = threading.Event()
        busy = spec_of(
            {"kind": "box", "children": [{"kind": "image", "bind": "img"}] * WORKERS},
            [{"kind": "image", "key": "img"}],
        )
        first = RenderedTool(busy, images=resolver)
        first.runtime.patch_state({"img": {"path": "busy.png"}})
        assert pump_until(lambda: resolver.most_active == WORKERS)  # every worker is in a read
        second = render_with(resolver)
        point_at(second, "waiting.png")  # queued behind them

        second.close()
        resolver.gate.set()
        drain()

        assert [path for path, _limit in resolver.reads] == ["busy.png"] * WORKERS
        first.close()


class TestInstalledLoaders:
    def test_they_are_looked_for_once(self) -> None:
        from tumbleweed.ui.gtk.imageview import installed_loaders

        assert installed_loaders() is installed_loaders()
        assert {"png", "jpeg"} <= installed_loaders()
