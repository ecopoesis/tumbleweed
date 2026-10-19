"""The widget for an `image`: resolve, inspect, decode off the main thread, then show (I4, I9, I11).

`ImageView` shows the `ImageRef` of an entry as a `Gtk.Picture`, or a line of plain text of ours
when there is nothing to show (`Loading...`, `Out of date`, or why it was refused). What stands
between a ref and pixels is the order of `tumbleweed.ui.images`: the resolver gives bytes,
`inspect_image` checks format and size without decoding, and only then does a decoder see them.

**The decoder is pinned (`PixbufDecoder`).** gdk-pixbuf, asked to load bytes, sniffs them and picks
a loader among every one installed, SVG's included, which parses XML and may reference other files.
So the decoder never asks it to sniff: it makes the loader for the one format `inspect_image`
found (`PixbufLoader.new_with_type`), which a PNG loader given SVG text refuses as "not a PNG". The
loader reports the size it read (`size-prepared`) before it allocates the pixels, and if that is not
the size the header had, the loader is told to make it one pixel and the image is refused.

**Nothing slow runs on the main thread (I9).** Reading the file (the resolver may be an IPC round
trip, S19), inspecting and decoding happen in two daemon worker threads shared by every image in
the process, and the result comes back through `GLib.idle_add`. Decoding is bounded anyway
(`MAX_DECODED_BYTES`): a worker that is busy delays other images, never the interface. An
image widget has at most one job in flight and wants one ref at a time: a ref that arrives while
a job runs replaces the one waiting, and a result for a ref that is no longer wanted is dropped.
A resolver that never returns holds up its worker (it must bound its wait: its contract) and
nothing else.

Workers touch no widget: they build a `Gdk.Texture` (a plain, immutable pixel buffer, made in any
thread) or an `ImageError`.
"""

import functools
import logging
import queue
import threading
import weakref
from collections.abc import Callable
from typing import Final, Protocol, final

from tumbleweed.core.errors import excerpt
from tumbleweed.shell._gi import Gdk, GdkPixbuf, GLib, Gtk
from tumbleweed.ui.gtk.budget import TextBudget
from tumbleweed.ui.gtk.text import TextBlock
from tumbleweed.ui.images import (
    BYTES_PER_PIXEL,
    MAX_IMAGE_BYTES,
    ImageDecodeError,
    ImageError,
    ImageFormat,
    ImageInfo,
    ImageResolver,
    ImageTooLargeError,
    UnsupportedImageError,
    inspect_image,
)
from tumbleweed.ui.spec import ImageRef

logger = logging.getLogger(__name__)

LOADING: Final = "Loading the image..."
MAX_DISPLAY: Final = 480  # pixels: a bigger image is shown scaled down to fit, never scaled up
WORKERS: Final = 2  # images decoded at once, in the whole process
MAX_TOOL_PIXEL_BYTES: Final = 64 * 1024 * 1024  # decoded pixels a tool keeps, all its images
ERROR_CLASS: Final = "error"

_LOADERS: Final = {ImageFormat.PNG: "png", ImageFormat.JPEG: "jpeg", ImageFormat.WEBP: "webp"}


class ImageDecoder(Protocol):
    """Turns bytes that `inspect_image` has passed into pixels. Called on a worker thread."""

    def decode(self, data: bytes, info: ImageInfo) -> Gdk.Texture: ...


@functools.cache
def installed_loaders() -> frozenset[str]:
    """The names of the gdk-pixbuf loaders this system has (WebP's is a separate package).

    Found once (`submit` does it on the main thread before any worker starts)."""
    return frozenset(
        name for fmt in GdkPixbuf.Pixbuf.get_formats() if (name := fmt.get_name()) is not None
    )


@final
class PixbufDecoder:
    """The decoder: gdk-pixbuf, with the loader pinned to the format that was inspected."""

    def decode(self, data: bytes, info: ImageInfo) -> Gdk.Texture:
        # Not trusting the caller: the bytes are inspected again (a few dozen bytes), so that no
        # route reaches a loader with a size nobody has checked.
        if inspect_image(data) != info:
            raise ImageDecodeError("the bytes are not the image that was inspected")
        name = _LOADERS[info.format]
        if name not in installed_loaders():
            raise UnsupportedImageError(f"this system has no {name} loader")
        sizes: list[tuple[int, int]] = []
        native = (int(info.width), int(info.height))
        shown = fit(*native)  # what is kept: the display size, so a tool's pixels stay bounded

        def prepared(loader: GdkPixbuf.PixbufLoader, width: int, height: int) -> None:
            # Called before the loader allocates the pixels. Whatever it says that the header did
            # not: have it make one pixel instead, and refuse the image below. An image over the
            # display size is scaled to it (a size at or under it is left exactly as it is).
            sizes.append((width, height))
            if (width, height) != native:
                loader.set_size(1, 1)
            elif shown != native:
                loader.set_size(*shown)

        try:
            loader = GdkPixbuf.PixbufLoader.new_with_type(name)
            loader.connect("size-prepared", prepared)
            try:
                loader.write_bytes(GLib.Bytes.new(data))
            finally:
                loader.close()  # raises what the decoder found wrong, after a failed write too
            pixbuf = loader.get_pixbuf()
        except GLib.Error as exc:
            raise ImageDecodeError(f"the {name} loader refused the image") from exc
        if sizes != [native]:
            raise ImageDecodeError(f"the {name} loader saw another size than the header said")
        if (
            pixbuf is None
            or (pixbuf.get_width(), pixbuf.get_height()) != shown
            or pixbuf.get_bits_per_sample() != 8
            or pixbuf.get_n_channels() not in (3, BYTES_PER_PIXEL)
        ):
            raise ImageDecodeError(f"the {name} loader made something else than was asked")
        return Gdk.Texture.new_for_pixbuf(pixbuf)


