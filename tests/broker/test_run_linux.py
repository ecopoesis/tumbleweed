"""`run_in_sandbox` with the real sandbox: grants are honoured and denials hold (S17 acceptance).

Everything the broker composes is checked with a fake sandbox in `test_daemon_runs.py`; this proves
that what it composes does what it says, in bubblewrap with the seccomp filter and Landlock where
the kernel has it. A program is run that reports what it can see, and what it sees is compared with
what was granted. Where the kernel has no Landlock (Docker Desktop's) the broker is configured the
way a development host is, as S15's tests do, and in CI it is required.
"""

import base64
import dataclasses
import json
import shutil
from datetime import timedelta

import pytest

from tests.broker.rig import Rig, running
from tests.ipc.bounded import run
from tests.sandbox.support import PYTHON, landlock_available
from tumbleweed.broker.audit import SandboxRun
from tumbleweed.broker.client import BrokerClient, BrokerRefused
from tumbleweed.broker.config import BrokerConfig
from tumbleweed.core.ids import RunId, TaskId
from tumbleweed.core.types import WorkspaceRelPath
from tumbleweed.sandbox.runner import SandboxRunner

pytestmark = pytest.mark.linux

SEES = """
import json, os, sys
home = sys.argv[1]
def attempt(path):
    try:
        with open(path, "rb") as file:
            return file.read().decode()
    except OSError as error:
        return "error:" + type(error).__name__
print(json.dumps({
    "photo": attempt(home + "/Photos/a.jpg"),
    "notes": attempt(home + "/Documents/notes.txt"),
    "key": attempt(home + "/.ssh/id_rsa"),
    "environ": sorted(os.environ),
    "cwd": os.getcwd(),
}))
"""


@pytest.fixture(autouse=True)
def need_bwrap() -> None:
    if shutil.which("bwrap") is None:
        pytest.skip("bubblewrap (bwrap) is not installed")


def development_config(config: BrokerConfig) -> BrokerConfig:
    """What a dev host runs: Landlock required wherever the kernel has it."""
    return dataclasses.replace(config, require_landlock=landlock_available())


def real_runner() -> SandboxRunner:
    return SandboxRunner(use_systemd_scope=False)


async def seen_by(
    rig: Rig, client: BrokerClient, task: TaskId, run_id: RunId | None = None
) -> dict[str, object]:
    result = await client.run_in_sandbox(
        task, run_id or RunId.new(), (PYTHON, "-c", SEES, str(rig.home))
    )
    assert result.exit_code == 0, base64.b64decode(result.stderr).decode(errors="replace")
    loaded: object = json.loads(base64.b64decode(result.stdout))
    assert isinstance(loaded, dict)
    return loaded


async def grant(rig: Rig, client: BrokerClient, task: TaskId, relative: str, scope: str) -> RunId:
    run_id = RunId.new()
    rig.prompts.allow(scope)
    outcome = await client.request_read(task, run_id, str(rig.home / relative), "x")
    assert outcome.granted, outcome.message
    return run_id


def test_a_program_sees_what_was_granted_and_nothing_else_of_the_users_files() -> None:
    async def scenario() -> None:
        async with running(runner=real_runner(), config=development_config) as rig:
            client = await rig.client()
            task = (await client.start_task()).task
            (rig.home / ".ssh" / "id_rsa").write_text("PRIVATE KEY")
            await grant(rig, client, task, "Photos", "task")

            seen = await seen_by(rig, client, task)

            assert seen["photo"] == "jpeg"  # granted
            assert seen["notes"] == "error:FileNotFoundError"  # the same home, not granted
            assert seen["key"] == "error:FileNotFoundError"  # a protected place: not even there
            assert seen["cwd"] == "/workspace"

    run(scenario, deadline=120)


