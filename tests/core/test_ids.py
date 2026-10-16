"""Exhaustive tests for the typed ID system, run against every ID in the v1 registry."""

import copy
import itertools
import operator
import pickle
import re
import threading
import time

import pytest
from pydantic import BaseModel, ConfigDict, TypeAdapter, ValidationError

from tumbleweed.core import ids
from tumbleweed.core.errors import InvalidValueError
from tumbleweed.core.ids import (
    ConnectionId,
    EntityId,
    GrantId,
    InvocationId,
    IpcRequestId,
    MessageId,
    PromptId,
    RunId,
    SessionId,
    SnapshotId,
    TaskId,
    ToolId,
    ToolInstanceId,
    _encode,
    _MonotonicUlid,
)

# The complete v1 registry (PLAN S01). Changing it is a deliberate act: this list is the check.
REGISTRY: dict[type[EntityId], str] = {
    SessionId: "ses",
    MessageId: "msg",
    TaskId: "tsk",
    ToolId: "tool",
    ToolInstanceId: "inst",
    GrantId: "grt",
    PromptId: "prm",
    RunId: "run",
    SnapshotId: "snap",
    ConnectionId: "conn",
    IpcRequestId: "req",
    InvocationId: "inv",
}
ID_TYPES = list(REGISTRY)
ID_PARAMS = [pytest.param(id_type, id=id_type.__name__) for id_type in ID_TYPES]
CROSS_TYPE_PAIRS = list(itertools.permutations(ID_TYPES, 2))
CROSS_TYPE_PARAMS = [
    pytest.param(a, b, id=f"{a.__name__}-{b.__name__}") for a, b in CROSS_TYPE_PAIRS
]

VALUE = "01j9z3c7q4v8s2k5m6n7p8r9t0"  # a well-formed value, valid under any prefix


def text_of(id_type: type[EntityId], value: str = VALUE) -> str:
    return f"{id_type.prefix}_{value}"


def value_of(entity_id: EntityId) -> str:
    return str(entity_id).partition("_")[2]


def test_the_registry_is_exactly_the_v1_set() -> None:
    assert set(EntityId.__subclasses__()) == set(REGISTRY)
    assert {t: t.prefix for t in EntityId.__subclasses__()} == REGISTRY
    assert len(set(REGISTRY.values())) == len(REGISTRY)


def test_registry_classes_are_final() -> None:
    assert all(getattr(id_type, "__final__", False) for id_type in ID_TYPES)


