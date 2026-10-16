"""Configuration: the TOML file, the environment's overrides, and the API key (I6).

The file is `$XDG_CONFIG_HOME/tumbleweed/config.toml` (`~/.config` if that is unset or not an
absolute path). It holds no secret: a key in it is refused. Environment variables override it:

    TUMBLEWEED_RESPONDER   anthropic | echo
    TUMBLEWEED_MODEL       the model of the `chat` role
    TUMBLEWEED_BASE_URL    where the API is (a proxy such as LiteLLM, later)
    TUMBLEWEED_KEY_FILE    absolute path of the key file

The API key comes from `ANTHROPIC_API_KEY`, or else from a key file (by default
`$XDG_CONFIG_HOME/tumbleweed/api-key`). Both are read with care:

- A key file must be a regular file (not a symlink, FIFO or device: the final component is opened
  with `O_NOFOLLOW | O_NONBLOCK` and its type checked on the open descriptor, so there is no race
  between the check and the read), owned by the user, with no group or other permissions (0600),
  and small. A looser file is refused with an error that names the mode, never the contents.
- `take_api_key` removes the key (and `ANTHROPIC_AUTH_TOKEN`, which the SDK would also use) from
  the environment it was given, so that no process the shell starts inherits it, whatever the
  outcome.
- The key lives in an `ApiKey`, which cannot be printed, formatted, pickled or compared into a log.

The config file is read with the same care (regular, owned by the user, not writable by others, at
most 64 KiB): a file someone else can write could point `base_url` at a server of theirs. A
`base_url` must be `https`, or `http` to a loopback host for a local proxy, with no credentials,
query or fragment in it, because the key is sent there.
"""

import enum
import errno
import json
import os
import stat
import tomllib
from collections.abc import Mapping, MutableMapping
from pathlib import Path, PurePosixPath
from typing import Final, Literal, NoReturn, final
from urllib.parse import urlsplit

from pydantic import BaseModel, ConfigDict, ValidationError, field_validator

from tumbleweed.agent.errors import ConfigError, MissingKeyError
from tumbleweed.agent.limits import AgentLimits
from tumbleweed.core.errors import InvalidValueError
from tumbleweed.core.types import ByteCount, ModelName

DEFAULT_MODEL: Final = ModelName("claude-sonnet-5-5")
DEFAULT_BASE_URL: Final = "https://api.anthropic.com"
MAX_CONFIG_BYTES: Final = ByteCount(64 * 1024)
MAX_KEY_FILE_BYTES: Final = ByteCount(4 * 1024)
KEY_ENV_VARIABLE: Final = "ANTHROPIC_API_KEY"
SECRET_ENV_VARIABLES: Final = (KEY_ENV_VARIABLE, "ANTHROPIC_AUTH_TOKEN")
_LOOPBACK_HOSTS = frozenset({"localhost", "127.0.0.1", "::1"})
_MAX_URL_CHARS = 2048
_MAX_PROBLEMS = 5


type Effort = Literal["low", "medium", "high", "xhigh", "max"]


class ResponderChoice(enum.StrEnum):
    """Which responder answers the chat."""

    ANTHROPIC = "anthropic"  # without a usable key, every message fails with a notice saying why
    ECHO = "echo"  # the placeholder of S02


class ModelRole(enum.StrEnum):
    """What a model is used for. Each role has its own model setting."""

    CHAT = "chat"


