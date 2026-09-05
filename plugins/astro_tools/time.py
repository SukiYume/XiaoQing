"""
时间转换模块
"""

import math
import re
from typing import Any

from core.plugin_base import PluginContextProtocol, run_sync
from core.public_errors import public_error_message

UNIX_TIMESTAMP_MIN_ABS = 100_000_000
_JD_MIN_ABS            = 2_400_000
_NUMERIC_TIME          = re.compile(r"[+-]?(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][+-]?\d+)?\Z")


def _build_time_response(label: str, t: Any) -> str:
    return (
        f"🕐 {label} 转换结果\n"
        f"UTC: {t.iso}\n"
        f"JD: {t.jd:.6f}\n"
        f"MJD: {t.mjd:.6f}\n"
        f"Unix: {t.unix:.2f}\n"
        f"格林威治恒星时: {t.sidereal_time('apparent', longitude='greenwich').to_string(sep=':', precision=0)}"
    )


def _parse_numeric_time(value: float, Time: Any) -> tuple[Any, str]:
    """按数量级区分无显式前缀的 Unix、JD 与 MJD 数值。"""

    abs_value = abs(value)
    if abs_value >= UNIX_TIMESTAMP_MIN_ABS:
        return Time(value, format="unix"), "Unix"
    if abs_value >= _JD_MIN_ABS:
        return Time(value, format="jd"), "JD"
    return Time(value, format="mjd"), "MJD"


def _finite_float(value: str) -> float:
    """解析有限浮点数，统一阻断 NaN/Inf。"""

    result = float(value)
    if not math.isfinite(result):
        raise ValueError("time value must be finite")
    return result


def _handle_time_sync(args: str, context: PluginContextProtocol) -> str:
    """处理时间转换命令"""
    args = args.strip()

    try:
        from astropy.time import Time

        # 空参数与显式 now 共用同一条当前时间路径。
        if not args or args.casefold() == "now":
            return _build_time_response("当前天文时间", Time.now())

        # 解析子命令
        parts      = args.split(None, 1)
        subcommand = parts[0].casefold()

        # 处理 jd 子命令
        if subcommand == "jd":
            if len(parts) < 2:
                return "请提供儒略日数值\n示例: /astro time jd 2460419.5"
            try:
                jd_val = _finite_float(parts[1])
                t = Time(jd_val, format="jd")
                return _build_time_response(f"JD {jd_val}", t)
            except (ValueError, OverflowError):
                return "无效的儒略日数值"

        # 处理 mjd 子命令
        if subcommand == "mjd":
            if len(parts) < 2:
                return "请提供修正儒略日数值\n示例: /astro time mjd 60419.5"
            try:
                mjd_val = _finite_float(parts[1])
                t = Time(mjd_val, format="mjd")
                return _build_time_response(f"MJD {mjd_val}", t)
            except (ValueError, OverflowError):
                return "无效的修正儒略日数值"

        if subcommand == "unix":
            if len(parts) < 2:
                return "请提供 Unix 时间戳\n示例: /astro time unix 1706616000"
            try:
                unix_value = _finite_float(parts[1])
                return _build_time_response(f"Unix {unix_value}", Time(unix_value, format="unix"))
            except (ValueError, OverflowError):
                return "无效的 Unix 时间戳"

        # 处理一般的时间格式（单个数字依次识别 Unix / JD / MJD）
        numeric_match = _NUMERIC_TIME.fullmatch(args)
        if numeric_match:
            numeric_value = _finite_float(numeric_match.group(0))
            t, time_type = _parse_numeric_time(numeric_value, Time)
            return _build_time_response(f"{time_type} {numeric_value}", t)

        # 处理其他时间格式（ISO等）
        t = Time(args)
        return _build_time_response(args, t)
    except ValueError:
        return "时间格式错误，请检查输入。\n\n支持的格式:\n- ISO: 2026-01-30 或 2026-01-30T12:00:00\n- JD: 2460419.5 (使用 'jd' 子命令)\n- MJD: 60419.5 (使用 'mjd' 子命令)\n- Unix时间戳: 1706616000 或 unix 1706616000"
    except Exception as exc:
        return public_error_message(
            context, exc, logger=context.logger, component="astro_tools.time"
        )


async def handle_time(args: str, context: PluginContextProtocol) -> str:
    """在线程 bulkhead 中执行 astropy 时间计算。"""

    return await run_sync(_handle_time_sync, args, context)
