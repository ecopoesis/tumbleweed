"""Tests for `tumbleweed.broker.policy`: allow, deny or ask, and the rules behind each."""

import datetime as dt
import itertools
import os
from pathlib import Path

import pytest
from pydantic import TypeAdapter, ValidationError

from tests.broker.consts import HASH_A, HASH_B, NOW
from tests.broker.fsutil import make_fifo
from tests.broker.requests import request, run_context
from tests.broker.support import World, resolve
from tumbleweed.broker import (
    AccessRequest,
    Allow,
    BrokerError,
    CapabilityKind,
    Decision,
    Deny,
    DenyContext,
    DenyRelation,
    DenyRules,
    ExtraDeny,
    FsIdentity,
    FsRead,
    FsWrite,
    Grant,
    GrantOrigin,
    GrantScope,
    Issuance,
    Issued,
    NeedsPrompt,
    NetFull,
    OnceGrant,
    PathError,
    PathFault,
    ResolvedPath,
    RunContext,
    RunTrigger,
    SkipReason,
    StoredPath,
    TaintKind,
    TaskGrant,
    TaskState,
    ToolGrant,
    decide,
    describe,
    issue_grant,
    open_granted,
    open_in_workspace,
    read_workspace_file,
    select_grants,
)
from tumbleweed.broker.policy import (
    BadExpiry,
    ScopeUnavailable,
    StaticDeny,
    TaintConflict,
    TaskEnded,
    TaskMismatch,
    Ungrantable,
    WriteGateClosed,
)
from tumbleweed.core.errors import InvalidValueError
from tumbleweed.core.ids import GrantId, RunId, TaskId, ToolId
from tumbleweed.core.types import ByteCount, ContentHash, WorkspaceRelPath

FS = frozenset({TaintKind.FS})
NET = frozenset({TaintKind.NET})
MINUTE = dt.timedelta(minutes=1)
READ, WRITE, NETWORK = CapabilityKind.FS_READ, CapabilityKind.FS_WRITE, CapabilityKind.NET_FULL


@pytest.fixture
def task() -> TaskState:
    return TaskState(task=TaskId.new())


def issued(
    world: World,
    task: TaskState,
    path: Path | str | None,
    *,
    scope: GrantScope = GrantScope.TASK,
    capability: CapabilityKind = READ,
    run: RunContext | None = None,
    origin: GrantOrigin = GrantOrigin.PROMPT,
    now: dt.datetime = NOW,
    expires_at: dt.datetime | None = None,
) -> tuple[Grant, TaskState]:
    """Issue a grant the way a user decision would, and return it with the task's new state."""
    result = issue_grant(
        task,
        request(task.task, path, capability=capability, run=run),
        scope=scope,
        origin=origin,
        grant_id=GrantId.new(),
        rules=world.rules,
        now=now,
        expires_at=expires_at,
    )
    assert isinstance(result, Issued), result
    return result.grant, result.task


def refusal_of(decision: Decision | Issuance) -> object:
    assert isinstance(decision, Deny), decision
    return decision.reason


def tool_run(tool: ToolId, *, content_hash: ContentHash = HASH_A) -> RunContext:
    return run_context(RunTrigger.USER_EVENT, tool=tool, content_hash=content_hash)


def forged(path: Path, capability: type[FsRead] | type[FsWrite], task: TaskState) -> Grant:
    """A task grant on `path` as a store would hand it back, bypassing `issue_grant`."""
    stored = StoredPath.record(resolve(path))
    return TaskGrant(
        id=GrantId.new(),
        capability=capability(path=stored),
        origin=GrantOrigin.PROMPT,
        issued_at=NOW,
        task=task.task,
    )


class TestDenyBeatsGrant:
    def test_a_grant_over_a_protected_place_does_not_open_it(
        self, world: World, task: TaskState
    ) -> None:
        grant = forged(world.home / ".ssh", FsRead, task)
        asking = request(task.task, world.home / ".ssh" / "id_rsa")

        decision = decide(task, [grant], asking, rules=world.rules, now=NOW)

        reason = refusal_of(decision)
        assert isinstance(reason, StaticDeny)
        assert (reason.entry, reason.relation) == ("~/.ssh", DenyRelation.INSIDE)

    def test_nor_does_a_grant_over_a_place_that_holds_a_protected_one(
        self, world: World, task: TaskState
    ) -> None:
        """Granted before the list grew, or forged into the store: its other files are no
        longer served by it, since mounting the grant would mount the protected place with it."""
        grant = forged(world.home / ".config", FsRead, task)
        (world.home / ".config" / "other").mkdir(exist_ok=True)

        selection = select_grants(task, run_context(), [grant], rules=world.rules, now=NOW)
        decision = decide(
            task,
            [grant],
            request(task.task, world.home / ".config" / "other"),
            rules=world.rules,
            now=NOW,
        )

        assert [(s.grant, s.reason) for s in selection.skipped] == [
            (grant.id, SkipReason.STATIC_DENY)
        ]
        assert isinstance(decision, NeedsPrompt)  # not an Allow; a new, narrower grant is possible

    def test_a_grant_made_before_the_list_grew_is_withdrawn_when_it_grows(
        self, world: World, task: TaskState
    ) -> None:
        grant, task = issued(world, task, world.home / "Documents")
        asking = request(task.task, world.home / "Documents" / "notes.txt")
        before = decide(task, [grant], asking, rules=world.rules, now=NOW)
        grown = DenyRules.build(
            world.context.model_copy(deep=True).__class__.model_validate(
                {
                    **dict(world.context),
                    "extra": (
                        ExtraDeny(label="snapshots", path=str(world.home / "Documents" / "snaps")),
                    ),
                }
            )
        )

        after = decide(task, [grant], asking, rules=grown, now=NOW)

        assert isinstance(before, Allow)
        assert isinstance(after, NeedsPrompt)

    @pytest.mark.parametrize(
        "relative", [".ssh", ".ssh/id_rsa", ".gnupg", ".aws/credentials", ".config/gcloud/x"]
    )
    def test_no_grant_makes_a_protected_path_allowed(
        self, world: World, task: TaskState, relative: str
    ) -> None:
        grants = [forged(world.home / ".ssh", FsRead, task), forged(world.home, FsRead, task)]

        decision = decide(
            task,
            grants,
            request(task.task, world.home / relative),
            rules=world.rules,
            now=NOW,
        )

        assert isinstance(refusal_of(decision), StaticDeny)

    def test_a_hard_link_to_a_protected_file_is_refused_even_under_a_grant(
        self, world: World, task: TaskState
    ) -> None:
        grant, task = issued(world, task, world.home / "Documents")
        os.link(world.home / ".netrc", world.home / "Documents" / "innocent.txt")

        decision = decide(
            task,
            [grant],
            request(task.task, world.home / "Documents" / "innocent.txt"),
            rules=world.rules,
            now=NOW,
        )

        reason = refusal_of(decision)
        assert isinstance(reason, StaticDeny)
        assert reason.entry == "~/.netrc"


