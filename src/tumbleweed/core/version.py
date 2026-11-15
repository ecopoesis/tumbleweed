"""The installed version of the `tumbleweed` distribution, which every program prints for
`--version` (S26)."""

from importlib import metadata


def installed_version() -> str:
    """The version in the distribution's metadata, or a line saying there is none (a source tree
    run without installing it)."""
    try:
        return metadata.version("tumbleweed")
    except metadata.PackageNotFoundError:
        return "unknown (the package is not installed)"
