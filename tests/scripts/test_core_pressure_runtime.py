"""压测请求使用动作预览协议，保持本地验收的投递边界。"""

import pytest
from aiohttp import ClientSession, web
from aiohttp.test_utils import TestServer

from scripts.run_core_pressure import _post_event


@pytest.mark.asyncio
async def test_pressure_event_explicitly_collects_actions():
    """真实 HTTP 服务校验请求头，防止压测意外进入 QQ 回复通道。"""
    requests = []

    async def handler(request):
        assert request.headers["Authorization"] == "Bearer isolated-token"
        assert request.headers["X-XiaoQing-Response-Mode"] == "actions"
        requests.append(await request.json())
        return web.json_response({"actions": []})

    app = web.Application()
    app.router.add_post("/event", handler)
    async with TestServer(app) as server, ClientSession() as session:
        result = await _post_event(
            session, str(server.make_url("/event")), "isolated-token", {"message": "/echo pressure"}
        )
    assert result.status == 200
    assert result.error is None
    assert requests == [{"message": "/echo pressure"}]
