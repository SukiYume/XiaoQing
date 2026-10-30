"""小爱桥接的单次提交、答复归属和型号协议回归。"""

import asyncio
import json
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from plugins.xiaoai import client, main


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "command_text",
    [
        "设备 extra",
        "数据 extra",
        "刷新设备 extra",
        "续期 extra",
        "状态 extra",
        "检查 extra",
        "帮助 extra",
        "取消登录 extra",
        "接口",
        "调用",
        "天气城市",
        "份量",
    ],
)
async def test_explicit_command_mistakes_return_help_before_cloud_or_planner(command_text):
    reply = await main.handle("xiaoai", command_text, {}, SimpleNamespace())
    assert "/help 米家 " + command_text.split()[0] in reply[0]["data"]["text"]


@pytest.mark.asyncio
async def test_shared_spec_fetch_reuses_index_and_selects_latest(monkeypatch):
    request = AsyncMock(return_value={"services": []})
    monkeypatch.setattr(client, "request_json", request)
    index = [
        {"model": "test.model", "version": 1, "type": "urn:old"},
        {"model": "test.model", "version": 2, "type": "urn:new"},
    ]
    assert await client.fetch_spec(None, "test.model", index) == {"type": "urn:new", "services": []}
    request.assert_awaited_once_with(
        None, "GET", client.SPEC_ROOT + "instance", params={"type": "urn:new"}
    )
    with pytest.raises(client.BridgeError, match="暂无公开规格"):
        await client.fetch_spec(None, "missing.model", index)
    assert request.await_count == 1


@pytest.mark.parametrize("account,recipient", [("old", 123), ("new", None)])
def test_shared_binding_preserves_notification_owner_only_for_same_account(
    tmp_path, account, recipient
):
    client.write_json(tmp_path / "speaker.json", {"account_id": "old", "notify_user_id": 123})
    client.save_binding(tmp_path, {"userId": account}, {"name": "音箱"})
    profile = client.read_json(tmp_path / "speaker.json")
    assert profile["account_id"] == account
    assert profile.get("notify_user_id") == recipient
    assert client.read_json(tmp_path / "token.json") == {"userId": account}
    assert client.read_json(tmp_path / "auth-state.json") == {}


def test_shared_journal_enforces_capacity_including_message_identity(tmp_path):
    ctx = SimpleNamespace(data_dir=tmp_path)
    path = tmp_path / "requests.json"
    client.write_json(path, {str(i): time.time() for i in range(999)})
    before = path.read_bytes()
    with pytest.raises(client.BridgeError, match="上限"):
        client.reserve_request(ctx, {"user_id": 1, "message_id": 2}, "requests.json", ["new"])
    assert path.read_bytes() == before
    assert client.reserve_request(ctx, {}, "requests.json", []) is True
    assert path.read_bytes() == before


@pytest.fixture
def env(tmp_path, monkeypatch):
    profile = {
        "name"     : "卧室音箱",
        "model"    : "xiaomi.wifispeaker.lx06",
        "region"   : "cn",
        "did"      : "123",
        "mina_id"  : "mina-123",
        "silent"   : True,
        "directive": {"siid": 5, "aiid": 5, "inputs": ["text", "silent"]},
    }
    client.write_json(tmp_path / "speaker.json", profile)
    client.write_json(
        tmp_path / "token.json",
        {
            "userId"       : "secret",
            "_refreshed_at": {"xiaomiio": time.time(), "micoapi": time.time()},
        },
    )
    io = SimpleNamespace(
        device_list=AsyncMock(
            return_value=[{"did": "123", "model": profile["model"], "isOnline": True}]
        ),
        miot_action=AsyncMock(return_value=0),
    )
    mina = SimpleNamespace(get_latest_ask=AsyncMock(side_effect=RuntimeError("sensitive token")))
    monkeypatch.setattr(main, "services", lambda *args: (io, mina))
    context = SimpleNamespace(data_dir=tmp_path, http_session=object(), state={})
    return context, io, mina


@pytest.mark.asyncio
async def test_exact_directive_and_persistent_duplicate_guard(env):
    context, io, _ = env
    event = {"message_id": 1, "user_id": 42}
    assert "已提交" in await main.execute(context, "打开台灯", event)
    io.miot_action.assert_awaited_once_with("123", (5, 5), ["打开台灯", True])
    context.state.clear()
    assert "重复保护" in await main.execute(context, "打开台灯", {"message_id": 2})
    assert "重复保护" in await main.execute(context, "开台灯", event)
    assert io.miot_action.await_count == 1


