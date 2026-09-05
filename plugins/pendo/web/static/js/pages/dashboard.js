import { api } from '../api.js';
import { showToast } from '../components/toast.js';
import {
    errorMessage,
    finiteNumber,
    formatAmount,
    formatMoneyCompact,
    isRecord,
    isoDate,
    nonNegativeInteger,
    pad2,
    parseDate,
    records,
} from '../utils/format.js';
import {
    formatZonedTime,
    zonedDateKey,
    zonedDateParts,
    zonedInstantEpoch,
} from '../utils/timezone.js';
import { BREAKPOINTS, escapeHtml, injectStyles, mediaMax, pageShellCss, subscribeDataChanges } from '../utils/ui.js';

const CSS_ID          = 'pendo-dashboard-styles';
const PRIORITY_LABELS = Object.freeze({
    1: '紧急',
    2: '高优先',
    3: '中优先',
    4: '低优先',
    5: '最低',
});

let _container              = null;
let _unsubscribeDataChanges = null;
let _loadVersion            = 0;

// 页面样式集中注入一次，后续数据刷新只替换内容区域。
function ensureStyles() {
    injectStyles(
        CSS_ID,
        `
        ${pageShellCss('dashboard-page', { padding: '26px 24px 32px', compactPadding: '20px 16px 28px', compactBreakpoint: BREAKPOINTS.MOBILE })}
        .dashboard-hero {
            display: grid; grid-template-columns: minmax(0, 1.2fr) auto; gap: 18px; align-items: end;
            padding: 22px 24px; margin-bottom: 18px; border-radius: 24px;
            background:
                radial-gradient(circle at top right, rgba(99,102,241,0.16), transparent 34%),
                radial-gradient(circle at bottom left, rgba(16,185,129,0.12), transparent 28%),
                linear-gradient(145deg, rgba(255,255,255,0.95), rgba(248,250,252,0.92));
            border: 1px solid rgba(99,102,241,0.14);
            box-shadow: 0 18px 42px rgba(15,23,42,0.05);
        }
        .dashboard-hero h2 { margin: 0; font-size: 26px; font-weight: 800; color: var(--color-dashboard); letter-spacing: -0.02em; }
        .dashboard-hero p { margin: 8px 0 0; font-size: 14px; line-height: 1.7; color: var(--color-text-secondary); max-width: 640px; }
        .dashboard-hero-tags { display: flex; flex-wrap: wrap; gap: 8px; justify-content: flex-end; }
        .dashboard-hero-tag {
            padding: 8px 12px; border-radius: 999px; background: rgba(255,255,255,0.78);
            border: 1px solid rgba(148,163,184,0.18); color: var(--color-text-secondary);
            font-size: 12px; font-weight: 600; white-space: nowrap;
        }
        .dashboard-summary { display: grid; grid-template-columns: repeat(4, minmax(0, 1fr)); gap: 12px; margin-bottom: 18px; }
        .dashboard-summary-card {
            position: relative; overflow: hidden; padding: 16px 16px 14px; border-radius: 20px;
            border: 1px solid rgba(226,232,240,0.92);
            background: linear-gradient(180deg, rgba(255,255,255,0.98), rgba(248,250,252,0.94));
            box-shadow: 0 12px 28px rgba(15,23,42,0.04);
            min-width: 0;
        }
        .dashboard-summary-card::after {
            content: ''; position: absolute; inset: auto -18px -30px auto; width: 96px; height: 96px;
            border-radius: 50%; opacity: 0.18; background: currentColor; filter: blur(8px);
        }
        .dashboard-summary-card.events { color: #4F46E5; }
        .dashboard-summary-card.tasks { color: #0F766E; }
        .dashboard-summary-card.ledger { color: #DC2626; }
        .dashboard-summary-card.diary { color: #7C3AED; }
        .dashboard-summary-top { display: flex; align-items: center; justify-content: space-between; gap: 12px; margin-bottom: 18px; }
        .dashboard-summary-icon {
            width: 40px; height: 40px; border-radius: 14px; display: inline-flex; align-items: center; justify-content: center;
            font-size: 20px; background: rgba(255,255,255,0.72); border: 1px solid rgba(255,255,255,0.7);
            box-shadow: inset 0 1px 0 rgba(255,255,255,0.6);
        }
        .dashboard-summary-pill {
            padding: 5px 9px; border-radius: 999px; font-size: 11px; font-weight: 700;
            background: rgba(255,255,255,0.76); color: currentColor;
        }
        .dashboard-summary-value {
            font-size: clamp(22px, 1.8vw, 28px); line-height: 1.04; font-weight: 800; letter-spacing: -0.03em; color: var(--color-text);
            overflow-wrap: anywhere; word-break: break-word;
        }
        .dashboard-summary-label { margin-top: 7px; font-size: 13px; font-weight: 600; color: var(--color-text); overflow-wrap: anywhere; word-break: break-word; }
        .dashboard-summary-meta { margin-top: 6px; font-size: 12px; color: var(--color-text-secondary); overflow-wrap: anywhere; word-break: break-word; }
        .dashboard-grid { display: grid; grid-template-columns: minmax(0, 1.12fr) minmax(320px, 0.88fr); gap: 16px; align-items: start; }
        .dashboard-column { display: flex; flex-direction: column; gap: 16px; min-width: 0; }
        .dashboard-panel {
            background: linear-gradient(180deg, rgba(255,255,255,0.98), rgba(248,250,252,0.94));
            border: 1px solid rgba(226,232,240,0.92); border-radius: 22px;
            box-shadow: 0 16px 34px rgba(15,23,42,0.04); overflow: hidden;
        }
        .dashboard-panel-header { display: flex; align-items: center; justify-content: space-between; gap: 12px; padding: 18px 20px 0; }
        .dashboard-panel-header h3 { margin: 0; font-size: 18px; font-weight: 750; color: var(--color-text); letter-spacing: -0.02em; }
        .dashboard-panel-title.events { color: var(--color-events); }
        .dashboard-panel-title.tasks { color: var(--color-tasks); }
        .dashboard-panel-title.ledger { color: var(--color-ledger); }
        .dashboard-panel-header p { margin: 6px 0 0; font-size: 13px; color: var(--color-text-secondary); }
        .dashboard-link { color: var(--color-text-secondary); font-size: 12px; font-weight: 600; text-decoration: none; }
        .dashboard-link:hover { color: var(--color-text); }
        .dashboard-panel-body { padding: 16px 20px 20px; }
        .dashboard-agenda-grid { display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 12px; }
        .dashboard-agenda-section-label { display: inline-flex; align-items: center; gap: 6px; margin-bottom: 10px; font-size: 12px; font-weight: 700; color: var(--color-text-secondary); }
        .dashboard-agenda-list { display: flex; flex-direction: column; gap: 10px; }
        .dashboard-event-card {
            display: grid; grid-template-columns: 54px minmax(0, 1fr); gap: 12px; padding: 12px;
            border-radius: 16px; background: rgba(255,255,255,0.82); border: 1px solid rgba(226,232,240,0.92);
        }
        .dashboard-event-date {
            display: flex; flex-direction: column; justify-content: center; align-items: center;
            border-radius: 14px; background: rgba(79,70,229,0.08); color: #4338CA; font-weight: 700; min-height: 58px;
        }
        .dashboard-event-date strong { font-size: 18px; line-height: 1; }
        .dashboard-event-date span { font-size: 11px; margin-top: 4px; }
        .dashboard-event-title { font-size: 14px; font-weight: 650; color: var(--color-text); line-height: 1.4; }
        .dashboard-event-subtitle { margin-top: 5px; font-size: 12px; font-weight: 700; color: #6366F1; }
        .dashboard-event-meta { margin-top: 6px; display: flex; flex-wrap: wrap; gap: 6px; font-size: 12px; color: var(--color-text-secondary); min-width: 0; }
        .dashboard-meta-pill {
            display: inline-flex; align-items: center; gap: 4px; padding: 5px 8px; border-radius: 999px; background: rgba(148,163,184,0.08);
            max-width: 100%;
        }
        .dashboard-meta-pill.location { white-space: normal; line-height: 1.45; align-items: flex-start; overflow-wrap: anywhere; }
        .dashboard-task-sections { display: grid; grid-template-columns: minmax(0, 1.1fr) minmax(220px, 0.9fr); gap: 14px; }
        .dashboard-task-section { padding: 14px; border-radius: 18px; background: rgba(255,255,255,0.78); border: 1px solid rgba(226,232,240,0.9); }
        .dashboard-task-section-head { display: flex; align-items: center; justify-content: space-between; gap: 10px; margin-bottom: 10px; }
        .dashboard-task-section-head strong { font-size: 13px; color: var(--color-text); }
        .dashboard-task-count { font-size: 11px; font-weight: 700; color: var(--color-text-secondary); padding: 4px 8px; border-radius: 999px; background: rgba(148,163,184,0.08); }
        .dashboard-task-list { display: flex; flex-direction: column; gap: 8px; }
        .dashboard-task-item {
            display: grid; grid-template-columns: auto minmax(0, 1fr); gap: 10px; align-items: start;
            padding: 10px 12px; border-radius: 14px; border: 1px solid rgba(226,232,240,0.88);
            background: linear-gradient(180deg, rgba(248,250,252,0.94), rgba(255,255,255,0.98));
        }
        .dashboard-task-item.is-completed { opacity: 0.72; background: rgba(248,250,252,0.85); }
        .dashboard-task-item input[type="checkbox"] { width: 16px; height: 16px; margin-top: 3px; cursor: pointer; }
        .dashboard-task-complete-dot { width: 16px; height: 16px; margin-top: 4px; border-radius: 999px; background: rgba(16,185,129,0.16); }
        .dashboard-task-title { font-size: 14px; font-weight: 600; color: var(--color-text); line-height: 1.4; }
        .dashboard-task-title.is-done { text-decoration: line-through; }
        .dashboard-task-meta { display: flex; flex-wrap: wrap; gap: 6px; margin-top: 6px; font-size: 11px; color: var(--color-text-secondary); }
        .dashboard-finance-top { display: grid; grid-template-columns: repeat(3, minmax(0, 1fr)); gap: 10px; margin-bottom: 14px; }
        .dashboard-finance-metric { padding: 12px; border-radius: 16px; background: rgba(255,255,255,0.82); border: 1px solid rgba(226,232,240,0.88); min-width: 0; }
        .dashboard-finance-metric span { font-size: 12px; color: var(--color-text-secondary); }
        .dashboard-finance-metric strong {
            display: block; margin-top: 8px; font-size: clamp(18px, 1.9vw, 22px); line-height: 1.1; font-weight: 800; letter-spacing: -0.03em;
            overflow-wrap: anywhere;
        }
        .dashboard-chart-card {
            display: block; padding: 14px; border-radius: 18px; color: inherit; text-decoration: none;
            background: linear-gradient(180deg, rgba(254,242,242,0.92), rgba(255,255,255,0.98));
            border: 1px solid rgba(239,68,68,0.12); cursor: pointer;
        }
        .dashboard-chart-card:focus-visible { outline: 2px solid var(--color-ledger); outline-offset: 3px; }
        .dashboard-chart-head { display: flex; align-items: center; justify-content: space-between; gap: 10px; margin-bottom: 10px; }
        .dashboard-chart-head strong { font-size: 14px; color: var(--color-ledger); }
        .dashboard-chart-head span { font-size: 11px; color: var(--color-text-secondary); }
        .dashboard-chart-container {
            display: grid; grid-template-columns: 48px minmax(0, 1fr); grid-template-rows: minmax(0, 1fr) 22px;
            height: 180px; min-width: 0;
        }
        .dashboard-chart-y-axis, .dashboard-chart-x-axis, .dashboard-chart-plot { position: relative; min-width: 0; }
        .dashboard-chart-y-axis { grid-column: 1; grid-row: 1; }
        .dashboard-chart-y-label {
            position: absolute; right: 8px; transform: translateY(50%); font-size: 10px; color: #94A3B8; white-space: nowrap;
        }
        .dashboard-chart-plot { grid-column: 2; grid-row: 1; }
        .dashboard-spending-chart { position: absolute; inset: 0; width: 100%; height: 100%; overflow: visible; }
        .dashboard-chart-grid { stroke: rgba(239,68,68,0.12); stroke-width: 1; stroke-dasharray: 4 6; vector-effect: non-scaling-stroke; }
        .dashboard-chart-area { fill: rgba(239,68,68,0.12); }
        .dashboard-chart-line { fill: none; stroke: #DC2626; stroke-width: 2.4; stroke-linecap: round; stroke-linejoin: round; vector-effect: non-scaling-stroke; }
        .dashboard-chart-point {
            position: absolute; width: 6px; height: 6px; transform: translate(-50%, -50%); border: 2px solid #DC2626;
            border-radius: 999px; background: #fff; box-sizing: border-box;
        }
        .dashboard-chart-x-axis { grid-column: 2; grid-row: 2; }
        .dashboard-chart-x-label {
            position: absolute; top: 5px; transform: translateX(-50%); font-size: 10px; color: #94A3B8; white-space: nowrap;
        }
        .dashboard-ledger-list { display: flex; flex-direction: column; gap: 8px; margin-top: 14px; }
        .dashboard-ledger-item {
            display: grid; grid-template-columns: minmax(0, 1fr) auto; gap: 10px; align-items: center;
            padding: 10px 12px; border-radius: 14px; background: rgba(255,255,255,0.86); border: 1px solid rgba(226,232,240,0.86);
        }
        .dashboard-ledger-item strong { display: block; font-size: 13px; color: var(--color-text); }
        .dashboard-ledger-item span { display: block; margin-top: 4px; font-size: 11px; color: var(--color-text-secondary); }
        .dashboard-ledger-amount { font-size: 14px; font-weight: 800; text-align: right; }
        .dashboard-ledger-amount.income, .dashboard-finance-value.income, .dashboard-finance-value.positive { color: var(--color-success); }
        .dashboard-ledger-amount.expense, .dashboard-finance-value.expense, .dashboard-finance-value.negative { color: var(--color-ledger); }
        .dashboard-ledger-amount.transfer { color: var(--color-text); }
        .dashboard-ledger-empty { margin-top: 14px; }
        .dashboard-diary-panel { display: grid; grid-template-columns: 1fr auto; gap: 14px; align-items: center; padding: 18px 20px; }
        .dashboard-diary-copy strong { display: block; font-size: 16px; color: var(--color-text); }
        .dashboard-diary-copy p { margin: 8px 0 0; font-size: 13px; line-height: 1.7; color: var(--color-text-secondary); }
        .dashboard-diary-link { margin-top: 12px; }
        .dashboard-diary-stat {
            min-width: 132px; padding: 12px 14px; border-radius: 18px; text-align: center;
            background: linear-gradient(180deg, rgba(124,58,237,0.10), rgba(255,255,255,0.9));
            border: 1px solid rgba(124,58,237,0.14);
        }
        .dashboard-diary-stat strong {
            display: block; font-size: clamp(22px, 1.8vw, 28px); line-height: 1.04; color: #7C3AED; letter-spacing: -0.03em;
            overflow-wrap: anywhere; word-break: break-word;
        }
        .dashboard-diary-stat span { display: block; margin-top: 6px; font-size: 12px; color: var(--color-text-secondary); overflow-wrap: anywhere; word-break: break-word; }
        .dashboard-empty {
            padding: 28px 16px; border-radius: 18px; background: rgba(248,250,252,0.86);
            border: 1px dashed rgba(148,163,184,0.28); text-align: center;
        }
        .dashboard-empty strong { display: block; font-size: 14px; color: var(--color-text); }
        .dashboard-empty p { margin: 8px 0 0; font-size: 12px; line-height: 1.7; color: var(--color-text-secondary); }
        ${mediaMax(
            BREAKPOINTS.XL,
            `
            .dashboard-summary { grid-template-columns: repeat(2, minmax(0, 1fr)); }
            .dashboard-grid { grid-template-columns: 1fr; }
            .dashboard-finance-top { grid-template-columns: repeat(3, minmax(0, 1fr)); }
            .dashboard-finance-metric strong { font-size: clamp(16px, 2.3vw, 20px); }
            .dashboard-event-card { grid-template-columns: 50px minmax(0, 1fr); gap: 10px; }
            .dashboard-event-title { font-size: 13px; }
            .dashboard-meta-pill.location { width: 100%; border-radius: 14px; }
        `,
        )}
        ${mediaMax(
            BREAKPOINTS.MOBILE,
            `
            .dashboard-hero { grid-template-columns: 1fr; }
            .dashboard-hero-tags { justify-content: flex-start; }
            .dashboard-summary { grid-template-columns: 1fr; gap: 10px; }
            .dashboard-summary-card { padding: 14px 14px 12px; border-radius: 18px; }
            .dashboard-summary-card::after { width: 72px; height: 72px; inset: auto -12px -24px auto; }
            .dashboard-summary-top { margin-bottom: 12px; }
            .dashboard-summary-icon { width: 34px; height: 34px; border-radius: 12px; font-size: 18px; }
            .dashboard-summary-pill { padding: 4px 8px; font-size: 10px; }
            .dashboard-summary-value { font-size: 24px; }
            .dashboard-summary-label { margin-top: 5px; font-size: 12px; }
            .dashboard-summary-meta { margin-top: 4px; font-size: 11px; }
            .dashboard-agenda-grid, .dashboard-task-sections, .dashboard-finance-top { grid-template-columns: 1fr; }
            .dashboard-diary-panel { grid-template-columns: 1fr; }
            .dashboard-event-meta { gap: 5px; }
            .dashboard-meta-pill { font-size: 11px; }
        `,
        )}
    `,
    );
}

