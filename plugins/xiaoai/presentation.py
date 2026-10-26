"""Readable Chinese labels shared by capability lists and actual device replies."""

from __future__ import annotations

import re
from typing import Any

ENUM_LABELS = {
    "no faults": "正常",
    "faults": "故障",
    "normal": "正常",
    "norma": "正常",
    "abnormal": "异常",
    "on": "开启",
    "off": "关闭",
    "yes": "是",
    "no": "否",
    "low": "低",
    "high": "高",
    "medium": "中",
    "empty": "已用尽",
    "full": "已满",
    "auto": "自动",
    "busy": "运行中",
    "idle": "空闲",
    "pause": "暂停",
    "stop": "停止",
    "playing": "播放中",
    "sleep": "睡眠",
    "favorite": "最爱",
    "cool": "制冷",
    "heat": "制热",
    "dry": "除湿",
    "fan": "送风",
    "increase": "增加",
    "decrease": "减少",
    "reading": "阅读",
    "night reading": "夜间阅读",
    "computer": "电脑",
    "candle": "烛光",
    "anti-blue": "护眼",
    "custom": "自定义",
    "twinkle": "闪烁",
    "native": "原生",
    "soft": "软",
    "hard": "硬",
    "fit": "适中",
    "havepan": "已放内胆",
    "nopan": "未放内胆",
    "alltimemode": "持续模式",
    "intermittentmode": "间歇模式",
    "pirmode": "感应模式",
    "water-out": "出水",
    "effective work": "工作中",
    "mode-change": "切换模式",
    "debug mode": "调试模式",
    "eng mode": "工程模式",
    "user mode": "用户模式",
    "official": "官方",
    "eaten-gram-dispaly": "显示进食克数",
    "left-gram-dispaly": "显示余粮克数",
    "percentage-display": "显示百分比",
    "all record": "持续录制",
    "motion record": "移动侦测录制",
    "no record": "停止录制",
    "waterless": "缺水",
    "watering": "出水中",
    "interval": "间歇",
    "constant": "持续",
    "no charge": "未充电",
    "not charging": "未充电",
    "charging": "充电中",
    "charge full": "已充满",
    "running": "烹饪中",
    "aptrunning": "预约中",
    "keepwarm": "保温中",
    "error": "故障",
    "updating": "更新中",
    "cookfinish": "烹饪完成",
    "fall asleep": "已入睡",
    "awake": "清醒",
    "wearing": "已佩戴",
    "not wear": "未佩戴",
    "total": "累计",
    "single": "单次",
    "not exist": "未插入",
    "broken": "损坏",
    "formating": "格式化中",
    "eject": "已弹出",
    "fixing": "修复中",
    "ejecting": "弹出中",
    "uninitialized": "未初始化",
    "not enough space": "空间不足",
    "file err": "文件错误",
    "incompatible": "不兼容",
}

UNITS = {
    "percentage": "%",
    "celsius": "°C",
    "arcdegrees": "°",
    "kelvin": "K",
    "gram": "克",
    "grams": "克",
    "minutes": "分钟",
    "seconds": "秒",
    "hours": "小时",
    "days": "天",
    "lux": "勒克斯",
    "rpm": "转/分钟",
    "watt": "瓦",
    "volt": "伏",
}

