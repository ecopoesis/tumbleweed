"""A run's network (S22, D29): slirp4netns attached to the sandbox's own network namespace.

Every sandbox has a network namespace of its own (`--unshare-net`, never `--share-net`). A run
whose policy has `network` set gets a user-mode network in it, and the program is held back until
the network is up:

1. bwrap starts with `--info-fd` and `--block-fd`. It makes its namespaces, sets the sandbox up
   and then waits on the block pipe before it execs the launcher's helper. The helper
   (`tumbleweed-sandbox-exec --go-fd`) waits for the byte `1` on a pipe of its own before it execs
   the program. bwrap's wait ends on end-of-file too; the helper's does not, so no failure, crash
   or kill of the launcher can start the program: **only a written `1` does**.
2. The launcher reads the child's PID and namespace ids from the info pipe and opens the child's
   network namespace once. It **refuses unless that namespace is the one bwrap reported and not
   the launcher's own**. (With `--share-net` bwrap reports none.) Everything after is given that
   open descriptor, never the PID again.
3. `slirp4netns --configure --disable-host-loopback` joins the user namespace that owns it
   (`_owning_user_namespace`) and the network namespace, makes `tap0`, brings the interface up
   and writes a byte to its ready pipe.
4. The launcher writes the byte that releases bwrap and the helper.

Everything is bounded by `SETUP_SECONDS`, and every failure raises `SandboxNetworkError` with a
fixed message (or `BwrapEnded`, which the runner turns into the launch error bwrap's own words
make): there is no run without the network it was told to have, and none with the host's.
`NetworkLink.abort` closes the gates (end-of-file, never go), kills the held init through a pidfd
and ends slirp4netns; the launcher calls `aclose` on every path out of a run.

This module and `runner` are the only users of slirp4netns; the argument list for bwrap is built
by `tumbleweed.sandbox.bwrap` from `NetworkFds`.
"""

import asyncio
import contextlib
import fcntl
import logging
import os
import select
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
"""slirp4netns's resolver; it forwards to the host's (port 53 only)."""
RESOLV_CONF: Final = f"nameserver {RESOLVER_ADDRESS}\n".encode("ascii")
TAP_NAME: Final = "tap0"
SLIRP_NOFILE: Final = 1024
"""Open files slirp4netns may hold: a connection of the program is one, and the program has 256."""
SLIRP_ADDRESS_SPACE: Final = 1 << 30
"""Address space slirp4netns may use (1 GiB): bounds what a program's traffic can make it hold."""
_NS_GET_USERNS: Final = 0xB701
_FAILED: Final = "the sandbox's network could not be set up; the program did not run"


class BwrapEnded(Exception):  # noqa: N818  (a fact about the process, not a failure of ours)
    """bwrap ended before the network was set up: its own words are the reason (the runner reads
    them), not the network's."""


@final
@dataclass(frozen=True, slots=True)
class NetworkFds:
    """The descriptor numbers bwrap and the helper are told about, all the child's ends: the pipe
    bwrap waits on (`--block-fd`), the one it writes its namespace ids to (`--info-fd`) and the
    one the helper waits on for the go byte."""

    block: int
    info: int
    go: int


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
    """slirp4netns's command line: attached to the network namespace of `pid` (joining the user
    namespace open as `userns_fd`, the one that owns it), host loopback unreachable, its own
    mount namespace and seccomp filter, ready and exit pipes by number. (Its `--netns-type=path`
    cannot be used: with `--enable-sandbox` it fails in `setegid(0)`, measured.)"""
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


def refuse_unless_own_namespace(reported: int | None, opened: int, launcher: int) -> None:
    """Raise unless the child's network namespace (`opened`, the inode of the descriptor the
    launcher holds) is the one bwrap reported and is not the launcher's own (`launcher`)."""
    if reported is None or reported != opened or opened == launcher:
        raise SandboxNetworkError(_FAILED)  # never attach to, or release, anyone else's