// ---------- 接口边界与展示格式 ----------

function formatTime(value) {
    return formatZonedTime(value, '--:--');
}

function formatMonthDayParts(value) {
    const parts = zonedDateParts(value);
    return parts ? { month: `${parts.month}月`, day: pad2(parts.day) } : { month: '--', day: '--' };
}

function formatDateOnly(value) {
    return zonedDateKey(value) || '未知日期';
}

/** 支出坐标只接受有限正数，并始终保留零点和至少一个有效上界。 */
function buildSpendingAxisTicks(values) {
    const maxValue = Array.isArray(values)
        ? values.reduce((maximum, value) => Math.max(maximum, finiteNumber(value), 1), 1)
        : 1;
    const ticks = [0, 0.33, 0.66, 1].map((ratio) => Math.round(maxValue * ratio));
    return Array.from(new Set(ticks)).sort((a, b) => a - b);
}

/** 在唯一的接口边界收敛损坏或缺失字段，后续渲染只处理稳定结构。 */
function normalizeDashboardData(payload) {
    const data         = isRecord(payload) ? payload : {};
    const summary      = isRecord(data.summary) ? data.summary : {};
    const taskBuckets  = isRecord(data.tasks) ? data.tasks : {};
    const monthSummary = isRecord(data.month_summary) ? data.month_summary : {};
    const eventsMonth  = records(data.events_month);

    return {
        summary: {
            events_month: nonNegativeInteger(summary.events_month),
            tasks_pending: nonNegativeInteger(summary.tasks_pending),
            tasks_done_recent: nonNegativeInteger(summary.tasks_done_recent),
            ledger_month_expense: Math.max(0, finiteNumber(summary.ledger_month_expense)),
            diary_month: nonNegativeInteger(summary.diary_month),
        },
        eventsAgenda: Array.isArray(data.events_agenda) ? records(data.events_agenda) : eventsMonth,
        tasks: {
            active: records(taskBuckets.active),
            completed: records(taskBuckets.completed),
        },
        spendingTrend: records(data.spending_trend),
        monthSummary: {
            byCurrency: isRecord(data.ledger_by_currency) ? data.ledger_by_currency : {},
            income: Math.max(0, finiteNumber(monthSummary.income)),
            expense: Math.max(0, finiteNumber(monthSummary.expense)),
            balance: finiteNumber(monthSummary.balance),
        },
        recentLedger: records(data.recent_ledger),
    };
}

