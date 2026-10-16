"""The system prompt: a versioned resource file next to this module (`prompts/system_<v>.md`).

The version is part of the file's name and is recorded with every invocation, so a log line says
which prompt a call used. A prompt change is a new file and a new `SYSTEM_PROMPT_VERSION`, not an
edit in place. The text is read once, bounded, and is plain text for the model: the file holds no
secret and no per-user data.
"""

import re
from importlib import resources
from typing import Final

from tumbleweed.core.errors import InvalidValueError

SYSTEM_PROMPT_VERSION: Final = "v1"
MAX_PROMPT_BYTES: Final = 16 * 1024
_VERSION = re.compile(r"[A-Za-z0-9]{1,16}")


def load_system_prompt(version: str = SYSTEM_PROMPT_VERSION) -> str:
    """The text of `prompts/system_<version>.md`. Raises `InvalidValueError` if it is not usable."""
    if _VERSION.fullmatch(version) is None:
        raise InvalidValueError("a prompt version is 1-16 letters and digits")
    path = resources.files("tumbleweed.agent") / "prompts" / f"system_{version}.md"
    try:
        data = path.read_bytes()
    except FileNotFoundError:
        raise InvalidValueError(f"there is no system prompt version {version!r}") from None
    if not 0 < len(data) <= MAX_PROMPT_BYTES:
        raise InvalidValueError("the system prompt must be 1 byte to 16 KiB")
    return data.decode("utf-8").strip()