class TestTheAncestorRuleWhenIssuing:
    @pytest.mark.parametrize("scope", list(GrantScope))
    def test_the_home_is_refused_in_every_scope(
        self, world: World, task: TaskState, scope: GrantScope
    ) -> None:
        result = issue_grant(
            task,
            request(task.task, world.home, run=tool_run(ToolId.new())),
            scope=scope,
            origin=GrantOrigin.PICKER,
            grant_id=GrantId.new(),
            rules=world.rules,
            now=NOW,
        )

        reason = refusal_of(result)
        assert isinstance(reason, StaticDeny)
        assert reason.relation is DenyRelation.ANCESTOR

    @pytest.mark.parametrize(
        ("relative", "relation"),
        [
            ("", DenyRelation.ANCESTOR),  # ~ itself
            (".ssh", DenyRelation.EQUAL),
            (".ssh/new-dir", DenyRelation.INSIDE),
            (".config", DenyRelation.ANCESTOR),
            (".local/share", DenyRelation.ANCESTOR),
            (".config/gcloud", DenyRelation.EQUAL),
        ],
    )
    def test_equal_inside_and_ancestor_are_each_refused(
        self, world: World, task: TaskState, relative: str, relation: DenyRelation
    ) -> None:
        path = world.home / relative if relative else world.home

        reason = refusal_of(
            issue_grant(
                task,
                request(task.task, path),
                scope=GrantScope.TASK,
                origin=GrantOrigin.PROMPT,
                grant_id=GrantId.new(),
                rules=world.rules,
                now=NOW,
            )
        )

        assert isinstance(reason, StaticDeny)
        assert reason.relation is relation

    def test_every_directory_above_the_home_is_refused_to_the_root(
        self, world: World, task: TaskState
    ) -> None:
        for ancestor in (*world.home.parents,):
            reason = refusal_of(
                issue_grant(
                    task,
                    request(task.task, ancestor),
                    scope=GrantScope.ONCE,
                    origin=GrantOrigin.PICKER,
                    grant_id=GrantId.new(),
                    rules=world.rules,
                    now=NOW,
                )
            )
            assert isinstance(reason, StaticDeny), ancestor

    def test_a_refusal_changes_nothing_about_the_task(self, world: World, task: TaskState) -> None:
        issue_grant(
            task,
            request(task.task, world.home),
            scope=GrantScope.TASK,
            origin=GrantOrigin.PROMPT,
            grant_id=GrantId.new(),
            rules=world.rules,
            now=NOW,
        )

        assert task.taint == frozenset()

    def test_an_ordinary_folder_is_granted(self, world: World, task: TaskState) -> None:
        grant, tainted = issued(world, task, world.home / "Photos")

        assert isinstance(grant, TaskGrant)
        assert isinstance(grant.capability, FsRead)
        assert grant.capability.path.path == str(world.home / "Photos")
        assert tainted.taint == FS


class TestTheWriteGate:
    @pytest.mark.parametrize("trigger", list(RunTrigger))
    def test_every_write_request_is_refused_with_a_clear_reason(
        self, world: World, task: TaskState, trigger: RunTrigger
    ) -> None:
        decision = decide(
            task,
            [],
            request(
                task.task, world.home / "Documents", capability=WRITE, run=run_context(trigger)
            ),
            rules=world.rules,
            now=NOW,
        )

        assert refusal_of(decision) == WriteGateClosed()
        assert "not available yet" in describe(WriteGateClosed())

    def test_a_write_grant_in_the_store_does_not_open_writing(
        self, world: World, task: TaskState
    ) -> None:
        grant = forged(world.home / "Documents", FsWrite, task)

        decision = decide(
            task,
            [grant],
            request(task.task, world.home / "Documents" / "notes.txt", capability=WRITE),
            rules=world.rules,
            now=NOW,
        )

        assert refusal_of(decision) == WriteGateClosed()

    def test_nor_does_it_open_reading(self, world: World, task: TaskState) -> None:
        """`fs.write` implies read, but there can be no write grant: one found is inert."""
        grant = forged(world.home / "Documents", FsWrite, task)

        selection = select_grants(task, run_context(), [grant], rules=world.rules, now=NOW)
        decision = decide(
            task,
            [grant],
            request(task.task, world.home / "Documents" / "notes.txt"),
            rules=world.rules,
            now=NOW,
        )

        assert [s.reason for s in selection.skipped] == [SkipReason.WRITE_GATE]
        assert isinstance(decision, NeedsPrompt)

    @pytest.mark.parametrize("scope", list(GrantScope))
    def test_no_write_grant_can_be_issued_in_any_scope(
        self, world: World, task: TaskState, scope: GrantScope
    ) -> None:
        result = issue_grant(
            task,
            request(
                task.task,
                world.home / "Documents",
                capability=WRITE,
                run=tool_run(ToolId.new()),
            ),
            scope=scope,
            origin=GrantOrigin.PICKER,
            grant_id=GrantId.new(),
            rules=world.rules,
            now=NOW,
        )

        assert refusal_of(result) == WriteGateClosed()

    def test_a_write_request_is_never_asked_about(self, world: World, task: TaskState) -> None:
        """A prompt would be a promise the gate cannot keep: the answer is a plain denial."""
        decision = decide(
            task,
            [],
            request(task.task, world.home / "Photos" / "a.jpg", capability=WRITE),
            rules=world.rules,
            now=NOW,
        )

        assert isinstance(decision, Deny)

    def test_the_protected_place_is_named_before_the_gate(
        self, world: World, task: TaskState
    ) -> None:
        """Where both refuse, the reason that will still hold when writes are enabled."""
        decision = decide(
            task,
            [],
            request(task.task, world.home / ".ssh" / "id_rsa", capability=WRITE),
            rules=world.rules,
            now=NOW,
        )

        assert isinstance(refusal_of(decision), StaticDeny)

    def test_an_ended_task_is_named_before_the_gate(self, world: World, task: TaskState) -> None:
        decision = decide(
            task.end(),
            [],
            request(task.task, world.home / "Documents", capability=WRITE),
            rules=world.rules,
            now=NOW,
        )

        assert refusal_of(decision) == TaskEnded()

    def test_nothing_is_ever_allowed_to_write(self, world: World, task: TaskState) -> None:
        """Whatever the grants, the scope or the trigger: no `Allow` for a write."""
        grants: list[Grant] = [
            forged(world.home / "Documents", FsWrite, task),
            forged(world.home / "Documents", FsRead, task),
        ]
        for trigger in RunTrigger:
            decision = decide(
                task,
                grants,
                request(
                    task.task,
                    world.home / "Documents" / "notes.txt",
                    capability=WRITE,
                    run=run_context(trigger),
                ),
                rules=world.rules,
                now=NOW,
            )
            assert not isinstance(decision, Allow | NeedsPrompt)


class TestOnce:
    def test_is_allowed_for_the_run_it_is_bound_to_and_is_spent_by_it(
        self, world: World, task: TaskState
    ) -> None:
        run = run_context()
        grant, task = issued(world, task, world.home / "Photos", scope=GrantScope.ONCE, run=run)

        decision = decide(
            task,
            [grant],
            request(task.task, world.home / "Photos" / "a.jpg", run=run),
            rules=world.rules,
            now=NOW,
        )

        assert isinstance(decision, Allow)
        assert decision.grant == grant.id
        assert decision.consumes
        assert decision.within_grant == WorkspaceRelPath("a.jpg")

    def test_another_run_cannot_use_it(self, world: World, task: TaskState) -> None:
        grant, task = issued(
            world, task, world.home / "Photos", scope=GrantScope.ONCE, run=run_context()
        )
        other = run_context()

        decision = decide(
            task,
            [grant],
            request(task.task, world.home / "Photos" / "a.jpg", run=other),
            rules=world.rules,
            now=NOW,
        )
        selection = select_grants(task, other, [grant], rules=world.rules, now=NOW)

        assert isinstance(decision, NeedsPrompt)
        assert [(s.grant, s.reason) for s in selection.skipped] == [
            (grant.id, SkipReason.OTHER_RUN)
        ]

    def test_it_is_spent_after_its_run_consumed_it(self, world: World, task: TaskState) -> None:
        run = run_context()
        grant, task = issued(world, task, world.home / "Photos", scope=GrantScope.ONCE, run=run)
        assert isinstance(grant, OnceGrant)
        spent = grant.consume(run.run)

        for asking_run in (run, run_context()):
            decision = decide(
                task,
                [spent],
                request(task.task, world.home / "Photos", run=asking_run),
                rules=world.rules,
                now=NOW,
            )
            assert isinstance(decision, NeedsPrompt)
        selection = select_grants(task, run, [spent], rules=world.rules, now=NOW)
        assert [s.reason for s in selection.skipped] == [SkipReason.CONSUMED]

    def test_only_its_own_run_can_consume_it(self, world: World, task: TaskState) -> None:
        run = run_context()
        grant, _ = issued(world, task, world.home / "Photos", scope=GrantScope.ONCE, run=run)
        assert isinstance(grant, OnceGrant)

        with pytest.raises(BrokerError):
            grant.consume(run_context().run)

    def test_the_run_it_belongs_to_has_the_once_grant_of_its_own_task_only(
        self, world: World, task: TaskState
    ) -> None:
        run = run_context()
        grant, _ = issued(world, task, world.home / "Photos", scope=GrantScope.ONCE, run=run)
        elsewhere = TaskState(task=TaskId.new())

        selection = select_grants(elsewhere, run, [grant], rules=world.rules, now=NOW)

        assert [s.reason for s in selection.skipped] == [SkipReason.OTHER_TASK]

    def test_a_task_grant_is_used_in_preference_so_the_once_grant_stays_unspent(
        self, world: World, task: TaskState
    ) -> None:
        run = run_context()
        once, task = issued(world, task, world.home / "Photos", scope=GrantScope.ONCE, run=run)
        wide, task = issued(
            world, task, world.home / "Photos", scope=GrantScope.TASK, now=NOW + MINUTE
        )

        decision = decide(
            task,
            [once, wide],
            request(task.task, world.home / "Photos" / "a.jpg", run=run),
            rules=world.rules,
            now=NOW + MINUTE,
        )

        assert isinstance(decision, Allow)
        assert decision.grant == wide.id
        assert not decision.consumes


