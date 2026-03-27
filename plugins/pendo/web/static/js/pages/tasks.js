import { api } from '../api.js';
import { showToast } from '../components/toast.js';
import { showModal, closeModal, showConfirmModal } from '../components/modal.js';
import { buildFormHTML, getFormData, initFormInteractions } from '../components/form.js';
import { renderCustomSelect, initCustomSelects } from '../components/custom_select.js';
import { BREAKPOINTS, injectStyles, mediaMax, pageShellCss } from '../utils/ui.js';

const CSS_ID = 'pendo-tasks-redesign-styles';
const TODAY = () => new Date();

const STATUS_META = {
    todo: { label: '待办', tone: '#F59E0B', bg: 'rgba(245,158,11,0.12)' },
    in_progress: { label: '进行中', tone: '#0F766E', bg: 'rgba(16,185,129,0.12)' },
    done: { label: '已完成', tone: '#16A34A', bg: 'rgba(34,197,94,0.12)' },
    cancelled: { label: '已取消', tone: '#64748B', bg: 'rgba(148,163,184,0.14)' },
};

const PRIORITY_INFO = {
    1: { icon: '🔴', label: '紧急', color: '#EF4444' },
    2: { icon: '🟠', label: '高', color: '#F97316' },
    3: { icon: '🟡', label: '中', color: '#EAB308' },
    4: { icon: '🟢', label: '低', color: '#22C55E' },
    5: { icon: '⚪', label: '最低', color: '#CBD5E1' },
};

const TASK_FIELDS = [
    { name: 'title', label: '标题', type: 'text', required: true },
    { name: 'priority', label: '优先级', type: 'priority', value: 3 },
    {
        name: 'status',
        label: '状态',
        type: 'select',
        options: [
            { value: 'todo', label: '待办' },
            { value: 'in_progress', label: '进行中' },
            { value: 'done', label: '已完成' },
            { value: 'cancelled', label: '已取消' },
        ],
        selectThemeClass: 'pselect-theme-tasks',
    },
    { name: 'due_time', label: '截止时间', type: 'datetime' },
    { name: 'category', label: '分类', type: 'text', placeholder: '未分类' },
    { name: 'content', label: '备注', type: 'textarea' },
];

let _container = null;
let _dataChangedHandler = null;
let _viewMode = 'list';
let _loading = false;
let _overview = null;
let _dragTaskId = null;
let _filters = {
    search: '',
    category: '',
    priority: '',
    status: '',
};

function pad(number) {
    return String(number).padStart(2, '0');
}

function startOfDay(value) {
    const day = new Date(value);
    day.setHours(0, 0, 0, 0);
    return day;
}

function dateKey(value) {
    const day = value instanceof Date ? value : new Date(value);
    return `${day.getFullYear()}-${pad(day.getMonth() + 1)}-${pad(day.getDate())}`;
}

function parseDate(value) {
    if (!value) return null;
    const date = new Date(value);
    if (Number.isNaN(date.getTime())) return null;
    return date;
}

function formatShortDate(value) {
    const date = parseDate(value);
    if (!date) return '未安排日期';
    return `${date.getMonth() + 1}/${date.getDate()} ${pad(date.getHours())}:${pad(date.getMinutes())}`;
}

function toDatetimeLocal(value) {
    if (!value) return '';
    return String(value).slice(0, 16);
}

function isOverdue(task, today = startOfDay(TODAY())) {
    if (!task?.due_time) return false;
    if (!['todo', 'in_progress'].includes(task.status)) return false;
    const due = parseDate(task.due_time);
    return due ? startOfDay(due) < today : false;
}

function taskSortKey(task) {
    const priority = Number(task.priority || 99);
    const due = task.due_time || '9999-12-31T23:59:59';
    return [priority, due, task.created_at || ''];
}

function sortTasks(tasks) {
    return [...tasks].sort((a, b) => {
        const ak = taskSortKey(a);
        const bk = taskSortKey(b);
        for (let i = 0; i < ak.length; i += 1) {
            if (ak[i] < bk[i]) return -1;
            if (ak[i] > bk[i]) return 1;
        }
        return 0;
    });
}

function sortDone(tasks) {
    return [...tasks].sort((a, b) => {
        const at = a.completed_at || a.updated_at || '';
        const bt = b.completed_at || b.updated_at || '';
        return bt.localeCompare(at);
    });
}

function filteredTasks() {
    const tasks = _overview?.all_tasks || [];
    const keyword = (_filters.search || '').trim().toLowerCase();
    return tasks.filter((task) => {
        if (_filters.category && (task.category || '未分类') !== _filters.category) return false;
        if (_filters.priority && String(task.priority || '') !== String(_filters.priority)) return false;
        if (_filters.status === 'active' && !['todo', 'in_progress'].includes(task.status)) return false;
        if (_filters.status && _filters.status !== 'active' && task.status !== _filters.status) return false;
        if (!keyword) return true;
        const haystack = [task.title || '', task.content || '', task.category || ''].join('\n').toLowerCase();
        return haystack.includes(keyword);
    });
}

