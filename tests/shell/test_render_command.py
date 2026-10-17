"""`tumbleweed-render <spec.json>`: previews a spec standalone, and says why it cannot."""

import os
import subprocess
import sys
from pathlib import Path

import pytest

from tests.ui.spec.support import golden_text
from tumbleweed.shell.render import SpecFileError, load_spec, main, read_spec_file
from tumbleweed.ui.spec.limits import MAX_SPEC_BYTES

ROOT = Path(__file__).resolve().parents[2]


def write(directory: Path, name: str, content: str | bytes) -> Path:
    path = directory / name
    if isinstance(content, str):
        path.write_text(content, encoding="utf-8")
    else:
        path.write_bytes(content)
    return path


class TestReadingTheFile:
    def test_a_valid_spec_is_loaded(self, tmp_path: Path) -> None:
        path = write(tmp_path, "tip.json", golden_text("tip_calculator"))

        assert load_spec(path).title == "Tip calculator"

    def test_a_missing_file_is_a_file_error(self, tmp_path: Path) -> None:
        with pytest.raises(SpecFileError, match="cannot open"):
            read_spec_file(tmp_path / "nope.json")

    def test_a_directory_is_not_a_regular_file(self, tmp_path: Path) -> None:
        with pytest.raises(SpecFileError, match=r"not a regular file|cannot"):
            read_spec_file(tmp_path)

    @pytest.mark.timeout(20)
    def test_a_fifo_is_refused_without_waiting_for_a_writer(self, tmp_path: Path) -> None:
        fifo = tmp_path / "pipe.json"
        os.mkfifo(fifo)

        with pytest.raises(SpecFileError, match="not a regular file"):
            read_spec_file(fifo)

    def test_a_file_over_the_size_limit_is_read_only_one_byte_past_it(self, tmp_path: Path) -> None:
        path = write(tmp_path, "huge.json", b" " * (MAX_SPEC_BYTES * 3))

        data = read_spec_file(path)

        assert len(data) == MAX_SPEC_BYTES + 1  # enough for the parser to say "too large"

    def test_a_file_exactly_at_the_limit_is_read_whole(self, tmp_path: Path) -> None:
        path = write(tmp_path, "edge.json", b"x" * MAX_SPEC_BYTES)

        assert len(read_spec_file(path)) == MAX_SPEC_BYTES

    def test_a_symlink_to_a_spec_is_followed_like_any_file_the_user_names(
        self, tmp_path: Path
    ) -> None:
        target = write(tmp_path, "real.json", golden_text("tip_calculator"))
        link = tmp_path / "link.json"
        link.symlink_to(target)

        assert load_spec(link).title == "Tip calculator"


class TestTheCommandLine:
    def test_a_file_that_is_not_a_valid_spec_prints_the_problems_and_exits_1(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        path = write(tmp_path, "bad.json", '{"version": "tumbleweed.ui/1", "title": "x"}')

        status = main([str(path)])

        error = capsys.readouterr().err
        assert status == 1
        assert "the spec is not valid" in error
        assert "root" in error

    def test_text_that_is_not_json_exits_1(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        path = write(tmp_path, "bad.json", "not json at all")

        assert main([str(path)]) == 1
        assert "not valid JSON" in capsys.readouterr().err

    def test_a_too_large_file_exits_1(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        path = write(tmp_path, "huge.json", b" " * (MAX_SPEC_BYTES + 5))

        assert main([str(path)]) == 1
        assert f"over {MAX_SPEC_BYTES} bytes" in capsys.readouterr().err

    def test_a_missing_file_exits_1_with_one_line(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        assert main([str(tmp_path / "nope.json")]) == 1

        lines = capsys.readouterr().err.strip().splitlines()
        assert len(lines) == 1
        assert lines[0].startswith("tumbleweed-render: cannot open the file")

    def test_the_problems_are_inert_text(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        text = golden_text("tip_calculator").replace("Tip calculator", "x\\u202e\\u001b[31mred")
        path = write(tmp_path, "evil.json", text)

        main([str(path)])

        error = capsys.readouterr().err
        assert "‮" not in error
        assert "\x1b" not in error

    @pytest.mark.parametrize("arguments", [[], ["a.json", "b.json"], ["--nope"]])
    def test_a_wrong_command_line_is_a_usage_error_with_exit_status_2(
        self, arguments: list[str], capsys: pytest.CaptureFixture[str]
    ) -> None:
        with pytest.raises(SystemExit) as raised:
            main(arguments)

        assert raised.value.code == 2
        assert capsys.readouterr().err.startswith("tumbleweed-render: error:")

    def test_help_is_answered_with_no_gtk(self) -> None:
        result = subprocess.run(
            [sys.executable, "-m", "tumbleweed.shell.render", "--help"],
            capture_output=True,
            text=True,
            check=False,
            timeout=60,
        )

        assert result.returncode == 0
        assert "Preview a UI spec" in result.stdout

    def test_a_refused_spec_never_loads_gtk(self, tmp_path: Path) -> None:
        path = write(tmp_path, "bad.json", "{}")
        probe = (
            "import sys\n"
            "from tumbleweed.shell.render import main\n"
            f"status = main([{str(path)!r}])\n"
            "loaded = [n for n in sys.modules if n == 'gi' or n.startswith('gi.')]\n"
            "print(status, loaded)\n"
        )

        result = subprocess.run(  # noqa: S603  (argv list, no shell; our own interpreter)
            [sys.executable, "-c", probe],
            capture_output=True,
            text=True,
            check=False,
            timeout=60,
            cwd=ROOT,
        )

        assert result.returncode == 0, result.stderr
        assert result.stdout.strip() == "1 []"


@pytest.mark.gtk
class TestThePreviewWindow:
    def test_the_window_shows_the_spec_on_a_canvas_and_the_title_is_ours(self) -> None:
        from tumbleweed.shell._gi import Gio
        from tumbleweed.shell.render_app import WINDOW_TITLE, RenderApplication
        from tumbleweed.ui.spec import parse_ui_spec

        spec = parse_ui_spec(golden_text("tip_calculator"))
        app = RenderApplication(spec)
        app.register(None)

        window = app.build_window()
        try:
            assert window.get_title() == WINDOW_TITLE
            assert spec.title not in (window.get_title() or "")
            canvas = window.get_content()
            from tumbleweed.shell.canvas import CanvasView

            assert isinstance(canvas, CanvasView)
            assert canvas.tool is not None
            assert canvas.tool.runtime.spec is spec
        finally:
            window.destroy()
        assert app.get_flags() == Gio.ApplicationFlags.NON_UNIQUE
