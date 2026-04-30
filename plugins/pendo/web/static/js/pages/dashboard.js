import { api } from '../api.js';
import { showToast } from '../components/toast.js';
import { navigate } from '../router.js';
import { loadChart } from '../lib/chart-loader.js';
import { BREAKPOINTS, escapeHtml, injectStyles, mediaMax, pageShellCss } from '../utils/ui.js';

const CSS_ID = 'pendo-dashboard-styles';

let _container = null;
let _chartInstance = null;
let _dataChangedHandler = null;

function ensureStyles() {
    injectStyles(CSS_ID, `
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
        .dashboard-chart-card { padding: 14px; border-radius: 18px; background: linear-gradient(180deg, rgba(254,242,242,0.92), rgba(255,255,255,0.98)); border: 1px solid rgba(239,68,68,0.12); cursor: pointer; }
        .dashboard-chart-head { display: flex; align-items: center; justify-content: space-between; gap: 10px; margin-bottom: 10px; }
        .dashboard-chart-head strong { font-size: 14px; color: var(--color-ledger); }
        .dashboard-chart-head span { font-size: 11px; color: var(--color-text-secondary); }
        .dashboard-chart-container { height: 180px; }
        .dashboard-ledger-list { display: flex; flex-direction: column; gap: 8px; margin-top: 14px; }
        .dashboard-ledger-item {
            display: grid; grid-template-columns: minmax(0, 1fr) auto; gap: 10px; align-items: center;
            padding: 10px 12px; border-radius: 14px; background: rgba(255,255,255,0.86); border: 1px solid rgba(226,232,240,0.86);
        }
        .dashboard-ledger-item strong { display: block; font-size: 13px; color: var(--color-text); }
        .dashboard-ledger-item span { display: block; margin-top: 4px; font-size: 11px; color: var(--color-text-secondary); }
        .dashboard-ledger-amount { font-size: 14px; font-weight: 800; text-align: right; }
        .dashboard-diary-panel { display: grid; grid-template-columns: 1fr auto; gap: 14px; align-items: center; padding: 18px 20px; }
        .dashboard-diary-copy strong { display: block; font-size: 16px; color: var(--color-text); }
        .dashboard-diary-copy p { margin: 8px 0 0; font-size: 13px; line-height: 1.7; color: var(--color-text-secondary); }
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
        ${mediaMax(BREAKPOINTS.XL, `
            .dashboard-summary { grid-template-columns: repeat(2, minmax(0, 1fr)); }
            .dashboard-grid { grid-template-columns: 1fr; }
            .dashboard-finance-top { grid-template-columns: repeat(3, minmax(0, 1fr)); }
            .dashboard-finance-metric strong { font-size: clamp(16px, 2.3vw, 20px); }
            .dashboard-event-card { grid-template-columns: 50px minmax(0, 1fr); gap: 10px; }
            .dashboard-event-title { font-size: 13px; }
            .dashboard-meta-pill.location { width: 100%; border-radius: 14px; }
        `)}
        ${mediaMax(BREAKPOINTS.MOBILE, `
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
        `)}
    `);
}

function formatTime(iso) {
    if (!iso) return '';
    const d = new Date(iso);
    return `${String(d.getHours()).padStart(2, '0')}:${String(d.getMinutes()).padStart(2, '0')}`;
}

function formatMonthDay(iso) {
    if (!iso) return { month: '--', day: '--' };
    const d = new Date(iso);
    return { month: `${d.getMonth() + 1}月`, day: String(d.getDate()).padStart(2, '0') };
}

function formatDate(iso) {
    if (!iso) return '';
    return iso.slice(0, 10);
}

function formatAmount(value) {
    return `¥${Number(value || 0).toLocaleString('zh-CN', { minimumFractionDigits: 2, maximumFractionDigits: 2 })}`;
}

function formatCompactAmount(value) {
    const amount = Number(value || 0);
    if (Math.abs(amount) >= 10000) return `¥${(amount / 10000).toFixed(1)}w`;
    return `¥${amount.toLocaleString('zh-CN', { maximumFractionDigits: 0 })}`;
}

