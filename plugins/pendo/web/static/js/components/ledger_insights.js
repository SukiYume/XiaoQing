/** Pendo Web 账目洞察面板的数据规范化和原生 SVG 图表。 */

import { arrayValue as asArray, formatAmount, formatMoneyCompact } from '../utils/format.js';
import { escapeHtml } from '../utils/ui.js';

const FOCUS_COPY = Object.freeze({
    expense: Object.freeze({ label: '支出', verb: '消费' }),
    income: Object.freeze({ label: '收入', verb: '入账' }),
    transfer: Object.freeze({ label: '转账', verb: '流转' }),
});

const CHART_PALETTE = Object.freeze(['#E15241', '#F48C58', '#F8B36D', '#F2C14E', '#D97757', '#D9DDE5']);

function toFiniteNumber(value, fallback = 0) {
    if (value == null || value === '') return fallback;
    try {
        const number = Number(value);
        return Number.isFinite(number) ? number : fallback;
    } catch {
        return fallback;
    }
}

function toNonNegativeNumber(value, fallback = 0) {
    return Math.max(0, toFiniteNumber(value, fallback));
}

function toCount(value) {
    return Math.max(0, Math.trunc(toFiniteNumber(value)));
}

function formatPercent(value, focusLabel) {
    if (value == null) return '无对比';
    const number = toFiniteNumber(value, null);
    if (number == null) return '无对比';
    if (number === 1) return `首次有${focusLabel}`;

    const percent = Math.abs(number) * 100;
    if (!Number.isFinite(percent)) return '无对比';
    return `${number >= 0 ? '+' : '-'}${percent.toFixed(percent >= 10 ? 0 : 1)}%`;
}

function pickEvenAxisIndexes(length, maxLabels) {
    const safeLength = Math.max(0, Math.trunc(toFiniteNumber(length)));
    const labelCount = Math.min(safeLength, Math.max(0, Math.trunc(toFiniteNumber(maxLabels))));
    if (labelCount === 0) return [];
    if (labelCount === 1) return [0];
    if (labelCount === safeLength) {
        return Array.from({ length: safeLength }, (_, index) => index);
    }

    // 按整段比例取样，始终保留首尾标签；旧的向上取整步长会漏掉末尾。
    return Array.from({ length: labelCount }, (_, index) => Math.round((index * (safeLength - 1)) / (labelCount - 1)));
}

function formatAxisLabel(key) {
    const text = String(key ?? '');
    if (!/^\d{4}-\d{2}-\d{2}$/.test(text)) return text;
    const [, month, day] = text.split('-');
    return `${Number(month)}/${Number(day)}`;
}

function normalizeCategories(value) {
    return asArray(value)
        .map((item) => ({
            category: String(item?.category ?? '').trim() || '未分类',
            total: toNonNegativeNumber(item?.total),
            count: toCount(item?.count),
        }))
        .filter((item) => item.total > 0);
}

function normalizeCandle(item, index) {
    const open  = toNonNegativeNumber(item?.open);
    const close = toNonNegativeNumber(item?.close);
    const high  = Math.max(toNonNegativeNumber(item?.high, Math.max(open, close)), open, close);
    const low   = Math.min(toNonNegativeNumber(item?.low, Math.min(open, close)), open, close);
    const key   = String(item?.key ?? '').trim() || `candle-${index}`;
    return {
        key,
        label: String(item?.label ?? '').trim() || formatAxisLabel(key),
        open,
        close,
        high,
        low,
    };
}

function renderEmptyCard(title, subtitle, body) {
    return `
        <section class="ledger-insight-card">
            <div class="ledger-insight-card-head">
                <div>
                    <h3>${escapeHtml(title)}</h3>
                    <p>${escapeHtml(subtitle)}</p>
                </div>
            </div>
            <div class="ledger-insight-empty">${escapeHtml(body)}</div>
        </section>`;
}

