import { api } from '../api.js';
import { showToast } from '../components/toast.js';
import {
    arrayValue as safeArray,
    errorMessage,
    finiteNumber,
    formatAmount,
    formatMoneyCompact,
    isRecord,
    isValidDateInput,
    noteCadenceSubtitle,
    pad2,
    records as safeRecords,
} from '../utils/format.js';
import { derivePresetRange, fetchItemRangeBounds, RANGE_PRESET_OPTIONS, todayRangeKey } from '../utils/date_ranges.js';
import { bindEnterAction, BREAKPOINTS, escapeHtml, injectStyles, mediaMax, pageShellCss } from '../utils/ui.js';

const CSS_ID = 'pendo-stats-waterfall-styles';
const RANGE_KEYS = new Set(RANGE_PRESET_OPTIONS.map((option) => option.key));
const TASK_TONES = Object.freeze({
    open: '#F59E0B',
    done: '#166534',
    cancelled: '#94a3b8',
    created: '#FCD34D',
});
const DEFAULT_MOOD_EMOJIS = {
    happy: '😊',
    sad: '😢',
    calm: '😌',
    excited: '🤩',
    angry: '😠',
    tired: '😴',
    anxious: '😰',
    grateful: '🙏',
    neutral: '😐',
};
const DEFAULT_MOOD_LABELS = {
    happy: '开心',
    calm: '平静',
    excited: '兴奋',
    sad: '难过',
    angry: '生气',
    tired: '疲惫',
    anxious: '焦虑',
    grateful: '感恩',
    neutral: '普通',
};

let _container = null;
let _range = 'month';
let _loading = false;
let _data = null;
let _customStart = '';
let _customEnd = '';
let _customDraftStart = '';
let _customDraftEnd = '';
let _heatmapData = null;
let _comparisonData = null;
let _moodEmojis = { ...DEFAULT_MOOD_EMOJIS };
let _moodLabels = { ...DEFAULT_MOOD_LABELS };
let _loadVersion = 0;
let _activeRequestSignature = '';
let _dataSignature = '';
let _chartSequence = 0;

// 通用数据边界：统计接口异常时不把 NaN、Infinity 或空记录带入图表。
function nonNegativeNumber(value, fallback = 0) {
    return Math.max(0, finiteNumber(value, fallback));
}

function normalizeTextMap(value, defaults) {
    if (!isRecord(value)) return { ...defaults };
    const entries = Object.entries(value)
        .filter(([, text]) => typeof text === 'string' && text.trim())
        .map(([key, text]) => [key, text.trim()]);
    return { ...defaults, ...Object.fromEntries(entries) };
}

function responseData(response) {
    return isRecord(response?.data) ? response.data : {};
}

function userTodayKey() {
    return todayRangeKey();
}

function formatCount(value) {
    return `${Math.round(nonNegativeNumber(value))}`;
}

function formatWordCompact(value) {
    const words = Math.round(nonNegativeNumber(value));
    if (words >= 10000) return `${(words / 10000).toFixed(1)}万字`;
    if (words >= 1000) return `${(words / 1000).toFixed(1)}k字`;
    return `${words}字`;
}

function formatPercent(value) {
    const ratio = Math.min(1, nonNegativeNumber(value));
    return `${Math.round(ratio * 100)}%`;
}

function sumBy(items, key) {
    return safeRecords(items).reduce((sum, item) => sum + finiteNumber(item[key]), 0);
}

// 时间范围与坐标采样集中在这里，避免各图表重复推导。
function rangeLabel() {
    if (_range === 'custom') return '当前范围';
    if (_range === 'all') return '全部时间';
    return RANGE_PRESET_OPTIONS.find((item) => item.key === _range)?.label || '当前范围';
}
function diaryRangeSentence() {
    return _range === 'custom' ? '这个范围内' : `${rangeLabel()}里`;
}
function compactAxisLabel(value) {
    const text = String(value ?? '');
    if (/^\d{4}-\d{2}-\d{2}$/.test(text)) return text.slice(5).replace('-', '/');
    if (/^\d{4}-W\d{2}$/.test(text)) return text.replace(/^\d{4}-/, '');
    if (/^\d{4}-\d{2}$/.test(text)) return text.slice(2).replace('-', '/');
    return text.length > 10 ? `${text.slice(0, 8)}…` : text;
}

function sortByNumericDesc(items, key) {
    return safeRecords(items)
        .slice()
        .sort((a, b) => finiteNumber(b[key]) - finiteNumber(a[key]));
}

function sampleIndexes(length, maxPoints = 18) {
    const size = Math.max(0, Math.floor(finiteNumber(length)));
    const limit = Math.max(1, Math.floor(finiteNumber(maxPoints, 18)));
    if (size <= limit) return Array.from({ length: size }, (_, index) => index);
    if (limit === 1) return [0];

    const step = (size - 1) / (limit - 1);
    const picked = new Set();
    for (let index = 0; index < limit; index += 1) {
        picked.add(Math.round(index * step));
    }
    return Array.from(picked).sort((a, b) => a - b);
}

function buildAxisTickLabels(labels, maxTicks = 6) {
    const all = safeArray(labels).map(compactAxisLabel);
    return sampleIndexes(all.length, maxTicks).map((index) => all[index]);
}

function buildRequestRangeValue(rangeKey = _range, range = deriveRangeDates()) {
    if (rangeKey === 'all') return 'all';
    return `${range.start}..${range.end}`;
}

function deriveRangeDates({
    rangeKey = _range,
    customStart = _customStart,
    customEnd = _customEnd,
    today = userTodayKey(),
} = {}) {
    return derivePresetRange(rangeKey, {
        today,
        customStart,
        customEnd,
        customFallback: 'month',
    });
}

function currentRangeRequest() {
    const rangeKey = RANGE_KEYS.has(_range) ? _range : 'month';
    const today = userTodayKey();
    const range = deriveRangeDates({
        rangeKey,
        customStart: _customStart,
        customEnd: _customEnd,
        today,
    });
    return {
        rangeKey,
        range,
        today,
        signature: `${rangeKey}|${range.start}|${range.end}`,
    };
}

function isCurrentLoad(container, version) {
    return _container === container && _loadVersion === version;
}

function overviewReferenceDay(range, today = userTodayKey()) {
    if (range?.start && range?.end && range.start <= today && today <= range.end) {
        return today;
    }
    return range?.end || today;
}

function resolveHeatmapYear(range = deriveRangeDates()) {
    const endDate = new Date(`${range.end}T00:00:00`);
    return Number.isNaN(endDate.getTime())
        ? Number(userTodayKey().slice(0, 4))
        : endDate.getFullYear();
}

function clampRangeToYear(range, year) {
    if (!year || !range?.start || !range?.end) return { start: '', end: '' };
    const yearStart = `${year}-01-01`;
    const yearEnd = `${year}-12-31`;
    const start = range.start > yearStart ? range.start : yearStart;
    const end = range.end < yearEnd ? range.end : yearEnd;
    return start <= end ? { start, end } : { start: '', end: '' };
}

function diffDays(start, end) {
    if (!isValidDateInput(start) || !isValidDateInput(end)) return 0;
    const startTime = Date.parse(`${start}T00:00:00Z`);
    const endTime = Date.parse(`${end}T00:00:00Z`);
    const diff = endTime - startTime;
    return Math.max(0, Math.round(diff / 86400000));
}

function ledgerRhythmSeries(ledger, range = deriveRangeDates()) {
    const spanDays = diffDays(range.start, range.end);
    const useMonthly = _range === 'year' || _range === 'last_year' || spanDays > 62;
    const source =
        useMonthly && safeRecords(ledger?.monthly).length ? safeRecords(ledger.monthly) : safeRecords(ledger?.daily);
    const labelKey = useMonthly ? 'month' : 'date';
    return source.map((item) => ({
        label: compactAxisLabel(String(item[labelKey] || '')),
        total: nonNegativeNumber(item.expense) + nonNegativeNumber(item.income),
    }));
}

function ledgerTrendSeries(ledger, range = deriveRangeDates()) {
    const spanDays = diffDays(range.start, range.end);
    const useMonthly = _range === 'year' || _range === 'last_year' || spanDays > 62;
    const source =
        useMonthly && safeRecords(ledger?.monthly).length ? safeRecords(ledger.monthly) : safeRecords(ledger?.daily);
    const labelKey = useMonthly ? 'month' : 'date';
    return source.map((item) => ({
        label: String(item[labelKey] || ''),
        expense: nonNegativeNumber(item.expense),
        income: nonNegativeNumber(item.income),
    }));
}

function moodEmoji(mood) {
    const normalized = String(mood || '')
        .trim()
        .toLowerCase();
    if (!normalized || !Object.hasOwn(_moodEmojis, normalized)) return '';
    return typeof _moodEmojis[normalized] === 'string' ? _moodEmojis[normalized] : '';
}

function formatMoodLabel(mood) {
    const normalized = String(mood || '').trim();
    if (!normalized) return '未记录';
    const emoji = moodEmoji(normalized);
    const moodKey = normalized.toLowerCase();
    const configuredLabel = Object.hasOwn(_moodLabels, moodKey) ? _moodLabels[moodKey] : '';
    const label = typeof configuredLabel === 'string' && configuredLabel ? configuredLabel : normalized;
    return emoji ? `${emoji} ${label}` : label;
}

function diaryCadenceSubtitle(granularity) {
    if (granularity === 'year') return `${diaryRangeSentence()}每年写了多少字。`;
    if (granularity === 'month') return `${diaryRangeSentence()}每月写了多少字。`;
    if (granularity === 'week') return `${diaryRangeSentence()}每周写了多少字。`;
    return `${diaryRangeSentence()}每天写了多少字。`;
}

// 轻量图表原语全部生成原生 HTML/SVG，不依赖外部图表运行时。
function sparklinePath(values, width = 440, height = 168, padding = 18) {
    const normalized = safeArray(values).map(nonNegativeNumber);
    if (!normalized.length) return { line: '', area: '', points: [] };
    const max = Math.max(...normalized, 1);
    const step = normalized.length > 1 ? (width - padding * 2) / (normalized.length - 1) : 0;
    const points = normalized.map((value, index) => {
        const x = padding + step * index;
        const y = height - padding - (value / max) * (height - padding * 2);
        return [x, y];
    });
    const line = points.map(([x, y], index) => `${index === 0 ? 'M' : 'L'}${x},${y}`).join(' ');
    const area = `${line} L ${padding + step * (normalized.length - 1)},${height - padding} L ${padding},${height - padding} Z`;
    return { line, area, points };
}

function compressSeries(labels, values, maxPoints = 26) {
    const safeLabels = safeArray(labels);
    const safeValues = safeArray(values).map(nonNegativeNumber);
    const limit = Math.max(1, Math.floor(finiteNumber(maxPoints, 26)));
    if (safeValues.length <= limit) return { labels: safeLabels, values: safeValues };
    if (limit === 1) return { labels: [safeLabels[0]], values: [safeValues[0]] };
    if (limit === 2) {
        return {
            labels: [safeLabels[0], safeLabels.at(-1)],
            values: [safeValues[0], safeValues.at(-1)],
        };
    }
    const innerSlots = limit - 2;
    const bucketSize = (safeValues.length - 2) / innerSlots;
    const picked = [0];
    for (let bucket = 0; bucket < innerSlots; bucket += 1) {
        const start = 1 + Math.floor(bucket * bucketSize);
        const end = Math.min(safeValues.length - 1, 1 + Math.floor((bucket + 1) * bucketSize));
        let bestIndex = Math.min(start, safeValues.length - 2);
        let bestValue = -Infinity;
        for (let index = start; index < Math.max(start + 1, end); index += 1) {
            const score = Math.abs(safeValues[index]);
            if (score > bestValue) {
                bestValue = score;
                bestIndex = index;
            }
        }
        picked.push(bestIndex);
    }
    picked.push(safeValues.length - 1);
    const unique = Array.from(new Set(picked)).sort((a, b) => a - b);
    return {
        labels: unique.map((index) => safeLabels[index]),
        values: unique.map((index) => safeValues[index]),
    };
}

