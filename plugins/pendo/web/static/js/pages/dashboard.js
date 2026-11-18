import { api } from '../api.js';
import { showToast } from '../components/toast.js';
import {
    errorMessage,
    finiteNumber,
    formatAmount,
    formatMoneyCompact,
    isRecord,
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
import { escapeHtml, subscribeDataChanges } from '../utils/ui.js';

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
            diary_recent: nonNegativeInteger(summary.diary_recent),
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
        <section class="dashboard-hero pendo-page-header">
            <div>
                <span class="eyebrow"><i></i>DASHBOARD</span>
                <h2>概览</h2>
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
            badge: '月视图',
            value: String(summary.events_month),
            label: '本月日程',
            meta: '包含已安排和即将到来的事项',
        },
        {
            tone: 'tasks',
            badge: '进度',
            value: String(summary.tasks_pending),
            label: '进行中任务',
            meta: `最近完成 ${summary.tasks_done_recent} 项`,
        },
        {
            tone: 'ledger',
            badge: '财务',
            value: formatMoneyCompact(summary.ledger_month_expense),
            label: '本月支出',
            meta: '按当前自然月累计',
        },
        {
            tone: 'diary',
            badge: '记录',
            value: String(summary.diary_recent),
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
                        <span class="dashboard-summary-icon pixel-icon" data-module="${card.tone}" aria-hidden="true"></span>
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
                                    <span class="dashboard-meta-pill"><span class="pixel-icon" data-icon="clock" aria-hidden="true"></span>${formatTime(event.start_time)}${event.end_time ? ` - ${formatTime(event.end_time)}` : ''}</span>
                                    ${event.location ? `<span class="dashboard-meta-pill location"><span class="pixel-icon" data-icon="pin" aria-hidden="true"></span>${escapeHtml(event.location)}</span>` : ''}
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
                    <h3 class="dashboard-panel-title"><span class="pixel-icon" data-module="events" aria-hidden="true"></span>本月日程</h3>
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
                    <h3 class="dashboard-panel-title"><span class="pixel-icon" data-module="tasks" aria-hidden="true"></span>任务进度</h3>
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
                    <h3 class="dashboard-panel-title"><span class="pixel-icon" data-module="ledger" aria-hidden="true"></span>本月财务</h3>
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
                    <strong class="dashboard-panel-title"><span class="pixel-icon" data-module="diary" aria-hidden="true"></span>日记记录</strong>
                    <p>近 30 天共写了 ${summary.diary_recent} 篇。</p>
                    <div class="dashboard-diary-link">
                        <a class="dashboard-link" href="#/diary">去写日记 →</a>
                    </div>
                </div>
                <div class="dashboard-diary-stat">
                    <strong>${summary.diary_recent}</strong>
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
 * 每次请求持有独立版本。路由销毁或后续刷新会让旧响应失效，避免慢响应覆盖新页面。
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
                showToast('任务已完成', 'success');
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
