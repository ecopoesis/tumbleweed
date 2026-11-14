"""Network grants in the broker (S22, D29): when a run has a network, I12 in both orders and
at every stage of the first grant's life, and what a refusal tells the model.

A fake sandbox records the policy it is given; the real sandbox's behaviour is in
`tests/sandbox/test_network.py` and `tests/broker/test_run_network_linux.py`.
"""

from datetime import timedelta

import pytest

from tests.broker.rig import FakeSandbox, Rig, running
from tests.ipc.bounded import run
from tumbleweed.broker import service as service_module
from tumbleweed.broker.audit import GrantDecision, SandboxRun, SandboxRunResult
from tumbleweed.broker.client import BrokerClient, BrokerRefused
from tumbleweed.broker.grants import GrantScope
from tumbleweed.broker.policy import GrantSelection, select_grants
from tumbleweed.broker.prompts import Allowed
from tumbleweed.core.ids import RunId, TaskId
from tumbleweed.sandbox.errors import SandboxNetworkError

COMMAND = ("/usr/bin/python3", "-c", "print(1)")
FRESH = "call start_task with fresh_context=true"


async def new_task(client: BrokerClient) -> TaskId:
    return (await client.start_task()).task


def photos(rig: Rig) -> str:
    return str(rig.home / "Photos")


async def give_files(rig: Rig, client: BrokerClient, task: TaskId, scope: str = "task") -> None:
    rig.prompts.allow(scope)
    assert (await client.request_read(task, RunId.new(), photos(rig), "x")).granted


async def give_network(rig: Rig, client: BrokerClient, task: TaskId, scope: str = "task") -> None:
    rig.prompts.allow(scope)
    assert (await client.request_network(task, RunId.new(), "x")).granted


