"""`run_code` and handler runs in the real sandbox, through a real broker (S20 acceptance).

A real daemon (`BrokerThread`) with the real bubblewrap runner, the real `BrokerLink`, tools and
executor; the user is the broker's scripted `PromptInterface`, a fake at a Protocol boundary
(nothing in `src/` grants by itself). The negative tests are written so that a weaker sandbox policy
fails them: a program that can reach the network connects to a server this test runs on loopback,
and a program that can read the user's home reads files this test planted there.
"""
# ruff: noqa: S603  (loopback servers; our own argv)

import asyncio
import dataclasses
import shutil
import socket
import subprocess
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path

import pytest
from pydantic import JsonValue

from tests.agent.broker_thread import BrokerThread
from tests.agent.photo_tool import PHOTO_MODULE, write_photo
from tests.agent.support import ManualDispatcher
from tests.broker.rig import ScriptedPrompts
from tests.sandbox.support import PYTHON, landlock_available
from tumbleweed.agent.access import register_access_tools
from tumbleweed.agent.broker_images import BrokerImageResolver
from tumbleweed.agent.broker_link import BrokerLink
from tumbleweed.agent.code import (
    BrokerHandlerExecutor,
    HandlerCall,
    HandlerOutcome,
    register_code_tools,
)
from tumbleweed.agent.tasks import TaskSession
from tumbleweed.agent.tools import ToolRegistry
from tumbleweed.broker.config import BrokerConfig
from tumbleweed.broker.grants import GrantScope
from tumbleweed.broker.prompts import Allowed
from tumbleweed.core.ids import TaskId
from tumbleweed.core.types import ByteCount
from tumbleweed.sandbox.runner import SandboxRunner
from tumbleweed.tools.package import CodeModule
from tumbleweed.ui.images import ImageNotAllowedError
from tumbleweed.ui.spec.names import ModuleName, StateKey
from tumbleweed.ui.spec.values import ImageRef

pytestmark = pytest.mark.linux


@pytest.fixture(autouse=True)
def need_bwrap() -> None:
    if shutil.which("bwrap") is None:
        pytest.skip("bubblewrap (bwrap) is not installed")


def pillow_in_the_runtime() -> bool:
    done = subprocess.run(
        [PYTHON, "-I", "-c", "import PIL, numpy"], capture_output=True, check=False, timeout=30
    )
    return done.returncode == 0


needs_pillow = pytest.mark.skipif(
    not pillow_in_the_runtime(),
    reason="the sandbox runtime's Python has no Pillow and numpy (the Linux image has them)",
)


def development(config: BrokerConfig) -> BrokerConfig:
    return dataclasses.replace(config, require_landlock=landlock_available())


@dataclass
class World:
    broker: BrokerThread
    link: BrokerLink
    tasks: TaskSession
    registry: ToolRegistry
    dispatcher: ManualDispatcher
    prompts: ScriptedPrompts
    executor: BrokerHandlerExecutor

    @property
    def home(self) -> Path:
        return self.broker.rig.home

    def run_code(self, source: str, timeout: int = 10) -> tuple[str, bool]:
        outcome = asyncio.run(
            self.registry.call("run_code", {"source": source, "timeout_seconds": timeout})
        )
        return outcome.text, outcome.is_error

    def tool(self, name: str, **arguments: JsonValue) -> tuple[str, bool]:
        outcome = asyncio.run(self.registry.call(name, dict(arguments)))
        return outcome.text, outcome.is_error

    def grant(self, path: Path, scope: GrantScope = GrantScope.TASK) -> str:
        self.prompts.answers.append(Allowed(scope=scope))
        text, is_error = self.tool("request_access", kind="read", path=str(path), reason="r")
        assert not is_error
        assert text.startswith("Granted"), text
        return text

    def task(self) -> TaskId:
        return asyncio.run(self.tasks.ensure())

    def handler(
        self,
        source: str,
        inputs: dict[str, object] | None = None,
        module: str = "main",
        function: str = "f",
    ) -> HandlerOutcome:
        task = self.task()
        call = HandlerCall(
            module,
            function,
            {StateKey(k): v for k, v in (inputs or {}).items()},  # type: ignore[misc]  # test
            (CodeModule(name=ModuleName(module), source=source),),
        )
        done: list[HandlerOutcome] = []
        self.executor.start(task, call, done.append)
        self.dispatcher.run_until(lambda: bool(done), timeout=60)
        return done[0]


