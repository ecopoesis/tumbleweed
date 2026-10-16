"""Exhaustive tests for the typed ID system, run against every ID in the v1 registry."""

import copy
import itertools
import operator
import os
import pickle
import re
import secrets
import signal
import threading
import time
import warnings
from pathlib import Path
from typing import ClassVar

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
    id_types,
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
    assert id_types() == tuple(REGISTRY)  # the same twelve, in definition order
    assert {t: t.prefix for t in id_types()} == REGISTRY
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


def decode(text: str) -> int:
    """The 128-bit number behind an ID's text, for tests that look at the arithmetic."""
    alphabet = "0123456789abcdefghjkmnpqrstvwxyz"
    number = 0
    for char in text.partition("_")[2]:
        number = number << 5 | alphabet.index(char)
    return number


class TestMonotonicGenerator:
    """The generator behind `new()`, driven with a fake clock and fake entropy."""

    @staticmethod
    def make(
        times: list[int], entropy: list[int], asked: list[int] | None = None
    ) -> _MonotonicUlid:
        clock = iter(times)
        randomness = iter(entropy)

        def draw(bits: int) -> int:
            if asked is not None:
                asked.append(bits)
            return next(randomness)

        return _MonotonicUlid(lambda: next(clock), draw)

    def test_a_new_millisecond_takes_the_timestamp_and_fresh_entropy(self) -> None:
        generator = self.make([1000, 1001], [0xAAAA, 0xBBBB])

        assert generator.next() == (1000 << 80) | 0xAAAA
        assert generator.next() == (1001 << 80) | 0xBBBB

    def test_within_one_millisecond_each_step_is_one_plus_a_random_amount(self) -> None:
        asked: list[int] = []
        generator = self.make([1000, 1000, 1000], [0xAAAA, 5, 7], asked)

        values = [generator.next() for _ in range(3)]

        base = (1000 << 80) | 0xAAAA
        assert values == [base, base + 1 + 5, base + 1 + 5 + 1 + 7]
        assert asked == [80, 64, 64]  # the whole field once, then 64 bits for each step

    def test_a_step_of_zero_entropy_still_moves_forward(self) -> None:
        generator = self.make([1000, 1000], [0xAAAA, 0])

        assert generator.next() < generator.next()

    def test_a_clock_that_steps_back_keeps_the_order_with_random_steps(self) -> None:
        asked: list[int] = []
        generator = self.make([1000, 999, 500], [0xAAAA, 11, 13], asked)

        values = [generator.next() for _ in range(3)]

        base = (1000 << 80) | 0xAAAA
        assert values == [base, base + 12, base + 12 + 14]
        assert asked == [80, 64, 64]

    def test_exhausting_the_random_field_carries_into_the_timestamp(self) -> None:
        all_ones = (1 << 80) - 1
        generator = self.make([1000, 1000, 1000], [all_ones, 0, 2**63])

        first, second, third = generator.next(), generator.next(), generator.next()

        assert first == (1000 << 80) | all_ones
        assert second == 1001 << 80  # all ones + 1 + 0: the timestamp moved on by one
        assert third == (1001 << 80) + 1 + 2**63
        assert first < second < third

    def test_reset_forgets_the_last_value_and_draws_fresh_entropy(self) -> None:
        asked: list[int] = []
        generator = self.make([1000, 1000, 1000], [0xAAAA, 5, 0xBBBB], asked)
        generator.next()
        generator.next()

        generator.reset()

        assert generator.next() == (1000 << 80) | 0xBBBB  # not the old sequence continued
        assert asked == [80, 64, 80]

    def test_reset_replaces_a_lock_that_was_held_at_the_moment_of_a_fork(self) -> None:
        generator = self.make([1000, 1001], [1, 2])
        generator._lock.acquire()  # as if another thread were inside next() when we forked

        generator.reset()

        # Asserted before next() is called, so that a regression fails here and cannot hang.
        assert not generator._lock.locked()
        assert generator.next() == (1000 << 80) | 1

    def test_encoding_is_26_big_endian_base32_characters(self) -> None:
        assert _encode(0) == "0" * 26
        assert _encode(1) == "0" * 25 + "1"
        assert _encode(31) == "0" * 25 + "z"
        assert _encode(32) == "0" * 24 + "10"
        assert _encode((1 << 128) - 1) == "7" + "z" * 25

    def test_decode_in_these_tests_inverts_the_encoding(self) -> None:
        for value in (0, 1, 31, 32, 2**80, (1 << 128) - 1):
            assert decode(f"tsk_{_encode(value)}") == value