function deriveDisplayModel(tasks) {
    const today = startOfDay(TODAY());
    const active = tasks.filter((task) => ['todo', 'in_progress'].includes(task.status));
    const done = tasks.filter((task) => task.status === 'done');
    const cancelled = tasks.filter((task) => task.status === 'cancelled');
    const overdueTasks = [];
    const focusTasks = [];
    const upNextTasks = [];
    const laterTasks = [];
    const backlogTasks = [];

    active.forEach((task) => {
        const due = parseDate(task.due_time);
        const dueDay = due ? startOfDay(due) : null;
        if (dueDay && dueDay < today) {
            overdueTasks.push(task);
            focusTasks.push(task);
        } else if (dueDay && dueDay.getTime() === today.getTime()) {
            focusTasks.push(task);
        } else if (dueDay && dueDay <= new Date(today.getTime() + 7 * 86400000)) {
            upNextTasks.push(task);
        } else if (dueDay) {
            laterTasks.push(task);
        } else {
            backlogTasks.push(task);
        }
    });

    const focusSorted = sortTasks(focusTasks).sort((a, b) => {
        const aOverdue = isOverdue(a, today) ? 0 : 1;
        const bOverdue = isOverdue(b, today) ? 0 : 1;
        if (aOverdue !== bOverdue) return aOverdue - bOverdue;
        return 0;
    });

    const doneTodayCount = done.filter((task) => {
        const completed = parseDate(task.completed_at || task.updated_at);
        return completed ? startOfDay(completed).getTime() === today.getTime() : false;
    }).length;

    const completionDays = Array.from({ length: 7 }, (_, index) => {
        const day = new Date(today.getTime() - (6 - index) * 86400000);
        const key = dateKey(day);
        const count = done.filter((task) => dateKey(task.completed_at || task.updated_at) === key).length;
        return { date: key, label: `${day.getMonth() + 1}/${day.getDate()}`, count };
    });

    const categoryMap = new Map();
    active.forEach((task) => {
        const category = task.category || '未分类';
        categoryMap.set(category, (categoryMap.get(category) || 0) + 1);
    });
    const categoryLoad = [...categoryMap.entries()]
        .sort((a, b) => b[1] - a[1])
        .slice(0, 6)
        .map(([category, count]) => ({
            category,
            count,
            share: active.length ? count / active.length : 0,
        }));

    return {
        summary: {
            active_count: active.length,
            focus_count: focusSorted.length,
            overdue_count: overdueTasks.length,
            done_today_count: doneTodayCount,
            done_count: done.length,
            cancelled_count: cancelled.length,
            completion_rate: (active.length + done.length) ? done.length / (active.length + done.length) : 0,
        },
        focus_tasks: focusSorted,
        up_next_tasks: sortTasks(upNextTasks),
        later_tasks: sortTasks(laterTasks),
        backlog_tasks: sortTasks(backlogTasks),
        done_recent: sortDone(done).slice(0, 8),
        overdue_tasks: sortTasks(overdueTasks),
        category_load: categoryLoad,
        completion_bars: completionDays,
        board_columns: {
            todo: sortTasks(tasks.filter((task) => task.status === 'todo')),
            in_progress: sortTasks(tasks.filter((task) => task.status === 'in_progress')),
            done: sortDone(tasks.filter((task) => task.status === 'done')),
            cancelled: sortDone(tasks.filter((task) => task.status === 'cancelled')),
        },
    };
}

