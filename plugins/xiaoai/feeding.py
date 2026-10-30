"""Feeder 2 firmware uses whole portions despite stale gram labels in MIoT."""

from __future__ import annotations

import re
from collections.abc import Sequence
from copy import deepcopy
from typing import Any

from .client import BridgeError

PORTION_MODELS    = {"xiaomi.feeder.pi2001"}
MAX_REQUEST_GRAMS = 150


def reject_unchecked_forwarding(text: str) -> None:
    if re.search(r"(?:出|投|加).{0,16}粮|喂食|投喂|喂猫|喂狗|加餐", text):
        raise BridgeError("出粮已拦截：请直接发送“小爱 出一份粮”，通过整份与数量校验后执行。")


def adapt(model: str, caps: dict[str, Any], grams_per_portion: Any) -> None:
    """Adapt the caller-owned capability copy without changing the stored specification."""
    if model not in PORTION_MODELS:
        return
    for cap in caps.values():
        if ":pet-food-out:" in cap.get("type", ""):
            if len(cap.get("inputs", [])) != 1:
                cap["supported"] = False
                continue
            valid = type(grams_per_portion) is int and 1 <= grams_per_portion <= MAX_REQUEST_GRAMS
            step  = grams_per_portion if valid else 1
            cap.update(
                name              = "按整份出粮",
                transport         = "feeder_portions",
                grams_per_portion = grams_per_portion if valid else None,
                wire_inputs       = deepcopy(cap["inputs"]),
            )
            cap["inputs"][0].update(
                name  = "目标出粮量（估算克数，整份换算）",
                unit  = "grams",
                range = [step, MAX_REQUEST_GRAMS // step * step, step],
            )
        elif cap.get("kind") == "property" and ":target-feeding-measure:" in cap.get("type", ""):
            # The setting's firmware semantics need separate verification. Keep reads only.
            cap.update(name="设备设定出粮量（原始值，单位待核验）", unit=None)
            cap["access"] = [a for a in cap["access"] if a != "write"]
        elif cap.get("kind") == "property" and "feeder-schedule" in cap.get("type", ""):
            cap["access"] = [a for a in cap["access"] if a != "write"]
        elif cap.get("kind") == "action" and any(
            "feeder-schedule" in p.get("type", "") for p in cap.get("inputs", [])
        ):
            cap["supported"] = False


def wire_arguments(cap: dict[str, Any], args: Any) -> list[int]:
    per = cap.get("grams_per_portion")
    if type(per) is not int or not 1 <= per <= MAX_REQUEST_GRAMS:
        raise BridgeError("出粮已拦截：请先设置设备每份的估算克数，例如“小爱 份量 10”。")
    if not isinstance(args, list) or len(args) != 1 or type(args[0]) is not int or args[0] <= 0:
        raise BridgeError("出粮已拦截：请给出正整数克数，设备按整份出粮。")
    grams = args[0]
    if grams % per:
        raise BridgeError(
            f"出粮已拦截：设备按整份出粮，每份约 {per} 克；{grams} 克无法换算为整数份。"
            "本次没有提交设备动作。"
        )
    if grams > MAX_REQUEST_GRAMS:
        raise BridgeError(f"出粮已拦截：单次请求上限为约 {MAX_REQUEST_GRAMS} 克。")
    return [grams // per]


def _number(text: str) -> int | None:
    if text.isascii() and text.isdigit():
        return int(text)
    digits = dict(zip("零一二两三四五六七八九", [0, 1, 2, 2, 3, 4, 5, 6, 7, 8, 9], strict=True))
    if text in digits:
        return digits[text]
    if text.count("十") == 1:
        left, right = text.split("十")
        if (not left or left in digits) and (not right or right in digits):
            return (digits.get(left, 1) * 10) + digits.get(right, 0)
    return None


def check_requested_amount(
    text: str | Sequence[str], items: list[dict[str, Any]], settings: dict[str, Any]
) -> None:
    """按原话核对出粮量；``text`` 为原话和后续补充时，以最后一次说出数量的那句为准。"""

    feeds = [i for i in items if ":pet-food-out:" in i["cap"].get("type", "")]
    if not feeds:
        return
    if len(feeds) != 1:
        raise BridgeError("出粮已拦截：请每次指定一台喂食器和一个出粮量。")
    texts = [text] if isinstance(text, str) else list(text)
    # Keep the entire number token, including decimal signs: partial matches must fail closed.
    stated = [
        found
        for found in (
            re.findall(r"([\d.\-+零一二两三四五六七八九十百]+)\s*(克|g|份)", item, re.I)
            for item in texts
        )
        if found
    ]
    matches = stated[-1] if stated else []
    if len(matches) != 1:
        raise BridgeError("出粮已拦截：请明确一个出粮量，例如“小爱 出一份粮”。")
    amount, unit = matches[0]
    count = _number(amount)
    if count is None or count <= 0:
        raise BridgeError("出粮已拦截：出粮量需要正整数，并能换算为设备的整份。")
    item = feeds[0]
    if unit == "份":
        per = settings.get("feeding_grams", {}).get(item["operation"]["device"])
        if type(per) is not int or per <= 0:
            raise BridgeError("出粮已拦截：请先用“小爱 份量 10”设置每份的估算克数。")
        count *= per
    if item["operation"].get("args") != [count]:
        raise BridgeError("出粮已拦截：解析数量与原话不一致，请重新明确克数或份数。")
