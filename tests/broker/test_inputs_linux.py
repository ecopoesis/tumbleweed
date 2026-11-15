"""A consumer's run reads a producer's files and can change none of them (S25 acceptance).

A real broker with the real bubblewrap runner: the producer task's workspace is mounted read-only
at `/inputs/<ConnectionId>` for a run of the consumer's handler (the stored code, through the
shim). The program tries every way of changing what it was shown, and of reaching it from
elsewhere; each must fail, and the files on the host must be as they were. The run's own
workspace is writable as a control, so that a failure here is the mount's and not the sandbox's.
"""

import base64
import dataclasses
import json
import shutil

import pytest

from tests.broker.connect_support import opened, stored_tool
from tests.broker.rig import Rig, running
from tests.broker.test_connection_runs import call, origin
from tests.ipc.bounded import run
from tests.sandbox.support import landlock_available
from tumbleweed.broker.client import BrokerClient
from tumbleweed.broker.config import BrokerConfig
from tumbleweed.core.ids import RunId
from tumbleweed.sandbox.handler_protocol import handler_command
from tumbleweed.sandbox.runner import SandboxRunner

pytestmark = pytest.mark.linux

CODE = """
import errno, json, os

def go(inputs):
    path = inputs["src"]["path"]  # an absolute path: it opens as any file ref of the prompt does
    root = "/".join(path.split("/")[:3])  # /inputs/<ConnectionId>
    found = {}

    def attempt(name, action):
        try:
            action()
        except OSError as error:
            found[name] = errno.errorcode.get(error.errno, str(error.errno))
        else:
            found[name] = "ALLOWED"

    with open("/workspace/mine.txt", "w") as mine:
        mine.write("mine")
    with open(path) as file:
        found["read"] = file.read()
    attempt("write", lambda: open(path, "w").close())
    attempt("append", lambda: open(path, "a").close())
    attempt("create", lambda: open(root + "/new.txt", "w").close())
    attempt("create_in_directory", lambda: open(root + "/out/new.txt", "w").close())
    attempt("mkdir", lambda: os.mkdir(root + "/made"))
    attempt("delete", lambda: os.unlink(path))
    attempt("rename", lambda: os.rename(path, root + "/renamed.txt"))
    attempt("rename_out", lambda: os.rename(path, "/workspace/taken.txt"))
    attempt("chmod", lambda: os.chmod(path, 0o777))
    attempt("truncate", lambda: os.truncate(path, 0))
    attempt("link_out", lambda: os.link(path, "/workspace/linked.txt"))
    attempt("link_in", lambda: os.link("/workspace/mine.txt", root + "/linked.txt"))
    attempt("symlink", lambda: os.symlink("/workspace", root + "/s"))
    attempt("utime", lambda: os.utime(path, (0, 0)))
    found["own_workspace"] = "written"
    return {"status": json.dumps(found, sort_keys=True)}
"""


@pytest.fixture(autouse=True)
def need_bwrap() -> None:
    if shutil.which("bwrap") is None:
        pytest.skip("bubblewrap (bwrap) is not installed")


def development_config(config: BrokerConfig) -> BrokerConfig:
    return dataclasses.replace(config, require_landlock=landlock_available())


async def consume(rig: Rig, client: BrokerClient) -> dict[str, str]:
    maker = await stored_tool(client, "Maker", outputs={"out": "file"})
    taker = await stored_tool(client, "Taker", inputs={"src": "file"}, source=CODE)
    first = (await client.start_task()).task
    producer = (await opened(client, first, maker)).instance
    (rig.workspaces / str(first) / "out").mkdir()
    (rig.workspaces / str(first) / "out" / "a.txt").write_text("the producer's file")
    await client.end_task(first)
    task = (await client.start_task()).task
    consumer = await opened(client, task, taker)
    made = await client.connect_tools(task, producer, "out", consumer.instance, "src")

    result = await client.run_in_sandbox(
        task,
        RunId.new(),
        handler_command(),
        payload=call(value={"path": "out/a.txt"}),
        origin=origin(consumer, "connection", made.connection),
    )

    assert result.exit_code == 0, base64.b64decode(result.stderr).decode(errors="replace")
    line = json.loads(base64.b64decode(result.stdout).splitlines()[-1])
    assert line["ok"] is True, line
    found = json.loads(line["outputs"]["status"])
    assert isinstance(found, dict)
    # What is on the host is exactly what the producer left.
    producer_dir = rig.workspaces / str(first)
    assert sorted(path.name for path in (producer_dir / "out").iterdir()) == ["a.txt"]
    assert (producer_dir / "out" / "a.txt").read_text() == "the producer's file"
    assert not (producer_dir / "new.txt").exists()
    assert (producer_dir / "out" / "a.txt").stat().st_mode & 0o777 != 0o777
    return found


def test_the_consumer_reads_the_producers_files_and_changes_none_of_them() -> None:
    async def scenario() -> None:
        async with running(
            runner=SandboxRunner(use_systemd_scope=False), config=development_config
        ) as rig:
            client = await rig.client()

            found = await consume(rig, client)

            assert found.pop("read") == "the producer's file"
            assert found.pop("own_workspace") == "written"
            across = {name: found.pop(name) for name in ("rename_out", "link_out")}
            assert found == dict.fromkeys(
                (
                    "write",
                    "append",
                    "create",
                    "create_in_directory",
                    "mkdir",
                    "delete",
                    "rename",
                    "chmod",
                    "truncate",
                    "link_in",
                    "symlink",
                    "utime",
                ),
                "EROFS",
            )
            # Out of the mount into the run's own workspace: another file system, so never.
            assert all(code in ("EROFS", "EXDEV", "EPERM") for code in across.values()), across

    run(scenario, deadline=120)
