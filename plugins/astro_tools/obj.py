"""
天文对象查询模块
"""

import asyncio
import math
import re
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from core.bounded_http import (
    JSON_MIME_POLICY,
    BodyLimits,
    JsonLimits,
    ResponseFormatError,
    parse_bounded_json,
    requests_request_bounded,
)
from core.public_errors import public_error_message

SIMBAD_TAP_SYNC_URL = "https://simbad.cds.unistra.fr/simbad/sim-tap/sync"
SIMBAD_CONNECT_TIMEOUT_SECONDS = 3
SIMBAD_REQUEST_TIMEOUT_SECONDS = 12
SIMBAD_TRANSPORT_TOTAL_TIMEOUT_SECONDS = 14
SIMBAD_TOTAL_TIMEOUT_SECONDS = 15
SIMBAD_MAX_OBJECT_NAME_CHARS = 256

_SIMBAD_MAX_COLUMNS = 32
_SIMBAD_MAX_COLUMN_NAME_CHARS = 64
_SIMBAD_MAX_TEXT_CHARS = 128
_SIMBAD_MAX_QUERY_CHARS = 64 * 1024
_SIMBAD_COLUMN_NAME = re.compile(r"[A-Za-z_][A-Za-z0-9_]*\Z")
_SIMBAD_REQUIRED_COLUMNS = frozenset({"ra", "dec", "otype", "v", "sp_type"})
_SIMBAD_BODY_LIMITS = BodyLimits(
    max_wire_bytes=128 * 1024,
    max_decoded_bytes=256 * 1024,
    max_decompression_ratio=20,
    ratio_grace_bytes=8 * 1024,
    chunk_bytes=16 * 1024,
)
_SIMBAD_JSON_LIMITS = JsonLimits(
    max_bytes=_SIMBAD_BODY_LIMITS.max_decoded_bytes,
    max_depth=8,
    max_nodes=1_024,
    max_string_chars=64 * 1024,
    max_number_chars=128,
)


@dataclass(frozen=True, slots=True)
class SimbadRow:
    """Validated subset of one SIMBAD TAP row consumed by ``handle_obj``."""

    ra_deg: float
    dec_deg: float
    otype: str | None
    v_magnitude: float | None
    sp_type: str | None


def _build_simbad_client():
    from astroquery.simbad import Simbad

    client = Simbad()
    client.ROW_LIMIT = 1
    client.TIMEOUT = SIMBAD_REQUEST_TIMEOUT_SECONDS
    client.reset_votable_fields()
    client.add_votable_fields("otype", "V", "sp")
    return client


def _build_simbad_query(name: str) -> str:
    """Use astroquery only as an offline, escaping-aware ADQL builder."""

    if not isinstance(name, str) or not name or len(name) > SIMBAD_MAX_OBJECT_NAME_CHARS:
        raise ValueError("invalid SIMBAD object name")
    payload = _build_simbad_client().query_object(name, get_query_payload=True)
    if not isinstance(payload, Mapping):
        raise ResponseFormatError("invalid SIMBAD query payload")
    query = payload.get("QUERY")
    if not isinstance(query, str) or not query or len(query) > _SIMBAD_MAX_QUERY_CHARS:
        raise ResponseFormatError("invalid SIMBAD ADQL query")
    if re.search(r"\bSELECT\s+TOP\s+1\b", query, flags=re.IGNORECASE) is None:
        raise ResponseFormatError("SIMBAD ADQL query is not row bounded")
    return query


def _finite_number(value: object, *, field: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ResponseFormatError(f"invalid SIMBAD {field} value")
    result = float(value)
    if not math.isfinite(result):
        raise ResponseFormatError(f"invalid SIMBAD {field} value")
    return result


def _optional_text(value: object, *, field: str) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str) or len(value) > _SIMBAD_MAX_TEXT_CHARS:
        raise ResponseFormatError(f"invalid SIMBAD {field} value")
    return value or None


