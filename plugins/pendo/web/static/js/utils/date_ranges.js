import { isoDate, todayStr as sharedTodayStr } from './format.js';

export const RANGE_PRESET_OPTIONS = [
    { key: 'week', label: '本周' },
    { key: 'month', label: '本月' },
    { key: 'quarter', label: '本季' },
    { key: 'year', label: '今年' },
    { key: 'last_year', label: '去年' },
    { key: 'custom', label: '自定义' },
    { key: 'all', label: '全部' },
];

export function todayRangeKey() {
    return sharedTodayStr();
}

function startOfToday(today = todayRangeKey()) {
    const date = new Date(`${today}T00:00:00`);
    if (Number.isNaN(date.getTime())) {
        const fallback = new Date();
        fallback.setHours(0, 0, 0, 0);
        return fallback;
    }
    return date;
}

export function derivePresetRange(
    preset,
    {
        today = todayRangeKey(),
        customStart = '',
        customEnd = '',
        customFallback = 'month',
        allStart = '1970-01-01',
        allEnd = '',
    } = {},
) {
    const base = startOfToday(today);

    if (preset === 'week') {
        const monday = new Date(base);
        monday.setDate(base.getDate() - ((base.getDay() + 6) % 7));
        return { start: isoDate(monday), end: today };
    }
    if (preset === 'month') {
        return {
            start: `${base.getFullYear()}-${String(base.getMonth() + 1).padStart(2, '0')}-01`,
            end: today,
        };
    }
    if (preset === 'quarter') {
        const firstMonth = Math.floor(base.getMonth() / 3) * 3;
        return {
            start: isoDate(new Date(base.getFullYear(), firstMonth, 1)),
            end: today,
        };
    }
    if (preset === 'year') {
        return {
            start: `${base.getFullYear()}-01-01`,
            end: today,
        };
    }
    if (preset === 'last_year') {
        const year = base.getFullYear() - 1;
        return {
            start: `${year}-01-01`,
            end: `${year}-12-31`,
        };
    }
    if (preset === 'custom') {
        if (customStart && customEnd) return { start: customStart, end: customEnd };
        if (!customFallback) return { start: '', end: '' };
        return derivePresetRange(customFallback, { today, allStart, allEnd, customFallback: '' });
    }
    if (preset === 'all') {
        return {
            start: allStart,
            end: allEnd || today,
        };
    }
    return {
        start: today,
        end: today,
    };
}

export async function fetchItemRangeBounds(
    apiClient,
    {
        type,
        sortField,
        startField = sortField,
        endField = startField,
        fallbackEnd = todayRangeKey(),
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
    const fallback = fallbackEnd || todayRangeKey();
    const earliest = earliestRes?.data?.items?.[0]?.[startField]?.slice?.(0, 10) || earliestRes?.data?.items?.[0]?.[startField] || fallback;
    const latestValue = latestRes?.data?.items?.[0]?.[endField]?.slice?.(0, 10)
        || latestRes?.data?.items?.[0]?.[endField]
        || latestRes?.data?.items?.[0]?.[startField]?.slice?.(0, 10)
        || latestRes?.data?.items?.[0]?.[startField]
        || fallback;
    const boundedEnd = minimumEnd && latestValue < minimumEnd ? minimumEnd : latestValue;
    return {
        start: earliest,
        end: boundedEnd,
    };
}