function renderSparkline(labels, values, color, formatter) {
    const compressed = compressSeries(labels, values, 28);
    if (!compressed.values.length) return '<div class="stats-empty-card">暂无数据</div>';
    const gradId = `stats-grad-${color.replace(/[^a-zA-Z0-9]/g, '')}-${++_chartSequence}`;
    const { line, area, points } = sparklinePath(compressed.values);
    const max = Math.max(...compressed.values, 1);
    const footerLabels = buildAxisTickLabels(compressed.labels, 6);
    const visiblePointIndexes = sampleIndexes(compressed.values.length, 14);
    return `
        <div class="stats-chart-block">
            <svg viewBox="0 0 440 168" class="stats-sparkline" role="img" aria-label="趋势图">
                <defs>
                    <linearGradient id="${gradId}" x1="0" y1="0" x2="0" y2="1">
                        <stop offset="0%" stop-color="${color}" stop-opacity="0.28"></stop>
                        <stop offset="100%" stop-color="${color}" stop-opacity="0.03"></stop>
                    </linearGradient>
                </defs>
                <path d="${area}" fill="url(#${gradId})"></path>
                <path d="${line}" fill="none" stroke="${color}" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round"></path>
                ${points
                    .map(([x, y], index) =>
                        visiblePointIndexes.includes(index)
                            ? `
                    <g>
                        <circle cx="${x}" cy="${y}" r="4" fill="${color}"></circle>
                        <title>${escapeHtml(compressed.labels[index] || '')} · ${escapeHtml(formatter(compressed.values[index]))}</title>
                    </g>
                `
                            : '',
                    )
                    .join('')}
                ${[0.25, 0.5, 0.75]
                    .map((ratio) => {
                        const y = 168 - 18 - ratio * (168 - 36);
                        return `<line x1="18" y1="${y}" x2="422" y2="${y}" stroke="rgba(148,163,184,0.18)" stroke-dasharray="4 7"></line>`;
                    })
                    .join('')}
                <text x="422" y="18" text-anchor="end" fill="#94a3b8" font-size="11">${escapeHtml(formatter(max))}</text>
            </svg>
            <div class="stats-sparkline-footer">
                ${footerLabels.map((label) => `<span title="${escapeHtml(label)}">${escapeHtml(label)}</span>`).join('')}
            </div>
        </div>
    `;
}

function renderColumnChart(items, valueKey, labelKey, color, formatter) {
    const rows = safeRecords(items);
    if (!rows.length) return '<div class="stats-empty-card">暂无数据</div>';
    const max = Math.max(...rows.map((item) => nonNegativeNumber(item[valueKey])), 1);
    return `
        <div class="stats-column-chart">
            ${rows
                .map((item) => {
                    const value = nonNegativeNumber(item[valueKey]);
                    const height = Math.max(8, Math.round((value / max) * 100));
                    return `
                    <div class="stats-column-item" title="${escapeHtml(item[labelKey])} · ${escapeHtml(formatter(value))}">
                        <div class="stats-column-stage">
                            <span class="stats-column-bar" style="height:${height}%; background:${color};"></span>
                        </div>
                        <span class="stats-column-value">${escapeHtml(formatter(value))}</span>
                        <span class="stats-column-label">${escapeHtml(compactAxisLabel(item[labelKey]))}</span>
                    </div>
                `;
                })
                .join('')}
        </div>
    `;
}

function renderBarRows(items, valueKey, labelKey, color, formatter) {
    const rows = safeRecords(items);
    if (!rows.length) return '<div class="stats-empty-card">暂无数据</div>';
    const max = Math.max(...rows.map((item) => nonNegativeNumber(item[valueKey])), 1);
    return `
        <div class="stats-bar-rows">
            ${rows
                .map((item) => {
                    const value = nonNegativeNumber(item[valueKey]);
                    return `
                    <div>
                        <div class="stats-bar-top">
                            <span class="stats-bar-label">${escapeHtml(item[labelKey])}</span>
                            <span class="stats-bar-value">${escapeHtml(formatter(value))}</span>
                        </div>
                        <div class="stats-bar-track">
                            <span class="stats-bar-fill" style="width:${Math.max(8, Math.round((value / max) * 100))}%; background:${color};"></span>
                        </div>
                    </div>
                `;
                })
                .join('')}
        </div>
    `;
}

function renderTreemap(items, valueKey, labelKey, colors, formatter) {
    const sorted = safeRecords(items)
        .map((item) => ({ ...item, __value: nonNegativeNumber(item[valueKey]) }))
        .filter((item) => item.__value > 0)
        .sort((a, b) => b.__value - a.__value)
        .slice(0, 8);
    if (!sorted.length) return '<div class="stats-empty-card">暂无数据</div>';
    const max = Math.max(...sorted.map((item) => item.__value), 1);
    return `
        <div class="stats-treemap">
            ${sorted
                .map((item, index) => {
                    const ratio = item.__value / max;
                    const span = ratio > 0.72 ? 2 : 1;
                    const shade = (0.14 + ratio * 0.2).toFixed(2);
                    return `
                    <div class="stats-treemap-tile" style="grid-column: span ${span}; background: linear-gradient(180deg, rgba(255,255,255,0.92), rgba(255,255,255,0.7)), rgba(255,255,255,0.88); border-color: rgba(255,255,255,0.55); box-shadow: inset 0 0 0 1px rgba(255,255,255,0.28);">
                        <span class="stats-treemap-glow" style="background:${colors[index % colors.length]}; opacity:${shade};"></span>
                        <div class="stats-treemap-label">${escapeHtml(item[labelKey])}</div>
                        <div class="stats-treemap-value">${escapeHtml(formatter(item.__value))}</div>
                    </div>
                `;
                })
                .join('')}
        </div>
    `;
}

function renderStackedColumns(items, labelKey, segments, formatter) {
    const rows = safeRecords(items);
    const safeSegments = safeRecords(segments);
    if (!rows.length || !safeSegments.length) return '<div class="stats-empty-card">暂无数据</div>';
    const normalized = rows.map((item) => {
        const total = safeSegments.reduce((sum, segment) => sum + nonNegativeNumber(item[segment.key]), 0);
        return { ...item, __total: total };
    });
    const max = Math.max(...normalized.map((item) => item.__total), 1);
    return `
        <div class="stats-stacked-columns">
            ${normalized
                .map((item) => {
                    const height = Math.max(12, Math.round((item.__total / max) * 100));
                    return `
                    <div class="stats-stacked-item" title="${escapeHtml(item[labelKey])} · ${escapeHtml(formatter(item.__total))}">
                        <div class="stats-stacked-stage">
                            <div class="stats-stacked-track" style="height:${height}%;">
                                ${safeSegments
                                    .map((segment) => {
                                        const value = nonNegativeNumber(item[segment.key]);
                                        const segmentHeight = item.__total ? (value / item.__total) * 100 : 0;
                                        return `<span class="stats-stacked-segment" style="height:${segmentHeight}%; background:${segment.color};"></span>`;
                                    })
                                    .join('')}
                            </div>
                        </div>
                        <span class="stats-stacked-total">${escapeHtml(formatter(item.__total))}</span>
                        <span class="stats-stacked-label">${escapeHtml(compactAxisLabel(item[labelKey]))}</span>
                    </div>
                `;
                })
                .join('')}
        </div>
    `;
}

function renderHistogram(items, valueKey, labelKey, color, formatter) {
    const rows = safeRecords(items);
    if (!rows.length) return '<div class="stats-empty-card">暂无数据</div>';
    const max = Math.max(...rows.map((item) => nonNegativeNumber(item[valueKey])), 1);
    return `
        <div class="stats-histogram">
            ${rows
                .map((item) => {
                    const value = nonNegativeNumber(item[valueKey]);
                    const height = Math.max(value ? 14 : 6, Math.round((value / max) * 100));
                    return `
                    <div class="stats-histogram-bin" title="${escapeHtml(item[labelKey])} · ${escapeHtml(formatter(value))}">
                        <div class="stats-histogram-stage">
                            <span class="stats-histogram-bar" style="height:${height}%; background:${color};"></span>
                        </div>
                        <span class="stats-histogram-value">${escapeHtml(formatter(value))}</span>
                        <span class="stats-histogram-label">${escapeHtml(compactAxisLabel(item[labelKey]))}</span>
                    </div>
                `;
                })
                .join('')}
        </div>
    `;
}

// 环图路径和二维热力图共享同一组安全数值边界。
function polarToCartesian(cx, cy, r, angle) {
    const rad = ((angle - 90) * Math.PI) / 180;
    return { x: cx + r * Math.cos(rad), y: cy + r * Math.sin(rad) };
}

function arcPath(cx, cy, outer, inner, startAngle, endAngle) {
    const startOuter = polarToCartesian(cx, cy, outer, endAngle);
    const endOuter = polarToCartesian(cx, cy, outer, startAngle);
    const startInner = polarToCartesian(cx, cy, inner, endAngle);
    const endInner = polarToCartesian(cx, cy, inner, startAngle);
    const largeArc = endAngle - startAngle > 180 ? 1 : 0;
    return [
        `M ${startOuter.x} ${startOuter.y}`,
        `A ${outer} ${outer} 0 ${largeArc} 0 ${endOuter.x} ${endOuter.y}`,
        `L ${endInner.x} ${endInner.y}`,
        `A ${inner} ${inner} 0 ${largeArc} 1 ${startInner.x} ${startInner.y}`,
        'Z',
    ].join(' ');
}

function fullRingPath(cx, cy, outer, inner) {
    return [
        `M ${cx} ${cy - outer}`,
        `A ${outer} ${outer} 0 1 1 ${cx} ${cy + outer}`,
        `A ${outer} ${outer} 0 1 1 ${cx} ${cy - outer}`,
        `M ${cx} ${cy - inner}`,
        `A ${inner} ${inner} 0 1 0 ${cx} ${cy + inner}`,
        `A ${inner} ${inner} 0 1 0 ${cx} ${cy - inner}`,
        'Z',
    ].join(' ');
}

function renderMatrixHeatmap(rows, xLabels, yLabels, color) {
    const records = safeRecords(rows);
    if (!records.length) return '<div class="stats-empty-card">暂无数据</div>';
    const matrix = new Map();
    records.forEach((row) => {
        matrix.set(`${row.weekday}|${row.slot}`, nonNegativeNumber(row.count));
    });
    const max = Math.max(...records.map((row) => nonNegativeNumber(row.count)), 1);
    return `
        <div class="stats-matrix">
            <div class="stats-matrix-corner"></div>
            ${xLabels.map((label) => `<div class="stats-matrix-xlabel">${escapeHtml(label)}</div>`).join('')}
            ${yLabels
                .map(
                    (weekday) => `
                <div class="stats-matrix-ylabel">${escapeHtml(weekday)}</div>
                ${xLabels
                    .map((slot) => {
                        const value = nonNegativeNumber(matrix.get(`${weekday}|${slot}`));
                        const opacity = value ? 0.12 + (value / max) * 0.88 : 0.06;
                        return `
                        <div class="stats-matrix-cell" title="${escapeHtml(weekday)} · ${escapeHtml(slot)} · ${value} 个"
                            style="background: rgba(${color}, ${opacity});">
                            <span>${value || ''}</span>
                        </div>
                    `;
                    })
                    .join('')}
            `,
                )
                .join('')}
        </div>
    `;
}

