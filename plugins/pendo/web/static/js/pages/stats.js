import { api } from '../api.js';
import { loadChart } from '../lib/chart-loader.js';

// ── constants ─────────────────────────────────────────────────────────────────

const CSS_ID = 'pendo-stats-styles';

const PALETTE = [
    '#6366F1', '#F59E0B', '#10B981', '#EF4444', '#3B82F6',
    '#EC4899', '#8B5CF6', '#F97316', '#14B8A6', '#E11D48',
];

const PRIORITY_LABELS = { 1: '紧急', 2: '高', 3: '中', 4: '低', 5: '最低' };

const TABS = [
    { key: 'ledger', label: '记账' },
    { key: 'tasks',  label: '待办' },
    { key: 'events', label: '日程' },
];

const RANGES = [
    { key: 'week',    label: '本周' },
    { key: 'month',   label: '本月' },
    { key: 'quarter', label: '本季' },
    { key: 'year',    label: '本年' },
];

// ── module state ──────────────────────────────────────────────────────────────

let _container       = null;
let _activeTab       = 'ledger';
let _range           = 'month';
let _charts          = [];          // all live Chart instances
let _tabLoaded       = {};          // { ledger: false, tasks: false, events: false }

// ── helpers ───────────────────────────────────────────────────────────────────

function formatAmount(n) {
    return '¥' + Number(n || 0).toFixed(2);
}

function destroyAllCharts() {
    _charts.forEach(c => { try { c.destroy(); } catch (_) {} });
    _charts = [];
}

function downloadChart(canvas, filename) {
    const link = document.createElement('a');
    link.download = filename + '.png';
    link.href = canvas.toDataURL('image/png');
    link.click();
}

function makeDownloadBtn(canvasId, filename) {
    return `<button class="btn btn-sm btn-secondary stats-dl-btn" data-canvas="${canvasId}" data-filename="${filename}" title="下载图表">↓ PNG</button>`;
}

function showEmpty(el, msg) {
    el.innerHTML = `<div class="empty-state" style="padding:40px 0;"><p>${msg || '暂无数据'}</p></div>`;
}

// ── styles ────────────────────────────────────────────────────────────────────

function injectStyles() {
    if (document.getElementById(CSS_ID)) return;
    const style = document.createElement('style');
    style.id = CSS_ID;
    style.textContent = `
        .stats-page { padding: 24px; max-width: 1200px; margin: 0 auto; }
        .stats-header { display: flex; align-items: center; justify-content: space-between; flex-wrap: wrap; gap: 12px; margin-bottom: 24px; }
        .stats-title { font-size: 20px; font-weight: 700; color: var(--color-stats, #8B5CF6); }
        .stats-tab-bar { display: flex; gap: 4px; background: var(--color-surface); border: 1px solid var(--color-border); border-radius: var(--radius-sm, 8px); padding: 4px; }
        .stats-tab-btn { padding: 6px 18px; border-radius: calc(var(--radius-sm, 8px) - 2px); border: none; background: transparent; color: var(--color-text-secondary); font-size: 14px; font-weight: 500; cursor: pointer; transition: all 0.15s; }
        .stats-tab-btn.active { background: var(--color-stats, #8B5CF6); color: #fff; }
        .stats-range-bar { display: flex; gap: 4px; }
        .stats-range-btn { padding: 5px 12px; border-radius: var(--radius-sm, 8px); border: 1px solid var(--color-border); background: transparent; color: var(--color-text-secondary); font-size: 13px; cursor: pointer; transition: all 0.15s; }
        .stats-range-btn.active { background: var(--color-stats, #8B5CF6); color: #fff; border-color: var(--color-stats, #8B5CF6); }
        .stats-tab-panel { display: none; }
        .stats-tab-panel.active { display: block; }
        .stats-summary-cards { display: grid; grid-template-columns: repeat(auto-fit, minmax(140px, 1fr)); gap: 12px; margin-bottom: 20px; }
        .stats-summary-card { background: var(--color-surface); border: 1px solid var(--color-border); border-radius: var(--radius, 12px); padding: 16px; text-align: center; }
        .stats-summary-card .sc-value { font-size: 24px; font-weight: 700; color: var(--color-stats, #8B5CF6); }
        .stats-summary-card .sc-label { font-size: 12px; color: var(--color-text-secondary); margin-top: 4px; }
        .stats-dl-btn { margin-top: 8px; float: right; }
        .chart-card { position: relative; }
        .chart-card::after { content: ''; display: table; clear: both; }
    `;
    document.head.appendChild(style);
}

