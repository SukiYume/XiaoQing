import asyncio

import pytest


@pytest.mark.asyncio
async def test_asyncio_marker_runs_without_external_plugin() -> None:
    await asyncio.sleep(0)
    assert asyncio.get_running_loop().is_running()