// ---------- 看板各区域的纯 HTML 渲染 ----------

function buildHero(summary, monthSummary) {
    return `
        <section class="dashboard-hero">
            <div>
                <h2>📊 概览</h2>
                <p>集中查看最近的安排、任务和资金变化。</p>
            </div>
            <div class="dashboard-hero-tags">
                <span class="dashboard-hero-tag">本月 ${summary.events_month} 场日程</span>
                <span class="dashboard-hero-tag">${summary.tasks_pending} 项进行中任务</span>
                <span class="dashboard-hero-tag">支出 ${formatMoneyCompact(monthSummary.expense)}</span>
            </div>
        </section>`;
}

function renderSummaryCards(summary) {
    const cards = [
        {
            tone: 'events',
            icon: '📅',
            badge: '月视图',
            value: String(summary.events_month),
            label: '本月日程',
            meta: '包含已安排和即将到来的事项',
        },
        {
            tone: 'tasks',
            icon: '✅',
            badge: '进度',
            value: String(summary.tasks_pending),
            label: '进行中任务',
            meta: `最近完成 ${summary.tasks_done_recent} 项`,
        },
        {
            tone: 'ledger',
            icon: '💸',
            badge: '财务',
            value: formatMoneyCompact(summary.ledger_month_expense),
            label: '本月支出',
            meta: '按当前自然月累计',
        },
        {
            tone: 'diary',
            icon: '📖',
            badge: '记录',
            value: String(summary.diary_month),
            label: '近 30 天日记',
            meta: '保留最近生活轨迹',
        },
    ];
    return `
        <section class="dashboard-summary">
            ${cards
                .map(
                    (card) => `
                <article class="dashboard-summary-card ${card.tone}">
                    <div class="dashboard-summary-top">
                        <span class="dashboard-summary-icon">${card.icon}</span>
                        <span class="dashboard-summary-pill">${card.badge}</span>
                    </div>
                    <div class="dashboard-summary-value">${card.value}</div>
                    <div class="dashboard-summary-label">${card.label}</div>
                    <div class="dashboard-summary-meta">${card.meta}</div>
                </article>
            `,
                )
                .join('')}
        </section>`;
}