// ── HTML skeleton ─────────────────────────────────────────────────────────────

function buildSkeleton() {
    const tabBtns = TABS.map(t =>
        `<button class="stats-tab-btn${t.key === _activeTab ? ' active' : ''}" data-tab="${t.key}">${t.label}</button>`
    ).join('');

    const rangeBtns = RANGES.map(r =>
        `<button class="stats-range-btn${r.key === _range ? ' active' : ''}" data-range="${r.key}">${r.label}</button>`
    ).join('');

    const panels = TABS.map(t => `
        <div class="stats-tab-panel${t.key === _activeTab ? ' active' : ''}" id="stats-panel-${t.key}">
            <div class="empty-state" style="padding:60px 0;"><p>加载中...</p></div>
        </div>
    `).join('');

    return `
        <div class="stats-page">
            <div class="stats-header">
                <div>
                    <div class="stats-title">📊 统计</div>
                </div>
                <div style="display:flex;gap:16px;align-items:center;flex-wrap:wrap;">
                    <div class="stats-tab-bar">${tabBtns}</div>
                    <div class="stats-range-bar">${rangeBtns}</div>
                </div>
            </div>
            ${panels}
        </div>
    `;
}

// ── ledger tab ────────────────────────────────────────────────────────────────

async function renderLedgerTab(panelEl) {
    panelEl.innerHTML = `
        <div class="chart-row">
            <div class="chart-card" id="sc-ledger-monthly">
                <h3>收支趋势</h3>
                <div class="chart-container" style="height:240px;"><canvas id="cv-ledger-monthly"></canvas></div>
                ${makeDownloadBtn('cv-ledger-monthly', '收支趋势')}
            </div>
            <div class="chart-card" id="sc-ledger-daily">
                <h3>每日支出</h3>
                <div class="chart-container" style="height:240px;"><canvas id="cv-ledger-daily"></canvas></div>
                ${makeDownloadBtn('cv-ledger-daily', '每日支出')}
            </div>
        </div>
        <div class="chart-row" style="margin-top:16px;">
            <div class="chart-card" id="sc-ledger-expense-cat">
                <h3>支出分类</h3>
                <div class="chart-container" style="height:260px;"><canvas id="cv-ledger-expense-cat"></canvas></div>
                ${makeDownloadBtn('cv-ledger-expense-cat', '支出分类')}
            </div>
            <div class="chart-card" id="sc-ledger-income-cat">
                <h3>收入分类</h3>
                <div class="chart-container" style="height:260px;"><canvas id="cv-ledger-income-cat"></canvas></div>
                ${makeDownloadBtn('cv-ledger-income-cat', '收入分类')}
            </div>
        </div>
    `;

    let data;
    try {
        const res = await api.get('/stats/ledger', { range: _range });
        data = res.data || {};
    } catch (err) {
        panelEl.innerHTML = `<div class="empty-state"><p>加载失败：${err.message}</p></div>`;
        return;
    }

    const Chart = await loadChart();

    // Monthly income/expense bar chart
    {
        const monthly = data.monthly || [];
        const el = document.getElementById('sc-ledger-monthly');
        const canvas = document.getElementById('cv-ledger-monthly');
        if (!monthly.length) {
            showEmpty(el, '暂无数据');
        } else {
            const labels = monthly.map(d => d.month);
            const c = new Chart(canvas, {
                type: 'bar',
                data: {
                    labels,
                    datasets: [
                        {
                            label: '收入',
                            data: monthly.map(d => d.income || 0),
                            backgroundColor: '#10B981',
                        },
                        {
                            label: '支出',
                            data: monthly.map(d => d.expense || 0),
                            backgroundColor: '#EF4444',
                        },
                    ],
                },
                options: {
                    responsive: true,
                    maintainAspectRatio: false,
                    plugins: {
                        legend: { position: 'top' },
                        tooltip: { callbacks: { label: ctx => formatAmount(ctx.parsed.y) } },
                    },
                    scales: {
                        x: { ticks: { color: '#9CA3AF', font: { size: 11 } }, grid: { display: false } },
                        y: { ticks: { color: '#9CA3AF', font: { size: 11 }, callback: v => '¥' + v }, grid: { color: 'rgba(0,0,0,0.04)' } },
                    },
                },
            });
            _charts.push(c);
        }
    }

    // Daily line chart
    {
        const daily = data.daily || [];
        const el = document.getElementById('sc-ledger-daily');
        const canvas = document.getElementById('cv-ledger-daily');
        if (!daily.length) {
            showEmpty(el, '暂无数据');
        } else {
            const labels = daily.map(d => {
                const parts = String(d.date).split('-');
                return parts.length >= 3 ? `${parts[1]}/${parts[2]}` : d.date;
            });
            const c = new Chart(canvas, {
                type: 'line',
                data: {
                    labels,
                    datasets: [
                        {
                            label: '收入',
                            data: daily.map(d => d.income || 0),
                            borderColor: '#10B981',
                            backgroundColor: 'rgba(16,185,129,0.08)',
                            borderWidth: 2,
                            pointRadius: 3,
                            fill: true,
                            tension: 0.4,
                        },
                        {
                            label: '支出',
                            data: daily.map(d => d.expense || 0),
                            borderColor: '#EF4444',
                            backgroundColor: 'rgba(239,68,68,0.08)',
                            borderWidth: 2,
                            pointRadius: 3,
                            fill: true,
                            tension: 0.4,
                        },
                    ],
                },
                options: {
                    responsive: true,
                    maintainAspectRatio: false,
                    plugins: {
                        legend: { position: 'top' },
                        tooltip: { callbacks: { label: ctx => formatAmount(ctx.parsed.y) } },
                    },
                    scales: {
                        x: { ticks: { color: '#9CA3AF', font: { size: 10 }, maxTicksLimit: 10 }, grid: { display: false } },
                        y: { ticks: { color: '#9CA3AF', font: { size: 10 }, callback: v => '¥' + v }, grid: { color: 'rgba(0,0,0,0.04)' } },
                    },
                },
            });
            _charts.push(c);
        }
    }

    // Expense category pie
    {
        const cats = data.expense_by_category || [];
        const el = document.getElementById('sc-ledger-expense-cat');
        const canvas = document.getElementById('cv-ledger-expense-cat');
        if (!cats.length) {
            showEmpty(el, '暂无支出数据');
        } else {
            const c = new Chart(canvas, {
                type: 'pie',
                data: {
                    labels: cats.map(d => d.category || '其他'),
                    datasets: [{ data: cats.map(d => d.total || 0), backgroundColor: PALETTE }],
                },
                options: {
                    responsive: true,
                    maintainAspectRatio: false,
                    plugins: {
                        legend: { position: 'right', labels: { font: { size: 12 } } },
                        tooltip: { callbacks: { label: ctx => `${ctx.label}: ${formatAmount(ctx.parsed)}` } },
                    },
                },
            });
            _charts.push(c);
        }
    }

    // Income category pie
    {
        const cats = data.income_by_category || [];
        const el = document.getElementById('sc-ledger-income-cat');
        const canvas = document.getElementById('cv-ledger-income-cat');
        if (!cats.length) {
            showEmpty(el, '暂无收入数据');
        } else {
            const c = new Chart(canvas, {
                type: 'pie',
                data: {
                    labels: cats.map(d => d.category || '其他'),
                    datasets: [{ data: cats.map(d => d.total || 0), backgroundColor: PALETTE }],
                },
                options: {
                    responsive: true,
                    maintainAspectRatio: false,
                    plugins: {
                        legend: { position: 'right', labels: { font: { size: 12 } } },
                        tooltip: { callbacks: { label: ctx => `${ctx.label}: ${formatAmount(ctx.parsed)}` } },
                    },
                },
            });
            _charts.push(c);
        }
    }
}

