"""Device discovery, validated plans, infrared boundaries and natural-language routing."""

import asyncio
import json
import time
from copy import deepcopy
from types import SimpleNamespace
from unittest.mock import AsyncMock

import aiohttp
import pytest
import pytest_asyncio

from plugins.xiaoai import catalog, client, home, main, operations, planner, weather
from tests.helpers.http import callback_result, mocked_aiohttp
from tests.helpers.xiaoai_test_support import (
    ACTION,
    DEVICES,
    PROP_GET,
    PROP_SET,
    SPEC_ALL,
    SPEC_ONE,
    UBUS,
)

LAMP    = catalog.device_key("123")
FEEDER  = "xiaomi.feeder.pi2001"
SPEAKER = "xiaomi.wifispeaker.lx06"


def prop(iid, name, fmt="uint8", access=None, **kwargs):
    return {
        "iid"        : iid,
        "type"       : f"urn:miot-spec-v2:property:{name}:0000:test:1",
        "description": name,
        "format"     : fmt,
        "access"     : access if access is not None else ["read", "write"],
        **kwargs,
    }


def action(iid, name, inputs=None):
    return {
        "iid"        : iid,
        "type"       : f"urn:miot-spec-v2:action:{name}:0000:test:1",
        "description": name,
        "in"         : inputs or [],
        "out"        : [],
    }


@pytest.fixture
def env(tmp_path, monkeypatch):
    spec = {
        "services": [
            {
                "iid"        : 2,
                "type"       : "urn:miot-spec-v2:service:light:0000:test:1",
                "description": "light",
                "properties" : [
                    prop(1, "on", "bool"),
                    prop(2, "brightness", **{"value-range": [1, 100, 1]}),
                    prop(8, "feeding-measure", access=[], **{"value-range": [0, 150, 1]}),
                ],
                "actions": [action(1, "pet-food-out", [8]), action(2, "turn-off")],
                "events" : [{"iid": 1, "description": "finished", "arguments": [8]}],
            }
        ]
    }
    model  = "test.device"
    device = {"did": "123", "name": "台灯", "model": model, "online": True, "room": ""}
    cat    = {
        "devices": {"d1": device},
        "specs"  : {model: {"capabilities": catalog.normalize_spec(spec)}},
    }
    io = SimpleNamespace(
        miot_set_props=AsyncMock(return_value=[0]),
        miot_action=AsyncMock(return_value=0),
        miot_request=AsyncMock(
            return_value=[
                {
                    "did"       : "123",
                    "siid"      : 2,
                    "piid"      : 2,
                    "value"     : 100,
                    "code"      : 0,
                    "updateTime": time.time(),
                }
            ]
        ),
        device_list=AsyncMock(
            return_value=[{"did": "123", "name": "台灯", "model": model, "isOnline": True}]
        ),
    )
    ai = SimpleNamespace(complete=AsyncMock())
    context = SimpleNamespace(
        data_dir=tmp_path, state={}, http_session=object(), capabilities=SimpleNamespace(ai=ai)
    )
    monkeypatch.setattr(operations.asyncio, "sleep", AsyncMock())
    return context, io, cat, ai, spec


def plan(op="set", capability="p2.2", **extra):
    return {"operations": [{"device": "d1", "capability": capability, "op": op, **extra}]}


def test_action_parameters_are_resolved_without_exposing_write(env):
    caps = catalog.capabilities(env[2], env[2]["devices"]["d1"])
    assert "p2.8" not in caps
    assert caps["a2.1"]["inputs"][0]["range"] == [0, 150, 1]
    assert caps["e2.1"]["arguments"][0]["name"] == "feeding-measure"


@pytest.mark.parametrize("value", [0, 101, True, "100", 1.5, float("nan"), None])
def test_property_value_bounds(env, value):
    with pytest.raises(client.BridgeError):
        operations.validate_plan(env[2], plan(value=value))


@pytest.mark.parametrize(
    "value,valid", [(16, True), (26.0, True), (26.5, False), (31, False), (True, False)]
)
def test_infrared_temperature_step(value, valid):
    schema = {"name": "temperature", "format": "float", "range": [16, 30, 1]}
    if valid:
        operations.validate_value(schema, value)
    else:
        with pytest.raises(client.BridgeError):
            operations.validate_value(schema, value)


@pytest.mark.asyncio
async def test_entire_plan_validates_before_any_action(env):
    ctx, io, cat, _, _ = env
    p = plan("action", "a2.1", args=[10])
    p["operations"].append({"device": "d1", "capability": "p2.2", "op": "set", "value": 101})
    with pytest.raises(client.BridgeError):
        await operations.execute_plan(ctx, io, cat, p, {})
    io.miot_action.assert_not_awaited()
    assert not (ctx.data_dir / "device-requests.json").exists()


@pytest.mark.asyncio
async def test_zero_input_action_defaults_to_empty_args_and_keeps_duplicate_identity(env):
    ctx, io, cat, _, _ = env
    request = plan("action", "a2.2")
    result  = await operations.execute_plan(ctx, io, cat, request, {"message_id": 1})
    assert "指令已提交" in result and "设备状态：待确认" in result
    io.miot_action.assert_awaited_once_with("123", (2, 2), [])
    assert "args" not in request["operations"][0]
    repeated = await operations.execute_plan(
        ctx,
        io,
        cat,
        plan("action", "a2.2", args=[]),
        {"message_id": 2},
    )
    assert "重复保护" in repeated
    io.miot_action.assert_awaited_once()


@pytest.mark.asyncio
async def test_missing_required_action_args_prevents_every_write(env):
    ctx, io, cat, _, _ = env
    request = plan(value=30)
    request["operations"].extend(plan("action", "a2.1")["operations"])
    with pytest.raises(client.BridgeError):
        await operations.execute_plan(ctx, io, cat, request, {})
    io.miot_set_props.assert_not_awaited()
    io.miot_action.assert_not_awaited()
    assert not (ctx.data_dir / "device-requests.json").exists()