function ensureStyles() {
    injectStyles(CSS_ID, `
        ${pageShellCss('tasks-shell', { compactPadding: '20px 16px 30px', compactBreakpoint: BREAKPOINTS.MOBILE })}
        .tasks-hero {
            display: grid; grid-template-columns: minmax(0, 1fr) auto; gap: 18px; align-items: center;
            padding: 24px 26px; border-radius: 26px; margin-bottom: 18px;
            background:
                radial-gradient(circle at top right, rgba(16,185,129,0.18), transparent 32%),
                radial-gradient(circle at bottom left, rgba(5,150,105,0.14), transparent 24%),
                linear-gradient(145deg, rgba(255,255,255,0.98), rgba(236,253,245,0.94));
            border: 1px solid rgba(16,185,129,0.16);
            box-shadow: 0 18px 40px rgba(15,23,42,0.05);
        }
        .tasks-hero h2 { margin: 0; font-size: 28px; font-weight: 800; color: var(--color-tasks); letter-spacing: -0.02em; }
        .tasks-hero p { margin: 8px 0 0; font-size: 14px; line-height: 1.75; color: var(--color-text-secondary); }
        .tasks-hero-tags { display: flex; gap: 8px; flex-wrap: wrap; margin-top: 14px; }
        .tasks-hero-tag {
            display: inline-flex; align-items: center; gap: 6px; height: 34px; padding: 0 14px; border-radius: 999px;
            background: rgba(16,185,129,0.08); color: #047857; font-size: 12px; font-weight: 700;
        }
        .tasks-hero-actions { display: flex; gap: 10px; flex-wrap: wrap; justify-content: flex-end; align-items: center; }
        .tasks-view-toggle {
            display: inline-flex; gap: 4px; padding: 4px; border-radius: 999px;
            background: rgba(255,255,255,0.88); border: 1px solid rgba(226,232,240,0.92);
        }
        .tasks-toggle-btn {
            border: none; background: transparent; color: var(--color-text-secondary); cursor: pointer;
            border-radius: 999px; padding: 9px 14px; font-size: 13px; font-weight: 700; transition: all 0.16s ease;
        }
        .tasks-toggle-btn.active { background: var(--color-tasks); color: #fff; box-shadow: 0 10px 24px rgba(16,185,129,0.24); }
        .tasks-layout {
            display: grid; grid-template-columns: minmax(0, 1.2fr) minmax(280px, 0.8fr);
            gap: 16px; margin-bottom: 18px;
        }
        .tasks-panel {
            background: linear-gradient(180deg, rgba(255,255,255,0.98), rgba(248,250,252,0.95));
            border: 1px solid rgba(226,232,240,0.92); border-radius: 20px;
            box-shadow: 0 16px 34px rgba(15,23,42,0.04);
        }
        .tasks-panel-head { display: flex; align-items: center; justify-content: space-between; gap: 12px; padding: 18px 20px 0; }
        .tasks-panel-head h3 { margin: 0; font-size: 18px; font-weight: 760; color: var(--color-text); letter-spacing: -0.02em; }
        .tasks-panel-head p { margin: 6px 0 0; font-size: 13px; color: var(--color-text-secondary); }
        .tasks-panel-body { padding: 16px 20px 20px; }
        .tasks-stat-grid { display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 12px; }
        .tasks-stat-card {
            padding: 16px; border-radius: 16px; background: rgba(255,255,255,0.86);
            border: 1px solid rgba(16,185,129,0.08);
        }
        .tasks-stat-label { font-size: 12px; font-weight: 700; color: var(--color-text-secondary); }
        .tasks-stat-value { margin-top: 8px; font-size: 30px; font-weight: 800; color: var(--color-text); letter-spacing: -0.03em; }
        .tasks-stat-meta { margin-top: 8px; font-size: 12px; color: var(--color-text-secondary); }
        .tasks-meter {
            display: grid; grid-template-columns: repeat(7, minmax(0, 1fr)); gap: 10px; align-items: end; margin-top: 18px;
        }
        .tasks-meter-bar { display: flex; flex-direction: column; gap: 8px; align-items: center; }
        .tasks-meter-stick {
            width: 100%; min-height: 12px; border-radius: 999px 999px 12px 12px;
            background: linear-gradient(180deg, rgba(16,185,129,0.92), rgba(5,150,105,0.45));
        }
        .tasks-meter-value { font-size: 11px; font-weight: 700; color: var(--color-text); }
        .tasks-meter-label { font-size: 11px; color: var(--color-text-secondary); }
        .tasks-category-list { display: flex; flex-direction: column; gap: 12px; }
        .tasks-category-row { display: grid; grid-template-columns: minmax(0, 1fr) auto; gap: 10px; }
        .tasks-category-top { display: flex; justify-content: space-between; gap: 8px; font-size: 13px; }
        .tasks-category-name { font-weight: 700; color: var(--color-text); overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
        .tasks-category-count { color: var(--color-text-secondary); font-weight: 700; }
        .tasks-category-track { height: 10px; border-radius: 999px; background: rgba(226,232,240,0.7); overflow: hidden; }
        .tasks-category-fill { height: 100%; border-radius: inherit; background: linear-gradient(90deg, rgba(16,185,129,0.88), rgba(52,211,153,0.55)); }
        .tasks-filter-bar {
            display: grid; grid-template-columns: minmax(0, 1.3fr) repeat(3, minmax(140px, 0.6fr));
            gap: 12px; margin-bottom: 18px; padding: 16px 18px; border-radius: 22px;
            background: linear-gradient(180deg, rgba(255,255,255,0.98), rgba(248,250,252,0.94));
            border: 1px solid rgba(226,232,240,0.92); box-shadow: 0 14px 30px rgba(15,23,42,0.04);
        }
        .tasks-filter-field { display: flex; flex-direction: column; gap: 6px; }
        .tasks-filter-field label { font-size: 11px; font-weight: 700; color: var(--color-text-secondary); letter-spacing: 0.04em; text-transform: uppercase; }
        .tasks-filter-field input { width: 100%; height: 40px; border-radius: 14px; border: 1px solid rgba(203,213,225,0.92); padding: 0 14px; }
        .tasks-filter-bar .pselect-trigger { height: 40px; padding: 0 14px; border-radius: 14px; background: rgba(255,255,255,0.92); }
        .tasks-filter-bar .pselect-label { min-width: 0; }
        .tasks-workspace {
            background: linear-gradient(180deg, rgba(255,255,255,0.98), rgba(248,250,252,0.95));
            border: 1px solid rgba(226,232,240,0.92); border-radius: 22px; box-shadow: 0 18px 34px rgba(15,23,42,0.04);
            overflow: hidden;
        }
        .tasks-workspace-head { display: flex; align-items: center; justify-content: space-between; gap: 12px; padding: 18px 20px; border-bottom: 1px solid rgba(226,232,240,0.75); }
        .tasks-workspace-title { margin: 0; font-size: 18px; font-weight: 760; color: var(--color-text); }
        .tasks-workspace-subtitle { margin: 6px 0 0; font-size: 13px; color: var(--color-text-secondary); }
        .tasks-content { padding: 18px 20px 22px; }
        .tasks-sections { display: flex; flex-direction: column; gap: 16px; }
        .tasks-section {
            border: 1px solid rgba(226,232,240,0.85); border-radius: 18px;
            background: rgba(255,255,255,0.9); padding: 16px;
        }
        .tasks-section-head { display: flex; align-items: center; justify-content: space-between; gap: 10px; margin-bottom: 12px; }
        .tasks-section-title { font-size: 16px; font-weight: 760; color: var(--color-text); }
        .tasks-section-meta { font-size: 12px; color: var(--color-text-secondary); }
        .tasks-task-list { display: flex; flex-direction: column; gap: 10px; }
        .task-row {
            display: grid; grid-template-columns: auto minmax(0, 1fr) auto; gap: 14px; align-items: start;
            padding: 14px; border-radius: 16px; border: 1px solid rgba(226,232,240,0.82);
            background: linear-gradient(180deg, rgba(255,255,255,0.96), rgba(248,250,252,0.92));
            cursor: pointer; transition: transform 0.16s ease, box-shadow 0.16s ease, border-color 0.16s ease;
        }
        .task-row:hover { transform: translateY(-1px); border-color: rgba(16,185,129,0.28); box-shadow: 0 12px 28px rgba(16,185,129,0.08); }
        .task-row-priority {
            width: 12px; height: 12px; border-radius: 999px; margin-top: 6px; box-shadow: 0 0 0 4px rgba(255,255,255,0.92);
        }
        .task-row-title { margin: 0; font-size: 15px; font-weight: 760; color: var(--color-text); }
        .task-row-note { margin-top: 6px; font-size: 13px; line-height: 1.6; color: var(--color-text-secondary); display: -webkit-box; -webkit-line-clamp: 2; -webkit-box-orient: vertical; overflow: hidden; }
        .task-row-meta { display: flex; flex-wrap: wrap; gap: 8px; margin-top: 10px; }
        .task-pill {
            display: inline-flex; align-items: center; gap: 6px; height: 28px; padding: 0 10px; border-radius: 999px;
            font-size: 12px; font-weight: 700; background: rgba(241,245,249,0.92); color: var(--color-text-secondary);
        }
        .task-pill.status { color: var(--task-pill-color); background: var(--task-pill-bg); }
        .task-row-actions { display: flex; gap: 8px; align-items: center; }
        .task-action-btn {
            border: 1px solid rgba(203,213,225,0.9); background: #fff; color: var(--color-text-secondary);
            border-radius: 12px; padding: 8px 10px; font-size: 12px; font-weight: 700; cursor: pointer;
        }
        .task-action-btn.primary { color: #047857; border-color: rgba(16,185,129,0.24); background: rgba(236,253,245,0.9); }
        .tasks-empty {
            padding: 28px 18px; border-radius: 18px; text-align: center; background: rgba(248,250,252,0.82);
            border: 1px dashed rgba(148,163,184,0.26); color: var(--color-text-secondary);
        }
        .tasks-board { display: grid; grid-template-columns: repeat(4, minmax(240px, 1fr)); gap: 14px; overflow-x: auto; padding-bottom: 4px; }
        .tasks-board-col {
            min-width: 240px; border-radius: 18px; border: 1px solid rgba(226,232,240,0.92);
            background: linear-gradient(180deg, rgba(255,255,255,0.96), rgba(248,250,252,0.92));
        }
        .tasks-board-head { display: flex; align-items: center; justify-content: space-between; gap: 10px; padding: 14px 16px; border-bottom: 1px solid rgba(226,232,240,0.75); }
        .tasks-board-title { display: inline-flex; align-items: center; gap: 8px; font-size: 14px; font-weight: 760; color: var(--color-text); }
        .tasks-board-dot { width: 10px; height: 10px; border-radius: 999px; }
        .tasks-board-count { min-width: 24px; height: 24px; padding: 0 8px; border-radius: 999px; display: inline-flex; align-items: center; justify-content: center; background: rgba(241,245,249,0.92); color: var(--color-text-secondary); font-size: 12px; font-weight: 700; }
        .tasks-board-list { min-height: 160px; padding: 12px; display: flex; flex-direction: column; gap: 10px; }
        .tasks-board-list.drag-over { background: rgba(16,185,129,0.05); border-radius: 0 0 22px 22px; }
        .tasks-board-card {
            border: 1px solid rgba(226,232,240,0.86); border-radius: 14px; background: #fff; padding: 12px;
            cursor: pointer; transition: box-shadow 0.16s ease, transform 0.16s ease; position: relative;
        }
        .tasks-board-card:hover { transform: translateY(-1px); box-shadow: 0 10px 22px rgba(15,23,42,0.08); }
        .tasks-board-card.dragging { opacity: 0.44; }
        .tasks-board-card h4 { margin: 0; font-size: 14px; font-weight: 760; color: var(--color-text); }
        .tasks-board-card p { margin: 8px 0 0; font-size: 12px; line-height: 1.6; color: var(--color-text-secondary); display: -webkit-box; -webkit-line-clamp: 2; -webkit-box-orient: vertical; overflow: hidden; }
        .tasks-board-meta { display: flex; flex-wrap: wrap; gap: 6px; margin-top: 10px; }
        .tasks-board-add { padding: 0 12px 12px; }
        .tasks-board-add button {
            width: 100%; border: 1px dashed rgba(148,163,184,0.5); background: transparent; color: var(--color-text-secondary);
            border-radius: 14px; padding: 10px 12px; cursor: pointer; font-weight: 700;
        }
        ${mediaMax(BREAKPOINTS.WIDE, `
            .tasks-layout { grid-template-columns: 1fr; }
            .tasks-filter-bar { grid-template-columns: repeat(2, minmax(0, 1fr)); }
            .tasks-board { grid-template-columns: repeat(2, minmax(240px, 1fr)); }
        `)}
        ${mediaMax(BREAKPOINTS.MOBILE, `
            .tasks-hero { grid-template-columns: 1fr; }
            .tasks-hero-actions { justify-content: flex-start; }
            .tasks-layout { gap: 14px; }
            .tasks-stat-grid { grid-template-columns: 1fr; }
            .tasks-filter-bar { grid-template-columns: 1fr; }
            .tasks-workspace-head { align-items: start; }
            .task-row { grid-template-columns: auto minmax(0, 1fr); }
            .task-row-actions { grid-column: 2; justify-content: flex-start; flex-wrap: wrap; }
            .tasks-board { grid-template-columns: 1fr; }
        `)}
    `);
}