def load_image(
    resolver: ImageResolver,
    decoder: ImageDecoder,
    ref: ImageRef,
    reserve: Callable[[ImageInfo], None] = lambda info: None,
) -> Gdk.Texture:
    """Resolve, inspect, reserve the pixels, decode: the whole pipeline, on a worker thread."""
    data = resolver.read(ref, MAX_IMAGE_BYTES)
    if len(data) > MAX_IMAGE_BYTES:  # the resolver was to stop at the limit; check, don't trust
        raise ImageDecodeError("the resolver returned more bytes than the limit")
    info = inspect_image(data)  # raises before any decoder sees the bytes
    reserve(info)  # raises ImageTooLargeError if the tool has no room for the pixels
    return decoder.decode(data, info)


@final
class PixelBudget:
    """The decoded pixels one tool keeps (I9): reserved before a decode, by the display size.

    Each image view holds one texture at most `MAX_DISPLAY` pixels a side (4 bytes a pixel); all
    of a tool's together are at most `MAX_TOOL_PIXEL_BYTES`, so a spec of hundreds of image views
    cannot ask for gigabytes. Used from the workers, so it takes a lock.
    """

    def __init__(self) -> None:
        self._held: dict[object, int] = {}
        self._lock = threading.Lock()

    @property
    def total(self) -> int:
        with self._lock:
            return sum(self._held.values())

    def reserve(self, owner: object, wanted: int) -> bool:
        with self._lock:
            held = sum(self._held.values()) - self._held.get(owner, 0)
            if held + wanted > MAX_TOOL_PIXEL_BYTES:
                return False
            self._held[owner] = wanted
            return True

    def release(self, owner: object) -> None:
        with self._lock:
            self._held.pop(owner, None)


# --- the workers ------------------------------------------------------------------------------

_jobs: queue.SimpleQueue[Callable[[], None]] = queue.SimpleQueue()
_workers_lock = threading.Lock()
_workers_started = False


def _work() -> None:
    while True:
        job = _jobs.get()
        try:
            job()
        except Exception:  # a job reports its own failures; this keeps the worker alive
            logger.exception("an image job failed")
        finally:
            del job  # not held while this worker waits: it refers to a widget that may be gone


def submit(job: Callable[[], None]) -> None:
    """Run `job` on one of the image workers (started on first use, as daemon threads)."""
    global _workers_started
    with _workers_lock:
        if not _workers_started:
            _workers_started = True
            installed_loaders()  # find the loaders here, on the main thread, before any worker
            for number in range(WORKERS):
                threading.Thread(
                    target=_work, name=f"tumbleweed-image-{number}", daemon=True
                ).start()
    _jobs.put(job)


def fit(width: int, height: int) -> tuple[int, int]:
    """The size to show a `width` by `height` image at: scaled down to `MAX_DISPLAY`, not up."""
    scale = min(1.0, MAX_DISPLAY / width, MAX_DISPLAY / height)
    return max(1, round(width * scale)), max(1, round(height * scale))


# --- the widget -------------------------------------------------------------------------------


