"""The lock of the hands-free dependencies (S08, DECISIONS D23, #42).

Hands-free mode runs on `onnxruntime` and `numpy`. A dependency that resolves differently per
platform without a word (what `openwakeword` did: 0.4.0 on Linux, 0.6.0 on macOS, because its Linux
build needs `tflite-runtime`, which has no wheels for Python 3.12+) is the failure #42 records, so
this checks, from `uv.lock`, that each is locked once, to one version, and has a binary wheel for
every Python CI runs (3.12 to 3.14) on every platform we lock; that the package whose
dependencies would not install is not there; and that the Python floor is what the wheels need.
"""

import tomllib
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
PYTHONS = ("cp312", "cp313", "cp314")
# (wheel tag fragment, name of the platform in `[tool.uv] environments`)
PLATFORMS = (
    ("macosx_", "arm64"),
    ("manylinux", "x86_64"),
    ("manylinux", "aarch64"),
)


def locked() -> dict[str, list[dict[str, object]]]:
    lock = tomllib.loads((ROOT / "uv.lock").read_text(encoding="utf-8"))
    packages: dict[str, list[dict[str, object]]] = {}
    for package in lock["package"]:
        assert isinstance(package, dict)
        packages.setdefault(str(package["name"]), []).append(package)
    return packages


def wheel_names(package: dict[str, object]) -> list[str]:
    wheels = package["wheels"]
    assert isinstance(wheels, list)
    return [str(wheel["url"]).rsplit("/", 1)[-1] for wheel in wheels]


@pytest.mark.parametrize("name", ["onnxruntime", "numpy"])
def test_it_is_locked_once_to_one_version_for_every_platform(name: str) -> None:
    entries = locked()[name]

    assert len(entries) == 1, f"{name} resolves to {[entry['version'] for entry in entries]}"
    assert "resolution-markers" not in entries[0]


@pytest.mark.parametrize("name", ["onnxruntime", "numpy"])
@pytest.mark.parametrize("python", PYTHONS)
@pytest.mark.parametrize(("fragment", "machine"), PLATFORMS)
def test_it_has_a_wheel_for_every_python_on_every_platform(
    name: str, python: str, fragment: str, machine: str
) -> None:
    (entry,) = locked()[name]

    matching = [
        wheel
        for wheel in wheel_names(entry)
        if f"-{python}-{python}-" in wheel
        and fragment in wheel
        and (machine in wheel or (machine == "arm64" and "arm64" in wheel))
    ]

    assert matching, f"no {python} wheel of {name} for {fragment}{machine}"


def test_the_package_whose_dependencies_do_not_install_is_not_used() -> None:
    packages = locked()

    assert "openwakeword" not in packages
    assert "tflite-runtime" not in packages


def test_the_dependencies_are_declared_with_a_floor_and_a_reason() -> None:
    text = (ROOT / "pyproject.toml").read_text(encoding="utf-8")
    config = tomllib.loads(text)

    dependencies = config["project"]["dependencies"]
    assert any(dep.startswith("onnxruntime>=") for dep in dependencies)
    assert any(dep.startswith("numpy>=") for dep in dependencies)
    assert "tflite-runtime" in text  # the comment says why the package is not used
