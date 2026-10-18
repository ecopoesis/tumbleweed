"""`tumbleweed-tools`: list, show, export and import saved tools through the running broker (S23).

**The CLI is a client of the broker, never of the store.** The tool store is the broker's: one
process writes it, so two writers cannot corrupt it, the audit log has every export and import, and
the broker's checks (size, shape, normal form, hash, the ID rule, the limits) are made in one
place. This program opens no database and no blob: it connects to the broker's socket (same-uid
peers only, like the shell), makes one request and prints the answer. It does not start the
broker; if none is running it says so (`tumbleweed-broker` starts one). Every wait has a bound:
the connection attempt (5 s) and the whole command (60 s).

    tumbleweed-tools list [QUERY]               the newest version of each tool, or those that match
    tumbleweed-tools show TOOL [--version N] [--json]
    tumbleweed-tools export TOOL FILE [--version N]    FILE `-` is standard output
    tumbleweed-tools import FILE                       FILE `-` is standard input

Everything printed that came from a tool (its name, description, tags) is untrusted text: every
character that is not printable (escape sequences, bidirectional overrides, line breaks) is
written as `\\uXXXX`, so a tool cannot move the cursor or forge a line in the terminal. `export`
refuses to write a bundle to a terminal (`-` when stdout is one), because a bundle is raw text;
to a file or a pipe it is the canonical bytes. `show --json` is the canonical bytes to a pipe and,
on a terminal, the same JSON value with every non-printable character escaped. A bundle
file is read as a regular file (never a symlink), at most `MAX_BUNDLE_BYTES`; an export never
overwrites a file and creates it with mode 0600.

A `-` for standard input waits for the user's own input, which is not a peer and has no deadline.

Exit status: 0 on success, 1 if the broker refused or could not be reached or a file could not be
used, 2 for a command line that is not understood.
"""

import argparse
import asyncio
import contextlib
import os
import stat
import sys
from collections.abc import Sequence
from datetime import timedelta
from pathlib import Path
from typing import Final, TextIO

from tumbleweed.broker.client import BrokerClient, BrokerConnector, BrokerRefused
from tumbleweed.broker.daemon import SOCKET_NAME
from tumbleweed.core.errors import InvalidValueError, TumbleweedError
from tumbleweed.core.ids import ToolId
from tumbleweed.core.types import ToolVersion
from tumbleweed.ipc.errors import IpcError
from tumbleweed.ipc.sockdir import SocketDirectory
from tumbleweed.ipc.tool_messages import ToolSummary
from tumbleweed.tools import Port, ToolPackageError, parse_package
from tumbleweed.tools.limits import MAX_BUNDLE_BYTES, MAX_QUERY_CHARS
from tumbleweed.ui.spec.errors import printable

CONNECT_TIMEOUT: Final = timedelta(seconds=5)
COMMAND_TIMEOUT: Final = timedelta(seconds=60)
_READ_FLAGS: Final = os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC | os.O_NONBLOCK
_CREATE_FLAGS: Final = os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC
_DESCRIPTION_COLUMNS: Final = 60


class CliError(TumbleweedError):
    """A failure to report to the user and exit 1 for: a file, the broker, a refusal."""


def _tool_id(text: str) -> ToolId:
    try:
        return ToolId(text)
    except InvalidValueError:
        raise argparse.ArgumentTypeError("not a tool ID (tool_ and 26 characters)") from None


def _version(text: str) -> ToolVersion:
    try:
        return ToolVersion(int(text))
    except (ValueError, InvalidValueError):
        raise argparse.ArgumentTypeError("not a version number (1 or more)") from None


def _query(text: str) -> str:
    if len(text) > MAX_QUERY_CHARS:
        raise argparse.ArgumentTypeError(f"at most {MAX_QUERY_CHARS} characters")
    return text


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="tumbleweed-tools", description="The tools the broker has saved."
    )
    parser.add_argument("--socket-name", default=SOCKET_NAME, help="the broker's socket name")
    commands = parser.add_subparsers(dest="command", required=True)
    listing = commands.add_parser("list", help="list the tools, or search them")
    listing.add_argument(
        "query", nargs="?", type=_query, help="words to look for in name, description, tags"
    )
    show = commands.add_parser("show", help="show one tool")
    show.add_argument("tool", type=_tool_id)
    show.add_argument("--version", type=_version, help="a version (default: the newest)")
    show.add_argument(
        "--json",
        action="store_true",
        help="print the package's canonical JSON (to a terminal it is escaped for the terminal, "
        "which makes it valid JSON of the same value but not the canonical bytes)",
    )
    export = commands.add_parser("export", help="write a tool's bundle to a file")
    export.add_argument("tool", type=_tool_id)
    export.add_argument("file", help="the file to create, or - for standard output")
    export.add_argument("--version", type=_version, help="a version (default: the newest)")
    imported = commands.add_parser("import", help="add the tool in a bundle file")
    imported.add_argument("file", help="the bundle file, or - for standard input")
    return parser