class TestExpiry:
    @pytest.mark.parametrize("scope", list(GrantScope))
    def test_a_grant_stops_working_at_the_instant_it_expires(
        self, world: World, task: TaskState, scope: GrantScope
    ) -> None:
        tool = ToolId.new()
        run = tool_run(tool)
        grant, task = issued(
            world,
            task,
            world.home / "Photos",
            scope=scope,
            run=run,
            expires_at=NOW + 5 * MINUTE,
        )
        asking = request(task.task, world.home / "Photos" / "a.jpg", run=run)

        outcomes = [
            decide(task, [grant], asking, rules=world.rules, now=NOW + offset)
            for offset in (
                MINUTE,
                5 * MINUTE - dt.timedelta(microseconds=1),
                5 * MINUTE,
                99 * MINUTE,
            )
        ]

        assert [type(outcome) for outcome in outcomes] == [Allow, Allow, NeedsPrompt, NeedsPrompt]
        selection = select_grants(task, run, [grant], rules=world.rules, now=NOW + 5 * MINUTE)
        assert [s.reason for s in selection.skipped] == [SkipReason.EXPIRED]

    def test_a_grant_without_expiry_does_not_expire(self, world: World, task: TaskState) -> None:
        grant, task = issued(world, task, world.home / "Photos")
        asking = request(task.task, world.home / "Photos")

        decision = decide(
            task, [grant], asking, rules=world.rules, now=NOW + dt.timedelta(days=36500)
        )

        assert isinstance(decision, Allow)

    def test_expiry_is_judged_in_instants_not_in_wall_clock_digits(
        self, world: World, task: TaskState
    ) -> None:
        grant, task = issued(world, task, world.home / "Photos", expires_at=NOW + MINUTE)
        elsewhere = dt.timezone(dt.timedelta(hours=-9))

        decision = decide(
            task,
            [grant],
            request(task.task, world.home / "Photos"),
            rules=world.rules,
            now=(NOW + MINUTE).astimezone(elsewhere),
        )

        assert isinstance(decision, NeedsPrompt)

    def test_a_time_without_a_time_zone_is_refused(self, world: World, task: TaskState) -> None:
        naive = dt.datetime(2026, 10, 16, 12, 0)  # noqa: DTZ001  (the point of the test)

        with pytest.raises(InvalidValueError, match="timezone-aware"):
            decide(
                task, [], request(task.task, world.home / "Photos"), rules=world.rules, now=naive
            )
        with pytest.raises(InvalidValueError, match="timezone-aware"):
            select_grants(task, run_context(), [], rules=world.rules, now=naive)
        with pytest.raises(InvalidValueError, match="timezone-aware"):
            issue_grant(
                task,
                request(task.task, world.home / "Photos"),
                scope=GrantScope.TASK,
                origin=GrantOrigin.PROMPT,
                grant_id=GrantId.new(),
                rules=world.rules,
                now=NOW,
                expires_at=naive,
            )

    @pytest.mark.parametrize("offset", [dt.timedelta(0), -MINUTE])
    def test_an_expiry_that_is_not_in_the_future_is_refused_when_issuing(
        self, world: World, task: TaskState, offset: dt.timedelta
    ) -> None:
        result = issue_grant(
            task,
            request(task.task, world.home / "Photos"),
            scope=GrantScope.TASK,
            origin=GrantOrigin.PROMPT,
            grant_id=GrantId.new(),
            rules=world.rules,
            now=NOW,
            expires_at=NOW + offset,
        )

        assert refusal_of(result) == BadExpiry()


class TestTaint:
    """I12: no task is ever both fs-tainted and net-tainted; taint is sticky."""

    def test_issuing_a_file_grant_taints_the_task_even_if_it_is_never_used(
        self, world: World, task: TaskState
    ) -> None:
        _, tainted = issued(world, task, world.home / "Photos")

        assert tainted.taint == FS
        assert task.taint == frozenset()  # the argument is not modified

    @pytest.mark.parametrize("scope", [GrantScope.ONCE, GrantScope.TASK, GrantScope.TOOL])
    @pytest.mark.parametrize("origin", list(GrantOrigin))
    def test_any_scope_and_the_picker_taint(
        self, world: World, task: TaskState, scope: GrantScope, origin: GrantOrigin
    ) -> None:
        _, tainted = issued(
            world,
            task,
            world.home / "Photos",
            scope=scope,
            origin=origin,
            run=tool_run(ToolId.new()),
        )

        assert tainted.taint == FS

    def test_issuing_the_network_taints_with_net(self, task: TaskState, world: World) -> None:
        grant, tainted = issued(world, task, None, capability=NETWORK)

        assert grant.capability == NetFull()
        assert tainted.taint == NET

    def test_a_net_request_in_an_fs_tainted_task_is_denied_not_asked(
        self, world: World, task: TaskState
    ) -> None:
        _, tainted = issued(world, task, world.home / "Photos")

        decision = decide(
            tainted, [], request(tainted.task, None, capability=NETWORK), rules=world.rules, now=NOW
        )

        assert refusal_of(decision) == TaintConflict(held=TaintKind.FS, wanted=TaintKind.NET)
        assert "fresh_context=true" in describe(
            TaintConflict(held=TaintKind.FS, wanted=TaintKind.NET)
        )

    def test_and_a_file_request_in_a_net_tainted_task(self, world: World, task: TaskState) -> None:
        _, tainted = issued(world, task, None, capability=NETWORK)

        decision = decide(
            tainted,
            [],
            request(tainted.task, world.home / "Photos"),
            rules=world.rules,
            now=NOW,
        )

        assert refusal_of(decision) == TaintConflict(held=TaintKind.NET, wanted=TaintKind.FS)

    def test_issuing_the_conflicting_grant_is_refused_in_either_order(
        self, world: World, task: TaskState
    ) -> None:
        _, fs_tainted = issued(world, task, world.home / "Photos")
        _, net_tainted = issued(world, task, None, capability=NETWORK)

        net_after_fs = issue_grant(
            fs_tainted,
            request(task.task, None, capability=NETWORK),
            scope=GrantScope.TASK,
            origin=GrantOrigin.PROMPT,
            grant_id=GrantId.new(),
            rules=world.rules,
            now=NOW,
        )
        fs_after_net = issue_grant(
            net_tainted,
            request(task.task, world.home / "Documents"),
            scope=GrantScope.ONCE,
            origin=GrantOrigin.PICKER,
            grant_id=GrantId.new(),
            rules=world.rules,
            now=NOW,
        )

        assert refusal_of(net_after_fs) == TaintConflict(held=TaintKind.FS, wanted=TaintKind.NET)
        assert refusal_of(fs_after_net) == TaintConflict(held=TaintKind.NET, wanted=TaintKind.FS)

    def test_the_same_kind_can_be_issued_again(self, world: World, task: TaskState) -> None:
        _, once = issued(world, task, world.home / "Photos")
        _, twice = issued(world, once, world.home / "Documents")

        assert twice.taint == FS

    def test_taint_outlives_the_grant_that_set_it(self, world: World, task: TaskState) -> None:
        """The grant expires, is revoked, is gone from the list: the task is still fs-tainted."""
        grant, tainted = issued(world, task, world.home / "Photos", expires_at=NOW + MINUTE)
        later = NOW + 10 * MINUTE
        net_asking = request(tainted.task, None, capability=NETWORK)

        with_expired = decide(tainted, [grant], net_asking, rules=world.rules, now=later)
        revoked = decide(tainted, [], net_asking, rules=world.rules, now=later)

        expected = TaintConflict(held=TaintKind.FS, wanted=TaintKind.NET)
        assert refusal_of(with_expired) == expected
        assert refusal_of(revoked) == expected

    def test_a_spent_once_grant_leaves_its_taint(self, world: World, task: TaskState) -> None:
        run = run_context()
        grant, tainted = issued(world, task, world.home / "Photos", scope=GrantScope.ONCE, run=run)
        assert isinstance(grant, OnceGrant)
        spent = grant.consume(run.run)

        decision = decide(
            tainted,
            [spent],
            request(tainted.task, None, capability=NETWORK, run=run),
            rules=world.rules,
            now=NOW,
        )

        assert isinstance(refusal_of(decision), TaintConflict)

    def test_a_task_that_inherits_turns_inherits_the_taint(
        self, world: World, task: TaskState
    ) -> None:
        _, tainted = issued(world, task, world.home / "Photos")

        inheriting = tainted.end().successor(TaskId.new(), fresh_context=False)
        fresh = tainted.end().successor(TaskId.new(), fresh_context=True)

        def net_for(state: TaskState) -> Decision:
            return decide(
                state,
                [],
                request(state.task, None, capability=NETWORK),
                rules=world.rules,
                now=NOW,
            )

        assert isinstance(refusal_of(net_for(inheriting)), TaintConflict)
        assert isinstance(net_for(fresh), NeedsPrompt)

    def test_an_allow_never_reports_less_taint_than_the_task_has(
        self, world: World, task: TaskState
    ) -> None:
        grant, tainted = issued(world, task, world.home / "Photos")

        decision = decide(
            tainted,
            [grant],
            request(tainted.task, world.home / "Photos"),
            rules=world.rules,
            now=NOW,
        )

        assert isinstance(decision, Allow)
        assert decision.task_taint == FS


