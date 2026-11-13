"""The code of the photo-adjust tool of the S20 tests: the golden spec's `photo` module, and a
photo to run it on (a PNG made with the standard library, so the test process needs no Pillow)."""

import struct
import zlib
from pathlib import Path

PHOTO_MODULE = """
import hashlib

import numpy
from PIL import Image, ImageEnhance, ImageOps


def adjust(inputs):
    try:
        image = Image.open(inputs["photo_path"])
        image.load()
    except (OSError, ValueError):
        return {"status": "Could not open that photo."}
    image = image.convert("RGB")
    image.thumbnail((320, 320))
    if inputs["grayscale"]:
        image = ImageOps.grayscale(image).convert("RGB")
    image = ImageEnhance.Brightness(image).enhance(1 + inputs["brightness"] / 100)
    image = ImageEnhance.Contrast(image).enhance(1 + inputs["contrast"] / 100)
    mean = float(numpy.asarray(image).mean())
    name = "adjusted-" + hashlib.sha256(image.tobytes()).hexdigest()[:10] + ".jpg"
    image.save(name, "JPEG", quality=85)
    return {"result": {"path": name}, "status": "Done. Average level " + str(round(mean)) + "."}
"""


def png(width: int = 64, height: int = 48) -> bytes:
    """A small RGB gradient as PNG bytes."""

    def chunk(kind: bytes, data: bytes) -> bytes:
        body = kind + data
        return struct.pack(">I", len(data)) + body + struct.pack(">I", zlib.crc32(body))

    rows = b"".join(
        b"\x00" + bytes(v for x in range(width) for v in (x * 4 % 256, y * 5 % 256, 90))
        for y in range(height)
    )
    return (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0))
        + chunk(b"IDAT", zlib.compress(rows))
        + chunk(b"IEND", b"")
    )


def write_photo(directory: Path, name: str = "a.png") -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / name
    path.write_bytes(png())
    return path