# --- files ------------------------------------------------------------------------------------


def read_bundle(name: str) -> str:
    """The text of a bundle file (`-`: standard input), at most `MAX_BUNDLE_BYTES` of UTF-8."""
    if name == "-":
        data = sys.stdin.buffer.read(MAX_BUNDLE_BYTES + 1)
    else:
        try:
            descriptor = os.open(name, _READ_FLAGS)
        except OSError as exc:
            raise CliError(f"cannot open the bundle file: {exc.strerror}") from None
        try:
            if not stat.S_ISREG(os.fstat(descriptor).st_mode):
                raise CliError("the bundle is not a regular file")
            with os.fdopen(descriptor, "rb", closefd=False) as stream:
                data = stream.read(MAX_BUNDLE_BYTES + 1)
        except OSError as exc:
            raise CliError(f"cannot read the bundle file: {exc.strerror}") from None
        finally:
            os.close(descriptor)
    if len(data) > MAX_BUNDLE_BYTES:
        raise CliError(f"the bundle is over {MAX_BUNDLE_BYTES} bytes")
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError:
        raise CliError("the bundle is not UTF-8 text") from None


def refuse_terminal(stdout: TextIO) -> None:
    """A bundle is the text of a tool, which can hold anything a terminal obeys (C1 controls such
    as the 8-bit forms of OSC 52, bidirectional overrides): it goes to a file or a pipe, never
    to a terminal."""
    if stdout.isatty():
        raise CliError("export to a file or a pipe: a bundle is not safe to print on a terminal")


def write_bundle(name: str, bundle: str, stdout: TextIO) -> None:
    """Write a bundle to a new file (never an existing one, mode 0600), or to standard output."""
    if name == "-":
        refuse_terminal(stdout)
        stdout.write(bundle)
        return
    try:
        descriptor = os.open(name, _CREATE_FLAGS, 0o600)
    except FileExistsError:
        raise CliError("the file exists; export does not overwrite") from None
    except OSError as exc:
        raise CliError(f"cannot create the file: {exc.strerror}") from None
    try:
        view = memoryview(bundle.encode("utf-8"))
        while view:
            view = view[os.write(descriptor, view) :]
    except OSError as exc:
        remove_if_ours(name, descriptor)
        raise CliError(f"cannot write the file: {exc.strerror}") from None
    finally:
        os.close(descriptor)


def remove_if_ours(name: str, descriptor: int) -> None:
    """Remove `name` after a failed write, if it is still the file we made."""
    with contextlib.suppress(OSError):
        made = os.fstat(descriptor)
        there = Path(name).lstat()
        if (made.st_dev, made.st_ino) == (there.st_dev, there.st_ino):
            Path(name).unlink()


# --- output -----------------------------------------------------------------------------------


def _line(summary: ToolSummary) -> str:
    description = printable(summary.description)
    if len(description) > _DESCRIPTION_COLUMNS:
        description = description[: _DESCRIPTION_COLUMNS - 3] + "..."
    tags = ",".join(summary.tags)
    taint = ",".join(summary.taint) or "none"
    return (
        f"{summary.tool}  v{summary.version}  {printable(summary.name)}  [{printable(tags)}]  "
        f"taint:{taint}  {description}"
    )


def _show(summary: ToolSummary, document: str, stdout: TextIO) -> None:
    try:
        package = parse_package(document.encode("utf-8"))
    except ToolPackageError:
        raise CliError("the broker sent a package that is not valid") from None
    manifest = package.manifest

    def ports(items: Sequence[Port]) -> str:
        return ", ".join(f"{port.name}:{port.type}" for port in items) or "none"

    lines = [
        f"{printable(manifest.name)}  ({manifest.id}, version {manifest.version})",
        f"hash:         {summary.hash}",
        f"saved:        {summary.saved_at.isoformat()} ({summary.origin})",
        (
            f"taint:        {', '.join(summary.taint) or 'none'} "
            f"(the package claims {', '.join(package.taint) or 'none'})"
        ),
        f"tags:         {printable(', '.join(manifest.tags)) or 'none'}",
        f"capabilities: {', '.join(manifest.capabilities) or 'none'}",
        f"inputs:       {ports(manifest.inputs)}",
        f"outputs:      {ports(manifest.outputs)}",
        "modules:      "
        + (
            ", ".join(f"{m.name} ({len(m.source.encode())} bytes)" for m in package.modules)
            or "none"
        ),
        f"description:  {printable(manifest.description)}",
    ]
    stdout.write("\n".join(lines) + "\n")