def still_that_namespace(pidfd: int, pid: int, opened: int) -> None:
    """Just before slirp4netns is given the PID: the process is still alive (the pidfd is not
    readable, so the number is not reaped and cannot have been reused) and its network
    namespace is still the one open as `opened`. The rest of the window (slirp4netns's own open of
    `/proc/PID`) fails closed: it first joins the owner of the namespace checked above, and the
    network namespace of any other process is owned by another user namespace, which `setns`
    refuses (EPERM)."""
    poller = select.poll()
    poller.register(pidfd, select.POLLIN)
    if poller.poll(0):
        raise SandboxNetworkError(_FAILED)  # the init ended: the number may name anyone
    if Path(f"/proc/{pid}/ns/net").stat().st_ino != os.fstat(opened).st_ino:
        raise SandboxNetworkError(_FAILED)


def _own_network_namespace() -> int:
    return Path("/proc/self/ns/net").stat().st_ino


@final
class NetworkLink:
    """The pipes and the slirp4netns process of one run. Create it before bwrap starts, pass
    `fds` to the child, call `release_child_ends` once bwrap has been started (or failed to
    start), `attach` once it has, and `aclose` on every path out."""

    def __init__(self, slirp4netns: str, *, prlimit: str | None = None) -> None:
        self._program = slirp4netns
        self._prlimit = prlimit
        self._block_read, self._block_write = os.pipe()
        self._go_read, self._go_write = os.pipe()
        self._info_read, self._info_write = os.pipe()
        self._child_ends_open = True
        self._gates_open = True
        self._info_read_open = True
        self._process: asyncio.subprocess.Process | None = None
        self._exit_write: int | None = None
        self._drain: asyncio.Task[None] | None = None
        self._pidfd: int | None = None
        self.stderr = bytearray()

    @property
    def fds(self) -> NetworkFds:
        return NetworkFds(block=self._block_read, info=self._info_write, go=self._go_read)

    def release_child_ends(self) -> None:
        """Close the ends only bwrap and the helper need (idempotent). Until the parent has, a
        bwrap that dies would not make the info pipe end."""
        if self._child_ends_open:
            self._child_ends_open = False
            os.close(self._block_read)
            os.close(self._info_write)
            os.close(self._go_read)

    async def attach(self, bwrap: asyncio.subprocess.Process) -> None:
        """Bring the network up and release the program; `SandboxNetworkError` if that cannot be
        done in `SETUP_SECONDS`, `BwrapEnded` if bwrap ended first. `bwrap` must already have been
        started with `fds`."""
        net = -1
        pidfd = -1
        try:
            async with asyncio.timeout(SETUP_SECONDS):
                info = await self._read_info(bwrap)
                pidfd = os.pidfd_open(info.child_pid)  # type: ignore[attr-defined,unused-ignore]  # Linux only: sandboxes never run elsewhere
                net = os.open(f"/proc/{info.child_pid}/ns/net", os.O_RDONLY | os.O_CLOEXEC)
                refuse_unless_own_namespace(
                    info.net_namespace, os.fstat(net).st_ino, _own_network_namespace()
                )
                # Only now is the pidfd ours to kill with: a number that was reused (refused
                # above) never gets a signal from us.
                self._pidfd, pidfd = pidfd, -1
                owner = await _owning_user_namespace(net, info.child_pid, bwrap)
                try:
                    still_that_namespace(self._pidfd, info.child_pid, net)
                    await self._start_slirp(info.child_pid, owner)
                finally:
                    os.close(owner)
                self._release()
        except TimeoutError:
            raise SandboxNetworkError(_FAILED) from None
        except OSError as exc:
            log.warning("the sandbox's network setup failed: %s", exc.strerror)
            raise SandboxNetworkError(_FAILED) from None
        finally:
            for descriptor in (net, pidfd):
                if descriptor >= 0:
                    os.close(descriptor)

    def abort(self) -> None:
        """End a setup that did not finish, now: close the gates (end-of-file is never "go": the
        helper refuses to run the program) and kill the held init through the pidfd, which names
        that very process however long ago its number was reported. Idempotent."""
        self._close_gates()
        if self._pidfd is not None:
            with contextlib.suppress(ProcessLookupError, PermissionError):
                signal.pidfd_send_signal(  # type: ignore[attr-defined,unused-ignore]  # Linux only
                    self._pidfd, signal.SIGKILL
                )

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
                    return _Info.model_validate_json(bytes(data))
                except ValidationError as error:
                    if all(item["type"] == "json_invalid" for item in error.errors()):
                        continue  # not all of it yet, or never JSON: more may come, to the limit
                    break
        finally:
            transport.close()
        with contextlib.suppress(TimeoutError):
            async with asyncio.timeout(1.0):
                await bwrap.wait()
        if bwrap.returncode is not None:
            raise BwrapEnded
        raise SandboxNetworkError(_FAILED)

    async def _start_slirp(self, pid: int, owner: int) -> None:
        ready_read, ready_write = os.pipe()
        exit_read, exit_write = os.pipe()
        self._exit_write = exit_write
        limits = (
            []
            if self._prlimit is None
            else [self._prlimit, f"--nofile={SLIRP_NOFILE}", f"--as={SLIRP_ADDRESS_SPACE}", "--"]
        )
        try:
            self._process = await asyncio.create_subprocess_exec(
                *limits,
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
        """Write the go byte to both gates (the helper's is the one that counts), then close."""
        if not self._gates_open:
            return
        self._gates_open = False
        try:
            for end in (self._block_write, self._go_write):
                os.write(end, b"1")
        except OSError:  # bwrap or the helper is gone: there is nothing to release
            raise SandboxNetworkError(_FAILED) from None
        finally:
            os.close(self._block_write)
            os.close(self._go_write)

    def _close_gates(self) -> None:
        if self._gates_open:
            self._gates_open = False
            os.close(self._block_write)
            os.close(self._go_write)

    async def aclose(self) -> None:
        """Stop slirp4netns and close every descriptor still held. Safe to call twice, on every
        path: a failure is logged, never raised over the run's own outcome."""
        self.release_child_ends()
        self.abort()
        if self._pidfd is not None:
            os.close(self._pidfd)
            self._pidfd = None
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


async def _owning_user_namespace(net: int, pid: int, bwrap: asyncio.subprocess.Process) -> int:
    """An open descriptor of the user namespace that owns the network namespace open as `net`.

    `--disable-userns` gives the sandbox's init a second user namespace of its own, nested in the
    one bwrap made (which owns the network namespace). A process that joins the init's, as
    slirp4netns does by default, has no right over the network namespace (`setns` says EPERM,
    for good). Joining the owner has. The init is waited for until it has moved into its own
    mapped namespace: before that, slirp4netns's own sandbox fails in `setegid(0)` (that wait
    matters in the container; on a quiet host the init is already there). Raises `BwrapEnded` if
    bwrap ends meanwhile. Bounded by the setup's timeout."""
    owner = fcntl.ioctl(net, _NS_GET_USERNS)
    try:
        while not _settled(pid, owner):
            if bwrap.returncode is not None:
                raise BwrapEnded
            await asyncio.sleep(0.002)
    except BaseException:
        os.close(owner)
        raise
    return int(owner)


def _settled(pid: int, owner: int) -> bool:
    """Whether `pid` is in a user namespace other than `owner` and its maps are written."""
    try:
        own = Path(f"/proc/self/fd/{owner}").readlink()
        theirs = Path(f"/proc/{pid}/ns/user").readlink()
        maps = [Path(f"/proc/{pid}/{name}").read_text() for name in ("uid_map", "gid_map")]
    except OSError:
        return False
    return own != theirs and all(text.strip() for text in maps)
