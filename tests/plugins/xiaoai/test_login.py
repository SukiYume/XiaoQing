"""消息登录所有权、任务清理和失效提醒投递回执。"""

import asyncio
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from core.delivery import SENSITIVE_ACTION_KEY
from plugins.xiaoai import auth, client, login, main


@pytest.fixture
def environment(tmp_path, monkeypatch):
    scanned      = asyncio.Event()
    choices_sent = asyncio.Event()
    token        = {
        "userId": "42",
        "passToken": "private-token",
        "deviceId": "device",
        "xiaomiio": ["key", "io"],
        "micoapi": ["key", "mina"],
        "_refreshed_at": {sid: time.time() for sid in auth.SERVICES},
    }
    instances = []

    class Flow:
        def __init__(self):
            self.login_url  = "https://account.xiaomi.com/longPolling/login?ticket=secret-qr"
            self.expires_at = time.monotonic() + 300
            self.closed     = False
            instances.append(self)

        async def start(self):
            pass

        async def wait(self):
            await scanned.wait()
            return token

        async def close(self):
            self.closed = True

    async def send(action):
        message = action["params"]["message"]
        if any(
            segment.get("data", {}).get("text", "").startswith("扫码授权已完成")
            for segment in message
        ):
            choices_sent.set()
        return True

    context = SimpleNamespace(
        data_dir     = tmp_path,
        state        = {},
        http_session = object(),
        send_action=AsyncMock(side_effect=send),
    )
    io = SimpleNamespace(
        device_list=AsyncMock(return_value=[{"did": "123", "model": "lx06", "name": "卧室音箱"}])
    )
    mina = SimpleNamespace(
        device_list=AsyncMock(return_value=[{"miotDID": "123", "deviceID": "mina-123"}])
    )
    monkeypatch.setattr(login, "QRFlow", Flow)
    monkeypatch.setattr(login, "services", lambda *args: (io, mina))
    monkeypatch.setattr(
        login,
        "fetch_spec",
        AsyncMock(return_value={"siid": 5, "aiid": 5, "inputs": ["text", "silent"]}),
    )
    monkeypatch.setattr(login, "protect_directory", lambda path: path.mkdir(exist_ok=True))
    monkeypatch.setattr(login, "directive_spec", lambda spec: spec)
    return SimpleNamespace(
        context      = context,
        scanned      = scanned,
        choices_sent = choices_sent,
        instances    = instances,
        token        = token,
        io           = io,
        mina         = mina,
    )


@pytest.mark.asyncio
async def test_message_login_selects_speaker_and_commits_only_after_owner_selection(environment):
    env     = environment
    context = env.context
    reply   = await main.handle("xiaoai", "登录", {"user_id": 100}, context)
    assert "二维码已发" in str(reply)
    action = context.send_action.await_args.args[0]
    assert action["params"]["user_id"] == 100
    assert action[SENSITIVE_ACTION_KEY] is True
    image = next(x for x in action["params"]["message"] if x["type"] == "image")
    assert image["data"]["file"].startswith("base64://")
    assert "secret-qr" not in str(reply)
    assert not (context.data_dir / "token.json").exists()
    assert "等待扫码" in str(await main.handle("xiaoai", "状态", {"user_id": 100}, context))
    assert "另一位管理员" in str(await main.handle("xiaoai", "取消登录", {"user_id": 200}, context))
    assert "另一位管理员" in str(await main.handle("xiaoai", "登录", {"user_id": 200}, context))
    env.scanned.set()
    await asyncio.wait_for(env.choices_sent.wait(), 2)
    assert not (context.data_dir / "token.json").exists()
    assert "另一位管理员" in str(await main.handle("xiaoai", "选择 1", {"user_id": 200}, context))
    assert "选择音箱" in str(await main.handle("xiaoai", "状态", {"user_id": 100}, context))
    reply = await main.handle("xiaoai", "选择 1", {"user_id": 100}, context)
    assert "自动续期" in str(reply)
    profile = client.read_json(context.data_dir / "speaker.json")
    assert profile["notify_user_id"] == 100
    assert profile["account_id"] == "42" and profile["did"] == "123"
    assert client.read_json(context.data_dir / "token.json")["passToken"] == "private-token"
    await main.shutdown(context)
    assert not login.runtime(context).tasks
    assert all(flow.closed for flow in env.instances)


