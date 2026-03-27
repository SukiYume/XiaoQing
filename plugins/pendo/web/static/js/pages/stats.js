import { api } from '../api.js';
import { showToast } from '../components/toast.js';
import { formatDateInput, pad2, todayStr as sharedTodayStr } from '../utils/format.js';
import { BREAKPOINTS, escapeHtml, injectStyles, mediaMax, pageShellCss } from '../utils/ui.js';

const CSS_ID = 'pendo-stats-waterfall-styles';
const RANGE_OPTIONS = [
    { key: 'week', label: '本周' },
    { key: 'month', label: '本月' },
    { key: 'quarter', label: '本季' },
    { key: 'year', label: '本年' },
    { key: 'last_year', label: '去年' },
    { key: 'custom', label: '自定义' },
];

let _container = null;
let _range = 'month';
let _loading = false;
let _data = null;
let _customStart = '';
let _customEnd = '';
let _customDraftStart = '';
let _customDraftEnd = '';

function nowDate() { return new Date(); }
function todayStr() {
    return sharedTodayStr();
}

function isValidDateInput(value) {
    if (!/^\d{4}-\d{2}-\d{2}$/.test(String(value || ''))) return false;
    const date = new Date(`${value}T00:00:00`);
    return !Number.isNaN(date.getTime()) && formatDateInput(date) === value;
}

function formatMoney(value) { return `¥${Number(value || 0).toFixed(2)}`; }
function formatMoneyCompact(value) {
    const amount = Number(value || 0);
    const sign = amount < 0 ? '-' : '';
    const absolute = Math.abs(amount);
    if (absolute >= 10000) return `${sign}¥${(absolute / 10000).toFixed(1)}万`;
    if (absolute >= 1000) return `${sign}¥${(absolute / 1000).toFixed(1)}k`;
    return `${sign}¥${absolute.toFixed(0)}`;
}
function formatCount(value) { return `${Number(value || 0)}`; }
function formatPercent(value) { return `${Math.round(Number(value || 0) * 100)}%`; }
function sumBy(items, key) { return (items || []).reduce((sum, item) => sum + Number(item?.[key] || 0), 0); }
function safeArray(value) { return Array.isArray(value) ? value : []; }
function rangeLabel() { return RANGE_OPTIONS.find((item) => item.key === _range)?.label || '当前范围'; }
function compactAxisLabel(value) {
    const text = String(value || '');
    if (/^\d{4}-\d{2}-\d{2}$/.test(text)) return text.slice(5).replace('-', '/');
    if (/^\d{4}-W\d{2}$/.test(text)) return text.replace(/^\d{4}-/, '');
    if (/^\d{4}-\d{2}$/.test(text)) return text.slice(2).replace('-', '/');
    return text.length > 10 ? `${text.slice(0, 8)}…` : text;
}

function sortByNumericDesc(items, key) {
    return safeArray(items).slice().sort((a, b) => Number(b?.[key] || 0) - Number(a?.[key] || 0));
}

function buildAxisTickLabels(labels, maxTicks = 6) {
    const all = safeArray(labels).map(compactAxisLabel);
    if (all.length <= maxTicks) return all;
    const step = Math.max(1, Math.ceil((all.length - 1) / (maxTicks - 1)));
    const picked = [];
    for (let index = 0; index < all.length; index += step) picked.push(all[index]);
    if (picked[picked.length - 1] !== all[all.length - 1]) picked[picked.length - 1] = all[all.length - 1];
    return picked;
}

function sampleIndexes(length, maxPoints = 18) {
    if (length <= maxPoints) return Array.from({ length }, (_, index) => index);
    const step = (length - 1) / (maxPoints - 1);
    const picked = new Set();
    for (let index = 0; index < maxPoints; index += 1) {
        picked.add(Math.round(index * step));
    }
    picked.add(0);
    picked.add(length - 1);
    return Array.from(picked).sort((a, b) => a - b);
}

function buildRequestRangeValue() {
    const range = deriveRangeDates();
    return `${range.start}..${range.end}`;
}

function deriveRangeDates() {
    const now = nowDate();
    if (_range === 'week') {
        const monday = new Date(now);
        monday.setDate(now.getDate() - ((now.getDay() + 6) % 7));
        return {
            start: `${monday.getFullYear()}-${pad2(monday.getMonth() + 1)}-${pad2(monday.getDate())}`,
            end: todayStr(),
        };
    }
    if (_range === 'month') return { start: `${now.getFullYear()}-${pad2(now.getMonth() + 1)}-01`, end: todayStr() };
    if (_range === 'quarter') {
        const firstMonth = Math.floor(now.getMonth() / 3) * 3 + 1;
        return { start: `${now.getFullYear()}-${pad2(firstMonth)}-01`, end: todayStr() };
    }
    if (_range === 'last_year') {
        const year = now.getFullYear() - 1;
        return { start: `${year}-01-01`, end: `${year}-12-31` };
    }
    if (_range === 'custom') {
        if (_customStart && _customEnd) return { start: _customStart, end: _customEnd };
        const end = todayStr();
        const startDate = new Date(now);
        startDate.setDate(now.getDate() - 29);
        return { start: formatDateInput(startDate), end };
    }
    return { start: `${now.getFullYear()}-01-01`, end: todayStr() };
}

function diffDays(start, end) {
    if (!start || !end) return 0;
    const startDate = new Date(`${start}T00:00:00`);
    const endDate = new Date(`${end}T00:00:00`);
    const diff = endDate.getTime() - startDate.getTime();
    return Math.max(0, Math.round(diff / 86400000));
}

