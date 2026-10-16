"""Configuration, the API key and its file: what is accepted, and what is refused (I6)."""

import copy
import os
import pickle
import re
import stat
import subprocess
import sys
from pathlib import Path

import pytest

from tumbleweed.agent.config import (
    DEFAULT_BASE_URL,
    DEFAULT_MODEL,
    MAX_CONFIG_BYTES,
    MAX_KEY_FILE_BYTES,
    AgentConfig,
    ApiKey,
    ModelRole,
    ResponderChoice,
    config_dir,
    default_key_file,
    load_config,
    parse_config,
    shell_data_dir,
    take_api_key,
)
from tumbleweed.agent.errors import ConfigError, MissingKeyError
from tumbleweed.core.errors import InvalidValueError

KEY = "sk-ant-test-0123456789abcdefghijklmnop"


def agent(text: str = "", **env: str) -> AgentConfig:
    return parse_config(text, env, source="test").agent


@pytest.fixture
def home(tmp_path: Path) -> Path:
    (tmp_path / "config" / "tumbleweed").mkdir(parents=True, mode=0o700)
    return tmp_path


def environ_for(home: Path, **extra: str) -> dict[str, str]:
    return {"HOME": str(home), "XDG_CONFIG_HOME": str(home / "config"), **extra}


def write_key(path: Path, text: str = KEY, mode: int = 0o600) -> Path:
    path.write_text(text)
    path.chmod(mode)
    return path