@pytest.mark.asyncio
async def test_cancel_and_shutdown_close_waiter_without_writing_credentials(environment):
    env     = environment
    context = env.context
    await main.handle("xiaoai", "登录", {"user_id": 100}, context)
    first = login.runtime(context).pending.task
    await main.handle("xiaoai", "取消登录", {"user_id": 100}, context)
    assert first.done() and env.instances[0].closed
    await main.handle("xiaoai", "登录", {"user_id": 100}, context)
    await main.shutdown(context)
    assert env.instances[1].closed and not login.runtime(context).pending
    assert not (context.data_dir / "token.json").exists()


@pytest.mark.asyncio
async def test_qr_delivery_failure_cleans_up(environment):
    env = environment
    env.context.send_action.side_effect = None
    env.context.send_action.return_value = False
    result = await main.handle("xiaoai", "登录", {"user_id": 100}, env.context)
    assert "发送失败" in str(result)
    assert not login.runtime(env.context).pending
    assert env.instances[0].closed


@pytest.mark.asyncio
async def test_selection_expiry_preserves_existing_credentials(environment):
    env     = environment
    context = env.context
    client.write_json(context.data_dir / "token.json", {"old": "preserve"})
    await main.handle("xiaoai", "登录", {"user_id": 100}, context)
    env.scanned.set()
    await asyncio.wait_for(env.choices_sent.wait(), 2)
    login.runtime(context).pending.expires_at = time.monotonic() - 1
    result = await main.handle("xiaoai", "选择 1", {"user_id": 100}, context)
    assert "结束" in str(result)
    assert client.read_json(context.data_dir / "token.json") == {"old": "preserve"}
    await main.shutdown(context)


@pytest.mark.asyncio
async def test_notification_commits_only_after_delivery_and_retries_failure(
    environment, monkeypatch
):
    env     = environment
    context = env.context
    client.write_json(context.data_dir / "token.json", env.token)
    client.write_json(context.data_dir / "speaker.json", {"region": "cn", "notify_user_id": 100})
    manager = login.runtime(context).manager
    manager.mark_required(env.token)
    monkeypatch.setattr(manager, "ensure_fresh", AsyncMock(side_effect=client.AuthRequired()))
    deliveries = await main.scheduled_auth(context)
    assert len(deliveries) == 1
    assert deliveries[0].target.user_id == 100
    assert not manager.status()["notified_at"]
    await deliveries[0].receipt.record(False)
    again = await main.scheduled_auth(context)
    assert len(again) == 1
    await again[0].receipt.record(True)
    assert manager.status()["notified_at"] > 0
    assert await main.scheduled_auth(context) == []
    # 新授权清除提醒状态，旧回执回调保持新状态。
    client.write_json(context.data_dir / "auth-state.json", {})
    manager.mark_required(env.token)
    outstanding = await main.scheduled_auth(context)
    client.write_json(context.data_dir / "auth-state.json", {})
    await outstanding[0].receipt.record(True)
    assert manager.status() == {}


@pytest.mark.asyncio
async def test_transient_network_failures_produce_no_login_alert(environment, monkeypatch):
    env     = environment
    context = env.context
    client.write_json(context.data_dir / "token.json", env.token)
    client.write_json(context.data_dir / "speaker.json", {"region": "cn", "notify_user_id": 100})
    env.io.device_list.side_effect = TimeoutError()
    monkeypatch.setattr(main, "services", lambda *args: (env.io, env.mina))
    assert await main.scheduled_auth(context) == []
    assert not login.runtime(context).manager.status()
    context.send_action.assert_not_awaited()


