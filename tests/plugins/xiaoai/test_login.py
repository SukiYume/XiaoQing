"""消息登录所有权、任务清理和失效提醒投递回执。"""

import asyncio
import json
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock

import aiohttp
import pytest
import pytest_asyncio

from core.delivery import SENSITIVE_ACTION_KEY
from plugins.xiaoai import auth, client, login, main
from tests.helpers.http import callback_result, mocked_aiohttp
from tests.helpers.xiaoai_test_support import (
    DEVICES,
    LOGIN,
    MINA_LIST,
    MINA_STS,
    POLL,
    QR,
    SPEC_ALL,
    SPEC_ONE,
    STS,
)

DIRECTIVE = {
    "services": [
        {
            "iid"       : 5,
            "properties": [
                {
                    "iid"   : 1,
                    "type"  : "urn:miot-spec-v2:property:text-content:abc",
                    "format": "string",
                },
                {
                    "iid"   : 2,
                    "type"  : "urn:miot-spec-v2:property:silent-execution:abc",
                    "format": "bool",
                },
            ],
            "actions": [
                {
                    "iid" : 5,
                    "type": "urn:miot-spec-v2:action:execute-text-directive:abc",
                    "in"  : [1, 2],
                }
            ],
        }
    ]
}


def _grant(sid):
    host = "sts.api.io.mi.com" if sid == "xiaomiio" else "api2.mina.mi.com"
    return {
        "code"     : 0,
        "userId"   : "42",
        "passToken": "private-token",
        "ssecurity": "a2V5",
        "nonce"    : "1",
        "location" : f"https://{host}/sts?ticket=private",
    }


class _Xiaomi:
    """小米账号（扫码登录、令牌续期）与米家云端（设备列表、规格）的 HTTP 替身。

    ``scanned`` 为真后轮询接口才给出授权；``renewal`` 为 ``"login_required"`` 时令牌续期被要求重新登录；
    ``device_error`` 不为空时设备列表请求抛出该异常。``renewals`` 记录每次续期请求的服务类型，
    ``device_requests`` 统计设备列表请求（米家与音箱两个接口）。
    """

    def __init__(self, mocked):
        self.scanned         = False
        self.renewal         = "ok"
        self.device_error    = None
        self.renewals        = []
        self.device_requests = 0
        mocked.get(LOGIN, callback=self._service_login, repeat=True)
        mocked.get(QR, callback=self._qr, repeat=True)
        mocked.get(POLL, callback=self._poll, repeat=True)
        mocked.get(STS, callback=self._service_token("io-new"), repeat=True)
        mocked.get(MINA_STS, callback=self._service_token("mina-new"), repeat=True)
        mocked.post(DEVICES, callback=self._device_list, repeat=True)
        mocked.get(MINA_LIST, callback=self._mina_list, repeat=True)
        mocked.get(SPEC_ALL, callback=self._spec_index, repeat=True)
        mocked.get(SPEC_ONE, callback=self._spec, repeat=True)

    @staticmethod
    def _reply(**kwargs):
        return callback_result(**kwargs)

    def _service_login(self, _url, **kwargs):
        sid     = kwargs["params"]["sid"]
        cookies = kwargs["cookies"]
        if "passToken" not in cookies:
            return self._reply(
                payload={
                    "code"    : 70016,
                    "qs"      : "encoded",
                    "callback": "https://sts.api.io.mi.com/sts",
                }
            )
        self.renewals.append(sid)
        if sid == "xiaomiio" and self.renewal == "login_required":
            return self._reply(payload={"code": 70016})
        return self._reply(payload=_grant(sid) | {"passToken": ""})

    def _qr(self, _url, **_kwargs):
        body = {
            "code"    : 0,
            "loginUrl": "https://ak.account.xiaomi.com/longPolling/login?ticket=secret-qr",
            "lp"      : "https://ak.lp.account.xiaomi.com/lp/s?ticket=private",
            "timeout" : 300,
        }
        return self._reply(body="&&&START&&&" + json.dumps(body))

    def _poll(self, _url, **_kwargs):
        return self._reply(payload=_grant("xiaomiio") if self.scanned else {"code": 0})

    @staticmethod
    def _service_token(value):
        def callback(_url, **_kwargs):
            return callback_result(
                headers = {"Set-Cookie": f"serviceToken={value}; Path=/"},
                body    = "ok",
            )

        return callback

    def _device_list(self, _url, **_kwargs):
        self.device_requests += 1
        if self.device_error is not None:
            raise self.device_error
        devices = [{"did": "123", "model": "lx06", "name": "卧室音箱"}]
        return self._reply(payload={"code": 0, "result": {"list": devices}})

    def _mina_list(self, _url, **_kwargs):
        self.device_requests += 1
        return self._reply(
            payload={"code": 0, "data": [{"miotDID": "123", "deviceID": "mina-123"}]}
        )

    def _spec_index(self, _url, **_kwargs):
        return self._reply(
            payload={"instances": [{"model": "lx06", "version": 1, "type": "urn:lx06"}]}
        )

    def _spec(self, _url, **_kwargs):
        return self._reply(payload=DIRECTIVE)