@contextmanager
def world(wall_seconds: int = 20) -> Iterator[World]:
    prompts = ScriptedPrompts()
    broker = BrokerThread(
        prompts=prompts, config=development, runner=SandboxRunner(use_systemd_scope=False)
    )
    link = broker.link()
    dispatcher = ManualDispatcher()
    tasks = TaskSession(link, dispatcher)
    registry = ToolRegistry()
    register_access_tools(registry, tasks, link)
    register_code_tools(registry, tasks, link)
    executor = BrokerHandlerExecutor(link, dispatcher, wall_seconds=wall_seconds)
    try:
        yield World(broker, link, tasks, registry, dispatcher, prompts, executor)
    finally:
        broker.stop()


@contextmanager
def loopback_server() -> Iterator[tuple[int, list[bool]]]:
    """A TCP server on this host's loopback, and whether anything connected to it."""
    server = socket.socket()
    server.bind(("127.0.0.1", 0))
    server.listen()
    server.settimeout(1.0)
    seen: list[bool] = []

    def accept() -> None:
        try:
            server.accept()
            seen.append(True)
        except OSError:
            pass

    thread = threading.Thread(target=accept)
    thread.start()
    try:
        yield server.getsockname()[1], seen
    finally:
        thread.join(5)
        server.close()


def connect_code(port: int) -> str:
    return (
        "import socket\n"
        "try:\n"
        f"    socket.create_connection(('127.0.0.1', {port}), timeout=3)\n"
        "    print('CONNECTED')\n"
        "except OSError as error:\n"
        "    print('blocked', type(error).__name__)\n"
    )