function ledgerRhythmSeries(ledger, range = deriveRangeDates()) {
    const spanDays = diffDays(range.start, range.end);
    const useMonthly = _range === 'year' || _range === 'last_year' || spanDays > 62;
    const source = useMonthly && safeArray(ledger.monthly).length
        ? safeArray(ledger.monthly)
        : safeArray(ledger.daily);
    const labelKey = useMonthly ? 'month' : 'date';
    return source.map((item) => ({
        label: compactAxisLabel(String(item[labelKey] || '')),
        total: Number(item.expense || 0) + Number(item.income || 0),
    }));
}

function deriveWritingMonth(range = deriveRangeDates()) {
    const endDate = range.end ? new Date(range.end) : nowDate();
    return { year: endDate.getFullYear(), month: endDate.getMonth() + 1 };
}

function sparklinePath(values, width = 440, height = 168, padding = 18) {
    if (!values.length) return { line: '', area: '', points: [] };
    const max = Math.max(...values, 1);
    const step = values.length > 1 ? (width - padding * 2) / (values.length - 1) : 0;
    const points = values.map((value, index) => {
        const x = padding + step * index;
        const y = height - padding - ((value / max) * (height - padding * 2));
        return [x, y];
    });
    const line = points.map(([x, y], index) => `${index === 0 ? 'M' : 'L'}${x},${y}`).join(' ');
    const area = `${line} L ${padding + step * (values.length - 1)},${height - padding} L ${padding},${height - padding} Z`;
    return { line, area, points };
}

function compressSeries(labels, values, maxPoints = 26) {
    const safeLabels = safeArray(labels);
    const safeValues = safeArray(values).map((value) => Number(value || 0));
    if (safeValues.length <= maxPoints) return { labels: safeLabels, values: safeValues };
    const innerSlots = Math.max(1, maxPoints - 2);
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
    if (!values.length) return '<div class="stats-empty-card">暂无数据</div>';
    const compressed = compressSeries(labels, values, 28);
    const gradId = `stats-grad-${color.replace(/[^a-zA-Z0-9]/g, '')}-${values.length}`;
    const { line, area, points } = sparklinePath(compressed.values);
    const max = Math.max(...compressed.values, 1);
    const footerLabels = buildAxisTickLabels(compressed.labels, 6);
    const visiblePointIndexes = sampleIndexes(compressed.values.length, 14);
    return `
        <div class="stats-chart-block">
            <svg viewBox="0 0 440 168" class="stats-sparkline">
                <defs>
                    <linearGradient id="${gradId}" x1="0" y1="0" x2="0" y2="1">
                        <stop offset="0%" stop-color="${color}" stop-opacity="0.28"></stop>
                        <stop offset="100%" stop-color="${color}" stop-opacity="0.03"></stop>
                    </linearGradient>
                </defs>
                <path d="${area}" fill="url(#${gradId})"></path>
                <path d="${line}" fill="none" stroke="${color}" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round"></path>
                ${points.map(([x, y], index) => visiblePointIndexes.includes(index) ? `
                    <g>
                        <circle cx="${x}" cy="${y}" r="4" fill="${color}"></circle>
                        <title>${escapeHtml(compressed.labels[index] || '')} · ${formatter(compressed.values[index])}</title>
                    </g>
                ` : '').join('')}
                ${[0.25, 0.5, 0.75].map((ratio) => {
                    const y = 168 - 18 - (ratio * (168 - 36));
                    return `<line x1="18" y1="${y}" x2="422" y2="${y}" stroke="rgba(148,163,184,0.18)" stroke-dasharray="4 7"></line>`;
                }).join('')}
                <text x="422" y="18" text-anchor="end" fill="#94a3b8" font-size="11">${formatter(max)}</text>
            </svg>
            <div class="stats-sparkline-footer">
                ${footerLabels.map((label) => `<span title="${escapeHtml(label)}">${escapeHtml(label)}</span>`).join('')}
            </div>
        </div>
    `;
}

function renderColumnChart(items, valueKey, labelKey, color, formatter) {
    if (!items.length) return '<div class="stats-empty-card">暂无数据</div>';
    const max = Math.max(...items.map((item) => Number(item[valueKey] || 0)), 1);
    return `
        <div class="stats-column-chart">
            ${items.map((item) => {
                const value = Number(item[valueKey] || 0);
                const height = Math.max(8, Math.round((value / max) * 100));
                return `
                    <div class="stats-column-item" title="${escapeHtml(item[labelKey])} · ${formatter(value)}">
                        <div class="stats-column-stage">
                            <span class="stats-column-bar" style="height:${height}%; background:${color};"></span>
                        </div>
                        <span class="stats-column-value">${formatter(value)}</span>
                        <span class="stats-column-label">${escapeHtml(compactAxisLabel(item[labelKey]))}</span>
                    </div>
                `;
            }).join('')}
        </div>
    `;
}

function renderBarRows(items, valueKey, labelKey, color, formatter) {
    if (!items.length) return '<div class="stats-empty-card">暂无数据</div>';
    const max = Math.max(...items.map((item) => Number(item[valueKey] || 0)), 1);
    return `
        <div class="stats-bar-rows">
            ${items.map((item) => {
                const value = Number(item[valueKey] || 0);
                return `
                    <div class="stats-bar-row">
                        <div class="stats-bar-top">
                            <span class="stats-bar-label">${escapeHtml(item[labelKey])}</span>
                            <span class="stats-bar-value">${formatter(value)}</span>
                        </div>
                        <div class="stats-bar-track">
                            <span class="stats-bar-fill" style="width:${Math.max(8, Math.round((value / max) * 100))}%; background:${color};"></span>
                        </div>
                    </div>
                `;
            }).join('')}
        </div>
    `;
}