async function fetchOverview() {
    const res = await api.get('/stats/tasks/overview', { today: dateKey(TODAY()) });
    return res.data || null;
}

function renderCategoryOptions(tasks) {
    const categories = [...new Set(tasks.map((task) => task.category || '未分类'))].sort();
    return [{ value: '', label: '全部分类' }, ...categories.map((category) => ({ value: category, label: category }))];
}

function renderPriorityOptions() {
    return [
        { value: '', label: '全部优先级' },
        { value: '1', label: '🔴 紧急' },
        { value: '2', label: '🟠 高' },
        { value: '3', label: '🟡 中' },
        { value: '4', label: '🟢 低' },
        { value: '5', label: '⚪ 最低' },
    ];
}

function renderStatusOptions() {
    return [
        { value: '', label: '全部状态' },
        { value: 'active', label: '当前推进' },
        { value: 'todo', label: '待办' },
        { value: 'in_progress', label: '进行中' },
        { value: 'done', label: '已完成' },
        { value: 'cancelled', label: '已取消' },
    ];
}

function renderHero(model) {
    const summary = model.summary;
    return `
        <section class="tasks-hero">
            <div>
                <h2>✅ 待办</h2>
                <p>集中查看当前任务、接下来要跟进的事项和最近完成项。</p>
                <div class="tasks-hero-tags">
                    <span class="tasks-hero-tag">${summary.focus_count} 项今日聚焦</span>
                    <span class="tasks-hero-tag">${summary.overdue_count} 项截止风险</span>
                    <span class="tasks-hero-tag">${summary.done_today_count} 项今日完成</span>
                </div>
            </div>
            <div class="tasks-hero-actions">
                <div class="tasks-view-toggle">
                    <button class="tasks-toggle-btn ${_viewMode === 'list' ? 'active' : ''}" id="task-view-list" data-view="list">执行列表</button>
                    <button class="tasks-toggle-btn ${_viewMode === 'board' ? 'active' : ''}" id="task-view-board" data-view="board">状态看板</button>
                </div>
                <button class="btn btn-primary" id="tasks-add-top">＋ 新建任务</button>
            </div>
        </section>`;
}