function renderEventSection(title, items, emptyTitle, emptyText) {
    if (!items.length) {
        return `
            <div>
                <div class="dashboard-agenda-section-label">${title}</div>
                <div class="dashboard-empty">
                    <strong>${emptyTitle}</strong>
                    <p>${emptyText}</p>
                </div>
            </div>`;
    }

    return `
        <div>
            <div class="dashboard-agenda-section-label">${title}</div>
            <div class="dashboard-agenda-list">
                ${items
                    .map((event) => {
                        const date = formatMonthDayParts(event.start_time);
                        const isMultiNode = event.entry_kind === 'multi_node' && event.display_subtitle;
                        const heading = isMultiNode
                            ? `${event.display_title || event.title || '(无标题)'} · ${event.display_subtitle}`
                            : event.display_title || event.title || '(无标题)';
                        return `
                        <article class="dashboard-event-card">
                            <div class="dashboard-event-date">
                                <strong>${escapeHtml(date.day)}</strong>
                                <span>${escapeHtml(date.month)}</span>
                            </div>
                            <div>
                                <div class="dashboard-event-title">${escapeHtml(heading)}</div>
                                ${event.display_subtitle && !isMultiNode ? `<div class="dashboard-event-subtitle">${escapeHtml(event.display_subtitle)}</div>` : ''}
                                <div class="dashboard-event-meta">
                                    <span class="dashboard-meta-pill">🕒 ${formatTime(event.start_time)}${event.end_time ? ` - ${formatTime(event.end_time)}` : ''}</span>
                                    ${event.location ? `<span class="dashboard-meta-pill location">📍 ${escapeHtml(event.location)}</span>` : ''}
                                </div>
                            </div>
                        </article>`;
                    })
                    .join('')}
            </div>
        </div>`;
}