function renderTreemap(items, valueKey, labelKey, colors, formatter) {
    if (!items.length) return '<div class="stats-empty-card">暂无数据</div>';
    const sorted = [...items]
        .map((item) => ({ ...item, __value: Number(item[valueKey] || 0) }))
        .filter((item) => item.__value > 0)
        .sort((a, b) => b.__value - a.__value)
        .slice(0, 8);
    if (!sorted.length) return '<div class="stats-empty-card">暂无数据</div>';
    const max = Math.max(...sorted.map((item) => item.__value), 1);
    return `
        <div class="stats-treemap">
            ${sorted.map((item, index) => {
                const ratio = item.__value / max;
                const span = ratio > 0.72 ? 2 : 1;
                const shade = (0.14 + ratio * 0.2).toFixed(2);
                return `
                    <div class="stats-treemap-tile" style="grid-column: span ${span}; background: linear-gradient(180deg, rgba(255,255,255,0.92), rgba(255,255,255,0.7)), rgba(255,255,255,0.88); border-color: rgba(255,255,255,0.55); box-shadow: inset 0 0 0 1px rgba(255,255,255,0.28);">
                        <span class="stats-treemap-glow" style="background:${colors[index % colors.length]}; opacity:${shade};"></span>
                        <div class="stats-treemap-label">${escapeHtml(item[labelKey])}</div>
                        <div class="stats-treemap-value">${formatter(item.__value)}</div>
                    </div>
                `;
            }).join('')}
        </div>
    `;
}

function renderStackedColumns(items, labelKey, segments, formatter) {
    if (!items.length) return '<div class="stats-empty-card">暂无数据</div>';
    const normalized = items.map((item) => {
        const total = segments.reduce((sum, segment) => sum + Number(item[segment.key] || 0), 0);
        return { ...item, __total: total };
    });
    const max = Math.max(...normalized.map((item) => item.__total), 1);
    return `
        <div class="stats-stacked-columns">
            ${normalized.map((item) => {
                const height = Math.max(12, Math.round((item.__total / max) * 100));
                return `
                    <div class="stats-stacked-item" title="${escapeHtml(item[labelKey])} · ${formatter(item.__total)}">
                        <div class="stats-stacked-stage">
                            <div class="stats-stacked-track" style="height:${height}%;">
                                ${segments.map((segment) => {
                                const value = Number(item[segment.key] || 0);
                                const segmentHeight = item.__total ? (value / item.__total) * 100 : 0;
                                return `<span class="stats-stacked-segment" style="height:${segmentHeight}%; background:${segment.color};"></span>`;
                                }).join('')}
                            </div>
                        </div>
                        <span class="stats-stacked-total">${formatter(item.__total)}</span>
                        <span class="stats-stacked-label">${escapeHtml(compactAxisLabel(item[labelKey]))}</span>
                    </div>
                `;
            }).join('')}
        </div>
    `;
}

function renderHistogram(items, valueKey, labelKey, color, formatter) {
    if (!items.length) return '<div class="stats-empty-card">暂无数据</div>';
    const max = Math.max(...items.map((item) => Number(item[valueKey] || 0)), 1);
    return `
        <div class="stats-histogram">
            ${items.map((item) => {
                const value = Number(item[valueKey] || 0);
                const height = Math.max(value ? 14 : 6, Math.round((value / max) * 100));
                return `
                    <div class="stats-histogram-bin" title="${escapeHtml(item[labelKey])} · ${formatter(value)}">
                        <div class="stats-histogram-stage">
                            <span class="stats-histogram-bar" style="height:${height}%; background:${color};"></span>
                        </div>
                        <span class="stats-histogram-value">${formatter(value)}</span>
                        <span class="stats-histogram-label">${escapeHtml(compactAxisLabel(item[labelKey]))}</span>
                    </div>
                `;
            }).join('')}
        </div>
    `;
}

function polarToCartesian(cx, cy, r, angle) {
    const rad = (angle - 90) * Math.PI / 180;
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
        `A ${outer} ${outer} 0 1 1 ${cx - 0.01} ${cy - outer}`,
        `A ${outer} ${outer} 0 1 1 ${cx} ${cy - outer}`,
        `M ${cx} ${cy - inner}`,
        `A ${inner} ${inner} 0 1 0 ${cx - 0.01} ${cy - inner}`,
        `A ${inner} ${inner} 0 1 0 ${cx} ${cy - inner}`,
        'Z',
    ].join(' ');
}

