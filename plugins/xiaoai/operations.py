"""Validate a complete plan before submitting any device mutation, then read back."""

from __future__ import annotations

import asyncio
import hashlib
import json
import math
from decimal import Decimal
from typing import Any

from .catalog import capabilities, resolve_device
from .client import BridgeError, reserve_request
from .feeding import check_requested_amount, reject_unchecked_forwarding, wire_arguments
from .infrared import voice_action
from .presentation import display_value

MAX_OPERATIONS = 12


def validate_value(schema: dict[str, Any], value: Any) -> None:
    fmt   = schema.get("format")
    valid = False
    if fmt == "bool":
        valid = type(value) is bool
    elif fmt == "string":
        valid = isinstance(value, str) and len(value) <= 2000 and all(ord(c) >= 32 for c in value)
    elif fmt in {"float", "double"}:
        valid = type(value) in {int, float} and math.isfinite(value)
    elif fmt in {"uint8", "uint16", "uint32", "uint64", "int8", "int16", "int32", "int64"}:
        bits = int(fmt.lstrip("uint"))
        low, high = (
            (0, 2**bits - 1) if fmt.startswith("u") else (-(2 ** (bits - 1)), 2 ** (bits - 1) - 1)
        )
        valid = type(value) is int and low <= value <= high
    if not valid:
        raise BridgeError(f"{schema['name']} 的参数类型或取值无效，需要 {fmt}。")
    choices = schema.get("values")
    if choices and value not in [choice["value"] for choice in choices]:
        raise BridgeError(f"{schema['name']} 的参数不在设备枚举中。")
    limits = schema.get("range")
    if limits:
        low, high, step = (Decimal(str(x)) for x in limits)
        number = Decimal(str(value))
        if not low <= number <= high or (step > 0 and (number - low) % step != 0):
            raise BridgeError(f"{schema['name']} 的范围是 {low}–{high}，步长 {step}。")


def validate_plan(catalog: dict[str, Any], plan: Any) -> list[dict[str, Any]]:
    if not isinstance(plan, dict) or set(plan) != {"operations"}:
        raise BridgeError("调用需要 operations 列表。")
    ops = plan["operations"]
    if not isinstance(ops, list) or not 1 <= len(ops) <= MAX_OPERATIONS:
        raise BridgeError(f"每次需要 1–{MAX_OPERATIONS} 项操作。")
    validated                          = []
    seen                               = set()
    schemas: dict[str, dict[str, Any]] = {}
    for op in ops:
        if not isinstance(op, dict) or not isinstance(op.get("device"), str):
            raise BridgeError("操作需要指定设备编号。")
        key, device = resolve_device(catalog, op["device"])
        if key not in schemas:
            schemas[key] = capabilities(catalog, device)
        cap_id = op.get("capability")
        cap    = schemas[key].get(cap_id) if isinstance(cap_id, str) else None
        if cap is None:
            raise BridgeError("操作所需能力未在该设备规格中定义。")
        kind      = op.get("op")
        allowed   = {"device", "capability", "op"}
        wire_args = None
        if kind == "read" and cap["kind"] == "property" and "read" in cap["access"]:
            pass
        elif kind == "set" and cap["kind"] == "property" and "write" in cap["access"]:
            allowed.add("value")
            validate_value(cap, op.get("value"))
        elif kind == "action" and cap["kind"] == "action" and cap["supported"]:
            allowed.add("args")
            args = op.get("args", [])
            op   = op | {"args": args}
            if ":execute-text-directive:" in cap.get("type", "") and isinstance(args, list):
                for arg in args:
                    if isinstance(arg, str):
                        reject_unchecked_forwarding(arg)
            if cap.get("transport") == "feeder_portions":
                wire_args = wire_arguments(cap, args)
            if not isinstance(args, list) or len(args) != len(cap["inputs"]):
                raise BridgeError("动作参数数量与设备规格不一致。")
            for schema, value in zip(cap["inputs"], args, strict=True):
                validate_value(schema, value)
            if wire_args is not None:
                for schema, value in zip(cap["wire_inputs"], wire_args, strict=True):
                    validate_value(schema, value)
        else:
            raise BridgeError("该能力不支持请求的操作。事件需要使用订阅通道。")
        if set(op) != allowed:
            raise BridgeError("操作字段不完整或包含未知字段。")
        if kind != "read" and device["online"] is False:
            raise BridgeError(f"{device['name']} 当前离线。")
        normalized = op | {"device": key}
        fingerprint = json.dumps(normalized, sort_keys=True, ensure_ascii=False)
        if fingerprint in seen:
            raise BridgeError("同一计划包含重复操作，请合并后提交。")
        seen.add(fingerprint)
        validated.append(
            {"operation": normalized, "device": device, "cap": cap, "wire_args": wire_args}
        )
    return validated