function renderMonthlyAgenda(events) {
    const now         = Date.now();
    const timedEvents = events
        .map((event) => {
            const start = Number.isFinite(event.start_epoch_ms)
                ? event.start_epoch_ms
                : zonedInstantEpoch(event.start_time);
            const end = Number.isFinite(event.end_epoch_ms)
                ? event.end_epoch_ms
                : event.end_time
                  ? zonedInstantEpoch(event.end_time)
                  : start;
            return Number.isFinite(start) && Number.isFinite(end) ? { event, start, end } : null;
        })
        .filter((entry) => entry !== null)
        .sort((left, right) => left.start - right.start);
    const upcoming = timedEvents
        .filter(({ end }) => end >= now)
        .slice(0, 4)
        .map(({ event }) => event);
    const past = timedEvents
        .filter(({ end }) => end < now)
        .slice(-4)
        .reverse()
        .map(({ event }) => event);

    return `
        <section class="dashboard-panel">
            <div class="dashboard-panel-header">
                <div>
                    <h3 class="dashboard-panel-title events">📅 本月日程</h3>
                    <p>查看这个月的重要安排。</p>
                </div>
                <a class="dashboard-link" href="#/events">查看全部 →</a>
            </div>
            <div class="dashboard-panel-body">
                <div class="dashboard-agenda-grid">
                    ${renderEventSection('即将到来', upcoming, '接下来暂无安排', '这个月后续没有新的日程，可以安心处理手头任务。')}
                    ${renderEventSection('最近经过', past, '本月还没有经过的事项', '新建一条日程后，这里会逐步形成月内轨迹。')}
                </div>
            </div>
        </section>`;
}

