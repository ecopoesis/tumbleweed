"""Builds the shell's responder from the configuration (`build_agent`).

The one place that reads the environment and the key. It removes the API key from the
environment it is given (see `take_api_key`), so nothing the shell starts later inherits it, and
it never raises for a problem the user can fix: a bad config file, no key or a key file that is too
open make an `UnavailableResponder`, whose reason the chat shows on the first message, so the
shell still starts and says what to do. `responder = "echo"` selects the S02 placeholder.
"""

import logging
from collections.abc import Callable, MutableMapping
from dataclasses import dataclass
from typing import final

from tumbleweed.agent.anthropic_provider import (
    AnthropicProvider,
    build_client,
    response_byte_limit,
)
from tumbleweed.agent.config import (
    SECRET_ENV_VARIABLES,
    AgentConfig,
    ModelRole,
    ResponderChoice,
    load_config,
    shell_data_dir,
    take_api_key,
)
from tumbleweed.agent.echo import EchoResponder
from tumbleweed.agent.errors import AgentError, describe_unexpected
from tumbleweed.agent.prompts import SYSTEM_PROMPT_VERSION, load_system_prompt
from tumbleweed.agent.recorder import InvocationRecorder, JsonlInvocationLog
from tumbleweed.agent.responder import Responder
from tumbleweed.agent.runtime import AgentResponder, MainThreadDispatcher, UnavailableResponder
from tumbleweed.agent.tools import ToolRegistry
from tumbleweed.core.errors import TumbleweedError

logger = logging.getLogger(__name__)


@final
@dataclass(frozen=True)
class AgentSetup:
    """The responder to give the shell, and what to call when the shell quits."""

    responder: Responder
    close: Callable[[], None]


def _nothing() -> None:
    return


def build_agent(environ: MutableMapping[str, str], dispatcher: MainThreadDispatcher) -> AgentSetup:
    """The responder the configuration asks for, or one that explains what is wrong."""
    try:
        config = load_config(environ).agent
    except AgentError as exc:
        for name in SECRET_ENV_VARIABLES:
            environ.pop(name, None)
        logger.warning("the configuration cannot be used: %s", type(exc).__name__)
        return AgentSetup(UnavailableResponder(str(exc)), _nothing)
    if config.responder is ResponderChoice.ECHO:
        for name in SECRET_ENV_VARIABLES:
            environ.pop(name, None)
        logger.info("responder: echo (placeholder)")
        return AgentSetup(EchoResponder(), _nothing)
    return _build_anthropic(environ, config, dispatcher)


def _build_anthropic(
    environ: MutableMapping[str, str], config: AgentConfig, dispatcher: MainThreadDispatcher
) -> AgentSetup:
    try:
        key = take_api_key(environ, config)
        client = build_client(
            api_key=key,
            base_url=config.base_url,
            max_response_bytes=response_byte_limit(config.limits.max_turn_output_tokens),
        )
        recorder = InvocationRecorder(
            JsonlInvocationLog(shell_data_dir(environ) / "invocations.jsonl"),
            prompt_version=SYSTEM_PROMPT_VERSION,
        )
        model = config.model_for(ModelRole.CHAT)
        responder = AgentResponder(
            provider=AnthropicProvider(client, effort=config.effort),
            recorder=recorder,
            registry=ToolRegistry(),
            model=model,
            system_prompt=load_system_prompt(),
            limits=config.limits,
            dispatcher=dispatcher,
            cleanup=client.close,
        )
    except TumbleweedError as exc:
        # No key, a key file that is too open, an unreadable prompt: say so in the chat.
        logger.warning("the model is not available: %s", type(exc).__name__)
        return AgentSetup(UnavailableResponder(str(exc)), _nothing)
    except Exception as exc:  # noqa: BLE001  (a client that cannot be built must not stop the shell)
        # The SDK or its HTTP library refused the settings (a base_url the checks let through).
        logger.error("the model client cannot be set up: %s", describe_unexpected(exc))
        return AgentSetup(
            UnavailableResponder("The model client could not be set up: check base_url."),
            _nothing,
        )
    logger.info("responder: %s (prompt %s)", model, SYSTEM_PROMPT_VERSION)
    return AgentSetup(responder, responder.close)
