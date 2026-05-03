import { formatAmount, formatMoneyCompact } from '../utils/format.js';
import { escapeHtml as esc } from '../utils/ui.js';

function fmtPercent(value) {
    if (value == null) return '无对比';
    if (value === 1) return '首次有支出';
    const pct = Math.abs(value * 100);
    return `${value >= 0 ? '+' : '-'}${pct.toFixed(pct >= 10 ? 0 : 1)}%`;
}

function pickEvenAxisIndexes(length, maxLabels) {
    if (length <= maxLabels) return Array.from({ length }, (_, index) => index);
    const safeMaxLabels = Math.max(maxLabels, 2);
    const step = Math.max(1, Math.ceil((length - 1) / (safeMaxLabels - 1)));
    const picked = [];
    for (let index = 0; index < length && picked.length < maxLabels; index += step) {
        picked.push(index);
    }
    return picked;
}

function formatAxisLabel(key) {
    const text = String(key || '');
    if (/^\d{4}-\d{2}-\d{2}$/.test(text)) {
        const [, month, day] = text.split('-');
        return `${Number(month)}/${Number(day)}`;
    }
    return text;
}

function emptyCard(title, subtitle, body) {
    return `
        <section class="ledger-insight-card">
            <div class="ledger-insight-card-head">
                <div>
                    <h3>${title}</h3>
                    <p>${subtitle}</p>
                </div>
            </div>
            <div class="ledger-insight-empty">${body}</div>
        </section>`;
}