def _validate_simbad_payload(payload: object) -> SimbadRow | None:
    if not isinstance(payload, Mapping):
        raise ResponseFormatError("invalid SIMBAD JSON root")
    metadata = payload.get("metadata")
    data = payload.get("data")
    if not isinstance(metadata, list) or not isinstance(data, list):
        raise ResponseFormatError("invalid SIMBAD JSON table")
    if not metadata or len(metadata) > _SIMBAD_MAX_COLUMNS:
        raise ResponseFormatError("invalid SIMBAD metadata column count")

    column_indexes: dict[str, int] = {}
    for index, item in enumerate(metadata):
        if not isinstance(item, Mapping):
            raise ResponseFormatError("invalid SIMBAD metadata entry")
        name = item.get("name")
        if (
            not isinstance(name, str)
            or not name
            or len(name) > _SIMBAD_MAX_COLUMN_NAME_CHARS
            or _SIMBAD_COLUMN_NAME.fullmatch(name) is None
        ):
            raise ResponseFormatError("invalid SIMBAD metadata column name")
        normalized = name.casefold()
        if normalized in column_indexes:
            raise ResponseFormatError("duplicate SIMBAD metadata column")
        column_indexes[normalized] = index

    if not _SIMBAD_REQUIRED_COLUMNS <= column_indexes.keys():
        raise ResponseFormatError("SIMBAD response is missing required columns")
    if len(data) > 1:
        raise ResponseFormatError("SIMBAD response exceeded MAXREC")
    if not data:
        return None

    raw_row = data[0]
    if not isinstance(raw_row, list) or len(raw_row) != len(metadata):
        raise ResponseFormatError("invalid SIMBAD data row width")

    def cell(name: str) -> Any:
        return raw_row[column_indexes[name]]

    ra_deg = _finite_number(cell("ra"), field="RA")
    dec_deg = _finite_number(cell("dec"), field="Dec")
    if not 0.0 <= ra_deg < 360.0:
        raise ResponseFormatError("SIMBAD RA is out of range")
    if not -90.0 <= dec_deg <= 90.0:
        raise ResponseFormatError("SIMBAD Dec is out of range")

    raw_v = cell("v")
    v_magnitude = None if raw_v is None else _finite_number(raw_v, field="V")
    return SimbadRow(
        ra_deg=ra_deg,
        dec_deg=dec_deg,
        otype=_optional_text(cell("otype"), field="otype"),
        v_magnitude=v_magnitude,
        sp_type=_optional_text(cell("sp_type"), field="sp_type"),
    )


def _query_simbad_object(name: str) -> SimbadRow | None:
    query = _build_simbad_query(name)
    response = requests_request_bounded(
        "POST",
        SIMBAD_TAP_SYNC_URL,
        limits=_SIMBAD_BODY_LIMITS,
        mime_policy=JSON_MIME_POLICY,
        headers={"Accept": "application/json"},
        request_kwargs={
            "data": {
                "REQUEST": "doQuery",
                "LANG": "ADQL",
                "FORMAT": "json",
                "MAXREC": "1",
                "QUERY": query,
            },
            "timeout": (
                SIMBAD_CONNECT_TIMEOUT_SECONDS,
                SIMBAD_REQUEST_TIMEOUT_SECONDS,
            ),
        },
        total_timeout_seconds=SIMBAD_TRANSPORT_TOTAL_TIMEOUT_SECONDS,
    )
    return _validate_simbad_payload(
        parse_bounded_json(response, limits=_SIMBAD_JSON_LIMITS)
    )