@pytest.mark.asyncio
async def test_set_and_readback(env):
    ctx, io, cat, _, _ = env
    text = await operations.execute_plan(ctx, io, cat, plan(value=100), {"message_id": 1})
    io.miot_set_props.assert_awaited_once_with("123", [(2, 2, 100)])
    assert "100" in text and "云端最近回报" in text


@pytest.mark.asyncio
async def test_canonical_operation_duplicate_and_message_guard(env):
    ctx, io, cat, _, _ = env
    p = plan("action", "a2.1", args=[10])
    await operations.execute_plan(ctx, io, cat, p, {"message_id": 1})
    p["operations"][0]["device"] = "台灯"
    assert "重复保护" in await operations.execute_plan(ctx, io, cat, p, {"message_id": 2})
    assert "重复保护" in await operations.execute_plan(
        ctx, io, cat, plan("action", "a2.2", args=[]), {"message_id": 1}
    )
    io.miot_action.assert_awaited_once()


@pytest.mark.asyncio
async def test_partial_failure_and_timeout_stop_following_actions(env, caplog):
    ctx, io, cat, _, _ = env
    io.miot_action.side_effect = TimeoutError("secret-token")
    p = plan("action", "a2.1", args=[10])
    p["operations"].append({"device": "d1", "capability": "a2.2", "op": "action", "args": []})
    result = await operations.execute_plan(ctx, io, cat, p, {})
    assert "待确认" in result and "secret" not in result
    assert "重复保护" in await operations.execute_plan(ctx, io, cat, p, {})
    io.miot_action.assert_awaited_once()
    assert "device.operation.failed" in caplog.text
    assert "TimeoutError" in caplog.text
    assert "secret-token" not in caplog.text


@pytest.mark.asyncio
async def test_readback_failure_is_logged_after_single_submission(env, caplog):
    ctx, io, cat, _, _ = env
    io.miot_request.side_effect = TimeoutError("secret-readback")
    result = await operations.execute_plan(ctx, io, cat, plan(value=100), {})
    assert "状态读取未完成" in result
    io.miot_set_props.assert_awaited_once()
    assert "device.readback.failed" in caplog.text
    assert "secret-readback" not in caplog.text


@pytest.mark.asyncio
async def test_cancel_preserves_submission_record(env):
    ctx, io, cat, _, _ = env
    io.miot_action.side_effect = asyncio.CancelledError()
    p = plan("action", "a2.1", args=[10])
    with pytest.raises(asyncio.CancelledError):
        await operations.execute_plan(ctx, io, cat, p, {})
    assert "重复保护" in await operations.execute_plan(ctx, io, cat, p, {})


@pytest.mark.asyncio
async def test_stale_readback_stops_composite_change(env):
    ctx, io, cat, _, _ = env
    io.miot_request.return_value[0]["value"] = 50
    p = plan(value=100)
    p["operations"].append({"device": "d1", "capability": "a2.2", "op": "action", "args": []})
    assert "尚未达到目标" in await operations.execute_plan(ctx, io, cat, p, {})
    io.miot_action.assert_not_awaited()


@pytest.mark.asyncio
async def test_read_only_queries_never_create_action_journal(env):
    ctx, io, cat, _, _ = env
    await operations.execute_plan(ctx, io, cat, plan("read"), {})
    assert not (ctx.data_dir / "device-requests.json").exists()
    io.miot_set_props.assert_not_awaited()


@pytest.mark.parametrize(
    "op",
    [
        plan("read", "e2.1"),
        plan("set", "p2.8", value=1),
        plan("action", "a2.1", args=[]),
        plan("action", "a2.1", args=[True]),
    ],
)
def test_illegal_access_and_action_schema(env, op):
    with pytest.raises(client.BridgeError):
        operations.validate_plan(env[2], op)


@pytest.mark.asyncio
async def test_discovery_caches_specs_but_updates_membership(env, monkeypatch):
    ctx, io, _, _, spec = env
    request = AsyncMock(
        side_effect=[
            {"instances": [{"model": "test.device", "version": 1, "type": "urn:test"}]},
            spec,
        ]
    )
    monkeypatch.setattr(catalog, "request_json", request)
    monkeypatch.setattr(client, "request_json", request)
    first = await catalog.discover(ctx, io, "account1", "cn")
    key   = catalog.device_key("123")
    assert first["devices"][key]["name"] == "台灯"
    io.device_list.return_value[0]["name"] = "新名称"
    second                                 = await catalog.discover(ctx, io, "account1", "cn")
    assert second["devices"][key]["name"] == "新名称"
    assert request.await_count == 2
    io.device_list.return_value = []
    third                       = await catalog.discover(ctx, io, "account1", "cn")
    assert third["devices"] == {}


@pytest.mark.asyncio
async def test_discovery_is_account_and_region_scoped(env, monkeypatch):
    ctx, io, _, _, spec = env
    entry = {"instances": [{"model": "test.device", "version": 1, "type": "urn:test"}]}
    request = AsyncMock(side_effect=[entry, spec, entry, spec, entry, spec])
    monkeypatch.setattr(catalog, "request_json", request)
    monkeypatch.setattr(client, "request_json", request)
    await catalog.discover(ctx, io, "a", "cn")
    await catalog.discover(ctx, io, "b", "cn")
    await catalog.discover(ctx, io, "b", "de")
    assert request.await_count == 6


@pytest.mark.asyncio
async def test_unknown_model_remains_in_inventory(env, monkeypatch):
    ctx, io, _, _, _ = env
    monkeypatch.setattr(catalog, "request_json", AsyncMock(return_value={"instances": []}))
    result = await catalog.discover(ctx, io, "a", "cn")
    assert "暂无公开规格" in catalog.inventory(result)


