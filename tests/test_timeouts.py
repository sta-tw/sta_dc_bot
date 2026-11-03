import asyncio

import pytest

from bot.utils.timeouts import wait_for


@pytest.mark.asyncio
async def test_wait_for_raises_builtin_timeout_error():
    async def never_finishes():
        await asyncio.Event().wait()

    with pytest.raises(TimeoutError) as caught:
        await wait_for(never_finishes(), timeout=0.01)

    assert type(caught.value) is TimeoutError


@pytest.mark.asyncio
async def test_wait_for_returns_result_in_time():
    async def quick():
        return 7

    assert await wait_for(quick(), timeout=1) == 7