@pytest.mark.asyncio
async def test_duplicate_text_allows_new_message_at_twenty_seconds(env, monkeypatch):
    context, io, _ = env
    now = time.time()
    monkeypatch.setattr(main.time, "time", lambda: now)
    assert "已提交" in await main.execute(context, "开台灯", {"message_id": 1})
    now += 19.9
    assert "20 秒" in await main.execute(context, "开台灯", {"message_id": 2})
    now += 0.1
    assert "已提交" in await main.execute(context, "开台灯", {"message_id": 2})
    assert io.miot_action.await_count == 2
    now += 21
    assert "重复保护" in await main.execute(context, "开台灯", {"message_id": 2})
    assert io.miot_action.await_count == 2


@pytest.mark.asyncio
async def test_timeout_is_ambiguous_and_never_retried(env):
    context, io, _ = env
    io.miot_action.side_effect = TimeoutError("password token secret")
    result                     = await main.execute(context, "打开台灯", {})
    assert "待确认" in result and "secret" not in result
    assert "重复保护" in await main.execute(context, "打开台灯", {})
    assert io.miot_action.await_count == 1


@pytest.mark.asyncio
async def test_cancellation_retains_journal(env):
    context, io, _ = env
    io.miot_action.side_effect = asyncio.CancelledError()
    with pytest.raises(asyncio.CancelledError):
        await main.execute(context, "打开台灯", {})
    assert "重复保护" in await main.execute(context, "打开台灯", {})


@pytest.mark.asyncio
async def test_check_is_read_only_and_offline_blocks(env):
    context, io, _ = env
    assert "正常" in await main.execute(context, "检查", {}, check=True)
    assert not (context.data_dir / "requests.json").exists()
    io.device_list.return_value[0]["isOnline"] = False
    with pytest.raises(client.BridgeError, match="离线"):
        await main.execute(context, "开灯", {})
    io.miot_action.assert_not_awaited()


def test_answers_require_new_id_time_and_matching_question():
    now     = int(time.time() * 1000)
    message = {
        "request_id"  : "new",
        "timestamp_ms": now,
        "response"    : {
            "answer": [
                {"question": "家里温湿度是多少？", "content": "26 度，50%"},
            ]
        },
    }
    assert main.matching_answer([message], "家里温湿度是多少", now, set()) == "26 度，50%"
    assert main.matching_answer([message], "家里温湿度是多少", now, {"new"}) == ""
    assert main.matching_answer([message], "家里温湿度是多少", now + 1, set()) == ""
    assert main.matching_answer([message], "开灯", now, set()) == ""


def test_spec_uses_semantics_and_input_order():
    spec = {
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
                    {"iid": 4, "type": "urn:miot-spec-v2:action:play-radio:abc", "in": []},
                    {
                        "iid" : 5,
                        "type": "urn:miot-spec-v2:action:execute-text-directive:abc",
                        "in"  : [2, 1],
                    },
                ],
            }
        ]
    }
    assert client.directive_spec(spec) == {"siid": 5, "aiid": 5, "inputs": ["silent", "text"]}
    spec["services"][0]["properties"][1]["format"] = "uint8"
    with pytest.raises(client.BridgeError):
        client.directive_spec(spec)


@pytest.mark.asyncio
async def test_account_single_request_no_retry_and_no_sensitive_errors(monkeypatch):
    request = AsyncMock(return_value={"code": 401, "message": "secret password"})
    monkeypatch.setattr(client, "request_json", request)
    account = client.TokenAccount(object(), {"userId": "u", "xiaomiio": ["key", "token"]})
    with pytest.raises(client.BridgeError) as raised:
        await account.mi_request("xiaomiio", "https://api.io.mi.com/app/miotspec/action", {}, {})
    assert "secret" not in str(raised.value)
    assert request.await_count == 1
    with pytest.raises(client.BridgeError):
        await account.mi_request("xiaomiio", "https://evil.example/action", {}, {})
    assert request.await_count == 1


