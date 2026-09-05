/** Pendo Web 的用户时区读取与严格墙钟转换。 */

import { api } from '../api.js';

const DATE_KEY_PATTERN      = /^\d{4}-\d{2}-\d{2}$/;
const WALL_DATETIME_PATTERN =     /^(\d{4})-(\d{2})-(\d{2})[T ](\d{2}):(\d{2})(?::(\d{2})(?:\.(\d{1,6}))?)?$/;
const AWARE_SUFFIX_PATTERN     = /(?:Z|[+-]\d{2}:?\d{2})$/i;
const OFFSET_SAMPLE_HOURS      = 48;
const OFFSET_SAMPLE_STEP_HOURS = 3;
const FORMATTER_CACHE          = new Map();

let _userTimeZone = '';

function formatterFor(timeZone) {
    const name = String(timeZone || '').trim();
    if (!name) throw new RangeError('用户时区未配置');
    const cached = FORMATTER_CACHE.get(name);
    if (cached) return cached;
    try {
        const formatter = new Intl.DateTimeFormat('en-CA-u-ca-iso8601-nu-latn', {
            timeZone: name,
            year: 'numeric',
            month: '2-digit',
            day: '2-digit',
            hour: '2-digit',
            minute: '2-digit',
            second: '2-digit',
            hourCycle: 'h23',
        });
        FORMATTER_CACHE.set(name, formatter);
        return formatter;
    } catch {
        throw new RangeError(`无效的用户时区：${name}`);
    }
}

function resolveTimeZone(timeZone) {
    const explicit = String(timeZone || '').trim();
    return explicit || getUserTimeZone();
}

function wallParts(value) {
    const match = String(value ?? '').trim().match(WALL_DATETIME_PATTERN);
    if (!match) return null;
    const parts = {
        year: Number(match[1]),
        month: Number(match[2]),
        day: Number(match[3]),
        hour: Number(match[4]),
        minute: Number(match[5]),
        second: Number(match[6] || '0'),
        millisecond: Number(String(match[7] || '').padEnd(3, '0').slice(0, 3) || '0'),
    };
    if (parts.month < 1 || parts.month > 12 || parts.hour > 23 || parts.minute > 59 || parts.second > 59) {
        return null;
    }
    const probe = new Date(0);
    probe.setUTCFullYear(parts.year, parts.month - 1, parts.day);
    probe.setUTCHours(parts.hour, parts.minute, parts.second, parts.millisecond);
    if (
        probe.getUTCFullYear() !== parts.year ||
        probe.getUTCMonth() !== parts.month - 1 ||
        probe.getUTCDate() !== parts.day ||
        probe.getUTCHours() !== parts.hour ||
        probe.getUTCMinutes() !== parts.minute ||
        probe.getUTCSeconds() !== parts.second ||
        probe.getUTCMilliseconds() !== parts.millisecond
    ) {
        return null;
    }
    return { ...parts, epoch: probe.getTime() };
}

function partsAt(formatter, epoch) {
    const values = {};
    for (const part of formatter.formatToParts(new Date(epoch))) {
        if (part.type !== 'literal') values[part.type] = Number(part.value);
    }
    return {
        year: values.year,
        month: values.month,
        day: values.day,
        hour: values.hour,
        minute: values.minute,
        second: values.second,
    };
}

function sameWallTime(left, right) {
    return (
        left.year === right.year &&
        left.month === right.month &&
        left.day === right.day &&
        left.hour === right.hour &&
        left.minute === right.minute &&
        left.second === right.second
    );
}

function inputValue(parts) {
    const pad = (value) => String(value).padStart(2, '0');
    return `${String(parts.year).padStart(4, '0')}-${pad(parts.month)}-${pad(parts.day)}T${pad(parts.hour)}:${pad(parts.minute)}`;
}

function wallDateTimeValue(parts) {
    const pad      = (value) => String(value).padStart(2, '0');
    const fraction = parts.millisecond ? `.${String(parts.millisecond).padStart(3, '0')}` : '';
    return `${String(parts.year).padStart(4, '0')}-${pad(parts.month)}-${pad(parts.day)}T${pad(parts.hour)}:${pad(parts.minute)}:${pad(parts.second)}${fraction}`;
}

export function setUserTimeZone(timeZone) {
    const name = String(timeZone || '').trim();
    formatterFor(name);
    _userTimeZone = name;
    return name;
}

export function getUserTimeZone() {
    if (!_userTimeZone) throw new RangeError('用户时区未配置');
    return _userTimeZone;
}

export async function fetchUserTimeZone() {
    const response = await api.get('/settings');
    const timeZone = String(response?.data?.timezone ?? '').trim();
    return setUserTimeZone(timeZone);
}

/** 把日期、旧墙钟或带偏移时刻解析成用户时区的展示字段。 */
export function zonedDateParts(value, timeZone = '') {
    if (typeof value === 'string') {
        const text = value.trim();
        if (!text) return null;
        if (DATE_KEY_PATTERN.test(text)) {
            return wallParts(`${text}T00:00:00`);
        }
        const wall = wallParts(text);
        if (wall && !AWARE_SUFFIX_PATTERN.test(text)) return wall;
    }

    let date;
    if (value instanceof Date) {
        date = new Date(value.getTime());
    } else if (typeof value === 'number') {
        if (!Number.isFinite(value)) return null;
        date = new Date(value);
    } else if (typeof value === 'string') {
        date = new Date(value.trim());
    } else {
        return null;
    }
    if (Number.isNaN(date.getTime())) return null;
    return partsAt(formatterFor(resolveTimeZone(timeZone)), date.getTime());
}