@pytest.mark.asyncio
async def test_ir_swing_forces_silent_and_reports_unverified(env):
    ctx, io, cat, _, _ = env
    cat["devices"]["d1"]["model"]          = "miir.aircondition.ir02"
    cat["specs"]["miir.aircondition.ir02"] = {"capabilities": {}}
    profile                                = {
        "did"      : "speaker",
        "silent"   : False,
        "directive": {"siid": 5, "aiid": 5, "inputs": ["text", "silent"]},
    }
    output = await operations.execute_plan(
        ctx, io, cat, plan("action", "x.swing", args=[True]), {}, profile
    )
    io.miot_action.assert_awaited_once_with("speaker", (5, 5), ["打开台灯扫风", True])
    assert "静默转发" in output and "待确认" in output
    io.miot_request.assert_not_awaited()


def test_ir_light_only_exposes_power(env):
    cat                             = env[2]
    cat["devices"]["d1"]["model"]   = "miir.light.ir01"
    cat["specs"]["miir.light.ir01"] = cat["specs"]["test.device"]
    caps                            = catalog.capabilities(cat, cat["devices"]["d1"])
    assert set(caps) == {"a2.2"}


@pytest.mark.asyncio
async def test_natural_language_uses_two_phase_plan(env):
    ctx, _, cat, ai, _ = env
    ai.complete.side_effect = [
        SimpleNamespace(content=json.dumps({"devices": ["d1"]})),
        SimpleNamespace(content=json.dumps(plan(value=100))),
    ]
    assert await planner.plan(ctx, "台灯亮度调最高", cat, {}) == plan(value=100)
    assert ai.complete.await_count == 2


@pytest.mark.asyncio
async def test_plan_reads_the_object_out_of_a_fenced_or_wrapped_reply(env):
    ctx, _, cat, ai, _ = env
    ai.complete.side_effect = [
        SimpleNamespace(content="```json\n" + json.dumps({"devices": ["d1"]}) + "\n```"),
        SimpleNamespace(content="计划如下：" + json.dumps(plan(value=100)) + "。"),
    ]
    assert await planner.plan(ctx, "台灯亮度调最高", cat, {}) == plan(value=100)


@pytest.mark.asyncio
@pytest.mark.parametrize("content", ["没有 JSON", "[1, 2]", ""])
async def test_plan_reply_without_an_object_is_a_bridge_error(env, content):
    ctx, _, cat, ai, _ = env
    ai.complete.side_effect = [SimpleNamespace(content=content)]
    with pytest.raises(client.BridgeError, match="自然语言解析暂未完成"):
        await planner.plan(ctx, "台灯亮度调最高", cat, {})


@pytest.mark.asyncio
async def test_unconfigured_portion_rejects_model_guess(env):
    ctx, io, cat, ai, _ = env
    ai.complete.side_effect = [
        SimpleNamespace(content=json.dumps({"devices": ["d1"]})),
        SimpleNamespace(content=json.dumps(plan("action", "a2.1", args=[1]))),
    ]
    output = await planner.plan(ctx, "出一份粮", cat, {})
    with pytest.raises(client.BridgeError, match="克数"):
        await operations.execute_plan(ctx, io, cat, output, {}, original_request="出一份粮")
    io.miot_action.assert_not_awaited()


@pytest.mark.asyncio
async def test_weather_bypasses_device_plan(env):
    ctx, _, cat, ai, _ = env
    value = {"weather": {"city": "北京", "days": 1}}
    ai.complete.return_value = SimpleNamespace(content=json.dumps(value))
    assert await planner.plan(ctx, "北京今天天气", cat, {}) == value
    ai.complete.assert_awaited_once()


@pytest.mark.asyncio
@pytest.mark.parametrize("amount,expected", [(10, True), (1, False)])
async def test_portion_uses_configured_grams(env, amount, expected):
    ctx, io, cat, ai, _ = env
    cat["feeding_grams"]    = {"d1": 10}
    ai.complete.side_effect = [
        SimpleNamespace(content=json.dumps({"devices": ["d1"]})),
        SimpleNamespace(content=json.dumps(plan("action", "a2.1", args=[amount]))),
    ]
    result = await planner.plan(ctx, "出一份粮", cat, {"feeding_grams": {"d1": 10}})
    if expected:
        await operations.execute_plan(ctx, io, cat, result, {}, original_request="出一份粮")
        io.miot_action.assert_awaited_once_with("123", (2, 1), [10])
    else:
        with pytest.raises(client.BridgeError, match="出粮已拦截"):
            await operations.execute_plan(ctx, io, cat, result, {}, original_request="出一份粮")
        io.miot_action.assert_not_awaited()


@pytest.mark.asyncio
async def test_model_cannot_invent_capability(env):
    ctx, io, cat, ai, _ = env
    ai.complete.side_effect = [
        SimpleNamespace(content=json.dumps({"devices": ["d1"]})),
        SimpleNamespace(content=json.dumps(plan("action", "a99.1", args=[]))),
    ]
    result = await planner.plan(ctx, "打开灯", cat, {})
    with pytest.raises(client.BridgeError):
        await operations.execute_plan(ctx, io, cat, result, {}, original_request="打开灯")
    io.miot_action.assert_not_awaited()


@pytest.mark.asyncio
async def test_model_plan_stays_within_selected_devices(env):
    ctx, _, cat, ai, _ = env
    cat["devices"]["other"] = cat["devices"]["d1"] | {"name": "其他设备", "did": "456"}
    ai.complete.side_effect = [
        SimpleNamespace(content=json.dumps({"devices": ["d1"]})),
        SimpleNamespace(
            content=json.dumps(
                {
                    "operations": [
                        {"device": "其他设备", "capability": "p2.2", "op": "set", "value": 100}
                    ]
                }
            )
        ),
    ]
    with pytest.raises(client.BridgeError, match="选择范围外"):
        await planner.plan(ctx, "台灯亮度调最高", cat, {})


@pytest.mark.asyncio
async def test_weather_location_and_forecast(env, monkeypatch):
    ctx     = env[0]
    request = AsyncMock(
        side_effect=[
            {
                "results": [
                    {"name": "北京", "latitude": 39.9, "longitude": 116.4, "country": "中国"}
                ]
            },
            {
                "timezone": "Asia/Shanghai",
                "daily"   : {
                    "time"                         : ["2026-10-26"],
                    "weather_code"                 : [0],
                    "temperature_2m_min"           : [10],
                    "temperature_2m_max"           : [20],
                    "precipitation_probability_max": [0],
                },
            },
        ]
    )
    monkeypatch.setattr(weather, "request_json", request)
    result = await weather.lookup(ctx, "北京")
    assert "2026-10-26" in result and "10–20°C" in result and "Open-Meteo" in result
    assert request.call_args_list[1].kwargs["params"]["timezone"] == "auto"