@final
class ModelsConfig(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid", frozen=True)

    chat: ModelName = DEFAULT_MODEL


@final
class AgentConfig(BaseModel):
    """The `[agent]` table."""

    model_config = ConfigDict(strict=True, extra="forbid", frozen=True)

    responder: ResponderChoice = ResponderChoice.ANTHROPIC
    base_url: str = DEFAULT_BASE_URL
    key_file: str | None = None
    effort: Effort | None = None
    """The model's effort (thinking depth); unset leaves the model's default."""
    models: ModelsConfig = ModelsConfig()
    limits: AgentLimits = AgentLimits()

    @field_validator("base_url")
    @classmethod
    def _base_url_is_safe(cls, value: str) -> str:
        if len(value) > _MAX_URL_CHARS or not value.isprintable() or " " in value:
            raise ValueError("base_url must be a short URL without spaces or control characters")
        parts = urlsplit(value)
        host = parts.hostname
        if host is None or parts.username is not None or parts.password is not None:
            raise ValueError("base_url needs a host and no credentials")
        if parts.query or parts.fragment:
            raise ValueError("base_url must have no query or fragment")
        if parts.scheme != "https" and not (parts.scheme == "http" and host in _LOOPBACK_HOSTS):
            raise ValueError("base_url must be https (http only to localhost, for a local proxy)")
        return value

    @field_validator("key_file")
    @classmethod
    def _key_file_is_absolute(cls, value: str | None) -> str | None:
        if value is not None and (
            not value.isprintable() or len(value) > 4096 or not PurePosixPath(value).is_absolute()
        ):
            raise ValueError("key_file must be an absolute path")
        return value

    def model_for(self, role: ModelRole) -> ModelName:
        match role:
            case ModelRole.CHAT:
                return self.models.chat


@final
class TumbleweedConfig(BaseModel):
    """The whole file. Other components add their tables as they arrive."""

    model_config = ConfigDict(strict=True, extra="forbid", frozen=True)

    agent: AgentConfig = AgentConfig()


@final
class ApiKey:
    """An API key that does not leak: it prints as redacted and cannot be pickled or copied
    into a model. `reveal` is called in one place, where the SDK client is built.
    """

    __slots__ = ("_value",)

    def __init__(self, value: str) -> None:
        raw: object = value
        if (
            type(raw) is not str
            or not 8 <= len(value) <= 512
            or not all("!" <= char <= "~" for char in value)
        ):
            # Nothing of the value in the message: it may be a key with a typo.
            raise InvalidValueError(
                "an API key must be 8-512 printable ASCII characters without spaces"
            )
        self._value = value

    def reveal(self) -> str:
        return self._value

    def __repr__(self) -> str:
        return "ApiKey(<redacted>)"

    __str__ = __repr__

    def __reduce__(self) -> NoReturn:
        raise TypeError("an ApiKey cannot be pickled")

    def __copy__(self) -> NoReturn:
        raise TypeError("an ApiKey cannot be copied")

    def __deepcopy__(self, memo: object) -> NoReturn:
        raise TypeError("an ApiKey cannot be copied")


def _home(environ: Mapping[str, str]) -> Path:
    home = environ.get("HOME")
    return Path(home) if home and PurePosixPath(home).is_absolute() else Path.home()


def _xdg(environ: Mapping[str, str], name: str, fallback: str) -> Path:
    value = environ.get(name)
    if value and PurePosixPath(value).is_absolute():
        return Path(value)
    return _home(environ) / fallback


def config_dir(environ: Mapping[str, str]) -> Path:
    """`$XDG_CONFIG_HOME/tumbleweed` (ARCHITECTURE section 9)."""
    return _xdg(environ, "XDG_CONFIG_HOME", ".config") / "tumbleweed"


def shell_data_dir(environ: Mapping[str, str]) -> Path:
    """`$XDG_DATA_HOME/tumbleweed/shell`: chat history and the invocation log."""
    return _xdg(environ, "XDG_DATA_HOME", ".local/share") / "tumbleweed" / "shell"


def default_key_file(environ: Mapping[str, str]) -> Path:
    return config_dir(environ) / "api-key"


def _read_regular_file(path: Path, *, limit: int, private: bool, what: str) -> bytes:
    """Read `path`, which must be a regular file of ours that nobody else can write (and, if
    `private`, nobody else can read). Raises `FileNotFoundError` if it is not there and
    `ConfigError` for anything wrong with it; the message never holds the contents.
    """
    flags = os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC | os.O_NONBLOCK
    try:
        descriptor = os.open(path, flags)
    except FileNotFoundError:
        raise
    except OSError as exc:
        reason = "is a symlink" if exc.errno == errno.ELOOP else exc.strerror or "cannot be opened"
        raise ConfigError(f"The {what} {path} {reason}; it must be a regular file.") from None
    try:
        info = os.fstat(descriptor)
        if not stat.S_ISREG(info.st_mode):
            raise ConfigError(f"The {what} {path} is not a regular file.")
        if info.st_uid != os.geteuid():
            raise ConfigError(f"The {what} {path} is not owned by you.")
        forbidden = 0o077 if private else 0o022
        if info.st_mode & forbidden:
            # The path once, early: the chat shows at most 200 characters of an error.
            need = "0600" if private else "no write for group or others"
            raise ConfigError(
                f"The {what} {path} has mode {stat.S_IMODE(info.st_mode):04o}; it must be {need}."
            )
        if info.st_size > limit:
            raise ConfigError(f"The {what} {path} is larger than {limit} bytes.")
        data = os.read(descriptor, limit + 1)
    finally:
        os.close(descriptor)
    if len(data) > limit:
        raise ConfigError(f"The {what} {path} is larger than {limit} bytes.")
    return data


def _env_overrides(environ: Mapping[str, str]) -> dict[str, object]:
    agent: dict[str, object] = {}
    if value := environ.get("TUMBLEWEED_RESPONDER"):
        agent["responder"] = value
    if value := environ.get("TUMBLEWEED_BASE_URL"):
        agent["base_url"] = value
    if value := environ.get("TUMBLEWEED_KEY_FILE"):
        agent["key_file"] = value
    if value := environ.get("TUMBLEWEED_MODEL"):
        agent["models"] = {"chat": value}
    return agent


def _problems(error: ValidationError) -> str:
    # Locations and our own messages only: the input values may hold a secret pasted in by mistake.
    items = error.errors(include_input=False, include_url=False, include_context=False)
    return "; ".join(
        f"{'.'.join(str(part) for part in item['loc'])}: {item['msg']}"
        for item in items[:_MAX_PROBLEMS]
    )[:500]


def parse_config(text: str, environ: Mapping[str, str], *, source: str) -> TumbleweedConfig:
    """Validate TOML `text`, with `environ`'s overrides applied. Raises `ConfigError`."""
    try:
        data = tomllib.loads(text)
        agent = data.setdefault("agent", {})
        if not isinstance(agent, dict):
            raise ConfigError(f"In {source}: [agent] must be a table.")
        for key, value in _env_overrides(environ).items():
            agent[key] = value
        return TumbleweedConfig.model_validate_json(json.dumps(data))
    except tomllib.TOMLDecodeError as exc:
        raise ConfigError(f"{source} is not valid TOML: {str(exc)[:200]}") from None
    except (TypeError, ValueError) as exc:
        if isinstance(exc, ValidationError):
            raise ConfigError(f"Invalid configuration in {source}: {_problems(exc)}") from None
        raise ConfigError(f"{source} holds a value that cannot be used.") from None


def load_config(environ: Mapping[str, str]) -> TumbleweedConfig:
    """The configuration: the file if there is one, then the environment's overrides."""
    path = config_dir(environ) / "config.toml"
    try:
        data = _read_regular_file(path, limit=MAX_CONFIG_BYTES, private=False, what="config file")
    except FileNotFoundError:
        return parse_config("", environ, source="the environment")
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError:
        raise ConfigError(f"The config file {path} is not UTF-8 text.") from None
    return parse_config(text, environ, source=str(path))


def _key_from_text(raw: str, *, source: str) -> ApiKey:
    try:
        return ApiKey(raw.strip())
    except InvalidValueError as exc:
        raise ConfigError(f"The API key in {source} is not usable: {exc}") from None


def take_api_key(environ: MutableMapping[str, str], config: AgentConfig) -> ApiKey:
    """The API key, from `ANTHROPIC_API_KEY` or the key file; the environment variables that
    could carry one are removed from `environ` first, so a failure leaves nothing behind either.

    Raises `MissingKeyError` if there is no key, `ConfigError` if there is one that cannot be used.
    """
    from_env = environ.get(KEY_ENV_VARIABLE, "")
    for name in SECRET_ENV_VARIABLES:
        environ.pop(name, None)
    if from_env.strip():
        return _key_from_text(from_env, source=f"${KEY_ENV_VARIABLE}")
    explicit = config.key_file is not None
    path = Path(config.key_file) if config.key_file is not None else default_key_file(environ)
    try:
        data = _read_regular_file(path, limit=MAX_KEY_FILE_BYTES, private=True, what="key file")
    except FileNotFoundError:
        if explicit:
            raise ConfigError(f"The key file {path} does not exist.") from None
        raise MissingKeyError(
            f"No API key is set. Set {KEY_ENV_VARIABLE}, or put the key in {path} (mode 0600), "
            "then restart."
        ) from None
    try:
        text = data.decode("ascii")
    except UnicodeDecodeError:
        raise ConfigError(f"The key file {path} is not ASCII text.") from None
    if not text.strip():
        raise MissingKeyError(f"The key file {path} is empty.")
    return _key_from_text(text, source=str(path))