function renderInsights(model) {
    const summary = model.summary;
    const completion = Math.round(summary.completion_rate * 100);
    const maxBar = Math.max(1, ...model.completion_bars.map((item) => item.count || 0));
    return `
        <section class="tasks-layout">
            <div class="tasks-panel">
                <div class="tasks-panel-head">
                    <div>
                        <h3>执行状态</h3>
                        <p>查看当前推进情况、截止风险和完成节奏。</p>
                    </div>
                    <div class="task-pill">${completion}% 完成率</div>
                </div>
                <div class="tasks-panel-body">
                    <div class="tasks-stat-grid">
                        <div class="tasks-stat-card">
                            <div class="tasks-stat-label">当前推进</div>
                            <div class="tasks-stat-value">${summary.active_count}</div>
                            <div class="tasks-stat-meta">包含待办和进行中的任务</div>
                        </div>
                        <div class="tasks-stat-card">
                            <div class="tasks-stat-label">今日聚焦</div>
                            <div class="tasks-stat-value">${summary.focus_count}</div>
                            <div class="tasks-stat-meta">逾期和今天到期会优先出现</div>
                        </div>
                        <div class="tasks-stat-card">
                            <div class="tasks-stat-label">截止风险</div>
                            <div class="tasks-stat-value">${summary.overdue_count}</div>
                            <div class="tasks-stat-meta">越早处理，列表会越干净</div>
                        </div>
                        <div class="tasks-stat-card">
                            <div class="tasks-stat-label">今日完成</div>
                            <div class="tasks-stat-value">${summary.done_today_count}</div>
                            <div class="tasks-stat-meta">最近完成 ${summary.done_count} 项</div>
                        </div>
                    </div>
                    <div class="tasks-meter">
                        ${model.completion_bars.map((item) => `
                            <div class="tasks-meter-bar">
                                <div class="tasks-meter-value">${item.count}</div>
                                <div class="tasks-meter-stick" style="height:${14 + (item.count / maxBar) * 66}px;"></div>
                                <div class="tasks-meter-label">${item.label}</div>
                            </div>`).join('')}
                    </div>
                </div>
            </div>
            <div class="tasks-panel">
                <div class="tasks-panel-head">
                    <div>
                        <h3>分类负载</h3>
                        <p>查看当前任务主要分布在哪些分类。</p>
                    </div>
                </div>
                <div class="tasks-panel-body">
                    ${model.category_load.length ? `
                        <div class="tasks-category-list">
                            ${model.category_load.map((item) => `
                                <div class="tasks-category-row">
                                    <div>
                                        <div class="tasks-category-top">
                                            <span class="tasks-category-name">${item.category}</span>
                                            <span class="tasks-category-count">${item.count} 项</span>
                                        </div>
                                        <div class="tasks-category-track">
                                            <div class="tasks-category-fill" style="width:${Math.max(10, Math.round(item.share * 100))}%;"></div>
                                        </div>
                                    </div>
                                    <div class="task-pill">${Math.round(item.share * 100)}%</div>
                                </div>`).join('')}
                        </div>` : `<div class="tasks-empty">当前没有进行中的任务，分类负载会在开始推进后出现。</div>`}
                </div>
            </div>
        </section>`;
}