function buildSpendingAxisTicks(values) {
    const maxValue = Math.max(...values.map(value => Number(value || 0)), 1);
    const ticks = [0, 0.33, 0.66, 1].map(ratio => Math.round(maxValue * ratio));
    return Array.from(new Set(ticks)).sort((a, b) => a - b);
}

const PRIORITY_LABEL = { 1: '紧急', 2: '高优先', 3: '中优先', 4: '低优先', 5: '最低' };

function priorityLabel(priority) {
    return PRIORITY_LABEL[priority] || '未设优先级';
}

function buildHero(summary, monthSummary) {
    return `
        <section class="dashboard-hero">
            <div>
                <h2>📊 概览</h2>
                <p>集中查看最近的安排、任务和资金变化。</p>
            </div>
            <div class="dashboard-hero-tags">
                <span class="dashboard-hero-tag">本月 ${summary.events_month ?? 0} 场日程</span>
                <span class="dashboard-hero-tag">${summary.tasks_pending ?? 0} 项进行中任务</span>
                <span class="dashboard-hero-tag">支出 ${formatCompactAmount(monthSummary.expense || 0)}</span>
            </div>
        </section>`;
}

function renderSummaryCards(summary) {
    const cards = [
        { tone: 'events', icon: '📅', badge: '月视图', value: String(summary.events_month ?? 0), label: '本月日程', meta: '包含已安排和即将到来的事项' },
        { tone: 'tasks', icon: '✅', badge: '进度', value: String(summary.tasks_pending ?? 0), label: '进行中任务', meta: `最近完成 ${summary.tasks_done_recent ?? 0} 项` },
        { tone: 'ledger', icon: '💸', badge: '财务', value: formatCompactAmount(summary.ledger_month_expense ?? 0), label: '本月支出', meta: '按当前自然月累计' },
        { tone: 'diary', icon: '📖', badge: '记录', value: String(summary.diary_month ?? 0), label: '近 30 天日记', meta: '保留最近生活轨迹' },
    ];
    return `
        <section class="dashboard-summary">
            ${cards.map(card => `
                <article class="dashboard-summary-card ${card.tone}">
                    <div class="dashboard-summary-top">
                        <span class="dashboard-summary-icon">${card.icon}</span>
                        <span class="dashboard-summary-pill">${card.badge}</span>
                    </div>
                    <div class="dashboard-summary-value">${card.value}</div>
                    <div class="dashboard-summary-label">${card.label}</div>
                    <div class="dashboard-summary-meta">${card.meta}</div>
                </article>
            `).join('')}
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
                ${items.map(event => {
                    const date = formatMonthDay(event.start_time);
                    const isMultiNode = event.entry_kind === 'multi_node' && event.display_subtitle;
                    const heading = isMultiNode
                        ? `${event.display_title || event.title || '(无标题)'} · ${event.display_subtitle}`
                        : (event.display_title || event.title || '(无标题)');
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
                }).join('')}
            </div>
        </div>`;
}