@pytest.mark.asyncio
async def test_weather_missing_city_is_explicit(env):
    with pytest.raises(client.BridgeError, match="城市"):
        await weather.lookup(env[0], "")


def test_portion_preferences_are_scoped_and_validated(env):
    ctx, _, cat, _, _ = env
    settings = home.preferences(ctx, "a")
    home.save_preference(ctx, settings, "份量 d1 10", cat)
    assert home.preferences(ctx, "a")["feeding_grams"]["d1"] == 10
    assert "feeding_grams" not in home.preferences(ctx, "b")
    with pytest.raises(client.BridgeError):
        home.save_preference(ctx, settings, "份量 d1 200", cat)


@pytest.mark.asyncio
async def test_default_handler_routes_to_home_explicit_keeps_speaker(env):
    ctx, _io, _cat, ai, spec = env
    speaker = {
        "name"      : "卧室音箱",
        "model"     : SPEAKER,
        "region"    : "cn",
        "did"       : "spk",
        "mina_id"   : "mina-1",
        "silent"    : True,
        "directive" : {"siid": 5, "aiid": 5, "inputs": ["text", "silent"]},
        "account_id": "u",
    }
    client.write_json(ctx.data_dir / "speaker.json", speaker)
    client.write_json(ctx.data_dir / "token.json", _token("u"))
    devices = [
        {"did": "123", "name": "台灯", "model": "test.device", "isOnline": True},
        {"did": "spk", "name": "卧室音箱", "model": SPEAKER, "isOnline": True},
    ]
    ai.complete.side_effect = [
        SimpleNamespace(content=json.dumps({"devices": [LAMP]})),
        SimpleNamespace(content=json.dumps(plan(value=100, device=LAMP))),
    ]
    async with aiohttp.ClientSession() as session:
        with mocked_aiohttp() as mocked:
            ctx.http_session = session
            cloud            = _Cloud(mocked, devices, {"test.device": spec})
            natural          = str(await main.handle("xiaoai", "台灯调最高", {}, ctx))
            assert "云端最近回报" in natural
            assert cloud.writes == [("set", {"did": "123", "siid": 2, "piid": 2, "value": 100})]
            explicit = str(await main.handle("xiaoai", "执行 今天天气", {}, ctx))
            assert "指令已提交" in explicit
            assert cloud.writes[1:] == [
                ("action", {"did": "spk", "siid": 5, "aiid": 5, "in": ["今天天气", True]})
            ]
    assert ai.complete.await_count == 2


def test_duplicate_device_names_require_unique_ids(env):
    cat                  = deepcopy(env[2])
    cat["devices"]["d2"] = deepcopy(cat["devices"]["d1"])
    with pytest.raises(client.BridgeError):
        catalog.resolve_device(cat, "台灯")


def test_complete_inventory_and_capabilities_preserve_all_entries(env):
    cat = deepcopy(env[2])
    for i in range(20):
        cat["devices"][f"extra{i}"] = cat["devices"]["d1"] | {"name": f"设备{i}"}
    text = catalog.inventory(cat)
    assert all(f"设备{i}" in text for i in range(20))
    assert "下一页" not in text and "1/" not in text
    caps = cat["specs"]["test.device"]["capabilities"]
    for i in range(20):
        caps[f"p9.{i}"] = caps["p2.2"] | {"name": f"功能{i}"}
    text = catalog.describe(cat, "台灯")
    assert all(f"功能{i}" in text for i in range(20))
    assert "下一页" not in text and "当前状态可直接询问" in text


@pytest.mark.parametrize(
    "name,comment,description,expected",
    [
        ("称重传感器故障", "null", "No Faults", "正常"),
        ("称重传感器故障", "Faults", "Faults", "故障"),
        ("宠物粮剩余量", "Low", "Low", "不足"),
        ("宠物粮剩余量", "Normal", "Normal", "充足"),
        ("未知状态", "null", "", "含义待确认"),
    ],
)
def test_enum_labels_are_human_readable_without_guessing(name, comment, description, expected):
    schema = {
        "name"  : name,
        "values": [{"value": 0, "comment": comment, "description": description}],
    }
    assert expected in catalog.value_hint(schema)
    assert expected in operations.display_value(schema, 0)


def test_short_device_name_and_single_feeder_portion(env):
    ctx, _, cat, _, _ = env
    cat["devices"]["d1"]["name"] = "客厅 喂食器"
    assert catalog.resolve_device(cat, "喂食器")[0] == "d1"
    settings = home.preferences(ctx, "a")
    home.save_preference(ctx, settings, "份量 10", cat)
    assert settings["feeding_grams"]["d1"] == 10
    home.save_preference(ctx, settings, "份量 客厅 喂食器 20", cat)
    assert settings["feeding_grams"]["d1"] == 20


def portion_feeder(cat):
    cat                           = deepcopy(cat)
    model                         = "xiaomi.feeder.pi2001"
    cat["specs"][model]           = cat["specs"].pop("test.device")
    cat["devices"]["d1"]["model"] = model
    cat["feeding_grams"]          = {"d1": 10}
    return cat


