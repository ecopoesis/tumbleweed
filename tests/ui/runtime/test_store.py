"""The state store: typed values, computed entries, the budget, and notifications."""

import logging
import threading
from collections.abc import Callable
from unittest import mock

import pytest

from tests.ui.runtime.support import (
    choice,
    flag,
    key,
    number,
    real,
    spec_with,
    text,
)
from tests.ui.spec.support import (
    Json,
    binary,
    computed,
    lit_int,
    lit_str,
    ref,
    variadic,
)
from tumbleweed.ui.runtime import MAX_DELIVERIES, Change, Origin, StateStore, WrongThreadError
from tumbleweed.ui.spec import SpecError
from tumbleweed.ui.spec.limits import MAX_STATE_TEXT_BYTES

USER = Origin.USER


def store_of(*state: Json) -> StateStore:
    return StateStore(spec_with(state))


def doubled(source: str = "n") -> Json:
    return computed("d", "int", binary("mul", ref(source), lit_int(2)), 0)


class TestInitialState:
    def test_values_start_as_the_defaults(self) -> None:
        store = store_of(
            number("n", 3),
            real("f", 1.5),
            text("s", "hi"),
            flag("b", default=True),
            choice("e", ["a", "b"], "b"),
        )

        assert store.value(key("n")) == 3
        assert store.value(key("f")) == 1.5
        assert store.value(key("s")) == "hi"
        assert store.value(key("b")) is True
        assert store.value(key("e")) == "b"

    def test_the_other_kinds_start_empty(self) -> None:
        store = store_of(
            {"kind": "image", "key": "i"},
            {"kind": "file", "key": "fi"},
            {"kind": "table", "key": "t"},
            {"kind": "list", "key": "l", "item_type": "int"},
            {"kind": "json", "key": "j"},
        )

        assert store.value(key("i")) is None
        assert store.value(key("fi")) is None
        assert store.value(key("l")) == ()
        assert store.value(key("j")) is None
        assert store.value(key("t")).rows == ()  # type: ignore[union-attr]  # a Table

    def test_computed_entries_are_evaluated_at_the_start_not_left_at_their_defaults(self) -> None:
        store = store_of(number("n", 21), doubled())

        assert store.value(key("d")) == 42

    def test_a_computed_entry_that_reads_nothing_is_evaluated_too(self) -> None:
        store = store_of(computed("c", "int", lit_int(7), 0))

        assert store.value(key("c")) == 7

    def test_computed_entries_are_evaluated_in_dependency_order(self) -> None:
        # Declared in the order that would be wrong: c reads b, b reads a.
        store = store_of(
            computed("c", "int", binary("add", ref("b"), lit_int(1)), 0),
            computed("b", "int", binary("add", ref("a"), lit_int(1)), 0),
            number("a", 1),
        )

        assert (store.value(key("b")), store.value(key("c"))) == (2, 3)

    def test_the_text_of_the_state_is_counted(self) -> None:
        store = store_of(text("a", "abc"), text("b", "é"), number("n", 1))

        assert store.text_bytes == 3 + 2

    def test_an_undeclared_key_is_a_key_error_on_reading(self) -> None:
        with pytest.raises(KeyError):
            store_of().value(key("nope"))


