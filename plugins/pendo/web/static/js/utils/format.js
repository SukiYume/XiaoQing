/** Pendo Web 共享的数据边界、本地日期、文本预览和人民币金额格式化工具。 */

const DATE_KEY_PATTERN = /^\d{4}-\d{2}-\d{2}$/;

export function isRecord(value) {
    return value !== null && typeof value === 'object' && !Array.isArray(value);
}

export function arrayValue(value) {
    return Array.isArray(value) ? value : [];
}

export function records(value) {
    return arrayValue(value).filter(isRecord);
}

export function finiteNumber(value, fallback = 0) {
    try {
        const number = Number(value);
        if (!Number.isFinite(number)) return fallback;
        return Object.is(number, -0) ? 0 : number;
    } catch {
        return fallback;
    }
}

export function nonNegativeInteger(value) {
    return Math.max(0, Math.trunc(finiteNumber(value)));
}

/** 保留接口中的标量文本语义；对象和数组一律回退。 */
export function textValue(value, fallback = '') {
    if (typeof value === 'string') return value;
    if (typeof value === 'number' || typeof value === 'boolean') return String(value);
    return fallback;
}

/** 表单和标识字段只接受字符串，并在边界处去除首尾空白。 */
export function trimmedTextValue(value, fallback = '') {
    return typeof value === 'string' ? value.trim() : fallback;
}

/** 设置字段不允许空字符串覆盖稳定默认值。 */
export function nonEmptyTextValue(value, fallback = '') {
    return trimmedTextValue(value) || fallback;
}

export function errorMessage(error, fallback = '未知错误') {
    const nested = isRecord(error) ? trimmedTextValue(error.message) : '';
    return nested || trimmedTextValue(error) || fallback;
}

export function pad2(value) {
    return String(value).padStart(2, '0');
}

export function isoDate(date) {
    if (!(date instanceof Date) || Number.isNaN(date.getTime())) return '';
    return `${date.getFullYear()}-${pad2(date.getMonth() + 1)}-${pad2(date.getDate())}`;
}

export function todayStr() {
    return isoDate(new Date());
}

export function parseDate(value) {
    let date;
    let text = '';

    if (value instanceof Date) {
        date = new Date(value.getTime());
    } else if (typeof value === 'number') {
        if (!Number.isFinite(value)) return null;
        date = new Date(value);
    } else if (typeof value === 'string') {
        text = value.trim();
        if (!text) return null;
        // 纯日期按本地午夜解析，避免浏览器把 YYYY-MM-DD 当作 UTC。
        date = new Date(DATE_KEY_PATTERN.test(text) ? `${text}T00:00:00` : text);
    } else {
        return null;
    }

    if (Number.isNaN(date.getTime())) return null;
    if (DATE_KEY_PATTERN.test(text) && isoDate(date) !== text) return null;
    return date;
}

export function isValidDateInput(value) {
    const text = String(value ?? '').trim();
    return DATE_KEY_PATTERN.test(text) && parseDate(text) !== null;
}

export function formatMonthDay(value, fallback = '未知时间') {
    const date = parseDate(value);
    return date ? `${date.getMonth() + 1}/${date.getDate()}` : fallback;
}

export function formatDateTime(value, fallback = '未知时间') {
    const date = parseDate(value);
    if (!date) return fallback;
    return `${isoDate(date)} ${pad2(date.getHours())}:${pad2(date.getMinutes())}`;
}

export function previewText(value, maxLength = 100) {
    const text = String(value ?? '').trim();
    if (!text) return '';

    const limit = Number.isInteger(maxLength) && maxLength >= 0 ? maxLength : 100;
    const characters = Array.from(text);
    return characters.length <= limit ? text : `${characters.slice(0, limit).join('')}...`;
}

export function noteCadenceSubtitle(granularity, rangeLabel) {
    const label = nonEmptyTextValue(rangeLabel, '当前范围');
    if (granularity === 'year') return `按${label}查看每年新增笔记数量。`;
    if (granularity === 'month') return `按${label}查看每月新增笔记数量。`;
    if (granularity === 'week') return `按${label}查看每周新增笔记数量。`;
    return `按${label}查看每天的笔记输入频率。`;
}

export function formatAmount(value) {
    const amount = finiteNumber(value);
    const sign = amount < 0 ? '-' : '';
    return `${sign}¥${Math.abs(amount).toLocaleString('zh-CN', {
        minimumFractionDigits: 2,
        maximumFractionDigits: 2,
    })}`;
}

export function formatMoneyCompact(value) {
    const amount = finiteNumber(value);
    const sign = amount < 0 ? '-' : '';
    const absolute = Math.abs(amount);
    if (absolute >= 10000) return `${sign}¥${(absolute / 10000).toFixed(1)}万`;
    if (absolute >= 1000) return `${sign}¥${(absolute / 1000).toFixed(1)}k`;
    return `${sign}¥${absolute.toFixed(0)}`;
}