class TestCreation:
    @pytest.mark.parametrize("id_type", ID_PARAMS)
    def test_new_has_the_serialized_form_prefix_underscore_ulid(
        self, id_type: type[EntityId]
    ) -> None:
        text = str(id_type.new())

        assert re.fullmatch(rf"{REGISTRY[id_type]}_[0-7][0-9a-hjkmnp-tv-z]{{25}}", text)

    @pytest.mark.parametrize("id_type", ID_PARAMS)
    def test_new_ids_are_unique_and_sort_in_creation_order(self, id_type: type[EntityId]) -> None:
        created = [id_type.new() for _ in range(5000)]  # far more than fit in one millisecond

        assert len(set(created)) == len(created)
        assert created == sorted(created)
        assert [str(i) for i in created] == sorted(str(i) for i in created)

    def test_ids_of_different_types_created_together_still_sort_by_time(self) -> None:
        tasks = [TaskId.new() for _ in range(100)]
        later = ToolId.new()

        assert value_of(later) > max(value_of(t) for t in tasks)

    def test_ids_created_from_many_threads_are_all_distinct(self) -> None:
        per_thread = 2000
        results: list[list[TaskId]] = [[] for _ in range(8)]

        def work(bucket: list[TaskId]) -> None:
            bucket.extend(TaskId.new() for _ in range(per_thread))

        threads = [threading.Thread(target=work, args=(bucket,)) for bucket in results]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()

        everything = [i for bucket in results for i in bucket]
        assert len(set(everything)) == 8 * per_thread
        for bucket in results:  # and each thread saw its own IDs in increasing order
            assert bucket == sorted(bucket)

    def test_the_timestamp_is_the_current_time_in_milliseconds(self) -> None:
        # Decode the first 10 characters (48 bits) of the value back into a millisecond count.
        alphabet = "0123456789abcdefghjkmnpqrstvwxyz"
        before_ms = time.time_ns() // 1_000_000
        value = value_of(TaskId.new())
        after_ms = time.time_ns() // 1_000_000

        timestamp_ms = 0
        for char in value[:10]:
            timestamp_ms = timestamp_ms << 5 | alphabet.index(char)

        assert before_ms <= timestamp_ms <= after_ms + 1  # +1: a same-millisecond carry

    def test_random_bits_differ_between_ids_created_in_different_milliseconds(self) -> None:
        first = value_of(TaskId.new())
        while True:  # wait for the clock to tick; at most a millisecond
            second = value_of(TaskId.new())
            if second[:10] != first[:10]:
                break

        assert second[10:] != first[10:]  # fresh entropy, not a counter (collision: 2**-80)

    @pytest.mark.parametrize("id_type", ID_PARAMS)
    def test_there_is_no_way_to_create_an_id_without_validation(
        self, id_type: type[EntityId]
    ) -> None:
        with pytest.raises(TypeError):
            id_type()  # type: ignore[call-arg]  # the point of the test: no-argument construction


class TestMonotonicGenerator:
    """The generator behind `new()`, driven with a fake clock and fake entropy."""

    @staticmethod
    def make(times: list[int], entropy: list[int]) -> _MonotonicUlid:
        clock = iter(times)
        randomness = iter(entropy)
        return _MonotonicUlid(lambda: next(clock), lambda _bits: next(randomness))

    def test_a_new_millisecond_takes_the_timestamp_and_fresh_entropy(self) -> None:
        generator = self.make([1000, 1001], [0xAAAA, 0xBBBB])

        assert generator.next() == (1000 << 80) | 0xAAAA
        assert generator.next() == (1001 << 80) | 0xBBBB

    def test_within_one_millisecond_values_count_up(self) -> None:
        generator = self.make([1000, 1000, 1000], [0xAAAA])

        assert [generator.next() for _ in range(3)] == [
            (1000 << 80) | (0xAAAA + n) for n in range(3)
        ]

    def test_a_clock_that_steps_back_never_makes_values_go_back(self) -> None:
        generator = self.make([1000, 999, 500], [0xAAAA])

        values = [generator.next() for _ in range(3)]

        assert values == sorted(set(values))

    def test_exhausting_the_random_part_carries_into_the_timestamp(self) -> None:
        all_ones = (1 << 80) - 1
        generator = self.make([1000, 1000], [all_ones])

        first, second = generator.next(), generator.next()

        assert first == (1000 << 80) | all_ones
        assert second == 1001 << 80

    def test_encoding_is_26_big_endian_base32_characters(self) -> None:
        assert _encode(0) == "0" * 26
        assert _encode(1) == "0" * 25 + "1"
        assert _encode(31) == "0" * 25 + "z"
        assert _encode(32) == "0" * 24 + "10"
        assert _encode((1 << 128) - 1) == "7" + "z" * 25