function renderFilters(tasks) {
    return `
        <section class="tasks-filter-bar">
            <div class="tasks-filter-field">
                <label>搜索</label>
                <input id="tasks-filter-search" type="search" placeholder="标题、备注、分类" value="${_filters.search}">
            </div>
            <div class="tasks-filter-field">
                <label>分类</label>
                ${renderCustomSelect({
                    id: 'tasks-filter-category',
                    options: renderCategoryOptions(tasks),
                    selected: _filters.category,
                    className: 'pselect-block pselect-theme-tasks',
                    placeholder: '全部分类',
                })}
            </div>
            <div class="tasks-filter-field">
                <label>优先级</label>
                ${renderCustomSelect({
                    id: 'tasks-filter-priority',
                    options: renderPriorityOptions(),
                    selected: _filters.priority,
                    className: 'pselect-block pselect-theme-tasks',
                    placeholder: '全部优先级',
                })}
            </div>
            <div class="tasks-filter-field">
                <label>状态</label>
                ${renderCustomSelect({
                    id: 'tasks-filter-status',
                    options: renderStatusOptions(),
                    selected: _filters.status,
                    className: 'pselect-block pselect-theme-tasks',
                    placeholder: '全部状态',
                })}
            </div>
        </section>`;
}

function taskRowHTML(task) {
    const priority = PRIORITY_INFO[task.priority] || PRIORITY_INFO[3];
    const status = STATUS_META[task.status] || STATUS_META.todo;
    const timeSource = task.status === 'done' ? (task.completed_at || task.updated_at) : task.due_time;
    const dueLabel = timeSource ? formatShortDate(timeSource) : (task.status === 'done' ? '刚刚完成' : '无截止时间');
    const dueText = (task.status !== 'done' && isOverdue(task)) ? `已逾期 · ${dueLabel}` : dueLabel;
    const content = task.content ? `<div class="task-row-note">${task.content}</div>` : '';
    return `
        <div class="task-row" data-task-id="${task.id}">
            <div class="task-row-priority" style="background:${priority.color};"></div>
            <div>
                <h4 class="task-row-title">${task.title || '(无标题)'}</h4>
                ${content}
                <div class="task-row-meta">
                    <span class="task-pill">${priority.icon} ${priority.label}</span>
                    <span class="task-pill">${task.category || '未分类'}</span>
                    <span class="task-pill status" style="--task-pill-color:${status.tone};--task-pill-bg:${status.bg};">${status.label}</span>
                    <span class="task-pill">${task.status === 'done' ? '完成于' : '截止'} · ${dueText}</span>
                </div>
            </div>
            <div class="task-row-actions">
                ${task.status !== 'done' ? `<button class="task-action-btn primary" data-action="done" data-id="${task.id}">完成</button>` : `<button class="task-action-btn" data-action="resume" data-id="${task.id}">恢复</button>`}
                ${task.status === 'todo' ? `<button class="task-action-btn" data-action="start" data-id="${task.id}">开始</button>` : ''}
                <button class="task-action-btn" data-action="edit" data-id="${task.id}">编辑</button>
            </div>
        </div>`;
}

function renderSection(title, subtitle, tasks) {
    return `
        <section class="tasks-section">
            <div class="tasks-section-head">
                <div>
                    <div class="tasks-section-title">${title}</div>
                    <div class="tasks-section-meta">${subtitle}</div>
                </div>
                <div class="task-pill">${tasks.length} 项</div>
            </div>
            ${tasks.length
                ? `<div class="tasks-task-list">${tasks.map(taskRowHTML).join('')}</div>`
                : `<div class="tasks-empty">当前没有内容。</div>`}
        </section>`;
}

