"""Tests for `tumbleweed.broker.grants` and `capabilities`: the grant model, as data."""

import datetime as dt

import pytest
from pydantic import TypeAdapter, ValidationError

from tests.broker.consts import HASH_A, HASH_B, NOW
from tumbleweed.broker import (
    Capability,
    CapabilityKind,
    FsIdentity,
    FsRead,
    FsWrite,
    Grant,
    GrantError,
    GrantOrigin,
    GrantScope,
    NetFull,
    OnceGrant,
    RunContext,
    RunTrigger,
    StoredPath,
    TaskGrant,
    ToolBinding,
    ToolGrant,
)
from tumbleweed.core.ids import GrantId, RunId, TaskId, ToolId, ToolInstanceId

GRANT = TypeAdapter(Grant)
CAPABILITY = TypeAdapter(Capability)
PATH = StoredPath(path="/home/u/Photos", identity=FsIdentity(fsid=7, ino=42))
LATER = NOW + dt.timedelta(minutes=5)


def once(run: RunId | None = None, task: TaskId | None = None, **extra: object) -> OnceGrant:
    return OnceGrant.model_validate(
        {
            "id": GrantId.new(),
            "capability": FsRead(path=PATH),
            "origin": GrantOrigin.PROMPT,
            "issued_at": NOW,
            "task": task or TaskId.new(),
            "run": run or RunId.new(),
            **extra,
        }
    )


class TestCapabilities:
    def test_each_kind_has_its_tag(self) -> None:
        assert FsRead(path=PATH).kind is CapabilityKind.FS_READ
        assert FsWrite(path=PATH).kind is CapabilityKind.FS_WRITE
        assert NetFull().kind is CapabilityKind.NET_FULL

    @pytest.mark.parametrize(
        "capability", [FsRead(path=PATH), FsWrite(path=PATH), NetFull()], ids=lambda c: c.kind.value
    )
    def test_round_trips_through_json(self, capability: FsRead | FsWrite | NetFull) -> None:
        text = CAPABILITY.dump_json(capability)

        assert CAPABILITY.validate_json(text) == capability
        assert f'"kind":"{capability.kind.value}"'.encode() in text

    def test_the_tag_chooses_the_model(self) -> None:
        text = '{"kind": "fs.read", "path": {"path": "/a", "identity": {"fsid": 1, "ino": 2}}}'

        assert isinstance(CAPABILITY.validate_json(text), FsRead)

    @pytest.mark.parametrize(
        "text",
        [
            '{"kind": "fs.delete", "path": {"path": "/a", "identity": {"fsid": 1, "ino": 2}}}',
            '{"kind": "net.full", "host": "example.com"}',
            '{"kind": "fs.read"}',
            '{"kind": "fs.read", "path": "/a"}',
            '{"path": {"path": "/a", "identity": {"fsid": 1, "ino": 2}}}',
            '{"kind": "net.full", "extra": true}',
        ],
        ids=["unknown", "host-scoped", "no-path", "bare-path", "no-kind", "extra"],
    )
    def test_refuses_what_is_not_a_capability(self, text: str) -> None:
        with pytest.raises(ValidationError):
            CAPABILITY.validate_json(text)

    def test_is_frozen(self) -> None:
        with pytest.raises(ValidationError):
            NetFull().kind = CapabilityKind.FS_READ  # type: ignore[misc,assignment]  # the point