def test_a_granted_path_is_read_only_inside_the_sandbox() -> None:
    async def scenario() -> None:
        async with running(runner=real_runner(), config=development_config) as rig:
            client = await rig.client()
            task = (await client.start_task()).task
            await grant(rig, client, task, "Photos", "task")
            code = (
                "import sys\n"
                "try:\n"
                "    open(sys.argv[1] + '/Photos/planted.txt', 'w').write('x')\n"
                "    print('wrote')\n"
                "except OSError as error:\n"
                "    print('refused', type(error).__name__)\n"
            )

            result = await client.run_in_sandbox(
                task, RunId.new(), (PYTHON, "-c", code, str(rig.home))
            )

            assert base64.b64decode(result.stdout).decode().startswith("refused")
            assert not (rig.home / "Photos" / "planted.txt").exists()

    run(scenario, deadline=120)


def test_a_request_the_user_denied_gives_the_program_nothing() -> None:
    async def scenario() -> None:
        async with running(runner=real_runner(), config=development_config) as rig:
            client = await rig.client()
            task = (await client.start_task()).task
            outcome = await client.request_read(task, RunId.new(), str(rig.home / "Photos"), "x")
            assert not outcome.granted

            seen = await seen_by(rig, client, task)

            assert seen["photo"] == "error:FileNotFoundError"

    run(scenario, deadline=120)


def test_a_once_grant_is_there_for_its_run_and_gone_for_the_next() -> None:
    async def scenario() -> None:
        async with running(runner=real_runner(), config=development_config) as rig:
            client = await rig.client()
            task = (await client.start_task()).task
            bound = await grant(rig, client, task, "Photos", "once")

            first = await seen_by(rig, client, task, bound)
            second = await seen_by(rig, client, task)

            assert first["photo"] == "jpeg"
            assert second["photo"] == "error:FileNotFoundError"

    run(scenario, deadline=120)


def test_a_task_that_ended_loses_its_grants_for_the_next_task() -> None:
    async def scenario() -> None:
        async with running(runner=real_runner(), config=development_config) as rig:
            client = await rig.client()
            first = (await client.start_task()).task
            await grant(rig, client, first, "Photos", "task")
            second = (await client.start_task(previous=first, fresh_context=True)).task

            seen = await seen_by(rig, client, second)

            assert seen["photo"] == "error:FileNotFoundError"

    run(scenario, deadline=120)


