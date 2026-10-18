"""The blob store: a read verifies, a write cannot replace, no path comes from outside (S23, I8)."""

import hashlib
import os
import stat
from collections.abc import Iterator
from pathlib import Path

import pytest

from tumbleweed.broker.errors import PrivateFsError, ToolBlobError, ToolBlobWriteError
from tumbleweed.broker.toolblobs import BLOB_DIRECTORY, ToolBlobs
from tumbleweed.core.errors import InvalidValueError
from tumbleweed.core.types import ContentHash
from tumbleweed.tools.limits import MAX_PACKAGE_BYTES

DATA = b'{"a":1}'


def name_of(data: bytes) -> ContentHash:
    return ContentHash(hashlib.sha256(data).hexdigest())


@pytest.fixture
def blobs(tmp_path: Path) -> Iterator[ToolBlobs]:
    opened = ToolBlobs.open(tmp_path)
    yield opened
    opened.close()


def blob_path(tmp_path: Path, name: ContentHash) -> Path:
    return tmp_path / BLOB_DIRECTORY / str(name)


def listing(tmp_path: Path) -> dict[str, bytes]:
    return {path.name: path.read_bytes() for path in sorted((tmp_path / BLOB_DIRECTORY).iterdir())}


class TestWriteAndRead:
    def test_what_is_written_is_read_back(self, tmp_path: Path, blobs: ToolBlobs) -> None:
        name = name_of(DATA)

        blobs.write(name, DATA)

        assert blobs.read(name) == DATA
        assert listing(tmp_path) == {str(name): DATA}  # and no temporary file is left

    def test_the_directory_is_0700_and_the_file_0600(
        self, tmp_path: Path, blobs: ToolBlobs
    ) -> None:
        blobs.write(name_of(DATA), DATA)

        assert stat.S_IMODE((tmp_path / BLOB_DIRECTORY).stat().st_mode) == 0o700
        assert stat.S_IMODE(blob_path(tmp_path, name_of(DATA)).stat().st_mode) == 0o600

    def test_the_name_of_a_file_is_its_hash_and_nothing_else_can_be_a_name(self) -> None:
        for bad in ("../x", "a/b", "/etc/passwd", "..", ".", "", "A" * 64, "g" * 64, "0" * 63):
            with pytest.raises(InvalidValueError):
                ContentHash(bad)

    def test_bytes_that_are_not_what_the_name_says_are_not_written(
        self, tmp_path: Path, blobs: ToolBlobs
    ) -> None:
        with pytest.raises(ToolBlobWriteError, match="not what their hash says"):
            blobs.write(name_of(b"other"), DATA)

        assert listing(tmp_path) == {}

    def test_bytes_over_the_limit_are_not_written(self, tmp_path: Path, blobs: ToolBlobs) -> None:
        big = b"x" * (MAX_PACKAGE_BYTES + 1)

        with pytest.raises(ToolBlobWriteError):
            blobs.write(name_of(big), big)

        assert listing(tmp_path) == {}

    def test_writing_the_same_bytes_again_leaves_the_file_as_it_is(
        self, tmp_path: Path, blobs: ToolBlobs
    ) -> None:
        name = name_of(DATA)
        blobs.write(name, DATA)
        before = blob_path(tmp_path, name).stat()

        blobs.write(name, DATA)

        after = blob_path(tmp_path, name).stat()
        assert (after.st_ino, after.st_mtime_ns) == (before.st_ino, before.st_mtime_ns)
        assert listing(tmp_path) == {str(name): DATA}


