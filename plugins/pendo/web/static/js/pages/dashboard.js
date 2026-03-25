import { api } from '../api.js';
import { showToast } from '../components/toast.js';
import { navigate } from '../router.js';
import { loadChart } from '../lib/chart-loader.js';

// ── helpers ──────────────────────────────────────────────────────────────────

function formatTime(iso) {
    if (!iso) return '';
    const d = new Date(iso);
    const h = String(d.getHours()).padStart(2, '0');
    const m = String(d.getMinutes()).padStart(2, '0');
    return `${h}:${m}`;
}

function formatAmount(n) {
    return '¥' + Number(n).toFixed(2);
}

const PRIORITY_LABEL = {
    1: '🔴 紧急',
    2: '🟠 高',
    3: '🟡 中',
    4: '🟢 低',
    5: '⚪ 最低',
};

function priorityLabel(p) {
    return PRIORITY_LABEL[p] || String(p);
}


// ── module state ──────────────────────────────────────────────────────────────

let _container = null;
let _chartInstance = null;
let _dataChangedHandler = null;

// ── render helpers ────────────────────────────────────────────────────────────

function renderSummaryCards(summary) {
    const cards = [
        {
            icon: '📅',
            colorClass: 'events',
            value: summary.events_today ?? 0,
            label: '今日日程',
        },
        {
            icon: '✅',
            colorClass: 'tasks',
            value: summary.tasks_pending ?? 0,
            label: '待办任务',
        },
        {
            icon: '💰',
            colorClass: 'ledger',
            value: summary.ledger_week ?? 0,
            label: '本周账单',
        },
        {
            icon: '📖',
            colorClass: 'diary',
            value: summary.diary_month ?? 0,
            label: '本月日记',
        },
    ];

    return `
        <div class="summary-cards">
            ${cards.map(c => `
                <div class="summary-card">
                    <div class="summary-card-icon ${c.colorClass}">${c.icon}</div>
                    <div class="summary-card-content">
                        <div class="summary-card-value">${c.value}</div>
                        <div class="summary-card-label">${c.label}</div>
                    </div>
                </div>
            `).join('')}
        </div>
    `;
}

function renderEventsTimeline(events) {
    if (!events || events.length === 0) {
        return `<div class="empty-state"><p>今日暂无日程</p></div>`;
    }

    const sorted = [...events].sort((a, b) => {
        return new Date(a.start_time) - new Date(b.start_time);
    });

    const now = new Date();

    return `
        <div style="padding-left: 28px; position: relative;">
            <div style="position:absolute;left:7px;top:0;bottom:0;width:2px;background:var(--color-border);border-radius:1px;"></div>
            ${sorted.map(ev => {
                const start = new Date(ev.start_time);
                const end = ev.end_time ? new Date(ev.end_time) : null;
                const isActive = start <= now && (!end || end >= now);
                return `
                    <div class="timeline-item${isActive ? ' active' : ''}">
                        <div class="timeline-date" style="color:var(--color-events);font-weight:600;">
                            ${formatTime(ev.start_time)}${end ? ' – ' + formatTime(ev.end_time) : ''}
                        </div>
                        <div class="timeline-content">
                            <div style="font-weight:600;color:var(--color-text);">${ev.title || '(无标题)'}</div>
                            ${ev.location ? `<div style="font-size:12px;color:var(--color-text-secondary);margin-top:4px;">📍 ${ev.location}</div>` : ''}
                        </div>
                    </div>
                `;
            }).join('')}
        </div>
    `;
}

