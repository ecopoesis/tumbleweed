"""`tumbleweed-tools`: a client of the running broker, careful with files and with output."""

import asyncio
import io
import os
import subprocess
import sys
from collections.abc import Sequence
from datetime import timedelta
from pathlib import Path

import pytest

from tests.broker.rig import Rig, running
from tests.broker.test_daemon_tools import save
from tests.ipc.bounded import run
from tests.tools.support import draft, package
from tumbleweed.broker import toolscli
from tumbleweed.broker.toolscli import CliError, main, read_bundle, write_bundle
from tumbleweed.core.ids import ToolId
from tumbleweed.tools import export_bundle, parse_bundle
from tumbleweed.tools.limits import MAX_BUNDLE_BYTES


class Result:
    def __init__(self, status: int, out: str, err: str) -> None:
        self.status, self.out, self.err = status, out, err


async def tools(
    rig: Rig,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    *args: str,
    stdin: bytes | None = None,
) -> Result:
    """Run `tumbleweed-tools` against the rig's broker, in a thread so that the broker is served."""
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(rig.socket.path.parent.parent))
    if stdin is not None:
        monkeypatch.setattr(sys, "stdin", io.TextIOWrapper(io.BytesIO(stdin), encoding="utf-8"))
    status = await asyncio.to_thread(main, list(args))
    captured = capsys.readouterr()
    return Result(status, captured.out, captured.err)


def bundle_of() -> str:
    return export_bundle(package()).decode("utf-8")