function donutCenterValueClass(value) {
    const length = String(value || '').length;
    if (length >= 11) return 'stats-donut-center-value tiny';
    if (length >= 9) return 'stats-donut-center-value small';
    return 'stats-donut-center-value';
}

function renderDonut(items, valueKey, labelKey, colors, centerValue, centerLabel, formatter) {
    const normalized = safeRecords(items)
        .map((item) => ({ ...item, value: nonNegativeNumber(item[valueKey]) }))
        .filter((item) => item.value > 0);
    const total = normalized.reduce((sum, item) => sum + item.value, 0);
    if (!total) return '<div class="stats-empty-card">暂无数据</div>';
    const providedColors = safeArray(colors).filter((color) => typeof color === 'string' && color);
    const palette = providedColors.length ? providedColors : ['#94a3b8'];
    let cursor = 0;
    const outer = 67;
    const inner = 43;
    return `
        <div class="stats-donut-wrap">
            <svg viewBox="0 0 180 180" class="stats-donut">
                <circle cx="90" cy="90" r="${outer}" fill="rgba(248,250,252,0.96)"></circle>
                ${normalized
                    .map((item, index) => {
                        const angle = (item.value / total) * 360;
                        const path =
                            angle >= 359.999
                                ? fullRingPath(90, 90, outer, inner)
                                : arcPath(90, 90, outer, inner, cursor, cursor + angle);
                        const color = palette[index % palette.length];
                        cursor += angle;
                        return `<path d="${path}" fill="${color}" fill-rule="evenodd" stroke="#ffffff" stroke-width="1.5"><title>${escapeHtml(item[labelKey])} · ${escapeHtml(formatter(item.value))}</title></path>`;
                    })
                    .join('')}
                <circle cx="90" cy="90" r="${inner - 1}" fill="#fff"></circle>
                <text x="90" y="86" text-anchor="middle" class="${donutCenterValueClass(centerValue)}">${escapeHtml(centerValue)}</text>
                <text x="90" y="106" text-anchor="middle" class="stats-donut-center-label">${escapeHtml(centerLabel)}</text>
            </svg>
            <div class="stats-donut-legend">
                ${normalized
                    .map(
                        (item, index) => `
                    <div class="stats-donut-legend-item">
                        <span class="stats-donut-legend-dot" style="background:${palette[index % palette.length]};"></span>
                        <span class="stats-donut-legend-name">${escapeHtml(item[labelKey])}</span>
                        <span class="stats-donut-legend-value">${escapeHtml(formatter(item.value))}</span>
                    </div>
                `,
                    )
                    .join('')}
            </div>
        </div>
    `;
}

function renderHeatStrip(items, valueKey, labelKey, color, formatter = (value) => `${value}`, options = {}) {
    const rows = safeRecords(items);
    if (!rows.length) return '<div class="stats-empty-card">暂无数据</div>';
    const max = Math.max(...rows.map((item) => nonNegativeNumber(item[valueKey])), 1);
    const columns = Math.min(31, Math.max(0, Math.floor(finiteNumber(options?.columns))));
    const style = columns > 0 ? ` style="grid-template-columns:repeat(${columns}, minmax(0, 1fr));"` : '';
    return `
        <div class="stats-heat-strip"${style}>
            ${rows
                .map((item) => {
                    const value = nonNegativeNumber(item[valueKey]);
                    const opacity = value ? 0.16 + (value / max) * 0.84 : 0.08;
                    return `
                    <div class="stats-heat-cell" title="${escapeHtml(item[labelKey])} · ${escapeHtml(formatter(value))}"
                        style="background:rgba(${color}, ${opacity});">
                        <span>${escapeHtml(item[labelKey])}</span>
                    </div>
                `;
                })
                .join('')}
        </div>
    `;
}

function renderTokenCloud(items, formatter) {
    const tokens = safeRecords(items);
    if (!tokens.length) return '<div class="stats-empty-card">暂无数据</div>';
    return `
        <div class="stats-token-cloud">
            ${tokens
                .map(
                    (item, index) => `
                <span class="stats-token stats-token-${(index % 4) + 1}">
                    ${escapeHtml(item.label)} · ${escapeHtml(formatter(nonNegativeNumber(item.value)))}
                </span>
            `,
                )
                .join('')}
        </div>
    `;
}

function renderActivityHeatmap(days, highlightStart, highlightEnd) {
    const normalizedDays = safeRecords(days)
        .filter((day) => isValidDateInput(String(day.date || '')))
        .map((day) => ({
            date: String(day.date),
            count: Math.round(nonNegativeNumber(day.count)),
            ledger: Math.round(nonNegativeNumber(day.ledger)),
            task: Math.round(nonNegativeNumber(day.task)),
            event: Math.round(nonNegativeNumber(day.event)),
            note: Math.round(nonNegativeNumber(day.note)),
            diary: Math.round(nonNegativeNumber(day.diary)),
        }))
        .sort((a, b) => a.date.localeCompare(b.date));
    if (!normalizedDays.length) return '<div class="stats-empty-card">暂无数据</div>';
    const weekdayLabels = ['一', '二', '三', '四', '五', '六', '日'];
    const firstDay = new Date(`${normalizedDays[0].date}T00:00:00`);
    const maxCount = Math.max(...normalizedDays.map((day) => day.count), 1);
    const weeks = [];
    let week = [];
    const startWeekday = (firstDay.getDay() + 6) % 7;
    for (let i = 0; i < startWeekday; i += 1) {
        week.push(null);
    }
    normalizedDays.forEach((d) => {
        const dt = new Date(`${d.date}T00:00:00`);
        const wd = (dt.getDay() + 6) % 7;
        week.push(d);
        if (wd === 6 || d === normalizedDays[normalizedDays.length - 1]) {
            while (week.length < 7) week.push(null);
            weeks.push(week);
            week = [];
        }
    });
    if (week.length > 0) {
        while (week.length < 7) week.push(null);
        weeks.push(week);
    }
    const monthLabels = [];
    let lastMonth = -1;
    weeks.forEach((w, wi) => {
        const firstValid = w.find((c) => c !== null);
        if (firstValid) {
            const m = new Date(`${firstValid.date}T00:00:00`).getMonth();
            if (m !== lastMonth) {
                monthLabels.push({ index: wi, label: `${m + 1}月` });
                lastMonth = m;
            }
        }
    });
    const isHighlighted = (dateStr) => {
        if (!highlightStart || !highlightEnd || !dateStr) return false;
        return dateStr >= highlightStart && dateStr <= highlightEnd;
    };
    const cellOpacity = (count) => {
        if (!count) return 0;
        return 0.24 + (count / maxCount) * 0.76;
    };
    const minWidth = 28 + weeks.length * 12 + (weeks.length - 1) * 2;
    return `
        <div class="stats-heatmap" style="--stats-heatmap-weeks:${weeks.length}; --stats-heatmap-min-width:${minWidth}px;">
            <div class="stats-heatmap-months">
                <div class="stats-heatmap-corner"></div>
                ${weeks
                    .map((_, wi) => {
                        const ml = monthLabels.find((m) => m.index === wi);
                        return `<div class="stats-heatmap-month-label">${ml ? escapeHtml(ml.label) : ''}</div>`;
                    })
                    .join('')}
            </div>
            <div class="stats-heatmap-grid">
                ${weekdayLabels
                    .map(
                        (label, yi) => `
                    <div class="stats-heatmap-ylabel">${escapeHtml(label)}</div>
                    ${weeks
                        .map((w) => {
                            const cell = w[yi];
                            if (!cell) return `<div class="stats-heatmap-cell empty"></div>`;
                            const highlighted = isHighlighted(cell.date);
                            const op = cellOpacity(cell.count);
                            const background = cell.count ? `rgba(16,185,129,${op})` : 'rgba(226,232,240,0.52)';
                            return `
                            <div class="stats-heatmap-cell${highlighted ? ' in-range' : ''}"
                                style="background:${background};"
                                title="${escapeHtml(cell.date)} · 活动 ${cell.count}（记账${cell.ledger} 任务${cell.task} 日程${cell.event} 笔记${cell.note} 日记${cell.diary}）">
                            </div>
                        `;
                        })
                        .join('')}
                `,
                    )
                    .join('')}
            </div>
            <div class="stats-heatmap-footer">
                <span class="stats-heatmap-legend-label">少</span>
                <div class="stats-heatmap-legend-cell" style="background:rgba(226,232,240,0.52)"></div>
                ${[0.24, 0.4, 0.56, 0.72, 0.88, 1.0]
                    .map(
                        (op) =>
                            `<div class="stats-heatmap-legend-cell" style="background:rgba(16,185,129,${op})"></div>`,
                    )
                    .join('')}
                <span class="stats-heatmap-legend-label">多</span>
            </div>
        </div>
    `;
}

function renderComparisonMetric(label, value, maxExpense, color) {
    const amount = nonNegativeNumber(value);
    const maximum = Math.max(1, nonNegativeNumber(maxExpense, 1));
    const width = amount > 0 ? Math.min(100, Math.max(6, Math.round((amount / maximum) * 100))) : 0;
    return `
        <div class="stats-comparison-metric" title="${escapeHtml(label)} ${formatAmount(amount)}">
            <div class="stats-comparison-metric-head">
                <span class="stats-comparison-metric-name">${escapeHtml(label)}</span>
                <strong>${formatMoneyCompact(amount)}</strong>
            </div>
            <div class="stats-comparison-track">
                <span class="stats-comparison-fill" style="width:${width}%; background:${color};"></span>
            </div>
        </div>
    `;
}

function renderComparisonBars(months) {
    const recent = safeRecords(months).slice(-6);
    if (!recent.length) return '<div class="stats-empty-card">暂无数据</div>';
    const maxExpense = Math.max(
        ...recent.map((m) =>
            Math.max(nonNegativeNumber(m.expense), nonNegativeNumber(m.prev_expense), nonNegativeNumber(m.yoy_expense)),
        ),
        1,
    );
    return `
        <div class="stats-comparison">
            <div class="stats-chip-row">
                <span class="stats-chip" style="border-left:3px solid #ef4444;">当月</span>
                <span class="stats-chip" style="border-left:3px solid #f97316;">上月</span>
                <span class="stats-chip" style="border-left:3px solid #94a3b8;">去年同月</span>
            </div>
            <div class="stats-comparison-table">
                ${recent
                    .map(
                        (m) => `
                    <div class="stats-comparison-row">
                        <div class="stats-comparison-label">${escapeHtml(compactAxisLabel(m.month))}</div>
                        <div class="stats-comparison-series">
                            ${renderComparisonMetric('当月', m.expense, maxExpense, '#ef4444')}
                            ${renderComparisonMetric('上月', m.prev_expense, maxExpense, '#f97316')}
                            ${renderComparisonMetric('去年同月', m.yoy_expense, maxExpense, '#94a3b8')}
                        </div>
                    </div>
                `,
                    )
                    .join('')}
            </div>
        </div>
    `;
}