function renderTasksList(tasks, container) {
    if (!tasks || tasks.length === 0) {
        return `<div class="empty-state"><p>暂无待办任务</p></div>`;
    }

    const sorted = [...tasks].sort((a, b) => (a.priority ?? 99) - (b.priority ?? 99));

    return `
        <ul style="list-style:none;padding:0;margin:0;display:flex;flex-direction:column;gap:8px;">
            ${sorted.map(task => `
                <li style="display:flex;align-items:center;gap:10px;padding:10px 12px;background:var(--color-bg);border-radius:var(--radius-sm);border:1px solid var(--color-border);">
                    <input
                        type="checkbox"
                        data-task-id="${task.id}"
                        style="width:16px;height:16px;cursor:pointer;flex-shrink:0;"
                        ${task.status === 'done' ? 'checked disabled' : ''}
                    />
                    <div style="flex:1;min-width:0;">
                        <div style="font-weight:500;color:var(--color-text);${task.status === 'done' ? 'text-decoration:line-through;opacity:0.5;' : ''}">${task.title || '(无标题)'}</div>
                        <div style="display:flex;gap:8px;margin-top:4px;flex-wrap:wrap;">
                            <span style="font-size:12px;">${priorityLabel(task.priority)}</span>
                            ${task.category ? `<span class="badge badge-gray">${task.category}</span>` : ''}
                            ${task.due_time ? `<span style="font-size:12px;color:var(--color-text-secondary);">截止 ${formatTime(task.due_time) || task.due_time.slice(0, 10)}</span>` : ''}
                        </div>
                    </div>
                </li>
            `).join('')}
        </ul>
    `;
}

async function renderSpendingChart(canvasId, spendingTrend) {
    if (!spendingTrend || spendingTrend.length === 0) return;

    try {
        const Chart = await loadChart();
        const canvas = document.getElementById(canvasId);
        if (!canvas) return;

        const labels = spendingTrend.map(d => {
            const parts = d.date.split('-');
            return `${parts[1]}/${parts[2]}`;
        });
        const values = spendingTrend.map(d => d.amount);

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
                    borderColor: '#EF4444',
                    backgroundColor: 'rgba(239,68,68,0.08)',
                    borderWidth: 2,
                    pointRadius: 3,
                    pointHoverRadius: 5,
                    fill: true,
                    tension: 0.4,
                }],
            },
            options: {
                responsive: true,
                maintainAspectRatio: false,
                plugins: {
                    legend: { display: false },
                    tooltip: {
                        callbacks: {
                            label: ctx => formatAmount(ctx.parsed.y),
                        },
                    },
                },
                scales: {
                    x: {
                        ticks: {
                            font: { size: 10 },
                            maxTicksLimit: 7,
                            color: '#9CA3AF',
                        },
                        grid: { display: false },
                    },
                    y: {
                        ticks: {
                            font: { size: 10 },
                            color: '#9CA3AF',
                            callback: v => '¥' + v,
                        },
                        grid: { color: 'rgba(0,0,0,0.04)' },
                    },
                },
            },
        });
    } catch (err) {
        console.warn('Dashboard chart error:', err);
    }
}

function renderMonthSummary(monthSummary) {
    const { income = 0, expense = 0, balance = 0 } = monthSummary || {};
    const items = [
        { label: '本月收入', value: income, color: 'var(--color-success)' },
        { label: '本月支出', value: expense, color: 'var(--color-ledger)' },
        { label: '结余', value: balance, color: balance >= 0 ? 'var(--color-success)' : 'var(--color-ledger)' },
    ];
    return `
        <div style="display:flex;gap:0;border-radius:var(--radius-sm);overflow:hidden;border:1px solid var(--color-border);">
            ${items.map((item, i) => `
                <div style="flex:1;padding:16px;text-align:center;${i > 0 ? 'border-left:1px solid var(--color-border);' : ''}background:var(--color-surface);">
                    <div style="font-size:18px;font-weight:700;color:${item.color};">${formatAmount(item.value)}</div>
                    <div style="font-size:12px;color:var(--color-text-secondary);margin-top:4px;">${item.label}</div>
                </div>
            `).join('')}
        </div>
    `;
}

// ── main page render ──────────────────────────────────────────────────────────

