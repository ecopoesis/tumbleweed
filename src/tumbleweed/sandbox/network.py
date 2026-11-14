"""A run's network (S22, D29): slirp4netns attached to the sandbox's own network namespace.

Every sandbox has a network namespace of its own (`--unshare-net`, never `--share-net`). A run
whose policy has `network` set gets a user-mode network in it, and the program is held back until
the network is up:

1. bwrap starts with `--info-fd` and `--block-fd`. It makes its namespaces, sets the sandbox up
   and then waits for a byte on the block pipe before it execs the program, so the program cannot
   have started.
2. The launcher reads the child's PID and namespace ids from the info pipe, and **refuses if the
   child's network namespace is its own**: a sandbox that shared the host's network would be
   attached to by nothing and reached by everything.
3. `slirp4netns --configure --disable-host-loopback` joins the owner of the child's network
   namespace (see `_owning_user_namespace`) and that namespace, makes `tap0`,
   brings the interface up and writes a byte to its ready pipe.
4. The launcher writes the byte that releases bwrap. No sleep, no race.

Everything is bounded by `SETUP_SECONDS`, and every failure raises `SandboxNetworkError` with a
fixed message, never an attachment that is partly there: there is no run without the network it
was told to have, and none with the host's. `NetworkLink.aclose` kills and reaps slirp4netns, and
the launcher calls it on every path out of a run.

This module and `runner` are the only users of slirp4netns; the argument list for bwrap is built
by `tumbleweed.sandbox.bwrap` from `NetworkFds`.
"""

import asyncio
import contextlib
import fcntl
import json
import logging
import os
import signal
from dataclasses import dataclass
from pathlib import Path
from typing import Final, final

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from tumbleweed.sandbox.errors import SandboxNetworkError
from tumbleweed.sandbox.process import kill_and_reap

log = logging.getLogger(__name__)

SETUP_SECONDS: Final = 10.0
"""The whole of the setup: the info document, slirp4netns's tap and the release."""
INFO_LIMIT: Final = 4096
STDERR_LIMIT: Final = 4096
RESOLVER_ADDRESS: Final = "10.0.2.3"
"""slirp4netns's resolver; it forwards to the host's."""
RESOLV_CONF: Final = f"nameserver {RESOLVER_ADDRESS}\n".encode("ascii")
TAP_NAME: Final = "tap0"
_FAILED: Final = "the sandbox's network could not be set up; the program did not run"


@final
@dataclass(frozen=True, slots=True)
class NetworkFds:
    """The two descriptor numbers bwrap is told about: the pipe it waits on (`--block-fd`) and the
    one it writes its namespace ids to (`--info-fd`). Both are the child's ends."""

    block: int
    info: int


@final
class _Info(BaseModel):
    """What bwrap writes to `--info-fd` (many more keys than these; ignoring them is the
    documented reason this is not `extra="forbid"`)."""

    model_config = ConfigDict(strict=True, extra="ignore", frozen=True, populate_by_name=True)

    child_pid: int = Field(alias="child-pid", gt=0)
    net_namespace: int | None = Field(default=None, alias="net-namespace", gt=0)
    """Absent when bwrap did not unshare the network: then it is refused."""


def slirp_argv(
    program: str, pid: int, *, ready: int, exit_: int, userns_fd: int | None = None
) -> list[str]:
    """slirp4netns's command line: attached to `pid`'s network namespace (joining the user
    namespace open as `userns_fd`, the one that owns it), host loopback unreachable, its own
    mount namespace and seccomp filter, ready and exit pipes by number."""
    return [
        program,
        *([] if userns_fd is None else [f"--userns-path=/proc/self/fd/{userns_fd}"]),
        "--configure",
        "--disable-host-loopback",
        "--enable-sandbox",
        "--enable-seccomp",
        "--ready-fd",
        str(ready),
        "--exit-fd",
        str(exit_),
        str(pid),
        TAP_NAME,
    ]


def _own_network_namespace() -> int:
    return Path("/proc/self/ns/net").stat().st_ino