function buildTrendSvg(points, currency) {
    const width       = 500;
    const height      = 200;
    const pad         = { top: 18, right: 18, bottom: 30, left: 48 };
    const maxValue    = Math.max(...points.map((point) => point.total), 1);
    const innerWidth  = width - pad.left - pad.right;
    const innerHeight = height - pad.top - pad.bottom;
    const stepX       = points.length > 1 ? innerWidth / (points.length - 1) : 0;
    const ticks       = [1, 0.66, 0.33, 0].map((ratio) => ({
        value: maxValue * ratio,
        y: pad.top + innerHeight - innerHeight * ratio,
    }));
    const coords = points.map((point, index) => ({
        ...point,
        x: pad.left + stepX * index,
        y: pad.top + innerHeight - (point.total / maxValue) * innerHeight,
    }));

    const linePath = coords
        .map((point, index) => `${index === 0 ? 'M' : 'L'} ${point.x.toFixed(2)} ${point.y.toFixed(2)}`)
        .join(' ');
    const baselineY = (pad.top + innerHeight).toFixed(2);
    const areaPath = `${linePath} L ${(pad.left + innerWidth).toFixed(2)} ${baselineY} L ${pad.left} ${baselineY} Z`;

    const gridLines = ticks
        .map(
            ({ y }) =>
                `<line x1="${pad.left}" y1="${y.toFixed(2)}" x2="${(pad.left + innerWidth).toFixed(2)}" y2="${y.toFixed(2)}" stroke="rgba(239,68,68,0.10)" stroke-dasharray="4 6"/>`,
        )
        .join('');
    const yLabels = ticks
        .map(
            ({ value, y }, index) => `
                <g>
                    ${
                        index === ticks.length - 1
                            ? `<line x1="${pad.left}" y1="${y.toFixed(2)}" x2="${pad.left}" y2="${y.toFixed(2)}" stroke="rgba(148,163,184,0.55)"/>`
                            : ''
                    }
                    <text class="ledger-insight-y-label" x="${pad.left - 8}" y="${(y + 4).toFixed(2)}" text-anchor="end">${escapeHtml(formatMoneyCompact(value, currency))}</text>
                </g>`,
        )
        .join('');
    const pointsHtml = coords
        .map(
            (point) => `
                <g>
                    <title>${escapeHtml(point.label)} ${escapeHtml(formatAmount(point.total, currency))} · ${point.count} 笔</title>
                    <circle cx="${point.x.toFixed(2)}" cy="${point.y.toFixed(2)}" r="2.7" fill="#fff" stroke="#E15241" stroke-width="1.45"/>
                </g>`,
        )
        .join('');

    const labelIndexes = new Set(pickEvenAxisIndexes(coords.length, 5));
    const labels       = coords
        .map((point, index) => {
            if (!labelIndexes.has(index)) return '';
            return `<text x="${point.x.toFixed(2)}" y="${height - 6}" text-anchor="middle">${escapeHtml(point.label)}</text>`;
        })
        .join('');

    return `
        <svg class="ledger-insight-svg ledger-insight-svg-trend" viewBox="0 0 ${width} ${height}" aria-hidden="true">
            <defs>
                <linearGradient id="ledgerTrendArea" x1="0" x2="0" y1="0" y2="1">
                    <stop offset="0%" stop-color="rgba(225,82,65,0.30)"/>
                    <stop offset="100%" stop-color="rgba(225,82,65,0.02)"/>
                </linearGradient>
            </defs>
            ${gridLines}
            ${yLabels}
            <path d="${areaPath}" fill="url(#ledgerTrendArea)"></path>
            <path d="${linePath}" fill="none" stroke="#E15241" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"></path>
            ${pointsHtml}
            <g class="ledger-insight-axis-labels">${labels}</g>
        </svg>`;
}

function polarToCartesian(cx, cy, radius, angle) {
    const radians = ((angle - 90) * Math.PI) / 180;
    return {
        x: cx + radius * Math.cos(radians),
        y: cy + radius * Math.sin(radians),
    };
}

