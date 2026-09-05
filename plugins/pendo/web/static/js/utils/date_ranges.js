/** 列表与统计页面共享的用户时区自然日期范围和数据首尾边界查询。 */

import { isoDate } from './format.js';
import { todayInUserTimeZone } from './timezone.js';

export const RANGE_PRESET_OPTIONS = [
    { key: 'week', label: '本周' },
    { key: 'month', label: '本月' },
    { key: 'quarter', label: '本季' },
    { key: 'year', label: '今年' },
    { key: 'last_year', label: '去年' },
    { key: 'custom', label: '自定义' },
    { key: 'all', label: '全部' },
];

/** 保留范围层的语义化名称，但不再增加一次无意义函数调用。 */
export { todayInUserTimeZone as todayRangeKey };

function normalizeDateKey(value) {
    if (typeof value !== 'string') return '';
    const key = value.trim().slice(0, 10);
    if (!/^\d{4}-\d{2}-\d{2}$/.test(key)) return '';

    const date = new Date(`${key}T00:00:00`);
    return !Number.isNaN(date.getTime()) && isoDate(date) === key ? key : '';
}

function resolveToday(today) {
    const fallback = normalizeDateKey(todayInUserTimeZone());
    if (!fallback) throw new RangeError('用户时区当天日期无效');
    const key = normalizeDateKey(today) || fallback;
    return { date: new Date(`${key}T00:00:00`), key };
}

export function derivePresetRange(
    preset,
    {
        today = todayInUserTimeZone(),
        customStart = '',
        customEnd = '',
        customFallback = 'month',
        allStart = '1970-01-01',
        allEnd = '',
    } = {},
) {
    const { date: base, key: todayKey } = resolveToday(today);
    const normalizedPreset = String(preset || '').trim();

    // 自然周期始终覆盖完整区间，不在今天提前截断。
    switch (normalizedPreset) {
        case 'week': {
            const monday = new Date(base);
            monday.setDate(base.getDate() - ((base.getDay() + 6) % 7));
            const sunday = new Date(monday);
            sunday.setDate(monday.getDate() + 6);
            return { start: isoDate(monday), end: isoDate(sunday) };
        }
        case 'month':
            return {
                start: isoDate(new Date(base.getFullYear(), base.getMonth(), 1)),
                end: isoDate(new Date(base.getFullYear(), base.getMonth() + 1, 0)),
            };
        case 'quarter': {
            const firstMonth = Math.floor(base.getMonth() / 3) * 3;
            return {
                start: isoDate(new Date(base.getFullYear(), firstMonth, 1)),
                end: isoDate(new Date(base.getFullYear(), firstMonth + 3, 0)),
            };
        }
        case 'year':
            return {
                start: isoDate(new Date(base.getFullYear(), 0, 1)),
                end: isoDate(new Date(base.getFullYear(), 11, 31)),
            };
        case 'last_year': {
            const year = base.getFullYear() - 1;
            return { start: `${year}-01-01`, end: `${year}-12-31` };
        }
        case 'custom': {
            const start = normalizeDateKey(customStart);
            const end   = normalizeDateKey(customEnd);
            if (start && end) return { start, end };
            if (!customFallback) return { start: '', end: '' };
            return derivePresetRange(customFallback, {
                today: todayKey,
                allStart,
                allEnd,
                customFallback: '',
            });
        }
        case 'all':
            return {
                start: normalizeDateKey(allStart) || '1970-01-01',
                end: normalizeDateKey(allEnd) || todayKey,
            };
        case 'today':
        default:
            return { start: todayKey, end: todayKey };
    }
}

/** 各取最早和最晚一条记录，得到“全部”筛选所需的稳定日期边界。 */
export async function fetchItemRangeBounds(
    apiClient,
    {
        type,
        sortField,
        startField = sortField,
        endField = startField,
        fallbackEnd = todayInUserTimeZone(),
        minimumEnd = '',
    },
) {
    const [earliestRes, latestRes] = await Promise.all([
        apiClient.get('/items', {
            type,
            sort: sortField,
            order: 'asc',
            page: 1,
            page_size: 1,
        }),
        apiClient.get('/items', {
            type,
            sort: sortField,
            order: 'desc',
            page: 1,
            page_size: 1,
        }),
    ]);
    const earliestItem = earliestRes?.data?.items?.[0];
    const latestItem   = latestRes?.data?.items?.[0];
    const fallback     = resolveToday(fallbackEnd).key;
    const start        = normalizeDateKey(earliestItem?.[startField]) || fallback;
    const latestStart  = normalizeDateKey(latestItem?.[startField]);
    let end            = normalizeDateKey(latestItem?.[endField]) || latestStart || fallback;

    // 防止损坏数据、空集合或最小展示日产生反向范围。
    for (const lowerBound of [latestStart, normalizeDateKey(minimumEnd), start]) {
        if (lowerBound > end) end = lowerBound;
    }
    return { start, end };
}