@final
class ImageView(Gtk.Box):
    """An image, or a line saying why there is none. All of it belongs to the main thread."""

    def __init__(
        self,
        alt: str,
        resolver: ImageResolver,
        decoder: ImageDecoder,
        budget: TextBudget,
        pixels: PixelBudget,
    ) -> None:
        super().__init__(orientation=Gtk.Orientation.VERTICAL)
        self._resolver = resolver
        self._decoder = decoder
        self._pixels = pixels
        self._held: object | None = None  # what this view holds of the pixel budget
        self._ref: ImageRef | None = None  # what was asked for last
        self._stale = False
        self._picture = Gtk.Picture(
            can_shrink=True, content_fit=Gtk.ContentFit.CONTAIN, halign=Gtk.Align.START
        )
        self._picture.set_alternative_text(alt)  # for assistive technology: never displayed
        self._picture.set_visible(False)
        self._notice = TextBlock(budget=budget)
        self._notice.set_visible(False)
        self.append(self._picture)
        self.append(self._notice)
        self._generation = 0  # which request is current: a result for another is dropped
        self._wanted: ImageRef | None = None  # the ref nobody is loading yet
        self._inflight = False
        self._cancelled = threading.Event()  # set when the tool is closed: jobs read it, no view

    @property
    def closed(self) -> bool:
        return self._cancelled.is_set()

    @property
    def texture(self) -> Gdk.Texture | None:
        """The pixels on show, if there are any."""
        paintable = self._picture.get_paintable()
        return paintable if isinstance(paintable, Gdk.Texture) else None

    @property
    def notice(self) -> str:
        """The line of text shown instead of an image ('' if none)."""
        return self._notice.shown_text if self._notice.get_visible() else ""

    def release(self) -> None:
        """Stop for good: a result still on its way is dropped and the pixels are let go."""
        self._cancelled.set()
        self._wanted = None
        self._clear()

    def set_ref(self, ref: ImageRef | None, *, stale: bool, refresh: bool = True) -> None:
        """Show what `ref` names, once it is loaded (the old image goes at once, so that the
        widget never shows an image the state no longer holds).

        The same ref again is read again (a handler that rewrote the file assigns an equal ref),
        unless `refresh` is False, which is what putting the widgets back to the state is."""
        if not refresh and ref == self._ref and stale == self._stale:
            return
        self._ref, self._stale = ref, stale
        self._generation += 1
        self._wanted = None
        self._clear()
        if stale:
            self._say("", stale=True)
        elif ref is not None:
            self._say(LOADING)
            self._wanted = ref
            self._start()

    def _clear(self) -> None:
        self._release_pixels()
        self._picture.set_paintable(None)
        self._picture.set_visible(False)
        self._hide_notice()

    def _release_pixels(self) -> None:
        if self._held is not None:
            self._pixels.release(self._held)
            self._held = None

    def _hide_notice(self) -> None:
        self._notice.set_text("")  # which gives back what the line had claimed of the budget
        self._notice.set_visible(False)
        self._notice.remove_css_class(ERROR_CLASS)

    def _say(self, text: str, *, stale: bool = False, failed: bool = False) -> None:
        self._notice.set_text(text, stale=stale)
        self._notice.set_visible(True)
        if failed or stale:
            self._notice.add_css_class(ERROR_CLASS)
        else:
            self._notice.remove_css_class(ERROR_CLASS)

    def _start(self) -> None:
        ref = self._wanted
        if ref is None or self._inflight or self.closed:
            return
        self._inflight = True
        generation = self._generation
        resolver, decoder = self._resolver, self._decoder
        # The job holds the flag and a weak reference, not the view: a job waiting in the queue
        # for a worker must not keep the widgets of a tool that was closed meanwhile alive.
        cancelled = self._cancelled
        target = weakref.ref(self)
        pixels = self._pixels
        token = object()  # what this job holds of the pixel budget, until a view takes it over

        def reserve(info: ImageInfo) -> None:
            width, height = fit(info.width, info.height)
            if not pixels.reserve(token, width * height * BYTES_PER_PIXEL):
                raise ImageTooLargeError("the tool's images would hold too many pixels")

        def job() -> None:  # on a worker: no widget is touched
            if cancelled.is_set():  # nobody waits for it any more
                return
            outcome: Gdk.Texture | ImageError
            try:
                outcome = load_image(resolver, decoder, ref, reserve)
            except ImageError as error:
                logger.warning("image %s not shown: %s", excerpt(str(ref.path)), error)
                outcome = error
            except Exception:
                logger.exception("image %s not shown", excerpt(str(ref.path)))
                outcome = ImageError("unexpected failure")
            GLib.idle_add(_deliver, target, generation, outcome, pixels, token)

        submit(job)

    def arrived(self, generation: int, outcome: Gdk.Texture | ImageError, token: object) -> None:
        """A worker's result, on the main thread (`_deliver`). `token` is what the job reserved
        of the pixel budget: this view keeps it if it shows the image, else it is given back."""
        self._inflight = False
        if self.closed:
            self._pixels.release(token)
            return
        if generation != self._generation or isinstance(outcome, ImageError):
            self._pixels.release(token)
        if generation != self._generation:
            self._start()  # a newer ref has been waiting
            return
        self._wanted = None
        try:
            if isinstance(outcome, ImageError):
                self._say(outcome.shown, failed=True)
            else:
                self._hide_notice()
                self._held = token
                self._picture.set_paintable(outcome)
                self._picture.set_size_request(*fit(outcome.get_width(), outcome.get_height()))
                self._picture.set_visible(True)
        except Exception:
            logger.exception("an image could not be put on show")
            self._clear()
            self._say(ImageError.shown, failed=True)


def _deliver(
    target: "weakref.ReferenceType[ImageView]",
    generation: int,
    outcome: Gdk.Texture | ImageError,
    pixels: PixelBudget,
    token: object,
) -> bool:
    view = target()
    if view is None:
        pixels.release(token)
    else:
        view.arrived(generation, outcome, token)
    return GLib.SOURCE_REMOVE