@pytest_asyncio.fixture
async def environment(tmp_path, monkeypatch):
    choices_sent = asyncio.Event()
    token        = {
        "userId"       : "42",
        "passToken"    : "private-token",
        "deviceId"     : "device",
        "xiaomiio"     : ["a2V5", "io"],
        "micoapi"      : ["a2V5", "mina"],
        "_refreshed_at": {sid: time.time() for sid in auth.SERVICES},
    }

    async def send(action):
        message = action["params"]["message"]
        if any(
            segment.get("data", {}).get("text", "").startswith("扫码授权已完成")
            for segment in message
        ):
            choices_sent.set()
        return True

    # 二维码未确认时每秒轮询一次；测试里缩短成 10 毫秒。
    real_sleep = asyncio.sleep

    async def quick_sleep(delay, *args):
        await real_sleep(min(delay, 0.01), *args)

    monkeypatch.setattr(asyncio, "sleep", quick_sleep)
    async with aiohttp.ClientSession() as session:
        with mocked_aiohttp() as mocked:
            context = SimpleNamespace(
                data_dir     = tmp_path,
                state        = {},
                http_session = session,
                send_action=AsyncMock(side_effect=send),
            )
            yield SimpleNamespace(
                context      = context,
                choices_sent = choices_sent,
                xiaomi       = _Xiaomi(mocked),
                token        = token,
                flows        = [],
            )


async def _start_login(env, user_id=100):
    """发起登录，并记下这次创建的扫码流程，供后面核对是否已关闭。"""

    reply   = await main.handle("xiaoai", "登录", {"user_id": user_id}, env.context)
    pending = login.runtime(env.context).pending
    if pending is not None and pending.flow not in env.flows:
        env.flows.append(pending.flow)
    return reply


def _closed(flow):
    return flow.session is None and not flow.poll_url and not flow.login_url


@pytest.mark.asyncio
async def test_message_login_selects_speaker_and_commits_only_after_owner_selection(environment):
    env     = environment
    context = env.context
    reply   = await _start_login(env)
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
    assert "另一位管理员" in str(await _start_login(env, 200))
    env.xiaomi.scanned = True
    await asyncio.wait_for(env.choices_sent.wait(), 5)
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
    assert env.flows and all(_closed(flow) for flow in env.flows)


@pytest.mark.asyncio
async def test_cancel_and_shutdown_close_waiter_without_writing_credentials(environment):
    env     = environment
    context = env.context
    await _start_login(env)
    first = login.runtime(context).pending.task
    await main.handle("xiaoai", "取消登录", {"user_id": 100}, context)
    assert first.done() and _closed(env.flows[0])
    await _start_login(env)
    await main.shutdown(context)
    assert _closed(env.flows[1]) and not login.runtime(context).pending
    assert not (context.data_dir / "token.json").exists()


@pytest.mark.asyncio
async def test_qr_delivery_failure_cleans_up(environment, monkeypatch):
    env        = environment
    closed     = []
    real_close = aiohttp.ClientSession.close

    async def tracked_close(session):
        closed.append(session)
        await real_close(session)

    monkeypatch.setattr(aiohttp.ClientSession, "close", tracked_close)
    env.context.send_action.side_effect = None
    env.context.send_action.return_value = False
    result = await main.handle("xiaoai", "登录", {"user_id": 100}, env.context)
    assert "发送失败" in str(result)
    assert not login.runtime(env.context).pending
    assert len(closed) == 1 and closed[0].closed


@pytest.mark.asyncio
async def test_selection_expiry_preserves_existing_credentials(environment):
    env     = environment
    context = env.context
    client.write_json(context.data_dir / "token.json", {"old": "preserve"})
    await _start_login(env)
    env.xiaomi.scanned = True
    await asyncio.wait_for(env.choices_sent.wait(), 5)
    login.runtime(context).pending.expires_at = time.monotonic() - 1
    result = await main.handle("xiaoai", "选择 1", {"user_id": 100}, context)
    assert "结束" in str(result)
    assert client.read_json(context.data_dir / "token.json") == {"old": "preserve"}
    await main.shutdown(context)


