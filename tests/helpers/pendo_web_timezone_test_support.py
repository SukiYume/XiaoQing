"""Pendo Web 时区模块的 Node 测试内联辅助。"""

from pathlib import Path


def inline_timezone_runtime(source_path: Path) -> str:
    """内联真实的纯转换函数，并用确定性用户时区替代网络读取。"""

    source = source_path.read_text(encoding="utf-8")
    source = source.replace("import { api } from '../api.js';", "")
    source = source.replace(
        "export async function fetchUserTimeZone()",
        "async function _unusedFetchUserTimeZone()",
    )
    source = source.replace("export function ", "function ")
    return f"""
process.env.TZ = 'America/Los_Angeles';
const {{
    setUserTimeZone,
    getUserTimeZone,
    formatZonedDateTime,
    formatZonedMonthDay,
    formatZonedTime,
    todayInUserTimeZone,
    zonedDateKey,
    zonedDateParts,
    zonedDateTimeToInput,
    zonedInputToUtcIso,
    zonedInstantEpoch,
}} = (() => {{
{source}
    return {{
        setUserTimeZone,
        getUserTimeZone,
        formatZonedDateTime,
        formatZonedMonthDay,
        formatZonedTime,
        todayInUserTimeZone,
        zonedDateKey,
        zonedDateParts,
        zonedDateTimeToInput,
        zonedInputToUtcIso,
        zonedInstantEpoch,
    }};
}})();
setUserTimeZone(globalThis.__userTimeZone || 'Asia/Shanghai');
const fetchUserTimeZone = async () => {{
    if (globalThis.__userTimeZoneError) throw globalThis.__userTimeZoneError;
    return setUserTimeZone(globalThis.__userTimeZone || 'Asia/Shanghai');
}};
"""


__all__ = ("inline_timezone_runtime",)