// 洞察文本只拼接页面计算结果；来自接口的标签必须先转义。
function generateInsights(data) {
    const insights = [];
    const ledger = data?.ledger || {};
    const tasks = data?.tasks || {};
    const events = data?.events || {};
    const diary = data?.diary || {};
    const expenseByCategory = sortByNumericDesc(ledger.expense_by_category, 'total');
    const incomeByCategory = sortByNumericDesc(ledger.income_by_category, 'total');
    const expenseTotal = sumBy(expenseByCategory, 'total');
    const incomeTotal = sumBy(incomeByCategory, 'total');
    const totals = normalizeTaskTotals(tasks.totals);
    const totalTasks = totals.total;

    if (expenseTotal > 0) {
        const top = expenseByCategory[0];
        insights.push({
            icon: '💰',
            color: '#fef2f2',
            text: `${rangeLabel()}支出 <strong>${formatAmount(expenseTotal)}</strong>，收入 <strong>${formatAmount(incomeTotal)}</strong>，结余 <strong>${formatAmount(incomeTotal - expenseTotal)}</strong>`,
        });
        if (top) {
            const pct = expenseTotal > 0 ? Math.round((nonNegativeNumber(top.total) / expenseTotal) * 100) : 0;
            insights.push({
                icon: '🏷️',
                color: '#fff7ed',
                text: `最大支出分类：<strong>${escapeHtml(top.category || '未分类')}</strong>，占比 ${pct}%（${formatAmount(top.total)}）`,
            });
        }
    }

    if (totalTasks > 0) {
        const doneCount = totals.done;
        const completionBase = totals.open + totals.done;
        const rate = Math.round(totals.completionRate * 100);
        const active = totals.open;
        const cancelled = totals.cancelled;
        insights.push({
            icon: '✅',
            color: '#f0fdf4',
            text:
                completionBase > 0
                    ? `任务完成率 <strong>${rate}%</strong>（${doneCount}/${completionBase}），未完成 <strong>${active}</strong> 项${cancelled ? `，已取消 <strong>${cancelled}</strong> 项` : ''}`
                    : `当前范围内暂无可完成任务${cancelled ? `，但有 <strong>${cancelled}</strong> 项已取消` : ''}。`,
        });
    }

    const timeSlots = safeRecords(events.time_slots);
    if (timeSlots.length > 0) {
        const peak = safeRecords(timeSlots).sort((a, b) => nonNegativeNumber(b.count) - nonNegativeNumber(a.count))[0];
        const peakCount = nonNegativeNumber(peak?.count);
        if (peak && peakCount > 0) {
            insights.push({
                icon: '📅',
                color: '#eef2ff',
                text: `最活跃时段：<strong>${escapeHtml(peak.slot || '')}</strong>，共 ${formatCount(peakCount)} 个日程`,
            });
        }
    }

    const diarySummary = diary.summary || {};
    const streak = diarySummary.current_streak || 0;
    if (_range === 'month' && streak > 0) {
        insights.push({
            icon: '📔',
            color: '#fdf2f8',
            text: `日记连续书写 <strong>${streak}</strong> 天，累计 <strong>${formatCount(diarySummary.total_words || 0)}</strong> 字`,
        });
    }

    if (insights.length === 0) {
        insights.push({
            icon: '📊',
            color: '#f8fafc',
            text: '当前范围内暂无足够数据生成洞察，试试切换到更大的时间范围。',
        });
    }

    return insights;
}

function renderInsightCard(data) {
    const insights = generateInsights(data);
    return renderCard({
        accent: '#4f46e5',
        eyebrow: 'Insights',
        title: '关键洞察',
        subtitle: `基于当前统计结果生成的摘要，默认按${rangeLabel()}解读。`,
        body: `
            <div class="stats-insights">
                ${insights
                    .map(
                        (item) => `
                    <div class="stats-insight-item">
                        <div class="stats-insight-icon" style="background:${item.color};">${item.icon}</div>
                        <div class="stats-insight-text">${item.text}</div>
                    </div>
                `,
                    )
                    .join('')}
            </div>
        `,
    });
}

function normalizeTaskTotals(totals = {}) {
    const source = isRecord(totals) ? totals : {};
    const open = Math.round(nonNegativeNumber(source.open));
    const done = Math.round(nonNegativeNumber(source.done));
    const cancelled = Math.round(nonNegativeNumber(source.cancelled));
    const total = open + done + cancelled;
    const completionBase = open + done;
    return {
        open,
        done,
        cancelled,
        total,
        completionRate: completionBase ? done / completionBase : 0,
    };
}

// 顶部大图、比较卡与通用卡片骨架。
function renderFeaturedDeck() {
    const range = deriveRangeDates();
    const heatmapYear = Math.round(nonNegativeNumber(_heatmapData?.year));
    const heatmapRange = clampRangeToYear(range, heatmapYear);
    return `
        <section class="stats-featured">
            ${renderCard({
                accent: '#10b981',
                eyebrow: 'Overview',
                title: '全年活动',
                subtitle: `${heatmapYear || ''}年全年活跃度概览，当前时间范围仅用于高亮，不改变底图统计。`,
                classes: 'stats-card--full',
                body: renderActivityHeatmap(
                    safeRecords(_heatmapData?.days),
                    heatmapRange.start || undefined,
                    heatmapRange.end || undefined,
                ),
            })}
        </section>
    `;
}

function renderComparisonCard() {
    if (!_comparisonData) return '';
    const months = safeRecords(_comparisonData.months);
    if (!months.length) return '';
    return renderCard({
        accent: '#f97316',
        eyebrow: 'Finance',
        title: '同比 · 环比',
        subtitle: '固定展示近 6 个月支出对比，不随当前时间范围切换。',
        body: renderComparisonBars(months),
        footer: (() => {
            const latest = months.at(-1);
            if (!latest) return '';
            const expense = nonNegativeNumber(latest.expense);
            const previousExpense = nonNegativeNumber(latest.prev_expense);
            const yearAgoExpense = nonNegativeNumber(latest.yoy_expense);
            const momVal = previousExpense ? ((expense - previousExpense) / previousExpense) * 100 : null;
            const yoyVal = yearAgoExpense ? ((expense - yearAgoExpense) / yearAgoExpense) * 100 : null;
            const momText = momVal !== null ? `${momVal >= 0 ? '+' : ''}${momVal.toFixed(1)}%` : '-';
            const yoyText = yoyVal !== null ? `${yoyVal >= 0 ? '+' : ''}${yoyVal.toFixed(1)}%` : '-';
            return renderMetricPairs([
                { label: '环比变化', value: momText },
                { label: '同比变化', value: yoyText },
            ]);
        })(),
    });
}

function renderMetricPairs(items) {
    const pairs = safeRecords(items);
    if (!pairs.length) return '';
    return `
        <div class="stats-metric-pairs">
            ${pairs
                .map(
                    (item) => `
                <div class="stats-metric-pair">
                    <span>${escapeHtml(item.label)}</span>
                    <strong>${escapeHtml(item.value)}</strong>
                </div>
            `,
                )
                .join('')}
        </div>
    `;
}

function renderCard({
    accent = '#4f46e5',
    eyebrow = '',
    title = '',
    subtitle = '',
    body = '',
    footer = '',
    classes = '',
}) {
    return `
        <article class="stats-card ${classes}" style="--stats-accent:${accent};">
            <div class="stats-card-head">
                <div>
                    ${eyebrow ? `<div class="stats-card-eyebrow">${escapeHtml(eyebrow)}</div>` : ''}
                    <h3>${escapeHtml(title)}</h3>
                    ${subtitle ? `<p>${escapeHtml(subtitle)}</p>` : ''}
                </div>
            </div>
            <div class="stats-card-body">${body}</div>
            ${footer ? `<div class="stats-card-footer">${footer}</div>` : ''}
        </article>
    `;
}