class TestToolGrants:
    """Scope `tool`: bound to (ToolId, content hash), for user-triggered runs only, and applying
    one to a run counts as issuing it."""

    def tool_grant(
        self,
        world: World,
        tool: ToolId,
        *,
        path: Path | None = None,
        capability: CapabilityKind = READ,
        content_hash: ContentHash = HASH_A,
        now: dt.datetime = NOW,
        task: TaskState | None = None,
    ) -> ToolGrant:
        owner = task or TaskState(task=TaskId.new())
        grant, _ = issued(
            world,
            owner,
            path if path is not None or capability is NETWORK else world.home / "Photos",
            scope=GrantScope.TOOL,
            capability=capability,
            run=tool_run(tool, content_hash=content_hash),
            now=now,
        )
        assert isinstance(grant, ToolGrant)
        return grant

    def test_applies_to_a_user_triggered_run_of_that_tool(
        self, world: World, task: TaskState
    ) -> None:
        tool = ToolId.new()
        grant = self.tool_grant(world, tool)

        decision = decide(
            task,
            [grant],
            request(task.task, world.home / "Photos" / "a.jpg", run=tool_run(tool)),
            rules=world.rules,
            now=NOW,
        )

        assert isinstance(decision, Allow)
        assert decision.grant == grant.id
        assert not decision.consumes

    def test_the_grant_binds_the_tool_and_the_exact_version(self, world: World) -> None:
        tool = ToolId.new()

        grant = self.tool_grant(world, tool)

        assert (grant.tool, grant.content_hash) == (tool, HASH_A)
        assert grant.trigger is RunTrigger.USER_EVENT

    @pytest.mark.parametrize(
        "trigger", [RunTrigger.MODEL, RunTrigger.STATE_PATCH, RunTrigger.CONNECTION]
    )
    def test_a_run_not_triggered_by_the_user_gets_task_grants_only(
        self, world: World, task: TaskState, trigger: RunTrigger
    ) -> None:
        tool = ToolId.new()
        grant = self.tool_grant(world, tool)
        run = run_context(trigger, tool=tool)

        selection = select_grants(task, run, [grant], rules=world.rules, now=NOW)
        decision = decide(
            task,
            [grant],
            request(task.task, world.home / "Photos" / "a.jpg", run=run),
            rules=world.rules,
            now=NOW,
        )

        assert [s.reason for s in selection.skipped] == [SkipReason.NOT_USER_TRIGGERED]
        assert selection.task_taint == frozenset()  # withheld, so not applied: no taint either
        assert isinstance(decision, NeedsPrompt)

    def test_a_new_version_must_be_granted_again(self, world: World, task: TaskState) -> None:
        tool = ToolId.new()
        grant = self.tool_grant(world, tool, content_hash=HASH_A)
        run = tool_run(tool, content_hash=HASH_B)

        selection = select_grants(task, run, [grant], rules=world.rules, now=NOW)

        assert [s.reason for s in selection.skipped] == [SkipReason.OTHER_TOOL]

    def test_another_tool_and_a_run_that_is_no_saved_tool_do_not_get_it(
        self, world: World, task: TaskState
    ) -> None:
        grant = self.tool_grant(world, ToolId.new())

        for run in (tool_run(ToolId.new()), run_context(RunTrigger.USER_EVENT)):
            selection = select_grants(task, run, [grant], rules=world.rules, now=NOW)
            assert [s.reason for s in selection.skipped] == [SkipReason.OTHER_TOOL]

    def test_applying_one_counts_as_issuing_it(self, world: World, task: TaskState) -> None:
        """The task becomes fs-tainted by the run starting, whether or not the run uses it."""
        tool = ToolId.new()
        grant = self.tool_grant(world, tool)
        run = tool_run(tool)

        selection = select_grants(task, run, [grant], rules=world.rules, now=NOW)

        assert selection.applicable == (grant,)
        assert selection.task_taint == FS

    def test_an_allow_carries_the_taint_the_caller_must_store(
        self, world: World, task: TaskState
    ) -> None:
        tool = ToolId.new()
        grant = self.tool_grant(world, tool)
        run = tool_run(tool)

        decision = decide(
            task,
            [grant],
            request(task.task, world.home / "Photos" / "a.jpg", run=run),
            rules=world.rules,
            now=NOW,
        )

        assert isinstance(decision, Allow)
        assert decision.task_taint == FS

    def test_and_so_does_the_taint_of_one_the_request_did_not_use(
        self, world: World, task: TaskState
    ) -> None:
        tool = ToolId.new()
        used, _ = issued(world, task, world.home / "Documents", scope=GrantScope.TASK)
        unused = self.tool_grant(world, tool, path=world.home / "Photos")
        run = tool_run(tool)
        task = TaskState(task=task.task)

        decision = decide(
            task,
            [used, unused],
            request(task.task, world.home / "Documents" / "notes.txt", run=run),
            rules=world.rules,
            now=NOW,
        )

        assert isinstance(decision, Allow)
        assert decision.grant == used.id  # a task grant is used before a tool grant
        assert decision.task_taint == FS  # and the tool grant was applied all the same

    def test_a_conflicting_tool_grant_is_withheld_from_the_run(
        self, world: World, task: TaskState
    ) -> None:
        """The task holds net; the tool's file grant would make it both: it is not applied."""
        tool = ToolId.new()
        grant = self.tool_grant(world, tool)
        _, net_tainted = issued(world, task, None, capability=NETWORK)
        run = tool_run(tool)

        selection = select_grants(net_tainted, run, [grant], rules=world.rules, now=NOW)
        decision = decide(
            net_tainted,
            [grant],
            request(net_tainted.task, world.home / "Photos" / "a.jpg", run=run),
            rules=world.rules,
            now=NOW,
        )

        assert selection.applicable == ()
        assert [(s.grant, s.reason) for s in selection.skipped] == [
            (grant.id, SkipReason.TAINT_CONFLICT)
        ]
        assert selection.task_taint == NET
        assert refusal_of(decision) == TaintConflict(held=TaintKind.NET, wanted=TaintKind.FS)

    def test_a_net_tool_grant_is_withheld_from_an_fs_tainted_task(
        self, world: World, task: TaskState
    ) -> None:
        tool = ToolId.new()
        grant = self.tool_grant(world, tool, capability=NETWORK)
        _, fs_tainted = issued(world, task, world.home / "Documents")

        selection = select_grants(fs_tainted, tool_run(tool), [grant], rules=world.rules, now=NOW)

        assert [s.reason for s in selection.skipped] == [SkipReason.TAINT_CONFLICT]

    def test_of_two_conflicting_tool_grants_the_earlier_one_applies(
        self, world: World, task: TaskState
    ) -> None:
        tool = ToolId.new()
        files = self.tool_grant(world, tool, now=NOW)
        network = self.tool_grant(world, tool, capability=NETWORK, now=NOW + MINUTE)
        run = tool_run(tool)

        first = select_grants(task, run, [network, files], rules=world.rules, now=NOW + MINUTE)
        files_late = self.tool_grant(world, tool, now=NOW + 2 * MINUTE)
        second = select_grants(
            task, run, [files_late, network], rules=world.rules, now=NOW + 2 * MINUTE
        )

        assert first.applicable == (files,)
        assert [(s.grant, s.reason) for s in first.skipped] == [
            (network.id, SkipReason.TAINT_CONFLICT)
        ]
        assert first.task_taint == FS
        assert second.applicable == (network,)
        assert [s.grant for s in second.skipped] == [files_late.id]
        assert second.task_taint == NET

    def test_taint_set_by_applying_a_tool_grant_is_sticky(
        self, world: World, task: TaskState
    ) -> None:
        tool = ToolId.new()
        grant = self.tool_grant(world, tool)
        run = tool_run(tool)
        first = decide(
            task,
            [grant],
            request(task.task, world.home / "Photos" / "a.jpg", run=run),
            rules=world.rules,
            now=NOW,
        )
        assert isinstance(first, Allow)
        stored = TaskState(task=task.task, taint=first.task_taint)

        # the grant is revoked, the tool is gone, the run is over: the task is still fs-tainted
        later = decide(
            stored,
            [],
            request(task.task, None, capability=NETWORK),
            rules=world.rules,
            now=NOW + 10 * MINUTE,
        )

        assert refusal_of(later) == TaintConflict(held=TaintKind.FS, wanted=TaintKind.NET)

    def test_the_prompt_offers_the_tool_scope_only_for_a_user_triggered_run_of_a_saved_tool(
        self, world: World, task: TaskState
    ) -> None:
        path = world.home / "Photos"
        tool = ToolId.new()
        offers = {
            name: decide(task, [], request(task.task, path, run=run), rules=world.rules, now=NOW)
            for name, run in {
                "user": tool_run(tool),
                "model": run_context(RunTrigger.MODEL, tool=tool),
                "unsaved": run_context(RunTrigger.USER_EVENT),
            }.items()
        }

        scopes = {name: d.scopes for name, d in offers.items() if isinstance(d, NeedsPrompt)}
        assert scopes == {
            "user": frozenset(GrantScope),
            "model": frozenset({GrantScope.ONCE, GrantScope.TASK}),
            "unsaved": frozenset({GrantScope.ONCE, GrantScope.TASK}),
        }

    def test_a_tool_grant_cannot_be_issued_for_any_other_run(
        self, world: World, task: TaskState
    ) -> None:
        tool = ToolId.new()
        for run in (
            run_context(RunTrigger.MODEL, tool=tool),
            run_context(RunTrigger.STATE_PATCH, tool=tool),
            run_context(RunTrigger.CONNECTION, tool=tool),
            run_context(RunTrigger.USER_EVENT),
        ):
            result = issue_grant(
                task,
                request(task.task, world.home / "Photos", run=run),
                scope=GrantScope.TOOL,
                origin=GrantOrigin.PROMPT,
                grant_id=GrantId.new(),
                rules=world.rules,
                now=NOW,
            )
            assert refusal_of(result) == ScopeUnavailable(scope=GrantScope.TOOL)