class TestParsing:
    @pytest.mark.parametrize("id_type", ID_PARAMS)
    def test_a_valid_text_round_trips_to_the_same_text(self, id_type: type[EntityId]) -> None:
        text = text_of(id_type)

        parsed = id_type(text)

        assert str(parsed) == text
        assert f"{parsed}" == text
        fresh = id_type.new()
        assert id_type(str(fresh)) == fresh

    @pytest.mark.parametrize("id_type", ID_PARAMS)
    def test_the_extreme_values_of_the_range_are_valid(self, id_type: type[EntityId]) -> None:
        assert str(id_type(text_of(id_type, "0" * 26))) == text_of(id_type, "0" * 26)
        assert str(id_type(text_of(id_type, "7" + "z" * 25))) == text_of(id_type, "7" + "z" * 25)

    @pytest.mark.parametrize(("id_type", "other"), CROSS_TYPE_PARAMS)
    def test_a_wrong_prefix_is_rejected(
        self, id_type: type[EntityId], other: type[EntityId]
    ) -> None:
        with pytest.raises(InvalidValueError, match=f"must start with '{id_type.prefix}_'"):
            id_type(text_of(other))

    @pytest.mark.parametrize("id_type", ID_PARAMS)
    @pytest.mark.parametrize(
        "malformed",
        [
            pytest.param("", id="empty"),
            pytest.param("_", id="separator-only"),
            pytest.param("{prefix}", id="prefix-without-separator"),
            pytest.param("{prefix}_", id="prefix-with-empty-value"),
            pytest.param("_" + VALUE, id="no-prefix"),
            pytest.param("{prefix}" + VALUE, id="no-separator"),
            pytest.param("{prefix}-" + VALUE, id="wrong-separator"),
            pytest.param("{prefix}__" + VALUE, id="double-separator"),
            pytest.param("{prefix}_" + VALUE + "_" + VALUE, id="two-values"),
            pytest.param("{PREFIX}_" + VALUE, id="uppercase-prefix"),
            pytest.param("{prefix}_" + VALUE.upper(), id="uppercase-value"),
            pytest.param("{prefix}_" + VALUE[:-1], id="value-too-short"),
            pytest.param("{prefix}_" + VALUE + "0", id="value-too-long"),
            pytest.param("{prefix}_" + VALUE[:-1] + "i", id="excluded-letter-i"),
            pytest.param("{prefix}_" + VALUE[:-1] + "l", id="excluded-letter-l"),
            pytest.param("{prefix}_" + VALUE[:-1] + "o", id="excluded-letter-o"),
            pytest.param("{prefix}_" + VALUE[:-1] + "u", id="excluded-letter-u"),
            pytest.param("{prefix}_8" + VALUE[1:], id="first-char-overflows-128-bits"),
            pytest.param("{prefix}_z" + VALUE[1:], id="first-char-way-over"),
            pytest.param("{prefix}_" + VALUE[:-1] + "-", id="punctuation"),
            pytest.param("{prefix}_" + VALUE[:-1] + "\u0660", id="arabic-indic-digit"),
            pytest.param("{prefix}_" + VALUE[:-1] + "\uff11", id="fullwidth-digit"),
            pytest.param("{prefix}_" + VALUE[:-1] + "\u0430", id="cyrillic-lookalike-a"),
            pytest.param(" {prefix}_" + VALUE, id="leading-space"),
            pytest.param("{prefix}_" + VALUE + " ", id="trailing-space"),
            pytest.param("{prefix}_" + VALUE + "\n", id="trailing-newline"),
            pytest.param("{prefix}_" + VALUE + "\x00", id="trailing-nul"),
            pytest.param("{prefix}_\n" + VALUE, id="leading-newline-in-value"),
        ],
    )
    def test_malformed_text_is_rejected(self, id_type: type[EntityId], malformed: str) -> None:
        text = malformed.format(prefix=id_type.prefix, PREFIX=id_type.prefix.upper())

        with pytest.raises(InvalidValueError):
            id_type(text)

    @pytest.mark.parametrize("id_type", ID_PARAMS)
    def test_a_huge_input_is_rejected_and_not_echoed_whole_in_the_error(
        self, id_type: type[EntityId]
    ) -> None:
        huge = f"{id_type.prefix}_" + "z" * 1_000_000

        with pytest.raises(InvalidValueError) as raised:
            id_type(huge)

        assert len(str(raised.value)) < 400

    def test_the_error_is_a_value_error_and_a_domain_error(self) -> None:
        with pytest.raises(ValueError, match="TaskId"):
            TaskId("nope")
        assert issubclass(InvalidValueError, ValueError)

    @pytest.mark.parametrize("id_type", ID_PARAMS)
    def test_every_character_of_the_alphabet_is_accepted_in_every_position(
        self, id_type: type[EntityId]
    ) -> None:
        for char in "0123456789abcdefghjkmnpqrstvwxyz":
            text = text_of(id_type, "0" + char * 25)
            assert str(id_type(text)) == text
        for char in "01234567":
            text = text_of(id_type, char + "0" * 25)
            assert str(id_type(text)) == text