function renderMatrixHeatmap(rows, xLabels, yLabels, color) {
    if (!rows.length) return '<div class="stats-empty-card">暂无数据</div>';
    const matrix = new Map();
    rows.forEach((row) => {
        matrix.set(`${row.weekday}|${row.slot}`, Number(row.count || 0));
    });
    const max = Math.max(...rows.map((row) => Number(row.count || 0)), 1);
    return `
        <div class="stats-matrix">
            <div class="stats-matrix-corner"></div>
            ${xLabels.map((label) => `<div class="stats-matrix-xlabel">${escapeHtml(label)}</div>`).join('')}
            ${yLabels.map((weekday) => `
                <div class="stats-matrix-ylabel">${escapeHtml(weekday)}</div>
                ${xLabels.map((slot) => {
                    const value = Number(matrix.get(`${weekday}|${slot}`) || 0);
                    const opacity = value ? (0.12 + (value / max) * 0.88) : 0.06;
                    return `
                        <div class="stats-matrix-cell" title="${escapeHtml(weekday)} · ${escapeHtml(slot)} · ${value} 个"
                            style="background: rgba(${color}, ${opacity});">
                            <span>${value || ''}</span>
                        </div>
                    `;
                }).join('')}
            `).join('')}
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
    if (!items.length) return '<div class="stats-empty-card">暂无数据</div>';
    const normalized = [...items]
        .map((item) => ({ ...item, value: Number(item[valueKey] || 0) }))
        .filter((item) => item.value > 0);
    const total = normalized.reduce((sum, item) => sum + item.value, 0);
    if (!total) return '<div class="stats-empty-card">暂无数据</div>';
    if (normalized.length === 1) {
        return `
            <div class="stats-donut-wrap">
                <svg viewBox="0 0 180 180" class="stats-donut">
                    <circle cx="90" cy="90" r="55" fill="none" stroke="rgba(241,245,249,0.98)" stroke-width="24"></circle>
                    <circle cx="90" cy="90" r="55" fill="none" stroke="${colors[0]}" stroke-width="24"></circle>
                    <circle cx="90" cy="90" r="42" fill="#fff"></circle>
                    <text x="90" y="86" text-anchor="middle" class="${donutCenterValueClass(centerValue)}">${escapeHtml(centerValue)}</text>
                    <text x="90" y="106" text-anchor="middle" class="stats-donut-center-label">${escapeHtml(centerLabel)}</text>
                </svg>
                <div class="stats-donut-legend">
                    <div class="stats-donut-legend-item">
                        <span class="stats-donut-legend-dot" style="background:${colors[0]};"></span>
                        <span class="stats-donut-legend-name">${escapeHtml(normalized[0][labelKey])}</span>
                        <span class="stats-donut-legend-value">${formatter(normalized[0].value)}</span>
                    </div>
                </div>
            </div>
        `;
    }
    let cursor = 0;
    const outer = 67;
    const inner = 43;
    return `
        <div class="stats-donut-wrap">
            <svg viewBox="0 0 180 180" class="stats-donut">
                <circle cx="90" cy="90" r="${outer}" fill="rgba(248,250,252,0.96)"></circle>
                ${normalized.map((item, index) => `
                    ${(() => {
                        const angle = total ? (item.value / total) * 360 : 0;
                        const path = angle >= 359.999
                            ? fullRingPath(90, 90, outer, inner)
                            : arcPath(90, 90, outer, inner, cursor, cursor + angle);
                        const color = colors[index % colors.length];
                        cursor += angle;
                        return `<path d="${path}" fill="${color}" fill-rule="evenodd"></path>`;
                    })()}
                `).join('')}
                <circle cx="90" cy="90" r="${inner - 1}" fill="#fff"></circle>
                <text x="90" y="86" text-anchor="middle" class="${donutCenterValueClass(centerValue)}">${escapeHtml(centerValue)}</text>
                <text x="90" y="106" text-anchor="middle" class="stats-donut-center-label">${escapeHtml(centerLabel)}</text>
            </svg>
            <div class="stats-donut-legend">
                ${normalized.map((item, index) => `
                    <div class="stats-donut-legend-item">
                        <span class="stats-donut-legend-dot" style="background:${colors[index % colors.length]};"></span>
                        <span class="stats-donut-legend-name">${escapeHtml(item[labelKey])}</span>
                        <span class="stats-donut-legend-value">${formatter(item.value)}</span>
                    </div>
                `).join('')}
            </div>
        </div>
    `;
}

function renderHeatStrip(items, valueKey, labelKey, color) {
    if (!items.length) return '<div class="stats-empty-card">暂无数据</div>';
    const max = Math.max(...items.map((item) => Number(item[valueKey] || 0)), 1);
    return `
        <div class="stats-heat-strip">
            ${items.map((item) => {
                const value = Number(item[valueKey] || 0);
                const opacity = value ? (0.16 + (value / max) * 0.84) : 0.08;
                return `
                    <div class="stats-heat-cell" title="${escapeHtml(item[labelKey])} · ${value}"
                        style="background:rgba(${color}, ${opacity});">
                        <span>${escapeHtml(item[labelKey])}</span>
                    </div>
                `;
            }).join('')}
        </div>
    `;
}

function renderTokenCloud(items, formatter) {
    if (!items.length) return '<div class="stats-empty-card">暂无数据</div>';
    return `
        <div class="stats-token-cloud">
            ${items.map((item, index) => `
                <span class="stats-token stats-token-${(index % 4) + 1}">
                    ${escapeHtml(item.label)} · ${formatter(item.value)}
                </span>
            `).join('')}
        </div>
    `;
}

function renderMetricPairs(items) {
    if (!items.length) return '';
    return `
        <div class="stats-metric-pairs">
            ${items.map((item) => `
                <div class="stats-metric-pair">
                    <span>${escapeHtml(item.label)}</span>
                    <strong>${escapeHtml(item.value)}</strong>
                </div>
            `).join('')}
        </div>
    `;
}

function renderCard({ accent = '#4f46e5', eyebrow = '', title = '', subtitle = '', body = '', footer = '', classes = '' }) {
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
    injectStyles(CSS_ID, `
        ${pageShellCss('stats-shell', { compactPadding: '20px 16px 30px', compactBreakpoint: BREAKPOINTS.COMPACT })}
        .stats-stack { display: flex; flex-direction: column; gap: 18px; }
        .stats-hero {
            display: grid; grid-template-columns: minmax(0, 1fr) auto; gap: 18px; align-items: center;
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
        .stats-chip, .stats-range-btn, .stats-date-field {
            display: inline-flex; align-items: center; gap: 6px; height: 36px; padding: 0 14px; border-radius: 999px;
            border: 1px solid rgba(203,213,225,0.92); background: rgba(255,255,255,0.92); color: #475569; font-size: 12px; font-weight: 700;
        }
        .stats-range-btn { cursor: pointer; }
        .stats-range-btn.active { background: #4f46e5; border-color: #4f46e5; color: #fff; box-shadow: 0 10px 24px rgba(79,70,229,0.18); }
        .stats-custom-range {
            display: inline-flex;
            align-items: center;
            flex-wrap: wrap;
            gap: 8px;
            margin-top: 10px;
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
        .stats-summary-grid { display: grid; grid-template-columns: repeat(6, minmax(0, 1fr)); gap: 12px; }
        .stats-summary-card {
            padding: 16px 16px 14px; border-radius: 22px;
            background: linear-gradient(180deg, rgba(255,255,255,0.98), rgba(248,250,252,0.95));
            border: 1px solid rgba(226,232,240,0.88); box-shadow: 0 14px 30px rgba(15,23,42,0.04);
        }
        .stats-summary-label { font-size: 12px; font-weight: 800; color: var(--color-text-secondary); }
        .stats-summary-value { margin-top: 10px; font-size: 30px; line-height: 1.04; letter-spacing: -0.03em; font-weight: 820; color: #0f172a; }
        .stats-summary-meta { margin-top: 8px; font-size: 12px; color: var(--color-text-secondary); }
        .stats-wall { column-count: 3; column-gap: 16px; }
        .stats-card {
            --stats-accent: #4f46e5;
            display: inline-block; width: 100%; break-inside: avoid; margin-bottom: 16px;
            padding: 18px; border-radius: 24px;
            background: linear-gradient(180deg, rgba(255,255,255,0.98), rgba(248,250,252,0.95));
            border: 1px solid rgba(226,232,240,0.88); box-shadow: 0 16px 34px rgba(15,23,42,0.04);
            position: relative; overflow: hidden;
        }
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
        .stats-donut-wrap { display: grid; grid-template-columns: 180px minmax(0, 1fr); gap: 12px; align-items: center; }
        .stats-donut { width: 100%; height: auto; }
        .stats-donut-center-value { font-size: 18px; font-weight: 800; fill: #111827; }
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
        ${mediaMax(BREAKPOINTS.XL, `.stats-summary-grid { grid-template-columns: repeat(3, minmax(0, 1fr)); } .stats-wall { column-count: 2; }`)}
        ${mediaMax(BREAKPOINTS.COMPACT, `.stats-hero { grid-template-columns: 1fr; padding: 22px 20px; } .stats-summary-grid { grid-template-columns: repeat(2, minmax(0, 1fr)); } .stats-donut-wrap { grid-template-columns: 1fr; } .stats-date-field { min-width: 132px; }`)}
        ${mediaMax(BREAKPOINTS.STATS_SMALL, `
            .stats-wall { column-count: 1; }
            .stats-summary-grid, .stats-metric-pairs, .stats-dual-grid, .stats-treemap { grid-template-columns: 1fr; }
            .stats-chip-row { justify-content: flex-start; }
            .stats-matrix { grid-template-columns: 36px repeat(6, minmax(0, 1fr)); gap: 6px; }
            .stats-matrix-cell { min-height: 38px; border-radius: 12px; font-size: 10px; }
            .stats-custom-range { flex-direction: column; align-items: stretch; }
            .stats-range-sep { display: none; }
            .stats-custom-range .stats-date-field,
            .stats-custom-range .stats-apply-btn { width: 100%; }
            .stats-column-stage, .stats-histogram-stage, .stats-stacked-stage { height: 112px; }
        `)}
    `);
}

async function fetchAllData() {
    const range = deriveRangeDates();
    const writingMonth = deriveWritingMonth(range);
    const rangeParam = buildRequestRangeValue();
    const [ledgerRes, tasksRes, eventsRes, notesRes, diaryRes] = await Promise.all([
        api.get('/stats/ledger', { range: rangeParam }),
        api.get('/stats/tasks', { range: rangeParam }),
        api.get('/stats/events', { range: rangeParam }),
        api.get('/stats/notes/overview', { today: range.end }),
        api.get('/stats/diary/overview', { year: writingMonth.year, month: writingMonth.month, today: range.end }),
    ]);
    return {
        ledger: ledgerRes?.data || {},
        tasks: tasksRes?.data || {},
        events: eventsRes?.data || {},
        notes: notesRes?.data || {},
        diary: diaryRes?.data || {},
    };
}

function financeCards() {
    const ledger = _data?.ledger || {};
    const range = deriveRangeDates();
    const trend = safeArray(ledger.daily).length ? safeArray(ledger.daily) : safeArray(ledger.monthly);
    const expenseByCategory = sortByNumericDesc(ledger.expense_by_category, 'total');
    const incomeByCategory = sortByNumericDesc(ledger.income_by_category, 'total');
    const expenseTotal = sumBy(expenseByCategory, 'total');
    const incomeTotal = sumBy(incomeByCategory, 'total');
    const balance = incomeTotal - expenseTotal;
    const topExpense = expenseByCategory[0];
    const topIncome = incomeByCategory[0];
    const expenseTree = expenseByCategory.slice(0, 8).map((item) => ({ ...item, category: item.category || '未分类' }));
    const trendLabels = trend.map((item) => item.date || item.month || '');
    const expenseValues = trend.map((item) => Number(item.expense || 0));
    const incomeValues = trend.map((item) => Number(item.income || 0));
    const rhythmSeries = ledgerRhythmSeries(ledger, range).slice(-8);
    return [
        renderCard({
            accent: '#ef4444',
            eyebrow: 'Finance',
            title: '收支总览',
            subtitle: `${rangeLabel()}内的收入、支出和结余。`,
            body: `
                <div class="stats-dual-grid">
                    <div class="stats-dual-card">
                        <div class="stats-dual-label">支出</div>
                        <div class="stats-dual-value" style="color:#dc2626;">${formatMoney(expenseTotal)}</div>
                        <div class="stats-dual-meta">${topExpense ? `最高分类 ${escapeHtml(topExpense.category || '未分类')}` : '暂无支出分类'}</div>
                    </div>
                    <div class="stats-dual-card">
                        <div class="stats-dual-label">收入</div>
                        <div class="stats-dual-value" style="color:#10b981;">${formatMoney(incomeTotal)}</div>
                        <div class="stats-dual-meta">${topIncome ? `主要来源 ${escapeHtml(topIncome.category || '未分类')}` : '暂无收入分类'}</div>
                    </div>
                </div>`,
            footer: renderMetricPairs([
                { label: '结余', value: formatMoney(balance) },
                { label: '支出分类数', value: formatCount(expenseByCategory.length) },
            ]),
        }),
        renderCard({
            accent: '#e15241',
            eyebrow: 'Finance',
            title: '支出走势',
            subtitle: '观察支出波峰和低谷。',
            body: renderSparkline(trendLabels, expenseValues, '#ef4444', formatMoney),
        }),
        renderCard({
            accent: '#10b981',
            eyebrow: 'Finance',
            title: '收入走势',
            subtitle: '查看进账在当前范围内的分布。',
            body: renderSparkline(trendLabels, incomeValues, '#10b981', formatMoney),
        }),
        renderCard({
            accent: '#f97316',
            eyebrow: 'Finance',
            title: '支出分类',
            subtitle: '当前范围内最重的支出去向。',
            body: renderDonut(
                expenseByCategory.slice(0, 6),
                'total',
                'category',
                ['#ef4444', '#f97316', '#f59e0b', '#fb7185', '#f43f5e', '#fdba74'],
                formatMoney(expenseTotal),
                '总支出',
                formatMoney,
            ),
        }),
        renderCard({
            accent: '#fb7185',
            eyebrow: 'Finance',
            title: '支出分层',
            subtitle: '用面积块快速感知各分类体量。',
            body: renderTreemap(
                expenseTree,
                'total',
                'category',
                ['#ef4444', '#f97316', '#fb7185', '#f59e0b', '#f43f5e', '#fdba74'],
                formatMoney,
            ),
        }),
        renderCard({
            accent: '#14b8a6',
            eyebrow: 'Finance',
            title: '收入来源',
            subtitle: '看看钱主要从哪里进来。',
            body: renderBarRows(incomeByCategory.slice(0, 6), 'total', 'category', '#14b8a6', formatMoney),
        }),
        renderCard({
            accent: '#f59e0b',
            eyebrow: 'Finance',
            title: '单笔金额分布',
            subtitle: '支出多集中在哪一档金额区间。',
            body: renderHistogram(
                safeArray(ledger.expense_amount_histogram),
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
            subtitle: '按当前筛选范围比较收支量级。',
            body: renderColumnChart(
                rhythmSeries,
                'total',
                'label',
                '#f59e0b',
                formatMoneyCompact,
            ),
        }),
    ].join('');
}

function taskCards() {
    const tasks = _data?.tasks || {};
    const totals = tasks.totals || {};
    const totalCount = Number(totals.todo || 0) + Number(totals.in_progress || 0) + Number(totals.done || 0) + Number(totals.cancelled || 0);
    const statusItems = [
        { label: '待办', count: Number(totals.todo || 0) },
        { label: '进行中', count: Number(totals.in_progress || 0) },
        { label: '已完成', count: Number(totals.done || 0) },
        { label: '已取消', count: Number(totals.cancelled || 0) },
    ].filter((item) => item.count > 0);
    const weekly = safeArray(tasks.weekly);
    return [
        renderCard({
            accent: '#10b981',
            eyebrow: 'Tasks',
            title: '任务状态',
            subtitle: '当前任务整体处于什么状态。',
            body: renderDonut(statusItems, 'count', 'label', ['#f59e0b', '#10b981', '#16a34a', '#94a3b8'], formatCount(totalCount), '总任务', (value) => `${value} 项`),
        }),
        renderCard({
            accent: '#22c55e',
            eyebrow: 'Tasks',
            title: '完成节奏',
            subtitle: `${rangeLabel()}里的任务收口情况。`,
            body: renderSparkline(weekly.map((item) => item.week || ''), weekly.map((item) => Number(item.done || 0)), '#10b981', (value) => `${value} 项`),
            footer: renderMetricPairs([
                { label: `${rangeLabel()}新增`, value: formatCount(tasks.new_this_week || 0) },
                { label: '当前在办', value: formatCount(Number(totals.todo || 0) + Number(totals.in_progress || 0)) },
            ]),
        }),
        renderCard({
            accent: '#16a34a',
            eyebrow: 'Tasks',
            title: '推进堆叠图',
            subtitle: '把新增和收口放在同一条周节奏里看。',
            body: renderStackedColumns(
                weekly.map((item) => ({
                    label: String(item.week || '').replace(/^\d{4}-W/, 'W'),
                    done: Number(item.done || 0),
                    open: Math.max(0, Number(item.total || 0) - Number(item.done || 0)),
                })),
                'label',
                [
                    { key: 'done', color: '#16a34a' },
                    { key: 'open', color: '#bbf7d0' },
                ],
                (value) => `${value}`,
            ),
            footer: `
                <div class="stats-chip-row">
                    <span class="stats-chip">深绿 = 已完成</span>
                    <span class="stats-chip">浅绿 = 新增待消化</span>
                </div>
            `,
        }),
        renderCard({
            accent: '#34d399',
            eyebrow: 'Tasks',
            title: '优先级分布',
            subtitle: '高优事项是否堆积。',
            body: renderBarRows(
                safeArray(tasks.by_priority).map((item) => ({ ...item, label: `优先级 ${item.priority ?? '未设'}` })),
                'count',
                'label',
                '#10b981',
                (value) => `${value} 项`,
            ),
        }),
        renderCard({
            accent: '#059669',
            eyebrow: 'Tasks',
            title: '分类负载',
            subtitle: '当前任务主要压在哪些分类。',
            body: renderColumnChart(
                safeArray(tasks.by_category).slice(0, 8).map((item) => ({ label: item.category || '未分类', count: item.count })),
                'count',
                'label',
                '#059669',
                (value) => `${value}`,
            ),
        }),
    ].join('');
}

function eventCards() {
    const events = _data?.events || {};
    const weekly = safeArray(events.weekly);
    const totalEvents = sumBy(weekly, 'count');
    const slotLabels = ['06-09', '09-12', '12-14', '14-18', '18-21', '21-24'];
    const weekdayLabels = ['周一', '周二', '周三', '周四', '周五', '周六', '周日'];
    return [
        renderCard({
            accent: '#6366f1',
            eyebrow: 'Events',
            title: '日程密度',
            subtitle: `${rangeLabel()}里的安排变化。`,
            body: renderSparkline(weekly.map((item) => item.week || ''), weekly.map((item) => Number(item.count || 0)), '#6366f1', (value) => `${value} 个`),
            footer: renderMetricPairs([
                { label: '日程总量', value: formatCount(totalEvents) },
                { label: '分类数', value: formatCount(safeArray(events.by_category).length) },
            ]),
        }),
        renderCard({
            accent: '#8b5cf6',
            eyebrow: 'Events',
            title: '常见时段',
            subtitle: '一天里哪些时段最常被安排占据。',
            body: renderBarRows(safeArray(events.time_slots).map((item) => ({ ...item, label: item.slot })), 'count', 'label', '#8b5cf6', (value) => `${value} 个`),
        }),
        renderCard({
            accent: '#7c3aed',
            eyebrow: 'Events',
            title: '周内时段热力图',
            subtitle: '看看安排更容易扎堆在哪几天、哪几个时段。',
            body: renderMatrixHeatmap(safeArray(events.weekday_slots), slotLabels, weekdayLabels, '124, 58, 237'),
        }),
        renderCard({
            accent: '#4f46e5',
            eyebrow: 'Events',
            title: '日程分类',
            subtitle: '当前范围内的主题分布。',
            body: renderDonut(
                safeArray(events.by_category).slice(0, 6).map((item) => ({ ...item, category: item.category || '未分类' })),
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
    const categories = safeArray(notes.categories);
    const hotTags = safeArray(notes.hot_tags).slice(0, 10).map((item) => ({ label: `#${item.tag}`, value: item.count }));
    return [
        renderCard({
            accent: '#3b82f6',
            eyebrow: 'Notes',
            title: '笔记节奏',
            subtitle: '最近两周的输入频率。',
            body: renderHeatStrip(safeArray(notes.cadence).map((item) => ({ label: item.label, count: item.count })), 'count', 'label', '59, 130, 246'),
            footer: renderMetricPairs([
                { label: '累计笔记', value: formatCount(summary.total_count || 0) },
                { label: '近 7 天新增', value: formatCount(summary.week_new_count || 0) },
            ]),
        }),
        renderCard({
            accent: '#2563eb',
            eyebrow: 'Notes',
            title: '笔记分类',
            subtitle: '知识沉淀主要集中在哪些主题。',
            body: renderBarRows(categories.map((item) => ({ ...item, category: item.category || '未分类' })), 'count', 'category', '#3b82f6', (value) => `${value} 条`),
        }),
        renderCard({
            accent: '#0ea5e9',
            eyebrow: 'Notes',
            title: '高频标签',
            subtitle: '最近常出现的关键词。',
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
    return [
        renderCard({
            accent: '#ec4899',
            eyebrow: 'Diary',
            title: '心情分布',
            subtitle: '本月常见情绪落点。',
            body: renderDonut(safeArray(diary.mood_breakdown).slice(0, 6), 'count', 'mood', ['#ec4899', '#f472b6', '#fb7185', '#f9a8d4', '#db2777', '#be185d'], formatCount(summary.entry_count || 0), '本月篇数', (value) => `${value} 天`),
        }),
        renderCard({
            accent: '#f43f5e',
            eyebrow: 'Diary',
            title: '书写密度',
            subtitle: '这个月每天的记录频率。',
            body: renderHeatStrip(safeArray(diary.cadence).map((item) => ({ label: item.label, count: item.count })), 'count', 'label', '236, 72, 153'),
            footer: renderMetricPairs([
                { label: '连续天数', value: formatCount(summary.current_streak || 0) },
                { label: '填充率', value: formatPercent(summary.fill_rate || 0) },
            ]),
        }),
        renderCard({
            accent: '#e11d48',
            eyebrow: 'Diary',
            title: '模板与回看',
            subtitle: '本月常用模板和记录节奏。',
            body: renderBarRows(safeArray(diary.template_usage).slice(0, 5).map((item) => ({ ...item, template_id: item.template_id || '手写' })), 'count', 'template_id', '#ec4899', (value) => `${value} 次`),
            footer: renderMetricPairs([
                { label: '最长连续', value: formatCount(summary.longest_streak || 0) },
                { label: '总字数', value: formatCount(summary.total_words || 0) },
            ]),
        }),
    ].join('');
}

function renderSummary() {
    const ledger = _data?.ledger || {};
    const tasks = _data?.tasks || {};
    const events = _data?.events || {};
    const notes = _data?.notes || {};
    const diary = _data?.diary || {};
    const totals = tasks.totals || {};
    const totalEvents = sumBy(events.weekly, 'count');
    const expenseTotal = sumBy(ledger.expense_by_category, 'total');
    const incomeTotal = sumBy(ledger.income_by_category, 'total');
    const rangeTitle = _range === 'custom' ? '自定义范围' : rangeLabel();
    return `
        <section class="stats-summary-grid">
            <article class="stats-summary-card"><div class="stats-summary-label">${rangeTitle}支出</div><div class="stats-summary-value">${formatMoney(expenseTotal)}</div><div class="stats-summary-meta">收入 ${formatMoney(incomeTotal)}</div></article>
            <article class="stats-summary-card"><div class="stats-summary-label">${rangeTitle}结余</div><div class="stats-summary-value">${formatMoney(incomeTotal - expenseTotal)}</div><div class="stats-summary-meta">按当前范围计算</div></article>
            <article class="stats-summary-card"><div class="stats-summary-label">${rangeTitle}任务</div><div class="stats-summary-value">${formatCount(Number(totals.todo || 0) + Number(totals.in_progress || 0) + Number(totals.done || 0))}</div><div class="stats-summary-meta">完成 ${formatCount(totals.done || 0)} 项</div></article>
            <article class="stats-summary-card"><div class="stats-summary-label">${rangeTitle}日程</div><div class="stats-summary-value">${formatCount(totalEvents)}</div><div class="stats-summary-meta">分类 ${formatCount(safeArray(events.by_category).length)} 种</div></article>
            <article class="stats-summary-card"><div class="stats-summary-label">笔记</div><div class="stats-summary-value">${formatCount(notes.summary?.total_count || 0)}</div><div class="stats-summary-meta">近 7 天新增 ${formatCount(notes.summary?.week_new_count || 0)}</div></article>
            <article class="stats-summary-card"><div class="stats-summary-label">日记</div><div class="stats-summary-value">${formatCount(diary.summary?.entry_count || 0)}</div><div class="stats-summary-meta">连续 ${formatCount(diary.summary?.current_streak || 0)} 天</div></article>
        </section>
    `;
}

function renderWall() {
    return `
        <section class="stats-wall">
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
    if (_loading && !_data) {
        _container.innerHTML = `<div class="stats-shell"><div class="stats-empty-card">正在加载统计视图...</div></div>`;
        return;
    }
    const range = deriveRangeDates();
    _container.innerHTML = `
        <div class="stats-shell">
            <div class="stats-stack">
                <section class="stats-hero">
                    <div>
                        <h2>📊 统计</h2>
                        <p>按时间范围查看财务、任务、日程、笔记和日记的整体变化。</p>
                        <div class="stats-hero-tags">
                            <span class="stats-chip">${rangeLabel()}</span>
                            <span class="stats-chip">${range.start} → ${range.end}</span>
                            <span class="stats-chip">按模块查看</span>
                            <span class="stats-chip">笔记与日记同步更新</span>
                        </div>
                    </div>
                    <div class="stats-chip-row">
                        ${RANGE_OPTIONS.map((option) => `<button type="button" class="stats-range-btn${_range === option.key ? ' active' : ''}" data-range="${option.key}">${option.label}</button>`).join('')}
                    </div>
                    ${_range === 'custom' ? `
                        <div class="stats-custom-range">
                            <input type="text" class="stats-date-field" id="stats-custom-start" inputmode="numeric" placeholder="YYYY-MM-DD" value="${escapeHtml(_customDraftStart || _customStart || range.start)}">
                            <span class="stats-range-sep">至</span>
                            <input type="text" class="stats-date-field" id="stats-custom-end" inputmode="numeric" placeholder="YYYY-MM-DD" value="${escapeHtml(_customDraftEnd || _customEnd || range.end)}">
                            <button type="button" class="stats-range-btn stats-apply-btn" id="stats-custom-apply">应用</button>
                        </div>
                    ` : ''}
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
    _container.querySelectorAll('.stats-range-btn[data-range]').forEach((button) => {
        button.onclick = async () => {
            const nextRange = button.dataset.range || 'month';
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
    const customStart = _container.querySelector('#stats-custom-start');
    const customEnd = _container.querySelector('#stats-custom-end');
    const customApply = _container.querySelector('#stats-custom-apply');
    const applyCustomRange = async () => {
        const nextStart = customStart?.value.trim() || '';
        const nextEnd = customEnd?.value.trim() || '';
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
        _customStart = nextStart;
        _customEnd = nextEnd;
        await loadAndRender();
    };
    if (customStart) {
        customStart.oninput = () => {
            _customDraftStart = customStart.value;
        };
        customStart.onkeydown = async (event) => {
            if (event.key === 'Enter') {
                event.preventDefault();
                await applyCustomRange();
            }
        };
    }
    if (customEnd) {
        customEnd.oninput = () => {
            _customDraftEnd = customEnd.value;
        };
        customEnd.onkeydown = async (event) => {
            if (event.key === 'Enter') {
                event.preventDefault();
                await applyCustomRange();
            }
        };
    }
    if (customApply) customApply.onclick = applyCustomRange;
}

async function loadAndRender() {
    _loading = true;
    renderPage();
    try {
        _data = await fetchAllData();
    } catch (err) {
        _data = null;
        showToast(`加载统计失败：${err.message}`, 'error');
    } finally {
        _loading = false;
    }
    renderPage();
}

export function render(container) {
    _container = container;
    _range = 'month';
    _loading = false;
    _data = null;
    const initialRange = deriveRangeDates();
    _customStart = initialRange.start;
    _customEnd = initialRange.end;
    _customDraftStart = _customStart;
    _customDraftEnd = _customEnd;
    renderPage();
    loadAndRender();
}

export function destroy() {
    _container = null;
    _data = null;
}

export function onRouteEnter(_params) {}