def test_the_environment_is_the_brokers_whatever_the_brokers_own_is(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-not-for-the-sandbox")
    monkeypatch.setenv("SECRET_TOKEN", "x")

    async def scenario() -> None:
        async with running(runner=real_runner(), config=development_config) as rig:
            client = await rig.client()
            task = (await client.start_task()).task

            seen = await seen_by(rig, client, task)

            names = seen["environ"]
            assert isinstance(names, list)
            fixed = {"HOME", "LANG", "PATH", "PWD", "PYTHONDONTWRITEBYTECODE", "TMPDIR"}
            assert fixed <= set(names) <= fixed | {"LC_CTYPE"}  # nothing of the broker's own
            assert "ANTHROPIC_API_KEY" not in names
            assert "SECRET_TOKEN" not in names

    run(scenario, deadline=120)


def test_what_the_program_writes_is_read_back_from_the_workspace_and_never_followed() -> None:
    async def scenario() -> None:
        async with running(runner=real_runner(), config=development_config) as rig:
            client = await rig.client()
            task = (await client.start_task()).task
            await grant(rig, client, task, "Photos", "task")
            (rig.home / ".ssh" / "id_rsa").write_text("PRIVATE KEY")
            code = (
                "import os, sys\n"
                "open('/workspace/result.txt', 'w').write('42')\n"
                "os.symlink(sys.argv[1] + '/.ssh/id_rsa', '/workspace/innocent.txt')\n"
                "os.symlink(sys.argv[1] + '/Photos', '/workspace/photos')\n"
                "os.mkfifo('/workspace/pipe')\n"
            )

            result = await client.run_in_sandbox(
                task, RunId.new(), (PYTHON, "-c", code, str(rig.home))
            )

            assert result.exit_code == 0
            wrote = await client.read_workspace_file(task, WorkspaceRelPath("result.txt"))
            assert base64.b64decode(wrote.data) == b"42"
            listing = await client.list_workspace(task)
            assert {(e.name, e.entry) for e in listing.entries} == {
                ("result.txt", "file"),
                ("innocent.txt", "symlink"),
                ("photos", "symlink"),
                ("pipe", "other"),
            }
            for planted in ("innocent.txt", "photos/a.jpg", "pipe"):
                with pytest.raises(BrokerRefused) as caught:
                    await client.read_workspace_file(task, WorkspaceRelPath(planted))
                assert caught.value.reason == "unsafe", planted
                assert "PRIVATE" not in str(caught.value)

    run(scenario, deadline=120)


def test_a_program_that_outlives_its_wall_clock_is_stopped_and_the_run_is_reported() -> None:
    async def scenario() -> None:
        async with running(runner=real_runner(), config=development_config) as rig:
            client = await rig.client()
            task = (await client.start_task()).task

            result = await client.run_in_sandbox(
                task,
                RunId.new(),
                (PYTHON, "-c", "import time; time.sleep(60)"),
                wall_seconds=1,
            )

            assert result.termination == "timed_out"
            assert result.exit_code is None

    run(scenario, deadline=120)


def test_output_over_the_limit_stops_the_program() -> None:
    async def scenario() -> None:
        async with running(runner=real_runner(), config=development_config) as rig:
            client = await rig.client()
            task = (await client.start_task()).task

            result = await client.run_in_sandbox(
                task,
                RunId.new(),
                (PYTHON, "-c", "import sys\nwhile True: sys.stdout.write('x' * 65536)"),
                max_output_bytes=1000,
            )

            assert result.termination == "output_limit"
            assert result.stdout_truncated
            assert len(base64.b64decode(result.stdout)) == 1000

    run(scenario, deadline=120)


def test_a_host_without_cgroup_limits_is_refused_on_the_target_and_nothing_runs() -> None:
    """The container and CI have no systemd user manager: with the limits required, as on the
    target, no run starts, and the refusal is recorded."""

    async def scenario() -> None:
        async with running(
            runner=SandboxRunner(use_systemd_scope=False),
            config=development_config,
            require_scope_limits=True,
        ) as rig:
            client = await rig.client()
            task = (await client.start_task()).task

            with pytest.raises(BrokerRefused) as caught:
                await client.run_in_sandbox(task, RunId.new(), (PYTHON, "-c", "print(1)"))

            assert caught.value.reason == "unavailable"
            (refused,) = rig.audit_of(SandboxRun)
            assert (refused.outcome, refused.reason) == ("refused", "limits_unenforced")

    run(scenario, deadline=120)


def test_landlock_is_required_where_the_daemon_says_so() -> None:
    """Required and absent (Docker Desktop's kernel): refused, never run without. Required and
    present (CI): the result says it was applied."""

    async def scenario() -> None:
        async with running(
            runner=real_runner(), config=lambda c: dataclasses.replace(c, require_landlock=True)
        ) as rig:
            client = await rig.client()
            task = (await client.start_task()).task
            call = client.run_in_sandbox(task, RunId.new(), (PYTHON, "-c", "print(1)"))

            if landlock_available():
                assert (await call).landlock == "applied"
            else:
                with pytest.raises(BrokerRefused) as caught:
                    await call
                assert caught.value.reason == "unavailable"

    run(scenario, deadline=120)


def test_the_run_is_over_in_the_time_it_was_given() -> None:
    async def scenario() -> None:
        async with running(runner=real_runner(), config=development_config) as rig:
            client = await rig.client()
            task = (await client.start_task()).task

            result = await client.run_in_sandbox(
                task, RunId.new(), (PYTHON, "-c", "print('quick')"), wall_seconds=30
            )

            assert timedelta(milliseconds=result.wall_milliseconds) < timedelta(seconds=30)
            assert base64.b64decode(result.stdout) == b"quick\n"

    run(scenario, deadline=120)