// ── tasks tab ─────────────────────────────────────────────────────────────────

async function renderTasksTab(panelEl) {
    panelEl.innerHTML = `
        <div id="stats-tasks-summary" style="margin-bottom:20px;"></div>
        <div class="chart-row">
            <div class="chart-card" id="sc-tasks-weekly">
                <h3>每周完成率</h3>
                <div class="chart-container" style="height:240px;"><canvas id="cv-tasks-weekly"></canvas></div>
                ${makeDownloadBtn('cv-tasks-weekly', '每周完成率')}
            </div>
            <div class="chart-card" id="sc-tasks-priority">
                <h3>优先级分布</h3>
                <div class="chart-container" style="height:240px;"><canvas id="cv-tasks-priority"></canvas></div>
                ${makeDownloadBtn('cv-tasks-priority', '优先级分布')}
            </div>
        </div>
        <div class="chart-row" style="margin-top:16px;">
            <div class="chart-card" id="sc-tasks-category">
                <h3>分类分布</h3>
                <div class="chart-container" style="height:260px;"><canvas id="cv-tasks-category"></canvas></div>
                ${makeDownloadBtn('cv-tasks-category', '任务分类')}
            </div>
            <div></div>
        </div>
    `;

    let data;
    try {
        const res = await api.get('/stats/tasks', { range: _range });
        data = res.data || {};
    } catch (err) {
        panelEl.innerHTML = `<div class="empty-state"><p>加载失败：${err.message}</p></div>`;
        return;
    }

    // Summary cards
    const summaryEl = document.getElementById('stats-tasks-summary');
    if (summaryEl) {
        const rate = (data.completion_rate != null)
            ? (Number(data.completion_rate) * (data.completion_rate <= 1 ? 100 : 1)).toFixed(1) + '%'
            : '—';
        summaryEl.innerHTML = `
            <div class="stats-summary-cards">
                <div class="stats-summary-card">
                    <div class="sc-value">${data.total ?? 0}</div>
                    <div class="sc-label">总任务</div>
                </div>
                <div class="stats-summary-card">
                    <div class="sc-value">${data.done ?? 0}</div>
                    <div class="sc-label">已完成</div>
                </div>
                <div class="stats-summary-card">
                    <div class="sc-value" style="color:var(--color-success, #10B981);">${rate}</div>
                    <div class="sc-label">完成率</div>
                </div>
                <div class="stats-summary-card">
                    <div class="sc-value" style="color:var(--color-stats, #8B5CF6);">${data.new_this_week ?? 0}</div>
                    <div class="sc-label">本周新增</div>
                </div>
            </div>
        `;
    }

    const Chart = await loadChart();

    // Weekly completion rate line chart
    {
        const weekly = data.weekly_completion || [];
        const el = document.getElementById('sc-tasks-weekly');
        const canvas = document.getElementById('cv-tasks-weekly');
        if (!weekly.length) {
            showEmpty(el, '暂无数据');
        } else {
            const c = new Chart(canvas, {
                type: 'line',
                data: {
                    labels: weekly.map(d => d.week),
                    datasets: [{
                        label: '完成率',
                        data: weekly.map(d => {
                            const v = Number(d.rate || 0);
                            return v <= 1 ? +(v * 100).toFixed(1) : +v.toFixed(1);
                        }),
                        borderColor: '#8B5CF6',
                        backgroundColor: 'rgba(139,92,246,0.08)',
                        borderWidth: 2,
                        pointRadius: 4,
                        fill: true,
                        tension: 0.4,
                    }],
                },
                options: {
                    responsive: true,
                    maintainAspectRatio: false,
                    plugins: {
                        legend: { display: false },
                        tooltip: { callbacks: { label: ctx => ctx.parsed.y + '%' } },
                    },
                    scales: {
                        x: { ticks: { color: '#9CA3AF', font: { size: 11 } }, grid: { display: false } },
                        y: {
                            min: 0, max: 100,
                            ticks: { color: '#9CA3AF', font: { size: 11 }, callback: v => v + '%' },
                            grid: { color: 'rgba(0,0,0,0.04)' },
                        },
                    },
                },
            });
            _charts.push(c);
        }
    }

    // Priority donut chart
    {
        const byPriority = data.by_priority || [];
        const el = document.getElementById('sc-tasks-priority');
        const canvas = document.getElementById('cv-tasks-priority');
        if (!byPriority.length) {
            showEmpty(el, '暂无数据');
        } else {
            const c = new Chart(canvas, {
                type: 'doughnut',
                data: {
                    labels: byPriority.map(d => PRIORITY_LABELS[d.priority] || String(d.priority)),
                    datasets: [{ data: byPriority.map(d => d.count || 0), backgroundColor: PALETTE }],
                },
                options: {
                    responsive: true,
                    maintainAspectRatio: false,
                    cutout: '55%',
                    plugins: {
                        legend: { position: 'right', labels: { font: { size: 12 } } },
                        tooltip: { callbacks: { label: ctx => `${ctx.label}: ${ctx.parsed}` } },
                    },
                },
            });
            _charts.push(c);
        }
    }

    // Category pie chart
    {
        const byCat = data.by_category || [];
        const el = document.getElementById('sc-tasks-category');
        const canvas = document.getElementById('cv-tasks-category');
        if (!byCat.length) {
            showEmpty(el, '暂无数据');
        } else {
            const c = new Chart(canvas, {
                type: 'pie',
                data: {
                    labels: byCat.map(d => d.category || '未分类'),
                    datasets: [{ data: byCat.map(d => d.count || 0), backgroundColor: PALETTE }],
                },
                options: {
                    responsive: true,
                    maintainAspectRatio: false,
                    plugins: {
                        legend: { position: 'right', labels: { font: { size: 12 } } },
                        tooltip: { callbacks: { label: ctx => `${ctx.label}: ${ctx.parsed}` } },
                    },
                },
            });
            _charts.push(c);
        }
    }
}

