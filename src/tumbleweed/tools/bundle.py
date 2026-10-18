"""The single-file form a tool package travels in: one canonical JSON document, no archive.

A bundle is `{"format": "tumbleweed.tool-bundle/1", "hash": "<sha-256>", "package": {...}}`: the
package exactly as it is stored (its canonical bytes embedded as they are) and the hash of those
bytes. There is no archive format on purpose: nothing in it can name a path, nest, expand or
repeat a member, so a zip bomb or a traversal has nothing to be made of. It is parsed by the same
strict models as a stored package, so it can carry nothing a package cannot: no grant, no state
value, no task, no unknown field anywhere (I8: tools do not carry privilege; an imported tool
must be granted again).

`parse_bundle` is what import calls on hostile bytes. It takes a bundle only if all of these hold,
and otherwise raises `ToolPackageError` and changes nothing:

- it is at most `MAX_BUNDLE_BYTES`, checked before it is parsed;
- it is UTF-8 JSON with no repeated key, an object with exactly the three members above;
- the package is valid (`parse_package`: every limit, every cross-check), and **is already in
  normal form**: NFC, LF, canonical RFC 8785 bytes, exactly what `canonical_package` would write
  for it. A bundle that is not (a hand-edited one, one that leaves out a default) is refused, not
  repaired, so that the hash a bundle states is the hash of what is stored;
- the stated hash is the SHA-256 of those bytes.

What a bundle does not carry is also what it cannot vouch for: its `taint` is the exporter's
claim, kept as written (a claim can only be of a restriction: nothing is gained by claiming none
for code that holds no data of ours, and the store never lowers the taint of a version it has).
"""

from typing import Final

from tumbleweed.core.errors import InvalidValueError
from tumbleweed.core.jcs import canonicalize
from tumbleweed.core.types import ContentHash
from tumbleweed.tools.errors import ToolPackageError, ToolTooLargeError
from tumbleweed.tools.limits import MAX_BUNDLE_BYTES
from tumbleweed.tools.package import (
    CanonicalPackage,
    canonical_package,
    load_json,
    parse_package,
)

BUNDLE_FORMAT: Final = "tumbleweed.tool-bundle/1"
_MEMBERS: Final = frozenset({"format", "hash", "package"})


def export_bundle(canonical: CanonicalPackage) -> bytes:
    """The bundle of a stored package, as UTF-8 bytes.

    Written by hand around the package's own bytes (the members are in canonical order:
    `format`, `hash`, `package`), so that what is exported is exactly what is hashed.
    """
    return (
        b'{"format":"'
        + BUNDLE_FORMAT.encode("ascii")
        + b'","hash":"'
        + str(canonical.hash).encode("ascii")
        + b'","package":'
        + canonical.data
        + b"}"
    )


def parse_bundle(data: bytes) -> CanonicalPackage:
    """The package in a bundle, if the bundle is acceptable (see the module docstring).

    Raises `ToolPackageError` (`ToolTooLargeError` if it is over `MAX_BUNDLE_BYTES`) and nothing
    else; the text never repeats the input beyond a short excerpt.
    """
    if type(data) is not bytes:
        raise ToolPackageError([f"expected bytes, got {type(data).__name__}"])
    if len(data) > MAX_BUNDLE_BYTES:
        raise ToolTooLargeError([f"the bundle is over {MAX_BUNDLE_BYTES} bytes"])
    document = load_json(data, what="bundle")
    if not isinstance(document, dict) or document.keys() != _MEMBERS:
        raise ToolPackageError(["a bundle has exactly the members format, hash and package"])
    if document["format"] != BUNDLE_FORMAT:
        raise ToolPackageError([f"the bundle format is not {BUNDLE_FORMAT}"])
    try:
        stated = ContentHash(document["hash"])
    except InvalidValueError as error:
        raise ToolPackageError([f"the bundle's hash: {error}"]) from None
    try:
        received = canonicalize(document["package"]).encode("utf-8")
    except ValueError as error:
        raise ToolPackageError([f"the package cannot be written canonically: {error}"]) from None
    canonical = canonical_package(parse_package(received))
    if canonical.data != received:
        raise ToolPackageError(
            ["the package is not in normal form (NFC, LF, every field written, canonical JSON)"]
        )
    if canonical.hash != stated:
        raise ToolPackageError(["the bundle's hash is not the hash of its package"])
    return canonical