@pytest.mark.asyncio
async def test_handler_help_status_and_busy(env):
    context, io, _ = env
    assert "配置已就绪" in str(await main.handle("xiaoai", "状态", {}, context))
    assert "查看设备" in str(await main.handle("xiaoai", "执行", {}, context))
    context.state["xiaoai_lock"] = asyncio.Lock()
    async with context.state["xiaoai_lock"]:
        assert "正在处理" in str(await main.handle("xiaoai", "开灯", {}, context))
    io.miot_action.assert_not_awaited()


def test_manifest_limits_control_to_private_admin():
    from pathlib import Path

    manifest = json.loads(
        (Path(main.__file__).with_name("plugin.json")).read_text(encoding="utf-8")
    )
    for command in manifest["commands"]:
        assert command["admin_only"] is True
        assert command["contexts"] == ["private"]


@pytest.mark.asyncio
async def test_real_library_protocol_uses_cloud_once():
    import aiohttp
    from aioresponses import aioresponses

    token = {"userId": "u", "deviceId": "dev", "xiaomiio": ["a2V5", "token"]}
    url   = "https://api.io.mi.com/app/miotspec/action"
    async with aiohttp.ClientSession() as session:
        io, _ = client.services(session, token, "cn")
        with aioresponses() as mocked:
            mocked.post(url, payload={"code": 0, "result": {"code": 0}})
            assert await io.miot_action("123", (5, 5), ["打开台灯", True]) == 0
            calls = [call for entries in mocked.requests.values() for call in entries]
            assert len(calls) == 1
            payload = json.loads(calls[0].kwargs["data"]["data"])
            assert payload["params"] == {
                "did" : "123",
                "siid": 5,
                "aiid": 5,
                "in"  : ["打开台灯", True],
            }
            assert calls[0].kwargs["allow_redirects"] is False


@pytest.mark.asyncio
async def test_polling_returns_only_current_answer(env, monkeypatch):
    context, io, mina = env
    mina.get_latest_ask.side_effect = [
        [],
        [
            {
                "request_id"  : "fresh",
                "timestamp_ms": int(time.time() * 1000) + 100,
                "response"    : {"answer": [{"question": "温度多少", "content": "25度"}]},
            }
        ],
    ]
    monkeypatch.setattr(main.asyncio, "sleep", AsyncMock())
    assert "小爱回复：25度" in await main.execute(context, "温度多少", {})
    assert io.miot_action.await_count == 1


@pytest.mark.asyncio
async def test_setup_binds_miot_id_and_keeps_password_in_memory(tmp_path, monkeypatch):
    import miservice

    from plugins.xiaoai import setup

    class Account:
        def __init__(self, session, username, password, token_store, otp_callback):
            assert token_store is None
            assert password == "test-password"
            self.token = {"userId": "u", "xiaomiio": ["key", "token"], "micoapi": ["key", "token"]}

        async def login(self, sid):
            return True

    monkeypatch.setattr(miservice, "MiAccount", Account)
    answers = iter(["test-user", "1"])
    monkeypatch.setattr("builtins.input", lambda _: next(answers))
    monkeypatch.setattr(setup.getpass, "getpass", lambda _: "test-password")
    monkeypatch.setattr(setup.logging, "disable", lambda _: None)
    monkeypatch.setattr(setup, "protect_directory", lambda p: p.mkdir(exist_ok=True))
    io = SimpleNamespace(
        device_list=AsyncMock(
            return_value=[
                {"did": 123, "model": "xiaomi.wifispeaker.lx06", "name": "卧室"},
            ]
        )
    )
    mina = SimpleNamespace(
        device_list=AsyncMock(return_value=[{"miotDID": "123", "deviceID": "mina"}])
    )
    monkeypatch.setattr(setup, "services", lambda *args: (io, mina))
    monkeypatch.setattr(
        setup,
        "fetch_spec",
        AsyncMock(return_value={"siid": 5, "aiid": 5, "inputs": ["text", "silent"]}),
    )
    monkeypatch.setattr(setup, "directive_spec", lambda spec: spec)
    await setup.setup(tmp_path, "cn", False)
    profile = client.read_json(tmp_path / "speaker.json")
    assert profile["did"] == "123" and profile["mina_id"] == "mina"
    assert profile["silent"] is True
    assert "test-password" not in (tmp_path / "token.json").read_text()