class TestEquality:
    @pytest.mark.parametrize("id_type", ID_PARAMS)
    def test_equal_text_is_equal_and_hashes_the_same(self, id_type: type[EntityId]) -> None:
        a, b = id_type(text_of(id_type)), id_type(text_of(id_type))

        assert a == b
        assert a is not b
        assert hash(a) == hash(b)
        assert len({a, b}) == 1

    @pytest.mark.parametrize("id_type", ID_PARAMS)
    def test_different_values_are_not_equal(self, id_type: type[EntityId]) -> None:
        assert id_type.new() != id_type.new()

    @pytest.mark.parametrize(("a_type", "b_type"), CROSS_TYPE_PARAMS)
    def test_different_types_are_never_equal_even_with_the_same_value(
        self, a_type: type[EntityId], b_type: type[EntityId]
    ) -> None:
        a, b = a_type(text_of(a_type)), b_type(text_of(b_type))

        assert value_of(a) == value_of(b)
        assert a != b
        assert not a == b  # noqa: SIM201  (exercise __eq__ itself, not just __ne__)
        assert len({a, b}) == 2
        assert {a: 1}.get(b) is None

    @pytest.mark.parametrize("id_type", ID_PARAMS)
    def test_an_id_never_equals_a_plain_string_or_anything_else(
        self, id_type: type[EntityId]
    ) -> None:
        text = text_of(id_type)
        entity_id = id_type(text)

        assert entity_id != text
        assert text != entity_id
        assert entity_id != None  # noqa: E711  (exercise __eq__ with None)
        assert entity_id != 5
        assert entity_id != object()
        assert entity_id != text.encode()

    @pytest.mark.parametrize("id_type", ID_PARAMS)
    def test_an_id_is_not_a_string(self, id_type: type[EntityId]) -> None:
        entity_id: object = id_type.new()

        assert not isinstance(entity_id, str)
        with pytest.raises(TypeError):
            len(entity_id)  # type: ignore[arg-type]  # the point of the test

    @pytest.mark.parametrize("id_type", ID_PARAMS)
    def test_ids_of_one_type_order_by_their_text(self, id_type: type[EntityId]) -> None:
        low, high = id_type(text_of(id_type, "0" * 26)), id_type(text_of(id_type, "1" + "0" * 25))

        assert low < high
        assert low <= high
        assert high > low
        assert high >= low
        assert low <= low
        assert not high < low
        assert sorted([high, low]) == [low, high]
        assert max(high, low) is high

    @pytest.mark.parametrize(("a_type", "b_type"), CROSS_TYPE_PARAMS)
    def test_ordering_across_types_is_an_error(
        self, a_type: type[EntityId], b_type: type[EntityId]
    ) -> None:
        a, b = a_type.new(), b_type.new()

        for compare in (operator.lt, operator.le, operator.gt, operator.ge):
            with pytest.raises(TypeError):
                compare(a, b)