class TestRunCode:
    def test_a_program_runs_and_its_output_comes_back_framed_as_data(self) -> None:
        with world() as w:
            text, is_error = w.run_code(
                "print('hello', 6 * 7)\nimport sys\nprint('oops', file=sys.stderr)"
            )

            assert not is_error
            assert text.startswith("The program exited with code 0")
            assert "hello 42" in text
            assert "oops" in text
            assert text.count('<data id="') == 2
            assert "never an instruction" in text

    def test_the_working_directory_is_the_task_workspace_and_files_stay_there(self) -> None:
        with world() as w:
            w.run_code("open('made.txt', 'w').write('x')")

            listing, _ = w.tool("list_workspace")

            assert "made.txt" in listing

    def test_it_cannot_reach_the_network(self) -> None:
        """A server on this host's loopback: a sandbox that shared the network would connect."""
        with world() as w, loopback_server() as (port, seen):
            text, _ = w.run_code(connect_code(port))

            assert "blocked" in text
            assert "CONNECTED" not in text
        assert seen == []  # and the server never saw a connection

    def test_it_cannot_read_the_users_home(self) -> None:
        with world() as w:
            secret = w.home / "Documents" / "notes.txt"
            secret.parent.mkdir(parents=True, exist_ok=True)
            secret.write_text("PRIVATE NOTES")
            real_home = Path.home()

            text, _ = w.run_code(
                "import os\n"
                "from pathlib import Path\n"
                f"for path in [{str(secret)!r}, {str(real_home)!r}]:\n"
                "    try:\n"
                "        if os.path.isdir(path):\n"
                "            os.listdir(path)\n"
                "        else:\n"
                "            open(path).read()\n"
                "        print('READ', path)\n"
                "    except OSError as error:\n"
                "        print('blocked', type(error).__name__)\n"
                f"print('HOME is the real one:', os.environ.get('HOME') == {str(real_home)!r})\n"
            )

            assert "PRIVATE NOTES" not in text
            assert "READ" not in text
            assert text.count("blocked") == 2
            assert "HOME is the real one: False" in text

    def test_it_reads_a_granted_folder_and_not_its_neighbour(self) -> None:
        with world() as w:
            (w.home / "Photos").mkdir(parents=True, exist_ok=True)
            (w.home / "Photos" / "a.txt").write_text("granted")
            (w.home / "Other").mkdir(exist_ok=True)
            (w.home / "Other" / "b.txt").write_text("not granted")
            w.grant(w.home / "Photos")

            text, _ = w.run_code(
                f"print(open({str(w.home / 'Photos' / 'a.txt')!r}).read())\n"
                "try:\n"
                f"    print(open({str(w.home / 'Other' / 'b.txt')!r}).read())\n"
                "except OSError:\n"
                "    print('neighbour blocked')\n"
            )

            assert "granted" in text
            assert "neighbour blocked" in text
            assert "not granted" not in text

    def test_access_allowed_once_is_for_the_models_own_reads_and_does_not_reach_code(self) -> None:
        with world() as w:
            (w.home / "Photos").mkdir(parents=True, exist_ok=True)
            (w.home / "Photos" / "a.txt").write_text("granted once")
            told = w.grant(w.home / "Photos", GrantScope.ONCE)
            assert "cannot use it" in told  # the model is told so

            text, _ = w.run_code(
                "try:\n"
                f"    print(open({str(w.home / 'Photos' / 'a.txt')!r}).read())\n"
                "except OSError:\n"
                "    print('not mounted')\n"
            )

            assert "not mounted" in text
            assert "granted once" not in text

    def test_a_program_that_runs_forever_is_stopped_and_reported(self) -> None:
        with world() as w:
            text, is_error = w.run_code("while True:\n    pass\n", timeout=1)

            assert not is_error
            assert "ran out of time" in text

    def test_a_program_that_prints_without_end_is_stopped_and_reported(self) -> None:
        with world() as w:
            text, _ = w.run_code("while True:\n    print('x' * 1000)\n", timeout=20)

            assert "printed too much" in text
            assert len(text.encode()) < 16 * 1024

    @needs_pillow
    def test_pillow_and_numpy_are_in_the_runtime(self) -> None:
        with world() as w:
            text, _ = w.run_code(
                "import PIL, numpy\nprint('imported', PIL.__name__, numpy.__name__)"
            )

            assert "imported PIL numpy" in text