// ── events tab ────────────────────────────────────────────────────────────────

async function renderEventsTab(panelEl) {
    panelEl.innerHTML = `
        <div class="chart-row">
            <div class="chart-card" id="sc-events-busyness">
                <h3>每周忙碌程度</h3>
                <div class="chart-container" style="height:240px;"><canvas id="cv-events-busyness"></canvas></div>
                ${makeDownloadBtn('cv-events-busyness', '每周忙碌程度')}
            </div>
            <div class="chart-card" id="sc-events-timeslot">
                <h3>时间段分布</h3>
                <div class="chart-container" style="height:240px;"><canvas id="cv-events-timeslot"></canvas></div>
                ${makeDownloadBtn('cv-events-timeslot', '时间段分布')}
            </div>
        </div>
        <div class="chart-row" style="margin-top:16px;">
            <div class="chart-card" id="sc-events-category">
                <h3>分类分布</h3>
                <div class="chart-container" style="height:260px;"><canvas id="cv-events-category"></canvas></div>
                ${makeDownloadBtn('cv-events-category', '日程分类')}
            </div>
            <div></div>
        </div>
    `;

    let data;
    try {
        const res = await api.get('/stats/events', { range: _range });
        data = res.data || {};
    } catch (err) {
        panelEl.innerHTML = `<div class="empty-state"><p>加载失败：${err.message}</p></div>`;
        return;
    }

    const Chart = await loadChart();

    // Weekly busyness bar chart (events per day of week)
    {
        const busyness = data.weekly_busyness || [];
        const el = document.getElementById('sc-events-busyness');
        const canvas = document.getElementById('cv-events-busyness');
        if (!busyness.length) {
            showEmpty(el, '暂无数据');
        } else {
            const c = new Chart(canvas, {
                type: 'bar',
                data: {
                    labels: busyness.map(d => d.day),
                    datasets: [{
                        label: '日程数',
                        data: busyness.map(d => d.count || 0),
                        backgroundColor: 'rgba(99,102,241,0.75)',
                        borderRadius: 4,
                    }],
                },
                options: {
                    responsive: true,
                    maintainAspectRatio: false,
                    plugins: {
                        legend: { display: false },
                        tooltip: { callbacks: { label: ctx => `${ctx.parsed.y} 个日程` } },
                    },
                    scales: {
                        x: { ticks: { color: '#9CA3AF', font: { size: 11 } }, grid: { display: false } },
                        y: {
                            ticks: { color: '#9CA3AF', font: { size: 11 }, stepSize: 1 },
                            grid: { color: 'rgba(0,0,0,0.04)' },
                            beginAtZero: true,
                        },
                    },
                },
            });
            _charts.push(c);
        }
    }

    // Time slot horizontal bar chart
    {
        const slots = data.by_time_slot || [];
        const el = document.getElementById('sc-events-timeslot');
        const canvas = document.getElementById('cv-events-timeslot');
        if (!slots.length) {
            showEmpty(el, '暂无数据');
        } else {
            const c = new Chart(canvas, {
                type: 'bar',
                data: {
                    labels: slots.map(d => d.slot),
                    datasets: [{
                        label: '日程数',
                        data: slots.map(d => d.count || 0),
                        backgroundColor: 'rgba(139,92,246,0.7)',
                        borderRadius: 3,
                    }],
                },
                options: {
                    indexAxis: 'y',
                    responsive: true,
                    maintainAspectRatio: false,
                    plugins: {
                        legend: { display: false },
                        tooltip: { callbacks: { label: ctx => `${ctx.parsed.x} 个日程` } },
                    },
                    scales: {
                        x: {
                            ticks: { color: '#9CA3AF', font: { size: 10 }, stepSize: 1 },
                            grid: { color: 'rgba(0,0,0,0.04)' },
                            beginAtZero: true,
                        },
                        y: { ticks: { color: '#9CA3AF', font: { size: 10 } }, grid: { display: false } },
                    },
                },
            });
            _charts.push(c);
        }
    }

    // Category pie chart
    {
        const byCat = data.by_category || [];
        const el = document.getElementById('sc-events-category');
        const canvas = document.getElementById('cv-events-category');
        if (!byCat.length) {
            showEmpty(el, '暂无数据');
        } else {
            const c = new Chart(canvas, {
                type: 'pie',
                data: {
                    labels: byCat.map(d => d.category || '未分类'),
                    datasets: [{ data: byCat.map(d => d.count || 0), backgroundColor: PALETTE }],
                },
                options: {
                    responsive: true,
                    maintainAspectRatio: false,
                    plugins: {
                        legend: { position: 'right', labels: { font: { size: 12 } } },
                        tooltip: { callbacks: { label: ctx => `${ctx.label}: ${ctx.parsed}` } },
                    },
                },
            });
            _charts.push(c);
        }
    }
}