function renderActiveTaskItem(task) {
    const taskId   = typeof task.id === 'string' ? task.id.trim() : '';
    const schedule = task.deadline_at
        ? `截止 ${formatDateOnly(task.deadline_at)} ${formatTime(task.deadline_at)}`
        : task.plan_date
          ? `计划 ${formatDateOnly(task.plan_date)}`
          : '未安排日期';
    const taskTitle = task.title || '(无标题)';
    const priority = PRIORITY_LABELS[task.priority] || '未设优先级';
    const taskIdAttribute = taskId ? `data-task-id="${escapeHtml(taskId)}"` : 'disabled aria-disabled="true"';
    return `
        <label class="dashboard-task-item">
            <input type="checkbox" ${taskIdAttribute} aria-label="完成待办：${escapeHtml(taskTitle)}">
            <div>
                <div class="dashboard-task-title">${escapeHtml(taskTitle)}</div>
                <div class="dashboard-task-meta">
                    <span class="dashboard-meta-pill">待办</span>
                    <span class="dashboard-meta-pill">${priority}</span>
                    ${task.category ? `<span class="dashboard-meta-pill">${escapeHtml(task.category)}</span>` : ''}
                    <span class="dashboard-meta-pill">${escapeHtml(schedule)}</span>
                </div>
            </div>
        </label>`;
}

function renderCompletedTaskItem(task) {
    const completedAt = task.completed_at || task.updated_at || '';
    return `
        <article class="dashboard-task-item is-completed">
            <span class="dashboard-task-complete-dot" aria-hidden="true"></span>
            <div>
                <div class="dashboard-task-title is-done">${escapeHtml(task.title || '(无标题)')}</div>
                <div class="dashboard-task-meta">
                    <span class="dashboard-meta-pill">已完成</span>
                    ${completedAt ? `<span class="dashboard-meta-pill">${formatDateOnly(completedAt)} ${formatTime(completedAt)}</span>` : ''}
                </div>
            </div>
        </article>`;
}

function renderTasksPanel({ active, completed }) {
    return `
        <section class="dashboard-panel">
            <div class="dashboard-panel-header">
                <div>
                    <h3 class="dashboard-panel-title tasks">✅ 任务进度</h3>
                    <p>同时查看进行中的任务和最近完成项。</p>
                </div>
                <a class="dashboard-link" href="#/tasks">查看全部 →</a>
            </div>
            <div class="dashboard-panel-body">
                <div class="dashboard-task-sections">
                    <section class="dashboard-task-section">
                        <div class="dashboard-task-section-head">
                            <strong>待办 / 进行中</strong>
                            <span class="dashboard-task-count">${active.length} 项</span>
                        </div>
                        <div class="dashboard-task-list" id="dashboard-active-tasks">
                            ${active.length ? active.map(renderActiveTaskItem).join('') : `<div class="dashboard-empty"><strong>当前没有待办任务</strong><p>可以回到待办页新建，或者先把最近完成的任务复盘一下。</p></div>`}
                        </div>
                    </section>
                    <section class="dashboard-task-section">
                        <div class="dashboard-task-section-head">
                            <strong>最近完成</strong>
                            <span class="dashboard-task-count">${completed.length} 项</span>
                        </div>
                        <div class="dashboard-task-list">
                            ${completed.length ? completed.map(renderCompletedTaskItem).join('') : `<div class="dashboard-empty"><strong>还没有完成记录</strong><p>完成一项任务后，这里会展示最近的收尾节奏。</p></div>`}
                        </div>
                    </section>
                </div>
            </div>
        </section>`;
}