CAPABILITY_NAMES = {
    "on": "开关",
    "brightness": "亮度",
    "color-temperature": "色温",
    "mode": "模式",
    "toggle": "切换开关",
    "brightness-delta": "亮度微调",
    "ct-delta": "色温微调",
    "ct-adjust-alexa": "灯光冷暖调节",
    "ir-mode": "空调模式",
    "ir-temperature": "空调温度",
    "fan-speed-down": "降低风速",
    "fan-speed-up": "提高风速",
    "temperature-down": "降低温度",
    "temperature-up": "提高温度",
    "turn-off": "关闭",
    "turn-on": "打开",
    "vup": "增大音量",
    "vdown": "减小音量",
    "fan-level": "风速档位",
    "relative-humidity": "湿度",
    "pm2.5-density": "PM2.5 浓度",
    "temperature": "温度",
    "filter-life-level": "滤芯剩余寿命",
    "filter-used-time": "滤芯使用时间",
    "alarm": "提示音",
    "physical-controls-locked": "童锁",
    "favorite-fan-level": "最爱模式风速",
    "battery-level": "电池电量",
    "volume": "音量",
    "mute": "静音",
    "playing-state": "播放状态",
    "play": "播放",
    "pause": "暂停",
    "next": "下一首",
    "previous": "上一首",
    "sleep-mode": "睡眠模式",
    "audio-id": "音频编号",
    "play-text": "朗读文字",
    "play-music": "播放音乐",
    "wake-up": "唤醒",
    "play-radio": "播放电台",
    "execute-text-directive": "音箱语义指令",
    "stop-alarm": "停止闹钟",
}

SERVICE_NAMES = {
    "light": "灯光",
    "light extension feature": "灯光调节",
    "indicator light": "指示灯",
    "air purifier": "空气净化器",
    "environment": "环境",
    "filter": "滤芯",
    "alarm": "提示音",
    "physical control locked": "童锁",
    "speaker": "扬声器",
    "microphone": "麦克风",
    "play control": "播放控制",
    "battery": "电池",
}


def type_name(schema: dict[str, Any]) -> str:
    parts = str(schema.get("type", "")).split(":")
    return parts[3] if len(parts) > 3 else ""


def localize_capabilities(caps: dict[str, Any]) -> None:
    """Localize the caller-owned capability copy."""
    for cap in caps.values():
        for item in [
            cap,
            *cap.get("inputs", []),
            *cap.get("outputs", []),
            *cap.get("arguments", []),
        ]:
            name = str(item.get("name", ""))
            if not re.search(r"[\u4e00-\u9fff]", name):
                item["name"] = CAPABILITY_NAMES.get(
                    type_name(item), {"EventRunStatus": "运行状态变化"}.get(name, name)
                )
        service        = str(cap.get("service", ""))
        cap["service"] = SERVICE_NAMES.get(service.casefold(), service)
        if type_name(cap) == "mute" and cap["service"] in {"扬声器", "麦克风"}:
            cap["name"] = cap["service"] + "静音"


def light_instructions(device: dict[str, Any], caps: dict[str, Any]) -> tuple[list[str], set[str]]:
    if device["model"] not in {"yeelink.light.lamp4", "miir.light.ir01"}:
        return [], set()
    writable = {
        type_name(c): (k, c)
        for k, c in caps.items()
        if c["kind"] == "property" and "write" in c["access"]
    }
    actions = {
        type_name(c): (k, c)
        for k, c in caps.items()
        if c["kind"] == "action" and c.get("supported")
    }
    name              = device["name"]
    lines: list[str]  = []
    covered: set[str] = set()
    if "on" in writable or {"turn-on", "turn-off"} <= actions.keys():
        lines.extend(["💡 开灯 / 关灯", f"  小爱 打开{name}", f"  小爱 关闭{name}", ""])
        covered.update(
            k for k, c in caps.items() if type_name(c) in {"on", "toggle", "turn-on", "turn-off"}
        )
    if "brightness" in writable:
        key, cap = writable["brightness"]
        low, high, step = cap.get("range") or [1, 100, 1]
        target = low + round((min(max(50, low), high) - low) / (step or 1)) * (step or 1)
        lines.extend(
            [
                f"🔆 调亮度（{low}–{high}{unit_label(cap)}）",
                f"  小爱 {name}亮度调到{target}{unit_label(cap)}",
            ]
        )
        covered.add(key)
        if "brightness-delta" in writable:
            lines.extend([f"  小爱 {name}调亮一点", f"  小爱 {name}调暗一点"])
            covered.add(writable["brightness-delta"][0])
        lines.append("")
    if "color-temperature" in writable:
        key, cap = writable["color-temperature"]
        lines.append("🌤 调灯光冷暖")
        if "ct-delta" in writable:
            lines.extend([f"  小爱 {name}调暖一点", f"  小爱 {name}调冷一点"])
            covered.add(writable["ct-delta"][0])
            if "ct-adjust-alexa" in writable:
                covered.add(writable["ct-adjust-alexa"][0])
        low, high, _ = cap.get("range") or [None, None, None]
        if low is not None:
            lines.extend(
                [
                    f"  暖光偏黄，冷光偏白；色温范围 {low}–{high}K。",
                    f"  也可说：小爱 {name}色温调到{low}K",
                ]
            )
        covered.add(key)
        lines.append("")
    if "mode" in writable:
        key, cap = writable["mode"]
        labels = [enum_label(v, cap) for v in cap.get("values") or []]
        labels = [v for v in labels if "待确认" not in v]
        if labels:
            lines.extend(
                [
                    "📖 切换模式",
                    f"  小爱 {name}切换到{labels[0]}模式",
                    "  可选：" + " / ".join(labels),
                    "",
                ]
            )
            covered.add(key)
    readable = [c for c in caps.values() if c["kind"] == "property" and "read" in c["access"]]
    if readable and covered:
        lines.extend(["🔎 查当前状态", f"  小爱 {name}现在是什么状态", ""])
    return lines, covered


