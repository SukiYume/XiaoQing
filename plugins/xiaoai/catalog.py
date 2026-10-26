"""Discover account devices and normalize their MIoT properties, actions and events."""

from __future__ import annotations

import asyncio
import hashlib
import json
import time
from copy import deepcopy
from typing import Any

from .client import SPEC_ROOT, BridgeError, fetch_spec, request_json, write_json
from .feeding import adapt
from .infrared import extensions
from .presentation import (
    concise_name,
    light_instructions,
    localize_capabilities,
    usage_examples,
    value_hint,
)

MAX_CACHE_BYTES = 4_000_000


def label(item: dict[str, Any]) -> str:
    return str(
        item.get("comment") or item.get("description") or item.get("type", "").split(":")[3]
    )[:120]


def normalize_spec(spec: dict[str, Any]) -> dict[str, dict[str, Any]]:
    """Action-only parameters retain their schema without becoming writable properties."""
    result: dict[str, dict[str, Any]] = {}
    for service in spec.get("services", []):
        siid = service["iid"]
        if ":device-information:" in service.get("type", ""):
            continue
        properties = {p["iid"]: p for p in service.get("properties", [])}
        for prop in properties.values():
            if not prop.get("access"):
                continue
            key         = f"p{siid}.{prop['iid']}"
            result[key] = _property(prop) | {
                "kind": "property",
                "siid": siid,
                "iid": prop["iid"],
                "service": label(service),
            }
        for action in service.get("actions", []):
            inputs = [_property(properties[i]) for i in action.get("in", []) if i in properties]
            key = f"a{siid}.{action['iid']}"
            result[key] = {
                "kind": "action",
                "siid": siid,
                "iid": action["iid"],
                "name": label(action),
                "type": action.get("type", ""),
                "service": label(service),
                "inputs": inputs,
                "supported": len(inputs) == len(action.get("in", [])),
                "outputs": [
                    _property(properties[i]) for i in action.get("out", []) if i in properties
                ],
            }
        for event in service.get("events", []):
            result[f"e{siid}.{event['iid']}"] = {
                "kind": "event",
                "siid": siid,
                "iid": event["iid"],
                "name": label(event),
                "service": label(service),
                "arguments": [
                    _property(properties[i]) for i in event.get("arguments", []) if i in properties
                ],
            }
    return result


def _property(prop: dict[str, Any]) -> dict[str, Any]:
    return {
        "name": label(prop),
        "type": prop.get("type", ""),
        "format": prop.get("format"),
        "access": prop.get("access", []),
        "unit": prop.get("unit"),
        "range": prop.get("value-range"),
        "values": prop.get("value-list"),
    }


def device_key(did: str) -> str:
    return "d" + hashlib.sha256(did.encode()).hexdigest()[:12]


async def discover(
    context: Any, io: Any, account_id: str, region: str, *, refresh: bool = False
) -> dict[str, Any]:
    """Refresh live membership each time; reuse account-scoped specifications for one day."""
    path                  = context.data_dir / "capabilities.json"
    cache: dict[str, Any] = {}
    if path.exists() and path.stat().st_size <= MAX_CACHE_BYTES:
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
            if (
                isinstance(value, dict)
                and value.get("account_id") == account_id
                and value.get("region") == region
            ):
                cache = value
        except (ValueError, OSError):
            pass
    devices = await io.device_list("full")
    if not isinstance(devices, list) or len(devices) > 500:
        raise BridgeError("设备列表异常或超过 500 台，请检查账号。")
    specs = (
        cache.get("specs", {})
        if not refresh and time.time() - cache.get("updated_at", 0) < 86400
        else {}
    )
    specs   = dict(specs) if isinstance(specs, dict) else {}
    models  = {str(d["model"]) for d in devices if isinstance(d, dict) and d.get("model")}
    missing = models - specs.keys()
    if missing:
        catalog = await request_json(
            context.http_session, "GET", SPEC_ROOT + "instances?status=all"
        )
        instances = catalog.get("instances", [])
        semaphore = asyncio.Semaphore(4)

        async def fetch(model: str) -> None:
            try:
                async with semaphore:
                    spec = await fetch_spec(context.http_session, model, instances)
                specs[model] = {"urn": spec["type"], "capabilities": normalize_spec(spec)}
            except BridgeError as exc:
                specs[model] = {"capabilities": {}, "error": str(exc)}
            except Exception:
                specs[model] = {"capabilities": {}, "error": "规格读取失败，可用“刷新设备”重试"}

        await asyncio.gather(*(fetch(model) for model in sorted(missing)))
    output = {}
    for d in devices:
        if not isinstance(d, dict) or not d.get("did") or not d.get("model"):
            continue
        did                     = str(d["did"])
        output[device_key(did)] = {
            "did": did,
            "name": str(d.get("name") or d["model"])[:120],
            "model": d["model"],
            "online": d.get("isOnline"),
            "room": str(d.get("room_name") or "")[:80],
        }
    result = {
        "account_id": account_id,
        "region": region,
        "updated_at": time.time() if missing or refresh else cache.get("updated_at", time.time()),
        "devices": output,
        "specs": {m: specs[m] for m in models},
    }
    if len(json.dumps(result, ensure_ascii=False).encode()) > MAX_CACHE_BYTES:
        raise BridgeError("设备能力清单超过大小限制。")
    write_json(path, result)
    return result