function ensureStyles() {
    injectStyles(
        CSS_ID,
        `
        ${pageShellCss('stats-shell', { compactPadding: '20px 16px 30px', compactBreakpoint: BREAKPOINTS.MOBILE })}

        /* 页面头部与范围选择 */
        .stats-stack { display: flex; flex-direction: column; gap: 18px; }
        .stats-hero {
            display: grid; grid-template-columns: minmax(0, 1fr) auto; gap: 18px; align-items: start;
            padding: 24px 26px; border-radius: 30px;
            background:
                radial-gradient(circle at top right, rgba(99,102,241,0.18), transparent 30%),
                radial-gradient(circle at bottom left, rgba(59,130,246,0.10), transparent 24%),
                linear-gradient(145deg, rgba(255,255,255,0.98), rgba(241,245,255,0.95));
            border: 1px solid rgba(99,102,241,0.14);
            box-shadow: 0 18px 42px rgba(79,70,229,0.06);
        }
        .stats-hero h2 { margin: 0; font-size: 32px; font-weight: 820; letter-spacing: -0.03em; color: #4338ca; }
        .stats-hero p { margin: 8px 0 0; font-size: 14px; line-height: 1.75; color: var(--color-text-secondary); }
        .stats-hero-tags, .stats-chip-row { display: flex; flex-wrap: wrap; gap: 8px; }
        .stats-hero-tags { margin-top: 14px; }
        .stats-range-group { display: flex; flex-direction: column; align-items: flex-end; gap: 10px; align-self: start; margin-top: 8px; }
        .stats-chip, .stats-range-btn, .stats-date-field {
            display: inline-flex; align-items: center; gap: 6px; height: 36px; padding: 0 14px; border-radius: 999px;
            border: 1px solid rgba(203,213,225,0.92); background: rgba(255,255,255,0.92); color: #475569; font-size: 12px; font-weight: 700;
        }
        .stats-range-btn { cursor: pointer; }
        .stats-range-btn.active { background: #4f46e5; border-color: #4f46e5; color: #fff; box-shadow: 0 10px 24px rgba(79,70,229,0.18); }
        .stats-range-btn:focus-visible, .stats-date-field:focus-visible {
            outline: 3px solid rgba(79,70,229,0.25); outline-offset: 2px; border-color: #6366f1;
        }
        .stats-custom-range {
            display: inline-flex;
            align-items: center;
            flex-wrap: wrap;
            gap: 8px;
            justify-content: flex-end;
        }
        .stats-date-field { font: inherit; min-width: 148px; }
        .stats-custom-range .stats-date-field {
            min-width: 0;
            width: 140px;
            padding: 0 12px;
        }
        .stats-range-sep {
            font-size: 12px;
            font-weight: 600;
            color: var(--color-text-secondary);
            flex: 0 0 auto;
        }
        .stats-custom-range .stats-apply-btn {
            padding: 0 12px;
            min-width: 64px;
            justify-content: center;
        }
        /* 摘要、瀑布流与通用卡片 */
        .stats-summary-grid { display: grid; grid-template-columns: repeat(6, minmax(0, 1fr)); gap: 12px; }
        .stats-summary-card {
            min-width: 0;
            padding: 16px 16px 14px; border-radius: 22px;
            background: linear-gradient(180deg, rgba(255,255,255,0.98), rgba(248,250,252,0.95));
            border: 1px solid rgba(226,232,240,0.88); box-shadow: 0 14px 30px rgba(15,23,42,0.04);
        }
        .stats-summary-label { font-size: 12px; font-weight: 800; color: var(--color-text-secondary); }
        .stats-summary-value {
            min-width: 0;
            margin-top: 10px;
            font-size: clamp(24px, 1.85vw, 30px);
            line-height: 1.04;
            letter-spacing: -0.03em;
            font-weight: 820;
            color: #0f172a;
            overflow-wrap: anywhere;
            word-break: break-word;
        }
        .stats-summary-meta {
            min-width: 0;
            margin-top: 8px;
            font-size: 12px;
            color: var(--color-text-secondary);
            overflow-wrap: anywhere;
            word-break: break-word;
        }
        .stats-featured { display: flex; flex-direction: column; gap: 16px; }
        .stats-wall { column-count: 3; column-gap: 16px; }
        .stats-card {
            --stats-accent: #4f46e5;
            display: inline-block;
            width: 100%;
            break-inside: avoid;
            margin-bottom: 16px;
            padding: 18px; border-radius: 24px;
            background: linear-gradient(180deg, rgba(255,255,255,0.98), rgba(248,250,252,0.95));
            border: 1px solid rgba(226,232,240,0.88); box-shadow: 0 16px 34px rgba(15,23,42,0.04);
            position: relative; overflow: hidden;
        }
        .stats-featured .stats-card { margin-bottom: 0; }
        .stats-card--full { width: 100%; }
        .stats-card::before {
            content: ''; position: absolute; inset: 0 auto auto 0; width: 100%; height: 4px;
            background: linear-gradient(90deg, var(--stats-accent), transparent 80%); opacity: 0.9;
        }
        .stats-card-head h3 { margin: 0; font-size: 18px; font-weight: 800; letter-spacing: -0.02em; color: #111827; }
        .stats-card-head p { margin: 6px 0 0; font-size: 12px; line-height: 1.7; color: var(--color-text-secondary); }
        .stats-card-eyebrow { margin-bottom: 8px; font-size: 11px; font-weight: 800; letter-spacing: 0.08em; text-transform: uppercase; color: var(--stats-accent); }
        .stats-card-body { margin-top: 14px; }
        .stats-card-footer { margin-top: 14px; padding-top: 12px; border-top: 1px solid rgba(226,232,240,0.82); }
        .stats-empty-card { padding: 28px 18px; border-radius: 18px; text-align: center; background: rgba(248,250,252,0.82); border: 1px dashed rgba(148,163,184,0.28); color: var(--color-text-secondary); }
        /* 折线、柱状、条形与面积块 */
        .stats-chart-block { display: flex; flex-direction: column; gap: 10px; }
        .stats-sparkline { display: block; width: 100%; height: auto; }
        .stats-sparkline-footer { display: grid; grid-template-columns: repeat(auto-fit, minmax(48px, 1fr)); gap: 8px; font-size: 10px; color: var(--color-text-secondary); }
        .stats-sparkline-footer span { text-align: center; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
        .stats-column-chart { display: grid; grid-template-columns: repeat(auto-fit, minmax(42px, 1fr)); gap: 10px; align-items: end; min-height: 184px; }
        .stats-column-item { min-width: 0; display: grid; gap: 8px; justify-items: center; align-items: end; }
        .stats-column-stage, .stats-histogram-stage, .stats-stacked-stage { position: relative; width: 100%; height: 124px; }
        .stats-column-bar {
            position: absolute; left: 50%; bottom: 0; transform: translateX(-50%);
            display: block; width: 100%; max-width: 26px; min-height: 8px; border-radius: 999px 999px 8px 8px;
            box-shadow: inset 0 1px 0 rgba(255,255,255,0.25);
        }
        .stats-column-value {
            width: 100%;
            font-size: 11px;
            font-weight: 700;
            color: #0f172a;
            text-align: center;
            white-space: nowrap;
            overflow: hidden;
            text-overflow: ellipsis;
        }
        .stats-column-label { width: 100%; font-size: 10px; color: var(--color-text-secondary); text-align: center; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
        .stats-bar-rows { display: flex; flex-direction: column; gap: 12px; }
        .stats-bar-top { display: flex; justify-content: space-between; gap: 10px; align-items: center; font-size: 13px; }
        .stats-bar-label { min-width: 0; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; color: #111827; font-weight: 700; }
        .stats-bar-value { color: var(--color-text-secondary); font-weight: 700; flex-shrink: 0; }
        .stats-bar-track { height: 10px; border-radius: 999px; background: rgba(226,232,240,0.72); overflow: hidden; }
        .stats-bar-fill { display: block; height: 100%; border-radius: inherit; }
        .stats-treemap { display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 10px; }
        .stats-treemap-tile {
            position: relative; min-height: 108px; padding: 14px; border-radius: 18px; overflow: hidden;
            border: 1px solid rgba(226,232,240,0.72);
        }
        .stats-treemap-glow {
            position: absolute; inset: auto -20px -22px auto; width: 90px; height: 90px; border-radius: 28px; filter: blur(12px);
        }
        .stats-treemap-label, .stats-treemap-value { position: relative; z-index: 1; }
        .stats-treemap-label { font-size: 12px; font-weight: 800; color: #475569; line-height: 1.5; }
        .stats-treemap-value { margin-top: 12px; font-size: 22px; line-height: 1.05; letter-spacing: -0.03em; font-weight: 820; color: #111827; }
        .stats-stacked-columns {
            display: grid; grid-template-columns: repeat(auto-fit, minmax(44px, 1fr)); gap: 10px; align-items: end; min-height: 220px;
        }
        .stats-stacked-item { min-width: 0; display: grid; gap: 8px; justify-items: center; align-items: end; }
        .stats-stacked-track {
            position: absolute; left: 50%; bottom: 0; transform: translateX(-50%);
            width: 100%; max-width: 28px; min-height: 12px; display: flex; flex-direction: column-reverse;
            border-radius: 999px; overflow: hidden; background: rgba(226,232,240,0.7);
        }
        .stats-stacked-segment { display: block; width: 100%; }
        .stats-stacked-total { font-size: 11px; font-weight: 800; color: #0f172a; }
        .stats-stacked-label { width: 100%; font-size: 10px; color: var(--color-text-secondary); text-align: center; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
        .stats-histogram {
            display: grid; grid-template-columns: repeat(auto-fit, minmax(54px, 1fr)); gap: 10px; align-items: end; min-height: 184px;
        }
        .stats-histogram-bin { min-width: 0; display: grid; gap: 8px; justify-items: center; align-items: end; }
        .stats-histogram-bar {
            position: absolute; left: 50%; bottom: 0; transform: translateX(-50%);
            display: block; width: 100%; max-width: 52px; min-height: 6px; border-radius: 12px 12px 4px 4px;
            box-shadow: inset 0 1px 0 rgba(255,255,255,0.25);
        }
        .stats-histogram-value { font-size: 10px; font-weight: 800; color: #0f172a; }
        .stats-histogram-label { width: 100%; font-size: 10px; color: var(--color-text-secondary); text-align: center; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
        /* 环图、热力图与比较图 */
        .stats-donut-wrap { display: grid; grid-template-columns: 180px minmax(0, 1fr); gap: 12px; align-items: center; }
        .stats-donut { width: 100%; height: auto; max-width: 180px; margin: 0 auto; }
        .stats-donut path { transition: opacity 0.2s ease, transform 0.2s ease; transform-origin: 90px 90px; }
        .stats-donut path:hover { opacity: 0.85; cursor: pointer; transform: scale(1.02); }
        .stats-donut-center-value { font-size: 17px; font-weight: 700; fill: #7f1d1d; }
        .stats-donut-center-value.small { font-size: 15px; }
        .stats-donut-center-value.tiny { font-size: 13px; }
        .stats-donut-center-label { font-size: 11px; fill: #94a3b8; }
        .stats-donut-legend { display: flex; flex-direction: column; gap: 8px; }
        .stats-donut-legend-item { display: grid; grid-template-columns: 10px minmax(0, 1fr) auto; gap: 8px; align-items: center; font-size: 12px; }
        .stats-donut-legend-dot { width: 10px; height: 10px; border-radius: 50%; }
        .stats-donut-legend-name { min-width: 0; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; color: #111827; }
        .stats-donut-legend-value { flex-shrink: 0; color: #334155; font-weight: 700; }
        .stats-heat-strip { display: grid; grid-template-columns: repeat(auto-fit, minmax(22px, 1fr)); gap: 6px; }
        .stats-heat-cell { aspect-ratio: 1 / 1; min-height: 28px; border-radius: 10px; display: flex; align-items: flex-end; justify-content: center; padding: 4px; color: #475569; font-size: 9px; font-weight: 700; }
        .stats-token-cloud { display: flex; flex-wrap: wrap; gap: 8px; }
        .stats-token { display: inline-flex; align-items: center; height: 34px; padding: 0 12px; border-radius: 999px; font-size: 12px; font-weight: 700; }
        .stats-token-1 { background: rgba(99,102,241,0.10); color: #4338ca; }
        .stats-token-2 { background: rgba(59,130,246,0.10); color: #1d4ed8; }
        .stats-token-3 { background: rgba(236,72,153,0.10); color: #be185d; }
        .stats-token-4 { background: rgba(16,185,129,0.10); color: #047857; }
        .stats-matrix {
            display: grid; grid-template-columns: 44px repeat(6, minmax(0, 1fr)); gap: 8px; align-items: center;
        }
        .stats-matrix-corner { height: 20px; }
        .stats-matrix-xlabel, .stats-matrix-ylabel { font-size: 11px; font-weight: 800; color: var(--color-text-secondary); text-align: center; }
        .stats-matrix-ylabel { text-align: left; padding-right: 4px; }
        .stats-matrix-cell {
            min-height: 44px; border-radius: 14px; display: flex; align-items: center; justify-content: center;
            border: 1px solid rgba(226,232,240,0.58); color: #0f172a; font-size: 11px; font-weight: 800;
        }
        .stats-matrix-cell span { opacity: 0.9; }
        .stats-metric-pairs { display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 10px; }
        .stats-metric-pair { padding: 12px 14px; border-radius: 16px; background: rgba(248,250,252,0.92); border: 1px solid rgba(226,232,240,0.8); }
        .stats-metric-pair span { display: block; font-size: 11px; font-weight: 700; color: var(--color-text-secondary); margin-bottom: 6px; }
        .stats-metric-pair strong { display: block; font-size: 20px; font-weight: 820; color: #0f172a; }
        .stats-dual-grid { display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 10px; }
        .stats-dual-card { padding: 14px; border-radius: 18px; background: rgba(248,250,252,0.92); border: 1px solid rgba(226,232,240,0.8); }
        .stats-dual-label { font-size: 12px; color: var(--color-text-secondary); font-weight: 700; }
        .stats-dual-value { margin-top: 8px; font-size: 24px; font-weight: 820; line-height: 1.06; letter-spacing: -0.03em; }
        .stats-dual-meta { margin-top: 6px; font-size: 12px; color: var(--color-text-secondary); }
        .stats-heatmap {
            --stats-heatmap-cell-size: 16px;
            --stats-heatmap-label-col: 28px;
            display: flex;
            flex-direction: column;
            gap: 6px;
            width: 100%;
            overflow-x: auto;
        }
        .stats-heatmap-months {
            display: grid;
            grid-template-columns: var(--stats-heatmap-label-col) repeat(var(--stats-heatmap-weeks), minmax(var(--stats-heatmap-cell-size), 1fr));
            gap: 2px;
            width: 100%;
            min-width: var(--stats-heatmap-min-width);
        }
        .stats-heatmap-corner { width: var(--stats-heatmap-label-col); }
        .stats-heatmap-month-label { font-size: 10px; color: var(--color-text-secondary); text-align: center; font-weight: 700; min-width: 16px; }
        .stats-heatmap-grid {
            display: grid;
            grid-template-columns: var(--stats-heatmap-label-col) repeat(var(--stats-heatmap-weeks), minmax(var(--stats-heatmap-cell-size), 1fr));
            grid-template-rows: repeat(7, auto);
            gap: 2px;
            width: 100%;
            min-width: var(--stats-heatmap-min-width);
        }
        .stats-heatmap-ylabel { display: flex; align-items: center; justify-content: flex-end; font-size: 9px; color: var(--color-text-secondary); padding-right: 4px; font-weight: 700; }
        .stats-heatmap-cell {
            width: 100%;
            height: auto;
            aspect-ratio: 1 / 1;
            border-radius: 3px;
            min-width: 0;
            min-height: 0;
        }
        .stats-heatmap-cell.empty { background: rgba(226,232,240,0.18); }
        .stats-heatmap-cell.in-range {
            box-shadow: inset 0 0 0 1px rgba(15,23,42,0.05), 0 0 0 1px rgba(255,255,255,0.32);
            filter: saturate(1.02);
        }
        .stats-heatmap-footer { display: flex; align-items: center; gap: 4px; margin-top: 6px; }
        .stats-heatmap-legend-label { font-size: 10px; color: var(--color-text-secondary); font-weight: 700; }
        .stats-heatmap-legend-cell { width: 12px; height: 12px; border-radius: 2px; }
        .stats-comparison { display: flex; flex-direction: column; gap: 12px; }
        .stats-comparison-table { display: flex; flex-direction: column; gap: 10px; }
        .stats-comparison-row { display: grid; grid-template-columns: 52px minmax(0, 1fr); gap: 12px; align-items: start; }
        .stats-comparison-label { font-size: 12px; color: var(--color-text-secondary); font-weight: 800; text-align: right; line-height: 1.4; padding-top: 10px; }
        .stats-comparison-series { display: grid; grid-template-columns: repeat(3, minmax(0, 1fr)); gap: 10px; }
        .stats-comparison-metric { padding: 10px 12px; border-radius: 16px; background: rgba(248,250,252,0.92); border: 1px solid rgba(226,232,240,0.82); }
        .stats-comparison-metric-head { display: flex; justify-content: space-between; gap: 8px; align-items: baseline; margin-bottom: 8px; }
        .stats-comparison-metric-name { font-size: 11px; font-weight: 700; color: var(--color-text-secondary); }
        .stats-comparison-metric-head strong { font-size: 12px; font-weight: 800; color: #0f172a; white-space: nowrap; }
        .stats-comparison-track { height: 8px; border-radius: 999px; background: rgba(226,232,240,0.72); overflow: hidden; }
        .stats-comparison-fill { display: block; height: 100%; border-radius: inherit; }
        .stats-insights { display: flex; flex-direction: column; gap: 10px; }
        .stats-insight-item { display: flex; align-items: flex-start; gap: 10px; font-size: 13px; line-height: 1.7; color: #334155; }
        .stats-insight-icon { flex-shrink: 0; width: 28px; height: 28px; border-radius: 8px; display: flex; align-items: center; justify-content: center; font-size: 14px; }
        .stats-insight-text strong { color: #0f172a; font-weight: 800; }
        /* 响应式收敛 */
        ${mediaMax(
            BREAKPOINTS.XL,
            `
            .stats-summary-grid { grid-template-columns: repeat(3, minmax(0, 1fr)); }
            .stats-wall { column-count: 2; }
        `,
        )}
        ${mediaMax(
            BREAKPOINTS.MOBILE,
            `
            .stats-hero { grid-template-columns: 1fr; padding: 22px 20px; }
            .stats-summary-grid { grid-template-columns: repeat(2, minmax(0, 1fr)); }
            .stats-wall { column-count: 2; }
            .stats-donut-wrap { grid-template-columns: 1fr; gap: 10px; }
            .stats-donut { max-width: min(200px, 52vw); }
            .stats-donut-center-value { font-size: 16px; }
            .stats-donut-center-value.small { font-size: 14px; }
            .stats-donut-center-value.tiny { font-size: 12px; }
            .stats-donut-center-label { font-size: 10px; }
            .stats-donut-legend-item { font-size: 11px; }
            .stats-date-field { min-width: 132px; }
            .stats-range-group { margin-top: 0; }
        `,
        )}
        ${mediaMax(
            BREAKPOINTS.PHONE,
            `
            .stats-wall { column-count: 1; }
            .stats-summary-grid, .stats-metric-pairs, .stats-dual-grid, .stats-treemap { grid-template-columns: 1fr; }
            .stats-range-group, .stats-chip-row { align-items: stretch; }
            .stats-chip-row { justify-content: flex-start; }
            .stats-matrix { grid-template-columns: 36px repeat(6, minmax(0, 1fr)); gap: 6px; }
            .stats-matrix-cell { min-height: 38px; border-radius: 12px; font-size: 10px; }
            .stats-custom-range { flex-direction: column; align-items: stretch; }
            .stats-range-sep { display: none; }
            .stats-custom-range .stats-date-field,
            .stats-custom-range .stats-apply-btn { width: 100%; }
            .stats-column-stage, .stats-histogram-stage, .stats-stacked-stage { height: 112px; }
            .stats-heatmap { --stats-heatmap-cell-size: 10px; --stats-heatmap-label-col: 18px; }
            .stats-comparison-row { grid-template-columns: 1fr; gap: 8px; }
            .stats-comparison-label { text-align: left; padding-top: 0; }
            .stats-comparison-series { grid-template-columns: 1fr; }
            .stats-insight-item { font-size: 12px; }
        `,
        )}
    `,
    );
}