async def read_property(io: Any, device: dict[str, Any], cap: dict[str, Any]) -> dict[str, Any]:
    result = await io.miot_request(
        "prop/get", [{"did": device["did"], "siid": cap["siid"], "piid": cap["iid"]}]
    )
    if not isinstance(result, list) or len(result) != 1 or not isinstance(result[0], dict):
        raise BridgeError("设备属性响应格式异常。")
    item = result[0]
    code = item.get("code")
    if type(code) is not int or code != 0 or "value" not in item:
        raise BridgeError(f"{device['name']} 的 {cap['name']} 暂时读取失败。")
    # 回报必须属于请求的那台设备和那个属性：其他设备的同编号属性不能当作目标状态，
    # 设置后回读也据此决定是否继续后续动作。
    if (
        str(item.get("did", "")) != str(device["did"])
        or item.get("siid", cap["siid"]) != cap["siid"]
        or item.get("piid", cap["iid"]) != cap["iid"]
    ):
        raise BridgeError("设备属性响应身份不匹配。")
    return {"value": item["value"], "updated_at": item.get("updateTime")}


def format_read(
    context: Any, device: dict[str, Any], cap: dict[str, Any], result: dict[str, Any]
) -> str:
    value  = display_value(cap, result["value"])
    stamp  = result.get("updated_at")
    suffix = ""
    if isinstance(stamp, (int, float)) and not isinstance(stamp, bool) and stamp > 0:
        from datetime import datetime

        zone   = context.now().tzinfo if callable(getattr(context, "now", None)) else None
        suffix = "；上报时间 " + datetime.fromtimestamp(stamp, zone).strftime("%Y-%m-%d %H:%M:%S")
    return f"• {device['name']} · {cap['name']}：{value}（云端最近回报{suffix}）"


async def execute_plan(
    context: Any,
    io: Any,
    catalog: dict[str, Any],
    plan: Any,
    event: dict[str, Any],
    profile: dict[str, Any] | None = None,
    *,
    original_request: str | list[str] | None = None,
) -> str:
    items = validate_plan(catalog, plan)
    if original_request is not None:
        texts = [original_request] if isinstance(original_request, str) else original_request
        if (
            not isinstance(texts, list)
            or not texts
            or not all(isinstance(item, str) and item.strip() for item in texts)
        ):
            raise BridgeError("original_request 需要包含本次任务要求。")
        check_requested_amount(texts, items, catalog)
    if any(item["cap"].get("transport") == "speaker_silent" for item in items):
        if not profile or profile.get("directive", {}).get("inputs", []).count("silent") != 1:
            raise BridgeError("红外扩展需要绑定支持静默文字执行的小爱音箱。")
    digests = [
        hashlib.sha256(
            json.dumps(item["operation"], sort_keys=True, ensure_ascii=False).encode()
        ).hexdigest()
        for item in items
        if item["operation"]["op"] != "read"
    ]
    if not reserve_request(context, event, "device-requests.json", digests):
        return "本次设备动作已被重复保护拦截；相同操作间隔需满 20 秒，请核对上次执行结果。"
    lines = ["🏠 设备反馈", "━━━━━━━━━━━━"]
    for item in items:
        op, device, cap = item["operation"], item["device"], item["cap"]
        title = f"{device['name']} · {cap['name']}"
        try:
            if op["op"] == "read":
                lines.append(
                    format_read(context, device, cap, await read_property(io, device, cap))
                )
                continue
            if op["op"] == "set":
                codes = await io.miot_set_props(
                    device["did"], [(cap["siid"], cap["iid"], op["value"])]
                )
                success = codes == [0]
            else:
                if cap.get("transport") == "speaker_silent":
                    assert profile is not None
                    code = await voice_action(io, profile, device, cap, op["args"])
                else:
                    code = await io.miot_action(
                        device["did"],
                        (cap["siid"], cap["iid"]),
                        item["wire_args"] if item["wire_args"] is not None else op["args"],
                    )
                success = type(code) is int and code == 0
            if not success:
                lines.append(f"{title}：设备返回失败，后续操作已停止。")
                break
            submitted = f"• {title}：指令已提交。"
            if item["wire_args"] is not None:
                submitted = f"✓ {device['name']}：已提交 {item['wire_args'][0]} 份，按设置估算约 {op['args'][0]} 克。"
            lines.append(submitted)
            if device["model"].startswith("miir."):
                lines.append(
                    "小爱静默转发已提交，红外执行效果待确认。"
                    if cap.get("transport") == "speaker_silent"
                    else "红外控制请求已提交；设备实际状态需现场确认。"
                )
                continue
            if op["op"] == "set" and "read" in cap["access"]:
                await asyncio.sleep(0.3)
                try:
                    result    = await read_property(io, device, cap)
                    lines[-1] = format_read(context, device, cap, result)
                    if result["value"] != op["value"]:
                        lines.append("回报值尚未达到目标，后续操作已停止，请核对设备状态。")
                        break
                except Exception:
                    lines.append("提交后的状态读取未完成，后续操作已停止。")
                    break
            elif op["op"] == "action":
                lines.append(
                    "设备状态：待确认；当前回报仅确认已接收指令，动作完成情况需设备事件或现场确认。"
                )
        except Exception:
            lines.append(f"{title}：本次结果待确认，后续操作已停止；动作保持单次提交。")
            break
    return "\n".join(lines)
