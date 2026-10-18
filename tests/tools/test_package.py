"""The tool package: its model, its canonical bytes, its hash and what it refuses (S23)."""

import json
from typing import TYPE_CHECKING, cast

import pytest

from tests.tools.support import SOURCE, draft, module, package, text_of, tree
from tests.ui.spec.support import Json, accepted, build
from tumbleweed.core.ids import ToolId
from tumbleweed.core.jcs import canonicalize
from tumbleweed.core.types import ContentHash, JsonValue, ToolVersion
from tumbleweed.tools import (
    Port,
    ToolDraft,
    ToolPackage,
    ToolPackageError,
    ToolTooLargeError,
    build_package,
    canonical_package,
    parse_draft,
    parse_package,
)
from tumbleweed.tools.limits import MAX_MODULE_BYTES, MAX_PACKAGE_BYTES

if TYPE_CHECKING:
    from tumbleweed.tools.package import Taint

NFD = "e\N{COMBINING ACUTE ACCENT}"
EXPANDS = chr(0x344)  # one character as written, two once normalised
NFC = "\N{LATIN SMALL LETTER E WITH ACUTE}"


def at(data: Json, where: tuple[str | int, ...]) -> Json:
    """The object of parsed JSON at a path of member names and list positions."""
    node: JsonValue = data
    for step in where:
        node = node[step] if isinstance(node, dict | list) else node  # type: ignore[index]  # JSON
    assert isinstance(node, dict)
    return node


def draft_json(**changes: JsonValue) -> Json:
    data = json.loads(draft().model_dump_json())
    assert isinstance(data, dict)
    return data | changes


def draft_problems(data: Json) -> list[str]:
    with pytest.raises(ToolPackageError) as caught:
        parse_draft(text_of(data))
    return list(caught.value.problems)


def problems_of_parse(data: bytes) -> list[str]:
    with pytest.raises(ToolPackageError) as caught:
        parse_package(data)
    return list(caught.value.problems)


class TestRoundTripAndHash:
    def test_what_is_stored_parses_back_to_the_same_package(self) -> None:
        stored = package(taint=("fs",))

        again = parse_package(stored.data)

        assert again == stored.package
        assert canonical_package(again).data == stored.data
        assert canonical_package(again).hash == stored.hash

    def test_the_bytes_are_canonical_json_and_the_hash_is_their_sha256(self) -> None:
        import hashlib

        stored = package()

        assert stored.data == canonicalize(json.loads(stored.data)).encode("utf-8")
        assert stored.hash == hashlib.sha256(stored.data).hexdigest()
        assert type(stored.hash) is ContentHash

    def test_the_same_tool_gives_the_same_bytes_and_hash_every_time(self) -> None:
        tool = ToolId.new()

        assert package(tool=tool).data == package(tool=tool).data
        assert package(tool=tool).hash == package(tool=tool).hash

    def test_a_golden_hash_is_stable(self) -> None:
        # A literal: the canonical form of a package is a contract, and every stored hash and every
        # tool grant depends on it. A change here must be a decision.
        tool = ToolId("tool_01m56q3ayn1sqq4f1newcs9742")

        stored = package(tool=tool)

        assert stored.hash == GOLDEN_HASH, stored.data.decode()

    @pytest.mark.parametrize(
        "change",
        [
            {"name": "Other name"},
            {"description": "Other."},
            {"tags": ("csv",)},
            {"capabilities": ()},
            {"modules": (module("csv_view", SOURCE + "\n"),)},
        ],
        ids=lambda change: next(iter(change)),
    )
    def test_any_change_to_what_runs_or_is_declared_changes_the_hash(
        self, change: dict[str, object]
    ) -> None:
        tool = ToolId.new()
        base = draft()

        changed = draft(**change)  # type: ignore[arg-type]  # the parametrized keywords of `draft`

        assert package(tool=tool, source=changed).hash != package(tool=tool, source=base).hash

    def test_the_version_and_the_taint_are_part_of_the_hash(self) -> None:
        tool = ToolId.new()

        assert package(tool=tool, version=1).hash != package(tool=tool, version=2).hash
        assert package(tool=tool, taint=()).hash != package(tool=tool, taint=("fs",)).hash
        assert package(tool=tool, taint=("fs",)).hash != package(tool=tool, taint=("net",)).hash

    def test_tags_and_capabilities_are_sets_and_have_one_form(self) -> None:
        tool = ToolId.new()
        one = draft(tags=("b", "a"), capabilities=("net.full", "fs.read"))
        other = draft(tags=("a", "b"), capabilities=("fs.read", "net.full"))

        assert package(tool=tool, source=one).hash == package(tool=tool, source=other).hash
        assert package(tool=tool, source=one).package.manifest.tags == ("a", "b")