DEVICE_GUIDES = {
    "chuangmi.camera.069a01": [
        ("p2.1", "write", "开关摄像机", "打开{name}"),
        ("p2.8", "write", "跟随移动目标", "开启{name}的移动追踪"),
        ("p5.1", "write", "侦测移动", "开启{name}的移动侦测"),
        ("p4.3", "read", "查看存储空间", "查询{name}的存储剩余空间"),
    ],
    "zhimi.airpurifier.ma2": [
        ("p2.1", "write", "开关净化器", "打开{name}"),
        ("p2.3", "write", "自动净化", "{name}切换到自动模式"),
        ("p3.2", "read", "查看空气质量", "查询{name}的PM2.5浓度"),
        ("p4.1", "read", "查看滤芯寿命", "查询{name}的滤芯剩余寿命"),
    ],
    "xiaomi.projector.rm4pro": [
        ("a7.1", "action", "开机", "打开{name}"),
        ("a2.1", "action", "关机", "关闭{name}"),
        ("p4.1", "write", "调整音量", "{name}音量调到30%"),
        ("a3.2", "action", "暂停播放", "暂停{name}的播放"),
        ("p2.2", "write", "切换画面来源", "{name}信号源切换到HDMI 1"),
    ],
    "xiaomi.pet_waterer.70m2": [
        ("p2.4", "write", "选择出水方式", "{name}切换到{mode}模式"),
        ("p2.10", "read", "检查是否缺水", "查询{name}的缺水状态"),
        ("p3.1", "read", "查看滤芯寿命", "查询{name}的滤芯剩余寿命"),
        ("p5.1", "read", "查看电量", "查询{name}的电池电量"),
    ],
    "chunmi.cooker.cmwy3": [
        ("p2.1", "read", "查看煮饭进度", "查询{name}的工作状态"),
        ("p3.3", "read", "查看还要多久", "查询{name}的剩余时长"),
        ("p3.4", "read", "查看保温时间", "查询{name}的保温计时时间"),
        ("a2.2", "action", "停止烹饪", "停止{name}的烹饪"),
    ],
    "miwear.watch.o66nfc": [
        ("p5.1034", "read", "查看充电状态", "查询{name}的电池充电状态"),
        ("p9.1097", "read", "查看佩戴状态", "查询{name}的设备佩戴状态"),
        ("p9.3", "write", "开启勿扰", "开启{name}的勿扰"),
    ],
    "xiaomi.aircondition.mc5": [
        ("p2.1", "write", "开关空调", "打开{name}"),
        ("p2.2", "write", "切换制冷", "{name}切换到制冷模式"),
        ("p2.4", "write", "调整温度", "{name}设定温度调到26度"),
        ("p3.4", "write", "开启摆风", "开启{name}的上下摆风"),
        ("p4.7", "read", "查看室温", "查询{name}的温度"),
    ],
    "xiaomi.wifispeaker.lx06": [
        ("p2.1", "write", "调整音量", "{name}音量调到30%"),
        ("a3.1", "action", "继续播放", "让{name}继续播放"),
        ("a3.2", "action", "暂停播放", "暂停{name}的播放"),
        ("a3.3", "action", "切歌", "让{name}播放下一首"),
    ],
    "xiaomi.feeder.pi2001": [
        ("a2.1", "feeding", "按份加餐", "给{name}出一份粮"),
        ("p2.6", "read", "查看粮仓余粮", "查询{name}的宠物粮剩余量"),
        ("p2.22", "read", "查看食盆余粮", "查询{name}的实时食盆余粮克数"),
        ("p2.10", "read", "检查卡粮", "查询{name}的卡粮状态"),
    ],
    "miaomiaoce.sensor_ht.t2": [
        ("p2.1", "read", "查看温度", "查询{name}的温度"),
        ("p2.2", "read", "查看湿度", "查询{name}的湿度"),
        ("p3.1", "read", "查看电量", "查询{name}的电池电量"),
    ],
    "miir.aircondition.ir02": [
        ("a2.6", "action", "开机", "打开{name}"),
        ("a2.5", "action", "关机", "关闭{name}"),
        ("p2.1", "write", "切换制冷", "{name}切换到制冷模式"),
        ("p2.2", "write", "调整温度", "{name}温度调到26度"),
        ("a2.2", "action", "调大风量", "提高{name}的风速"),
    ],
}


