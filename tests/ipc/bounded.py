"""Run an async test scenario with a deadline, so that a broken test fails instead of hanging."""

import asyncio
from collections.abc import Awaitable, Callable

DEADLINE = 20.0


def run[T](scenario: Callable[[], Awaitable[T]], *, deadline: float = DEADLINE) -> T:
    """Run an async scenario on a fresh event loop, failing (not hanging) after `deadline` s."""

    async def bounded() -> T:
        async with asyncio.timeout(deadline):
            return await scenario()

    return asyncio.run(bounded())
