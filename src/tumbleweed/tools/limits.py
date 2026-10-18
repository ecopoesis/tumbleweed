"""The hard limits of a tool package, a bundle and a search (ARCHITECTURE I9).

A package and a bundle are hostile input (an imported file, a model's draft): every field is
bounded, and so is the whole. `tests/tools/test_limits.py` checks each `MAX_*` name here at its
limit and one past it.

- Text lengths are in Unicode code points; `MAX_MODULE_BYTES` and `MAX_MODULES_BYTES` are in
  UTF-8 bytes of source.
- `MAX_PACKAGE_BYTES` bounds the canonical JSON of a whole package, which is what is stored and
  hashed. It is what makes a package and its bundle fit one IPC frame (1 MiB) with room to spare:
  a JSON string that holds a document at most doubles its size (every quote and backslash gets one
  more), so the bundle as the text of an IPC message is under twice `MAX_BUNDLE_BYTES`.
- The spec inside a package is at most `ui.spec.limits.MAX_SPEC_BYTES` as canonical JSON, the
  same bound `parse_ui_spec` puts on the text it is given.
"""

from typing import Final

MAX_PACKAGE_BYTES: Final = 400 * 1024
MAX_BUNDLE_BYTES: Final = MAX_PACKAGE_BYTES + 1024  # the envelope: its format and the hash

MAX_NAME_LENGTH: Final = 64
MAX_DESCRIPTION_LENGTH: Final = 1_000
MAX_TAGS: Final = 8
MAX_TAG_LENGTH: Final = 32
MAX_CAPABILITIES: Final = 3
MAX_PORTS: Final = 16  # inputs, and outputs, each
MAX_MODULES: Final = 16
MAX_MODULE_BYTES: Final = 48 * 1024
MAX_MODULES_BYTES: Final = 96 * 1024

# What the store holds (checked by the broker, I9): more is refused, not evicted.
MAX_TOOLS: Final = 500
MAX_VERSIONS_PER_TOOL: Final = 100
MAX_STORE_BYTES: Final = 256 * 1024 * 1024  # of canonical packages, all versions

# A search.
MAX_QUERY_CHARS: Final = 200
MAX_QUERY_TERMS: Final = 8
MAX_TERM_CHARS: Final = 64
MAX_RESULTS: Final = 50
