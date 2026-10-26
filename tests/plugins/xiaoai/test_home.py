"""Device discovery, validated plans, infrared boundaries and natural-language routing."""

import asyncio
import json
from copy import deepcopy
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from plugins.xiaoai import catalog, client, home, main, operations, planner, weather


def prop(iid, name, fmt="uint8", access=None, **kwargs):
    return {
        "iid": iid,
        "type": f"urn:miot-spec-v2:property:{name}:0000:test:1",
        "description": name,
        "format": fmt,
        "access": access if access is not None else ["read", "write"],
        **kwargs,
    }


def action(iid, name, inputs=None):
    return {
        "iid": iid,
        "type": f"urn:miot-spec-v2:action:{name}:0000:test:1",
        "description": name,
        "in": inputs or [],
        "out": [],
    }


@pytest.fixture
def env(tmp_path, monkeypatch):
    spec = {
        "services": [
            {
                "iid": 2,
                "type": "urn:miot-spec-v2:service:light:0000:test:1",
                "description": "light",
                "properties": [
                    prop(1, "on", "bool"),
                    prop(2, "brightness", **{"value-range": [1, 100, 1]}),
                    prop(8, "feeding-measure", access=[], **{"value-range": [0, 150, 1]}),
                ],
                "actions": [action(1, "pet-food-out", [8]), action(2, "turn-off")],
                "events": [{"iid": 1, "description": "finished", "arguments": [8]}],
            }
        ]
    }
    model  = "test.device"
    device = {"did": "123", "name": "台灯", "model": model, "online": True, "room": ""}
    cat    = {
        "devices": {"d1": device},
        "specs": {model: {"capabilities": catalog.normalize_spec(spec)}},
    }
    io = SimpleNamespace(
        miot_set_props=AsyncMock(return_value=[0]),
        miot_action=AsyncMock(return_value=0),
        miot_request=AsyncMock(return_value=[{"siid": 2, "piid": 2, "value": 100, "code": 0}]),
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
async def test_partial_failure_and_timeout_stop_following_actions(env):
    ctx, io, cat, _, _ = env
    io.miot_action.side_effect = TimeoutError("secret-token")
    p = plan("action", "a2.1", args=[10])
    p["operations"].append({"device": "d1", "capability": "a2.2", "op": "action", "args": []})
    result = await operations.execute_plan(ctx, io, cat, p, {})
    assert "待确认" in result and "secret" not in result
    assert "重复保护" in await operations.execute_plan(ctx, io, cat, p, {})
    io.miot_action.assert_awaited_once()


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
        "did": "speaker",
        "silent": False,
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
                "daily": {
                    "time": ["2026-10-26"],
                    "weather_code": [0],
                    "temperature_2m_min": [10],
                    "temperature_2m_max": [20],
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
async def test_default_handler_routes_to_home_explicit_keeps_speaker(env, monkeypatch):
    ctx = env[0]
    monkeypatch.setattr(main.login, "command", AsyncMock(return_value=None))
    natural = AsyncMock(return_value="home result")
    legacy = AsyncMock(return_value="speaker result")
    monkeypatch.setattr(main.home, "handle", natural)
    monkeypatch.setattr(main, "execute", legacy)
    assert "home result" in str(await main.handle("xiaoai", "台灯调最高", {}, ctx))
    assert "speaker result" in str(await main.handle("xiaoai", "执行 今天天气", {}, ctx))
    natural.assert_awaited_once()
    legacy.assert_awaited_once()


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
        "name": name,
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


def setup_structured_home(env, monkeypatch):
    ctx, io, cat, ai, _ = env
    cat = portion_feeder(cat)
    monkeypatch.setattr(home, "read_json", lambda _: {"account_id": "u", "region": "cn"})
    manager = SimpleNamespace(
        ensure_fresh=AsyncMock(return_value={"userId": "u"}), refresh=AsyncMock()
    )
    monkeypatch.setattr(home.login, "runtime", lambda _: SimpleNamespace(manager=manager))
    monkeypatch.setattr(home, "services", lambda *args: (io, None))
    monkeypatch.setattr(home.catalog, "discover", AsyncMock(return_value=cat))
    monkeypatch.setattr(home, "preferences", lambda *args: {"feeding_grams": {"d1": 10}})
    return ctx, io, ai


@pytest.mark.asyncio
async def test_structured_discovery_and_execution_skip_plugin_ai(env, monkeypatch):
    ctx, io, ai = setup_structured_home(env, monkeypatch)
    devices = json.loads(await home.handle(ctx, "数据", {}))
    assert devices["devices"]["d1"]["name"] == "台灯"
    assert "did" not in devices["devices"]["d1"]
    assert "p2.2" in await home.handle(ctx, "接口 d1 1", {})
    p = plan("action", "a2.1", args=[20]) | {"original_request": "出两份粮"}
    result = await home.handle(ctx, "调用 " + json.dumps(p), {})
    assert "已提交 2 份" in result
    io.miot_action.assert_awaited_once_with("123", (2, 1), [2])
    ai.complete.assert_not_awaited()


@pytest.mark.asyncio
async def test_natural_language_rejects_invalid_later_step_before_any_device_write(
    env, monkeypatch
):
    ctx, io, ai = setup_structured_home(env, monkeypatch)
    p = plan(value=100)
    p["operations"].append({"device": "d1", "capability": "a99.1", "op": "action", "args": []})
    ai.complete.side_effect = [
        SimpleNamespace(content=json.dumps({"devices": ["d1"]})),
        SimpleNamespace(content=json.dumps(p)),
    ]
    with pytest.raises(client.BridgeError, match="未在该设备规格中定义"):
        await home.handle(ctx, "台灯亮度调最高", {})
    io.miot_set_props.assert_not_awaited()
    io.miot_action.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("grams,utterance", [(20, "出一份粮"), (15, "出15克粮"), (20, "出粮")])
async def test_structured_feeding_rejects_changed_or_unrepresentable_amount(
    env, monkeypatch, grams, utterance
):
    ctx, io, ai = setup_structured_home(env, monkeypatch)
    p = plan(value=100)
    p["operations"].append(plan("action", "a2.1", args=[grams])["operations"][0])
    p["original_request"] = utterance
    with pytest.raises(client.BridgeError, match="出粮已拦截"):
        await home.handle(ctx, "调用 " + json.dumps(p), {})
    io.miot_action.assert_not_awaited()
    io.miot_set_props.assert_not_awaited()
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


def test_old_feeder_cache_is_adapted_without_mutating_source(env):
    cat          = portion_feeder(env[2])
    caps         = cat["specs"]["xiaomi.feeder.pi2001"]["capabilities"]
    caps["p2.7"] = caps["p2.2"] | {
        "type": "urn:miot-spec-v2:property:target-feeding-measure:0000:xiaomi-pi2001:1"
    }
    caps["a5.1"] = {
        "kind": "action",
        "type": "urn:xiaomi-spec:action:feeder-schedule:0000:test:1",
        "supported": True,
        "inputs": [
            {
                "name": "schedule",
                "type": "urn:xiaomi-spec:property:feeder-schedule:0000:test:1",
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