@final
class NetworkLink:
    """The pipes and the slirp4netns process of one run. Create it before bwrap starts, pass
    `fds` to the child, call `release_child_ends` once bwrap has been started (or failed to
    start), `attach` once it has, and `aclose` on every path out."""

    def __init__(self, slirp4netns: str) -> None:
        self._program = slirp4netns
        self._block_read, self._block_write = os.pipe()
        self._info_read, self._info_write = os.pipe()
        self._child_ends_open = True
        self._block_write_open = True
        self._info_read_open = True
        self._process: asyncio.subprocess.Process | None = None
        self._exit_write: int | None = None
        self._drain: asyncio.Task[None] | None = None
        self.stderr = bytearray()
        self._child_pid: int | None = None

    @property
    def fds(self) -> NetworkFds:
        return NetworkFds(block=self._block_read, info=self._info_write)

    def release_child_ends(self) -> None:
        """Close the ends only bwrap needs (idempotent). Until the parent has, a bwrap that dies
        would not make the info pipe end."""
        if self._child_ends_open:
            self._child_ends_open = False
            os.close(self._block_read)
            os.close(self._info_write)

    async def attach(self, bwrap: asyncio.subprocess.Process) -> None:
        """Bring the network up and release bwrap; `SandboxNetworkError` if that cannot be done
        in `SETUP_SECONDS`. `bwrap` must already have been started with `fds`."""
        try:
            async with asyncio.timeout(SETUP_SECONDS):
                info = await self._read_info(bwrap)
                self._child_pid = info.child_pid
                if info.net_namespace in (None, _own_network_namespace()):
                    raise SandboxNetworkError(_FAILED)  # never attach to, or release, the host's
                await self._start_slirp(info.child_pid)
                self._release()
        except TimeoutError:
            raise SandboxNetworkError(_FAILED) from None
        except OSError as exc:
            log.warning("the sandbox's network setup failed: %s", exc.strerror)
            raise SandboxNetworkError(_FAILED) from None

    def kill_sandbox(self, bwrap: asyncio.subprocess.Process) -> None:
        """Kill bwrap's whole process group and the sandbox's init, which is held on the block
        pipe and does not yet die with its parent: a failed setup must end it for certain, and
        before the block pipe closes (closing it would release it, like a byte does)."""
        with contextlib.suppress(ProcessLookupError, PermissionError):
            os.killpg(bwrap.pid, signal.SIGKILL)  # bwrap's own session (`--new-session`)
        if self._child_pid is not None:
            with contextlib.suppress(ProcessLookupError, PermissionError):
                os.kill(self._child_pid, signal.SIGKILL)

    async def _read_info(self, bwrap: asyncio.subprocess.Process) -> _Info:
        loop = asyncio.get_running_loop()
        reader = asyncio.StreamReader()
        file = os.fdopen(self._info_read, "rb", buffering=0)
        self._info_read_open = False  # `file` owns it now
        transport, _ = await loop.connect_read_pipe(
            lambda: asyncio.StreamReaderProtocol(reader), file
        )
        try:
            data = bytearray()
            while len(data) < INFO_LIMIT:
                chunk = await reader.read(INFO_LIMIT - len(data))
                if not chunk:
                    break
                data += chunk
                try:
                    return _Info.model_validate(json.loads(data))
                except json.JSONDecodeError:
                    continue  # not all of it yet
                except ValidationError:
                    break
        finally:
            transport.close()
        if bwrap.returncode is not None:
            log.warning("bwrap ended before the sandbox's network was set up")
        raise SandboxNetworkError(_FAILED)

    async def _start_slirp(self, pid: int) -> None:
        owner = await _owning_user_namespace(pid)
        ready_read, ready_write = os.pipe()
        exit_read, exit_write = os.pipe()
        self._exit_write = exit_write
        try:
            self._process = await asyncio.create_subprocess_exec(
                *slirp_argv(
                    self._program, pid, ready=ready_write, exit_=exit_read, userns_fd=owner
                ),
                stdin=asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.PIPE,
                env={},
                cwd="/",
                pass_fds=[ready_write, exit_read, owner],
            )
        except BaseException:
            os.close(ready_read)
            raise
        finally:
            os.close(ready_write)
            os.close(exit_read)
            os.close(owner)
        assert self._process.stderr is not None  # noqa: S101  (PIPE was asked for)
        self._drain = asyncio.ensure_future(_capped(self._process.stderr, self.stderr))
        with os.fdopen(ready_read, "rb", buffering=0) as ready:
            loop = asyncio.get_running_loop()
            reader = asyncio.StreamReader()
            transport, _ = await loop.connect_read_pipe(
                lambda: asyncio.StreamReaderProtocol(reader), ready
            )
            try:
                answer = await reader.read(1)
            finally:
                transport.close()
        if answer != b"1":
            await asyncio.sleep(0.05)  # let the drain see what it said as it ended
            log.warning("slirp4netns did not become ready: %s", _printable(self.stderr))
            raise SandboxNetworkError(_FAILED)  # it ended without saying it was ready

    def _release(self) -> None:
        if not self._block_write_open:
            return
        self._block_write_open = False
        try:
            os.write(self._block_write, b"1")
        except OSError:  # bwrap is gone: there is nothing to release
            raise SandboxNetworkError(_FAILED) from None
        finally:
            os.close(self._block_write)

    async def aclose(self) -> None:
        """Stop slirp4netns and close every descriptor still held. Safe to call twice, on every
        path: a failure is logged, never raised over the run's own outcome."""
        self.release_child_ends()
        if self._block_write_open:
            self._block_write_open = False  # bwrap, if it is still waiting, sees the end
            os.close(self._block_write)
        if self._info_read_open:
            self._info_read_open = False
            os.close(self._info_read)
        if self._exit_write is not None:
            os.close(self._exit_write)  # slirp4netns's own signal to end
            self._exit_write = None
        process = self._process
        if process is not None and not await asyncio.shield(kill_and_reap(process)):
            log.error("slirp4netns (pid %s) did not end after SIGKILL", process.pid)
        if self._drain is not None:
            self._drain.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._drain