async def handle_obj(args: str, context) -> str:
    """处理天文对象查询命令"""
    args = args.strip()
    if not args:
        return "请提供天体名称\n示例: /astro obj Crab Pulsar\n或: /astro obj sun"

    obj_name = args.lower()

    # from .obj import SOLAR_SYSTEM # if circular import was an issue, but here it is same file

    if obj_name in SOLAR_SYSTEM:
        return SOLAR_SYSTEM[obj_name]()

    # 从SIMBAD查询其他天体
    try:
        from astropy import units as u
        from astropy.coordinates import SkyCoord

        result = await asyncio.wait_for(
            asyncio.to_thread(_query_simbad_object, args),
            timeout=SIMBAD_TOTAL_TIMEOUT_SECONDS,
        )

        if result is None:
            return f"未找到天体: {args}\n\n提示: 可以尝试使用英文名称，如 'Crab Nebula', 'Betelgeuse' 等"

        coord = SkyCoord(ra=result.ra_deg * u.deg, dec=result.dec_deg * u.deg, frame='icrs')
        ra_hms = coord.ra.to_string(unit=u.hour, sep=':', pad=True, precision=2)
        dec_dms = coord.dec.to_string(unit=u.deg, sep=':', pad=True, precision=1, alwayssign=True)

        result_text = f"🌟 {args}\n\n"
        result_text += "**坐标 (J2000):**\n"
        result_text += f"RA: {ra_hms} ({coord.ra.deg:.6f}°)\n"
        result_text += f"Dec: {dec_dms} ({coord.dec.deg:.6f}°)\n\n"

        # 添加天体类型
        if result.otype:
            result_text += f"类型: {result.otype}\n"

        # 添加V波段星等
        if result.v_magnitude is not None:
            result_text += f"V星等: {result.v_magnitude:.2f}\n"

        # 添加光谱型
        if result.sp_type:
            result_text += f"光谱型: {result.sp_type}"

        return result_text
    except asyncio.TimeoutError:
        return "SIMBAD 查询超时，请稍后再试。"
    except Exception as exc:
        return public_error_message(
            context, exc, logger=context.logger, component="astro_tools.obj"
        )


def _get_sun_info() -> str:
    """获取太阳信息"""
    from astropy import constants as const
    from astropy import units as u

    mass = const.M_sun.to(u.kg)
    radius = const.R_sun.to(u.km)
    luminosity = const.L_sun.to(u.W)

    return f"☀️ 太阳 (Sun)\n\n" \
           f"**基本参数:**\n" \
           f"质量: {mass.value:.3e} kg (1 M☉)\n" \
           f"半径: {radius.value:,.0f} km (109 R⊕)\n" \
           f"光度: {luminosity.value:.3e} W (1 L☉)\n" \
           f"有效温度: 5778 K\n" \
           f"光谱型: G2V\n" \
           f"年龄: ~4.6 Gyr\n\n" \
           f"**轨道参数:**\n" \
           f"到地球平均距离: 1 AU = 1.496×10⁸ km\n" \
           f"银河系轨道周期: ~230 Myr\n\n" \
           f"**组成:**\n" \
           f"氢: ~73%\n" \
           f"氦: ~25%\n" \
           f"其他元素: ~2%"


def _get_moon_info() -> str:
    """获取月球信息"""
    return "🌙 月球 (Moon)\n\n" \
           "**基本参数:**\n" \
           "质量: 7.342×10²² kg (0.0123 M⊕)\n" \
           "半径: 1,737 km (0.273 R⊕)\n" \
           "密度: 3.344 g/cm³\n" \
           "表面重力: 1.62 m/s² (0.165 g)\n" \
           "逃逸速度: 2.38 km/s\n\n" \
           "**轨道参数:**\n" \
           "半长轴: 384,400 km\n" \
           "轨道周期: 27.32 天 (恒星月)\n" \
           "同步自转周期: 27.32 天\n" \
           "轨道离心率: 0.0549\n" \
           "轨道倾角: 5.145°\n\n" \
           "**表面特征:**\n" \
           "表面温度: -173°C 到 127°C\n" \
           "月球正面总有朝向地球（潮汐锁定）"