def json_escaped(text: str) -> str:
    """`text` (JSON) with every character that is not printable written as a JSON `\\uXXXX` escape
    (a surrogate pair for a character beyond the basic plane): the same JSON value, safe to print.
    Only strings hold such characters, so the escape is valid wherever one occurs."""
    out: list[str] = []
    for char in text:
        if char.isprintable():
            out.append(char)
            continue
        code = ord(char)
        if code > 0xFFFF:
            code -= 0x10000
            out.append(f"\\u{0xD800 + (code >> 10):04x}\\u{0xDC00 + (code & 0x3FF):04x}")
        else:
            out.append(f"\\u{code:04x}")
    return "".join(out)


def write_json(document: str, stdout: TextIO) -> None:
    """The package's canonical JSON, exactly (no newline added, so that a pipe's bytes hash to the
    package's hash), or, on a terminal, escaped for it and said to be so."""
    if stdout.isatty():
        stdout.write(json_escaped(document) + "\n")
        sys.stderr.write("tumbleweed-tools: escaped for the terminal: not the canonical bytes\n")
    else:
        stdout.write(document)


# --- commands ---------------------------------------------------------------------------------


async def connect(socket_name: str) -> BrokerClient:
    try:
        directory = SocketDirectory.from_environment(dict(os.environ), create=False)
        path = directory.socket_path(socket_name)
        connector = BrokerConnector(path, give_up=CONNECT_TIMEOUT)
        return await connector.connect()
    except (IpcError, TumbleweedError) as error:
        raise CliError(
            f"cannot reach the broker ({type(error).__name__}); is tumbleweed-broker running?"
        ) from None


async def _run(args: argparse.Namespace, stdout: TextIO) -> None:
    command: str = args.command
    bundle = read_bundle(args.file) if command == "import" else ""
    if command == "export" and args.file == "-":
        refuse_terminal(stdout)  # before asking: an export is audited when it is handed out
    async with asyncio.timeout(COMMAND_TIMEOUT.total_seconds()):
        client = await connect(args.socket_name)
        async with client:
            match command:
                case "list":
                    found = await client.list_tools(args.query)
                    for summary in found.tools:
                        stdout.write(_line(summary) + "\n")
                case "show":
                    shown = await client.show_tool(args.tool, args.version)
                    if args.json:
                        write_json(shown.document, stdout)
                    else:
                        _show(shown.summary, shown.document, stdout)
                case "export":
                    exported = await client.export_tool(args.tool, args.version)
                    write_bundle(args.file, exported.bundle, stdout)
                case "import":
                    added = await client.import_tool(bundle)
                    verb = "imported" if added.created else "already stored"
                    claimed = ", ".join(added.summary.claimed_taint) or "none"
                    effective = ", ".join(added.summary.taint) or "none"
                    stdout.write(f"{verb}: {_line(added.summary)}\n")
                    stdout.write(
                        f"  taint the package claims: {claimed}; taint kept: {effective}\n"
                    )
                case _:
                    raise CliError("unknown command")


def main(argv: Sequence[str] | None = None) -> int:
    """The entry point of `tumbleweed-tools`. Returns the process exit status."""
    args = _parser().parse_args(argv)
    try:
        asyncio.run(_run(args, sys.stdout))
    except BrokerRefused as refused:
        sys.stderr.write(f"tumbleweed-tools: the broker refused: {refused.reason}: ")
        sys.stderr.write(f"{printable(refused.message)}\n")
        return 1
    except CliError as error:
        sys.stderr.write(f"tumbleweed-tools: {printable(str(error))}\n")
        return 1
    except TimeoutError:
        sys.stderr.write("tumbleweed-tools: the broker did not answer in time\n")
        return 1
    except IpcError as error:
        sys.stderr.write(
            f"tumbleweed-tools: the broker connection failed: {type(error).__name__}\n"
        )
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