class TestImmutability:
    @pytest.mark.parametrize("id_type", ID_PARAMS)
    def test_attributes_cannot_be_set_changed_or_deleted(self, id_type: type[EntityId]) -> None:
        entity_id = id_type.new()
        text = str(entity_id)

        with pytest.raises(AttributeError, match="immutable"):
            entity_id._text = "other"
        with pytest.raises(AttributeError, match="immutable"):
            entity_id.extra = 1
        with pytest.raises(AttributeError, match="immutable"):
            del entity_id._text
        assert str(entity_id) == text

    @pytest.mark.parametrize("id_type", ID_PARAMS)
    def test_copy_and_deepcopy_give_the_same_equal_object(self, id_type: type[EntityId]) -> None:
        entity_id = id_type.new()

        assert copy.copy(entity_id) is entity_id
        assert copy.deepcopy(entity_id) is entity_id
        assert copy.deepcopy([entity_id])[0] == entity_id

    @pytest.mark.parametrize("id_type", ID_PARAMS)
    def test_pickling_is_refused_because_unpickling_would_skip_validation(
        self, id_type: type[EntityId]
    ) -> None:
        with pytest.raises(TypeError, match="cannot be pickled"):
            pickle.dumps(id_type.new())

    @pytest.mark.parametrize("id_type", ID_PARAMS)
    def test_repr_shows_type_and_text(self, id_type: type[EntityId]) -> None:
        text = text_of(id_type)

        assert repr(id_type(text)) == f"{id_type.__name__}({text!r})"


class TestSubclassing:
    def test_a_prefix_cannot_be_reused(self) -> None:
        with pytest.raises(TypeError, match="already TaskId's"):

            class Duplicate(EntityId, prefix="tsk"):
                pass

    @pytest.mark.parametrize("prefix", ["", "ab", "abcde", "TSK", "ts1", "ts_", "tsk "])
    def test_a_prefix_must_be_three_or_four_lowercase_letters(self, prefix: str) -> None:
        with pytest.raises(TypeError, match="3-4 lowercase letters"):

            class Odd(EntityId, prefix=prefix):
                pass

    def test_a_failed_definition_leaves_the_registry_alone(self) -> None:
        before = dict(ids._PREFIXES)

        with pytest.raises(TypeError):

            class Odd(EntityId, prefix="X"):
                pass

        assert before == ids._PREFIXES

    def test_a_subclass_needs_a_prefix(self) -> None:
        with pytest.raises(TypeError, match="prefix"):

            class Nameless(EntityId):  # type: ignore[call-arg]  # the point of the test
                pass


class Record(BaseModel):
    """A boundary model in the house style, holding one of every ID."""

    model_config = ConfigDict(strict=True, extra="forbid", frozen=True)

    session: SessionId
    message: MessageId
    task: TaskId
    tool: ToolId
    instance: ToolInstanceId
    grant: GrantId
    prompt: PromptId
    run: RunId
    snapshot: SnapshotId
    connection: ConnectionId
    request: IpcRequestId
    invocation: InvocationId
    maybe_task: TaskId | None = None


def fresh_record() -> Record:
    return Record(
        session=SessionId.new(),
        message=MessageId.new(),
        task=TaskId.new(),
        tool=ToolId.new(),
        instance=ToolInstanceId.new(),
        grant=GrantId.new(),
        prompt=PromptId.new(),
        run=RunId.new(),
        snapshot=SnapshotId.new(),
        connection=ConnectionId.new(),
        request=IpcRequestId.new(),
        invocation=InvocationId.new(),
    )