class _Cloud:
    """米家云端与公开规格站的 HTTP 替身：记录每次设备写入（属性设置、动作）的参数。

    ``writes`` 里是 ``("set", 参数)`` / ``("action", 参数)``；属性读回统一回报亮度 100；音箱问答
    接口回 500；``specs`` 里没有的型号在规格索引中查不到。
    """

    def __init__(self, mocked, devices, specs):
        self.devices = devices
        self.specs   = specs
        self.writes  = []
        mocked.post(DEVICES, callback=self._device_list, repeat=True)
        mocked.post(PROP_GET, callback=self._prop_get, repeat=True)
        mocked.post(PROP_SET, callback=self._prop_set, repeat=True)
        mocked.post(ACTION, callback=self._action, repeat=True)
        mocked.post(UBUS, callback=self._ubus, repeat=True)
        mocked.get(SPEC_ALL, callback=self._spec_index, repeat=True)
        mocked.get(SPEC_ONE, callback=self._spec, repeat=True)

    @staticmethod
    def _reply(payload, status=200):
        return callback_result(status=status, payload=payload)

    @staticmethod
    def _params(kwargs):
        return json.loads(kwargs["data"]["data"])["params"]

    def _device_list(self, _url, **_kwargs):
        return self._reply({"code": 0, "result": {"list": self.devices}})

    def _prop_get(self, _url, **kwargs):
        result = [
            {**item, "value": 100, "code": 0, "updateTime": time.time()}
            for item in self._params(kwargs)
        ]
        return self._reply({"code": 0, "result": result})

    def _prop_set(self, _url, **kwargs):
        params = self._params(kwargs)
        self.writes.extend(("set", item) for item in params)
        return self._reply({"code": 0, "result": [{**item, "code": 0} for item in params]})

    def _action(self, _url, **kwargs):
        self.writes.append(("action", self._params(kwargs)))
        return self._reply({"code": 0, "result": {"code": 0}})

    def _ubus(self, _url, **_kwargs):
        return self._reply({}, status=500)

    def _spec_index(self, _url, **_kwargs):
        instances = [
            {"model": model, "version": 1, "type": f"urn:test:{model}"} for model in self.specs
        ]
        return self._reply({"instances": instances})

    def _spec(self, url, **_kwargs):
        return self._reply(self.specs[str(url.query["type"]).removeprefix("urn:test:")])


def _token(user):
    return {
        "userId"       : user,
        "deviceId"     : "device",
        "xiaomiio"     : ["a2V5", "io"],
        "micoapi"      : ["a2V5", "mina"],
        "_refreshed_at": {"xiaomiio": time.time(), "micoapi": time.time()},
    }


@pytest_asyncio.fixture
async def structured(env):
    """一台已配置每份 10 克的喂食器（规格同 ``env`` 里的灯），数据目录里是真实的凭据和偏好文件。"""

    ctx, _io, _cat, ai, spec = env
    client.write_json(ctx.data_dir / "speaker.json", {"account_id": "u", "region": "cn"})
    client.write_json(ctx.data_dir / "token.json", _token("u"))
    client.write_json(
        ctx.data_dir / "preferences.json",
        {"account_id": "u", "feeding_grams": {LAMP: 10}},
    )
    devices = [{"did": "123", "name": "台灯", "model": FEEDER, "isOnline": True}]
    async with aiohttp.ClientSession() as session:
        with mocked_aiohttp() as mocked:
            ctx.http_session = session
            yield ctx, _Cloud(mocked, devices, {FEEDER: spec}), ai


@pytest.mark.asyncio
async def test_structured_discovery_and_execution_skip_plugin_ai(structured):
    ctx, cloud, ai = structured
    devices = json.loads(await home.handle(ctx, "数据", {}))
    assert devices["devices"][LAMP]["name"] == "台灯"
    assert "did" not in devices["devices"][LAMP]
    assert "p2.2" in await home.handle(ctx, f"接口 {LAMP} 1", {})
    ctx.request_texts = ("出两份粮",)
    result            = await home.handle(
        ctx, "调用 " + json.dumps(plan("action", "a2.1", args=[20], device=LAMP)), {}
    )
    assert "已提交 2 份" in result
    assert cloud.writes == [("action", {"did": "123", "siid": 2, "aiid": 1, "in": [2]})]
    ai.complete.assert_not_awaited()


@pytest.mark.asyncio
async def test_natural_language_rejects_invalid_later_step_before_any_device_write(structured):
    ctx, cloud, ai = structured
    p = plan(value=100, device=LAMP)
    p["operations"].append({"device": LAMP, "capability": "a99.1", "op": "action", "args": []})
    ai.complete.side_effect = [
        SimpleNamespace(content=json.dumps({"devices": [LAMP]})),
        SimpleNamespace(content=json.dumps(p)),
    ]
    with pytest.raises(client.BridgeError, match="未在该设备规格中定义"):
        await home.handle(ctx, "台灯亮度调最高", {})
    assert cloud.writes == []


@pytest.mark.asyncio
@pytest.mark.parametrize("grams,utterance", [(20, "出一份粮"), (15, "出15克粮"), (20, "出粮")])
async def test_structured_feeding_rejects_changed_or_unrepresentable_amount(
    structured, grams, utterance
):
    ctx, cloud, ai = structured
    p = plan(value=100, device=LAMP)
    p["operations"].append(plan("action", "a2.1", args=[grams], device=LAMP)["operations"][0])
    ctx.request_texts = (utterance,)
    with pytest.raises(client.BridgeError, match="出粮已拦截"):
        await home.handle(ctx, "调用 " + json.dumps(p), {})
    assert cloud.writes == []
    ai.complete.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("grams,portions", [(10, 1), (20, 2), (50, 5), (150, 15)])
async def test_feeder_grams_are_converted_to_wire_portions(env, grams, portions):
    ctx, io, cat, _, _ = env
    result = await operations.execute_plan(
        ctx, io, portion_feeder(cat), plan("action", "a2.1", args=[grams]), {}
    )
    io.miot_action.assert_awaited_once_with("123", (2, 1), [portions])
    assert f"已提交 {portions} 份" in result and f"约 {grams} 克" in result


@pytest.mark.asyncio
@pytest.mark.parametrize("grams", [0, 5, 15, 151, -10, True, 10.5])
async def test_unrepresentable_feeding_blocks_entire_plan_before_submission(env, grams):
    ctx, io, cat, _, _ = env
    p = plan(value=100)
    p["operations"].append(plan("action", "a2.1", args=[grams])["operations"][0])
    with pytest.raises(client.BridgeError, match="出粮已拦截"):
        await operations.execute_plan(ctx, io, portion_feeder(cat), p, {})
    io.miot_action.assert_not_awaited()
    io.miot_set_props.assert_not_awaited()
    assert not (ctx.data_dir / "device-requests.json").exists()


