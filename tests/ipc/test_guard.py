"""The guard in conftest.py that fails a test when asyncio could only log an unhandled error."""

import asyncio
import gc


def test_the_guard_sees_a_task_that_failed_with_nobody_waiting(
    no_error_is_lost_to_the_event_loop: list[str],
) -> None:
    async def scenario() -> None:
        async def fail() -> None:
            raise RuntimeError("nobody is waiting for me")

        task = asyncio.get_running_loop().create_task(fail())
        await asyncio.sleep(0.05)
        assert task.done()
        del task
        gc.collect()  # the loop is told when the unretrieved failure is collected

    asyncio.run(scenario())

    assert any("nobody is waiting for me" in item for item in no_error_is_lost_to_the_event_loop)
    no_error_is_lost_to_the_event_loop.clear()  # it was the point: do not fail this test for it


def test_the_guard_sees_a_callback_that_raised(
    no_error_is_lost_to_the_event_loop: list[str],
) -> None:
    async def scenario() -> None:
        def broken() -> None:
            raise RuntimeError("a callback that raised")

        asyncio.get_running_loop().call_soon(broken)
        await asyncio.sleep(0.05)

    asyncio.run(scenario())

    assert any("a callback that raised" in item for item in no_error_is_lost_to_the_event_loop)
    no_error_is_lost_to_the_event_loop.clear()