def _get_earth_info() -> str:
    """获取地球信息"""
    return "🌍 地球 (Earth)\n\n" \
           "**基本参数:**\n" \
           "质量: 5.972×10²⁴ kg (1 M⊕)\n" \
           "赤道半径: 6,378 km (1 R⊕)\n" \
           "极半径: 6,357 km\n" \
           "平均密度: 5.514 g/cm³\n" \
           "表面重力: 9.807 m/s² (1 g)\n" \
           "逃逸速度: 11.2 km/s\n\n" \
           "**轨道参数:**\n" \
           "半长轴: 1 AU = 1.496×10⁸ km\n" \
           "轨道周期: 365.25 天\n" \
           "自转周期: 23小时56分4秒\n" \
           "轨道离心率: 0.0167\n" \
           "轨道倾角: 0° (定义)\n" \
           "自转轴倾角: 23.44°\n\n" \
           "**大气组成:**\n" \
           "氮: 78%\n" \
           "氧: 21%\n" \
           "氩和其他: 1%"


def _get_mercury_info() -> str:
    """获取水星信息"""
    return "☿️ 水星 (Mercury)\n\n" \
           "**基本参数:**\n" \
           "质量: 3.30×10²³ kg (0.055 M⊕)\n" \
           "半径: 2,440 km (0.383 R⊕)\n" \
           "密度: 5.427 g/cm³\n" \
           "表面重力: 3.7 m/s² (0.38 g)\n\n" \
           "**轨道参数:**\n" \
           "半长轴: 0.387 AU\n" \
           "轨道周期: 87.97 天\n" \
           "自转周期: 58.65 天\n" \
           "轨道离心率: 0.206 (太阳系最大)\n\n" \
           "**特点:**\n" \
           "表面温度: -173°C 到 427°C\n" \
           "几乎没有大气\n" \
           "表面有大量陨石坑"


def _get_venus_info() -> str:
    """获取金星信息"""
    return "♀️ 金星 (Venus)\n\n" \
           "**基本参数:**\n" \
           "质量: 4.87×10²⁴ kg (0.815 M⊕)\n" \
           "半径: 6,052 km (0.949 R⊕)\n" \
           "密度: 5.243 g/cm³\n" \
           "表面重力: 8.87 m/s² (0.91 g)\n\n" \
           "**轨道参数:**\n" \
           "半长轴: 0.723 AU\n" \
           "轨道周期: 224.7 天\n" \
           "自转周期: 243 天 (逆向)\n" \
           "轨道离心率: 0.007 (最接近圆形)\n\n" \
           "**特点:**\n" \
           "表面温度: ~462°C (太阳系最热)\n" \
           "浓厚的CO₂大气\n" \
           "表面气压: 92个地球大气压\n" \
           "强烈的温室效应"


def _get_mars_info() -> str:
    """获取火星信息"""
    return "♂️ 火星 (Mars)\n\n" \
           "**基本参数:**\n" \
           "质量: 6.42×10²³ kg (0.107 M⊕)\n" \
           "半径: 3,390 km (0.532 R⊕)\n" \
           "密度: 3.934 g/cm³\n" \
           "表面重力: 3.71 m/s² (0.38 g)\n\n" \
           "**轨道参数:**\n" \
           "半长轴: 1.524 AU\n" \
           "轨道周期: 687 天\n" \
           "自转周期: 24.6 小时\n" \
           "轨道离心率: 0.093\n" \
           "自转轴倾角: 25.19°\n\n" \
           "**特点:**\n" \
           "表面温度: -140°C 到 20°C\n" \
           "稀薄的CO₂大气\n" \
           "有两颗小卫星: 火卫一和火卫二\n" \
           "表面有极冠和古河道"