// 数据请求：固定本次范围快照，全部时间的三个边界查询并行执行。
async function fetchAllData({ rangeKey, range, today }) {
    const rangeParam = buildRequestRangeValue(rangeKey, range);
    const heatmapYear = resolveHeatmapYear(range);
    const [ledgerRange, notesRange, diaryRange] =
        rangeKey === 'all'
            ? await Promise.all([
                  fetchItemRangeBounds(api, {
                      type: 'ledger',
                      sortField: 'ledger_date',
                      startField: 'ledger_date',
                      endField: 'ledger_date',
                      fallbackEnd: range.end,
                  }),
                  fetchItemRangeBounds(api, {
                      type: 'note',
                      sortField: 'created_at',
                      startField: 'created_at',
                      endField: 'created_at',
                      fallbackEnd: range.end,
                  }),
                  fetchItemRangeBounds(api, {
                      type: 'diary',
                      sortField: 'diary_date',
                      startField: 'diary_date',
                      endField: 'diary_date',
                      fallbackEnd: range.end,
                  }),
              ])
            : [range, range, range];
    const [ledgerRes, tasksRes, eventsRes, notesRes, diaryRes, heatmapRes, comparisonRes, moodRes] = await Promise.all([
        api.get(
            '/stats/ledger',
            rangeKey === 'all' ? { start_date: ledgerRange.start, end_date: ledgerRange.end } : { range: rangeParam },
        ),
        api.get('/stats/tasks', { range: rangeParam }),
        api.get('/stats/events', { range: rangeParam }),
        api.get('/stats/notes/overview', {
            start_date: notesRange.start,
            end_date: notesRange.end,
            today: overviewReferenceDay(notesRange, today),
        }),
        api.get('/stats/diary/overview', {
            start_date: diaryRange.start,
            end_date: diaryRange.end,
            today: diaryRange.end,
            cadence_granularity: 'auto',
        }),
        api.get('/stats/activity-heatmap', { year: heatmapYear }),
        api.get('/stats/ledger/comparison', { months: 6 }),
        api.get('/config/diary/moods').catch(() => null),
    ]);
    const moodData = responseData(moodRes);
    return {
        data: {
            ledger: responseData(ledgerRes),
            tasks: responseData(tasksRes),
            events: responseData(eventsRes),
            notes: responseData(notesRes),
            diary: responseData(diaryRes),
        },
        heatmapData: responseData(heatmapRes),
        comparisonData: responseData(comparisonRes),
        moodEmojis: normalizeTextMap(moodData.mood_emojis, DEFAULT_MOOD_EMOJIS),
        moodLabels: normalizeTextMap(moodData.mood_labels, DEFAULT_MOOD_LABELS),
    };
}

// 各业务模块卡片按财务、任务、日程、笔记、日记顺序排列。
function financeCards() {
    const ledger = _data?.ledger || {};
    const range = deriveRangeDates();
    const trend = ledgerTrendSeries(ledger, range);
    const expenseByCategory = sortByNumericDesc(ledger.expense_by_category, 'total');
    const incomeByCategory = sortByNumericDesc(ledger.income_by_category, 'total');
    const expenseTotal = sumBy(expenseByCategory, 'total');
    const incomeTotal = sumBy(incomeByCategory, 'total');
    const balance = incomeTotal - expenseTotal;
    const topExpense = expenseByCategory[0];
    const topIncome = incomeByCategory[0];
    const expenseTree = expenseByCategory.slice(0, 8).map((item) => ({ ...item, category: item.category || '未分类' }));
    const trendLabels = trend.map((item) => item.label || '');
    const expenseValues = trend.map((item) => item.expense);
    const incomeValues = trend.map((item) => item.income);
    const rhythmSeries = ledgerRhythmSeries(ledger, range).slice(-8);
    return [
        renderCard({
            accent: '#ef4444',
            eyebrow: 'Finance',
            title: '收支总览',
            subtitle: `按${rangeLabel()}汇总收入、支出与结余。`,
            body: `
                <div class="stats-dual-grid">
                    <div class="stats-dual-card">
                        <div class="stats-dual-label">支出</div>
                        <div class="stats-dual-value" style="color:#dc2626;">${formatAmount(expenseTotal)}</div>
                        <div class="stats-dual-meta">${topExpense ? `最高分类 ${escapeHtml(topExpense.category || '未分类')}` : '暂无支出分类'}</div>
                    </div>
                    <div class="stats-dual-card">
                        <div class="stats-dual-label">收入</div>
                        <div class="stats-dual-value" style="color:#10b981;">${formatAmount(incomeTotal)}</div>
                        <div class="stats-dual-meta">${topIncome ? `主要来源 ${escapeHtml(topIncome.category || '未分类')}` : '暂无收入分类'}</div>
                    </div>
                </div>`,
            footer: renderMetricPairs([
                { label: '结余', value: formatAmount(balance) },
                { label: '支出分类数', value: formatCount(expenseByCategory.length) },
            ]),
        }),
        renderCard({
            accent: '#e15241',
            eyebrow: 'Finance',
            title: '支出走势',
            subtitle: `按${rangeLabel()}观察支出在时间轴上的波峰和低谷。`,
            body: renderSparkline(trendLabels, expenseValues, '#ef4444', formatAmount),
        }),
        renderCard({
            accent: '#10b981',
            eyebrow: 'Finance',
            title: '收入走势',
            subtitle: `按${rangeLabel()}查看进账在时间轴上的分布。`,
            body: renderSparkline(trendLabels, incomeValues, '#10b981', formatAmount),
        }),
        renderCard({
            accent: '#f97316',
            eyebrow: 'Finance',
            title: '支出分类',
            subtitle: `按${rangeLabel()}统计支出主要流向。`,
            body: renderDonut(
                expenseByCategory.slice(0, 6),
                'total',
                'category',
                ['#ef4444', '#f97316', '#f59e0b', '#fb7185', '#f43f5e', '#fdba74'],
                formatAmount(expenseTotal),
                '总支出',
                formatAmount,
            ),
        }),
        renderCard({
            accent: '#fb7185',
            eyebrow: 'Finance',
            title: '支出分层',
            subtitle: `按${rangeLabel()}用面积块对比各支出分类体量。`,
            body: renderTreemap(
                expenseTree,
                'total',
                'category',
                ['#ef4444', '#f97316', '#fb7185', '#f59e0b', '#f43f5e', '#fdba74'],
                formatAmount,
            ),
        }),
        renderCard({
            accent: '#14b8a6',
            eyebrow: 'Finance',
            title: '收入来源',
            subtitle: `按${rangeLabel()}查看收入主要来源。`,
            body: renderBarRows(incomeByCategory.slice(0, 6), 'total', 'category', '#14b8a6', formatAmount),
        }),
        renderCard({
            accent: '#f59e0b',
            eyebrow: 'Finance',
            title: '单笔金额分布',
            subtitle: `按${rangeLabel()}统计支出集中在哪些金额区间。`,
            body: renderHistogram(
                safeRecords(ledger.expense_amount_histogram),
                'count',
                'bucket',
                '#f59e0b',
                (value) => `${value} 笔`,
            ),
        }),
        renderCard({
            accent: '#f59e0b',
            eyebrow: 'Finance',
            title: `${rangeLabel()}节奏`,
            subtitle: `按${rangeLabel()}比较各时段收支量级。`,
            body: renderColumnChart(rhythmSeries, 'total', 'label', '#f59e0b', formatMoneyCompact),
        }),
    ].join('');
}