class TestARunHasANetworkOnlyWithTheGrant:
    def test_a_task_that_holds_net_full_gets_a_network_and_no_files(self) -> None:
        async def scenario() -> None:
            async with running() as rig:
                client = await rig.client()
                task = await new_task(client)
                await give_network(rig, client, task)

                result = await client.run_in_sandbox(task, RunId.new(), COMMAND)

                (policy,) = rig.sandbox.policies
                assert policy.network
                assert policy.grants == ()
                assert policy.inputs == ()
                assert result.network
                assert [entry.network for entry in rig.audit_of(SandboxRun)] == [True]

        run(scenario)

    def test_a_task_without_it_has_none(self) -> None:
        async def scenario() -> None:
            async with running() as rig:
                client = await rig.client()
                task = await new_task(client)

                result = await client.run_in_sandbox(task, RunId.new(), COMMAND)

                assert not rig.sandbox.policies[0].network
                assert not result.network
                assert [entry.network for entry in rig.audit_of(SandboxRun)] == [False]

        run(scenario)

    def test_a_task_with_files_has_none(self) -> None:
        async def scenario() -> None:
            async with running() as rig:
                client = await rig.client()
                task = await new_task(client)
                await give_files(rig, client, task)

                await client.run_in_sandbox(task, RunId.new(), COMMAND)

                assert not rig.sandbox.policies[0].network

        run(scenario)

    def test_a_once_grant_is_a_network_for_that_run_only(self) -> None:
        async def scenario() -> None:
            async with running() as rig:
                client = await rig.client()
                task = await new_task(client)
                run_id = RunId.new()
                rig.prompts.allow("once")
                assert (await client.request_network(task, run_id, "x")).granted

                await client.run_in_sandbox(task, run_id, COMMAND)
                await client.run_in_sandbox(task, RunId.new(), COMMAND)

                assert [p.network for p in rig.sandbox.policies] == [True, False]

        run(scenario)

    def test_an_expired_or_revoked_grant_gives_no_network(self) -> None:
        async def scenario() -> None:
            async with running() as rig:
                client = await rig.client()
                task = await new_task(client)
                rig.prompts.answers.append(
                    Allowed(scope=GrantScope.TASK, expires_at=rig.clock.now + timedelta(minutes=5))
                )
                assert (await client.request_network(task, RunId.new(), "x")).granted
                rig.clock.advance(timedelta(minutes=6))
                await client.run_in_sandbox(task, RunId.new(), COMMAND)
                rig.prompts.allow("task")
                granted = await client.request_network(task, RunId.new(), "x")
                assert granted.grant is not None
                await client.revoke_grant(task, granted.grant)

                await client.run_in_sandbox(task, RunId.new(), COMMAND)

                assert [p.network for p in rig.sandbox.policies] == [False, False]

        run(scenario)

    def test_a_run_with_a_network_and_files_is_refused_even_if_a_selection_held_both(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        async def scenario() -> None:
            async with running() as rig:
                client = await rig.client()
                task = await new_task(client)
                await give_network(rig, client, task)
                # A grant for files that a composition (S25) might have put in the selection:
                # taken from another task, as if it applied here.
                other = await new_task(client)
                await give_files(rig, client, other)
                service = rig.daemon.service
                (files_grant,) = service._live[other].grants  # noqa: SLF001
                real = select_grants

                def both(*args: object, **kwargs: object) -> GrantSelection:
                    selection: GrantSelection = real(*args, **kwargs)  # type: ignore[arg-type]  # a pass-through
                    return GrantSelection.model_validate(
                        {
                            "applicable": (*selection.applicable, files_grant),
                            "skipped": selection.skipped,
                            "task_taint": selection.task_taint,
                        }
                    )

                monkeypatch.setattr(service_module, "select_grants", both)

                with pytest.raises(BrokerRefused) as caught:
                    await client.run_in_sandbox(task, RunId.new(), COMMAND)

                assert caught.value.reason == "denied"
                assert rig.sandbox.policies == []
                (entry,) = [e for e in rig.audit_of(SandboxRun) if e.outcome == "refused"]
                assert entry.reason == "net_with_files"

        run(scenario)


class TestWhenTheNetworkCannotBeSetUp:
    def test_the_run_is_refused_with_fixed_text_and_audited(self) -> None:
        async def scenario() -> None:
            sandbox = FakeSandbox()
            sandbox.fail_with = SandboxNetworkError("slirp4netns said /secret/path")
            async with running(sandbox=sandbox) as rig:
                client = await rig.client()
                task = await new_task(client)
                await give_network(rig, client, task)

                with pytest.raises(BrokerRefused) as caught:
                    await client.run_in_sandbox(task, RunId.new(), COMMAND)

                assert caught.value.reason == "unavailable"
                assert "network could not be set up" in str(caught.value)
                assert "secret" not in str(caught.value)
                (ended,) = rig.audit_of(SandboxRunResult)
                assert (ended.outcome, ended.reason) == ("failed", "network_unavailable")

        run(scenario)


class TestTaintInBothOrders:
    """I12: files then network, network then files; with the first grant live, expired, revoked
    or used up. A refusal is fixed text for the model and the user is never asked."""

    @pytest.mark.parametrize("stage", ["live", "expired", "revoked", "used"])
    def test_a_task_given_files_cannot_be_given_the_network(self, stage: str) -> None:
        async def scenario() -> None:
            async with running() as rig:
                client = await rig.client()
                task = await new_task(client)
                await first_grant(rig, client, task, files=True, stage=stage)
                asked = len(rig.prompts.asked)
                rig.prompts.allow("task")  # would be accepted, if anyone asked

                outcome = await client.request_network(task, RunId.new(), "x")

                assert not outcome.granted
                assert FRESH in outcome.message
                assert len(rig.prompts.asked) == asked  # the user was not asked
                assert rig.audit_of(GrantDecision)[-1].reason == "taint_conflict"
                await client.run_in_sandbox(task, RunId.new(), COMMAND)
                assert not rig.sandbox.policies[-1].network

        run(scenario)

    @pytest.mark.parametrize("stage", ["live", "expired", "revoked", "used"])
    def test_a_task_given_the_network_cannot_be_given_files(self, stage: str) -> None:
        async def scenario() -> None:
            async with running() as rig:
                client = await rig.client()
                task = await new_task(client)
                await first_grant(rig, client, task, files=False, stage=stage)
                asked = len(rig.prompts.asked)
                rig.prompts.allow("task")

                outcome = await client.request_read(task, RunId.new(), photos(rig), "x")

                assert not outcome.granted
                assert FRESH in outcome.message
                assert len(rig.prompts.asked) == asked
                assert rig.audit_of(GrantDecision)[-1].reason == "taint_conflict"
                await client.run_in_sandbox(task, RunId.new(), COMMAND)
                assert not rig.sandbox.policies[-1].grants

        run(scenario)

    def test_a_net_tainted_task_is_refused_a_write_and_a_picker_without_a_prompt(self) -> None:
        async def scenario() -> None:
            async with running() as rig:
                client = await rig.client()
                task = await new_task(client)
                await give_network(rig, client, task)
                asked = len(rig.prompts.asked)
                rig.prompts.allow("task")
                rig.prompts.allow("task")

                write = await client.request_write(task, RunId.new(), photos(rig), "x")
                from tumbleweed.ipc.broker_messages import PickAccess

                pick = await client.request_grant(
                    task, RunId.new(), PickAccess(kind="fs.pick", choose="file"), "x"
                )

                assert not write.granted
                assert not pick.granted
                assert FRESH in write.message
                assert FRESH in pick.message
                assert len(rig.prompts.asked) == asked

        run(scenario)

    def test_a_fresh_task_can_be_given_what_the_old_one_could_not(self) -> None:
        async def scenario() -> None:
            async with running() as rig:
                client = await rig.client()
                task = await new_task(client)
                await give_files(rig, client, task)

                fresh = await client.start_task(previous=task, fresh_context=True)

                assert fresh.taint == ()
                await give_network(rig, client, fresh.task)

        run(scenario)

    def test_a_non_fresh_successor_keeps_the_taint_and_so_the_refusal(self) -> None:
        async def scenario() -> None:
            async with running() as rig:
                client = await rig.client()
                task = await new_task(client)
                await give_network(rig, client, task)
                successor = await client.start_task(previous=task, fresh_context=False)
                assert successor.taint == ("net",)
                rig.prompts.allow("task")

                outcome = await client.request_read(successor.task, RunId.new(), photos(rig), "x")

                assert not outcome.granted
                assert FRESH in outcome.message

        run(scenario)


async def first_grant(
    rig: Rig, client: BrokerClient, task: TaskId, *, files: bool, stage: str
) -> None:
    """Make the task's first grant and bring it to `stage`: live, expired, revoked or used up."""
    run_id = RunId.new()
    if stage == "used":
        rig.prompts.allow("once")
    elif stage == "expired":
        rig.prompts.answers.append(
            Allowed(scope=GrantScope.TASK, expires_at=rig.clock.now + timedelta(minutes=5))
        )
    else:
        rig.prompts.allow("task")
    if files:
        outcome = await client.request_read(task, run_id, photos(rig), "x")
    else:
        outcome = await client.request_network(task, run_id, "x")
    assert outcome.granted, outcome.message
    if stage == "used":
        await client.run_in_sandbox(task, run_id, COMMAND)
    elif stage == "expired":
        rig.clock.advance(timedelta(minutes=6))
    elif stage == "revoked":
        assert outcome.grant is not None
        await client.revoke_grant(task, outcome.grant)