function buildTrendSvg(points) {
    const width = 500;
    const height = 200;
    const pad = { top: 18, right: 18, bottom: 30, left: 48 };
    const values = points.map(point => Number(point.total || 0));
    const maxValue = Math.max(...values, 1);
    const innerWidth = width - pad.left - pad.right;
    const innerHeight = height - pad.top - pad.bottom;
    const stepX = points.length > 1 ? innerWidth / (points.length - 1) : 0;
    const ticks = [1, 0.66, 0.33, 0].map(ratio => ({
        value: maxValue * ratio,
        y: pad.top + innerHeight - innerHeight * ratio,
    }));

    const coords = points.map((point, index) => {
        const x = pad.left + stepX * index;
        const y = pad.top + innerHeight - (Number(point.total || 0) / maxValue) * innerHeight;
        return { ...point, x, y };
    });

    const linePath = coords.map((point, index) =>
        `${index === 0 ? 'M' : 'L'} ${point.x.toFixed(2)} ${point.y.toFixed(2)}`
    ).join(' ');
    const areaPath = `${linePath} L ${(pad.left + innerWidth).toFixed(2)} ${(pad.top + innerHeight).toFixed(2)} L ${pad.left} ${(pad.top + innerHeight).toFixed(2)} Z`;

    const gridLines = ticks.map(({ y }) => {
        return `<line x1="${pad.left}" y1="${y.toFixed(2)}" x2="${(pad.left + innerWidth).toFixed(2)}" y2="${y.toFixed(2)}" stroke="rgba(239,68,68,0.10)" stroke-dasharray="4 6"/>`;
    }).join('');

    const yLabels = ticks.map(({ value, y }, index) => `
        <g>
            ${index === ticks.length - 1
                ? `<line x1="${pad.left}" y1="${y.toFixed(2)}" x2="${pad.left}" y2="${y.toFixed(2)}" stroke="rgba(148,163,184,0.55)" />`
                : ''}
            <text class="ledger-insight-y-label" x="${pad.left - 8}" y="${(y + 4).toFixed(2)}" text-anchor="end">${esc(formatMoneyCompact(value))}</text>
        </g>
    `).join('');

    const pointsHtml = coords.map((point) => `
        <g>
            <title>${esc(point.label)} ${formatAmount(point.total || 0)} · ${point.count || 0} 笔</title>
            <circle cx="${point.x.toFixed(2)}" cy="${point.y.toFixed(2)}" r="2.7" fill="#fff" stroke="#E15241" stroke-width="1.45"/>
        </g>
    `).join('');

    const labelIndexes = pickEvenAxisIndexes(coords.length, 5);
    const labels = coords.map((point, index) => {
        if (!labelIndexes.includes(index)) return '';
        return `<text x="${point.x.toFixed(2)}" y="${height - 6}" text-anchor="middle">${esc(point.label)}</text>`;
    }).join('');

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

function polarToCartesian(cx, cy, r, angle) {
    const rad = (angle - 90) * Math.PI / 180;
    return { x: cx + r * Math.cos(rad), y: cy + r * Math.sin(rad) };
}

function arcPath(cx, cy, rOuter, rInner, startAngle, endAngle) {
    const startOuter = polarToCartesian(cx, cy, rOuter, endAngle);
    const endOuter = polarToCartesian(cx, cy, rOuter, startAngle);
    const startInner = polarToCartesian(cx, cy, rInner, endAngle);
    const endInner = polarToCartesian(cx, cy, rInner, startAngle);
    const largeArc = endAngle - startAngle > 180 ? 1 : 0;

    return [
        `M ${startOuter.x} ${startOuter.y}`,
        `A ${rOuter} ${rOuter} 0 ${largeArc} 0 ${endOuter.x} ${endOuter.y}`,
        `L ${endInner.x} ${endInner.y}`,
        `A ${rInner} ${rInner} 0 ${largeArc} 1 ${startInner.x} ${startInner.y}`,
        'Z',
    ].join(' ');
}

function fullRingPath(cx, cy, rOuter, rInner) {
    return [
        `M ${cx} ${cy - rOuter}`,
        `A ${rOuter} ${rOuter} 0 1 1 ${cx} ${cy + rOuter}`,
        `A ${rOuter} ${rOuter} 0 1 1 ${cx} ${cy - rOuter}`,
        `M ${cx} ${cy - rInner}`,
        `A ${rInner} ${rInner} 0 1 0 ${cx} ${cy + rInner}`,
        `A ${rInner} ${rInner} 0 1 0 ${cx} ${cy - rInner}`,
        'Z',
    ].join(' ');
}

function buildRingSvg(categories, total, centerLabel = '支出总额') {
    const palette = ['#E15241', '#F48C58', '#F8B36D', '#F2C14E', '#D97757', '#D9DDE5'];
    const width = 220;
    const height = 220;
    const cx = 110;
    const cy = 110;
    const outer = 84;
    const inner = 56;
    let startAngle = 0;

    const topCategories = categories.slice(0, 5);
    const remainingTotal = categories.slice(5).reduce((sum, item) => sum + Number(item.total || 0), 0);
    const segments = [...topCategories];
    if (remainingTotal > 0) {
        segments.push({ category: '其他', total: remainingTotal, share: remainingTotal / total, count: 0 });
    }

    const arcs = segments.map((item, index) => {
        const slice = total ? (Number(item.total || 0) / total) * 360 : 0;
        const endAngle = startAngle + slice;
        const path = slice >= 359.999
            ? fullRingPath(cx, cy, outer, inner)
            : arcPath(cx, cy, outer, inner, startAngle, endAngle);
        const html = `
            <path d="${path}" fill="${palette[index % palette.length]}" fill-rule="evenodd" stroke="#ffffff" stroke-width="1.5">
                <title>${esc(item.category)} ${formatAmount(item.total || 0)} · ${(Number(item.share || 0) * 100).toFixed(1)}%</title>
            </path>`;
        startAngle = endAngle;
        return html;
    }).join('');

    return `
        <div class="ledger-insight-ring-wrap">
            <svg class="ledger-insight-svg ledger-insight-svg-ring" viewBox="0 0 ${width} ${height}" aria-hidden="true">
                <circle cx="${cx}" cy="${cy}" r="${outer}" fill="rgba(225,82,65,0.06)"></circle>
                ${arcs}
                <circle cx="${cx}" cy="${cy}" r="${inner - 1}" fill="#fff"></circle>
                <text x="${cx}" y="${cy - 4}" text-anchor="middle" class="ledger-ring-center-value">${formatMoneyCompact(total)}</text>
                <text x="${cx}" y="${cy + 20}" text-anchor="middle" class="ledger-ring-center-label">${esc(centerLabel)}</text>
            </svg>
            <div class="ledger-insight-legend">
                ${segments.map((item, index) => `
                    <div class="ledger-insight-legend-item">
                        <span class="ledger-insight-legend-dot" style="background:${palette[index % palette.length]};"></span>
                        <span class="ledger-insight-legend-name">${esc(item.category)}</span>
                        <span class="ledger-insight-legend-value">${formatMoneyCompact(item.total || 0)}</span>
                    </div>
                `).join('')}
            </div>
        </div>`;
}

function buildHotspots(categories, total) {
    return `
        <div class="ledger-hotspot-list">
            ${categories.slice(0, 5).map((item, index) => {
                const pct = total ? Math.max((Number(item.total || 0) / total) * 100, 8) : 0;
                return `
                    <div class="ledger-hotspot-row">
                        <div class="ledger-hotspot-row-head">
                            <span class="ledger-hotspot-rank">0${index + 1}</span>
                            <span class="ledger-hotspot-name">${esc(item.category)}</span>
                            <span class="ledger-hotspot-amount">${formatAmount(item.total || 0)}</span>
                        </div>
                        <div class="ledger-hotspot-track">
                            <div class="ledger-hotspot-fill" style="width:${pct.toFixed(2)}%;"></div>
                        </div>
                        <div class="ledger-hotspot-meta">${(Number(item.share || 0) * 100).toFixed(1)}% · ${item.count || 0} 笔</div>
                    </div>`;
            }).join('')}
        </div>`;
}

function buildCandleSvg(candles, rangeKeys = []) {
    const width = 500;
    const height = 200;
    const pad = { top: 18, right: 18, bottom: 28, left: 48 };
    const innerWidth = width - pad.left - pad.right;
    const innerHeight = height - pad.top - pad.bottom;
    const highs = candles.map(item => Number(item.high || 0));
    const lows = candles.map(item => Number(item.low || 0));
    const maxValue = Math.max(...highs, 1);
    const minValue = Math.min(...lows, 0);
    const spread = Math.max(maxValue - minValue, 1);
    const domainKeys = Array.isArray(rangeKeys) && rangeKeys.length ? rangeKeys : candles.map((item) => item.key);
    const domainIndex = new Map(domainKeys.map((key, index) => [key, index]));
    const domainStep = domainKeys.length > 1 ? innerWidth / (domainKeys.length - 1) : innerWidth;
    const xForDomainIndex = (index) => {
        if (domainKeys.length <= 1) return pad.left + innerWidth / 2;
        return pad.left + domainStep * index;
    };
    const candleWidth = Math.max(4, Math.min(12, domainStep * 0.34));
    const ticks = [1, 0.66, 0.33, 0].map(ratio => ({
        value: minValue + spread * ratio,
        y: pad.top + innerHeight - innerHeight * ratio,
    }));

    const grid = ticks.map(({ y }) => {
        return `<line x1="${pad.left}" y1="${y.toFixed(2)}" x2="${(pad.left + innerWidth).toFixed(2)}" y2="${y.toFixed(2)}" stroke="rgba(15,23,42,0.06)"/>`;
    }).join('');

    const yLabels = ticks.map(({ value, y }, index) => `
        <g>
            ${index === ticks.length - 1
                ? `<line x1="${pad.left}" y1="${y.toFixed(2)}" x2="${pad.left}" y2="${y.toFixed(2)}" stroke="rgba(148,163,184,0.55)" />`
                : ''}
            <text class="ledger-insight-y-label" x="${pad.left - 8}" y="${(y + 4).toFixed(2)}" text-anchor="end">${esc(formatMoneyCompact(value))}</text>
        </g>
    `).join('');

    const candlesHtml = candles.map((item, index) => {
        const candleDomainIndex = domainIndex.get(item.key) ?? index;
        const x = xForDomainIndex(candleDomainIndex);
        const highY = pad.top + innerHeight - ((Number(item.high || 0) - minValue) / spread) * innerHeight;
        const lowY = pad.top + innerHeight - ((Number(item.low || 0) - minValue) / spread) * innerHeight;
        const openY = pad.top + innerHeight - ((Number(item.open || 0) - minValue) / spread) * innerHeight;
        const closeY = pad.top + innerHeight - ((Number(item.close || 0) - minValue) / spread) * innerHeight;
        const bodyTop = Math.min(openY, closeY);
        const bodyHeight = Math.max(Math.abs(closeY - openY), 5);
        const isUp = Number(item.close || 0) >= Number(item.open || 0);
        const fill = isUp ? '#F3A46C' : '#D9684C';
        const stroke = isUp ? '#E98A48' : '#C9573B';
        const wickStroke = isUp ? 'rgba(233,138,72,0.75)' : 'rgba(201,87,59,0.80)';
        return `
            <g>
                <title>${esc(item.label)} 高:${formatAmount(item.high || 0)} 低:${formatAmount(item.low || 0)} 开:${formatAmount(item.open || 0)} 收:${formatAmount(item.close || 0)}</title>
                <line x1="${x.toFixed(2)}" y1="${highY.toFixed(2)}" x2="${x.toFixed(2)}" y2="${lowY.toFixed(2)}" stroke="${wickStroke}" stroke-width="1.4" stroke-linecap="round"></line>
                <rect x="${(x - candleWidth / 2).toFixed(2)}" y="${bodyTop.toFixed(2)}" width="${candleWidth.toFixed(2)}" height="${bodyHeight.toFixed(2)}" rx="${Math.min(3, candleWidth / 3).toFixed(2)}" fill="${fill}" stroke="${stroke}" stroke-width="0.8" opacity="0.96"></rect>
            </g>`;
    }).join('');

    const labelIndexes = pickEvenAxisIndexes(domainKeys.length, 5);
    const labels = domainKeys.map((key, index) => {
        if (!labelIndexes.includes(index)) return '';
        const x = xForDomainIndex(index);
        return `<text x="${x.toFixed(2)}" y="${height - 6}" text-anchor="middle">${esc(formatAxisLabel(key))}</text>`;
    }).join('');

    return `
        <svg class="ledger-insight-svg ledger-insight-svg-kline" viewBox="0 0 ${width} ${height}" aria-hidden="true">
            ${grid}
            ${yLabels}
            ${candlesHtml}
            <g class="ledger-insight-axis-labels">${labels}</g>
        </svg>`;
}

export function renderLedgerInsightsPanel(data) {
    const summary = data?.summary || {};
    const timeline = data?.expense_timeline || [];
    const categories = data?.expense_categories || [];
    const hotspots = data?.expense_hotspots || [];
    const candles = data?.expense_candles || [];
    const focusTransactionType = ['income', 'expense', 'transfer'].includes(summary.focus_transaction_type)
        ? summary.focus_transaction_type : 'expense';
    const focusLabel = focusTransactionType === 'income' ? '收入' : (focusTransactionType === 'transfer' ? '转账' : '支出');
    const focusVerb = focusTransactionType === 'income' ? '入账' : (focusTransactionType === 'transfer' ? '流转' : '消费');
    const bucketLabel = summary.bucket_mode === 'month' ? '按月' : '按日';

    const focusTotal = Number(summary.focus_total || 0);
    const averageFocusAmount = Number(summary.average_focus_amount || 0);
    const peakLabel = summary.peak_bucket_label || '暂无峰值';
    const peakTotal = Number(summary.peak_bucket_total || 0);
    const deltaText = fmtPercent(summary.delta_vs_previous);
    const deltaLabel = summary.delta_label || '较上一周期';
    const deltaClass = summary.delta_vs_previous == null
        ? ''
        : (summary.delta_vs_previous >= 0 ? 'is-up' : 'is-down');

    const trendCard = timeline.some(point => Number(point.total || 0) > 0)
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
                        <strong>${formatAmount(focusTotal)}</strong>
                    </div>
                    <div class="ledger-pulse-metric">
                        <span class="ledger-pulse-label">单笔均值</span>
                        <strong>${formatAmount(averageFocusAmount)}</strong>
                    </div>
                    <div class="ledger-pulse-metric">
                        <span class="ledger-pulse-label">峰值时段</span>
                        <strong>${peakLabel}</strong>
                        <small>${formatMoneyCompact(peakTotal)}</small>
                    </div>
                    <div class="ledger-pulse-metric">
                        <span class="ledger-pulse-label">${deltaLabel}</span>
                        <strong class="${deltaClass}">${deltaText}</strong>
                    </div>
                </div>
                ${buildTrendSvg(timeline)}
            </section>`
        : emptyCard(`${focusLabel}脉搏`, `当前筛选结果里暂无${focusLabel}变化`, `调整筛选条件后，这里会显示${focusLabel}随时间的变化。`);

    const ringCard = categories.length
        ? `
            <section class="ledger-insight-card ledger-insight-card-ring">
                <div class="ledger-insight-card-head">
                    <div>
                        <h3>分类构成</h3>
                        <p>${focusLabel}主要落在哪些类别</p>
                    </div>
                </div>
                ${buildRingSvg(categories, focusTotal, `${focusLabel}总额`)}
            </section>`
        : emptyCard('分类构成', `当前筛选结果里暂无${focusLabel}分类`, `有${focusLabel}数据后，这里会显示分类占比。`);

    const hotspotCard = hotspots.length
        ? `
            <section class="ledger-insight-card ledger-insight-card-hotspot">
                <div class="ledger-insight-card-head">
                    <div>
                        <h3>${focusLabel}热点</h3>
                        <p>本期${focusLabel}最高的类别排行</p>
                    </div>
                </div>
                ${buildHotspots(hotspots, focusTotal)}
            </section>`
        : emptyCard(`${focusLabel}热点`, '当前筛选结果里暂无排行', `当有${focusLabel}数据时，这里会显示热点排行。`);

    const klineCard = candles.length
        ? `
            <section class="ledger-insight-card ledger-insight-card-kline">
                <div class="ledger-insight-card-head">
                    <div>
                        <h3>${focusLabel} K 线</h3>
                        <p>每个时段单笔${focusLabel}的开高低收</p>
                    </div>
                </div>
                ${buildCandleSvg(candles, timeline.map((item) => item.key))}
            </section>`
        : emptyCard(`${focusLabel} K 线`, `当前筛选结果里暂无足够的${focusLabel}波动`, `出现单笔${focusLabel}后，这里会显示类 K 线波动。`);

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