class TestNormalisationOnSave:
    def test_the_stored_bytes_are_the_normalised_bytes(self) -> None:
        source = "x = 'é'\r\ny = 1\r\n".replace("é", NFD)
        stored = package(source=draft(modules=(module("csv_view", source),)))

        assert stored.package.modules[0].source == f"x = '{NFC}'\ny = 1\n"
        assert NFD.encode() not in stored.data
        assert b"\\r" not in stored.data

    def test_authored_with_nfd_and_crlf_or_nfc_and_lf_is_one_tool(self) -> None:
        tool = ToolId.new()
        raw = draft(
            name=f"caf{NFD}",
            description=f"th{NFD}",
            modules=(module("csv_view", f"a = '{NFD}'\r\nb = 2\r"),),
        )
        clean = draft(
            name=f"caf{NFC}",
            description=f"th{NFC}",
            modules=(module("csv_view", f"a = '{NFC}'\nb = 2\n"),),
        )

        assert package(tool=tool, source=raw).data == package(tool=tool, source=clean).data
        assert package(tool=tool, source=raw).hash == package(tool=tool, source=clean).hash

    def test_a_string_default_of_the_spec_is_normalised_too(self) -> None:
        state = [{"kind": "string", "key": "note", "default": f"{NFD}\r\n"}]
        made = ToolDraft(name="n", ui=accepted(build(state=state)))  # type: ignore[arg-type]  # JSON

        stored = package(source=made)

        assert stored.package.ui.state[0].default == f"{NFC}\n"

    def test_what_normalising_makes_invalid_is_refused_not_stored(self) -> None:
        # U+0344 is one character and two once normalised: it fits max_length 1 as written and
        # not as stored. The package is validated again after normalising.
        state = [{"kind": "string", "key": "note", "default": EXPANDS, "max_length": 1}]
        made = ToolDraft(name="n", ui=accepted(build(state=state)))  # type: ignore[arg-type]  # JSON

        with pytest.raises(ToolPackageError, match="2 characters, the limit is 1"):
            package(source=made)

    def test_a_failure_to_write_canonically_is_a_package_error(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        def refuse(_value: object) -> bytes:
            raise ValueError("two keys are one")

        monkeypatch.setattr("tumbleweed.tools.package.canonical_bytes", refuse)

        with pytest.raises(ToolPackageError, match="cannot be written canonically"):
            package()


class TestDraftsAreNormalisedThenValidated:
    def test_a_name_is_judged_by_the_length_it_is_stored_with(self) -> None:
        letter = "e\N{COMBINING ACUTE ACCENT}"
        nfd = letter * 40  # 80 characters, 40 once stored
        data = draft_json(name=nfd)

        made = parse_draft(text_of(data))

        assert made.name == "\N{LATIN SMALL LETTER E WITH ACUTE}" * 40
        assert len(nfd) == 80
        assert package(source=made).package.manifest.name == made.name

    def test_text_that_is_too_long_even_once_stored_is_refused(self) -> None:
        assert draft_problems(draft_json(name="\N{LATIN SMALL LETTER E WITH ACUTE}" * 65))

    def test_crlf_in_code_is_one_character_shorter_per_line_when_checked(self) -> None:
        entry: Json = {"name": "csv_view", "source": "a\r\n" * (MAX_MODULE_BYTES // 3)}

        assert parse_draft(text_of(draft_json(modules=[entry]))).modules[0].source.count("\r") == 0

    def test_a_character_unicode_15_had_not_assigned_is_refused(self) -> None:
        # U+105D2 U+0307 composes in Unicode 16.0 and not in 15.0: Pythons would hash it apart.
        for field in ("name", "description"):
            problems = draft_problems(draft_json(**{field: "\U000105d2\u0307"}))
            assert any("newer than Unicode 15.0" in p and "leave it out" in p for p in problems)

    def test_the_same_in_a_package_is_refused_when_it_is_made(self) -> None:
        module: Json = {"name": "csv_view", "source": "x = '\U000105d2\u0307'\n"}
        # A draft is refused as written; a package built by hand (a model is not the only way in)
        # is refused when it is normalised.
        with pytest.raises(ToolPackageError, match=r"newer than Unicode 15\.0"):
            parse_draft(text_of(draft_json(modules=[module])))
        assert draft_problems(draft_json(tags=["a"], description="\U000105d2"))

    def test_the_size_limit_of_a_draft_and_its_boundary(self) -> None:
        good = text_of(draft_json())
        padded = good + b" " * (MAX_PACKAGE_BYTES - len(good))

        assert parse_draft(padded).name == "CSV viewer"
        with pytest.raises(ToolTooLargeError):
            parse_draft(padded + b" ")
        with pytest.raises(ToolPackageError, match="expected bytes"):
            parse_draft("{}")  # type: ignore[arg-type]  # the point


class TestTaint:
    def test_the_package_records_the_taint_it_was_saved_with(self) -> None:
        assert package(taint=()).package.taint == ()
        assert package(taint=("fs",)).package.taint == ("fs",)
        assert package(taint=("net",)).package.taint == ("net",)
        assert b'"taint":["fs"]' in package(taint=("fs",)).data

    def test_a_package_is_never_both_fs_and_net_tainted(self) -> None:
        with pytest.raises(ToolPackageError, match="at most 1"):
            package(taint=("fs", "net"))
        data = tree()
        data["taint"] = ["fs", "net"]
        assert problems_of_parse(text_of(data))

    def test_taint_can_be_nothing_else(self) -> None:
        data = tree()
        data["taint"] = ["disk"]

        assert problems_of_parse(text_of(data))


class TestOneFormForSets:
    def test_a_package_with_unsorted_or_repeated_tags_or_capabilities_is_refused(self) -> None:
        cases: list[tuple[str, JsonValue]] = [
            ("tags", ["b", "a"]),
            ("tags", ["a", "a"]),
            ("capabilities", ["net.full", "fs.read"]),
        ]
        for field, value in cases:
            data = tree()
            at(data, ("manifest",))[field] = value

            assert any("sorted and distinct" in p for p in problems_of_parse(text_of(data)))


class TestPorts:
    def ports(self, inputs: list[Json], outputs: list[Json]) -> Json:
        return draft_json(inputs=[*inputs], outputs=[*outputs])

    def test_a_port_is_a_state_entry_with_the_matching_value_type(self) -> None:
        made = parse_draft(text_of(draft_json()))

        assert made.inputs == (Port(name="csv_path", type="text"),)  # type: ignore[arg-type]  # str

    def test_an_unknown_state_entry_is_refused(self) -> None:
        problems = draft_problems(self.ports([{"name": "nope", "type": "text"}], []))

        assert any("input port nope: not a state entry" in problem for problem in problems)

    def test_a_port_of_the_wrong_type_is_refused(self) -> None:
        problems = draft_problems(self.ports([], [{"name": "data", "type": "json"}]))

        assert any("output port data: the entry is of kind table" in p for p in problems)

    def test_an_entry_with_no_port_type_cannot_be_a_port(self) -> None:
        problems = draft_problems(self.ports([{"name": "has_header", "type": "text"}], []))

        assert any("has_header: the entry is of kind bool" in problem for problem in problems)

    def test_a_port_type_is_one_of_the_five_value_types(self) -> None:
        assert draft_problems(self.ports([{"name": "csv_path", "type": "number"}], []))

    def test_the_same_port_twice_is_refused(self) -> None:
        both: list[Json] = [{"name": "csv_path", "type": "text"}] * 2

        assert any("input ports must not repeat" in p for p in draft_problems(self.ports(both, [])))

    def test_a_computed_entry_cannot_be_an_input(self) -> None:
        state = [
            {"kind": "string", "key": "a", "default": ""},
            {
                "kind": "string",
                "key": "b",
                "default": "x",
                "computed": {"kind": "string", "value": "x"},
            },
        ]
        ui = json.loads(accepted(build(state=state)).model_dump_json())  # type: ignore[arg-type]
        base: Json = {"name": "t", "ui": ui}

        as_output = base | {"outputs": [{"name": "b", "type": "text"}]}
        as_input = base | {"inputs": [{"name": "b", "type": "text"}]}

        assert parse_draft(text_of(as_output)).outputs
        assert any("computed, so it is read-only" in p for p in draft_problems(as_input))


class TestModules:
    def test_every_sandbox_handler_needs_its_module(self) -> None:
        problems = draft_problems(draft_json(modules=[]))

        assert any("csv_view is not one of the package's code modules" in p for p in problems)

    def test_module_names_are_not_paths_or_upper_case(self) -> None:
        for bad in ("../x", "a/b", "A", "a.b", "", "_x", "x" * 65, "a b", NFC):
            entry: Json = {"name": bad, "source": ""}
            assert draft_problems(draft_json(modules=[entry])), bad

    def test_a_module_name_is_given_once(self) -> None:
        entry: Json = {"name": "csv_view", "source": SOURCE}

        problems = draft_problems(draft_json(modules=[entry, entry]))

        assert any("module names must not repeat" in problem for problem in problems)

    def test_the_source_is_text_without_nul_or_surrogates(self) -> None:
        for bad in ("a\0b", "\ud800"):
            entry: Json = {"name": "csv_view", "source": bad}
            data = draft_json(modules=[entry])
            with pytest.raises(ToolPackageError):
                parse_draft(json.dumps(data).encode("ascii"))  # `ensure_ascii`: \u0000, \ud800

    def test_the_names_of_the_standard_library_are_taken_as_they_are(self) -> None:
        # #56: a module called `os` is a valid name here. The sandbox loader (S20) imports tool
        # modules under a private package, so it cannot resolve to the standard library.
        entry: Json = {"name": "os", "source": SOURCE}
        data = draft_json(modules=[entry])
        handlers = cast("list[Json]", cast("Json", data["ui"])["handlers"])
        handlers[0]["module"] = "os"

        assert parse_draft(text_of(data)).modules[0].name == "os"


class TestOnlyWhatIsDeclaredCanBeCarried:
    """I8: a package carries no grants and no state; every model is closed."""

    @pytest.mark.parametrize(
        "where",
        [(), ("manifest",), ("ui",), ("modules", 0), ("manifest", "inputs", 0)],
        ids=str,
    )
    @pytest.mark.parametrize("field", ["grants", "state_values", "values", "instances", "task"])
    def test_an_extra_member_anywhere_is_refused(
        self, where: tuple[str | int, ...], field: str
    ) -> None:
        data = tree()
        at(data, where)[field] = {"anything": 1}

        problems = problems_of_parse(text_of(data))

        assert any("extra" in problem.lower() or "not permitted" in problem for problem in problems)

    def test_state_in_a_spec_is_defaults_not_values(self) -> None:
        data = tree()
        ui = cast("dict[str, JsonValue]", data["ui"])
        state = cast("list[dict[str, JsonValue]]", ui["state"])
        state[0]["value"] = "the user's current text"

        assert problems_of_parse(text_of(data))

    def test_a_grant_is_not_a_capability_declaration(self) -> None:
        data = tree()
        manifest = cast("dict[str, JsonValue]", data["manifest"])
        manifest["capabilities"] = [{"kind": "fs.read", "path": "/home/user"}]

        assert problems_of_parse(text_of(data))


class TestParsingHostileBytes:
    def test_not_bytes_is_refused(self) -> None:
        with pytest.raises(ToolPackageError, match="expected bytes"):
            parse_package("{}")  # type: ignore[arg-type]  # the point

    def test_over_the_size_limit_is_refused_before_it_is_parsed(self) -> None:
        with pytest.raises(ToolTooLargeError):
            parse_package(b"[" * (MAX_PACKAGE_BYTES + 1))

    @pytest.mark.parametrize(
        "data",
        [
            b"",
            b"\xff\xfe",
            b"\xef\xbb\xbf{}",
            b"{",
            b"[]",
            b"null",
            b'{"a":1,"a":2}',
            b"NaN",
            b'{"format":NaN}',
            b"PK\x03\x04" + b"\0" * 100,
            b"\x1f\x8b\x08" + b"\0" * 100,
        ],
        ids=repr,
    )
    def test_what_is_not_a_package_is_refused_with_a_package_error(self, data: bytes) -> None:
        assert problems_of_parse(data)

    def test_nesting_cannot_exhaust_the_stack(self) -> None:
        assert problems_of_parse(b"[" * 300_000)
        assert problems_of_parse(b'{"a":' * 60_000)

    def test_an_object_wider_than_any_model_allows_is_refused_while_parsing(self) -> None:
        wide = b"{" + b",".join(b'"k%d":0' % number for number in range(6000)) + b"}"

        assert any("members" in problem for problem in problems_of_parse(wide))

    def test_a_repeated_key_is_refused_not_resolved(self) -> None:
        data = text_of(tree())
        data = data.replace(b'"taint":[]', b'"taint":["fs"],"taint":[]')

        assert any("repeats the key" in problem for problem in problems_of_parse(data))

    def test_the_errors_are_bounded_and_do_not_repeat_the_input(self) -> None:
        marker = "SECRET-MARKER-" + "x" * 500
        data = tree()
        manifest = cast("dict[str, JsonValue]", data["manifest"])
        manifest["name"] = marker
        manifest["id"] = marker
        manifest["tags"] = [marker] * 20
        manifest["version"] = marker
        data["format"] = marker
        data["taint"] = [marker]

        problems = problems_of_parse(text_of(data))

        assert len(problems) <= 11
        assert all(len(problem) <= 300 for problem in problems)
        assert "SECRET-MARKER" not in " ".join(problems)

    def test_no_error_other_than_a_package_error_escapes(self) -> None:
        for data in (
            b"\x00" * 10,
            b'{"format":"tumbleweed.tool/1"}',
            text_of({"a": [1] * 100}),
            b'{"format":1e999}',
        ):
            with pytest.raises(ToolPackageError):
                parse_package(data)


class TestSize:
    def test_the_whole_package_is_at_most_the_limit(self) -> None:
        stored = package()
        padded = stored.data + b" " * (MAX_PACKAGE_BYTES - len(stored.data))

        assert parse_package(padded) == stored.package
        with pytest.raises(ToolTooLargeError):
            parse_package(padded + b" ")

    def test_the_spec_limit_is_exact(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from tumbleweed.core.jcs import canonicalize

        size = len(canonicalize(draft().ui.model_dump(mode="json")).encode("utf-8"))

        monkeypatch.setattr("tumbleweed.tools.package.MAX_SPEC_BYTES", size)
        package()  # exactly the limit is fine
        monkeypatch.setattr("tumbleweed.tools.package.MAX_SPEC_BYTES", size - 1)
        with pytest.raises(ToolTooLargeError, match="the spec is over"):
            package()

    def test_the_spec_limit_is_on_the_canonical_spec_not_on_how_it_was_written(self) -> None:
        # `1e20` is five bytes as written and 21 as canonical JSON writes it, so a spec that is
        # under the limit as the model wrote it can be over it as it is stored.
        from tumbleweed.ui.spec.limits import MAX_SPEC_BYTES

        def numbers(key: str, count: int) -> Json:
            return {"kind": "json", "key": key, "default": [1e20] * count}

        ui = accepted(build(state=[numbers("a", 9_990), numbers("b", 3_000)]))
        written = len(json.dumps(ui.model_dump(mode="json")))
        assert written < MAX_SPEC_BYTES
        made = ToolDraft(name="big", ui=ui)

        with pytest.raises(ToolTooLargeError, match="the spec is over"):
            package(source=made)


def test_a_package_is_built_for_a_version_of_a_tool() -> None:
    tool = ToolId.new()
    taint: tuple[Taint, ...] = ("net",)

    built = build_package(draft(), tool=tool, version=ToolVersion(3), taint=taint)

    assert isinstance(built, ToolPackage)
    assert built.manifest.id == tool
    assert built.manifest.version == 3
    assert built.manifest.spec_version == built.ui.version == "tumbleweed.ui/1"
    assert built.taint == ("net",)


GOLDEN_HASH = ContentHash("8ef66c331c745a35cf25bebd9e5a06435722cc1b932ba7f3667e98531f58d2f3")
