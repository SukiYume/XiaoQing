"""Infrared catalogue extensions with explicit transport and feedback limitations."""

from __future__ import annotations

from typing import Any


def extensions(model: str) -> dict[str, Any]:
    if model != "miir.aircondition.ir02":
        return {}
    common = {
        "kind": "action",
        "supported": True,
        "outputs": [],
        "service": "红外空调扩展",
        "transport": "speaker_silent",
        "verification": "静默语义转发；实际红外效果待确认",
    }
    return {
        "x.swing": common
        | {"name": "扫风开关", "voice": "swing", "inputs": [{"name": "扫风", "format": "bool"}]},
        "x.fan": common
        | {
            "name": "绝对风速",
            "voice": "fan",
            "inputs": [
                {
                    "name": "风速",
                    "format": "string",
                    "values": [{"value": x} for x in ("自动", "低", "中", "高")],
                }
            ],
        },
    }


async def voice_action(
    io: Any, profile: dict[str, Any], device: dict[str, Any], cap: dict[str, Any], args: list[Any]
) -> Any:
    name = device["name"]
    text = (
        f"{'打开' if args[0] else '关闭'}{name}扫风"
        if cap["voice"] == "swing"
        else f"把{name}风速调到{args[0]}风"
    )
    spec   = profile["directive"]
    values = [text if kind == "text" else True for kind in spec["inputs"]]
    return await io.miot_action(profile["did"], (spec["siid"], spec["aiid"]), values)