// ── tab switching ─────────────────────────────────────────────────────────────

async function activateTab(tabKey) {
    if (!_container) return;

    // Update tab button styles
    _container.querySelectorAll('.stats-tab-btn').forEach(btn => {
        btn.classList.toggle('active', btn.dataset.tab === tabKey);
    });

    // Show/hide panels
    _container.querySelectorAll('.stats-tab-panel').forEach(panel => {
        panel.classList.toggle('active', panel.id === `stats-panel-${tabKey}`);
    });

    _activeTab = tabKey;

    // Destroy charts before loading new tab
    destroyAllCharts();

    const panelEl = document.getElementById(`stats-panel-${tabKey}`);
    if (!panelEl) return;

    // Lazy load: only render when first activated or range changes
    if (!_tabLoaded[tabKey]) {
        _tabLoaded[tabKey] = true;
        if (tabKey === 'ledger')  await renderLedgerTab(panelEl);
        if (tabKey === 'tasks')   await renderTasksTab(panelEl);
        if (tabKey === 'events')  await renderEventsTab(panelEl);

        // Attach download button listeners for new charts
        attachDownloadListeners(panelEl);
    }
}

function attachDownloadListeners(root) {
    root.querySelectorAll('.stats-dl-btn').forEach(btn => {
        btn.addEventListener('click', (e) => {
            e.stopPropagation();
            const canvas = document.getElementById(btn.dataset.canvas);
            if (canvas) downloadChart(canvas, btn.dataset.filename || 'chart');
        });
    });
}