function renderListView(model) {
    return `
        <div class="tasks-sections">
            ${renderSection('今日聚焦', '先处理逾期和今天要收口的任务。', model.focus_tasks.slice(0, 6))}
            ${renderSection('接下来', '未来 7 天内需要留意的任务。', model.up_next_tasks.slice(0, 8))}
            ${renderSection('稍后与待排期', '更晚的任务和没有日期的事项。', [...model.later_tasks.slice(0, 4), ...model.backlog_tasks.slice(0, 4)])}
            ${renderSection('最近完成', '看一下最近是怎么收尾的。', model.done_recent.slice(0, 6))}
        </div>`;
}

function boardCardHTML(task) {
    const priority = PRIORITY_INFO[task.priority] || PRIORITY_INFO[3];
    return `
        <div class="tasks-board-card" data-task-id="${task.id}" draggable="true">
            <h4>${task.title || '(无标题)'}</h4>
            ${task.content ? `<p>${task.content}</p>` : ''}
            <div class="tasks-board-meta">
                <span class="task-pill">${priority.icon} ${priority.label}</span>
                <span class="task-pill">${task.category || '未分类'}</span>
                ${task.due_time ? `<span class="task-pill">${formatShortDate(task.due_time)}</span>` : ''}
            </div>
        </div>`;
}

function renderBoardView(model) {
    const columns = [
        { key: 'todo', label: '待办', color: '#F59E0B' },
        { key: 'in_progress', label: '进行中', color: '#10B981' },
        { key: 'done', label: '已完成', color: '#16A34A' },
        { key: 'cancelled', label: '已取消', color: '#94A3B8' },
    ];
    return `
        <div class="tasks-board">
            ${columns.map((column) => `
                <section class="tasks-board-col">
                    <div class="tasks-board-head">
                        <div class="tasks-board-title">
                            <span class="tasks-board-dot" style="background:${column.color};"></span>
                            ${column.label}
                        </div>
                        <span class="tasks-board-count">${model.board_columns[column.key].length}</span>
                    </div>
                    <div class="tasks-board-list" data-col="${column.key}">
                        ${model.board_columns[column.key].length
                            ? model.board_columns[column.key].map(boardCardHTML).join('')
                            : `<div class="tasks-empty">暂无任务</div>`}
                    </div>
                    ${column.key === 'todo' ? `<div class="tasks-board-add"><button id="tasks-add-board">＋ 添加任务</button></div>` : ''}
                </section>`).join('')}
        </div>`;
}

function renderWorkspace(model) {
    const subtitle = _viewMode === 'list'
        ? '按时间和优先级安排接下来要处理的事。'
        : '按状态查看全部任务。';
    return `
        <section class="tasks-workspace">
            <div class="tasks-workspace-head">
                <div>
                    <h3 class="tasks-workspace-title">${_viewMode === 'list' ? '执行列表' : '状态看板'}</h3>
                    <p class="tasks-workspace-subtitle">${subtitle}</p>
                </div>
                <div class="task-pill">${filteredTasks().length} 项匹配</div>
            </div>
            <div class="tasks-content">
                ${_viewMode === 'list' ? renderListView(model) : renderBoardView(model)}
            </div>
        </section>`;
}

function renderPage() {
    if (!_container) return;
    ensureStyles();

    if (_loading && !_overview) {
        _container.innerHTML = `<div class="tasks-shell"><div class="tasks-empty">正在加载待办...</div></div>`;
        return;
    }

    const tasks = filteredTasks();
    const model = deriveDisplayModel(tasks);

    _container.innerHTML = `
        <div class="tasks-shell">
            ${renderHero(model)}
            ${renderInsights(model)}
            ${renderFilters(_overview?.all_tasks || [])}
            ${renderWorkspace(model)}
        </div>
    `;

    attachListeners();
}

async function updateTaskStatus(taskId, status) {
    const task = (_overview?.all_tasks || []).find((item) => String(item.id) === String(taskId));
    if (!task) return;
    await api.put(`/items/${taskId}`, { status });
    showToast('任务状态已更新', 'success');
    window.dispatchEvent(new CustomEvent('pendo-data-changed', { detail: { type: 'task' } }));
    await loadAndRender();
}