def resolve_device(catalog: dict[str, Any], query: str) -> tuple[str, dict[str, Any]]:
    devices = catalog["devices"]
    if query in devices:
        return query, devices[query]
    matches = [(key, d) for key, d in devices.items() if query == d["name"]]
    if not matches and query.strip():
        matches = [
            (key, d) for key, d in devices.items() if query.casefold() in d["name"].casefold()
        ]
    if len(matches) != 1:
        if matches:
            choices = "、".join(f"{d['name']}（{key}）" for key, d in matches)
            raise BridgeError(f"找到多台设备：{choices}。请使用完整名称或编号。")
        raise BridgeError("没有找到这台设备。发送“小爱 设备”可查看全部名称。")
    return matches[0]


def capabilities(catalog: dict[str, Any], device: dict[str, Any]) -> dict[str, Any]:
    caps: dict[str, Any] = deepcopy(
        catalog["specs"].get(device["model"], {}).get("capabilities", {})
    )
    if device["model"] == "miir.light.ir01":
        caps = {
            key: cap
            for key, cap in caps.items()
            if any(f":{name}:" in cap.get("type", "") for name in ("turn-on", "turn-off"))
        }
    caps.update(extensions(device["model"]))
    key = next(
        (k for k, d in catalog["devices"].items() if d["did"] == device["did"]),
        device_key(device["did"]),
    )
    adapt(device["model"], caps, catalog.get("feeding_grams", {}).get(key))
    localize_capabilities(caps)
    return caps


def inventory(catalog: dict[str, Any]) -> str:
    devices = list(catalog["devices"].items())
    names   = [d["name"] for _, d in devices]
    online  = sum(d["online"] is True for _, d in devices)
    offline = sum(d["online"] is False for _, d in devices)
    lines   = [
        "🏠 我的设备",
        f"共 {len(devices)} 台 · 在线 {online} · 离线 {offline}",
        "━━━━━━━━━━━━",
    ]
    for state, heading in ((True, "🟢 在线"), (False, "⚪ 离线"), (None, "◌ 状态未知")):
        group = [
            (k, d)
            for k, d in devices
            if (d["online"] is state if state is not None else type(d["online"]) is not bool)
        ]
        if not group:
            continue
        lines.append(heading)
        for key, d in group:
            name  = d["name"] + (f"（{key}）" if names.count(d["name"]) > 1 else "")
            error = catalog["specs"].get(d["model"], {}).get("error", "")
            tag   = " · 红外" if d["model"].startswith("miir.") else ""
            lines.append(f"  • {name}{tag}" + (f" · {error}" if error else ""))
        lines.append("")
    lines.extend(["━━━━━━━━━━━━", "查看功能：小爱 能力 台灯", "直接操作：小爱 打开台灯"])
    return "\n".join(lines)


def describe(catalog: dict[str, Any], query: str, page: int = 1, *, raw: bool = False) -> str:
    key, device = resolve_device(catalog, query)
    caps = capabilities(catalog, device)
    if raw:
        # Protocol detail is explicitly requested and retains bounded pages.
        items = list(caps.items())
        pages = max(1, (len(items) + 5) // 6)
        if not 1 <= page <= pages:
            raise BridgeError(f"页码范围为 1–{pages}。")
        lines = [f"{device['name']} · 接口 {page}/{pages} 页", f"设备编号：{key}"]
        lines.extend(
            k + " " + json.dumps(c, ensure_ascii=False, separators=(",", ":"))
            for k, c in items[(page - 1) * 6 : page * 6]
        )
        if page < pages:
            lines.append(f"下一页：小爱 接口 {query} {page + 1}")
        return "\n".join(lines)
    state = (
        "🟢 在线"
        if device["online"] is True
        else "⚪ 离线"
        if device["online"] is False
        else "◌ 状态未知"
    )
    lines = [f"🏠 {device['name']} · 使用方法", state, "━━━━━━━━━━━━"]
    instructions, covered = light_instructions(device, caps)
    lines.extend(instructions or usage_examples(device, caps))
    groups: dict[str, list[str]] = {"▶ 可执行": [], "⚙ 可设置": [], "◉ 可查询": []}
    events                       = []
    for cap_id, cap in caps.items():
        if cap_id in covered:
            continue
        title = concise_name(cap)
        if title in {"开关", "工作状态", "状态", "模式", "格式化", "弹出"}:
            title = cap.get("service", "设备") + " · " + title
        if cap["kind"] == "action" and cap.get("supported"):
            details = [
                concise_name(p) + (f" {value_hint(p)}" if value_hint(p) else "")
                for p in cap["inputs"]
            ]
            line = title + (" · " + "；".join(details) if details else "")
            if cap.get("transport") == "speaker_silent":
                line += "（小爱静默转发）"
            groups["▶ 可执行"].append(line)
        elif cap["kind"] == "property":
            access   = cap["access"]
            writable = "write" in access
            if not writable and "read" not in access:
                continue
            detail = value_hint(cap, writable=writable)
            if cap.get("format") == "string" and writable:
                detail = "需明确参数格式"
            line = title + (" · " + detail if detail else "")
            groups["⚙ 可设置" if writable else "◉ 可查询"].append(line)
        elif cap["kind"] == "event":
            events.append(title)
    for heading, entries in groups.items():
        if entries:
            lines.extend([heading, *("  • " + item for item in entries), ""])
    if events:
        lines.extend(["◇ 设备事件（自动通知待接入）", "  " + "、".join(dict.fromkeys(events)), ""])
    if not any(groups.values()) and not covered:
        lines.append("当前暂无可调用功能。")
    lines.extend(["━━━━━━━━━━━━", "发送上面的例句即可操作；当前状态可直接询问。"])
    if device["model"].startswith("miir."):
        lines.append("红外执行效果需现场确认。")
    return "\n".join(lines)