class TestGrantModels:
    def test_a_once_grant_is_bound_to_a_task_and_a_run(self) -> None:
        run, task = RunId.new(), TaskId.new()

        grant = once(run=run, task=task)

        assert grant.kind is GrantScope.ONCE
        assert (grant.run, grant.task, grant.consumed_by) == (run, task, None)

    def test_a_task_grant(self) -> None:
        grant = TaskGrant(
            id=GrantId.new(),
            capability=NetFull(),
            origin=GrantOrigin.PICKER,
            issued_at=NOW,
            task=TaskId.new(),
        )

        assert grant.kind is GrantScope.TASK
        assert grant.expires_at is None

    def test_a_tool_grant_carries_the_tool_the_hash_and_the_user_trigger_restriction(self) -> None:
        grant = ToolGrant(
            id=GrantId.new(),
            capability=FsRead(path=PATH),
            origin=GrantOrigin.PROMPT,
            issued_at=NOW,
            tool=ToolId.new(),
            content_hash=HASH_A,
        )

        assert grant.kind is GrantScope.TOOL
        assert grant.content_hash == HASH_A
        assert grant.trigger is RunTrigger.USER_EVENT

    @pytest.mark.parametrize("trigger", ["model", "state_patch", "connection"])
    def test_a_tool_grant_cannot_be_made_for_any_other_trigger(self, trigger: str) -> None:
        text = (
            f'{{"kind": "tool", "id": "{GrantId.new()}", "capability": {{"kind": "net.full"}}, '
            f'"origin": "prompt", "issued_at": "2026-10-16T12:00:00Z", "tool": "{ToolId.new()}", '
            f'"content_hash": "{HASH_A}", "trigger": "{trigger}"}}'
        )

        with pytest.raises(ValidationError):
            GRANT.validate_json(text)

    @pytest.mark.parametrize("scope", ["once", "task", "tool"])
    def test_every_scope_round_trips_through_the_union(self, scope: str) -> None:
        grants: dict[str, Grant] = {
            "once": once(),
            "task": TaskGrant(
                id=GrantId.new(),
                capability=FsRead(path=PATH),
                origin=GrantOrigin.PROMPT,
                issued_at=NOW,
                expires_at=LATER,
                task=TaskId.new(),
            ),
            "tool": ToolGrant(
                id=GrantId.new(),
                capability=NetFull(),
                origin=GrantOrigin.PROMPT,
                issued_at=NOW,
                tool=ToolId.new(),
                content_hash=HASH_B,
            ),
        }

        grant = grants[scope]

        assert GRANT.validate_json(GRANT.dump_json(grant)) == grant

    def test_ids_of_the_wrong_type_are_refused(self) -> None:
        with pytest.raises(ValidationError):
            once(run=ToolId.new())  # type: ignore[arg-type]  # the point of the test
        with pytest.raises(ValidationError):
            TaskGrant.model_validate(
                {
                    "id": TaskId.new(),
                    "capability": NetFull(),
                    "origin": GrantOrigin.PROMPT,
                    "issued_at": NOW,
                    "task": TaskId.new(),
                }
            )

    def test_a_bad_content_hash_is_refused(self) -> None:
        with pytest.raises(ValidationError):
            ToolGrant.model_validate_json(
                f'{{"id": "{GrantId.new()}", "capability": {{"kind": "net.full"}}, '
                '"origin": "prompt", "issued_at": "2026-10-16T12:00:00Z", '
                f'"tool": "{ToolId.new()}", "content_hash": "ABC"}}'
            )

    def test_a_grant_must_expire_after_it_was_issued(self) -> None:
        for expires in (NOW, NOW - dt.timedelta(seconds=1)):
            with pytest.raises(ValidationError, match="expire after"):
                once(expires_at=expires)

    def test_times_must_carry_a_timezone(self) -> None:
        naive = dt.datetime(2026, 10, 16, 12, 0)  # noqa: DTZ001  (the point of the test)
        with pytest.raises(ValidationError):
            once(issued_at=naive)
        with pytest.raises(ValidationError):
            once(expires_at=naive.replace(year=2027))

    def test_unknown_fields_are_refused(self) -> None:
        with pytest.raises(ValidationError):
            once(note="x")

    def test_is_frozen(self) -> None:
        grant = once()

        with pytest.raises(ValidationError):
            grant.consumed_by = grant.run  # type: ignore[misc]  # the point of the test

    def test_an_unknown_scope_is_refused(self) -> None:
        text = (
            f'{{"kind": "forever", "id": "{GrantId.new()}", "capability": {{"kind": "net.full"}}, '
            f'"origin": "prompt", "issued_at": "2026-10-16T12:00:00Z", "task": "{TaskId.new()}"}}'
        )

        with pytest.raises(ValidationError):
            GRANT.validate_json(text)


class TestExpiry:
    def test_a_grant_without_expiry_never_expires(self) -> None:
        assert not once().is_expired(NOW + dt.timedelta(days=36500))

    def test_expired_from_the_instant_it_expires(self) -> None:
        grant = once(expires_at=LATER)

        assert not grant.is_expired(LATER - dt.timedelta(microseconds=1))
        assert grant.is_expired(LATER)
        assert grant.is_expired(LATER + dt.timedelta(microseconds=1))

    def test_compares_instants_across_time_zones(self) -> None:
        grant = once(expires_at=LATER)
        elsewhere = dt.timezone(dt.timedelta(hours=9))

        assert grant.is_expired(LATER.astimezone(elsewhere))
        assert not grant.is_expired((LATER - dt.timedelta(seconds=1)).astimezone(elsewhere))


class TestConsume:
    def test_the_run_it_is_bound_to_consumes_it(self) -> None:
        run = RunId.new()
        grant = once(run=run)

        spent = grant.consume(run)

        assert spent.consumed_by == run
        assert grant.consumed_by is None  # the original is unchanged
        assert (spent.id, spent.task, spent.run, spent.capability) == (
            grant.id,
            grant.task,
            grant.run,
            grant.capability,
        )

    def test_no_other_run_can_consume_it(self) -> None:
        grant = once()

        with pytest.raises(GrantError, match="only by the run"):
            grant.consume(RunId.new())

    def test_it_cannot_be_consumed_twice(self) -> None:
        run = RunId.new()
        spent = once(run=run).consume(run)

        with pytest.raises(GrantError, match="already consumed"):
            spent.consume(run)

    def test_a_grant_cannot_be_stored_as_consumed_by_another_run(self) -> None:
        with pytest.raises(ValidationError, match="only by the run"):
            once(consumed_by=RunId.new())

    def test_a_spent_grant_survives_a_round_trip_spent(self) -> None:
        run = RunId.new()
        spent = once(run=run).consume(run)

        assert GRANT.validate_json(GRANT.dump_json(spent)) == spent


class TestRunContext:
    def test_a_run_with_and_without_a_saved_tool(self) -> None:
        binding = ToolBinding(instance=ToolInstanceId.new(), tool=ToolId.new(), content_hash=HASH_A)

        assert (
            RunContext(run=RunId.new(), trigger=RunTrigger.USER_EVENT, tool=binding).tool == binding
        )
        assert RunContext(run=RunId.new(), trigger=RunTrigger.MODEL).tool is None

    def test_every_trigger_is_one_of_four(self) -> None:
        assert {trigger.value for trigger in RunTrigger} == {
            "user_event",
            "model",
            "state_patch",
            "connection",
        }

    def test_is_strict(self) -> None:
        with pytest.raises(ValidationError):
            RunContext.model_validate({"run": RunId.new(), "trigger": "voice"})
        with pytest.raises(ValidationError):
            RunContext.model_validate({"run": TaskId.new(), "trigger": "model"})