def wait_for_child(pid: int, *, seconds: float) -> int:
    """The wait status of a child, polled against a deadline so that a stuck child cannot hang."""
    deadline = time.monotonic() + seconds
    while True:
        done, status = os.waitpid(pid, os.WNOHANG)
        if done == pid:
            return status
        if time.monotonic() > deadline:
            os.kill(pid, signal.SIGKILL)
            os.waitpid(pid, 0)
            pytest.fail(f"the forked child did not finish within {seconds} seconds")
        time.sleep(0.01)


class TestUnpredictability:
    """Ordering is kept, but no ID can be derived from its neighbours (I5: PromptId)."""

    NEAR = 2**32  # a step this small, by chance, has a probability of 2**-32 per pair

    def test_consecutive_ids_of_one_type_are_far_apart(self) -> None:
        numbers = [decode(str(TaskId.new())) for _ in range(5000)]  # several per millisecond

        deltas = [b - a for a, b in itertools.pairwise(numbers)]

        assert all(delta > self.NEAR for delta in deltas)

    def test_consecutive_ids_of_different_types_are_far_apart_too(self) -> None:
        types = [PromptId, TaskId, ToolId, MessageId]
        numbers = [decode(str(types[n % len(types)].new())) for n in range(5000)]

        deltas = [b - a for a, b in itertools.pairwise(numbers)]

        assert all(delta > self.NEAR for delta in deltas)

    def test_the_same_millisecond_really_occurs_in_these_runs(self) -> None:
        """Guards the two tests above: they must exercise the within-a-millisecond path."""
        timestamps = [decode(str(TaskId.new())) >> 80 for _ in range(5000)]

        assert len(set(timestamps)) < len(timestamps)

    def test_after_a_clock_step_back_ids_are_ordered_and_not_small_steps(self) -> None:
        clock = iter([1000] * 5 + [900] * 5 + [1000] * 5 + [1001] * 5)
        generator = _MonotonicUlid(lambda: next(clock), secrets.randbits)

        numbers = [generator.next() for _ in range(20)]

        assert numbers == sorted(numbers)
        assert len(set(numbers)) == 20
        # Every step but those into a fresh millisecond (indexes 15) comes from the random step.
        deltas = [b - a for a, b in itertools.pairwise(numbers)]
        assert all(delta > self.NEAR for delta in deltas)

    def test_a_guessed_neighbour_is_not_the_next_id(self) -> None:
        """The attack the fix is for: from one ID, try `previous + 1` and a few neighbours."""
        first = decode(str(PromptId.new()))
        second = decode(str(PromptId.new()))

        assert second - first not in range(1, 1000)

    def test_ordering_still_holds_across_a_long_run_and_types(self) -> None:
        types = id_types()
        created = [types[n % len(types)].new() for n in range(20000)]
        numbers = [decode(str(i)) for i in created]
        texts = [str(i) for i in created]

        assert numbers == sorted(numbers)
        assert texts == sorted(texts, key=lambda t: t.partition("_")[2])
        assert len(set(numbers)) == len(numbers)

    @pytest.mark.skipif(not hasattr(os, "fork"), reason="os.fork is not available on this platform")
    def test_a_forked_child_never_collides_with_its_parent(self, tmp_path: Path) -> None:
        TaskId.new()  # the parent has state to be inherited
        parent_last = decode(str(TaskId.new()))
        report = tmp_path / "child-ids"
        with warnings.catch_warnings():
            # Python 3.12 warns when forking a process that has threads (pytest may have some).
            warnings.simplefilter("ignore", DeprecationWarning)
            pid = os.fork()
        if pid == 0:  # the child: report whether the generator was reset, and its IDs
            status = 1
            try:
                reset = ids._ULIDS._last == 0
                report.write_text("\n".join(str(TaskId.new()) for _ in range(2000)))
                status = 0 if reset else 2
            finally:
                os._exit(status)  # no atexit handlers, no pytest teardown in the child
        parent_ids = [str(TaskId.new()) for _ in range(2000)]
        wait_status = wait_for_child(pid, seconds=30)

        assert os.waitstatus_to_exitcode(wait_status) == 0, "the child did not reset its generator"
        child_ids = report.read_text().splitlines()
        assert len(child_ids) == 2000
        assert len(set(child_ids)) == 2000
        assert not set(child_ids) & set(parent_ids)
        assert not set(child_ids) & {f"tsk_{_encode(parent_last)}"}
        assert child_ids == sorted(child_ids)


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
    def test_there_is_no_instance_dict_to_rewrite_the_value_through(
        self, id_type: type[EntityId]
    ) -> None:
        entity_id = id_type.new()
        text = str(entity_id)

        assert not hasattr(entity_id, "__dict__")
        with pytest.raises(TypeError):
            vars(entity_id)["_text"] = "tsk_forged"
        assert str(entity_id) == text

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
                __slots__ = ()

    @pytest.mark.parametrize("prefix", ["", "ab", "abcde", "TSK", "ts1", "ts_", "tsk "])
    def test_a_prefix_must_be_three_or_four_lowercase_letters(self, prefix: str) -> None:
        with pytest.raises(TypeError, match="3-4 lowercase letters"):

            class Odd(EntityId, prefix=prefix):
                __slots__ = ()

    def test_a_failed_definition_leaves_the_registry_alone(self) -> None:
        before = dict(ids._PREFIXES)

        with pytest.raises(TypeError):

            class Odd(EntityId, prefix="X"):
                __slots__ = ()

        assert before == ids._PREFIXES

    def test_a_subclass_needs_a_prefix(self) -> None:
        with pytest.raises(TypeError, match="prefix"):

            class Nameless(EntityId):  # type: ignore[call-arg]  # the point of the test
                __slots__ = ()

    @pytest.mark.parametrize("id_type", ID_PARAMS)
    def test_a_final_id_type_cannot_be_subclassed_at_runtime_either(
        self, id_type: type[EntityId]
    ) -> None:
        """mypy rejects it too (the misuse fixture); this is for code mypy does not see."""
        before = dict(ids._PREFIXES)

        with pytest.raises(TypeError, match=f"cannot subclass {id_type.__name__}: it is final"):
            type("Forged", (id_type,), {"__slots__": ()}, prefix="forg")

        assert before == ids._PREFIXES  # the failed attempt did not register anything

    def test_a_subclass_must_declare_slots(self) -> None:
        with pytest.raises(TypeError, match="must declare __slots__"):

            class Leaky(EntityId, prefix="lky"):
                pass

        assert "lky" not in ids._PREFIXES