@pytest.mark.asyncio
async def test_selection_expiring_during_directory_setup_preserves_binding(
    environment, monkeypatch
):
    env     = environment
    context = env.context
    client.write_json(context.data_dir / "token.json", {"old": "preserve"})
    await main.handle("xiaoai", "登录", {"user_id": 100}, context)
    env.scanned.set()
    await asyncio.wait_for(env.choices_sent.wait(), 2)

    def expire(path):
        login.runtime(context).pending.expires_at = time.monotonic() - 1

    monkeypatch.setattr(login, "protect_directory", expire)
    reply = await main.handle("xiaoai", "选择 1", {"user_id": 100}, context)
    assert "过期" in str(reply)
    assert client.read_json(context.data_dir / "token.json") == {"old": "preserve"}
    assert not (context.data_dir / "speaker.json").exists()
    await main.shutdown(context)


@pytest.mark.asyncio
async def test_schedule_rejects_partial_account_rebinding(environment, monkeypatch):
    env = environment
    client.write_json(env.context.data_dir / "token.json", env.token)
    client.write_json(
        env.context.data_dir / "speaker.json",
        {
            "region": "cn",
            "notify_user_id": 200,
            "account_id": "other-account",
        },
    )
    manager = login.runtime(env.context).manager
    manager.mark_required(env.token)
    probe = AsyncMock(side_effect=client.AuthRequired())
    monkeypatch.setattr(manager, "ensure_fresh", probe)
    assert await main.scheduled_auth(env.context) == []
    probe.assert_not_awaited()


@pytest.mark.asyncio
async def test_old_receipt_preserves_new_expiration_of_same_account(environment):
    env = environment
    client.write_json(env.context.data_dir / "token.json", env.token)
    client.write_json(
        env.context.data_dir / "speaker.json", {"region": "cn", "notify_user_id": 100}
    )
    manager = login.runtime(env.context).manager
    manager.mark_required(env.token)
    delivery = (await main.scheduled_auth(env.context))[0]
    client.write_json(env.context.data_dir / "auth-state.json", {})
    manager.mark_required(env.token)
    await delivery.receipt.record(True)
    assert not manager.status()["notified_at"]


@pytest.mark.asyncio
@pytest.mark.parametrize("expired", [False, True])
async def test_cloud_rejection_reminds_only_when_account_renewal_requires_login(
    environment, monkeypatch, expired
):
    env                      = environment
    env.token["xiaomiio"][0] = "a2V5"
    client.write_json(env.context.data_dir / "token.json", env.token)
    client.write_json(
        env.context.data_dir / "speaker.json",
        {
            "region": "cn",
            "notify_user_id": 100,
            "account_id": "42",
        },
    )
    monkeypatch.setattr(client, "request_json", AsyncMock(return_value={"code": 401}))
    refresh = AsyncMock(return_value=env.token)
    if expired:
        refresh.side_effect = client.AuthRequired()
    monkeypatch.setattr(auth, "refresh_service", refresh)
    deliveries = await main.scheduled_auth(env.context)
    assert len(deliveries) == int(expired)
    assert bool(login.runtime(env.context).manager.status().get("needs_login")) == expired
    refresh.assert_awaited_once()


@pytest.mark.asyncio
async def test_manual_renewal_updates_both_services_without_device_actions(
    environment, monkeypatch
):
    env = environment
    client.write_json(env.context.data_dir / "token.json", env.token)
    refresh = AsyncMock(return_value=env.token)
    monkeypatch.setattr(auth, "refresh_service", refresh)
    reply = await main.handle("xiaoai", "续期", {"user_id": 100}, env.context)
    assert "续期完成" in str(reply)
    assert [call.args[2] for call in refresh.await_args_list] == list(auth.SERVICES)
    env.io.device_list.assert_not_awaited()
    env.mina.device_list.assert_not_awaited()
