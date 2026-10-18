"""Every limit of `tumbleweed.tools.limits` at the limit and one past it (S23, I9)."""

import inspect

import pytest

from tests.tools.support import module, package
from tests.ui.spec.support import Json, accepted, build
from tumbleweed.core.types import JsonValue
from tumbleweed.tools import ToolDraft, ToolPackageError, parse_draft
from tumbleweed.tools import limits as limits_module
from tumbleweed.tools.limits import (
    MAX_CAPABILITIES,
    MAX_DESCRIPTION_LENGTH,
    MAX_MODULE_BYTES,
    MAX_MODULES,
    MAX_MODULES_BYTES,
    MAX_NAME_LENGTH,
    MAX_PORTS,
    MAX_TAG_LENGTH,
    MAX_TAGS,
)
from tumbleweed.ui.spec.limits import MAX_SPEC_BYTES
from tumbleweed.ui.spec.names import ModuleName

from .support import csv_ui, draft, text_of
from .test_package import draft_json, draft_problems


def accepted_draft(**changes: object) -> ToolDraft:
    return parse_draft(text_of(draft_json(**changes)))  # type: ignore[arg-type]  # JSON


def test_every_limit_has_a_test_here_or_in_its_own_module() -> None:
    names = {name for name, _ in inspect.getmembers(limits_module) if name.startswith("MAX_")}

    covered = {
        "MAX_NAME_LENGTH", "MAX_DESCRIPTION_LENGTH", "MAX_TAGS", "MAX_TAG_LENGTH",
        "MAX_CAPABILITIES", "MAX_PORTS", "MAX_MODULES", "MAX_MODULE_BYTES", "MAX_MODULES_BYTES",
        "MAX_PACKAGE_BYTES",  # tests/tools/test_package.py: TestSize
        "MAX_BUNDLE_BYTES",  # tests/tools/test_bundle.py: TestWhatImportRefuses
        "MAX_QUERY_CHARS", "MAX_QUERY_TERMS", "MAX_TERM_CHARS",  # tests/tools/test_query.py
        "MAX_RESULTS",  # tests/broker/test_toolstore.py
        "MAX_TOOLS", "MAX_VERSIONS_PER_TOOL", "MAX_STORE_BYTES",  # tests/broker/test_toolstore.py
    }  # fmt: skip
    assert names == covered


def test_the_name() -> None:
    accepted_draft(name="n" * MAX_NAME_LENGTH)
    assert draft_problems(draft_json(name="n" * (MAX_NAME_LENGTH + 1)))
    assert draft_problems(draft_json(name=""))


def test_the_description() -> None:
    accepted_draft(description="d" * MAX_DESCRIPTION_LENGTH)
    assert draft_problems(draft_json(description="d" * (MAX_DESCRIPTION_LENGTH + 1)))


def test_the_tags_and_their_length() -> None:
    tags = [f"t{n}" for n in range(MAX_TAGS)]
    accepted_draft(tags=tags)
    assert draft_problems(draft_json(tags=[*tags, "one-more"]))
    accepted_draft(tags=["t" * MAX_TAG_LENGTH])
    assert draft_problems(draft_json(tags=["t" * (MAX_TAG_LENGTH + 1)]))


@pytest.mark.parametrize(
    "tag", ["Upper", "has space", "-lead", "a_b", "e\N{COMBINING ACUTE ACCENT}", ""]
)
def test_a_tag_is_lowercase_ascii_words(tag: str) -> None:
    assert draft_problems(draft_json(tags=[tag]))


def test_the_capabilities_are_the_three_kinds_and_distinct() -> None:
    kinds = ["fs.read", "fs.write", "net.full"]
    assert len(kinds) == MAX_CAPABILITIES
    accepted_draft(capabilities=kinds)
    assert draft_problems(draft_json(capabilities=[*kinds, "fs.read"]))
    assert draft_problems(draft_json(capabilities=["root"]))


def plain(count: int = 0) -> Json:
    """A draft whose spec has `count` string entries (no handlers) and nothing else."""
    entries: list[Json] = [{"kind": "string", "key": f"k{n}", "default": ""} for n in range(count)]
    return {"name": "n", "ui": accepted(build(state=entries)).model_dump(mode="json")}


def ports(count: int) -> list[JsonValue]:
    return [{"name": f"k{n}", "type": "text"} for n in range(count)]


def test_the_ports_of_each_direction() -> None:
    fits = plain(MAX_PORTS)
    parse_draft(text_of({**fits, "inputs": ports(MAX_PORTS), "outputs": ports(MAX_PORTS)}))

    over = plain(MAX_PORTS + 1)
    assert draft_problems({**over, "inputs": ports(MAX_PORTS + 1)})
    assert draft_problems({**over, "outputs": ports(MAX_PORTS + 1)})


def test_the_number_of_modules() -> None:
    def modules(count: int) -> list[JsonValue]:
        return [{"name": f"m{n}", "source": "x"} for n in range(count)]

    parse_draft(text_of({**plain(), "modules": modules(MAX_MODULES)}))
    assert draft_problems({**plain(), "modules": modules(MAX_MODULES + 1)})


def test_the_size_of_a_module_is_in_utf8_bytes() -> None:
    def one(source: str) -> Json:
        data = draft_json(modules=[{"name": "csv_view", "source": source}])
        return data

    parse_draft(text_of(one("a" * MAX_MODULE_BYTES)))
    assert draft_problems(one("a" * (MAX_MODULE_BYTES + 1)))
    two_byte = "é" * (MAX_MODULE_BYTES // 2)  # exactly the limit in bytes
    parse_draft(text_of(one(two_byte)))
    assert draft_problems(one(two_byte + "a"))
    assert draft_problems(one("é" * (MAX_MODULE_BYTES // 2 + 1)))


def test_the_size_of_all_modules() -> None:
    each = MAX_MODULES_BYTES // 3
    assert each <= MAX_MODULE_BYTES

    def three(extra: int) -> Json:
        names = ["csv_view", "m2", "m3"]
        sources = ["a" * each, "b" * each, "c" * (each + extra)]
        return draft_json(
            modules=[{"name": n, "source": s} for n, s in zip(names, sources, strict=True)]
        )

    parse_draft(text_of(three(0)))
    assert any("more than" in p for p in draft_problems(three(1)))


def test_the_spec_limit_is_the_ui_specs_own() -> None:
    assert MAX_SPEC_BYTES == 256 * 1024


def test_a_valid_draft_at_none_of_the_limits_is_fine() -> None:
    assert package(source=draft()).package.manifest.name == "CSV viewer"
    assert csv_ui() is not None
    assert module().name == ModuleName("csv_view")
    assert isinstance(accepted_draft(), ToolDraft)
    with pytest.raises(ToolPackageError):
        parse_draft(b"{}")