class TestAbstractBase:
    def test_the_base_cannot_be_constructed(self) -> None:
        with pytest.raises(TypeError, match="EntityId is abstract"):
            EntityId("tsk_01j9z3c7q4v8s2k5m6n7p8r9t0")

    @pytest.mark.parametrize("bad", [123, None, b"x", ToolId.new()])
    def test_the_base_says_abstract_whatever_it_is_given(self, bad: object) -> None:
        with pytest.raises(TypeError, match="EntityId is abstract"):
            EntityId(bad)  # type: ignore[arg-type]  # the point of the test

    def test_the_base_cannot_create_a_new_id(self) -> None:
        with pytest.raises(TypeError, match="EntityId is abstract"):
            EntityId.new()

    def test_the_base_cannot_be_a_pydantic_field(self) -> None:
        with pytest.raises(TypeError, match="EntityId is abstract"):

            class Bad(BaseModel):
                id: EntityId


class TestInputTypes:
    """Whatever is passed in, a constructor answers with InvalidValueError and nothing else."""

    BAD_INPUTS: ClassVar[list[object]] = [
        123,
        0,
        1.5,
        True,
        None,
        b"tsk_" + VALUE.encode(),
        bytearray(b"x"),
        ["tsk_x"],
        ("tsk_x",),
        {"a": 1},
        object(),
        10**5000,
    ]

    @pytest.mark.parametrize("id_type", ID_PARAMS)
    def test_a_non_str_is_rejected(self, id_type: type[EntityId]) -> None:
        for bad in [*self.BAD_INPUTS, TaskId.new(), ToolId.new(), PromptId.new()]:
            with pytest.raises(InvalidValueError, match="is built from a str"):
                id_type(bad)  # type: ignore[arg-type]  # the point of the test

    @pytest.mark.parametrize(("a_type", "b_type"), CROSS_TYPE_PARAMS)
    def test_another_id_is_not_text(self, a_type: type[EntityId], b_type: type[EntityId]) -> None:
        with pytest.raises(InvalidValueError, match="is built from a str, got"):
            a_type(b_type.new())  # type: ignore[arg-type]  # the point of the test

    def test_the_message_names_the_type_and_not_the_value(self) -> None:
        with pytest.raises(InvalidValueError) as raised:
            TaskId(10**5000)  # type: ignore[arg-type]  # the point of the test

        assert len(str(raised.value)) < 100


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

    def test_optional_ids_accept_null_and_an_id(self) -> None:
        without = fresh_record()
        with_task = Record.model_validate({**without.model_dump(), "maybe_task": TaskId.new()})

        assert Record.model_validate_json(without.model_dump_json()).maybe_task is None
        assert (
            Record.model_validate_json(with_task.model_dump_json()).maybe_task
            == with_task.maybe_task
        )

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
