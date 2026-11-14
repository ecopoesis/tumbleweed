"""A task that was granted the network reaches a server through the real sandbox, and a task that
was not does not (S22 acceptance, over IPC with real bubblewrap and slirp4netns)."""

import base64
import json
import shutil
from pathlib import Path

import pytest

from tests.broker.rig import running
from tests.broker.test_run_linux import development_config, real_runner
from tests.ipc.bounded import run
from tests.sandbox.support import PYTHON
from tests.sandbox.test_network import TRY_CONNECT, external_address, listening
from tumbleweed.core.ids import RunId, TaskId

pytestmark = pytest.mark.linux


@pytest.fixture(autouse=True)
def need_tools() -> None:
    for tool in ("bwrap", "slirp4netns"):
        if shutil.which(tool) is None:
            pytest.skip(f"{tool} is not installed")
    if not Path("/dev/net/tun").exists():
        pytest.skip("/dev/net/tun is not available")


def test_the_network_is_for_the_task_that_was_granted_it_and_for_no_one_else() -> None:
    async def scenario() -> None:
        address = external_address()
        with listening(address) as port:
            targets = json.dumps({"server": [address, port]})
            async with running(runner=real_runner(), config=development_config) as rig:
                client = await rig.client()
                plain = (await client.start_task()).task
                granted = (await client.start_task()).task
                rig.prompts.allow("task")
                assert (await client.request_network(granted, RunId.new(), "x")).granted

                async def attempt(task: TaskId) -> str:
                    result = await client.run_in_sandbox(
                        task,
                        RunId.new(),
                        (PYTHON, "-c", TRY_CONNECT, targets),
                    )
                    assert result.exit_code == 0
                    seen: dict[str, str] = json.loads(base64.b64decode(result.stdout))
                    return seen["server"]

                assert (await attempt(plain)).startswith("failed")
                assert await attempt(granted) == "connected"

    run(scenario, deadline=120)