class TestCommands:
    def test_list_show_export_and_import_through_the_broker(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        async def scenario() -> None:
            async with running() as rig:
                tool = await save(rig, draft(name="CSV viewer", tags=("csv",)), taint="fs")

                listed = await tools(rig, monkeypatch, capsys, "list")
                searched = await tools(rig, monkeypatch, capsys, "list", "viewer")
                nothing = await tools(rig, monkeypatch, capsys, "list", "zebra")
                shown = await tools(rig, monkeypatch, capsys, "show", str(tool))
                as_json = await tools(rig, monkeypatch, capsys, "show", str(tool), "--json")
                target = tmp_path / "csv.tool.json"
                exported = await tools(rig, monkeypatch, capsys, "export", str(tool), str(target))

                assert listed.status == searched.status == nothing.status == 0
                assert listed.out == searched.out
                assert str(tool) in listed.out
                assert "CSV viewer" in listed.out
                assert "taint:fs" in listed.out
                assert nothing.out == ""
                assert shown.status == 0
                assert "version 1" in shown.out
                assert "csv_path:text" in shown.out
                assert "data:table" in shown.out
                assert "csv_view" in shown.out
                assert as_json.out.startswith('{"format":"tumbleweed.tool/1"')
                assert exported.status == 0
                assert parse_bundle(target.read_bytes()).package.manifest.id == tool
                assert target.stat().st_mode & 0o777 == 0o600

        run(scenario)

    def test_import_reads_a_file_and_says_what_it_did(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        async def scenario() -> None:
            async with running() as rig:
                source = tmp_path / "in.json"
                source.write_text(bundle_of(), encoding="utf-8")

                first = await tools(rig, monkeypatch, capsys, "import", str(source))
                again = await tools(rig, monkeypatch, capsys, "import", str(source))

                assert first.status == again.status == 0
                assert first.out.startswith("imported: tool_")
                assert again.out.startswith("already stored: tool_")
                assert len((await (await rig.client()).list_tools()).tools) == 1

        run(scenario)

    def test_standard_input_and_output_are_dash(
        self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        async def scenario() -> None:
            async with running() as rig:
                text = bundle_of()
                added = await tools(rig, monkeypatch, capsys, "import", "-", stdin=text.encode())
                (summary,) = (await (await rig.client()).list_tools()).tools
                out = await tools(rig, monkeypatch, capsys, "export", str(summary.tool), "-")

                assert added.status == 0
                assert out.status == 0
                assert out.out == text

        run(scenario)

    def test_a_refusal_is_reported_with_its_reason_and_exit_status_1(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        async def scenario() -> None:
            async with running() as rig:
                junk = tmp_path / "junk.json"
                junk.write_text("not a bundle", encoding="utf-8")
                target = tmp_path / "never.json"

                imported = await tools(rig, monkeypatch, capsys, "import", str(junk))
                missing = await tools(rig, monkeypatch, capsys, "show", str(ToolId.new()))
                export = await tools(
                    rig, monkeypatch, capsys, "export", str(ToolId.new()), str(target)
                )

                assert imported.status == missing.status == export.status == 1
                assert "refused: invalid_request" in imported.err
                assert "refused: not_found" in missing.err
                assert not target.exists()  # nothing is made for an export that was refused

        run(scenario)

    def test_the_text_of_a_tool_cannot_move_the_cursor_or_forge_a_line(
        self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        async def scenario() -> None:
            async with running() as rig:
                tool = await save(rig, draft(description="first line\nSECOND LINE\ttab"))

                listed = await tools(rig, monkeypatch, capsys, "list")
                shown = await tools(rig, monkeypatch, capsys, "show", str(tool))

                assert len(listed.out.splitlines()) == 1
                assert "\\u000a" in listed.out
                assert "SECOND LINE" not in shown.out.splitlines()
                assert all(
                    "\x1b" not in line and "\t" not in line for line in shown.out.splitlines()
                )

        run(scenario)

    def test_a_broker_that_is_not_running_is_said_so_without_a_traceback(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        directory = tmp_path / "runtime"
        directory.mkdir(mode=0o700)
        monkeypatch.setenv("XDG_RUNTIME_DIR", str(directory))
        monkeypatch.setattr(toolscli, "CONNECT_TIMEOUT", timedelta(milliseconds=200))

        status = main(["list"])

        captured = capsys.readouterr()
        assert status == 1
        assert "is tumbleweed-broker running?" in captured.err
        assert "Traceback" not in captured.err


class TestTheCommandLine:
    @pytest.mark.parametrize(
        "args",
        [
            [],
            ["bogus"],
            ["show", "nope"],
            ["show", "tool_x"],
            ["export", "tool_x"],
            ["show"],
            ["import"],
        ],
    )
    def test_a_command_line_that_is_not_understood_exits_2(
        self, args: Sequence[str], capsys: pytest.CaptureFixture[str]
    ) -> None:
        with pytest.raises(SystemExit) as caught:
            main(list(args))

        assert caught.value.code == 2
        assert "usage:" in capsys.readouterr().err

    def test_a_version_is_a_number_from_one(self, capsys: pytest.CaptureFixture[str]) -> None:
        for bad in ("0", "-1", "x", "1.5", ""):
            with pytest.raises(SystemExit) as caught:
                main(["show", str(ToolId.new()), "--version", bad])
            assert caught.value.code == 2
        capsys.readouterr()

    def test_the_module_runs_as_a_program(self) -> None:
        result = subprocess.run(
            [sys.executable, "-I", "-m", "tumbleweed.broker.toolscli", "--help"],
            capture_output=True,
            text=True,
            check=False,
            timeout=60,
        )

        assert result.returncode == 0
        assert "tumbleweed-tools" in result.stdout


class TestBundleFiles:
    def test_a_bundle_file_is_read(self, tmp_path: Path) -> None:
        path = tmp_path / "b.json"
        path.write_text(bundle_of(), encoding="utf-8")

        assert read_bundle(str(path)).startswith('{"format"')

    def test_a_symlink_is_not_followed(self, tmp_path: Path) -> None:
        real = tmp_path / "real.json"
        real.write_text(bundle_of(), encoding="utf-8")
        link = tmp_path / "link.json"
        link.symlink_to(real)

        with pytest.raises(CliError, match="cannot open"):
            read_bundle(str(link))

    def test_what_is_not_a_regular_file_is_refused_and_does_not_hang(self, tmp_path: Path) -> None:
        fifo = tmp_path / "fifo"
        os.mkfifo(fifo)

        with pytest.raises(CliError, match="not a regular file"):
            read_bundle(str(fifo))
        with pytest.raises(CliError, match="not a regular file"):
            read_bundle(str(tmp_path))

    def test_a_file_over_the_limit_is_refused_after_reading_no_more_than_the_limit(
        self, tmp_path: Path
    ) -> None:
        path = tmp_path / "huge.json"
        with path.open("wb") as file:
            file.truncate(100 * MAX_BUNDLE_BYTES)  # sparse: cheap

        with pytest.raises(CliError, match="over"):
            read_bundle(str(path))

    def test_text_that_is_not_utf8_is_refused(self, tmp_path: Path) -> None:
        path = tmp_path / "bin"
        path.write_bytes(b"\xff\xfe{}")

        with pytest.raises(CliError, match="not UTF-8"):
            read_bundle(str(path))

    def test_a_missing_file(self, tmp_path: Path) -> None:
        with pytest.raises(CliError, match="cannot open"):
            read_bundle(str(tmp_path / "nope"))

    def test_standard_input_is_bounded_too(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(
            sys, "stdin", io.TextIOWrapper(io.BytesIO(b"x" * (MAX_BUNDLE_BYTES + 5)))
        )

        with pytest.raises(CliError, match="over"):
            read_bundle("-")

    def test_a_new_file_is_made_0600_and_never_overwritten(self, tmp_path: Path) -> None:
        path = tmp_path / "out.json"

        write_bundle(str(path), "first", io.StringIO())

        assert path.read_text() == "first"
        assert path.stat().st_mode & 0o777 == 0o600
        with pytest.raises(CliError, match="does not overwrite"):
            write_bundle(str(path), "second", io.StringIO())
        assert path.read_text() == "first"

    def test_a_symlink_is_never_written_through(self, tmp_path: Path) -> None:
        victim = tmp_path / "victim"
        victim.write_text("precious")
        link = tmp_path / "link"
        link.symlink_to(victim)

        with pytest.raises(CliError):
            write_bundle(str(link), "evil", io.StringIO())

        assert victim.read_text() == "precious"

    def test_a_directory_that_is_missing(self, tmp_path: Path) -> None:
        with pytest.raises(CliError, match="cannot create"):
            write_bundle(str(tmp_path / "no" / "such" / "out"), "x", io.StringIO())

    def test_dash_writes_to_standard_output(self) -> None:
        out = io.StringIO()

        write_bundle("-", "bundle", out)

        assert out.getvalue() == "bundle"

    def test_a_failed_write_removes_the_file_it_made(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        def full(_descriptor: int, _data: object) -> int:
            raise OSError(28, "No space left on device")

        monkeypatch.setattr(os, "write", full)
        path = tmp_path / "out.json"

        with pytest.raises(CliError, match="cannot write"):
            write_bundle(str(path), "x" * 100, io.StringIO())

        assert not path.exists()
