from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock

import pytest

from core.server import InboundServer


@pytest.mark.asyncio
async def test_inbound_stop_closes_admission_and_drains_existing_handler() -> None:
    entered = asyncio.Event()
    release = asyncio.Event()

    async def handler(_payload):
        entered.set()
        await release.wait()
        return []

    server = InboundServer("127.0.0.1", 0, "token", handler)
    server._accepting_events = True
    server._site = MagicMock(stop=AsyncMock())
    server._runner = MagicMock(cleanup=AsyncMock())
    active = asyncio.create_task(server._invoke_handler({"post_type": "message"}))
    await entered.wait()

    stopping = asyncio.create_task(server.stop())
    await asyncio.sleep(0)
    assert server._accepting_events is False
    with pytest.raises(RuntimeError, match="shutting down"):
        await server._invoke_handler({})

    release.set()
    await stopping
    await active
    assert server._active_handler_tasks == set()
    assert server._site is None


@pytest.mark.asyncio
async def test_inbound_stop_cancels_handler_after_deadline() -> None:
    entered = asyncio.Event()

    async def handler(_payload):
        entered.set()
        await asyncio.Event().wait()
        return []

    server = InboundServer("127.0.0.1", 0, "token", handler)
    server._accepting_events = True
    server._handler_drain_timeout_seconds = 0.001
    active = asyncio.create_task(server._invoke_handler({}))
    await entered.wait()
    await server.stop()
    assert active.cancelled()
    assert server._active_handler_tasks == set()
