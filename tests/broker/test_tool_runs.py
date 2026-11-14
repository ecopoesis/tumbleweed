"""A stored tool's run and its tool grants over IPC (S24, D30 (2), (3), (8)): the negative tests.

What is proved: the code that reaches the sandbox is the store's; a binding the broker did not
make is refused; a tool grant applies to a user-triggered run of exactly that tool and version and
to nothing else; a new version, a model-triggered run and modified code never get it; revoking
works, is audited and takes effect for the next run.
"""

from collections.abc import Sequence
from datetime import timedelta

import pytest

from tests.broker.rig import Rig, running
from tests.broker.stored import (
    SHELL_CODE,
    call_document,
    draft_text,
    origin_of,
    run_tool,
    save,
    sent_payload,
)
from tests.ipc.bounded import run
from tests.tools.support import SOURCE, draft
from tumbleweed.broker.audit import FileAccess, GrantDecision, GrantRevoked, SandboxRun
from tumbleweed.broker.client import BrokerClient, BrokerRefused
from tumbleweed.broker.grants import GrantScope
from tumbleweed.broker.prompts import Allowed
from tumbleweed.core.ids import GrantId, RunId, TaskId, ToolId, ToolInstanceId
from tumbleweed.core.types import ToolVersion
from tumbleweed.ipc.broker_messages import (
    MODEL_ORIGIN,
    ListDir,
    NetworkAccess,
    PickAccess,
    ReadAccess,
    ReadFile,
    Refused,
    RequestGrant,
    RunOrigin,
    WriteAccess,
)
from tumbleweed.ipc.messages import Response
from tumbleweed.ipc.tool_messages import ToolOpened
from tumbleweed.sandbox.handler_protocol import handler_command


async def opened_tool(
    client: BrokerClient, *, tool: ToolId | None = None, version: ToolVersion | None = None
) -> tuple[TaskId, ToolOpened]:
    """A tool saved from one task and opened into another (so there is a binding to use)."""
    saver = (await client.start_task()).task
    saved_tool = tool or (await save(client, saver)).summary.tool
    task = (await client.start_task()).task
    return task, await client.open_tool(task, saved_tool, version)


def mounted(rig: Rig) -> Sequence[str]:
    return [str(grant.target) for grant in rig.sandbox.policies[-1].grants]


async def grant_for_tool(
    rig: Rig, client: BrokerClient, task: TaskId, opened: ToolOpened, folder: str = "Photos"
) -> GrantId:
    """The user's own action: pick a folder for this tool, answering at the `tool` scope."""
    rig.prompts.answers.append(Allowed(scope=GrantScope.TOOL, picked=str(rig.home / folder)))
    outcome = await client.request_grant(
        task,
        RunId.new(),
        PickAccess(kind="fs.pick", choose="folder"),
        "You chose to give this tool access.",
        origin_of(opened),
    )
    assert outcome.granted, outcome.message
    assert outcome.scope == "tool"
    assert outcome.grant is not None
    return outcome.grant