class TestPydantic:
    def test_a_model_of_every_id_round_trips_through_json_as_strings(self) -> None:
        record = fresh_record()

        dumped = record.model_dump_json()
        restored = Record.model_validate_json(dumped)

        assert restored == record
        assert type(restored.task) is TaskId
        assert re.search(rf'"task":"{re.escape(str(record.task))}"', dumped)
        assert '"maybe_task":null' in dumped

    def test_json_mode_dumps_strings_python_mode_keeps_the_typed_objects(self) -> None:
        record = fresh_record()

        assert record.model_dump(mode="json")["task"] == str(record.task)
        assert record.model_dump()["task"] is record.task

    def test_the_python_dump_validates_back(self) -> None:
        record = fresh_record()

        assert Record.model_validate(record.model_dump()) == record

    def test_text_validates_in_python_mode_too_which_is_how_dicts_from_toml_or_sql_load(
        self,
    ) -> None:
        record = fresh_record()

        assert Record.model_validate(record.model_dump(mode="json")) == record

    @pytest.mark.parametrize(("a_type", "b_type"), CROSS_TYPE_PARAMS)
    def test_a_field_rejects_an_id_of_another_type_object_or_text(
        self, a_type: type[EntityId], b_type: type[EntityId]
    ) -> None:
        adapter = TypeAdapter(a_type)
        wrong = b_type.new()

        with pytest.raises(ValidationError):
            adapter.validate_python(wrong)
        with pytest.raises(ValidationError):
            adapter.validate_python(str(wrong))
        with pytest.raises(ValidationError):
            adapter.validate_json(f'"{wrong}"')

    @pytest.mark.parametrize("id_type", ID_PARAMS)
    @pytest.mark.parametrize(
        "bad_json",
        [
            "5",
            "true",
            "null",
            "[]",
            "{}",
            '""',
            '"tsk"',
            '"tsk_"',
            '"not an id"',
            '"' + "z" * 5000 + '"',
        ],
    )
    def test_malformed_json_is_rejected(self, id_type: type[EntityId], bad_json: str) -> None:
        with pytest.raises(ValidationError):
            TypeAdapter(id_type).validate_json(bad_json)

    @pytest.mark.parametrize("id_type", ID_PARAMS)
    @pytest.mark.parametrize("bad", [5, True, None, b"tsk_x", 1.5, ["a"], object()])
    def test_non_text_python_values_are_rejected(
        self, id_type: type[EntityId], bad: object
    ) -> None:
        with pytest.raises(ValidationError):
            TypeAdapter(id_type).validate_python(bad)

    def test_a_malformed_id_in_json_names_the_field(self) -> None:
        payload = fresh_record().model_dump_json().replace('"task":"tsk_', '"task":"tol_')

        with pytest.raises(ValidationError) as raised:
            Record.model_validate_json(payload)

        assert [error["loc"] for error in raised.value.errors()] == [("task",)]

    def test_optional_ids_accept_null(self) -> None:
        record = fresh_record().model_copy(update={"maybe_task": TaskId.new()})

        assert Record.model_validate_json(record.model_dump_json()).maybe_task == record.maybe_task

    def test_a_frozen_model_holding_ids_is_hashable_and_deep_copies(self) -> None:
        record = fresh_record()

        assert hash(record) == hash(record.model_copy(deep=True))
        assert record.model_copy(deep=True) == record

    @pytest.mark.parametrize("id_type", ID_PARAMS)
    def test_ids_work_as_json_object_keys(self, id_type: type[EntityId]) -> None:
        adapter = TypeAdapter(dict[id_type, int])  # type: ignore[valid-type]  # id_type is a runtime variable
        mapping = {id_type.new(): 1, id_type.new(): 2}

        restored = adapter.validate_json(adapter.dump_json(mapping))

        assert restored == mapping

    @pytest.mark.parametrize("id_type", ID_PARAMS)
    def test_the_json_schema_is_a_string_with_the_exact_pattern(
        self, id_type: type[EntityId]
    ) -> None:
        schema = TypeAdapter(id_type).json_schema()

        assert schema == {
            "type": "string",
            "pattern": f"^{id_type.prefix}_[0-7][0-9a-hjkmnp-tv-z]{{25}}$",
        }
        assert re.fullmatch(schema["pattern"], str(id_type.new()))

    def test_a_model_field_cannot_be_the_abstract_base(self) -> None:
        with pytest.raises(TypeError, match="EntityId is abstract"):

            class Bad(BaseModel):
                id: EntityId