function renderSpendingChart(spendingTrend) {
    const trend = records(spendingTrend);
    if (!trend.length) return '';

    const values    = trend.map((point) => Math.max(0, finiteNumber(point.amount)));
    const axisTicks = buildSpendingAxisTicks(values);
    const axisMax   = axisTicks[axisTicks.length - 1] || 1;
    const lastIndex = trend.length - 1;
    const points    = trend.map((point, index) => {
        const x     = lastIndex ? (index / lastIndex) * 100 : 50;
        const value = values[index];
        const y     = 100 - (value / axisMax) * 100;
        const date  = parseDate(point.date);
        const label = date ? `${pad2(date.getMonth() + 1)}/${pad2(date.getDate())}` : '未知日期';
        return { x, y, value, label };
    });
    const linePoints = points.map((point) => `${point.x.toFixed(2)},${point.y.toFixed(2)}`).join(' ');
    const areaPoints = `${points[0].x.toFixed(2)},100 ${linePoints} ${points.at(-1).x.toFixed(2)},100`;
    const labelCount = Math.min(5, points.length);
    const labelIndexes =
        labelCount === 1
            ? [0]
            : Array.from({ length: labelCount }, (_, index) => Math.round((index * lastIndex) / (labelCount - 1)));
    const peak = Math.max(...values, 0);

    return `
        <div class="dashboard-chart-container" role="img" aria-label="${escapeHtml(`本月支出走势，共 ${points.length} 个日期，最高单日 ${formatAmount(peak)}`)}">
            <div class="dashboard-chart-y-axis" aria-hidden="true">
                ${axisTicks.map((tick) => `<span class="dashboard-chart-y-label" style="bottom:${((tick / axisMax) * 100).toFixed(2)}%;">${escapeHtml(formatMoneyCompact(tick))}</span>`).join('')}
            </div>
            <div class="dashboard-chart-plot">
                <svg class="dashboard-spending-chart" viewBox="0 0 100 100" preserveAspectRatio="none" aria-hidden="true" focusable="false">
                    ${axisTicks.map((tick) => `<line class="dashboard-chart-grid" x1="0" x2="100" y1="${(100 - (tick / axisMax) * 100).toFixed(2)}" y2="${(100 - (tick / axisMax) * 100).toFixed(2)}"></line>`).join('')}
                    <polygon class="dashboard-chart-area" points="${areaPoints}"></polygon>
                    <polyline class="dashboard-chart-line" points="${linePoints}"></polyline>
                </svg>
                ${points.map((point) => `<span class="dashboard-chart-point" style="left:${point.x.toFixed(2)}%;top:${point.y.toFixed(2)}%;" title="${escapeHtml(`${point.label} ${formatAmount(point.value)}`)}" aria-hidden="true"></span>`).join('')}
            </div>
            <div class="dashboard-chart-x-axis" aria-hidden="true">
                ${labelIndexes.map((index) => `<span class="dashboard-chart-x-label" style="left:${points[index].x.toFixed(2)}%;">${escapeHtml(points[index].label)}</span>`).join('')}
            </div>
        </div>`;
}

function renderRecentLedger(recentLedger) {
    if (!recentLedger.length) {
        return `<div class="dashboard-empty dashboard-ledger-empty"><strong>本月还没有账目变化</strong><p>新增一笔收入或支出后，这里会显示最近的资金流动。</p></div>`;
    }

    return `
        <div class="dashboard-ledger-list">
            ${recentLedger
                .slice(0, 5)
                .map((item) => {
                    const transactionType = ['income', 'transfer'].includes(item.transaction_type)
                        ? item.transaction_type
                        : 'expense';
                    const prefix = transactionType === 'transfer' ? '' : transactionType === 'income' ? '+' : '-';
                    const amount = `${prefix}${formatAmount(Math.abs(finiteNumber(item.amount)), item.currency)}`;
                    return `
                    <article class="dashboard-ledger-item">
                        <div>
                            <strong>${escapeHtml(item.title || '(无摘要)')}</strong>
                            <span>${escapeHtml(item.ledger_category || '未分类')} · ${formatDateOnly(item.ledger_date)}</span>
                        </div>
                        <div class="dashboard-ledger-amount ${transactionType}">${amount}</div>
                    </article>`;
                })
                .join('')}
        </div>`;
}

