"""Private admin entry point for discovery, natural language and structured calls."""

from __future__ import annotations

import json
from typing import Any

from . import catalog, login, planner, weather
from .client import BridgeError, read_json, services, write_json
from .feeding import MAX_REQUEST_GRAMS, PORTION_MODELS
from .operations import execute_plan, validate_value


def preferences(context: Any, account: str) -> dict[str, Any]:
    path  = context.data_dir / "preferences.json"
    value = read_json(path) if path.exists() else {}
    return value if value.get("account_id") == account else {"account_id": account}


def preference_summary(context: Any) -> str:
    try:
        profile  = read_json(context.data_dir / "speaker.json")
        settings = preferences(context, str(profile.get("account_id", "")))
    except (OSError, ValueError, BridgeError):
        return ""
    lines = []
    if settings.get("weather_city"):
        lines.append(f"📍 天气城市：{settings['weather_city']}")
    portions = list(settings.get("feeding_grams", {}).values())
    if len(portions) == 1:
        lines.append(f"🍚 设备每份约 {portions[0]} 克（整份出粮）")
    return "\n".join(lines)


def save_preference(
    context: Any, settings: dict[str, Any], text: str, devices: dict[str, Any]
) -> str | None:
    if text.startswith("天气城市 "):
        city = text.removeprefix("天气城市 ").strip()
        if not city or len(city) > 100:
            raise BridgeError("城市名称需要 1–100 字。")
        settings["weather_city"] = city
        write_json(context.data_dir / "preferences.json", settings)
        return f"✓ 默认天气城市：{city}\n以后直接说“小爱 天气”即可。"
    if text.startswith("份量 "):
        parts = text[3:].strip().rsplit(" ", 1)
        if len(parts) == 1 and parts[0].isdigit():
            feeders = [
                k
                for k, d in devices["devices"].items()
                if any(
                    ":pet-food-out:" in c.get("type", "")
                    for c in catalog.capabilities(devices, d).values()
                )
            ]
            if len(feeders) != 1:
                raise BridgeError("请指定喂食器：小爱 份量 <设备名> 10")
            parts = [feeders[0], parts[0]]
        if len(parts) != 2 or not parts[1].isdigit():
            raise BridgeError("设置每份 10 克：小爱 份量 10\n有多台喂食器时：小爱 份量 <设备名> 10")
        key, device = catalog.resolve_device(devices, parts[0])
        actions = [
            c
            for c in catalog.capabilities(devices, device).values()
            if ":pet-food-out:" in c.get("type", "")
        ]
        grams = int(parts[1])
        if len(actions) != 1 or len(actions[0]["inputs"]) != 1 or grams <= 0:
            raise BridgeError("请指定支持单次按量出粮的喂食器和正整数克数。")
        if device["model"] in PORTION_MODELS:
            if grams > MAX_REQUEST_GRAMS:
                raise BridgeError(f"每份估算克数需在 1–{MAX_REQUEST_GRAMS} 之间。")
        else:
            validate_value(actions[0]["inputs"][0], grams)
        settings.setdefault("feeding_grams", {})[key] = grams
        write_json(context.data_dir / "preferences.json", settings)
        return f"✓ {device['name']} · 设备每份约 {grams} 克\n以后直接说“小爱 出一份粮”即可。"
    return None


async def handle(context: Any, text: str, event: dict[str, Any]) -> str:
    profile = read_json(context.data_dir / "speaker.json")
    manager = login.runtime(context).manager
    token   = await manager.ensure_fresh()
    account = str(token["userId"])
    if profile.get("account_id") and profile["account_id"] != account:
        raise BridgeError("账号与设备绑定信息需要更新，请发送 /米家 登录。")
    settings = preferences(context, account)
    if text == "天气" or text.startswith("天气 "):
        return await weather.lookup(context, text[2:].strip() or settings.get("weather_city", ""))
    io, _ = services(context.http_session, token, profile["region"], manager.refresh)
    devices = await catalog.discover(
        context, io, account, profile["region"], refresh=text == "刷新设备"
    )
    devices["feeding_grams"] = settings.get("feeding_grams", {})
    saved                    = save_preference(context, settings, text, devices)
    if saved:
        return saved
    if text == "数据":
        return json.dumps(
            {
                "devices": {
                    key: {field: device[field] for field in ("name", "model", "room", "online")}
                    for key, device in devices["devices"].items()
                }
            },
            ensure_ascii = False,
            separators   = (",", ":"),
        )
    if text in {"设备", "刷新设备", "能力"}:
        return catalog.inventory(devices)
    if text.startswith("设备 "):
        raise BridgeError("查看全部设备：小爱 设备")
    if text.startswith(("能力 ", "接口 ")):
        query = text[3:].strip()
        page  = 1
        parts = query.rsplit(" ", 1)
        names = [d["name"] for d in devices["devices"].values()]
        if (
            text.startswith("接口 ")
            and query not in names
            and len(parts) == 2
            and parts[1].isdigit()
        ):
            query, page = parts[0], int(parts[1])
        return catalog.describe(devices, query, page, raw=text.startswith("接口 "))
    if text in [d["name"] for d in devices["devices"].values()]:
        return catalog.describe(devices, text)
    original_request = text
    if text.startswith("调用 "):
        try:
            plan = json.loads(text[3:])
            if not isinstance(plan, dict):
                raise ValueError("object required")
        except ValueError:
            raise BridgeError("调用参数需要 JSON 对象。") from None
        original_request = plan.pop("original_request", None)
    else:
        plan = await planner.plan(context, text, devices, settings)
    if set(plan) == {"clarify"}:
        question = plan["clarify"]
        if not isinstance(question, str) or not question.strip():
            raise BridgeError("请求需要补充设备或参数。")
        return question[:600]
    if set(plan) == {"weather"}:
        request = plan["weather"]
        if not isinstance(request, dict) or set(request) - {"city", "days"}:
            raise BridgeError("天气参数无效。")
        return await weather.lookup(
            context, request.get("city") or settings.get("weather_city", ""), request.get("days", 1)
        )
    return await execute_plan(
        context, io, devices, plan, event, profile, original_request=original_request
    )