class TestFile:
    def test_defaults(self) -> None:
        config = agent()

        assert config.responder is ResponderChoice.ANTHROPIC
        assert config.base_url == DEFAULT_BASE_URL
        assert config.model_for(ModelRole.CHAT) == DEFAULT_MODEL == "claude-sonnet-5-5"
        assert (config.limits.max_iterations, config.limits.reply_timeout_seconds) == (8, 120)
        assert config.effort is None

    def test_the_file_sets_everything_documented(self) -> None:
        config = agent(
            """
            [agent]
            responder = "echo"
            base_url = "http://localhost:4000"
            key_file = "/run/keys/anthropic"
            effort = "low"
            [agent.models]
            chat = "claude-haiku-5-5"
            [agent.limits]
            max_iterations = 3
            max_turn_output_tokens = 1000
            reply_timeout_seconds = 30
            """
        )

        assert config.responder is ResponderChoice.ECHO
        assert (config.base_url, config.key_file) == (
            "http://localhost:4000",
            "/run/keys/anthropic",
        )
        assert (config.effort, str(config.models.chat)) == ("low", "claude-haiku-5-5")
        assert (config.limits.max_iterations, config.limits.max_turn_output_tokens) == (3, 1000)

    def test_the_environment_overrides_the_file(self) -> None:
        config = agent(
            '[agent]\nresponder = "echo"\n[agent.models]\nchat = "from-file"\n',
            TUMBLEWEED_RESPONDER="anthropic",
            TUMBLEWEED_MODEL="claude-opus-5-5",
            TUMBLEWEED_BASE_URL="https://proxy.example.com/v1",
            TUMBLEWEED_KEY_FILE="/run/keys/other",
        )

        assert config.responder is ResponderChoice.ANTHROPIC
        assert str(config.models.chat) == "claude-opus-5-5"
        assert (config.base_url, config.key_file) == (
            "https://proxy.example.com/v1",
            "/run/keys/other",
        )

    def test_an_environment_override_is_validated_like_the_file(self) -> None:
        with pytest.raises(ConfigError, match="base_url"):
            agent(TUMBLEWEED_BASE_URL="http://evil.example.com")

    @pytest.mark.parametrize(
        "url",
        [
            "http://api.anthropic.com",  # the key would cross the network in clear text
            "http://localhost.evil.com",
            "ftp://example.com",
            "https://user:pass@example.com",
            "https://example.com/?x=1",
            "https://example.com/#frag",
            "https://",
            "example.com",
            "https://exa mple.com",
            "https://example.com/\n",
            "https://" + "a" * 3000 + ".com",
        ],
    )
    def test_a_base_url_that_could_leak_the_key_is_refused(self, url: str) -> None:
        with pytest.raises(ConfigError, match="base_url"):
            agent(TUMBLEWEED_BASE_URL=url)

    @pytest.mark.parametrize(
        "url", ["http://localhost:4000", "http://127.0.0.1:4000/v1", "http://[::1]:4000"]
    )
    def test_a_local_proxy_may_be_plain_http(self, url: str) -> None:
        assert agent(f'[agent]\nbase_url = "{url}"').base_url == url

    @pytest.mark.parametrize(
        "text",
        [
            '[agent]\napi_key = "' + KEY + '"',
            '[agent]\nanthropic_api_key = "' + KEY + '"',
            "[unknown]\nx = 1",
            '[agent]\nresponder = "gpt"',
            "[agent.limits]\nmax_iterations = 0",
            "[agent.limits]\nmax_turn_output_tokens = 999999",
            '[agent.limits]\nmax_iterations = "3"',
            '[agent]\nkey_file = "relative/key"',
            '[agent.models]\nchat = "has space"',
            "agent = 5",
            "not toml [",
            "when = 1979-05-27T07:32:00Z",
        ],
    )
    def test_invalid_files_are_refused_and_the_message_never_holds_a_value(self, text: str) -> None:
        with pytest.raises(ConfigError) as raised:
            agent(text)

        assert KEY not in str(raised.value)
        assert len(str(raised.value)) < 700

    @pytest.mark.parametrize("path", ["/keys/a\nb", "/keys/\x1b[31m", "/keys/a\x00b", "relative"])
    def test_a_key_file_path_must_be_printable_and_absolute(self, path: str) -> None:
        with pytest.raises(ConfigError, match="key_file"):
            agent(TUMBLEWEED_KEY_FILE=path)

    def test_load_config_reads_the_file_under_xdg_config_home(self, home: Path) -> None:
        path = home / "config" / "tumbleweed" / "config.toml"
        path.write_text('[agent]\nresponder = "echo"\n')
        path.chmod(0o644)

        assert load_config(environ_for(home)).agent.responder is ResponderChoice.ECHO

    def test_no_file_means_defaults_and_the_environment(self, home: Path) -> None:
        config = load_config(environ_for(home, TUMBLEWEED_MODEL="claude-opus-5-5")).agent

        assert str(config.models.chat) == "claude-opus-5-5"

    def test_a_config_file_others_can_write_is_refused(self, home: Path) -> None:
        # Someone who can write it could point base_url at a server of theirs.
        path = home / "config" / "tumbleweed" / "config.toml"
        path.write_text("")
        path.chmod(0o664)

        with pytest.raises(ConfigError, match="mode 0664"):
            load_config(environ_for(home))

    def test_a_symlinked_config_file_is_refused(self, home: Path) -> None:
        target = home / "elsewhere.toml"
        target.write_text("")
        (home / "config" / "tumbleweed" / "config.toml").symlink_to(target)

        with pytest.raises(ConfigError, match="symlink"):
            load_config(environ_for(home))

    def test_a_huge_config_file_is_refused_unread(self, home: Path) -> None:
        path = home / "config" / "tumbleweed" / "config.toml"
        path.write_text("#" * (MAX_CONFIG_BYTES + 1))

        with pytest.raises(ConfigError, match="larger than"):
            load_config(environ_for(home))

    def test_directories_follow_xdg_and_ignore_a_relative_value(self, tmp_path: Path) -> None:
        env = {
            "HOME": str(tmp_path),
            "XDG_CONFIG_HOME": "relative",
            "XDG_DATA_HOME": "also/relative",
        }

        assert config_dir(env) == tmp_path / ".config" / "tumbleweed"
        assert shell_data_dir(env) == tmp_path / ".local" / "share" / "tumbleweed" / "shell"
        env = {"HOME": str(tmp_path), "XDG_CONFIG_HOME": "/x/c", "XDG_DATA_HOME": "/x/d"}
        assert default_key_file(env) == Path("/x/c/tumbleweed/api-key")
        assert shell_data_dir(env) == Path("/x/d/tumbleweed/shell")