function taskCards() {
    const tasks = _data?.tasks || {};
    const totals = normalizeTaskTotals(tasks.totals);
    const totalCount = totals.total;
    const statusItems = [
        { label: '未完成', count: totals.open, color: TASK_TONES.open },
        { label: '已完成', count: totals.done, color: TASK_TONES.done },
        { label: '已取消', count: totals.cancelled, color: TASK_TONES.cancelled },
    ].filter((item) => item.count > 0);
    const weekly = safeRecords(tasks.weekly);
    const planItems = safeRecords(tasks.by_plan)
        .slice(0, 8)
        .map((item) => ({ label: item.plan || '未安排', count: item.count }));
    const categoryItems = safeRecords(tasks.by_category)
        .slice(0, 8)
        .map((item) => ({ label: item.category || '未分类', count: item.count }));
    return [
        renderCard({
            accent: '#10b981',
            eyebrow: 'Tasks',
            title: '任务状态',
            subtitle: `按${rangeLabel()}统一为未完成、已完成、已取消三种结果。`,
            body: renderDonut(
                statusItems,
                'count',
                'label',
                statusItems.map((item) => item.color),
                formatCount(totalCount),
                '总任务',
                (value) => `${value} 项`,
            ),
        }),
        renderCard({
            accent: '#22c55e',
            eyebrow: 'Tasks',
            title: '完成节奏',
            subtitle: `${rangeLabel()}里真正完成的任务节奏。`,
            body: renderSparkline(
                weekly.map((item) => item.week || ''),
                weekly.map((item) => nonNegativeNumber(item.done)),
                '#10b981',
                (value) => `${value} 项`,
            ),
            footer: renderMetricPairs([
                { label: `${rangeLabel()}新增`, value: formatCount(tasks.new_this_week || 0) },
                { label: '当前未完成', value: formatCount(totals.open) },
            ]),
        }),
        renderCard({
            accent: '#16a34a',
            eyebrow: 'Tasks',
            title: '新增与收口',
            subtitle: `按${rangeLabel()}比较新建、完成和取消的周节奏。`,
            body: renderStackedColumns(
                weekly.map((item) => ({
                    label: String(item.week || '').replace(/^\d{4}-W/, 'W'),
                    created: nonNegativeNumber(item.created),
                    done: nonNegativeNumber(item.done),
                    cancelled: nonNegativeNumber(item.cancelled),
                })),
                'label',
                [
                    { key: 'created', color: TASK_TONES.created },
                    { key: 'done', color: TASK_TONES.done },
                    { key: 'cancelled', color: TASK_TONES.cancelled },
                ],
                (value) => `${value}`,
            ),
            footer: `
                <div class="stats-chip-row">
                    <span class="stats-chip">浅橙 = 新增</span>
                    <span class="stats-chip">深绿 = 已完成</span>
                    <span class="stats-chip">灰蓝 = 已取消</span>
                </div>
            `,
        }),
        renderCard({
            accent: '#34d399',
            eyebrow: 'Tasks',
            title: '优先级分布',
            subtitle: `按${rangeLabel()}观察高优事项是否堆积。`,
            body: renderBarRows(
                safeRecords(tasks.by_priority).map((item) => ({ ...item, label: `优先级 ${item.priority ?? '未设'}` })),
                'count',
                'label',
                '#10b981',
                (value) => `${value} 项`,
            ),
        }),
        renderCard({
            accent: '#059669',
            eyebrow: 'Tasks',
            title: '计划分布',
            subtitle: `按${rangeLabel()}查看未完成任务主要分布在哪些日期桶。`,
            body: renderColumnChart(planItems, 'count', 'label', '#059669', (value) => `${value}`),
        }),
        renderCard({
            accent: '#0f766e',
            eyebrow: 'Tasks',
            title: '文字分类',
            subtitle: `按${rangeLabel()}查看未完成任务压在哪些非日期分类。`,
            body: renderColumnChart(categoryItems, 'count', 'label', '#0f766e', (value) => `${value}`),
        }),
    ].join('');
}

function eventCards() {
    const events = _data?.events || {};
    const weekly = safeRecords(events.weekly);
    const totalEvents = sumBy(weekly, 'count');
    const slotLabels = ['06-09', '09-12', '12-14', '14-18', '18-21', '21-24'];
    const weekdayLabels = ['周一', '周二', '周三', '周四', '周五', '周六', '周日'];
    return [
        renderCard({
            accent: '#6366f1',
            eyebrow: 'Events',
            title: '日程密度',
            subtitle: `按${rangeLabel()}查看安排数量随时间的变化。`,
            body: renderSparkline(
                weekly.map((item) => item.week || ''),
                weekly.map((item) => nonNegativeNumber(item.count)),
                '#6366f1',
                (value) => `${value} 个`,
            ),
            footer: renderMetricPairs([
                { label: '日程总量', value: formatCount(totalEvents) },
                { label: '分类数', value: formatCount(safeRecords(events.by_category).length) },
            ]),
        }),
        renderCard({
            accent: '#8b5cf6',
            eyebrow: 'Events',
            title: '常见时段',
            subtitle: `按${rangeLabel()}看一天里哪些时段最常被安排占据。`,
            body: renderBarRows(
                safeRecords(events.time_slots).map((item) => ({ ...item, label: item.slot })),
                'count',
                'label',
                '#8b5cf6',
                (value) => `${value} 个`,
            ),
        }),
        renderCard({
            accent: '#7c3aed',
            eyebrow: 'Events',
            title: '周内时段热力图',
            subtitle: `按${rangeLabel()}观察安排更容易扎堆在哪几天、哪几个时段。`,
            body: renderMatrixHeatmap(safeRecords(events.weekday_slots), slotLabels, weekdayLabels, '124, 58, 237'),
        }),
        renderCard({
            accent: '#4f46e5',
            eyebrow: 'Events',
            title: '日程分类',
            subtitle: `按${rangeLabel()}统计日程主题分布。`,
            body: renderDonut(
                safeRecords(events.by_category)
                    .slice(0, 6)
                    .map((item) => ({ ...item, category: item.category || '未分类' })),
                'count',
                'category',
                ['#6366f1', '#8b5cf6', '#3b82f6', '#a855f7', '#818cf8', '#60a5fa'],
                formatCount(totalEvents),
                '日程总数',
                (value) => `${value} 个`,
            ),
        }),
    ].join('');
}

function noteCards() {
    const notes = _data?.notes || {};
    const summary = notes.summary || {};
    const cadenceGranularity = notes.cadence_granularity || 'day';
    const categories = safeRecords(notes.categories);
    const hotTags = safeRecords(notes.hot_tags)
        .slice(0, 10)
        .map((item) => ({ label: `#${item.tag}`, value: item.count }));
    const cadenceItems = safeRecords(notes.cadence).map((item) => ({ label: item.label, count: item.count }));
    const cadenceBody =
        cadenceGranularity === 'day'
            ? renderHeatStrip(cadenceItems, 'count', 'label', '59, 130, 246', (value) => `${value}`, {
                  columns: Math.min(Math.max(cadenceItems.length, 1), 7),
              })
            : renderColumnChart(cadenceItems, 'count', 'label', '#3b82f6', (value) => `${value}`);
    return [
        renderCard({
            accent: '#3b82f6',
            eyebrow: 'Notes',
            title: '笔记节奏',
            subtitle: noteCadenceSubtitle(cadenceGranularity, rangeLabel()),
            body: cadenceBody,
            footer: renderMetricPairs([
                { label: `${rangeLabel()}笔记`, value: formatCount(summary.total_count || 0) },
                { label: '范围尾端近 7 天新增', value: formatCount(summary.week_new_count || 0) },
            ]),
        }),
        renderCard({
            accent: '#2563eb',
            eyebrow: 'Notes',
            title: '笔记分类',
            subtitle: `按${rangeLabel()}统计知识沉淀主题。`,
            body: renderBarRows(
                categories.map((item) => ({ ...item, category: item.category || '未分类' })),
                'count',
                'category',
                '#3b82f6',
                (value) => `${value} 条`,
            ),
        }),
        renderCard({
            accent: '#0ea5e9',
            eyebrow: 'Notes',
            title: '高频标签',
            subtitle: `按${rangeLabel()}统计高频标签。`,
            body: renderTokenCloud(hotTags, (value) => `${value}`),
            footer: renderMetricPairs([
                { label: '平均长度', value: `${summary.average_length || 0}` },
                { label: '带标签比例', value: formatPercent(summary.tagged_rate || 0) },
            ]),
        }),
    ].join('');
}