function openTaskModal(existing = null) {
    const isEdit = Boolean(existing);
    const fields = TASK_FIELDS.map((field) => {
        let value = field.value ?? '';
        if (existing) {
            if (field.name === 'due_time') value = toDatetimeLocal(existing.due_time);
            else value = existing[field.name] ?? field.value ?? '';
        }
        return { ...field, value };
    });

    const content = showModal(isEdit ? '编辑任务' : '新建任务', `<form id="task-form">${buildFormHTML(fields)}</form>`, {
        footer: `
            ${isEdit ? `<button class="btn btn-danger btn-sm" id="task-delete" style="margin-right:auto;">删除</button>` : ''}
            <button class="btn btn-secondary" id="task-cancel">取消</button>
            <button class="btn btn-primary" id="task-save">保存</button>
        `,
    });

    initFormInteractions(content);
    content.querySelector('#task-cancel').onclick = closeModal;

    if (isEdit) {
        content.querySelector('#task-delete').onclick = async () => {
            closeModal();
            const confirmed = await showConfirmModal({
                title: '删除任务',
                message: `确定要删除“${existing.title || '这个任务'}”吗？删除后会从执行列表和看板里一起移除。`,
                confirmText: '删除',
                cancelText: '返回编辑',
                tone: 'danger',
            });
            if (!confirmed) {
                openTaskModal(existing);
                return;
            }
            try {
                await api.delete(`/items/${existing.id}`);
                showToast('任务已删除', 'success');
                window.dispatchEvent(new CustomEvent('pendo-data-changed', { detail: { type: 'task' } }));
                await loadAndRender();
            } catch (err) {
                showToast(`删除失败：${err.message}`, 'error');
            }
        };
    }

    content.querySelector('#task-save').onclick = async () => {
        const form = content.querySelector('#task-form');
        const payload = getFormData(form);
        if (!payload.title) {
            showToast('请填写标题', 'warning');
            return;
        }
        if (!payload.priority) payload.priority = 3;
        try {
            if (isEdit) {
                await api.put(`/items/${existing.id}`, payload);
                showToast('任务已更新', 'success');
            } else {
                await api.post('/items', { type: 'task', status: 'todo', ...payload });
                showToast('任务已创建', 'success');
            }
            closeModal();
            window.dispatchEvent(new CustomEvent('pendo-data-changed', { detail: { type: 'task' } }));
            await loadAndRender();
        } catch (err) {
            showToast(`保存失败：${err.message}`, 'error');
        }
    };
}

function attachListeners() {
    if (!_container) return;

    initCustomSelects(_container, {
        'tasks-filter-category': (value) => { _filters.category = value; renderPage(); },
        'tasks-filter-priority': (value) => { _filters.priority = value; renderPage(); },
        'tasks-filter-status': (value) => { _filters.status = value; renderPage(); },
    });

    _container.querySelectorAll('[data-view]').forEach((button) => {
        button.onclick = () => {
            _viewMode = button.dataset.view;
            renderPage();
        };
    });

    const addTop = _container.querySelector('#tasks-add-top');
    if (addTop) addTop.onclick = () => openTaskModal(null);
    const addBoard = _container.querySelector('#tasks-add-board');
    if (addBoard) addBoard.onclick = () => openTaskModal(null);

    const search = _container.querySelector('#tasks-filter-search');
    if (search) {
        search.onchange = () => {
            _filters.search = search.value || '';
            renderPage();
        };
    }

    _container.onclick = async (event) => {
        const actionButton = event.target.closest('[data-action]');
        if (actionButton) {
            event.stopPropagation();
            const taskId = actionButton.dataset.id;
            const action = actionButton.dataset.action;
            const task = (_overview?.all_tasks || []).find((item) => String(item.id) === String(taskId));
            if (!task) return;
            if (action === 'edit') {
                openTaskModal(task);
                return;
            }
            if (action === 'done') {
                await updateTaskStatus(taskId, 'done');
                return;
            }
            if (action === 'resume') {
                await updateTaskStatus(taskId, 'todo');
                return;
            }
            if (action === 'start') {
                await updateTaskStatus(taskId, 'in_progress');
            }
            return;
        }

        const taskCard = event.target.closest('[data-task-id]');
        if (taskCard) {
            const task = (_overview?.all_tasks || []).find((item) => String(item.id) === String(taskCard.dataset.taskId));
            if (task) openTaskModal(task);
        }
    };

    if (_viewMode === 'board') {
        _container.querySelectorAll('.tasks-board-card').forEach((card) => {
            card.addEventListener('dragstart', (event) => {
                _dragTaskId = card.dataset.taskId;
                card.classList.add('dragging');
                event.dataTransfer.effectAllowed = 'move';
            });
            card.addEventListener('dragend', () => {
                card.classList.remove('dragging');
                _dragTaskId = null;
            });
        });

        _container.querySelectorAll('.tasks-board-list').forEach((zone) => {
            zone.addEventListener('dragover', (event) => {
                event.preventDefault();
                zone.classList.add('drag-over');
            });
            zone.addEventListener('dragleave', (event) => {
                if (!zone.contains(event.relatedTarget)) zone.classList.remove('drag-over');
            });
            zone.addEventListener('drop', async (event) => {
                event.preventDefault();
                zone.classList.remove('drag-over');
                const status = zone.dataset.col;
                if (!_dragTaskId || !status) return;
                await updateTaskStatus(_dragTaskId, status);
            });
        });
    }
}

async function loadAndRender() {
    _loading = true;
    renderPage();
    try {
        _overview = await fetchOverview();
    } catch (err) {
        _overview = null;
        showToast(`加载任务失败：${err.message}`, 'error');
    } finally {
        _loading = false;
    }
    renderPage();
}

export function render(container) {
    _container = container;
    _overview = null;
    _loading = false;
    _viewMode = 'list';
    _filters = { search: '', category: '', priority: '', status: '' };
    renderPage();
    loadAndRender();
    _dataChangedHandler = async (event) => {
        const changedType = event?.detail?.type;
        if (changedType && changedType !== 'task') return;
        await loadAndRender();
    };
    window.addEventListener('pendo-data-changed', _dataChangedHandler);
}

export function destroy() {
    if (_dataChangedHandler) {
        window.removeEventListener('pendo-data-changed', _dataChangedHandler);
        _dataChangedHandler = null;
    }
    _container = null;
    _overview = null;
}

export function onRouteEnter(_params) {}