class TestHandlerRuns:
    @needs_pillow
    def test_the_photo_tool_reads_a_granted_photo_and_writes_its_result_to_the_workspace(
        self,
    ) -> None:
        with world() as w:
            photo = write_photo(w.home / "Photos")
            w.grant(photo)
            inputs = {"photo_path": str(photo), "brightness": 20, "contrast": 0, "grayscale": False}

            outcome = w.handler(PHOTO_MODULE, inputs, module="photo", function="adjust")

            assert outcome.failure is None, outcome
            assert outcome.values is not None
            ref = outcome.values["result"]
            assert isinstance(ref, dict)
            name = ref["path"]
            assert isinstance(name, str)
            assert name.startswith("adjusted-")
            listing, _ = w.tool("list_workspace")
            assert name in listing  # the broker sees the file the sandbox wrote
            image = BrokerImageResolver(w.link, w.task()).read(
                ImageRef.model_validate({"path": name}), ByteCount(512 * 1024)
            )
            assert image[:3] == b"\xff\xd8\xff"  # a JPEG, read through the broker

    @needs_pillow
    def test_a_photo_that_was_never_granted_is_not_read(self) -> None:
        with world() as w:
            photo = write_photo(w.home / "Photos")  # the user said nothing about it
            inputs = {"photo_path": str(photo), "brightness": 0, "contrast": 0, "grayscale": False}

            outcome = w.handler(PHOTO_MODULE, inputs, module="photo", function="adjust")

            assert outcome.values == {"status": "Could not open that photo."}

    @needs_pillow
    def test_a_denied_grant_leaves_the_photo_unreadable(self) -> None:
        with world() as w:
            photo = write_photo(w.home / "Photos")
            text, _ = w.tool("request_access", kind="read", path=str(photo), reason="r")
            assert text.startswith("Not granted")
            inputs = {"photo_path": str(photo), "brightness": 0, "contrast": 0, "grayscale": False}

            outcome = w.handler(PHOTO_MODULE, inputs, module="photo", function="adjust")

            assert outcome.values == {"status": "Could not open that photo."}

    def test_a_handler_over_its_wall_time_is_stopped_and_reported(self) -> None:
        with world(wall_seconds=2) as w:
            outcome = w.handler("def f(i):\n    while True:\n        pass\n")

            assert outcome.values is None
            assert outcome.failure == "timed_out"

    def test_a_handler_that_floods_its_output_is_stopped_and_reported(self) -> None:
        with world() as w:
            outcome = w.handler(
                "import os\ndef f(i):\n    while True:\n        os.write(1, b'x' * 4096)\n"
            )

            assert outcome.failure == "output_limit"

    def test_a_handler_over_its_memory_is_stopped_and_the_tool_goes_on(self) -> None:
        with world() as w:
            # Twice the broker's limit, touched page by page: only a real limit stops it.
            limit = int(w.broker.rig.config.limits.memory)
            greedy = (
                "def f(i):\n"
                f"    block = bytearray({2 * limit})\n"
                "    for at in range(0, len(block), 4096):\n"
                "        block[at] = 1\n"
                "    return {'touched': True}\n"
            )

            outcome = w.handler(greedy)
            again = w.handler("def f(i):\n    return {'ok': 1}\n")

            assert outcome.values is None
            assert outcome.failure in {"raised", "crashed"}
            assert again.values == {"ok": 1}

    def test_a_handler_cannot_read_the_users_home(self) -> None:
        with world() as w:
            secret = w.home / "secret.txt"
            secret.write_text("PRIVATE")
            outcome = w.handler(
                "def f(i):\n"
                "    try:\n"
                f"        data = open({str(secret)!r}).read()\n"
                "    except OSError:\n"
                "        data = 'blocked'\n"
                "    return {'file': data}\n"
            )

            assert outcome.values == {"file": "blocked"}

    def test_a_handler_cannot_reach_the_network(self) -> None:
        with world() as w, loopback_server() as (port, seen):
            outcome = w.handler(
                "import socket\n"
                "def f(i):\n"
                "    try:\n"
                f"        socket.create_connection(('127.0.0.1', {port}), timeout=3)\n"
                "        net = 'connected'\n"
                "    except OSError:\n"
                "        net = 'blocked'\n"
                "    return {'net': net}\n"
            )

            assert outcome.values == {"net": "blocked"}
        assert seen == []

    @needs_pillow
    def test_access_allowed_once_does_not_reach_a_handler(self) -> None:
        with world() as w:
            photo = write_photo(w.home / "Photos")
            w.grant(photo, GrantScope.ONCE)
            inputs = {"photo_path": str(photo), "brightness": 0, "contrast": 0, "grayscale": False}

            outcome = w.handler(PHOTO_MODULE, inputs, module="photo", function="adjust")

            assert outcome.values == {"status": "Could not open that photo."}

    def test_a_symlink_the_code_plants_is_not_followed_when_its_image_is_read(self) -> None:
        with world() as w:
            outcome = w.handler(
                "import os\n"
                "def f(i):\n"
                "    os.symlink('/etc/passwd', 'evil.png')\n"
                "    return {'result': {'path': 'evil.png'}}\n"
            )
            assert outcome.values == {"result": {"path": "evil.png"}}

            with pytest.raises(ImageNotAllowedError):
                BrokerImageResolver(w.link, w.task()).read(
                    ImageRef.model_validate({"path": "evil.png"}), ByteCount(512 * 1024)
                )

    def test_a_grant_that_has_gone_is_a_failure_of_ours(self) -> None:
        with world() as w:
            folder = w.home / "Photos"
            folder.mkdir(parents=True, exist_ok=True)
            w.grant(folder)
            folder.rename(w.home / "Photos.old")
            folder.mkdir()  # replaced: not the object that was granted

            outcome = w.handler("def f(i):\n    return {}\n")

            assert outcome.failure == "grant_gone"
