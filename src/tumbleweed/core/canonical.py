"""What the tool store hashes: normalised text, RFC 8785 bytes, and the SHA-256 of those bytes.

A saved tool is a JSON document. Before it is stored, every string in it (values and object
keys alike) is normalised: Unicode NFC, and a line feed for any line ending (`\\r\\n` and `\\r`).
Then `tumbleweed.core.jcs` writes it as RFC 8785 canonical JSON, and the SHA-256 of those UTF-8
bytes is its `ContentHash`. Normalisation happens before the bytes are made and kept, so the
stored bytes are the normalised bytes and the hash covers exactly what is executed from them
(ARCHITECTURE section 5); two texts that differ only in how an accent or a line ending is
spelled are one text once saved.

Normalising can change a text's length (NFC can expand a few characters) and can make two
object keys equal, so a caller validates the normalised value, never the value it started from,
and a collision is a `ValueError` here rather than a silently lost member. Nothing in this module
validates a value beyond that: `jcs.canonicalize` refuses what is not JSON, and the caller's
models refuse what is not theirs.
"""

import hashlib
import unicodedata

from tumbleweed.core.jcs import MAX_NODES, canonicalize
from tumbleweed.core.types import ContentHash
from tumbleweed.core.unicode15 import first_unstable

_MAX_DEPTH = 200  # as in `jcs`; deeper is refused there too, this only stops the copy early


def normalize_text(value: str) -> str:
    """`value` as the tool store writes text: NFC, with a line feed for any line ending.

    NFC is the running Python's own (`unicodedata`); `normalize_tree` is what refuses text whose
    normal form could differ between Python versions."""
    return unicodedata.normalize("NFC", value.replace("\r\n", "\n").replace("\r", "\n"))


def _stable(text: str) -> str:
    if first_unstable(text) is not None:
        raise ValueError("text holds a character that Unicode 15.0 had not assigned")
    return text


def normalize_tree(value: object, *, max_nodes: int = MAX_NODES) -> object:
    """A copy of a JSON value in which every string, object keys included, is normalised.

    Iterative, and bounded like `jcs.canonicalize` (`max_nodes`, depth 200). Only exact `dict`,
    `list` and `str` are walked; any other value is passed through unchanged for the
    canonicaliser to accept or refuse. `ValueError` if a string holds a code point that Unicode
    15.0 had not assigned (its NFC could differ between Pythons, so its hash would: `unicode15`),
    if two keys of an object are one key after normalisation, or if the value is too big or deep.
    """
    holder: list[object] = [None]
    stack: list[tuple[object, list[object] | dict[str, object], int | str, int]] = [
        (value, holder, 0, 1)
    ]
    nodes = 0
    while stack:
        item, container, slot, depth = stack.pop()
        nodes += 1
        if nodes > max_nodes:
            raise ValueError(f"more than {max_nodes} values")
        if depth > _MAX_DEPTH:
            raise ValueError(f"nested deeper than {_MAX_DEPTH}")
        kind = type(item)
        copy: object
        if isinstance(item, str) and kind is str:
            copy = _stable(normalize_text(item))
        elif isinstance(item, list) and kind is list:
            if nodes + len(item) > max_nodes:
                raise ValueError(f"more than {max_nodes} values")
            items: list[object] = [None] * len(item)
            copy = items
            stack.extend((child, items, index, depth + 1) for index, child in enumerate(item))
        elif isinstance(item, dict) and kind is dict:
            if nodes + 2 * len(item) > max_nodes:
                raise ValueError(f"more than {max_nodes} values")
            members: dict[str, object] = {}
            copy = members
            for key, child in item.items():
                if type(key) is not str:
                    raise ValueError(
                        f"a key of an object must be a string, got {type(key).__name__}"
                    )
                name = _stable(normalize_text(key))
                if name in members:
                    raise ValueError("two keys of an object are one key once normalised")
                members[name] = None
                stack.append((child, members, name, depth + 1))
            nodes += len(item)
        else:
            copy = item
        match container, slot:
            case list(), int():
                container[slot] = copy
            case dict(), str():
                container[slot] = copy
    return holder[0]


def canonical_bytes(value: object) -> bytes:
    """The bytes the tool store keeps for a JSON value: normalised, then RFC 8785, as UTF-8."""
    return canonicalize(normalize_tree(value)).encode("utf-8")


def content_hash(data: bytes) -> ContentHash:
    """The `ContentHash` of canonical bytes: their SHA-256 as lowercase hex."""
    return ContentHash(hashlib.sha256(data).hexdigest())