class TestAssign:
    def test_a_written_value_replaces_the_old_one(self) -> None:
        store = store_of(number("n", 1))

        assert store.write(key("n"), 5, origin=USER) is True
        assert store.value(key("n")) == 5

    def test_the_value_is_coerced_as_the_spec_says(self) -> None:
        store = store_of(
            number("n", 1, minimum=0, maximum=10),
            real("f", 0.0, minimum=0.0, maximum=1.0),
            text("s", max_length=3),
            choice("e", ["a", "b"], "a"),
        )

        store.assign(
            [(key("n"), 99), (key("f"), 7), (key("s"), "abcdef"), (key("e"), "b")], origin=USER
        )

        assert store.value(key("n")) == 10
        assert store.value(key("f")) == 1.0
        assert type(store.value(key("f"))) is float
        assert store.value(key("s")) == "abc"
        assert store.value(key("e")) == "b"

    def test_an_enum_given_a_non_option_keeps_its_old_value(self) -> None:
        store = store_of(choice("e", ["a", "b"], "a"))

        applied = store.assign([(key("e"), "zzz")], origin=USER)

        assert applied == (False,)
        assert store.value(key("e")) == "a"

    def test_it_says_for_each_write_whether_it_was_applied(self) -> None:
        store = store_of(number("n", 1), choice("e", ["a"], "a"))

        applied = store.assign(
            [(key("n"), 2), (key("e"), "q"), (key("n"), 3)], origin=Origin.HANDLER
        )

        assert applied == (True, False, True)
        assert store.value(key("n")) == 3  # later writes to a key win

    def test_a_computed_entry_cannot_be_written(self, caplog: pytest.LogCaptureFixture) -> None:
        store = store_of(number("n", 1), doubled())
        changes = recorded_by(store)

        with caplog.at_level(logging.WARNING):
            applied = store.assign([(key("d"), 99)], origin=USER)

        assert applied == (False,)
        assert store.value(key("d")) == 2
        assert changes == []
        assert "not a writable entry" in caplog.text

    def test_an_undeclared_key_is_refused(self) -> None:
        store = store_of(number("n", 1))

        assert store.assign([(key("nope"), 1)], origin=USER) == (False,)

    @pytest.mark.parametrize(
        "decl",
        [
            {"kind": "image", "key": "x"},
            {"kind": "table", "key": "x"},
            {"kind": "json", "key": "x"},
            {"kind": "list", "key": "x", "item_type": "int"},
        ],
    )
    def test_an_entry_only_a_patch_can_change_is_refused(self, decl: Json) -> None:
        store = store_of(decl)

        assert store.assign([(key("x"), "text")], origin=USER) == (False,)

    def test_the_value_is_checked_once_more_before_it_is_stored(self) -> None:
        # A bug in `coerce_for_write` must not be able to store what `check_value` refuses.
        store = store_of(number("n", 1, minimum=0, maximum=10))

        with mock.patch("tumbleweed.ui.runtime.store.coerce_for_write", return_value=99):
            applied = store.assign([(key("n"), 5)], origin=USER)

        assert applied == (False,)
        assert store.value(key("n")) == 1

    def test_the_snapshot_is_a_copy(self) -> None:
        store = store_of(number("n", 1))
        snapshot = store.snapshot()

        store.write(key("n"), 2, origin=USER)

        assert snapshot == {"n": 1}
        snapshot[key("n")] = 50
        assert store.value(key("n")) == 2

    def test_what_an_expression_reads_is_the_scalar_or_false(self) -> None:
        store = store_of(number("n", 4), {"kind": "json", "key": "j"})

        assert store.read(key("n")) == 4
        assert store.read(key("j")) is False
        assert store.read(key("nope")) is False


def recorded_by(store: StateStore) -> list[Change]:
    changes: list[Change] = []
    store.subscribe(changes.append)
    return changes