class TestTheCodeIsTheStores:
    def test_the_sandbox_gets_the_stored_modules_and_the_shell_supplies_none(self) -> None:
        async def scenario() -> None:
            async with running() as rig:
                client = await rig.client()
                task, opened = await opened_tool(client)

                await run_tool(client, task, origin_of(opened))

                payload = sent_payload(rig)
                assert payload["code"] == {"csv_view": SOURCE}
                assert payload["module"] == "csv_view"
                assert payload["function"] == "load"
                assert payload["inputs"] == {
                    "csv_path": "/x.csv",
                    "delimiter": ",",
                    "has_header": True,
                }
                assert rig.sandbox.policies[-1].command == handler_command()

        run(scenario)

    @pytest.mark.parametrize("trigger", ["user_event", "model"])
    def test_code_the_shell_supplies_is_refused_and_nothing_runs(self, trigger: str) -> None:
        async def scenario() -> None:
            async with running() as rig:
                client = await rig.client()
                task, opened = await opened_tool(client)

                with pytest.raises(BrokerRefused) as caught:
                    await run_tool(
                        client,
                        task,
                        origin_of(opened, trigger=trigger),
                        payload=call_document(code={"csv_view": SHELL_CODE}),
                    )

                assert caught.value.reason == "invalid_request"
                assert rig.sandbox.policies == []
                (entry,) = rig.audit_of(SandboxRun)
                assert entry.outcome == "refused"
                assert entry.reason == "bound_code_supplied"

        run(scenario)

    def test_a_handler_the_stored_spec_does_not_declare_is_refused(self) -> None:
        async def scenario() -> None:
            async with running() as rig:
                client = await rig.client()
                task, opened = await opened_tool(client)

                with pytest.raises(BrokerRefused) as caught:
                    await run_tool(
                        client, task, origin_of(opened), payload=call_document(function="steal")
                    )

                assert caught.value.reason == "invalid_request"
                assert rig.sandbox.policies == []
                assert rig.audit_of(SandboxRun)[0].reason == "bound_no_such_handler"

        run(scenario)

    @pytest.mark.parametrize(
        ("command", "payload"),
        [
            (("python3", "-c", "print(1)"), None),  # not the shim: an arbitrary program
            (None, "this is not a handler document"),
            (None, "[]"),
        ],
    )
    def test_a_run_that_is_not_the_shims_handler_call_is_refused(
        self, command: tuple[str, ...] | None, payload: str | None
    ) -> None:
        async def scenario() -> None:
            async with running() as rig:
                client = await rig.client()
                task, opened = await opened_tool(client)

                with pytest.raises(BrokerRefused) as caught:
                    await run_tool(
                        client, task, origin_of(opened), payload=payload, command=command
                    )

                assert caught.value.reason == "invalid_request"
                assert rig.sandbox.policies == []

        run(scenario)

    def test_a_run_without_a_binding_is_what_it_was_and_gets_no_tool_grant(self) -> None:
        async def scenario() -> None:
            async with running() as rig:
                client = await rig.client()
                task, opened = await opened_tool(client)
                await grant_for_tool(rig, client, task, opened)

                await client.run_in_sandbox(
                    task, RunId.new(), ("python3", "-c", "1"), payload="print(1)"
                )

                assert sent_payload_text(rig) == "print(1)"
                assert mounted(rig) == []

        run(scenario)


def sent_payload_text(rig: Rig) -> str:
    payload = rig.sandbox.policies[-1].payload
    assert payload is not None
    return payload.decode("utf-8")


class TestAForgedBindingIsRefused:
    @pytest.mark.parametrize("what", ["instance", "tool", "hash", "stranger", "all"])
    def test_a_binding_the_task_did_not_open_is_refused_before_any_grant_is_looked_at(
        self, what: str
    ) -> None:
        async def scenario() -> None:
            async with running() as rig:
                client = await rig.client()
                task, opened = await opened_tool(client)
                _, other = await opened_tool(client)  # another tool, opened in another task
                await grant_for_tool(rig, client, task, opened)
                forged = {
                    "instance": origin_of(opened, instance=ToolInstanceId.new()),
                    "tool": origin_of(opened, tool=other.summary.tool),
                    "hash": origin_of(opened, content_hash=other.summary.hash),
                    "stranger": origin_of(other),  # a real instance, of a task that is not this one
                    "all": origin_of(
                        opened,
                        tool=ToolId.new(),
                        instance=ToolInstanceId.new(),
                        content_hash="0" * 64,
                    ),
                }[what]

                with pytest.raises(BrokerRefused) as caught:
                    await run_tool(client, task, forged)

                assert caught.value.reason == "invalid_request"
                assert rig.sandbox.policies == []
                assert rig.audit_of(SandboxRun)[-1].reason == "bad_binding"

        run(scenario)

    def test_a_forged_binding_gets_no_prompt_no_read_and_no_listing(self) -> None:
        async def scenario() -> None:
            async with running() as rig:
                client = await rig.client()
                task, opened = await opened_tool(client)
                await grant_for_tool(rig, client, task, opened)
                forged = origin_of(opened, instance=ToolInstanceId.new())
                asked = len(rig.prompts.asked)
                folder, photo = str(rig.home / "Photos"), str(rig.home / "Photos" / "a.jpg")

                answers = [
                    await client.connection.call(
                        RequestGrant(
                            kind="request_grant",
                            task=task,
                            run=RunId.new(),
                            access=PickAccess(kind="fs.pick", choose="file"),
                            reason="x",
                            origin=forged,
                        ),
                        Response,
                    ),
                    await client.connection.call(
                        ReadFile(
                            kind="read_file", task=task, run=RunId.new(), path=photo, origin=forged
                        ),
                        Response,
                    ),
                    await client.connection.call(
                        ListDir(
                            kind="list_dir", task=task, run=RunId.new(), path=folder, origin=forged
                        ),
                        Response,
                    ),
                ]

                assert all(
                    isinstance(a, Refused) and a.reason == "invalid_request" for a in answers
                )
                assert len(rig.prompts.asked) == asked

        run(scenario)

    def test_another_connections_binding_is_not_a_task_of_this_one(self) -> None:
        async def scenario() -> None:
            async with running() as rig:
                mine = await rig.client()
                theirs = await rig.client()
                task, opened = await opened_tool(mine)

                with pytest.raises(BrokerRefused) as caught:
                    await run_tool(theirs, task, origin_of(opened))

                assert caught.value.reason == "unknown_task"
                assert rig.sandbox.policies == []

        run(scenario)

    def test_a_user_event_with_no_binding_is_not_a_message(self) -> None:
        with pytest.raises(ValueError, match="names the saved tool"):
            RunOrigin.model_validate({"trigger": "user_event", "binding": None})