ROUTER_GUIDE = [
    ("p2.1", "read", "查看下载速度", "查询{name}的下载速度"),
    ("p2.2", "read", "查看上传速度", "查询{name}的上传速度"),
    ("p2.3", "read", "查看联网数量", "查询{name}的连接设备数"),
]


def usage_examples(device: dict[str, Any], caps: dict[str, Any]) -> list[str]:
    """Show everyday tasks only when the current schema exposes the required capability."""
    model = device["model"]
    name  = device["name"]
    # Each entry is (capability id, required operation, task label, spoken request).
    entries = (
        ROUTER_GUIDE
        if model in {"xiaomi.router.rd03", "xiaomi.router.ra81"}
        else DEVICE_GUIDES.get(model, [])
    )
    lines = []
    for key, operation, label, request in entries:
        cap   = caps.get(key, {})
        valid = (
            cap.get("transport") == "feeder_portions" and bool(cap.get("grams_per_portion"))
            if operation == "feeding"
            else cap.get("supported") and cap.get("kind") == "action"
            if operation == "action"
            else operation in cap.get("access", [])
        )
        if valid:
            modes = [enum_label(v, cap) for v in cap.get("values") or []]
            if "{mode}" in request and (not modes or "待确认" in modes[0]):
                continue
            lines.extend(
                [
                    f"💬 {label}",
                    "  小爱 " + request.format(name=name, mode=modes[0] if modes else ""),
                    "",
                ]
            )
    if model == "njcuk.fitting.ad1204" and caps.get("a2.1", {}).get("supported"):
        lines = ["当前接入功能：恢复默认设置。执行后设备设置会重置。", ""]
    if model == "xiaomi.feeder.pi2001":
        feeding = caps.get("a2.1", {})
        grams   = feeding.get("grams_per_portion")
        lines.extend(
            [
                f"每份约 {grams} 克，按整份出粮。"
                if grams
                else f"先设置每份克数，例如：小爱 份量 {name} 10",
                "喂食计划的编辑功能待核验。",
                "",
            ]
        )
    if lines:
        return lines
    for cap in caps.values():
        if cap.get("transport") == "feeder_portions" and cap.get("grams_per_portion"):
            lines.append(f"小爱 给{name}出一份粮")
        elif type_name(cap) == "on" and "write" in cap.get("access", []) and not lines:
            lines.extend([f"小爱 打开{name}", f"小爱 关闭{name}"])
        elif type_name(cap) == "turn-on" and cap.get("supported") and not lines:
            lines.append(f"小爱 打开{name}")
        elif type_name(cap) in {"ir-mode", "mode"} and "write" in cap.get("access", []):
            modes = [enum_label(v, cap) for v in cap.get("values") or []]
            if modes:
                lines.append(f"小爱 {name}切换到{modes[0]}模式")
    query = next(
        (c for c in caps.values() if c["kind"] == "property" and "read" in c["access"]), None
    )
    if query:
        lines.append(f"小爱 查询{name}的{query['name']}")
    return ["💬 可以直接这样说", *("  " + line for line in lines[:3]), ""] if lines else []