class TestNotifications:
    def test_a_write_notifies_with_its_origin_and_the_value_the_entry_has(self) -> None:
        store = store_of(number("n", 1, minimum=0, maximum=10))
        changes = recorded_by(store)

        store.write(key("n"), 50, origin=Origin.HANDLER)

        assert changes == [Change(key("n"), 10, Origin.HANDLER)]

    def test_an_equal_value_still_notifies_because_a_widget_may_need_to_reload(self) -> None:
        store = store_of({"kind": "image", "key": "i"}, number("n", 1))
        changes = recorded_by(store)

        store.write(key("n"), 1, origin=USER)
        store.patch({"i": {"path": "a.png"}})
        store.patch({"i": {"path": "a.png"}})

        assert [c.key for c in changes] == ["n", "i", "i"]
        assert all(c.applied for c in changes)

    def test_a_write_that_is_refused_notifies_with_the_old_value_so_the_widget_goes_back(
        self,
    ) -> None:
        store = store_of(choice("e", ["a", "b"], "a"))
        changes = recorded_by(store)

        store.write(key("e"), "zzz", origin=USER)

        assert changes == [Change(key("e"), "a", USER, applied=False)]

    def test_computed_entries_are_announced_after_the_write_in_dependency_order(self) -> None:
        store = store_of(
            number("n", 1),
            computed("c", "int", binary("add", ref("b"), lit_int(1)), 0),
            computed("b", "int", binary("add", ref("n"), lit_int(1)), 0),
        )
        changes = recorded_by(store)

        store.write(key("n"), 10, origin=USER)

        assert [(c.key, c.value, c.origin) for c in changes] == [
            ("n", 10, USER),
            ("b", 11, Origin.COMPUTED),
            ("c", 12, Origin.COMPUTED),
        ]

    def test_a_computed_entry_whose_value_did_not_change_is_not_announced(self) -> None:
        store = store_of(
            number("n", 1),
            computed(
                "sign",
                "int",
                {
                    "kind": "if",
                    "condition": binary("gt", ref("n"), lit_int(0)),
                    "then": lit_int(1),
                    "otherwise": lit_int(-1),
                },
                0,
            ),
        )
        changes = recorded_by(store)

        store.write(key("n"), 2, origin=USER)

        assert [c.key for c in changes] == ["n"]

    def test_listeners_are_told_in_the_order_they_subscribed_and_can_stop(self) -> None:
        store = store_of(number("n", 1))
        order: list[str] = []
        store.subscribe(lambda _c: order.append("first"))
        stop = store.subscribe(lambda _c: order.append("second"))
        store.subscribe(lambda _c: order.append("third"))

        store.write(key("n"), 2, origin=USER)
        stop()
        stop()  # stopping twice is harmless
        store.write(key("n"), 3, origin=USER)

        assert order == ["first", "second", "third", "first", "third"]

    def test_a_listener_that_raises_is_logged_and_the_others_still_hear(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        store = store_of(number("n", 1))
        heard: list[Change] = []

        def broken(_change: Change) -> None:
            raise RuntimeError("a widget bug")

        store.subscribe(broken)
        store.subscribe(heard.append)

        with caplog.at_level(logging.ERROR):
            store.write(key("n"), 2, origin=USER)

        assert len(heard) == 1
        assert "a state listener failed on n" in caplog.text
        assert store.value(key("n")) == 2

    def test_a_listener_that_writes_has_its_notifications_delivered_after_the_queued_ones(
        self,
    ) -> None:
        store = store_of(number("n", 1), number("m", 0))
        seen: list[tuple[str, object]] = []

        def echo(change: Change) -> None:
            seen.append((change.key, change.value))
            if change.key == "n":
                store.write(key("m"), 7, origin=Origin.HANDLER)

        store.subscribe(echo)
        store.subscribe(lambda c: seen.append(("second", c.key)))

        store.write(key("n"), 2, origin=USER)

        # `n` reaches both listeners before the write that the first one made reaches either.
        assert seen == [("n", 2), ("second", "n"), ("m", 7), ("second", "m")]

    def test_listeners_that_answer_each_other_forever_are_cut_off(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        store = store_of(flag("a"), flag("b"))

        def flip_other(change: Change) -> None:
            other = key("b") if change.key == "a" else key("a")
            store.write(other, change.value is not True, origin=Origin.HANDLER)

        store.subscribe(flip_other)

        with caplog.at_level(logging.ERROR):
            store.write(key("a"), True, origin=USER)  # returns instead of hanging

        assert "dropped" in caplog.text
        assert MAX_DELIVERIES > 100

    def test_a_listener_may_read_a_consistent_state(self) -> None:
        store = store_of(number("n", 1), doubled())
        seen: list[tuple[object, object]] = []
        store.subscribe(lambda _c: seen.append((store.value(key("n")), store.value(key("d")))))

        store.write(key("n"), 5, origin=USER)

        assert seen == [(5, 10), (5, 10)]  # for n, then for d: both already updated


class TestComputedPropagation:
    def test_only_the_entries_that_read_what_changed_are_evaluated(self) -> None:
        store = store_of(
            number("a", 1),
            number("b", 1),
            computed("from_a", "int", binary("add", ref("a"), lit_int(1)), 0),
            computed("from_b", "int", binary("add", ref("b"), lit_int(1)), 0),
        )

        with mock.patch(
            "tumbleweed.ui.runtime.store.evaluate",
            wraps=__import__("tumbleweed.ui.runtime.interpreter", fromlist=["evaluate"]).evaluate,
        ) as spy:
            store.write(key("a"), 5, origin=USER)

        assert spy.call_count == 1
        assert store.value(key("from_a")) == 6
        assert store.value(key("from_b")) == 2

    def test_a_chain_and_a_diamond_each_settle_in_one_pass(self) -> None:
        store = store_of(
            number("a", 1),
            computed("left", "int", binary("add", ref("a"), lit_int(10)), 0),
            computed("right", "int", binary("add", ref("a"), lit_int(100)), 0),
            computed("both", "int", binary("add", ref("left"), ref("right")), 0),
            computed("after", "int", binary("mul", ref("both"), lit_int(2)), 0),
        )
        changes = recorded_by(store)

        store.write(key("a"), 2, origin=USER)

        assert store.value(key("both")) == 12 + 102
        assert store.value(key("after")) == (12 + 102) * 2
        assert [c.key for c in changes].count(key("both")) == 1  # not once per path
        assert [c.key for c in changes] == ["a", "left", "right", "both", "after"]

    def test_a_computed_value_is_coerced_like_any_other_write(self) -> None:
        store = StateStore(
            spec_with(
                [
                    number("n", 1),
                    {
                        "kind": "int",
                        "key": "capped",
                        "default": 0,
                        "minimum": 0,
                        "maximum": 10,
                        "computed": binary("mul", ref("n"), lit_int(100)),
                    },
                    {
                        "kind": "string",
                        "key": "short",
                        "default": "",
                        "max_length": 3,
                        "computed": variadic("concat", lit_str("ab"), lit_str("cdef")),
                    },
                    {
                        "kind": "float",
                        "key": "wide",
                        "default": 0.0,
                        "computed": ref("n"),
                    },
                ]
            )
        )

        store.write(key("n"), 5, origin=USER)

        assert store.value(key("capped")) == 10
        assert store.value(key("short")) == "abc"
        assert store.value(key("wide")) == 5.0
        assert type(store.value(key("wide"))) is float

    def test_a_write_that_is_not_applied_recomputes_nothing(self) -> None:
        store = store_of(
            choice("e", ["a", "b"], "a"),
            computed("c", "string", variadic("concat", ref("e"), lit_str("!")), ""),
        )
        changes = recorded_by(store)

        store.write(key("e"), "zzz", origin=USER)

        assert [c.key for c in changes] == ["e"]


class TestPatch:
    def test_a_patch_is_applied_whole_with_origin_patch(self) -> None:
        store = store_of(number("n", 1), text("s", "a"), doubled())
        changes = recorded_by(store)

        store.patch({"n": 5, "s": "b"})

        assert store.value(key("n")) == 5
        assert store.value(key("s")) == "b"
        assert store.value(key("d")) == 10
        assert [(c.key, c.origin) for c in changes] == [
            ("n", Origin.PATCH),
            ("s", Origin.PATCH),
            ("d", Origin.COMPUTED),
        ]

    def test_a_patch_is_not_clamped_it_is_refused(self) -> None:
        store = store_of(number("n", 1, minimum=0, maximum=10))
        changes = recorded_by(store)

        with pytest.raises(SpecError, match="at most 10"):
            store.patch({"n": 50})

        assert store.value(key("n")) == 1
        assert changes == []

    @pytest.mark.parametrize(
        ("patch", "message"),
        [
            ({"nope": 1}, "not declared"),
            ({"d": 1}, "computed"),
            ({"n": "5"}, "an int"),
            ({"n": True}, "an int"),
            ({"n": 5.0}, "an int"),
            ({"e": "zzz"}, "one of 2 options"),
            ({"s": "x" * 1001}, "limit is 1000"),
            ({"n": 1, "e": "zzz"}, "one of 2 options"),  # one bad value refuses the lot
        ],
    )
    def test_an_invalid_patch_is_refused_whole_and_changes_nothing(
        self, patch: dict[str, object], message: str
    ) -> None:
        store = store_of(number("n", 1), text("s"), choice("e", ["a", "b"], "a"), doubled())
        before = store.snapshot()
        changes = recorded_by(store)

        with pytest.raises(SpecError, match=message):
            store.patch(patch)

        assert store.snapshot() == before
        assert changes == []

    def test_only_limits_the_keys_for_a_sandbox_result(self) -> None:
        store = store_of(number("n", 1), number("m", 1))

        with pytest.raises(SpecError, match="not one of the entries this may write"):
            store.patch({"n": 2, "m": 2}, origin=Origin.SANDBOX, only=[key("n")])
        store.patch({"n": 2}, origin=Origin.SANDBOX, only=[key("n")])

        assert (store.value(key("n")), store.value(key("m"))) == (2, 1)

    def test_a_patch_can_set_every_kind_of_value(self) -> None:
        store = store_of(
            {"kind": "table", "key": "t"},
            {"kind": "list", "key": "l", "item_type": "float"},
            {"kind": "json", "key": "j"},
            {"kind": "file", "key": "f"},
        )

        store.patch(
            {
                "t": {"columns": [{"name": "a", "type": "int"}], "rows": [[1], [2]]},
                "l": [1, 2.5],
                "j": {"a": [1, None]},
                "f": {"path": "out/x.txt"},
            }
        )

        assert len(store.value(key("t")).rows) == 2  # type: ignore[union-attr]  # a Table
        assert store.value(key("l")) == (1.0, 2.5)
        assert store.value(key("j")) == {"a": [1, None]}

    def test_a_patch_of_a_computed_entrys_input_recomputes_it(self) -> None:
        store = store_of(number("n", 1), doubled())

        store.patch({"n": 21})

        assert store.value(key("d")) == 42


def budget_store() -> StateStore:
    """A store holding exactly `MAX_STATE_TEXT_BYTES - 65536` bytes: room for 64 KiB more."""
    state = [text(f"b{i}", max_length=65536) for i in range(63)]
    state += [text("t", max_length=65536), text("u", max_length=10)]
    store = StateStore(spec_with(state))
    store.patch({f"b{i}": "x" * 65536 for i in range(32)})
    store.patch({f"b{i}": "x" * 65536 for i in range(32, 63)})
    assert store.text_bytes == 63 * 65536
    return store


class TestTheStateBudget:
    def test_a_write_that_grows_the_text_up_to_the_budget_is_applied(self) -> None:
        store = budget_store()

        assert store.write(key("t"), "y" * 65536, origin=USER) is True
        assert store.text_bytes == MAX_STATE_TEXT_BYTES

    def test_a_write_that_grows_it_past_the_budget_is_held_back(self) -> None:
        store = budget_store()
        store.write(key("t"), "y" * 65536, origin=USER)
        changes = recorded_by(store)

        applied = store.write(key("u"), "z", origin=USER)

        assert applied is False
        assert store.value(key("u")) == ""
        assert store.text_bytes == MAX_STATE_TEXT_BYTES
        assert changes == [Change(key("u"), "", USER, applied=False)]

    def test_a_write_that_shrinks_or_keeps_the_size_is_applied_at_the_limit(self) -> None:
        store = budget_store()
        store.write(key("t"), "y" * 65536, origin=USER)

        assert store.write(key("t"), "z" * 65536, origin=USER) is True  # same size
        assert store.write(key("t"), "z" * 100, origin=USER) is True  # shrinks
        assert store.write(key("u"), "z", origin=USER) is True  # and there is room again
        assert store.text_bytes == 63 * 65536 + 100 + 1

    def test_the_size_counts_bytes_not_characters(self) -> None:
        store = budget_store()
        store.write(key("t"), "y" * 65535, origin=USER)  # one byte of room left

        assert store.write(key("u"), "z", origin=USER) is True
        assert store.write(key("u"), "é", origin=USER) is False  # two bytes: one too many

    def test_a_patch_that_would_exceed_the_budget_is_refused(self) -> None:
        store = budget_store()
        store.write(key("t"), "y" * 65536, origin=USER)

        with pytest.raises(SpecError, match="would grow the text of the state"):
            store.patch({"u": "z"})

        assert store.value(key("u")) == ""


def over_budget_store() -> StateStore:
    """A store at the budget whose computed entry `c` has just been held back."""
    state = [text(f"b{i}", max_length=65536) for i in range(62)]
    state += [
        text("t", max_length=65536),
        text("pad", max_length=65536),
        {
            **computed("c", "string", variadic("concat", ref("t"), lit_str("!")), ""),
            "max_length": 65536,
        },
        {
            **computed("c2", "string", variadic("concat", ref("c"), lit_str("?")), ""),
            "max_length": 65536,
        },
        computed("c3", "int", binary("add", lit_int(1), lit_int(1)), 0),
    ]
    store = StateStore(spec_with(state))
    store.patch({f"b{i}": "x" * 65536 for i in range(31)})
    store.patch({f"b{i}": "x" * 65536 for i in range(31, 62)})
    return store


class TestStaleComputedEntries:
    def test_a_computed_write_that_is_held_back_marks_the_entry_stale(self) -> None:
        store = over_budget_store()
        changes = recorded_by(store)
        store.write(key("pad"), "p" * 65535, origin=USER)  # leaves 65536 + 1 for t, c, c2

        store.write(key("t"), "t" * 60000, origin=USER)  # c would be 60001 more: over

        stale = [c for c in changes if c.stale]
        assert store.is_stale(key("c")) is True
        assert store.value(key("c")) == "!"  # the old value, kept
        assert stale[0].key == "c"
        assert stale[0].applied is False
        assert stale[0].origin is Origin.COMPUTED

    def test_an_entry_that_reads_a_stale_one_is_stale_too_and_one_that_does_not_is_not(
        self,
    ) -> None:
        store = over_budget_store()
        store.write(key("pad"), "p" * 65535, origin=USER)

        store.write(key("t"), "t" * 60000, origin=USER)

        assert store.is_stale(key("c2")) is True
        assert store.is_stale(key("c3")) is False
        assert store.is_stale(key("t")) is False  # not computed: never stale
        assert store.is_stale(key("nope")) is False

    def test_a_later_recomputation_that_applies_clears_the_mark_and_says_so(self) -> None:
        store = over_budget_store()
        store.write(key("pad"), "p" * 65535, origin=USER)
        store.write(key("t"), "t" * 60000, origin=USER)
        changes = recorded_by(store)

        store.write(key("t"), "t", origin=USER)  # now c fits again

        assert store.value(key("c")) == "t!"
        assert store.is_stale(key("c")) is False
        assert store.is_stale(key("c2")) is False
        cleared = {str(c.key): c for c in changes if c.origin is Origin.COMPUTED}
        assert cleared["c"].stale is False
        assert cleared["c"].applied is True
        assert cleared["c2"].stale is False

    def test_a_stale_entry_that_stays_stale_notifies_only_when_the_mark_changes(self) -> None:
        store = over_budget_store()
        store.write(key("pad"), "p" * 65535, origin=USER)
        store.write(key("t"), "t" * 60000, origin=USER)
        changes = recorded_by(store)

        store.write(key("t"), "t" * 61000, origin=USER)  # still too big for c

        assert [c.key for c in changes] == ["t"]
        assert store.is_stale(key("c")) is True


class TestThreads:
    def run_elsewhere(self, action: Callable[[], object]) -> BaseException | None:
        raised: list[BaseException] = []

        def target() -> None:
            try:
                action()
            except BaseException as error:  # noqa: BLE001  (the test reports it)
                raised.append(error)

        thread = threading.Thread(target=target)
        thread.start()
        thread.join(timeout=10)
        assert not thread.is_alive()
        return raised[0] if raised else None

    @pytest.mark.parametrize(
        "action",
        [
            lambda store: store.value(key("n")),
            lambda store: store.snapshot(),
            lambda store: store.is_stale(key("n")),
            lambda store: store.subscribe(lambda _c: None),
            lambda store: store.write(key("n"), 2, origin=USER),
            lambda store: store.assign([(key("n"), 2)], origin=USER),
            lambda store: store.patch({"n": 2}),
            lambda store: store.check_thread(),
        ],
        ids=["value", "snapshot", "is_stale", "subscribe", "write", "assign", "patch", "check"],
    )
    def test_the_store_belongs_to_the_thread_that_made_it(
        self, action: Callable[[StateStore], object]
    ) -> None:
        store = store_of(number("n", 1))

        error = self.run_elsewhere(lambda: action(store))

        assert isinstance(error, WrongThreadError)
        assert store.value(key("n")) == 1  # and nothing was changed


class TestStaleEntriesAreRetriedWhenRoomIsMade:
    def make_stale(self) -> StateStore:
        store = over_budget_store()
        store.write(key("pad"), "p" * 65535, origin=USER)
        store.write(key("t"), "t" * 60000, origin=USER)
        assert store.is_stale(key("c"))
        return store

    def test_a_write_elsewhere_that_shrinks_the_state_lets_the_stale_entry_catch_up(self) -> None:
        store = self.make_stale()
        changes = recorded_by(store)

        store.write(key("pad"), "", origin=USER)  # frees 64 KiB, and reads nothing c reads

        assert store.value(key("c")) == "t" * 60000 + "!"
        assert store.is_stale(key("c")) is False
        # c2 reads c and is 60,002 bytes more: it still does not fit, and says so
        assert store.is_stale(key("c2")) is True
        assert {c.key: c.stale for c in changes if c.origin is Origin.COMPUTED} == {key("c"): False}

        store.write(key("t"), "t", origin=USER)  # now there is room for both

        assert store.is_stale(key("c2")) is False
        assert store.value(key("c2")) == "t!?"

    def test_a_patch_that_shrinks_the_state_does_the_same(self) -> None:
        store = self.make_stale()

        store.patch({"b0": ""})

        assert store.is_stale(key("c")) is False
        assert store.value(key("c")) == "t" * 60000 + "!"

    def test_a_write_that_makes_no_room_leaves_the_entry_stale_and_quiet(self) -> None:
        store = self.make_stale()
        changes = recorded_by(store)

        store.write(key("pad"), "q" * 65535, origin=USER)  # same size: no room made

        assert store.is_stale(key("c")) is True
        assert [c.key for c in changes] == ["pad"]


class TestTextAccounting:
    def test_a_patch_updates_the_text_the_budget_counts(self) -> None:
        store = store_of(text("a", "abc"), text("b", ""), number("n", 1))

        store.patch({"a": "éé", "b": "xyz"})
        assert store.text_bytes == 4 + 3

        store.patch({"a": ""})
        assert store.text_bytes == 3

    def test_a_write_updates_it_too_and_the_two_agree(self) -> None:
        store = store_of(text("a", "abc"), text("b", ""))

        store.write(key("b"), "€", origin=USER)  # three bytes
        store.patch({"a": "z"})

        assert store.text_bytes == 1 + 3

    def test_a_patch_that_ends_exactly_at_the_budget_is_applied(self) -> None:
        store = budget_store()

        store.patch({"t": "y" * 65536})

        assert store.text_bytes == MAX_STATE_TEXT_BYTES

    def test_a_computed_write_that_ends_exactly_at_the_budget_is_applied_and_not_stale(
        self,
    ) -> None:
        state = [text(f"b{i}", max_length=65536) for i in range(63)]
        state += [
            text("t", max_length=65536),
            {**computed("c", "string", ref("t"), ""), "max_length": 65536},
        ]
        store = StateStore(spec_with(state))
        store.patch({f"b{i}": "x" * 65536 for i in range(32)})
        store.patch({f"b{i}": "x" * 65536 for i in range(32, 63)})
        # 63 strings = 4,128,768; t and c (a copy of t) may share the remaining 65,536 bytes
        store.write(key("t"), "y" * 32768, origin=USER)

        assert store.value(key("c")) == "y" * 32768
        assert store.is_stale(key("c")) is False
        assert store.text_bytes == MAX_STATE_TEXT_BYTES
        store.write(key("t"), "y" * 32767, origin=USER)
        assert store.text_bytes == MAX_STATE_TEXT_BYTES - 2 * 1
        store.write(key("t"), "y" * 32769, origin=USER)  # t fits (to the byte); c would not
        assert store.is_stale(key("c")) is True


def chained_store() -> StateStore:
    """Two computed copies (`c1` of `y`, `c2` of `x`, each twice as long) with `c1` before `c2`
    in evaluation order, in a state with room for little: 15 strings of 64K four-byte characters."""
    gclef = "\U0001d11e"

    def doubled(key_: str, source: str) -> Json:
        return {
            "kind": "string",
            "key": key_,
            "default": "",
            "max_length": 65536,
            "computed": {"kind": "concat", "args": [ref(source), ref(source)]},
        }

    state = [text(f"a{i}", max_length=65536) for i in range(15)]
    state += [text("x", max_length=65536), text("y", max_length=65536)]
    state += [doubled("c1", "y"), doubled("c2", "x")]
    store = StateStore(spec_with(state))
    for first in range(0, 15, 7):
        store.patch({f"a{j}": gclef * 65536 for j in range(first, min(first + 7, 15))})
    return store


class TestMoreThanOnePassOfRetrying:
    def test_an_entry_that_fits_only_once_a_later_one_has_shrunk_is_served_by_the_next_pass(
        self,
    ) -> None:
        gclef = "\U0001d11e"
        store = chained_store()
        store.write(key("x"), gclef * 20000, origin=USER)  # x 80K and c2 160K
        store.write(key("y"), gclef * 2000, origin=USER)  # y 8K: c1 (16K) does not fit
        assert store.is_stale(key("c1"))
        assert not store.is_stale(key("c2"))

        # x shrinks: its copy c2 shrinks too, and that frees room for c1, which is earlier in
        # the order than the write that freed it: it is served by a second pass.
        store.write(key("x"), gclef * 19750, origin=USER)

        assert store.is_stale(key("c1")) is False
        assert store.value(key("c1")) == gclef * 4000
        assert store.is_stale(key("c2")) is False