class TestKey:
    def test_the_key_comes_from_the_environment(self, home: Path) -> None:
        environ = environ_for(home, ANTHROPIC_API_KEY=KEY)

        assert take_api_key(environ, agent()).reveal() == KEY

    def test_taking_the_key_removes_it_and_the_auth_token_from_the_environment(
        self, home: Path
    ) -> None:
        environ = environ_for(home, ANTHROPIC_API_KEY=KEY, ANTHROPIC_AUTH_TOKEN="tok-123456789")  # noqa: S106  (a canary)

        take_api_key(environ, agent())

        assert "ANTHROPIC_API_KEY" not in environ
        assert "ANTHROPIC_AUTH_TOKEN" not in environ

    def test_the_environment_is_scrubbed_even_when_the_key_is_unusable(self, home: Path) -> None:
        environ = environ_for(home, ANTHROPIC_API_KEY="bad key")

        with pytest.raises(ConfigError):
            take_api_key(environ, agent())

        assert "ANTHROPIC_API_KEY" not in environ

    def test_a_child_process_does_not_inherit_the_key(
        self, home: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("ANTHROPIC_API_KEY", KEY)
        monkeypatch.setenv("HOME", str(home))
        monkeypatch.setenv("XDG_CONFIG_HOME", str(home / "config"))

        take_api_key(os.environ, agent())
        child = subprocess.run(
            [sys.executable, "-c", "import os; print(sorted(os.environ))"],
            capture_output=True,
            text=True,
            check=True,
            timeout=30,
        )

        assert "ANTHROPIC_API_KEY" not in child.stdout
        assert KEY not in child.stdout

    def test_a_private_key_file_is_read_and_stripped(self, home: Path) -> None:
        write_key(default_key_file(environ_for(home)), KEY + "\n")

        assert take_api_key(environ_for(home), agent()).reveal() == KEY

    def test_the_environment_wins_over_the_file(self, home: Path) -> None:
        write_key(default_key_file(environ_for(home)), "file-key-0123456789")

        assert take_api_key(environ_for(home, ANTHROPIC_API_KEY=KEY), agent()).reveal() == KEY

    def test_a_configured_key_file_is_used(self, home: Path) -> None:
        elsewhere = write_key(home / "my-key")

        assert (
            take_api_key(environ_for(home), agent(f'[agent]\nkey_file = "{elsewhere}"')).reveal()
            == KEY
        )

    @pytest.mark.parametrize("mode", [0o640, 0o644, 0o604, 0o660, 0o666, 0o602, 0o677])
    def test_a_key_file_with_group_or_other_permissions_is_refused(
        self, home: Path, mode: int
    ) -> None:
        path = write_key(default_key_file(environ_for(home)), mode=mode)

        with pytest.raises(ConfigError) as raised:
            take_api_key(environ_for(home), agent())

        assert f"mode {mode:04o}" in str(raised.value)
        assert str(path) in str(raised.value)
        assert "must be 0600" in str(raised.value)
        assert KEY not in str(raised.value)

    @pytest.mark.parametrize("mode", [0o600, 0o400])
    def test_owner_only_modes_are_accepted(self, home: Path, mode: int) -> None:
        write_key(default_key_file(environ_for(home)), mode=mode)

        assert take_api_key(environ_for(home), agent()).reveal() == KEY

    def test_a_symlinked_key_file_is_refused_even_to_a_private_file(self, home: Path) -> None:
        real = write_key(home / "real-key")
        default_key_file(environ_for(home)).symlink_to(real)

        with pytest.raises(ConfigError, match="symlink"):
            take_api_key(environ_for(home), agent())

    @pytest.mark.timeout(10)
    def test_a_fifo_is_refused_and_does_not_hang_the_shell(self, home: Path) -> None:
        os.mkfifo(default_key_file(environ_for(home)), 0o600)

        with pytest.raises(ConfigError, match="not a regular file"):
            take_api_key(environ_for(home), agent())

    def test_a_directory_is_refused(self, home: Path) -> None:
        default_key_file(environ_for(home)).mkdir(mode=0o700)

        with pytest.raises(ConfigError, match="not a regular file"):
            take_api_key(environ_for(home), agent())

    def test_a_device_is_refused(self, home: Path) -> None:
        elsewhere = "/dev/null"

        with pytest.raises(ConfigError, match=re.escape("not a regular file")):
            take_api_key(environ_for(home), agent(f'[agent]\nkey_file = "{elsewhere}"'))

    def test_a_key_file_of_another_user_is_refused(
        self, home: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        write_key(default_key_file(environ_for(home)))
        monkeypatch.setattr(os, "geteuid", lambda: home.stat().st_uid + 1)

        with pytest.raises(ConfigError, match="not owned by you"):
            take_api_key(environ_for(home), agent())

    def test_a_huge_key_file_is_refused_unread(self, home: Path) -> None:
        write_key(default_key_file(environ_for(home)), "k" * (MAX_KEY_FILE_BYTES + 1))

        with pytest.raises(ConfigError, match="larger than"):
            take_api_key(environ_for(home), agent())

    def test_a_missing_default_key_file_says_how_to_set_a_key(self, home: Path) -> None:
        with pytest.raises(MissingKeyError) as raised:
            take_api_key(environ_for(home), agent())

        message = str(raised.value)
        assert "ANTHROPIC_API_KEY" in message
        assert "api-key" in message
        assert "0600" in message

    def test_a_missing_configured_key_file_is_a_config_error(self, home: Path) -> None:
        with pytest.raises(ConfigError, match="does not exist"):
            take_api_key(environ_for(home), agent('[agent]\nkey_file = "/nonexistent/key"'))

    def test_an_empty_key_file_is_no_key(self, home: Path) -> None:
        write_key(default_key_file(environ_for(home)), "  \n")

        with pytest.raises(MissingKeyError, match="empty"):
            take_api_key(environ_for(home), agent())

    @pytest.mark.parametrize(
        "content",
        ["SECRETWORD has spaces", "SECRETWORD\nwith\nlines", "SECRET", "SECRET\x00WORD-0123456789"],
    )
    def test_a_key_file_that_holds_no_valid_key_is_refused_without_echoing_it(
        self, home: Path, content: str
    ) -> None:
        write_key(default_key_file(environ_for(home)), content)

        with pytest.raises(ConfigError) as raised:
            take_api_key(environ_for(home), agent())

        assert "SECRET" not in str(raised.value)

    def test_a_key_file_that_is_not_ascii_is_refused(self, home: Path) -> None:
        default_key_file(environ_for(home)).write_bytes("kéy-0123456789".encode())
        default_key_file(environ_for(home)).chmod(0o600)

        with pytest.raises(ConfigError, match="not ASCII"):
            take_api_key(environ_for(home), agent())


class TestApiKeyDoesNotLeak:
    def test_it_prints_redacted_everywhere(self) -> None:
        key = ApiKey(KEY)

        for shown in (repr(key), str(key), f"{key}", f"{key!r}", f"{[key]}", f"{{'k': {key}}}"):
            assert KEY not in shown
        assert "redacted" in repr(key)

    def test_it_cannot_be_pickled_or_copied(self) -> None:
        key = ApiKey(KEY)

        with pytest.raises(TypeError):
            pickle.dumps(key)
        with pytest.raises(TypeError):
            copy.copy(key)
        with pytest.raises(TypeError):
            copy.deepcopy(key)

    def test_it_has_no_dict_to_dump(self) -> None:
        assert not hasattr(ApiKey(KEY), "__dict__")

    @pytest.mark.parametrize(
        "bad", ["", "SECRET", "SECRET has space", "SECRET\tkey-0123456789", "S" * 513, "SECRÉT" * 4]
    )
    def test_an_invalid_key_is_refused_without_its_text(self, bad: str) -> None:
        with pytest.raises(InvalidValueError) as raised:
            ApiKey(bad)

        assert "SECR" not in str(raised.value)
        assert "SSSS" not in str(raised.value)

    def test_a_non_string_is_refused(self) -> None:
        with pytest.raises(InvalidValueError):
            ApiKey(b"sk-ant-0123456789")  # type: ignore[arg-type]  # the point of the test


def test_key_file_mode_the_error_names_is_the_real_one(home: Path) -> None:
    path = write_key(default_key_file(environ_for(home)), mode=0o644)

    assert stat.S_IMODE(path.stat().st_mode) == 0o644