function arcPath(cx, cy, outerRadius, innerRadius, startAngle, endAngle) {
    const startOuter = polarToCartesian(cx, cy, outerRadius, endAngle);
    const endOuter   = polarToCartesian(cx, cy, outerRadius, startAngle);
    const startInner = polarToCartesian(cx, cy, innerRadius, endAngle);
    const endInner   = polarToCartesian(cx, cy, innerRadius, startAngle);
    const largeArc   = endAngle - startAngle > 180 ? 1 : 0;

    return [
        `M ${startOuter.x} ${startOuter.y}`,
        `A ${outerRadius} ${outerRadius} 0 ${largeArc} 0 ${endOuter.x} ${endOuter.y}`,
        `L ${endInner.x} ${endInner.y}`,
        `A ${innerRadius} ${innerRadius} 0 ${largeArc} 1 ${startInner.x} ${startInner.y}`,
        'Z',
    ].join(' ');
}

// 起止点重合时单个 SVG 圆弧不可见，完整圆环必须拆成两个半圆。
function fullRingPath(cx, cy, outerRadius, innerRadius) {
    return [
        `M ${cx} ${cy - outerRadius}`,
        `A ${outerRadius} ${outerRadius} 0 1 1 ${cx} ${cy + outerRadius}`,
        `A ${outerRadius} ${outerRadius} 0 1 1 ${cx} ${cy - outerRadius}`,
        `M ${cx} ${cy - innerRadius}`,
        `A ${innerRadius} ${innerRadius} 0 1 0 ${cx} ${cy + innerRadius}`,
        `A ${innerRadius} ${innerRadius} 0 1 0 ${cx} ${cy - innerRadius}`,
        'Z',
    ].join(' ');
}

function buildRingSvg(categories, total, centerLabel, currency) {
    const width          = 220;
    const height         = 220;
    const cx             = 110;
    const cy             = 110;
    const outerRadius    = 84;
    const innerRadius    = 56;
    const topCategories  = categories.slice(0, 5);
    const remainingTotal = categories.slice(5).reduce((sum, item) => sum + item.total, 0);
    const segments       = [...topCategories];
    if (remainingTotal > 0) {
        segments.push({ category: '其他', total: remainingTotal, count: 0 });
    }

    const segmentTotal = segments.reduce((sum, item) => sum + item.total, 0);
    const displayTotal = Math.max(toNonNegativeNumber(total), segmentTotal);
    let startAngle     = 0;
    const arcs         = segments
        .map((item, index) => {
            const share = displayTotal ? item.total / displayTotal : 0;
            const slice = Math.min(share * 360, 360);
            if (slice <= 0) return '';

            const endAngle = startAngle + slice;
            const path     = slice >= 359.999
                    ? fullRingPath(cx, cy, outerRadius, innerRadius)
                    : arcPath(cx, cy, outerRadius, innerRadius, startAngle, endAngle);
            startAngle = endAngle;
            return `
                <path d="${path}" fill="${CHART_PALETTE[index % CHART_PALETTE.length]}" fill-rule="evenodd" stroke="#ffffff" stroke-width="1.5">
                    <title>${escapeHtml(item.category)} ${escapeHtml(formatAmount(item.total, currency))} · ${(share * 100).toFixed(1)}%</title>
                </path>`;
        })
        .join('');

    const legend = segments
        .map(
            (item, index) => `
                <div class="ledger-insight-legend-item">
                    <span class="ledger-insight-legend-dot" style="background:${CHART_PALETTE[index % CHART_PALETTE.length]};"></span>
                    <span class="ledger-insight-legend-name">${escapeHtml(item.category)}</span>
                    <span class="ledger-insight-legend-value">${escapeHtml(formatMoneyCompact(item.total, currency))}</span>
                </div>`,
        )
        .join('');

    return `
        <div class="ledger-insight-ring-wrap">
            <svg class="ledger-insight-svg ledger-insight-svg-ring" viewBox="0 0 ${width} ${height}" aria-hidden="true">
                <circle cx="${cx}" cy="${cy}" r="${outerRadius}" fill="rgba(225,82,65,0.06)"></circle>
                ${arcs}
                <circle cx="${cx}" cy="${cy}" r="${innerRadius - 1}" fill="#fff"></circle>
                <text x="${cx}" y="${cy - 4}" text-anchor="middle" class="ledger-ring-center-value">${escapeHtml(formatMoneyCompact(displayTotal, currency))}</text>
                <text x="${cx}" y="${cy + 20}" text-anchor="middle" class="ledger-ring-center-label">${escapeHtml(centerLabel)}</text>
            </svg>
            <div class="ledger-insight-legend">${legend}</div>
        </div>`;
}

