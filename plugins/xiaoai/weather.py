"""Independent weather lookup with explicit location and local forecast dates."""

from __future__ import annotations

from typing import Any

from .client import BridgeError, request_json

WEATHER_CODES = {
    0: "晴",
    1: "大部晴朗",
    2: "局部多云",
    3: "阴",
    45: "雾",
    48: "冻雾",
    51: "小毛毛雨",
    53: "毛毛雨",
    55: "强毛毛雨",
    56: "冻毛毛雨",
    57: "强冻毛毛雨",
    61: "小雨",
    63: "中雨",
    65: "大雨",
    66: "冻雨",
    67: "强冻雨",
    71: "小雪",
    73: "中雪",
    75: "大雪",
    77: "雪粒",
    80: "小阵雨",
    81: "阵雨",
    82: "强阵雨",
    85: "阵雪",
    86: "强阵雪",
    95: "雷雨",
    96: "雷雨伴冰雹",
    99: "强雷雨伴冰雹",
}


async def lookup(context: Any, city: str, days: int = 1) -> str:
    if not isinstance(city, str) or not city.strip() or len(city) > 100:
        raise BridgeError(
            "请指定天气城市，例如 /米家 天气 北京，或用 /米家 天气城市 北京 保存默认城市。"
        )
    if type(days) is not int or not 1 <= days <= 7:
        raise BridgeError("天气预报天数范围为 1–7。")
    places = await request_json(
        context.http_session,
        "GET",
        "https://geocoding-api.open-meteo.com/v1/search",
        params={"name": city, "count": 5, "language": "zh", "format": "json"},
    )
    matches = places.get("results", [])
    if not matches:
        raise BridgeError("没有找到这个天气城市，请使用城市全名或英文名称。")
    # An explicit qualifier can disambiguate cities with the same name.
    exact = [
        p
        for p in matches
        if p.get("name", "").casefold().removesuffix("市") == city.casefold().removesuffix("市")
    ]
    candidates = exact or matches
    if len(candidates) > 1 and candidates[0].get("feature_code") not in {"PPLC", "PPLA"}:
        choices = "；".join(
            f"{p.get('name')}，{p.get('admin1', '')}，{p.get('country', '')}"
            for p in candidates[:3]
        )
        raise BridgeError(f"找到多个地点：{choices}。请补充省份或国家，例如 Paris, France。")
    place = candidates[0]
    data  = await request_json(
        context.http_session,
        "GET",
        "https://api.open-meteo.com/v1/forecast",
        params={
            "latitude": place["latitude"],
            "longitude": place["longitude"],
            "timezone": "auto",
            "forecast_days": days,
            "daily": "weather_code,temperature_2m_max,temperature_2m_min,precipitation_probability_max",
        },
    )
    daily = data.get("daily", {})
    dates = daily.get("time", [])
    if not dates:
        raise BridgeError("天气服务暂时没有返回预报。")
    name  = " / ".join(str(place[k]) for k in ("name", "admin1", "country") if place.get(k))
    lines = [f"🌦 {name} · 天气", f"当地时区：{data.get('timezone', '当地时区')}", "━━━━━━━━━━━━"]
    for index, date in enumerate(dates):
        code = daily["weather_code"][index]
        low, high = daily["temperature_2m_min"][index], daily["temperature_2m_max"][index]
        rain = daily["precipitation_probability_max"][index]
        lines.append(
            f"{date} · {WEATHER_CODES.get(code, '天气代码 ' + str(code))}\n  🌡 {low}–{high}°C  ☔ 降水概率 {rain}%"
        )
    lines.append("来源：Open-Meteo 天气预报。")
    return "\n".join(lines)