async function loadAndRender() {
    if (!_container) return;

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
    const events = data.events || [];
    const tasks = data.tasks || [];
    const spendingTrend = data.spending_trend || [];
    const monthSummary = data.month_summary || {};

    const CHART_CANVAS_ID = 'dashboard-spending-chart';

    _container.innerHTML = `
        <div style="padding:24px;max-width:1200px;margin:0 auto;">

            <!-- Page title -->
            <div style="margin-bottom:24px;">
                <h2 style="font-size:20px;font-weight:700;color:var(--color-dashboard);">📊 概览</h2>
                <p style="font-size:13px;color:var(--color-text-secondary);margin-top:2px;">今日数据摘要</p>
            </div>

            <!-- Summary cards -->
            ${renderSummaryCards(summary)}

            <!-- Main content grid: left=events+tasks, right=chart+ledger -->
            <div class="chart-row" style="align-items:start;">

                <!-- Left column: events + tasks -->
                <div style="display:flex;flex-direction:column;gap:16px;">

                    <!-- Today's events -->
                    <div class="card">
                        <div class="card-header">
                            <h3 style="color:var(--color-events);">📅 今日日程</h3>
                            <a href="#/events" style="font-size:13px;color:var(--color-text-secondary);">查看全部 →</a>
                        </div>
                        <div id="dashboard-events-list">
                            ${renderEventsTimeline(events)}
                        </div>
                    </div>

                    <!-- Pending tasks -->
                    <div class="card">
                        <div class="card-header">
                            <h3 style="color:var(--color-tasks);">✅ 待办任务</h3>
                            <a href="#/tasks" style="font-size:13px;color:var(--color-text-secondary);">查看全部 →</a>
                        </div>
                        <div id="dashboard-tasks-list">
                            ${renderTasksList(tasks, null)}
                        </div>
                    </div>

                </div>

                <!-- Right column: spending chart + month summary -->
                <div style="display:flex;flex-direction:column;gap:16px;">

                    <!-- Spending trend chart -->
                    <div class="chart-card" style="cursor:pointer;" id="dashboard-chart-card">
                        <h3 style="color:var(--color-ledger);">💸 近期支出趋势</h3>
                        ${spendingTrend.length > 0
                            ? `<div class="chart-container" style="height:180px;"><canvas id="${CHART_CANVAS_ID}"></canvas></div>`
                            : `<div class="empty-state" style="padding:24px 0;"><p>暂无支出数据</p></div>`
                        }
                        <div style="font-size:12px;color:var(--color-text-tertiary);margin-top:8px;text-align:right;">点击查看详细统计 →</div>
                    </div>

                    <!-- Month income/expense/balance -->
                    <div class="card">
                        <div class="card-header">
                            <h3 style="color:var(--color-ledger);">💰 本月财务</h3>
                            <a href="#/ledger" style="font-size:13px;color:var(--color-text-secondary);">查看账本 →</a>
                        </div>
                        ${renderMonthSummary(monthSummary)}
                    </div>

                </div>

            </div>
        </div>
    `;

    // Navigate to stats when chart card is clicked
    const chartCard = document.getElementById('dashboard-chart-card');
    if (chartCard) {
        chartCard.addEventListener('click', () => navigate('stats'));
    }

    // Attach task checkbox listeners
    const tasksListEl = document.getElementById('dashboard-tasks-list');
    if (tasksListEl) {
        tasksListEl.addEventListener('change', async (e) => {
            const cb = e.target;
            if (!cb.matches('input[type="checkbox"][data-task-id]')) return;
            const id = cb.dataset.taskId;
            cb.disabled = true;
            try {
                await api.put('/items/' + id, { status: 'done' });
                showToast('任务已完成 ✅', 'success');
                await loadAndRender();
            } catch (err) {
                showToast('标记失败：' + err.message, 'error');
                cb.checked = false;
                cb.disabled = false;
            }
        });
    }

    // Render chart (after DOM is ready)
    if (spendingTrend.length > 0) {
        await renderSpendingChart(CHART_CANVAS_ID, spendingTrend);
    }
}

// ── page module exports ───────────────────────────────────────────────────────

export function render(container) {
    _container = container;
    loadAndRender();

    // Listen for data changes (e.g. quick-add via FAB)
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

export function onRouteEnter(_params) {
    // Nothing special needed on route enter; render() is called by router
}