function renderMonthlyAgenda(events) {
    const now = new Date();
    const sorted = [...events].sort((a, b) => new Date(a.start_time) - new Date(b.start_time));
    const upcoming = sorted.filter(event => {
        const end = event.end_time ? new Date(event.end_time) : new Date(event.start_time);
        return end >= now;
    }).slice(0, 4);
    const past = sorted.filter(event => {
        const end = event.end_time ? new Date(event.end_time) : new Date(event.start_time);
        return end < now;
    }).slice(-4).reverse();

    return `
        <section class="dashboard-panel">
            <div class="dashboard-panel-header">
                <div>
                    <h3 style="color:var(--color-events);">📅 本月日程</h3>
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
    const schedule = task.deadline_at
        ? `截止 ${formatDate(task.deadline_at)} ${formatTime(task.deadline_at)}`
        : (task.plan_date ? `计划 ${formatDate(task.plan_date)}` : '未安排日期');
    const status = '待办';
    return `
        <label class="dashboard-task-item">
            <input type="checkbox" data-task-id="${escapeHtml(task.id || '')}" aria-label="完成待办：${escapeHtml(task.title || '(无标题)')}">
            <div>
                <div class="dashboard-task-title">${escapeHtml(task.title || '(无标题)')}</div>
                <div class="dashboard-task-meta">
                    <span class="dashboard-meta-pill">${status}</span>
                    <span class="dashboard-meta-pill">${priorityLabel(task.priority)}</span>
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
            <div style="width:16px;height:16px;border-radius:999px;background:rgba(16,185,129,0.16);margin-top:4px;"></div>
            <div>
                <div class="dashboard-task-title is-done">${escapeHtml(task.title || '(无标题)')}</div>
                <div class="dashboard-task-meta">
                    <span class="dashboard-meta-pill">已完成</span>
                    ${completedAt ? `<span class="dashboard-meta-pill">${formatDate(completedAt)} ${formatTime(completedAt)}</span>` : ''}
                </div>
            </div>
        </article>`;
}

function renderTasksPanel(tasks) {
    const active = tasks?.active || [];
    const completed = tasks?.completed || [];
    return `
        <section class="dashboard-panel">
            <div class="dashboard-panel-header">
                <div>
                    <h3 style="color:var(--color-tasks);">✅ 任务进度</h3>
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

async function renderSpendingChart(canvasId, spendingTrend) {
    if (!spendingTrend.length) return;
    const Chart = await loadChart();
    const canvas = document.getElementById(canvasId);
    if (!canvas) return;

    const labels = spendingTrend.map(point => {
        const parts = point.date.split('-');
        return `${parts[1]}/${parts[2]}`;
    });
    const values = spendingTrend.map(point => point.amount);
    const axisTicks = buildSpendingAxisTicks(values);
    const axisMax = axisTicks[axisTicks.length - 1] || 1;

    if (_chartInstance) {
        _chartInstance.destroy();
        _chartInstance = null;
    }

    _chartInstance = new Chart(canvas, {
        type: 'line',
        data: {
            labels,
            datasets: [{
                data: values,
                borderColor: '#DC2626',
                backgroundColor: 'rgba(239,68,68,0.12)',
                borderWidth: 2.4,
                pointRadius: 0,
                pointHoverRadius: 4,
                tension: 0.38,
                fill: true,
            }],
        },
        options: {
            responsive: true,
            maintainAspectRatio: false,
            plugins: {
                legend: { display: false },
                tooltip: { callbacks: { label: (ctx) => formatAmount(ctx.parsed.y) } },
            },
            scales: {
                x: { grid: { display: false }, ticks: { color: '#94A3B8', maxTicksLimit: 6 } },
                y: {
                    min: 0,
                    max: axisMax,
                    afterBuildTicks: (axis) => {
                        axis.ticks = axisTicks.map(value => ({ value }));
                    },
                    border: { display: false },
                    grid: {
                        color: 'rgba(239,68,68,0.12)',
                        borderDash: [4, 6],
                        drawTicks: false,
                    },
                    ticks: { color: '#94A3B8', callback: (value) => `¥${value}` },
                },
            },
        },
    });
}

function renderRecentLedger(recentLedger) {
    if (!recentLedger.length) {
        return `<div class="dashboard-empty" style="margin-top:14px;"><strong>本月还没有账目变化</strong><p>新增一笔收入或支出后，这里会显示最近的资金流动。</p></div>`;
    }

    return `
        <div class="dashboard-ledger-list">
            ${recentLedger.slice(0, 5).map(item => {
                const txType = item.transaction_type || 'expense';
                const isIncome = txType === 'income';
                const isTransfer = txType === 'transfer';
                const amount = `${isTransfer ? '' : (isIncome ? '+' : '-')}${formatAmount(item.amount)}`;
                const amountColor = isTransfer ? 'var(--color-text)' : (isIncome ? 'var(--color-success)' : 'var(--color-ledger)');
                return `
                    <article class="dashboard-ledger-item">
                        <div>
                            <strong>${escapeHtml(item.title || '(无摘要)')}</strong>
                            <span>${escapeHtml(item.ledger_category || '未分类')} · ${escapeHtml(item.ledger_date || '')}</span>
                        </div>
                        <div class="dashboard-ledger-amount" style="color:${amountColor};">${amount}</div>
                    </article>`;
            }).join('')}
        </div>`;
}

function renderFinancePanel(spendingTrend, monthSummary, recentLedger) {
    const chartId = 'dashboard-spending-chart';
    return `
        <section class="dashboard-panel">
            <div class="dashboard-panel-header">
                <div>
                    <h3 style="color:var(--color-ledger);">💰 本月财务</h3>
                    <p>查看最近的收支变化和账目记录。</p>
                </div>
                <a class="dashboard-link" href="#/ledger">查看账本 →</a>
            </div>
            <div class="dashboard-panel-body">
                <div class="dashboard-finance-top">
                    <div class="dashboard-finance-metric">
                        <span>本月收入</span>
                        <strong style="color:var(--color-success);">${formatAmount(monthSummary.income || 0)}</strong>
                    </div>
                    <div class="dashboard-finance-metric">
                        <span>本月支出</span>
                        <strong style="color:var(--color-ledger);">${formatAmount(monthSummary.expense || 0)}</strong>
                    </div>
                    <div class="dashboard-finance-metric">
                        <span>当前结余</span>
                        <strong style="color:${(monthSummary.balance || 0) >= 0 ? 'var(--color-success)' : 'var(--color-ledger)'};">${formatAmount(monthSummary.balance || 0)}</strong>
                    </div>
                </div>
                <div class="dashboard-chart-card" id="dashboard-chart-card">
                    <div class="dashboard-chart-head">
                        <strong>支出走势</strong>
                        <span>点击查看详细统计</span>
                    </div>
                    ${spendingTrend.length
                        ? `<div class="dashboard-chart-container"><canvas id="${chartId}"></canvas></div>`
                        : `<div class="dashboard-empty"><strong>暂无支出走势</strong><p>本月出现支出后，这里会逐步形成趋势线。</p></div>`}
                </div>
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
                    <p>近 30 天共写了 ${summary.diary_month ?? 0} 篇。</p>
                    <div style="margin-top:12px;">
                        <a class="dashboard-link" href="#/diary">去写日记 →</a>
                    </div>
                </div>
                <div class="dashboard-diary-stat">
                    <strong>${summary.diary_month ?? 0}</strong>
                    <span>近 30 天记录</span>
                </div>
            </div>
        </section>`;
}

async function loadAndRender() {
    if (!_container) return;
    ensureStyles();
    _container.innerHTML = `<div class="empty-state"><p>加载中...</p></div>`;

    let data;
    try {
        const res = await api.get('/dashboard');
        data = res.data || {};
    } catch (err) {
        _container.innerHTML = `<div class="empty-state"><p>加载失败：${err.message}</p></div>`;
        return;
    }

    const summary = data.summary || {};
    const eventsMonth = data.events_month || [];
    const eventsAgenda = data.events_agenda || eventsMonth;
    const tasks = data.tasks || { active: [], completed: [] };
    const spendingTrend = data.spending_trend || [];
    const monthSummary = data.month_summary || {};
    const recentLedger = data.recent_ledger || [];

    _container.innerHTML = `
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

    const chartCard = document.getElementById('dashboard-chart-card');
    if (chartCard) chartCard.addEventListener('click', () => navigate('stats'));

    const activeTasksEl = document.getElementById('dashboard-active-tasks');
    if (activeTasksEl) {
        activeTasksEl.addEventListener('change', async (event) => {
            const checkbox = event.target;
            if (!checkbox.matches('input[type="checkbox"][data-task-id]')) return;
            const id = checkbox.dataset.taskId;
            checkbox.disabled = true;
            try {
                await api.put('/items/' + id, { status: 'done' });
                showToast('任务已完成 ✅', 'success');
                await loadAndRender();
            } catch (err) {
                showToast('标记失败：' + err.message, 'error');
                checkbox.checked = false;
                checkbox.disabled = false;
            }
        });
    }

    if (spendingTrend.length) {
        await renderSpendingChart('dashboard-spending-chart', spendingTrend);
    } else if (_chartInstance) {
        _chartInstance.destroy();
        _chartInstance = null;
    }
}

export function render(container) {
    _container = container;
    loadAndRender();
    _dataChangedHandler = () => loadAndRender();
    window.addEventListener('pendo-data-changed', _dataChangedHandler);
}

export function destroy() {
    if (_chartInstance) {
        _chartInstance.destroy();
        _chartInstance = null;
    }
    if (_dataChangedHandler) {
        window.removeEventListener('pendo-data-changed', _dataChangedHandler);
        _dataChangedHandler = null;
    }
    _container = null;
}

export function onRouteEnter(_params) {}