async function reloadCurrentTab() {
    if (!_container) return;
    _tabLoaded[_activeTab] = false;
    destroyAllCharts();
    const panelEl = document.getElementById(`stats-panel-${_activeTab}`);
    if (panelEl) {
        panelEl.innerHTML = `<div class="empty-state" style="padding:60px 0;"><p>加载中...</p></div>`;
    }
    await activateTab(_activeTab);
}

// ── page module exports ───────────────────────────────────────────────────────

export function render(container) {
    _container = container;
    _tabLoaded = {};

    injectStyles();

    _container.innerHTML = buildSkeleton();

    // Tab button events
    _container.querySelectorAll('.stats-tab-btn').forEach(btn => {
        btn.addEventListener('click', () => activateTab(btn.dataset.tab));
    });

    // Range button events
    _container.querySelectorAll('.stats-range-btn').forEach(btn => {
        btn.addEventListener('click', () => {
            if (btn.dataset.range === _range) return;
            _range = btn.dataset.range;
            _container.querySelectorAll('.stats-range-btn').forEach(b => {
                b.classList.toggle('active', b.dataset.range === _range);
            });
            // Invalidate all loaded tabs so they reload with new range
            _tabLoaded = {};
            reloadCurrentTab();
        });
    });

    // Load the initial active tab
    activateTab(_activeTab);
}

export function destroy() {
    destroyAllCharts();
    _container = null;
    _tabLoaded = {};
}

export function onRouteEnter(_params) {
    // Nothing special needed; render() is called by router
}