async def _capped(stream: asyncio.StreamReader, into: bytearray) -> None:
    """Read a pipe to its end and keep the first `STDERR_LIMIT` bytes (a full pipe would stop
    slirp4netns)."""
    while chunk := await stream.read(4096):
        into += chunk[: max(0, STDERR_LIMIT - len(into))]


def _printable(data: bytearray) -> str:
    return bytes(data).decode("utf-8", errors="replace").encode("unicode_escape").decode("ascii")


async def _owning_user_namespace(pid: int) -> int:
    """An open descriptor of the user namespace that owns `pid`'s network namespace.

    `--disable-userns` gives the sandbox's init a second user namespace of its own, nested in the
    one bwrap made (which owns the network namespace). A process that joins the init's, as
    slirp4netns does by default, has no right over the network namespace (`setns` says EPERM,
    for good). Joining the owner has. The init is waited for until it has moved into its own,
    mapped namespace: before that, slirp4netns's own sandbox fails in `setegid(0)`. Bounded by the
    setup's timeout."""
    net = os.open(f"/proc/{pid}/ns/net", os.O_RDONLY | os.O_CLOEXEC)
    try:
        owner = fcntl.ioctl(net, _NS_GET_USERNS)
        try:
            while True:
                if _settled(pid, owner):
                    return owner
                await asyncio.sleep(0.002)
        except BaseException:
            os.close(owner)
            raise
    finally:
        os.close(net)


_NS_GET_USERNS: Final = 0xB701


def _settled(pid: int, owner: int) -> bool:
    """Whether `pid` is in a user namespace other than `owner` and its maps are written."""
    try:
        own = Path(f"/proc/self/fd/{owner}").readlink()
        theirs = Path(f"/proc/{pid}/ns/user").readlink()
        maps = [Path(f"/proc/{pid}/{name}").read_text() for name in ("uid_map", "gid_map")]
    except OSError:
        return False
    return own != theirs and all(text.strip() for text in maps)