@pytest.mark.asyncio
async def test_feeder_requires_portion_calibration(env):
    ctx, io, cat, _, _ = env
    cat = portion_feeder(cat)
    cat.pop("feeding_grams")
    with pytest.raises(client.BridgeError, match="先设置"):
        await operations.execute_plan(ctx, io, cat, plan("action", "a2.1", args=[10]), {})
    io.miot_action.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "text,model_grams,allowed",
    [
        ("出一份粮", 10, True),
        ("出5份粮", 50, True),
        ("出20克粮", 20, True),
        ("出5克粮", 10, False),
        ("出5g粮", 50, False),
        ("出0.5份粮", 10, False),
        ("出粮", 10, False),
        ("出十一份粮", 10, False),
        ("出十一份粮", 110, True),
    ],
)
async def test_feeding_plan_must_match_original_quantity(env, text, model_grams, allowed):
    ctx, io, cat, ai, _ = env
    cat                     = portion_feeder(cat)
    ai.complete.side_effect = [
        SimpleNamespace(content=json.dumps({"devices": ["d1"]})),
        SimpleNamespace(content=json.dumps(plan("action", "a2.1", args=[model_grams]))),
    ]
    result = await planner.plan(ctx, text, cat, {"feeding_grams": {"d1": 10}})
    if allowed:
        await operations.execute_plan(ctx, io, cat, result, {}, original_request=text)
        io.miot_action.assert_awaited_once_with("123", (2, 1), [model_grams // 10])
    else:
        with pytest.raises(client.BridgeError, match="出粮已拦截"):
            await operations.execute_plan(ctx, io, cat, result, {}, original_request=text)
        io.miot_action.assert_not_awaited()


def test_cached_feeder_spec_is_adapted_without_mutating_source(env):
    cat          = portion_feeder(env[2])
    caps         = cat["specs"]["xiaomi.feeder.pi2001"]["capabilities"]
    caps["p2.7"] = caps["p2.2"] | {
        "type": "urn:miot-spec-v2:property:target-feeding-measure:0000:xiaomi-pi2001:1"
    }
    caps["a5.1"] = {
        "kind"     : "action",
        "type"     : "urn:xiaomi-spec:action:feeder-schedule:0000:test:1",
        "supported": True,
        "inputs"   : [
            {
                "name"  : "schedule",
                "type"  : "urn:xiaomi-spec:property:feeder-schedule:0000:test:1",
                "format": "string",
            }
        ],
    }
    before  = deepcopy(cat)
    adapted = catalog.capabilities(cat, cat["devices"]["d1"])
    assert adapted["a2.1"]["inputs"][0]["range"] == [10, 150, 10]
    assert "write" not in adapted["p2.7"]["access"]
    assert adapted["a5.1"]["supported"] is False
    assert cat == before


@pytest.mark.asyncio
async def test_speaker_forwarding_cannot_bypass_feeding_quantity_guard(env):
    ctx, io, cat, _, _ = env
    with pytest.raises(client.BridgeError, match="出粮已拦截"):
        await main.execute(ctx, "出5克粮", {})
    cap = cat["specs"]["test.device"]["capabilities"]["a2.1"]
    cap.update(
        type   = "urn:miot-spec-v2:action:execute-text-directive:0000:test:1",
        inputs = [{"name": "text", "format": "string"}],
    )
    with pytest.raises(client.BridgeError, match="出粮已拦截"):
        await operations.execute_plan(ctx, io, cat, plan("action", "a2.1", args=["出5克粮"]), {})
    io.miot_action.assert_not_awaited()


@pytest.mark.asyncio
async def test_structured_interface_accepts_json_arguments(structured):
    """接口命令的 JSON 写法和“设备 页码”写法结果相同；格式不对时报出用法。"""

    ctx, _cloud, _ai = structured
    assert await home.handle(
        ctx, '接口 {"device": "' + LAMP + '", "page": 1}', {}
    ) == await home.handle(ctx, f"接口 {LAMP} 1", {})
    for bad in (
        '{"device": "' + LAMP + '", "page": 0}',
        '{"device": ""}',
        '{"device": "' + LAMP + '", "x": 1}',
        "{",
    ):
        with pytest.raises(client.BridgeError, match="接口参数需要"):
            await home.handle(ctx, "接口 " + bad, {})


@pytest.mark.asyncio
async def test_delegated_call_checks_amount_against_initiator_words(structured):
    """代为执行时按 Core 传入的发起人原话核对数量；参数里不能自带原话。"""

    ctx, cloud, _ai = structured
    ctx.request_texts = ("出一份粮",)
    call = plan("action", "a2.1", args=[20], device=LAMP)
    with pytest.raises(client.BridgeError, match="出粮已拦截"):
        await home.handle(ctx, "调用 " + json.dumps(call), {})
    forged = call | {"original_request": "出两份粮"}
    with pytest.raises(client.BridgeError, match="operations"):
        await home.handle(ctx, "调用 " + json.dumps(forged), {})
    assert cloud.writes == []

    ctx.request_texts = ("出一份粮", "改成两份")
    result            = await home.handle(ctx, "调用 " + json.dumps(call), {})
    assert "已提交 2 份" in result


@pytest.mark.asyncio
@pytest.mark.parametrize("target,cap_id", [(30, "p2.2"), (False, "p2.1")])
async def test_delayed_fresh_report_confirms_without_replaying_write(
    env, monkeypatch, caplog, target, cap_id
):
    ctx, io, cat, _, _ = env
    monkeypatch.setattr(operations.time, "time", lambda: 1800000000.75)
    caplog.set_level("INFO")
    piid                        = int(cap_id.split(".")[1])
    base                        = {"did": "123", "siid": 2, "piid": piid, "code": 0}
    old                         = 50 if type(target) is int else True
    io.miot_request.side_effect = [
        [base | {"value": old, "updateTime": 1799990000}],
        [base | {"value": target, "updateTime": 1799999990}],
        [base | {"value": target, "updateTime": 1800000000}],
    ]
    request = plan(value=target, capability=cap_id)
    request["operations"].append(plan("action", "a2.2", args=[])["operations"][0])
    result = await operations.execute_plan(ctx, io, cat, request, {})
    assert "状态已核对，提交后的回报与目标一致" in result
    io.miot_set_props.assert_awaited_once()
    io.miot_action.assert_awaited_once()
    # 基准（旧值）之后第一次回报就到了目标：值变过即说明回报已更新，不需要比对时间戳。
    assert io.miot_request.await_count == 2
    assert "reason_code=matched" in caplog.text
    assert "device.operation.submitted" in caplog.text
    assert "台灯" not in caplog.text


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "stamp,reason",
    [
        (1799999000, "stale_report"),
        (None, "timestamp_unverified"),
        (float("nan"), "timestamp_unverified"),
        (1800000030, "timestamp_unverified"),
    ],
)
async def test_without_baseline_matching_value_needs_fresh_report(
    env, monkeypatch, caplog, stamp, reason
):
    ctx, io, cat, _, _ = env
    monkeypatch.setattr(operations.time, "time", lambda: 1800000000.75)
    report                      = io.miot_request.return_value[0] | {"updateTime": stamp}
    io.miot_request.side_effect = [client.BridgeError("baseline")] + [[report]] * 9
    request = plan(value=100)
    request["operations"].append(plan("action", "a2.2", args=[])["operations"][0])
    result = await operations.execute_plan(ctx, io, cat, request, {})
    assert "结果待确认" in result and "状态已核对" not in result
    assert "指令已提交" in result
    io.miot_set_props.assert_awaited_once()
    io.miot_action.assert_not_awaited()
    assert io.miot_request.await_count == 1 + len(operations.CONFIRM_DELAYS)
    assert f"reason_code={reason}" in caplog.text
    assert "device.confirmation.completed" in caplog.text


@pytest.mark.asyncio
async def test_confirmation_deadline_bounds_hung_read(env, monkeypatch, caplog):
    ctx, io, cat, _, _ = env
    monkeypatch.setattr(operations, "CONFIRM_TIMEOUT_SECONDS", 0.01)

    async def hanging(*args):
        await asyncio.Event().wait()

    io.miot_request.side_effect = hanging
    result = await operations.execute_plan(ctx, io, cat, plan(value=100), {})
    assert "确认等待已超时" in result
    io.miot_set_props.assert_awaited_once()
    assert "reason_code=timeout" in caplog.text
    assert "duration_s=" in caplog.text


@pytest.mark.asyncio
async def test_hung_baseline_read_is_bounded_and_still_submits_once(env, monkeypatch, caplog):
    ctx, io, cat, _, _ = env
    monkeypatch.setattr(operations, "BASELINE_TIMEOUT_SECONDS", 0.01)
    monkeypatch.setattr(operations, "CONFIRM_TIMEOUT_SECONDS", 0.01)
    caplog.set_level("INFO")

    async def hanging(*args):
        await asyncio.Event().wait()

    io.miot_request.side_effect = hanging
    result = await operations.execute_plan(ctx, io, cat, plan(value=100), {})
    assert "确认等待已超时" in result
    io.miot_set_props.assert_awaited_once()
    assert "device.baseline.unavailable" in caplog.text


@pytest.mark.asyncio
async def test_already_at_target_confirms_on_first_read_and_continues(env, monkeypatch, caplog):
    ctx, io, cat, _, _ = env
    caplog.set_level("INFO")
    # 灯本来就是目标状态：设备不会上报新时间戳，也不能因此卡住后续操作或等满确认时限。
    io.miot_request.return_value[0]["updateTime"] = time.time() - 86400
    request = plan(value=100)
    request["operations"].append(plan("action", "a2.2", args=[])["operations"][0])
    result = await operations.execute_plan(ctx, io, cat, request, {})
    assert "设备提交前已是目标状态" in result and "待确认，后续操作已停止" not in result
    assert io.miot_request.await_count == 2
    io.miot_set_props.assert_awaited_once()
    io.miot_action.assert_awaited_once()
    assert "reason_code=already_target" in caplog.text


@pytest.mark.asyncio
async def test_host_clock_skew_does_not_reject_fresh_report(env, monkeypatch):
    ctx, io, cat, _, _ = env
    monkeypatch.setattr(operations.time, "time", lambda: 1800000000.0)
    base                        = {"did": "123", "siid": 2, "piid": 2, "code": 0}
    # 小米云比本机快 3 秒、慢 3 秒都不影响：判断只看值相对基准的变化。
    io.miot_request.side_effect = [
        [base | {"value": 50, "updateTime": 1800000003}],
        [base | {"value": 100, "updateTime": 1799999997}],
    ]
    result = await operations.execute_plan(ctx, io, cat, plan(value=100), {})
    assert "状态已核对" in result


@pytest.mark.asyncio
async def test_plain_read_shows_out_of_spec_and_float32_reports(env):
    ctx, io, cat, _, _ = env
    cap = operations.capabilities(cat, cat["devices"]["d1"])["p2.2"]
    assert operations.normalize_reported(
        {"format": "float", "range": [-30, 100, 0.1]}, 26.299999237060547
    ) == 26.3
    assert operations.normalize_reported(
        {"format": "float", "range": [-30, 100, 0.1]}, 26.34
    ) == 26.34
    # 固件报出规格外的值时，普通查询照实显示，不当作读取失败。
    io.miot_request.return_value[0]["value"] = 250
    result = await operations.execute_plan(ctx, io, cat, plan("read"), {})
    assert "250" in result and "待确认" not in result
    with pytest.raises(client.BridgeError):
        await operations.read_property(io, cat["devices"]["d1"], cap, strict=True)


@pytest.mark.asyncio
async def test_cancel_during_confirmation_preserves_single_submission(env, caplog):
    ctx, io, cat, _, _ = env
    caplog.set_level("INFO")
    io.miot_request.side_effect = [io.miot_request.return_value, asyncio.CancelledError()]
    with pytest.raises(asyncio.CancelledError):
        await operations.execute_plan(ctx, io, cat, plan(value=100), {})
    assert "device.confirmation.cancelled" in caplog.text
    assert "重复保护" in await operations.execute_plan(ctx, io, cat, plan(value=100), {})
    io.miot_set_props.assert_awaited_once()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "field,value",
    [("value", "secret-invalid-value"), ("did", "other-device"), ("siid", 99), ("piid", 99)],
)
async def test_confirmation_validates_property_identity_and_value(env, caplog, field, value):
    ctx, io, cat, _, _ = env
    io.miot_request.return_value[0][field] = value
    result = await operations.execute_plan(ctx, io, cat, plan(value=100), {})
    assert "结果待确认" in result
    assert "reason_code=read_failed" in caplog.text
    assert "secret-invalid-value" not in caplog.text
    io.miot_set_props.assert_awaited_once()