function diaryCards() {
    const diary = _data?.diary || {};
    const summary = diary.summary || {};
    const cadenceGranularity = diary.cadence_granularity || 'day';
    const cadenceItems = safeRecords(diary.cadence).map((item) => ({
        ...item,
        mood_label: formatMoodLabel(item.mood),
        template_label: item.template_id || '手写',
    }));
    const moodItems = safeRecords(diary.mood_breakdown)
        .slice(0, 6)
        .map((item) => ({ ...item, label: formatMoodLabel(item.mood) }));
    const densityBody =
        cadenceGranularity === 'day'
            ? renderHeatStrip(
                  cadenceItems.map((item) => ({ label: item.label, words: item.words })),
                  'words',
                  'label',
                  '236, 72, 153',
                  formatWordCompact,
              )
            : renderColumnChart(
                  cadenceItems.map((item) => ({ label: item.label, words: item.words })),
                  'words',
                  'label',
                  '#ec4899',
                  formatWordCompact,
              );
    return [
        renderCard({
            accent: '#ec4899',
            eyebrow: 'Diary',
            title: '心情分布',
            subtitle: `${diaryRangeSentence()}常见情绪落点。`,
            body: renderDonut(
                moodItems,
                'count',
                'label',
                ['#ec4899', '#f472b6', '#fb7185', '#f9a8d4', '#db2777', '#be185d'],
                formatCount(summary.entry_count || 0),
                `${rangeLabel()}篇数`,
                (value) => `${value} 条`,
            ),
        }),
        renderCard({
            accent: '#f43f5e',
            eyebrow: 'Diary',
            title: '书写密度',
            subtitle: diaryCadenceSubtitle(cadenceGranularity),
            body: densityBody,
            footer: renderMetricPairs([
                { label: '连续天数', value: formatCount(summary.current_streak || 0) },
                { label: '填充率', value: formatPercent(summary.fill_rate || 0) },
            ]),
        }),
        renderCard({
            accent: '#e11d48',
            eyebrow: 'Diary',
            title: '模板与回看',
            subtitle: `${diaryRangeSentence()}常用模板和回看节奏。`,
            body: renderBarRows(
                safeRecords(diary.template_usage)
                    .slice(0, 5)
                    .map((item) => ({ ...item, label: item.template_id || '手写' })),
                'count',
                'label',
                '#ec4899',
                (value) => `${value} 次`,
            ),
            footer: renderMetricPairs([
                {
                    label: '区间最长连续',
                    value: formatCount(summary.period_longest_streak || 0),
                },
                { label: '总字数', value: formatCount(summary.total_words || 0) },
            ]),
        }),
    ].join('');
}

// 页面级摘要、瀑布流与交互绑定。
function renderSummary() {
    const ledger = _data?.ledger || {};
    const tasks = _data?.tasks || {};
    const events = _data?.events || {};
    const notes = _data?.notes || {};
    const diary = _data?.diary || {};
    const totals = normalizeTaskTotals(tasks.totals);
    const totalEvents = sumBy(events.weekly, 'count');
    const expenseTotal = sumBy(ledger.expense_by_category, 'total');
    const incomeTotal = sumBy(ledger.income_by_category, 'total');
    const rangeTitle = _range === 'custom' ? '自定义范围' : rangeLabel();
    return `
        <section class="stats-summary-grid">
            <article class="stats-summary-card"><div class="stats-summary-label">${rangeTitle}支出</div><div class="stats-summary-value">${formatAmount(expenseTotal)}</div><div class="stats-summary-meta">收入 ${formatAmount(incomeTotal)}</div></article>
            <article class="stats-summary-card"><div class="stats-summary-label">${rangeTitle}结余</div><div class="stats-summary-value">${formatAmount(incomeTotal - expenseTotal)}</div><div class="stats-summary-meta">按当前范围计算</div></article>
            <article class="stats-summary-card"><div class="stats-summary-label">${rangeTitle}任务</div><div class="stats-summary-value">${formatCount(totals.total)}</div><div class="stats-summary-meta">完成 ${formatCount(totals.done)} 项 · 取消 ${formatCount(totals.cancelled)} 项</div></article>
            <article class="stats-summary-card"><div class="stats-summary-label">${rangeTitle}日程</div><div class="stats-summary-value">${formatCount(totalEvents)}</div><div class="stats-summary-meta">分类 ${formatCount(safeRecords(events.by_category).length)} 种</div></article>
            <article class="stats-summary-card"><div class="stats-summary-label">${rangeTitle}笔记</div><div class="stats-summary-value">${formatCount(notes.summary?.total_count || 0)}</div><div class="stats-summary-meta">范围尾端近 7 天新增 ${formatCount(notes.summary?.week_new_count || 0)}</div></article>
            <article class="stats-summary-card"><div class="stats-summary-label">日记</div><div class="stats-summary-value">${formatCount(diary.summary?.entry_count || 0)}</div><div class="stats-summary-meta">区间最长 ${formatCount(diary.summary?.period_longest_streak || 0)} 天</div></article>
        </section>
    `;
}

function renderWall() {
    return `
        ${renderFeaturedDeck()}
        <section class="stats-wall">
            ${_data ? renderInsightCard(_data) : ''}
            ${renderComparisonCard()}
            ${financeCards()}
            ${taskCards()}
            ${eventCards()}
            ${noteCards()}
            ${diaryCards()}
        </section>
    `;
}

function renderPage() {
    if (!_container) return;
    ensureStyles();
    if (_loading && _activeRequestSignature !== _dataSignature) {
        _container.innerHTML = `
            <div class="stats-shell" aria-busy="true">
                <div class="stats-empty-card" role="status" aria-live="polite">正在加载统计视图...</div>
            </div>
        `;
        return;
    }
    _chartSequence = 0;
    const range = deriveRangeDates();
    const rangeSummary = _range === 'all' ? '全部时间' : `${range.start} → ${range.end}`;
    _container.innerHTML = `
        <div class="stats-shell" ${_loading ? 'aria-busy="true"' : ''}>
            <div class="stats-stack">
                <section class="stats-hero">
                    <div>
                        <h2>📊 统计</h2>
                        <p>按时间范围查看财务、任务、日程、笔记和日记的整体变化。</p>
                        <div class="stats-hero-tags">
                            <span class="stats-chip">${rangeLabel()}</span>
                            <span class="stats-chip">${rangeSummary}</span>
                            <span class="stats-chip">按模块查看</span>
                            <span class="stats-chip">笔记与日记同步更新</span>
                            ${_loading ? '<span class="stats-chip" role="status" aria-live="polite">正在更新...</span>' : ''}
                        </div>
                    </div>
                    <div class="stats-range-group">
                        <div class="stats-chip-row">
                            ${RANGE_PRESET_OPTIONS.map((option) => `<button type="button" class="stats-range-btn${_range === option.key ? ' active' : ''}" data-range="${escapeHtml(option.key)}" aria-pressed="${_range === option.key}">${escapeHtml(option.label)}</button>`).join('')}
                        </div>
                        ${
                            _range === 'custom'
                                ? `
                            <div class="stats-custom-range">
                                <input type="text" class="stats-date-field" id="stats-custom-start" inputmode="numeric" autocomplete="off" spellcheck="false" placeholder="YYYY-MM-DD" value="${escapeHtml(_customDraftStart || _customStart || range.start)}" aria-label="统计开始日期">
                                <span class="stats-range-sep">至</span>
                                <input type="text" class="stats-date-field" id="stats-custom-end" inputmode="numeric" autocomplete="off" spellcheck="false" placeholder="YYYY-MM-DD" value="${escapeHtml(_customDraftEnd || _customEnd || range.end)}" aria-label="统计结束日期">
                                <button type="button" class="stats-range-btn stats-apply-btn" id="stats-custom-apply">应用</button>
                            </div>
                        `
                                : ''
                        }
                    </div>
                </section>
                ${renderSummary()}
                ${renderWall()}
            </div>
        </div>
    `;
    attachListeners();
}

function attachListeners() {
    if (!_container) return;
    const container = _container;
    container.querySelectorAll('.stats-range-btn[data-range]').forEach((button) => {
        button.onclick = async () => {
            if (_container !== container) return;
            const nextRange = button.dataset.range || '';
            if (!RANGE_KEYS.has(nextRange)) return;
            if (nextRange === _range) return;
            if (nextRange === 'custom' && (!_customStart || !_customEnd)) {
                const fallback = deriveRangeDates();
                _customStart = fallback.start;
                _customEnd = fallback.end;
                _customDraftStart = _customStart;
                _customDraftEnd = _customEnd;
            }
            _range = nextRange;
            await loadAndRender();
        };
    });
    const customStart = container.querySelector('#stats-custom-start');
    const customEnd = container.querySelector('#stats-custom-end');
    const customApply = container.querySelector('#stats-custom-apply');
    const applyCustomRange = async () => {
        if (_container !== container) return;
        const nextStart = String(customStart?.value || '').trim();
        const nextEnd = String(customEnd?.value || '').trim();
        _customDraftStart = nextStart;
        _customDraftEnd = nextEnd;
        if (!isValidDateInput(nextStart) || !isValidDateInput(nextEnd)) {
            showToast('请输入有效日期，格式为 YYYY-MM-DD', 'error');
            return;
        }
        if (nextStart > nextEnd) {
            showToast('开始日期不能晚于结束日期', 'error');
            return;
        }
        if (nextStart === _customStart && nextEnd === _customEnd && !_loading) return;
        _customStart = nextStart;
        _customEnd = nextEnd;
        await loadAndRender();
    };
    if (customStart) {
        customStart.oninput = () => {
            _customDraftStart = customStart.value;
        };
        bindEnterAction(customStart, applyCustomRange);
    }
    if (customEnd) {
        customEnd.oninput = () => {
            _customDraftEnd = customEnd.value;
        };
        bindEnterAction(customEnd, applyCustomRange);
    }
    if (customApply) customApply.onclick = applyCustomRange;
}

async function loadAndRender() {
    if (!_container) return;
    const request = currentRangeRequest();
    if (_loading && request.signature === _activeRequestSignature) return;

    const container = _container;
    const version = ++_loadVersion;
    _activeRequestSignature = request.signature;
    _loading = true;
    renderPage();
    try {
        const result = await fetchAllData(request);
        if (!isCurrentLoad(container, version)) return;
        _data = result.data;
        _heatmapData = result.heatmapData;
        _comparisonData = result.comparisonData;
        _moodEmojis = result.moodEmojis;
        _moodLabels = result.moodLabels;
        _dataSignature = request.signature;
    } catch (error) {
        if (isCurrentLoad(container, version)) {
            if (_dataSignature !== request.signature) {
                _data = null;
                _heatmapData = null;
                _comparisonData = null;
                _moodEmojis = { ...DEFAULT_MOOD_EMOJIS };
                _moodLabels = { ...DEFAULT_MOOD_LABELS };
                _dataSignature = '';
            }
            showToast(`加载统计失败：${errorMessage(error)}`, 'error');
        }
    } finally {
        if (isCurrentLoad(container, version)) {
            _loading = false;
            _activeRequestSignature = '';
            renderPage();
        }
    }
}

// 路由生命周期：首屏可等待数据，销毁会让所有在途请求失效。
export async function render(container) {
    if (
        !container ||
        typeof container.querySelector !== 'function' ||
        typeof container.querySelectorAll !== 'function'
    ) {
        throw new TypeError('统计页需要有效的容器元素');
    }

    _loadVersion += 1;
    _container = container;
    _range = 'month';
    _loading = false;
    _activeRequestSignature = '';
    _dataSignature = '';
    _chartSequence = 0;
    _data = null;
    _heatmapData = null;
    _comparisonData = null;
    const initialRange = deriveRangeDates();
    _customStart = initialRange.start;
    _customEnd = initialRange.end;
    _customDraftStart = _customStart;
    _customDraftEnd = _customEnd;
    _moodEmojis = { ...DEFAULT_MOOD_EMOJIS };
    _moodLabels = { ...DEFAULT_MOOD_LABELS };
    await loadAndRender();
}

export function destroy() {
    _loadVersion += 1;
    _container = null;
    _range = 'month';
    _loading = false;
    _activeRequestSignature = '';
    _dataSignature = '';
    _chartSequence = 0;
    _data = null;
    _customStart = '';
    _customEnd = '';
    _customDraftStart = '';
    _customDraftEnd = '';
    _heatmapData = null;
    _comparisonData = null;
    _moodEmojis = { ...DEFAULT_MOOD_EMOJIS };
    _moodLabels = { ...DEFAULT_MOOD_LABELS };
}