class TestTheDecision:
    def test_a_covered_request_is_allowed_with_the_path_below_the_grant(
        self, world: World, task: TaskState
    ) -> None:
        grant, task = issued(world, task, world.home / "Photos")

        nested = decide(
            task,
            [grant],
            request(task.task, world.home / "Photos" / "2026" / "trip" / "a.jpg"),
            rules=world.rules,
            now=NOW,
        )
        itself = decide(
            task, [grant], request(task.task, world.home / "Photos"), rules=world.rules, now=NOW
        )

        assert isinstance(nested, Allow)
        assert nested.within_grant == WorkspaceRelPath("2026/trip/a.jpg")
        assert isinstance(itself, Allow)
        assert itself.within_grant is None

    def test_a_request_beside_the_grant_is_asked_about(self, world: World, task: TaskState) -> None:
        grant, task = issued(world, task, world.home / "Photos")

        for sibling in ("Documents", "Documents/notes.txt", "Projects"):
            decision = decide(
                task,
                [grant],
                request(task.task, world.home / sibling),
                rules=world.rules,
                now=NOW,
            )
            assert isinstance(decision, NeedsPrompt), sibling

    def test_a_name_that_starts_like_the_grant_is_not_inside_it(
        self, world: World, task: TaskState
    ) -> None:
        (world.home / "Photos-old").mkdir()
        grant, task = issued(world, task, world.home / "Photos")

        decision = decide(
            task, [grant], request(task.task, world.home / "Photos-old"), rules=world.rules, now=NOW
        )

        assert isinstance(decision, NeedsPrompt)

    def test_a_directory_replaced_after_the_grant_is_not_covered(
        self, world: World, task: TaskState
    ) -> None:
        grant, task = issued(world, task, world.home / "Photos")
        (world.home / "Photos").rename(world.home / "Photos-moved")
        (world.home / "Photos").mkdir()
        (world.home / "Photos" / "a.jpg").write_text("planted")

        replaced = decide(
            task,
            [grant],
            request(task.task, world.home / "Photos" / "a.jpg"),
            rules=world.rules,
            now=NOW,
        )
        moved = decide(
            task,
            [grant],
            request(task.task, world.home / "Photos-moved" / "a.jpg"),
            rules=world.rules,
            now=NOW,
        )

        assert isinstance(replaced, NeedsPrompt)  # the name is the same, the directory is not
        assert isinstance(moved, Allow)  # the directory is, under another name

    def test_a_symlink_in_a_granted_folder_is_judged_by_where_it_leads(
        self, world: World, task: TaskState
    ) -> None:
        grant, task = issued(world, task, world.home / "Photos")
        (world.home / "Photos" / "to-ssh").symlink_to(world.home / ".ssh")
        (world.home / "Photos" / "to-docs").symlink_to(world.home / "Documents")

        into_protected = decide(
            task,
            [grant],
            request(task.task, world.home / "Photos" / "to-ssh" / "id_rsa"),
            rules=world.rules,
            now=NOW,
        )
        out_of_grant = decide(
            task,
            [grant],
            request(task.task, world.home / "Photos" / "to-docs" / "notes.txt"),
            rules=world.rules,
            now=NOW,
        )

        assert isinstance(refusal_of(into_protected), StaticDeny)
        assert isinstance(out_of_grant, NeedsPrompt)  # not covered by the Photos grant

    def test_a_missing_path_in_a_granted_folder_is_allowed_and_a_missing_path_elsewhere_is_not(
        self, world: World, task: TaskState
    ) -> None:
        grant, task = issued(world, task, world.home / "Photos")

        inside = decide(
            task,
            [grant],
            request(task.task, world.home / "Photos" / "later.jpg"),
            rules=world.rules,
            now=NOW,
        )
        outside = decide(
            task,
            [grant],
            request(task.task, world.home / "Documents" / "later.txt"),
            rules=world.rules,
            now=NOW,
        )

        assert isinstance(inside, Allow)
        assert inside.within_grant == WorkspaceRelPath("later.jpg")
        assert refusal_of(outside) == Ungrantable()

    def test_needs_prompt_carries_exactly_what_was_asked_for_in_structured_form(
        self, world: World, task: TaskState
    ) -> None:
        path = world.home / "Photos" / "a.jpg"

        decision = decide(task, [], request(task.task, path), rules=world.rules, now=NOW)

        assert isinstance(decision, NeedsPrompt)
        assert isinstance(decision.capability, FsRead)
        assert decision.capability.path == StoredPath.record(resolve(path))
        assert decision.scopes == {GrantScope.ONCE, GrantScope.TASK}

    def test_network_needs_a_prompt_too(self, world: World, task: TaskState) -> None:
        decision = decide(
            task, [], request(task.task, None, capability=NETWORK), rules=world.rules, now=NOW
        )

        assert isinstance(decision, NeedsPrompt)
        assert decision.capability == NetFull()

    def test_a_network_grant_covers_a_network_request_only(
        self, world: World, task: TaskState
    ) -> None:
        grant, task = issued(world, task, None, capability=NETWORK)

        net = decide(
            task, [grant], request(task.task, None, capability=NETWORK), rules=world.rules, now=NOW
        )
        files = decide(
            task, [grant], request(task.task, world.home / "Photos"), rules=world.rules, now=NOW
        )

        assert isinstance(net, Allow)
        assert net.within_grant is None
        assert isinstance(refusal_of(files), TaintConflict)

    def test_a_file_grant_covers_no_network_request_and_taints_against_one(
        self, world: World, task: TaskState
    ) -> None:
        """Found in a store beside a clean task state, the file grant still counts: the task
        cannot be given the network on top of it."""
        grant = forged(world.home / "Photos", FsRead, task)

        decision = decide(
            task, [grant], request(task.task, None, capability=NETWORK), rules=world.rules, now=NOW
        )

        assert refusal_of(decision) == TaintConflict(held=TaintKind.FS, wanted=TaintKind.NET)

    @pytest.mark.parametrize("path", ["missing", "fifo"])
    def test_what_cannot_be_recorded_is_not_asked_about(
        self, world: World, task: TaskState, path: str
    ) -> None:
        make_fifo(world.home / "fifo")

        decision = decide(
            task, [], request(task.task, world.home / path), rules=world.rules, now=NOW
        )

        assert refusal_of(decision) == Ungrantable()

    def test_a_request_for_another_task_is_refused(self, world: World, task: TaskState) -> None:
        decision = decide(
            task,
            [],
            request(TaskId.new(), world.home / "Photos"),
            rules=world.rules,
            now=NOW,
        )

        assert refusal_of(decision) == TaskMismatch()

    def test_an_ended_task_is_refused_everything(self, world: World, task: TaskState) -> None:
        grant, task = issued(world, task, world.home / "Photos")

        decision = decide(
            task.end(),
            [grant],
            request(task.task, world.home / "Photos"),
            rules=world.rules,
            now=NOW,
        )

        assert refusal_of(decision) == TaskEnded()

    def test_a_grant_of_another_task_is_not_used(self, world: World) -> None:
        mine, theirs = TaskState(task=TaskId.new()), TaskState(task=TaskId.new())
        grant, _ = issued(world, theirs, world.home / "Photos")

        decision = decide(
            mine, [grant], request(mine.task, world.home / "Photos"), rules=world.rules, now=NOW
        )

        assert isinstance(decision, NeedsPrompt)

    def test_the_decision_does_not_depend_on_the_order_of_the_grants(
        self, world: World, task: TaskState
    ) -> None:
        run = run_context()
        once, task = issued(
            world, task, world.home / "Photos", scope=GrantScope.ONCE, run=run, now=NOW
        )
        wide, task = issued(world, task, world.home / "Photos", now=NOW + MINUTE)
        narrow, task = issued(world, task, world.home / "Photos" / "2026", now=NOW + 2 * MINUTE)
        asking = request(task.task, world.home / "Photos" / "2026" / "x.jpg", run=run)

        decisions = {
            decide(
                task, list(order), asking, rules=world.rules, now=NOW + 3 * MINUTE
            ).model_dump_json()
            for order in itertools.permutations([once, wide, narrow])
        }

        assert len(decisions) == 1
        assert Allow.model_validate_json(decisions.pop()).grant == wide.id

    def test_the_same_inputs_give_the_same_decision(self, world: World, task: TaskState) -> None:
        grant, task = issued(world, task, world.home / "Photos")
        asking = request(task.task, world.home / "Photos" / "a.jpg")

        first = decide(task, [grant], asking, rules=world.rules, now=NOW)
        second = decide(task, [grant], asking, rules=world.rules, now=NOW)

        assert first == second

    def test_deciding_touches_neither_the_file_system_nor_the_inputs(
        self, world: World, task: TaskState, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        grant, tainted = issued(world, task, world.home / "Photos")
        asking = request(tainted.task, world.home / "Photos" / "a.jpg")
        before = (tainted.model_dump_json(), grant.model_dump_json())

        def forbidden(*args: object, **kwargs: object) -> None:
            raise AssertionError("the decision function must be pure")

        for name in ("stat", "lstat", "open", "fstat", "statvfs", "fstatvfs", "listdir", "scandir"):
            monkeypatch.setattr(os, name, forbidden)
        decision = decide(tainted, [grant], asking, rules=world.rules, now=NOW)
        decide(tainted, [], request_for_net(tainted), rules=world.rules, now=NOW)
        select_grants(tainted, run_context(), [grant], rules=world.rules, now=NOW)

        assert isinstance(decision, Allow)
        assert (tainted.model_dump_json(), grant.model_dump_json()) == before

    @pytest.mark.parametrize("kind", ["allow", "needs_prompt", "deny"])
    def test_every_decision_round_trips_through_json(
        self, world: World, task: TaskState, kind: str
    ) -> None:
        grant, tainted = issued(world, task, world.home / "Photos")
        asking = {
            "allow": request(tainted.task, world.home / "Photos"),
            "needs_prompt": request(tainted.task, world.home / "Documents"),
            "deny": request(tainted.task, world.home / ".ssh"),
        }[kind]
        decision = decide(tainted, [grant], asking, rules=world.rules, now=NOW)
        adapter = TypeAdapter(Decision)

        assert decision.kind == kind
        assert adapter.validate_json(adapter.dump_json(decision)) == decision


def request_for_net(task: TaskState) -> AccessRequest:
    return request(task.task, None, capability=NETWORK)


class TestAccessRequest:
    def test_a_file_request_needs_a_path(self, task: TaskState) -> None:
        for capability in (READ, WRITE):
            with pytest.raises(ValidationError, match="needs a path"):
                AccessRequest(task=task.task, run=run_context(), capability=capability)

    def test_a_network_request_has_none(self, task: TaskState, world: World) -> None:
        with pytest.raises(ValidationError, match="no path"):
            AccessRequest(
                task=task.task,
                run=run_context(),
                capability=NETWORK,
                path=resolve(world.home),
            )

    def test_a_resolved_path_cannot_be_parsed_from_text(self, task: TaskState) -> None:
        text = (
            f'{{"task": "{task.task}", "run": {{"run": "{RunId.new()}", "trigger": "model"}}, '
            '"capability": "fs.read", "path": {"canonical": "/tmp", "chain": []}}'
        )

        with pytest.raises(ValidationError):
            AccessRequest.model_validate_json(text)


class TestIssuing:
    def test_a_once_grant_is_bound_to_the_run_that_asked(
        self, world: World, task: TaskState
    ) -> None:
        run = run_context()

        grant, _ = issued(world, task, world.home / "Photos", scope=GrantScope.ONCE, run=run)

        assert isinstance(grant, OnceGrant)
        assert (grant.run, grant.task) == (run.run, task.task)

    def test_a_task_grant_is_bound_to_the_task(self, world: World, task: TaskState) -> None:
        grant, _ = issued(world, task, world.home / "Photos")

        assert isinstance(grant, TaskGrant)
        assert grant.task == task.task

    def test_the_grant_records_when_how_and_until_when(self, world: World, task: TaskState) -> None:
        grant, _ = issued(
            world,
            task,
            world.home / "Photos",
            origin=GrantOrigin.PICKER,
            expires_at=NOW + MINUTE,
        )

        assert (grant.issued_at, grant.expires_at, grant.origin) == (
            NOW,
            NOW + MINUTE,
            GrantOrigin.PICKER,
        )

    def test_what_the_user_picked_is_granted_not_what_the_model_asked(
        self, world: World, task: TaskState
    ) -> None:
        """A picked folder replaces the file asked for: the request is the user's choice."""
        grant, _ = issued(world, task, world.home / "Photos", origin=GrantOrigin.PICKER)

        assert isinstance(grant.capability, FsRead)
        assert grant.capability.path.path == str(world.home / "Photos")

    def test_a_missing_target_cannot_be_granted(self, world: World, task: TaskState) -> None:
        result = issue_grant(
            task,
            request(task.task, world.home / "Photos" / "nothing"),
            scope=GrantScope.TASK,
            origin=GrantOrigin.PROMPT,
            grant_id=GrantId.new(),
            rules=world.rules,
            now=NOW,
        )

        assert refusal_of(result) == Ungrantable()

    def test_an_ended_task_or_another_tasks_request_is_refused(
        self, world: World, task: TaskState
    ) -> None:
        for state, asking_task in ((task.end(), task.task), (task, TaskId.new())):
            result = issue_grant(
                state,
                request(asking_task, world.home / "Photos"),
                scope=GrantScope.TASK,
                origin=GrantOrigin.PROMPT,
                grant_id=GrantId.new(),
                rules=world.rules,
                now=NOW,
            )
            assert isinstance(refusal_of(result), TaskEnded | TaskMismatch)

    def test_the_result_round_trips_through_json(self, world: World, task: TaskState) -> None:
        result = issue_grant(
            task,
            request(task.task, world.home / "Photos"),
            scope=GrantScope.TASK,
            origin=GrantOrigin.PROMPT,
            grant_id=GrantId.new(),
            rules=world.rules,
            now=NOW,
        )
        adapter = TypeAdapter(Issuance)

        assert adapter.validate_json(adapter.dump_json(result)) == result


class TestDescribe:
    REFUSALS = (
        StaticDeny(entry="~/.ssh", relation=DenyRelation.EQUAL),
        StaticDeny(entry="~/.ssh", relation=DenyRelation.INSIDE),
        StaticDeny(entry="~/.ssh", relation=DenyRelation.ANCESTOR),
        WriteGateClosed(),
        TaintConflict(held=TaintKind.FS, wanted=TaintKind.NET),
        TaintConflict(held=TaintKind.NET, wanted=TaintKind.FS),
        TaskEnded(),
        TaskMismatch(),
        Ungrantable(),
        ScopeUnavailable(scope=GrantScope.TOOL),
        BadExpiry(),
    )

    @pytest.mark.parametrize("reason", REFUSALS, ids=lambda r: r.kind)
    def test_is_short_printable_fixed_text(self, reason: object) -> None:
        text = describe(reason)  # type: ignore[arg-type]

        assert text.startswith("refused: ")
        assert text.isprintable()
        assert len(text) < 200

    def test_a_refusal_for_a_path_never_names_the_path(self, world: World, task: TaskState) -> None:
        secret = world.home / ".ssh" / "my-very-private-key-name"
        reason = refusal_of(
            decide(task, [], request(task.task, secret), rules=world.rules, now=NOW)
        )

        text = describe(reason)  # type: ignore[arg-type]

        assert "my-very-private-key-name" not in text
        assert str(world.home) not in text
        assert "~/.ssh" in text  # the rule is named, not the path

    def test_every_refusal_kind_is_covered_above(self) -> None:
        kinds = {reason.kind for reason in self.REFUSALS}

        assert kinds == {
            "static_deny",
            "write_gate",
            "taint_conflict",
            "task_ended",
            "task_mismatch",
            "ungrantable",
            "scope_unavailable",
            "bad_expiry",
        }


class TestTheRunTimeHalf:
    """What S17 does after a decision: open the grant by identity, then read below it."""

    def test_the_whole_flow_from_request_to_bytes(self, world: World, task: TaskState) -> None:
        run = run_context()
        asking = request(task.task, world.home / "Photos" / "a.jpg", run=run)
        first = decide(task, [], asking, rules=world.rules, now=NOW)
        assert isinstance(first, NeedsPrompt)

        # the user answers "allow once"
        issuance = issue_grant(
            task,
            request(task.task, world.home / "Photos", run=run),
            scope=GrantScope.ONCE,
            origin=GrantOrigin.PICKER,
            grant_id=GrantId.new(),
            rules=world.rules,
            now=NOW,
        )
        assert isinstance(issuance, Issued)
        grant, task = issuance.grant, issuance.task
        assert isinstance(grant, OnceGrant)

        second = decide(task, [grant], asking, rules=world.rules, now=NOW)
        assert isinstance(second, Allow)
        assert second.consumes
        assert second.within_grant is not None
        spent = grant.consume(run.run)
        third = decide(task, [spent], asking, rules=world.rules, now=NOW)
        assert isinstance(third, NeedsPrompt)

        assert isinstance(grant.capability, FsRead)
        with open_granted(grant.capability.path, rules=world.rules) as root:
            data = read_workspace_file(root, second.within_grant, max_bytes=ByteCount(100))
        assert data == b"jpeg"

    def test_a_symlink_swapped_in_after_the_grant_refuses_the_run(
        self, world: World, task: TaskState
    ) -> None:
        grant, task = issued(world, task, world.home / "Photos")
        assert isinstance(grant.capability, FsRead)
        decision = decide(
            task,
            [grant],
            request(task.task, world.home / "Photos" / "a.jpg"),
            rules=world.rules,
            now=NOW,
        )
        assert isinstance(decision, Allow)
        (world.home / "Photos").rename(world.home / "Photos-moved")
        (world.home / "Photos").symlink_to(world.home / "Documents")

        with pytest.raises(PathError) as raised:
            open_granted(grant.capability.path, rules=world.rules)

        assert raised.value.fault is PathFault.SYMLINK

    def test_a_grant_that_now_overlaps_the_list_is_not_opened(
        self, world: World, task: TaskState
    ) -> None:
        stored = StoredPath.record(resolve(world.home / "Documents"))
        grown = DenyRules.build(
            DenyContext.model_validate(
                {
                    **dict(world.context),
                    "extra": (ExtraDeny(label="late", path=str(world.home / "Documents" / "x")),),
                }
            )
        )

        with pytest.raises(PathError) as raised:
            open_granted(stored, rules=grown)

        assert raised.value.fault is PathFault.PROTECTED

    def test_the_stored_identity_is_checked(self, world: World) -> None:
        stored = StoredPath.record(resolve(world.home / "Documents"))
        wrong = StoredPath(
            path=stored.path,
            identity=FsIdentity(fsid=stored.identity.fsid, ino=stored.identity.ino + 1),
        )

        with pytest.raises(PathError) as raised:
            open_granted(wrong, rules=world.rules)

        assert raised.value.fault is PathFault.IDENTITY_MISMATCH

    def test_the_part_below_the_grant_is_opened_without_following_symlinks(
        self, world: World, task: TaskState
    ) -> None:
        (world.home / "Photos" / "link").symlink_to(world.home / "Documents")
        grant, _ = issued(world, task, world.home / "Photos")
        assert isinstance(grant.capability, FsRead)

        with (
            open_granted(grant.capability.path, rules=world.rules) as root,
            pytest.raises(PathError) as raised,
        ):
            open_in_workspace(root, WorkspaceRelPath("link/notes.txt"))

        assert raised.value.fault is PathFault.SYMLINK


def test_the_resolved_path_of_a_request_is_the_one_a_grant_records(
    world: World, task: TaskState
) -> None:
    path = world.home / "Photos"
    resolved = ResolvedPath.resolve(str(path))

    grant, _ = issued(world, task, path)

    assert isinstance(grant.capability, FsRead)
    assert grant.capability.path == StoredPath.record(resolved)


def net_task_grant(task: TaskState, *, issued_at: dt.datetime = NOW) -> Grant:
    return TaskGrant(
        id=GrantId.new(),
        capability=NetFull(),
        origin=GrantOrigin.PROMPT,
        issued_at=issued_at,
        task=task.task,
    )


class TestAnInconsistentStore:
    """A store can disagree with the task's state (a lost write, a forged row). I12 must hold
    anyway: nothing here may ever make a task both fs- and net-tainted, and `decide` and
    `issue_grant` must agree on what the task holds."""

    def test_a_clean_task_with_a_file_task_grant_and_a_net_tool_grant_never_gets_both(
        self, world: World, task: TaskState
    ) -> None:
        tool = ToolId.new()
        run = tool_run(tool)
        files = forged(world.home / "Photos", FsRead, task)
        network = ToolGrant(
            id=GrantId.new(),
            capability=NetFull(),
            origin=GrantOrigin.PROMPT,
            issued_at=NOW - MINUTE,  # older: it would win if tool grants went first
            tool=tool,
            content_hash=run.tool.content_hash if run.tool else HASH_A,
        )

        selection = select_grants(task, run, [network, files], rules=world.rules, now=NOW)

        assert selection.applicable == (files,)
        assert [(s.grant, s.reason) for s in selection.skipped] == [
            (network.id, SkipReason.TAINT_CONFLICT)
        ]
        assert selection.task_taint == FS
        decision = decide(
            task,
            [network, files],
            request(task.task, world.home / "Photos" / "a.jpg", run=run),
            rules=world.rules,
            now=NOW,
        )
        assert isinstance(decision, Allow)
        assert decision.task_taint == FS  # healed: the task now holds what the store says

    def test_two_task_grants_that_conflict_the_earlier_one_applies(
        self, world: World, task: TaskState
    ) -> None:
        files = forged(world.home / "Photos", FsRead, task)
        network = net_task_grant(task, issued_at=NOW + MINUTE)

        selection = select_grants(
            task, run_context(), [network, files], rules=world.rules, now=NOW + MINUTE
        )

        assert selection.applicable == (files,)
        assert [(s.grant, s.reason) for s in selection.skipped] == [
            (network.id, SkipReason.TAINT_CONFLICT)
        ]
        assert selection.task_taint == FS

    def test_a_net_request_is_then_refused_not_covered(self, world: World, task: TaskState) -> None:
        files = forged(world.home / "Photos", FsRead, task)
        network = net_task_grant(task, issued_at=NOW + MINUTE)

        decision = decide(
            task,
            [files, network],
            request(task.task, None, capability=NETWORK),
            rules=world.rules,
            now=NOW + MINUTE,
        )

        assert refusal_of(decision) == TaintConflict(held=TaintKind.FS, wanted=TaintKind.NET)

    def test_a_once_or_task_grant_that_conflicts_with_the_task_is_skipped(
        self, world: World, task: TaskState
    ) -> None:
        net_held = TaskState(task=task.task, taint=NET)
        files = forged(world.home / "Photos", FsRead, task)

        selection = select_grants(net_held, run_context(), [files], rules=world.rules, now=NOW)

        assert [s.reason for s in selection.skipped] == [SkipReason.TAINT_CONFLICT]
        assert selection.task_taint == NET

    def test_issuing_counts_the_grants_that_apply_to_the_run(
        self, world: World, task: TaskState
    ) -> None:
        """The state is clean, the store holds a file grant: the network cannot be issued."""
        files = forged(world.home / "Photos", FsRead, task)

        result = issue_grant(
            task,
            request(task.task, None, capability=NETWORK),
            scope=GrantScope.TASK,
            origin=GrantOrigin.PROMPT,
            grant_id=GrantId.new(),
            rules=world.rules,
            now=NOW,
            grants=[files],
        )

        assert refusal_of(result) == TaintConflict(held=TaintKind.FS, wanted=TaintKind.NET)

    def test_issuing_counts_the_tool_grants_that_apply_to_the_run(
        self, world: World, task: TaskState
    ) -> None:
        tool = ToolId.new()
        run = tool_run(tool)
        assert run.tool is not None
        tool_files = ToolGrant(
            id=GrantId.new(),
            capability=FsRead(path=StoredPath.record(resolve(world.home / "Photos"))),
            origin=GrantOrigin.PROMPT,
            issued_at=NOW - MINUTE,
            tool=tool,
            content_hash=run.tool.content_hash,
        )

        refused = issue_grant(
            task,
            request(task.task, None, capability=NETWORK, run=run),
            scope=GrantScope.TASK,
            origin=GrantOrigin.PROMPT,
            grant_id=GrantId.new(),
            rules=world.rules,
            now=NOW,
            grants=[tool_files],
        )
        elsewhere = issue_grant(  # the same grants, for a run the tool grant does not apply to
            task,
            request(task.task, None, capability=NETWORK),
            scope=GrantScope.TASK,
            origin=GrantOrigin.PROMPT,
            grant_id=GrantId.new(),
            rules=world.rules,
            now=NOW,
            grants=[tool_files],
        )

        assert refusal_of(refused) == TaintConflict(held=TaintKind.FS, wanted=TaintKind.NET)
        assert isinstance(elsewhere, Issued)

    def test_what_is_issued_carries_the_taint_of_what_applies(
        self, world: World, task: TaskState
    ) -> None:
        files = forged(world.home / "Photos", FsRead, task)

        result = issue_grant(
            task,
            request(task.task, world.home / "Documents"),
            scope=GrantScope.TASK,
            origin=GrantOrigin.PROMPT,
            grant_id=GrantId.new(),
            rules=world.rules,
            now=NOW,
            grants=[files],
        )

        assert isinstance(result, Issued)
        assert result.task.taint == FS


class TestAnEndedTask:
    def test_its_once_and_task_grants_are_skipped_as_ended(
        self, world: World, task: TaskState
    ) -> None:
        run = run_context()
        once, _ = issued(world, task, world.home / "Photos", scope=GrantScope.ONCE, run=run)
        wide, tainted = issued(world, task, world.home / "Documents")

        selection = select_grants(tainted.end(), run, [once, wide], rules=world.rules, now=NOW)

        assert selection.applicable == ()
        assert {(s.grant, s.reason) for s in selection.skipped} == {
            (once.id, SkipReason.TASK_ENDED),
            (wide.id, SkipReason.TASK_ENDED),
        }

    def test_a_live_task_keeps_them(self, world: World, task: TaskState) -> None:
        wide, tainted = issued(world, task, world.home / "Documents")

        selection = select_grants(tainted, run_context(), [wide], rules=world.rules, now=NOW)

        assert selection.applicable == (wide,)


class TestAllowNamesTheTarget:
    def test_the_identity_the_decision_saw(self, world: World, task: TaskState) -> None:
        grant, task = issued(world, task, world.home / "Photos")

        decision = decide(
            task,
            [grant],
            request(task.task, world.home / "Photos" / "a.jpg"),
            rules=world.rules,
            now=NOW,
        )

        assert isinstance(decision, Allow)
        assert decision.target == resolve(world.home / "Photos" / "a.jpg").identity

    def test_none_for_a_file_that_did_not_exist_and_for_the_network(
        self, world: World, task: TaskState
    ) -> None:
        grant, task = issued(world, task, world.home / "Photos")

        missing = decide(
            task,
            [grant],
            request(task.task, world.home / "Photos" / "later.jpg"),
            rules=world.rules,
            now=NOW,
        )

        assert isinstance(missing, Allow)
        assert missing.target is None

    def test_opening_below_the_grant_with_it_refuses_a_file_swapped_after_the_decision(
        self, world: World, task: TaskState
    ) -> None:
        grant, task = issued(world, task, world.home / "Photos")
        assert isinstance(grant.capability, FsRead)
        decision = decide(
            task,
            [grant],
            request(task.task, world.home / "Photos" / "a.jpg"),
            rules=world.rules,
            now=NOW,
        )
        assert isinstance(decision, Allow)
        assert decision.within_grant is not None
        # replaced by another file of the same name (the old one is renamed, not deleted: the
        # inode number of a deleted file can be handed to the next one)
        (world.home / "Photos" / "a.jpg").rename(world.home / "Photos" / "old.jpg")
        (world.home / "Photos" / "a.jpg").write_text("swapped")

        with open_granted(grant.capability.path, rules=world.rules) as root:
            with pytest.raises(PathError) as raised:
                read_workspace_file(
                    root,
                    decision.within_grant,
                    max_bytes=ByteCount(100),
                    expect=decision.target,
                )
            unchecked = read_workspace_file(root, decision.within_grant, max_bytes=ByteCount(100))

        assert raised.value.fault is PathFault.IDENTITY_MISMATCH
        assert unchecked == b"swapped"  # without `expect` nothing would have noticed