@pytest.mark.asyncio
async def test_notification_commits_only_after_delivery_and_retries_failure(environment):
    env     = environment
    context = env.context
    client.write_json(context.data_dir / "token.json", env.token)
    client.write_json(context.data_dir / "speaker.json", {"region": "cn", "notify_user_id": 100})
    manager = login.runtime(context).manager
    manager.mark_required(env.token)
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
async def test_transient_network_failures_produce_no_login_alert(environment):
    env     = environment
    context = env.context
    client.write_json(context.data_dir / "token.json", env.token)
    client.write_json(context.data_dir / "speaker.json", {"region": "cn", "notify_user_id": 100})
    env.xiaomi.device_error = TimeoutError()
    assert await main.scheduled_auth(context) == []
    assert env.xiaomi.device_requests == 1
    assert not login.runtime(context).manager.status()
    context.send_action.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", [TimeoutError(), client.ClientRejected()])
async def test_health_check_records_failure_and_recovery(environment, caplog, failure):
    env = environment
    client.write_json(env.context.data_dir / "token.json", env.token)
    client.write_json(
        env.context.data_dir / "speaker.json", {"region": "cn", "notify_user_id": 100}
    )
    caplog.set_level("INFO")
    env.xiaomi.device_error = failure
    assert await main.scheduled_auth(env.context) == []
    assert "cloud.health.failed" in caplog.text
    assert type(failure).__name__ in caplog.text
    assert not login.runtime(env.context).manager.status()
    env.xiaomi.device_error = None
    assert await main.scheduled_auth(env.context) == []
    assert "cloud.health.recovered" in caplog.text
    assert not env.context.state.get("xiaoai_health_failed")
    env.context.send_action.assert_not_awaited()


@pytest.mark.asyncio
async def test_selection_expiring_during_directory_setup_preserves_binding(
    environment, monkeypatch
):
    env     = environment
    context = env.context
    client.write_json(context.data_dir / "token.json", {"old": "preserve"})
    await _start_login(env)
    env.xiaomi.scanned = True
    await asyncio.wait_for(env.choices_sent.wait(), 5)

    def expire(path):
        login.runtime(context).pending.expires_at = time.monotonic() - 1

    monkeypatch.setattr(login, "protect_directory", expire)
    reply = await main.handle("xiaoai", "选择 1", {"user_id": 100}, context)
    assert "过期" in str(reply)
    assert client.read_json(context.data_dir / "token.json") == {"old": "preserve"}
    assert not (context.data_dir / "speaker.json").exists()
    await main.shutdown(context)


@pytest.mark.asyncio
async def test_schedule_rejects_partial_account_rebinding(environment):
    env = environment
    client.write_json(env.context.data_dir / "token.json", env.token)
    client.write_json(
        env.context.data_dir / "speaker.json",
        {
            "region"        : "cn",
            "notify_user_id": 200,
            "account_id"    : "other-account",
        },
    )
    manager = login.runtime(env.context).manager
    manager.mark_required(env.token)
    # 账号对不上时直接放弃；若继续往下走，已标记失效的令牌会给 200 号发出登录提醒。
    assert await main.scheduled_auth(env.context) == []
    assert not manager.status()["notified_at"]


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
    env = environment
    client.write_json(env.context.data_dir / "token.json", env.token)
    client.write_json(
        env.context.data_dir / "speaker.json",
        {
            "region"        : "cn",
            "notify_user_id": 100,
            "account_id"    : "42",
        },
    )
    env.xiaomi.renewal = "login_required" if expired else "ok"
    monkeypatch.setattr(client, "request_json", AsyncMock(return_value={"code": 401}))
    deliveries = await main.scheduled_auth(env.context)
    assert len(deliveries) == int(expired)
    assert bool(login.runtime(env.context).manager.status().get("needs_login")) == expired
    assert env.xiaomi.renewals == ["xiaomiio"]


@pytest.mark.asyncio
async def test_manual_renewal_updates_both_services_without_device_actions(environment):
    env = environment
    client.write_json(env.context.data_dir / "token.json", env.token)
    reply = await main.handle("xiaoai", "续期", {"user_id": 100}, env.context)
    assert "续期完成" in str(reply)
    assert env.xiaomi.renewals == list(auth.SERVICES)
    assert env.xiaomi.device_requests == 0