def concise_name(schema: dict[str, Any]) -> str:
    """Keep manufacturer protocol descriptions in the explicit interface view."""
    name = str(schema.get("name", ""))
    for prefix, label in {
        "自检结果,": "自检结果",
        "设置参数设置数据": "设备参数（编码格式）",
        "获取日志数据；": "获取设备日志",
        "自检项参数 ": "自检项目编号",
        "食谱标识，": "食谱编号",
        "米种信息,": "米种信息",
        "有锅时 0": "内胆放置状态",
    }.items():
        if name.startswith(prefix):
            return label
    return name


def enum_label(item: dict[str, Any], schema: dict[str, Any]) -> str:
    """Discard placeholder comments, preserving a usable description as evidence."""
    name        = str(schema.get("name", ""))
    description = str(item.get("description") or "").strip()
    comment     = str(item.get("comment") or "").strip()
    if type_name(schema) == "mode" and description.casefold() == "effective work":
        return "工作"
    if comment.casefold() in {"null", "none", "undefined", ""}:
        comment = description
    # Remaining food and consumables use quantity language.
    if any(word in name for word in ("剩余", "余量", "耗材")):
        quantity = {"normal": "充足", "norma": "充足", "low": "不足", "empty": "已用尽"}
        if description.casefold() in quantity:
            return quantity[description.casefold()]
    if description.casefold() == "maxschedule":
        return f"最多 {item['value']} 条"
    for text in (comment, description):
        if re.search(r"[\u4e00-\u9fff]", text):
            return text
        if text.casefold() in ENUM_LABELS:
            return ENUM_LABELS[text.casefold()]
        match = re.fullmatch(r"level\s*(\d+)", text, re.I)
        if match:
            return f"{match.group(1)} 档"
        if re.fullmatch(r"(?:HDMI|USB)\s*\d*", text, re.I):
            return text
    return f"选项 {item.get('value', '?')}（含义待确认）"


def unit_label(schema: dict[str, Any]) -> str:
    unit = str(schema.get("unit") or "")
    if unit:
        return UNITS.get(unit, unit)
    name = str(schema.get("name", ""))
    if "克数" in name:
        return "克"
    return ""


def value_hint(schema: dict[str, Any], *, writable: bool = True) -> str:
    choices = schema.get("values")
    if choices:
        return " / ".join(dict.fromkeys(enum_label(c, schema) for c in choices))
    if schema.get("format") == "bool":
        return "开启 / 关闭" if writable else ""
    limits = schema.get("range")
    if limits and writable:
        low, high, step = limits
        if step > 0 and (high - low) / step <= 4:
            values = [low + step * i for i in range(int((high - low) / step) + 1)]
            return " / ".join(str(v) for v in values) + unit_label(schema)
        return f"{low}–{high}{unit_label(schema)}" + (f"，每次 {step}" if step > 1 else "")
    return ""


def display_value(schema: dict[str, Any], value: Any) -> str:
    if value is None:
        return "暂无数据"
    for item in schema.get("values") or []:
        if item["value"] == value:
            return enum_label(item, schema)
    if type(value) is bool:
        name = str(schema.get("name", ""))
        if any(
            word in name for word in ("开关", "模式", "静音", "锁", "追踪", "侦测", "水印")
        ) or ":on:" in schema.get("type", ""):
            return "已开启" if value else "已关闭"
        return "是" if value else "否"
    return str(value) + unit_label(schema)