export function zonedDateKey(value, timeZone = '') {
    const parts = zonedDateParts(value, timeZone);
    if (!parts) return '';
    const pad = (part) => String(part).padStart(2, '0');
    return `${String(parts.year).padStart(4, '0')}-${pad(parts.month)}-${pad(parts.day)}`;
}

export function formatZonedTime(value, fallback = '未知时间', timeZone = '') {
    const parts = zonedDateParts(value, timeZone);
    if (!parts) return fallback;
    const pad = (part) => String(part).padStart(2, '0');
    return `${pad(parts.hour)}:${pad(parts.minute)}`;
}

export function formatZonedMonthDay(value, fallback = '未知时间', timeZone = '') {
    const parts = zonedDateParts(value, timeZone);
    return parts ? `${parts.month}/${parts.day}` : fallback;
}

export function formatZonedDateTime(value, fallback = '未知时间', timeZone = '') {
    const text = typeof value === 'string' ? value.trim() : '';
    if (DATE_KEY_PATTERN.test(text) && wallParts(`${text}T00:00:00`)) return text;
    const dateKey = zonedDateKey(value, timeZone);
    const time    = formatZonedTime(value, '', timeZone);
    return dateKey && time ? `${dateKey} ${time}` : fallback;
}

export function todayInUserTimeZone(now = new Date(), timeZone = '') {
    return zonedDateKey(now, timeZone);
}

/** 把带偏移时刻或用户墙钟转换成真实时间轴毫秒值。 */
export function zonedInstantEpoch(value, timeZone = '') {
    if (value instanceof Date) return Number.isNaN(value.getTime()) ? Number.NaN : value.getTime();
    if (typeof value === 'number') return Number.isFinite(value) ? value : Number.NaN;
    const text = String(value ?? '').trim();
    if (!text) return Number.NaN;
    if (AWARE_SUFFIX_PATTERN.test(text)) {
        const aware = new Date(text);
        return aware.getTime();
    }
    const wall = DATE_KEY_PATTERN.test(text) ? wallParts(`${text}T00:00:00`) : wallParts(text);
    if (!wall) return Number.NaN;
    try {
        return new Date(zonedInputToUtcIso(wallDateTimeValue(wall), resolveTimeZone(timeZone))).getTime();
    } catch {
        return Number.NaN;
    }
}

/** 把 ISO 时刻显示为指定 IANA 时区的 datetime-local 值；旧朴素值保持原墙钟。 */
export function zonedDateTimeToInput(value, timeZone = '') {
    const text = String(value ?? '').trim();
    if (!text) return '';
    const naive = wallParts(text);
    if (naive && !AWARE_SUFFIX_PATTERN.test(text)) return inputValue(naive);

    const date = new Date(text);
    if (Number.isNaN(date.getTime())) return '';
    return inputValue(partsAt(formatterFor(resolveTimeZone(timeZone)), date.getTime()));
}

/**
 * 把指定 IANA 时区中的墙钟解析为唯一 UTC 时刻。
 * DST 跳时和回拨重叠都拒绝，避免浏览器自行猜测造成静默改时。
 */
export function zonedInputToUtcIso(value, timeZone = '') {
    const wall = wallParts(value);
    if (!wall) return '';
    const resolvedTimeZone = resolveTimeZone(timeZone);
    const formatter        = formatterFor(resolvedTimeZone);
    const offsets          = new Set();
    const hourMs           = 60 * 60 * 1000;
    for (let hours = -OFFSET_SAMPLE_HOURS; hours <= OFFSET_SAMPLE_HOURS; hours += OFFSET_SAMPLE_STEP_HOURS) {
        const sampleEpoch       = wall.epoch + hours * hourMs;
        const sampleWall        = partsAt(formatter, sampleEpoch);
        const sampleMillisecond = ((sampleEpoch % 1000) + 1000) % 1000;
        const sampleAsUtc       = new Date(0);
        sampleAsUtc.setUTCFullYear(sampleWall.year, sampleWall.month - 1, sampleWall.day);
        sampleAsUtc.setUTCHours(
            sampleWall.hour,
            sampleWall.minute,
            sampleWall.second,
            sampleMillisecond,
        );
        offsets.add(sampleAsUtc.getTime() - sampleEpoch);
    }

    const candidates = new Set();
    for (const offset of offsets) {
        const candidate = wall.epoch - offset;
        if (sameWallTime(partsAt(formatter, candidate), wall)) candidates.add(candidate);
    }
    if (candidates.size === 0) {
        throw new RangeError(`该时间在 ${resolvedTimeZone} 不存在（可能处于夏令时跳时区间）`);
    }
    if (candidates.size > 1) {
        throw new RangeError(`该时间在 ${resolvedTimeZone} 对应两个时刻，请避开夏令时回拨区间`);
    }
    return new Date([...candidates][0]).toISOString().replace('.000Z', '+00:00');
}