def _get_jupiter_info() -> str:
    """获取木星信息"""
    return "♃ 木星 (Jupiter)\n\n" \
           "**基本参数:**\n" \
           "质量: 1.90×10²⁷ kg (318 M⊕)\n" \
           "赤道半径: 71,492 km (11.2 R⊕)\n" \
           "密度: 1.326 g/cm³\n" \
           "表面重力: 24.79 m/s² (2.53 g)\n\n" \
           "**轨道参数:**\n" \
           "半长轴: 5.204 AU\n" \
           "轨道周期: 11.86 年\n" \
           "自转周期: 9.93 小时 (最快)\n" \
           "轨道离心率: 0.049\n\n" \
           "**特点:**\n" \
           "气态巨行星\n" \
           "大红斑 - 巨型风暴\n" \
           "已知卫星: 95颗+\n" \
           "主要卫星: 木卫一、二、三、四(伽利略卫星)\n" \
           "强大的磁场"


def _get_saturn_info() -> str:
    """获取土星信息"""
    return "♄ 土星 (Saturn)\n\n" \
           "**基本参数:**\n" \
           "质量: 5.68×10²⁶ kg (95.2 M⊕)\n" \
           "赤道半径: 60,268 km (9.45 R⊕)\n" \
           "密度: 0.687 g/cm³ (最低)\n" \
           "表面重力: 10.44 m/s² (1.07 g)\n\n" \
           "**轨道参数:**\n" \
           "半长轴: 9.537 AU\n" \
           "轨道周期: 29.46 年\n" \
           "自转周期: 10.66 小时\n" \
           "轨道离心率: 0.056\n\n" \
           "**特点:**\n" \
           "壮观的行星环系统\n" \
           "已知卫星: 146颗+\n" \
           "最大卫星: 土卫六(泰坦)，有浓厚大气\n" \
           "密度小于水"


def _get_uranus_info() -> str:
    """获取天王星信息"""
    return "♅ 天王星 (Uranus)\n\n" \
           "**基本参数:**\n" \
           "质量: 8.68×10²⁵ kg (14.5 M⊕)\n" \
           "赤道半径: 25,559 km (4.01 R⊕)\n" \
           "密度: 1.270 g/cm³\n" \
           "表面重力: 8.69 m/s² (0.89 g)\n\n" \
           "**轨道参数:**\n" \
           "半长轴: 19.19 AU\n" \
           "轨道周期: 84.02 年\n" \
           "自转周期: 17.24 小时 (逆向)\n" \
           "轨道离心率: 0.046\n" \
           "自转轴倾角: 97.77° (几乎侧躺)\n\n" \
           "**特点:**\n" \
           "冰巨星\n" \
           "蓝绿色(甲烷大气)\n" \
           "已知卫星: 27颗\n" \
           "有暗淡的行星环"


def _get_neptune_info() -> str:
    """获取海王星信息"""
    return "♆ 海王星 (Neptune)\n\n" \
           "**基本参数:**\n" \
           "质量: 1.02×10²⁶ kg (17.1 M⊕)\n" \
           "赤道半径: 24,764 km (3.88 R⊕)\n" \
           "密度: 1.638 g/cm³\n" \
           "表面重力: 11.15 m/s² (1.14 g)\n\n" \
           "**轨道参数:**\n" \
           "半长轴: 30.07 AU\n" \
           "轨道周期: 164.8 年\n" \
           "自转周期: 16.11 小时\n" \
           "轨道离心率: 0.009\n\n" \
           "**特点:**\n" \
           "冰巨星\n" \
           "深蓝色(甲烷大气)\n" \
           "太阳系风速最快的行星\n" \
           "已知卫星: 14颗\n" \
           "最大卫星: 海卫一(特里同)，逆向轨道"


# 太阳系天体字典
SOLAR_SYSTEM = {
    'sun': _get_sun_info,
    'moon': _get_moon_info,
    'earth': _get_earth_info,
    'mercury': _get_mercury_info,
    'venus': _get_venus_info,
    'mars': _get_mars_info,
    'jupiter': _get_jupiter_info,
    'saturn': _get_saturn_info,
    'uranus': _get_uranus_info,
    'neptune': _get_neptune_info,
}