@pytest.mark.asyncio
@pytest.mark.parametrize("name", ["走廊风扇", "书房插座", "办公照明"])
async def test_preloaded_named_device_uses_one_model_call(env, name, caplog):
    ctx, _, cat, ai, _ = env
    caplog.set_level("INFO")
    cat["devices"]["d1"]["name"] = name
    ai.complete.return_value = SimpleNamespace(content=json.dumps(plan(value=30)))
    assert await planner.plan(ctx, name + "设为30", cat, {}) == plan(value=30)
    ai.complete.assert_awaited_once()
    payload = json.loads(ai.complete.call_args.args[1][1]["content"])
    assert set(payload["prefetched"]) == {"d1"}
    assert "stage=combined" in caplog.text and "duration_s=" in caplog.text


@pytest.mark.asyncio
async def test_preloaded_plan_can_expand_to_multiple_devices(env):
    ctx, _, cat, ai, _ = env
    cat["devices"]["d2"] = cat["devices"]["d1"] | {"name": "客厅照明", "did": "456"}
    request = plan(value=30)
    request["operations"].append(plan(value=40, device="d2")["operations"][0])
    ai.complete.side_effect = [
        SimpleNamespace(content=json.dumps({"devices": ["d1", "d2"]})),
        SimpleNamespace(content=json.dumps(request)),
    ]
    assert await planner.plan(ctx, "台灯调到30，客厅的灯调到40", cat, {}) == request
    first = json.loads(ai.complete.call_args_list[0].args[1][1]["content"])
    assert set(first["devices"]) == {"d1", "d2"}
    assert set(first["prefetched"]) == {"d1"}
    second = json.loads(ai.complete.call_args_list[1].args[1][1]["content"])
    assert set(second["devices"]) == {"d1", "d2"}