class TestToolGrants:
    def test_the_prompt_offers_the_tool_scope_names_the_tool_and_the_grant_applies_to_a_user_run(
        self,
    ) -> None:
        async def scenario() -> None:
            async with running() as rig:
                client = await rig.client()
                task, opened = await opened_tool(client)

                grant = await grant_for_tool(rig, client, task, opened)
                await run_tool(client, task, origin_of(opened))

                prompt = rig.prompts.asked[-1]
                assert GrantScope.TOOL in prompt.scopes
                assert prompt.tool == opened.summary.tool
                assert prompt.tool_name == opened.summary.name
                assert mounted(rig) == [str(rig.home / "Photos")]
                (decision,) = rig.audit_of(GrantDecision)
                assert decision.scope == "tool"
                assert decision.grant == grant

        run(scenario)

    def test_a_model_triggered_run_of_the_same_instance_gets_no_tool_grant(self) -> None:
        async def scenario() -> None:
            async with running() as rig:
                client = await rig.client()
                task, opened = await opened_tool(client)
                await grant_for_tool(rig, client, task, opened)

                await run_tool(client, task, origin_of(opened, trigger="model"))
                assert mounted(rig) == []
                await run_tool(client, task, MODEL_ORIGIN, payload="print(1)", command=("python3",))
                assert mounted(rig) == []

        run(scenario)

    def test_a_request_that_is_the_models_never_offers_the_tool_scope(self) -> None:
        async def scenario() -> None:
            async with running() as rig:
                client = await rig.client()
                task, opened = await opened_tool(client)
                rig.prompts.answers.append(
                    Allowed(scope=GrantScope.TOOL, picked=str(rig.home / "Photos"))
                )

                outcome = await client.request_grant(
                    task, RunId.new(), PickAccess(kind="fs.pick", choose="folder"), "x"
                )
                bound_model = await client.request_grant(
                    task,
                    RunId.new(),
                    PickAccess(kind="fs.pick", choose="folder"),
                    "x",
                    origin_of(opened, trigger="model"),
                )

                assert not outcome.granted
                assert not bound_model.granted
                assert all(GrantScope.TOOL not in prompt.scopes for prompt in rig.prompts.asked)
                assert await client.list_tool_grants() is not None
                assert (await client.list_tool_grants()).grants == ()

        run(scenario)

    def test_a_new_version_must_be_granted_again(self) -> None:
        async def scenario() -> None:
            async with running() as rig:
                client = await rig.client()
                task, first = await opened_tool(client)
                await grant_for_tool(rig, client, task, first)
                saver = (await client.start_task()).task
                await client.save_tool(
                    saver, draft_text(draft(name="Refined")), tool=first.summary.tool
                )
                second_task = (await client.start_task()).task
                second = await client.open_tool(second_task, first.summary.tool)

                await run_tool(client, second_task, origin_of(second))
                assert second.summary.version == 2
                assert mounted(rig) == []  # v1's grant does not reach v2
                await run_tool(client, task, origin_of(first))
                assert mounted(rig) == [str(rig.home / "Photos")]  # and v1 still has it
                await grant_for_tool(rig, client, second_task, second)
                await run_tool(client, second_task, origin_of(second))
                assert mounted(rig) == [str(rig.home / "Photos")]

        run(scenario)

    def test_the_grant_outlives_the_task_and_serves_the_next_task_that_opens_the_version(
        self,
    ) -> None:
        async def scenario() -> None:
            async with running() as rig:
                client = await rig.client()
                task, opened = await opened_tool(client)
                await grant_for_tool(rig, client, task, opened)
                await client.end_task(task)
                later = (await client.start_task(fresh_context=True)).task
                again = await client.open_tool(later, opened.summary.tool)

                await run_tool(client, later, origin_of(again))

                assert mounted(rig) == [str(rig.home / "Photos")]

        run(scenario)

    def test_the_list_shows_the_grant_and_the_task_revoke_does_not_reach_it(self) -> None:
        async def scenario() -> None:
            async with running() as rig:
                client = await rig.client()
                task, opened = await opened_tool(client)
                grant = await grant_for_tool(rig, client, task, opened)

                (info,) = (await client.list_tool_grants()).grants

                assert info.grant == grant
                assert info.tool == opened.summary.tool
                assert info.tool_name == opened.summary.name
                assert info.version == 1
                assert info.newest
                assert info.access == "fs.read"
                assert info.path == str(rig.home / "Photos")
                with pytest.raises(BrokerRefused) as caught:
                    await client.revoke_grant(task, grant)  # a task's revoke is not for tool grants
                assert caught.value.reason == "not_found"
                assert len((await client.list_tool_grants()).grants) == 1

        run(scenario)

    def test_revoke_takes_effect_for_the_next_run_and_is_audited(self) -> None:
        async def scenario() -> None:
            async with running() as rig:
                client = await rig.client()
                task, opened = await opened_tool(client)
                grant = await grant_for_tool(rig, client, task, opened)
                await run_tool(client, task, origin_of(opened))
                assert mounted(rig) == [str(rig.home / "Photos")]

                await client.revoke_tool_grant(grant)
                await run_tool(client, task, origin_of(opened))

                assert mounted(rig) == []
                assert (await client.list_tool_grants()).grants == ()
                (entry,) = rig.audit_of(GrantRevoked)
                assert entry.reason == "revoked_by_user"
                assert entry.task is None
                assert entry.grant == grant
                with pytest.raises(BrokerRefused) as again:
                    await client.revoke_tool_grant(grant)
                assert again.value.reason == "not_found"
                assert len(rig.audit_of(GrantRevoked)) == 1

        run(scenario)

    def test_the_list_says_when_a_grant_is_for_an_older_version(self) -> None:
        async def scenario() -> None:
            async with running() as rig:
                client = await rig.client()
                task, opened = await opened_tool(client)
                await grant_for_tool(rig, client, task, opened)
                await client.save_tool(task, draft_text(draft(name="v2")), tool=opened.summary.tool)

                (info,) = (await client.list_tool_grants()).grants

                assert info.version == 1
                assert not info.newest

        run(scenario)

    def test_a_tool_grant_that_would_mix_taints_is_withheld_from_the_run(self) -> None:
        async def scenario() -> None:
            async with running() as rig:
                client = await rig.client()
                task, opened = await opened_tool(client)
                await grant_for_tool(rig, client, task, opened)
                network = (await client.start_task()).task
                again = await client.open_tool(network, opened.summary.tool)
                rig.prompts.allow("task")
                assert (await client.request_network(network, RunId.new(), "x")).granted

                await run_tool(client, network, origin_of(again))

                assert mounted(rig) == []  # fs was withheld: the task has the network (I12)

        run(scenario)

    def test_a_task_that_a_tool_grant_made_fs_tainted_cannot_then_be_given_the_network(
        self,
    ) -> None:
        async def scenario() -> None:
            async with running() as rig:
                client = await rig.client()
                task, opened = await opened_tool(client)
                await grant_for_tool(rig, client, task, opened)
                asked = len(rig.prompts.asked)

                rig.prompts.allow("task")
                outcome = await client.request_network(task, RunId.new(), "x")

                assert not outcome.granted
                assert len(rig.prompts.asked) == asked  # refused before anyone was asked

        run(scenario)

    def test_the_tool_scope_is_dropped_once_as_many_grants_are_held_as_may_be(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr("tumbleweed.broker.service.MAX_TOOL_GRANTS", 1)

        async def scenario() -> None:
            async with running() as rig:
                client = await rig.client()
                task, opened = await opened_tool(client)
                await grant_for_tool(rig, client, task, opened)
                rig.prompts.answers.append(Allowed(scope=GrantScope.TASK, picked=str(rig.home)))

                await client.request_grant(
                    task,
                    RunId.new(),
                    PickAccess(kind="fs.pick", choose="folder"),
                    "x",
                    origin_of(opened),
                )

                assert GrantScope.TOOL not in rig.prompts.asked[-1].scopes

        run(scenario)

    def test_no_tool_grant_serves_a_read_or_a_listing_whatever_the_origin_says(self) -> None:
        async def scenario() -> None:
            async with running() as rig:
                client = await rig.client()
                task, opened = await opened_tool(client)
                await grant_for_tool(rig, client, task, opened)
                path = str(rig.home / "Photos" / "a.jpg")
                folder = str(rig.home / "Photos")

                for trigger in ("user_event", "model"):
                    origin = origin_of(opened, trigger=trigger)
                    read = await client.connection.call(
                        ReadFile(
                            kind="read_file", task=task, run=RunId.new(), path=path, origin=origin
                        ),
                        Response,
                    )
                    listing = await client.connection.call(
                        ListDir(
                            kind="list_dir", task=task, run=RunId.new(), path=folder, origin=origin
                        ),
                        Response,
                    )
                    assert isinstance(read, Refused)
                    assert isinstance(listing, Refused)
                    assert read.reason == listing.reason == "invalid_request"
                refused = [e for e in rig.audit_of(FileAccess) if e.reason == "bound_read"]
                assert len(refused) == 4
                with pytest.raises(BrokerRefused) as model:
                    await client.read_file(task, RunId.new(), path)  # the model's own read
                assert model.value.reason == "not_granted"

        run(scenario)

    @pytest.mark.parametrize("kind", ["read", "write"])
    def test_a_user_event_request_for_a_path_the_requester_chose_is_refused_unasked(
        self, kind: str
    ) -> None:
        async def scenario() -> None:
            async with running() as rig:
                client = await rig.client()
                task, opened = await opened_tool(client)
                path = str(rig.home / "Photos")
                access = (
                    ReadAccess(kind="fs.read", path=path)
                    if kind == "read"
                    else WriteAccess(kind="fs.write", path=path)
                )
                asked = len(rig.prompts.asked)

                with pytest.raises(BrokerRefused) as caught:
                    await client.request_grant(task, RunId.new(), access, "x", origin_of(opened))

                assert caught.value.reason == "invalid_request"
                assert len(rig.prompts.asked) == asked
                assert rig.audit_of(GrantDecision)[-1].reason == "bound_access"

        run(scenario)

    def test_the_network_is_asked_for_a_tool_at_the_tool_scope(self) -> None:
        async def scenario() -> None:
            async with running() as rig:
                client = await rig.client()
                task, opened = await opened_tool(client)
                rig.prompts.answers.append(Allowed(scope=GrantScope.TOOL))

                outcome = await client.request_grant(
                    task, RunId.new(), NetworkAccess(kind="net.full"), "x", origin_of(opened)
                )

                assert outcome.granted
                assert outcome.scope == "tool"
                assert GrantScope.TOOL in rig.prompts.asked[-1].scopes

        run(scenario)

    def test_an_expired_tool_grant_is_neither_listed_nor_counted(self) -> None:
        async def scenario() -> None:
            async with running() as rig:
                client = await rig.client()
                task, opened = await opened_tool(client)
                rig.prompts.answers.append(
                    Allowed(
                        scope=GrantScope.TOOL,
                        picked=str(rig.home / "Photos"),
                        expires_at=rig.clock.now + timedelta(minutes=5),
                    )
                )
                await client.request_grant(
                    task,
                    RunId.new(),
                    PickAccess(kind="fs.pick", choose="folder"),
                    "x",
                    origin_of(opened),
                )
                assert len((await client.list_tool_grants()).grants) == 1

                rig.clock.advance(timedelta(minutes=6))

                assert (await client.list_tool_grants()).grants == ()
                await run_tool(client, task, origin_of(opened))
                assert mounted(rig) == []

        run(scenario)

    def test_a_binding_for_an_instance_the_task_no_longer_remembers_is_refused(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr("tumbleweed.broker.service.MAX_INSTANCES_PER_TASK", 2)

        async def scenario() -> None:
            async with running() as rig:
                client = await rig.client()
                task, first = await opened_tool(client)
                second = await client.open_tool(task, first.summary.tool)
                third = await client.open_tool(task, first.summary.tool)

                await run_tool(client, task, origin_of(second))
                await run_tool(client, task, origin_of(third))
                with pytest.raises(BrokerRefused) as caught:
                    await run_tool(client, task, origin_of(first))  # the oldest was forgotten

                assert caught.value.reason == "invalid_request"

        run(scenario)


def test_the_audit_keeps_nothing_of_the_inputs_the_shell_sent() -> None:
    async def scenario() -> None:
        async with running() as rig:
            client = await rig.client()
            task, opened = await opened_tool(client)
            await run_tool(client, task, origin_of(opened))

            assert "/x.csv" not in rig.audit_path.read_text(encoding="utf-8")
            assert rig.audit_of(SandboxRun)[-1].program == "python3"

    run(scenario)
