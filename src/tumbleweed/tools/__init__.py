"""The tool package: what a saved tool is, and the one canonical form it is stored and hashed in.

Pure Python, no I/O, no GTK (an import-linter contract keeps it so); the broker keeps the store
(`tumbleweed.broker.toolstore`) and the shell can build and read packages without the broker.

- `package`: the model (`ToolPackage`: manifest, UI spec, code modules, taint), `ToolDraft` (what
  is saved, before an ID, a version and a taint are given), `canonical_package` (normalise, write
  RFC 8785 bytes, hash) and `parse_package`.
- `bundle`: the single-file form a package travels in (`export_bundle`, `parse_bundle`).
- `query`: the text of a search made safe for SQLite FTS5.
- `limits`, `errors`.

ARCHITECTURE section 5 is the contract; DECISIONS D19 records the choices.
"""

from tumbleweed.tools.bundle import BUNDLE_FORMAT, export_bundle, parse_bundle
from tumbleweed.tools.errors import ToolPackageError, ToolTooLargeError
from tumbleweed.tools.package import (
    PACKAGE_FORMAT,
    CanonicalPackage,
    CodeModule,
    Port,
    ToolDraft,
    ToolManifest,
    ToolPackage,
    ToolTag,
    build_package,
    canonical_package,
    parse_draft,
    parse_package,
)
from tumbleweed.tools.query import fts_match

__all__ = [
    "BUNDLE_FORMAT",
    "PACKAGE_FORMAT",
    "CanonicalPackage",
    "CodeModule",
    "Port",
    "ToolDraft",
    "ToolManifest",
    "ToolPackage",
    "ToolPackageError",
    "ToolTag",
    "ToolTooLargeError",
    "build_package",
    "canonical_package",
    "export_bundle",
    "fts_match",
    "parse_bundle",
    "parse_draft",
    "parse_package",
]