@pytest.mark.asyncio
async def test_single_pass_plan_cannot_use_unloaded_schema(env):
    ctx, _, cat, ai, _ = env
    cat["devices"]["d2"] = cat["devices"]["d1"] | {"name": "其他设备", "did": "456"}
    ai.complete.return_value = SimpleNamespace(content=json.dumps(plan(value=100, device="d2")))
    with pytest.raises(client.BridgeError, match="选择范围外"):
        await planner.plan(ctx, "台灯设为100", cat, {})


@pytest.mark.asyncio
async def test_large_catalog_retains_two_phase_planning(env, monkeypatch):
    ctx, _, cat, ai, _ = env
    monkeypatch.setattr(planner, "PREFETCH_MAX_CHARS", 1)
    ai.complete.side_effect = [
        SimpleNamespace(content=json.dumps({"devices": ["d1"]})),
        SimpleNamespace(content=json.dumps(plan(value=100))),
    ]
    assert await planner.plan(ctx, "台灯调到100", cat, {}) == plan(value=100)
    first = json.loads(ai.complete.call_args_list[0].args[1][1]["content"])
    assert first["prefetched"] == {}
    assert ai.complete.await_count == 2


@pytest.mark.asyncio
async def test_single_pass_http_flow_waits_for_fresh_state(env, monkeypatch, caplog):
    ctx, _, _, ai, spec = env
    caplog.set_level("INFO")
    client.write_json(ctx.data_dir / "speaker.json", {"account_id": "u", "region": "cn"})
    client.write_json(ctx.data_dir / "token.json", _token("u"))
    devices = [{"did": "123", "name": "办公照明", "model": "test.device", "isOnline": True}]
    reads   = []

    def report(cloud, _url, **kwargs):
        params = cloud._params(kwargs)
        reads.append(params)
        stamp = time.time() if len(reads) > 1 else time.time() - 3600
        return cloud._reply(
            {
                "code"  : 0,
                "result": [{**p, "value": 30, "code": 0, "updateTime": stamp} for p in params],
            }
        )

    monkeypatch.setattr(_Cloud, "_prop_get", report)
    ai.complete.return_value = SimpleNamespace(content=json.dumps(plan(value=30, device=LAMP)))
    async with aiohttp.ClientSession() as session:
        with mocked_aiohttp() as mocked:
            ctx.http_session = session
            cloud            = _Cloud(mocked, devices, {"test.device": spec})
            result           = str(await main.handle("xiaoai", "办公照明调到30", {}, ctx))
    assert "状态已核对" in result
    assert len(cloud.writes) == 1 and len(reads) == 2
    ai.complete.assert_awaited_once()
    for event in (
        "device.discovery.completed",
        "plan.model.completed",
        "device.operation.submitted",
        "device.confirmation.completed",
        "request.completed",
    ):
        assert event in caplog.text


@pytest.mark.asyncio
async def test_write_rejection_logs_code_and_stops(env, caplog):
    ctx, io, cat, _, _ = env
    io.miot_set_props.return_value = [-704042011]
    result = await operations.execute_plan(ctx, io, cat, plan(value=100), {})
    assert "设备返回失败" in result
    assert "upstream_code=-704042011" in caplog.text
    # 只有提交前的一次基准读；写入被拒后不再读取确认。
    io.miot_request.assert_awaited_once()
