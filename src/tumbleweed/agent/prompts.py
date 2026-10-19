"""The system prompt: a versioned resource file next to this module (`prompts/system_<v>.md`).

The version is part of the file's name and is recorded with every invocation, so a log line says
which prompt a call used. A prompt change is a new file and a new `SYSTEM_PROMPT_VERSION`, not an
edit in place. The text is read once, bounded, and is plain text for the model: the file holds no
secret and no per-user data.

From v2 on, the prompt ends with the UI spec reference (`prompts/ui_spec_v1.md`), which is generated
from the spec's schema and docs (`tumbleweed.agent.spec_prompt`), not typed. The reference has a
version of its own, the spec's, and is bounded separately.
"""

import re
from importlib import resources
from typing import Final

from tumbleweed.core.errors import InvalidValueError

SYSTEM_PROMPT_VERSION: Final = "v2"
MAX_PROMPT_BYTES: Final = 16 * 1024  # of the prompt's own file
MAX_SPEC_REFERENCE_BYTES: Final = 64 * 1024  # of the generated reference
_VERSION = re.compile(r"[A-Za-z0-9]{1,16}")
_SPEC_REFERENCE_OF: Final = {"v2": "ui_spec_v1"}  # prompt version -> file of its spec reference


def _read(name: str, limit: int, what: str) -> str:
    path = resources.files("tumbleweed.agent") / "prompts" / name
    try:
        data = path.read_bytes()
    except FileNotFoundError:
        raise InvalidValueError(f"there is no {what} {name!r}") from None
    if not 0 < len(data) <= limit:
        raise InvalidValueError(f"the {what} must be 1 byte to {limit // 1024} KiB")
    return data.decode("utf-8").strip()


def load_system_prompt(version: str = SYSTEM_PROMPT_VERSION) -> str:
    """The text of `prompts/system_<version>.md`, with the spec reference it carries (v2 on).

    Raises `InvalidValueError` if it is not usable.
    """
    if _VERSION.fullmatch(version) is None:
        raise InvalidValueError("a prompt version is 1-16 letters and digits")
    text = _read(f"system_{version}.md", MAX_PROMPT_BYTES, "system prompt version")
    reference = _SPEC_REFERENCE_OF.get(version)
    if reference is None:
        return text
    return f"{text}\n\n{_read(f'{reference}.md', MAX_SPEC_REFERENCE_BYTES, 'spec reference')}"