function renderFinancePanel(spendingTrend, monthSummary, recentLedger) {
    const balance = finiteNumber(monthSummary.balance);
    return `
        <section class="dashboard-panel">
            <div class="dashboard-panel-header">
                <div>
                    <h3 class="dashboard-panel-title ledger">💰 本月财务</h3>
                    <p>人民币走势与各币种独立汇总。</p>
                </div>
                <a class="dashboard-link" href="#/ledger">查看账本 →</a>
            </div>
            <div class="dashboard-panel-body">
                <div class="dashboard-finance-top">
                    <div class="dashboard-finance-metric">
                        <span>本月收入</span>
                        <strong class="dashboard-finance-value income">${formatAmount(monthSummary.income)}</strong>
                    </div>
                    <div class="dashboard-finance-metric">
                        <span>本月支出</span>
                        <strong class="dashboard-finance-value expense">${formatAmount(monthSummary.expense)}</strong>
                    </div>
                    <div class="dashboard-finance-metric">
                        <span>当前结余</span>
                        <strong class="dashboard-finance-value ${balance >= 0 ? 'positive' : 'negative'}">${formatAmount(balance)}</strong>
                    </div>
                </div>
                ${Object.entries(monthSummary.byCurrency || {}).filter(([code]) => code !== 'CNY').map(([code, totals]) => `<p>${escapeHtml(code)} · 收入 ${formatAmount(totals.income, code)} · 支出 ${formatAmount(totals.expense, code)} · 结余 ${formatAmount(totals.balance, code)}</p>`).join('')}
                <a class="dashboard-chart-card" href="#/stats">
                    <div class="dashboard-chart-head">
                        <strong>支出走势</strong>
                        <span>查看详细统计</span>
                    </div>
                    ${
                        spendingTrend.length
                            ? renderSpendingChart(spendingTrend)
                            : `<div class="dashboard-empty"><strong>暂无支出走势</strong><p>本月出现支出后，这里会逐步形成趋势线。</p></div>`
                    }
                </a>
                ${renderRecentLedger(recentLedger)}
            </div>
        </section>`;
}

function renderDiaryPanel(summary) {
    return `
        <section class="dashboard-panel">
            <div class="dashboard-diary-panel">
                <div class="dashboard-diary-copy">
                    <strong>📖 日记记录</strong>
                    <p>近 30 天共写了 ${summary.diary_month} 篇。</p>
                    <div class="dashboard-diary-link">
                        <a class="dashboard-link" href="#/diary">去写日记 →</a>
                    </div>
                </div>
                <div class="dashboard-diary-stat">
                    <strong>${summary.diary_month}</strong>
                    <span>近 30 天记录</span>
                </div>
            </div>
        </section>`;
}

function buildDashboardMarkup(payload) {
    const { summary, eventsAgenda, tasks, spendingTrend, monthSummary, recentLedger } = normalizeDashboardData(payload);

    return `
        <div class="dashboard-page">
            ${buildHero(summary, monthSummary)}
            ${renderSummaryCards(summary)}
            <div class="dashboard-grid">
                <div class="dashboard-column">
                    ${renderMonthlyAgenda(eventsAgenda)}
                    ${renderTasksPanel(tasks)}
                </div>
                <div class="dashboard-column">
                    ${renderFinancePanel(spendingTrend, monthSummary, recentLedger)}
                    ${renderDiaryPanel(summary)}
                </div>
            </div>
        </div>
    `;
}

/**
 * 拉取并刷新看板。
 *
 * 每次请求持有独立版本；路由销毁或后续刷新会让旧响应失效，避免慢响应覆盖新页面。
 */
async function loadAndRender() {
    const container = _container;
    if (!container) return;

    const loadVersion = ++_loadVersion;
    container.innerHTML = `<div class="empty-state"><p>加载中...</p></div>`;

    try {
        const response = await api.get('/dashboard');
        if (_container !== container || loadVersion !== _loadVersion) return;

        container.innerHTML = buildDashboardMarkup(response?.data);
        const activeTasksElement = container.querySelector('#dashboard-active-tasks');
        activeTasksElement?.addEventListener('change', async (event) => {
            const checkbox = event.target;
            if (!checkbox?.matches?.('input[type="checkbox"][data-task-id]') || checkbox.disabled) return;

            const taskId = checkbox.dataset.taskId;
            if (!taskId) return;
            checkbox.disabled = true;
            try {
                await api.put(`/items/${encodeURIComponent(taskId)}`, { status: 'done' });
                if (_container !== container) return;
                showToast('任务已完成 ✅', 'success');
                await loadAndRender();
            } catch (error) {
                if (_container !== container) return;
                showToast(`标记失败：${errorMessage(error)}`, 'error');
                checkbox.checked = false;
                checkbox.disabled = false;
            }
        });
    } catch (error) {
        if (_container !== container || loadVersion !== _loadVersion) return;
        container.innerHTML = `<div class="empty-state"><p>加载失败：${escapeHtml(errorMessage(error))}</p></div>`;
    }
}

export function render(container) {
    if (!container || typeof container.querySelector !== 'function') {
        throw new TypeError('看板需要有效的 DOM 挂载容器');
    }

    _unsubscribeDataChanges?.();
    _loadVersion += 1;
    _container = container;
    ensureStyles();
    _unsubscribeDataChanges = subscribeDataChanges(null, () => {
        void loadAndRender();
    });
    void loadAndRender();
}

export function destroy() {
    _loadVersion += 1;
    _unsubscribeDataChanges?.();
    _unsubscribeDataChanges = null;
    _container = null;
}
