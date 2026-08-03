/** Pendo Web 的用户时区读取与严格墙钟转换。 */

import { api } from '../api.js';

const WALL_DATETIME_PATTERN =
    /^(\d{4})-(\d{2})-(\d{2})[T ](\d{2}):(\d{2})(?::(\d{2}))?$/;
const AWARE_SUFFIX_PATTERN = /(?:Z|[+-]\d{2}:?\d{2})$/i;
const OFFSET_SAMPLE_HOURS = 48;
const OFFSET_SAMPLE_STEP_HOURS = 3;

function formatterFor(timeZone) {
    const name = String(timeZone || '').trim();
    if (!name) throw new RangeError('用户时区未配置');
    try {
        return new Intl.DateTimeFormat('en-CA-u-ca-iso8601-nu-latn', {
            timeZone: name,
            year: 'numeric',
            month: '2-digit',
            day: '2-digit',
            hour: '2-digit',
            minute: '2-digit',
            second: '2-digit',
            hourCycle: 'h23',
        });
    } catch {
        throw new RangeError(`无效的用户时区：${name}`);
    }
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
    };
    if (parts.month < 1 || parts.month > 12 || parts.hour > 23 || parts.minute > 59 || parts.second > 59) {
        return null;
    }
    const probe = new Date(0);
    probe.setUTCFullYear(parts.year, parts.month - 1, parts.day);
    probe.setUTCHours(parts.hour, parts.minute, parts.second, 0);
    if (
        probe.getUTCFullYear() !== parts.year ||
        probe.getUTCMonth() !== parts.month - 1 ||
        probe.getUTCDate() !== parts.day ||
        probe.getUTCHours() !== parts.hour ||
        probe.getUTCMinutes() !== parts.minute ||
        probe.getUTCSeconds() !== parts.second
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

export async function fetchUserTimeZone() {
    const response = await api.get('/settings');
    const timeZone = String(response?.data?.timezone ?? '').trim();
    formatterFor(timeZone);
    return timeZone;
}

/** 把 ISO 时刻显示为指定 IANA 时区的 datetime-local 值；旧朴素值保持原墙钟。 */
export function zonedDateTimeToInput(value, timeZone) {
    const text = String(value ?? '').trim();
    if (!text) return '';
    const naive = wallParts(text);
    if (naive && !AWARE_SUFFIX_PATTERN.test(text)) return inputValue(naive);

    const date = new Date(text);
    if (Number.isNaN(date.getTime())) return '';
    return inputValue(partsAt(formatterFor(timeZone), date.getTime()));
}

/**
 * 把指定 IANA 时区中的墙钟解析为唯一 UTC 时刻。
 * DST 跳时和回拨重叠都拒绝，避免浏览器自行猜测造成静默改时。
 */
export function zonedInputToUtcIso(value, timeZone) {
    const wall = wallParts(value);
    if (!wall) return '';
    const formatter = formatterFor(timeZone);
    const offsets = new Set();
    const hourMs = 60 * 60 * 1000;
    for (let hours = -OFFSET_SAMPLE_HOURS; hours <= OFFSET_SAMPLE_HOURS; hours += OFFSET_SAMPLE_STEP_HOURS) {
        const sampleEpoch = wall.epoch + hours * hourMs;
        const sampleWall = partsAt(formatter, sampleEpoch);
        const sampleAsUtc = new Date(0);
        sampleAsUtc.setUTCFullYear(sampleWall.year, sampleWall.month - 1, sampleWall.day);
        sampleAsUtc.setUTCHours(sampleWall.hour, sampleWall.minute, sampleWall.second, 0);
        offsets.add(sampleAsUtc.getTime() - sampleEpoch);
    }

    const candidates = new Set();
    for (const offset of offsets) {
        const candidate = wall.epoch - offset;
        if (sameWallTime(partsAt(formatter, candidate), wall)) candidates.add(candidate);
    }
    if (candidates.size === 0) {
        throw new RangeError(`该时间在 ${timeZone} 不存在（可能处于夏令时跳时区间）`);
    }
    if (candidates.size > 1) {
        throw new RangeError(`该时间在 ${timeZone} 对应两个时刻，请避开夏令时回拨区间`);
    }
    return new Date([...candidates][0]).toISOString().replace('.000Z', '+00:00');
}