function buildHotspots(categories, total, currency) {
    const denominator = Math.max(toNonNegativeNumber(total), ...categories.map((item) => item.total));
    return `
        <div class="ledger-hotspot-list">
            ${categories
                .slice(0, 5)
                .map((item, index) => {
                    const share = denominator ? Math.min((item.total / denominator) * 100, 100) : 0;
                    const barWidth = share > 0 ? Math.max(share, 8) : 0;
                    return `
                        <div class="ledger-hotspot-row">
                            <div class="ledger-hotspot-row-head">
                                <span class="ledger-hotspot-rank">${String(index + 1).padStart(2, '0')}</span>
                                <span class="ledger-hotspot-name">${escapeHtml(item.category)}</span>
                                <span class="ledger-hotspot-amount">${escapeHtml(formatAmount(item.total, currency))}</span>
                            </div>
                            <div class="ledger-hotspot-track">
                                <div class="ledger-hotspot-fill" style="width:${barWidth.toFixed(2)}%;"></div>
                            </div>
                            <div class="ledger-hotspot-meta">${share.toFixed(1)}% · ${item.count} 笔</div>
                        </div>`;
                })
                .join('')}
        </div>`;
}

function buildCandleSvg(candles, rangeKeys, currency) {
    const width       = 500;
    const height      = 200;
    const pad         = { top: 18, right: 18, bottom: 28, left: 48 };
    const innerWidth  = width - pad.left - pad.right;
    const innerHeight = height - pad.top - pad.bottom;
    const maxValue    = Math.max(...candles.map((item) => item.high), 1);
    const minValue    = Math.min(...candles.map((item) => item.low), 0);
    const spread      = Math.max(maxValue - minValue, 1);

    // 时间线包含零支出日期；追加意外缺失的蜡烛键，避免坐标落到视图之外。
    const domainKeys = [
        ...new Set([
            ...asArray(rangeKeys)
                .map((key) => String(key ?? '').trim())
                .filter(Boolean),
            ...candles.map((item) => item.key),
        ]),
    ];
    const domainIndex = new Map(domainKeys.map((key, index) => [key, index]));
    const domainStep = domainKeys.length > 1 ? innerWidth / (domainKeys.length - 1) : innerWidth;
    const xForDomainIndex = (index) =>
        domainKeys.length <= 1 ? pad.left + innerWidth / 2 : pad.left + domainStep * index;
    const candleWidth = Math.max(4, Math.min(12, domainStep * 0.34));
    const ticks       = [1, 0.66, 0.33, 0].map((ratio) => ({
        value: minValue + spread * ratio,
        y: pad.top + innerHeight - innerHeight * ratio,
    }));

    const grid = ticks
        .map(
            ({ y }) =>
                `<line x1="${pad.left}" y1="${y.toFixed(2)}" x2="${(pad.left + innerWidth).toFixed(2)}" y2="${y.toFixed(2)}" stroke="rgba(15,23,42,0.06)"/>`,
        )
        .join('');
    const yLabels = ticks
        .map(
            ({ value, y }, index) => `
                <g>
                    ${
                        index === ticks.length - 1
                            ? `<line x1="${pad.left}" y1="${y.toFixed(2)}" x2="${pad.left}" y2="${y.toFixed(2)}" stroke="rgba(148,163,184,0.55)"/>`
                            : ''
                    }
                    <text class="ledger-insight-y-label" x="${pad.left - 8}" y="${(y + 4).toFixed(2)}" text-anchor="end">${escapeHtml(formatMoneyCompact(value, currency))}</text>
                </g>`,
        )
        .join('');
    const candlesHtml = candles
        .map((item) => {
            const x = xForDomainIndex(domainIndex.get(item.key));
            const highY = pad.top + innerHeight - ((item.high - minValue) / spread) * innerHeight;
            const lowY = pad.top + innerHeight - ((item.low - minValue) / spread) * innerHeight;
            const openY = pad.top + innerHeight - ((item.open - minValue) / spread) * innerHeight;
            const closeY = pad.top + innerHeight - ((item.close - minValue) / spread) * innerHeight;
            const bodyTop = Math.min(openY, closeY);
            const bodyHeight = Math.max(Math.abs(closeY - openY), 5);
            const isUp = item.close >= item.open;
            const fill = isUp ? '#F3A46C' : '#D9684C';
            const stroke = isUp ? '#E98A48' : '#C9573B';
            const wickStroke = isUp ? 'rgba(233,138,72,0.75)' : 'rgba(201,87,59,0.80)';
            return `
                <g>
                    <title>${escapeHtml(item.label)} 高:${escapeHtml(formatAmount(item.high, currency))} 低:${escapeHtml(formatAmount(item.low, currency))} 开:${escapeHtml(formatAmount(item.open, currency))} 收:${escapeHtml(formatAmount(item.close, currency))}</title>
                    <line x1="${x.toFixed(2)}" y1="${highY.toFixed(2)}" x2="${x.toFixed(2)}" y2="${lowY.toFixed(2)}" stroke="${wickStroke}" stroke-width="1.4" stroke-linecap="round"></line>
                    <rect x="${(x - candleWidth / 2).toFixed(2)}" y="${bodyTop.toFixed(2)}" width="${candleWidth.toFixed(2)}" height="${bodyHeight.toFixed(2)}" rx="${Math.min(3, candleWidth / 3).toFixed(2)}" fill="${fill}" stroke="${stroke}" stroke-width="0.8" opacity="0.96"></rect>
                </g>`;
        })
        .join('');

    const labelIndexes = new Set(pickEvenAxisIndexes(domainKeys.length, 5));
    const labels       = domainKeys
        .map((key, index) => {
            if (!labelIndexes.has(index)) return '';
            const x = xForDomainIndex(index);
            return `<text x="${x.toFixed(2)}" y="${height - 6}" text-anchor="middle">${escapeHtml(formatAxisLabel(key))}</text>`;
        })
        .join('');

    return `
        <svg class="ledger-insight-svg ledger-insight-svg-kline" viewBox="0 0 ${width} ${height}" aria-hidden="true">
            ${grid}
            ${yLabels}
            ${candlesHtml}
            <g class="ledger-insight-axis-labels">${labels}</g>
        </svg>`;
}