class TestDurabilityAndFailures:
    def test_the_directory_is_fsynced_after_the_link_and_before_write_returns(
        self, tmp_path: Path, blobs: ToolBlobs, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        events: list[str] = []
        real_fsync, real_link = os.fsync, os.link
        directory = (tmp_path / BLOB_DIRECTORY).stat()

        def fsync(descriptor: int) -> None:
            info = os.fstat(descriptor)
            kind = "directory" if stat.S_ISDIR(info.st_mode) else "file"
            assert kind != "directory" or info.st_ino == directory.st_ino
            events.append(f"fsync {kind}")
            real_fsync(descriptor)

        def link(*args: object, **kwargs: object) -> None:
            events.append("link")
            real_link(*args, **kwargs)  # type: ignore[arg-type]  # forwarded as received

        monkeypatch.setattr(os, "fsync", fsync)
        monkeypatch.setattr(os, "link", link)

        blobs.write(name_of(DATA), DATA)

        assert events == ["fsync file", "link", "fsync directory"]

    def test_a_full_disk_is_a_write_failure_not_an_integrity_failure(
        self, tmp_path: Path, blobs: ToolBlobs, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        def full(_descriptor: int, _data: object) -> int:
            raise OSError(28, "No space left on device")

        monkeypatch.setattr(os, "write", full)

        with pytest.raises(ToolBlobWriteError) as caught:
            blobs.write(name_of(DATA), DATA)

        assert not isinstance(caught.value, ToolBlobError)
        assert listing(tmp_path) == {}

    def test_a_directory_that_cannot_be_written_is_a_write_failure(
        self, tmp_path: Path, blobs: ToolBlobs
    ) -> None:
        (tmp_path / BLOB_DIRECTORY).chmod(0o500)
        try:
            if os.access(tmp_path / BLOB_DIRECTORY, os.W_OK):
                pytest.skip("this user can write to a directory it has no write permission on")
            with pytest.raises(ToolBlobWriteError):
                blobs.write(name_of(DATA), DATA)
        finally:
            (tmp_path / BLOB_DIRECTORY).chmod(0o700)


class TestAReadVerifies:
    def written(self, tmp_path: Path, blobs: ToolBlobs) -> tuple[ContentHash, Path]:
        name = name_of(DATA)
        blobs.write(name, DATA)
        return name, blob_path(tmp_path, name)

    def test_a_missing_blob_is_refused(self, blobs: ToolBlobs) -> None:
        with pytest.raises(ToolBlobError):
            blobs.read(name_of(b"never written"))

    def test_changed_bytes_are_detected_and_not_returned(
        self, tmp_path: Path, blobs: ToolBlobs
    ) -> None:
        name, path = self.written(tmp_path, blobs)
        path.write_bytes(b'{"a":2}')

        with pytest.raises(ToolBlobError, match="not what its hash says"):
            blobs.read(name)

    def test_one_flipped_bit_is_detected(self, tmp_path: Path, blobs: ToolBlobs) -> None:
        name, path = self.written(tmp_path, blobs)
        flipped = bytearray(DATA)
        flipped[3] ^= 1
        path.write_bytes(bytes(flipped))

        with pytest.raises(ToolBlobError):
            blobs.read(name)

    def test_truncation_and_extension_are_detected(self, tmp_path: Path, blobs: ToolBlobs) -> None:
        name, path = self.written(tmp_path, blobs)
        for changed in (DATA[:-1], b"", DATA + b" "):
            path.write_bytes(changed)
            with pytest.raises(ToolBlobError):
                blobs.read(name)

    def test_an_empty_blob_verifies_only_as_the_hash_of_nothing(
        self, tmp_path: Path, blobs: ToolBlobs
    ) -> None:
        name, path = self.written(tmp_path, blobs)
        path.write_bytes(b"")

        with pytest.raises(ToolBlobError):
            blobs.read(name)

    def test_a_symlink_in_its_place_is_refused_even_to_the_right_bytes(
        self, tmp_path: Path, blobs: ToolBlobs
    ) -> None:
        name, path = self.written(tmp_path, blobs)
        elsewhere = tmp_path / "elsewhere"
        elsewhere.write_bytes(DATA)  # the very bytes the hash is of
        path.unlink()
        path.symlink_to(elsewhere)

        with pytest.raises(ToolBlobError):
            blobs.read(name)

    def test_a_directory_in_its_place_is_refused(self, tmp_path: Path, blobs: ToolBlobs) -> None:
        name, path = self.written(tmp_path, blobs)
        path.unlink()
        path.mkdir()

        with pytest.raises(ToolBlobError):
            blobs.read(name)

    def test_a_fifo_in_its_place_is_refused_and_does_not_hang(
        self, tmp_path: Path, blobs: ToolBlobs
    ) -> None:
        name, path = self.written(tmp_path, blobs)
        path.unlink()
        os.mkfifo(path)

        with pytest.raises(ToolBlobError, match="not what its hash says"):
            blobs.read(name)

    def test_a_second_hard_link_is_refused(self, tmp_path: Path, blobs: ToolBlobs) -> None:
        name, path = self.written(tmp_path, blobs)
        os.link(path, tmp_path / "another-name")

        with pytest.raises(ToolBlobError):
            blobs.read(name)

    def test_a_file_over_the_size_limit_is_refused_without_reading_it(
        self, tmp_path: Path, blobs: ToolBlobs
    ) -> None:
        name, path = self.written(tmp_path, blobs)
        with path.open("r+b") as file:
            file.truncate(50 * MAX_PACKAGE_BYTES)  # sparse: cheap

        with pytest.raises(ToolBlobError):
            blobs.read(name)

    def test_what_cannot_be_a_blob_is_refused_before_a_byte_is_read(
        self,
        tmp_path: Path,
        blobs: ToolBlobs,
        monkeypatch: pytest.MonkeyPatch,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        def read_nothing(_descriptor: int) -> bytes:
            raise AssertionError("a file that cannot be a blob was read")

        monkeypatch.setattr(ToolBlobs, "_read_all", staticmethod(read_nothing))
        name, path = self.written(tmp_path, blobs)

        with path.open("r+b") as file:  # over the limit
            file.truncate(50 * MAX_PACKAGE_BYTES)
        with pytest.raises(ToolBlobError):
            blobs.read(name)
        assert "over the size limit" in caplog.text
        path.unlink()
        os.mkfifo(path)  # not a regular file
        with pytest.raises(ToolBlobError):
            blobs.read(name)
        assert "not a regular file" in caplog.text
        path.unlink()
        path.mkdir()
        caplog.clear()
        with pytest.raises(ToolBlobError):
            blobs.read(name)
        assert "not a regular file" in caplog.text

    def test_a_loose_mode_is_set_back_to_0600_and_the_read_goes_on(
        self, tmp_path: Path, blobs: ToolBlobs
    ) -> None:
        name, path = self.written(tmp_path, blobs)
        path.chmod(0o644)

        assert blobs.read(name) == DATA
        assert stat.S_IMODE(path.stat().st_mode) == 0o600


class TestAWriteCannotReplace:
    def test_a_blob_that_does_not_verify_is_not_overwritten_and_not_deleted(
        self, tmp_path: Path, blobs: ToolBlobs
    ) -> None:
        name = name_of(DATA)
        blob_path(tmp_path, name).write_bytes(b"planted")

        with pytest.raises(ToolBlobError):
            blobs.write(name, DATA)

        assert listing(tmp_path) == {str(name): b"planted"}

    def test_a_symlink_at_the_final_name_is_not_followed_and_its_target_not_written(
        self, tmp_path: Path, blobs: ToolBlobs
    ) -> None:
        name = name_of(DATA)
        target = tmp_path / "victim"
        target.write_bytes(b"precious")
        blob_path(tmp_path, name).symlink_to(target)

        with pytest.raises(ToolBlobError):
            blobs.write(name, DATA)

        assert target.read_bytes() == b"precious"
        assert blob_path(tmp_path, name).is_symlink()
        assert [p.name for p in (tmp_path / BLOB_DIRECTORY).iterdir()] == [str(name)]

    def test_a_failed_write_leaves_no_temporary_file(
        self, tmp_path: Path, blobs: ToolBlobs, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        def broken(*_args: object, **_kwargs: object) -> None:
            raise PermissionError

        monkeypatch.setattr(os, "link", broken)

        with pytest.raises(ToolBlobWriteError, match="cannot be written"):
            blobs.write(name_of(DATA), DATA)

        assert listing(tmp_path) == {}


class TestOpening:
    def test_a_crash_leaves_temporary_files_and_opening_removes_them(self, tmp_path: Path) -> None:
        directory = tmp_path / BLOB_DIRECTORY
        directory.mkdir(mode=0o700)
        (directory / ".tmp-deadbeef").write_bytes(b"half")
        (directory / "keep-me").write_bytes(b"not ours to remove")
        kept = name_of(DATA)
        (directory / str(kept)).write_bytes(DATA)

        opened = ToolBlobs.open(tmp_path)
        opened.close()

        assert sorted(p.name for p in directory.iterdir()) == sorted(["keep-me", str(kept)])

    def test_a_symlinked_directory_is_refused(self, tmp_path: Path) -> None:
        real = tmp_path / "real"
        real.mkdir(mode=0o700)
        (tmp_path / BLOB_DIRECTORY).symlink_to(real)

        with pytest.raises(PrivateFsError):
            ToolBlobs.open(tmp_path)

    def test_a_directory_with_a_loose_mode_is_set_to_0700(self, tmp_path: Path) -> None:
        directory = tmp_path / BLOB_DIRECTORY
        directory.mkdir(mode=0o755)
        directory.chmod(0o755)

        ToolBlobs.open(tmp_path).close()

        assert stat.S_IMODE(directory.stat().st_mode) == 0o700

    def test_closing_twice_is_fine(self, tmp_path: Path) -> None:
        opened = ToolBlobs.open(tmp_path)

        opened.close()
        opened.close()