export function renderLedgerInsightsPanel(data) {
    const payload  = data && typeof data === 'object' && !Array.isArray(data) ? data : {};
    const currency = payload.currency || 'CNY';
    const summary  =         payload.summary && typeof payload.summary === 'object' && !Array.isArray(payload.summary)
            ? payload.summary
            : {};
    const focusTransactionType = ['income', 'expense', 'transfer'].includes(summary.focus_transaction_type)
        ? summary.focus_transaction_type
        : 'expense';
    const { label: focusLabel, verb: focusVerb } = FOCUS_COPY[focusTransactionType];
    const bucketLabel = summary.bucket_mode === 'month' ? '按月' : '按日';

    // 在单一入口完成类型收敛，后续 SVG 几何只处理可信的有限数值。
    const timeline = asArray(payload.expense_timeline).map((point, index) => {
        const key = String(point?.key ?? '').trim() || `point-${index}`;
        return {
            key,
            label: String(point?.label ?? '').trim() || formatAxisLabel(key),
            total: toNonNegativeNumber(point?.total),
            count: toCount(point?.count),
        };
    });
    const categories       = normalizeCategories(payload.expense_categories);
    const explicitHotspots = normalizeCategories(payload.expense_hotspots);
    const hotspots         = explicitHotspots.length ? explicitHotspots : categories.slice(0, 5);
    const candles          = asArray(payload.expense_candles).map(normalizeCandle);

    const focusTotal = toNonNegativeNumber(summary.focus_total);
    const averageFocusAmount = toNonNegativeNumber(summary.average_focus_amount);
    const peakLabel = String(summary.peak_bucket_label ?? '').trim() || '暂无峰值';
    const peakTotal = toNonNegativeNumber(summary.peak_bucket_total);
    const deltaValue = summary.delta_vs_previous == null ? null : toFiniteNumber(summary.delta_vs_previous, null);
    const deltaText = formatPercent(deltaValue, focusLabel);
    const deltaLabel = String(summary.delta_label ?? '').trim() || '较上一周期';
    const deltaClass = deltaValue == null ? '' : deltaValue >= 0 ? 'is-up' : 'is-down';

    const trendCard = timeline.some((point) => point.total > 0)
        ? `
            <section class="ledger-insight-card ledger-insight-card-pulse">
                <div class="ledger-insight-card-head">
                    <div>
                        <h3>${focusLabel}脉搏</h3>
                        <p>当前筛选范围内的${focusVerb}节奏</p>
                    </div>
                    <span class="ledger-insight-badge">${bucketLabel}</span>
                </div>
                <div class="ledger-pulse-metrics">
                    <div class="ledger-pulse-metric">
                        <span class="ledger-pulse-label">总${focusLabel}</span>
                        <strong>${escapeHtml(formatAmount(focusTotal, currency))}</strong>
                    </div>
                    <div class="ledger-pulse-metric">
                        <span class="ledger-pulse-label">单笔均值</span>
                        <strong>${escapeHtml(formatAmount(averageFocusAmount, currency))}</strong>
                    </div>
                    <div class="ledger-pulse-metric">
                        <span class="ledger-pulse-label">峰值时段</span>
                        <strong>${escapeHtml(peakLabel)}</strong>
                        <small>${escapeHtml(formatMoneyCompact(peakTotal, currency))}</small>
                    </div>
                    <div class="ledger-pulse-metric">
                        <span class="ledger-pulse-label">${escapeHtml(deltaLabel)}</span>
                        <strong class="${deltaClass}">${deltaText}</strong>
                    </div>
                </div>
                ${buildTrendSvg(timeline, currency)}
            </section>`
        : renderEmptyCard(
              `${focusLabel}脉搏`,
              `当前筛选结果里暂无${focusLabel}变化`,
              `调整筛选条件后，这里会显示${focusLabel}随时间的变化。`,
          );

    const ringCard = categories.length
        ? `
            <section class="ledger-insight-card ledger-insight-card-ring">
                <div class="ledger-insight-card-head">
                    <div>
                        <h3>分类构成</h3>
                        <p>${focusLabel}主要落在哪些类别</p>
                    </div>
                </div>
                ${buildRingSvg(categories, focusTotal, `${focusLabel}总额`, currency)}
            </section>`
        : renderEmptyCard(
              '分类构成',
              `当前筛选结果里暂无${focusLabel}分类`,
              `有${focusLabel}数据后，这里会显示分类占比。`,
          );

    const hotspotCard = hotspots.length
        ? `
            <section class="ledger-insight-card ledger-insight-card-hotspot">
                <div class="ledger-insight-card-head">
                    <div>
                        <h3>${focusLabel}热点</h3>
                        <p>本期${focusLabel}最高的类别排行</p>
                    </div>
                </div>
                ${buildHotspots(hotspots, focusTotal, currency)}
            </section>`
        : renderEmptyCard(
              `${focusLabel}热点`,
              '当前筛选结果里暂无排行',
              `当有${focusLabel}数据时，这里会显示热点排行。`,
          );

    const klineCard = candles.length
        ? `
            <section class="ledger-insight-card ledger-insight-card-kline">
                <div class="ledger-insight-card-head">
                    <div>
                        <h3>${focusLabel} K 线</h3>
                        <p>每个时段单笔${focusLabel}的开高低收</p>
                    </div>
                </div>
                ${buildCandleSvg(
                    candles,
                    timeline.map((item) => item.key),
                    currency,
                )}
            </section>`
        : renderEmptyCard(
              `${focusLabel} K 线`,
              `当前筛选结果里暂无足够的${focusLabel}波动`,
              `出现单笔${focusLabel}后，这里会显示类 K 线波动。`,
          );

    return `
        <section class="ledger-insights-panel">
            <div class="ledger-insights-main">
                ${trendCard}
                ${klineCard}
            </div>
            <div class="ledger-insights-side">
                ${ringCard}
                ${hotspotCard}
            </div>
        </section>`;
}
