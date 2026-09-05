import { api } from '../api.js';
import { showToast } from '../components/toast.js';
import { showModal, closeModal, showConfirmModal, safeHtml } from '../components/modal.js';
import { buildFormHTML, getFormData, initFormInteractions } from '../components/form.js';
import { renderCustomSelect, initCustomSelects } from '../components/custom_select.js';
import {
    errorMessage,
    isRecord,
    pad2,
    parseDate,
    trimmedTextValue as textValue,
} from '../utils/format.js';
import {
    fetchUserTimeZone,
    todayInUserTimeZone,
    zonedDateKey,
    zonedDateParts,
    zonedDateTimeToInput,
    zonedInputToUtcIso,
} from '../utils/timezone.js';
import { BREAKPOINTS, escapeHtml, injectStyles, mediaMax, pageShellCss, subscribeDataChanges } from '../utils/ui.js';

const CSS_ID             = 'pendo-tasks-redesign-styles';
const TODAY              = () => parseDate(todayInUserTimeZone());
const ISO_DATE_RE        = /^\d{4}-\d{2}-\d{2}$/;
const TASK_STATUSES      = new Set(['open', 'done', 'cancelled']);
const VIEW_MODES         = new Set(['list', 'board']);
const PLAN_FILTER_VALUES = new Set(['', 'today', 'week', 'month', 'future', 'undated', 'custom']);
const DEFAULT_FILTERS    = Object.freeze({
    search: '',
    plan: '',
    category: '',
    status: '',
    customStart: '',
    customEnd: '',
});

const STATUS_META = {
    open: { label: '未完成', tone: '#F59E0B', bg: 'rgba(245,158,11,0.12)' },
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
            { value: 'open', label: '未完成' },
            { value: 'done', label: '已完成' },
            { value: 'cancelled', label: '已取消' },
        ],
        selectThemeClass: 'pselect-theme-tasks',
    },
    { name: 'plan_date', label: '计划日期', type: 'date' },
    { name: 'deadline_at', label: '截止时间', type: 'datetime' },
    { name: 'category', label: '分类', type: 'text', placeholder: '例如：工作、学习' },
    { name: 'content', label: '备注', type: 'textarea' },
];
const PLAN_FILTER_OPTIONS = [
    { value: '', label: '全部计划日期' },
    { value: 'today', label: '今天' },
    { value: 'week', label: '本周' },
    { value: 'month', label: '本月' },
    { value: 'future', label: '更晚' },
    { value: 'undated', label: '未安排日期' },
    { value: 'custom', label: '自定义时间段' },
];
const STATUS_FILTER_OPTIONS = [
    { value: '', label: '全部状态' },
    { value: 'open', label: '未完成' },
    { value: 'done', label: '已完成' },
    { value: 'cancelled', label: '已取消' },
];

let _container              = null;
let _unsubscribeDataChanges = null;
let _viewMode               = 'list';
let _loading                = false;
let _loadError              = '';
let _overview               = null;
let _dragTaskId             = null;
let _filters                = { ...DEFAULT_FILTERS };
let _loadVersion            = 0;
const _pendingTaskIds       = new Set();

// 数据与错误边界：接口损坏记录不能进入筛选、排序或动作路径。
function idValue(value) {
    return value === null || value === undefined ? '' : String(value).trim();
}

// 所有日期桶都按用户时区自然日计算，纯日期保持原有日期语义。
function startOfDay(value) {
    const day = parseDate(value);
    if (!day) return null;
    day.setHours(0, 0, 0, 0);
    return day;
}

function dateKey(value) {
    const day = parseDate(value);
    if (!day) return '';
    return `${day.getFullYear()}-${pad2(day.getMonth() + 1)}-${pad2(day.getDate())}`;
}

function firstDayOfWeek(value = TODAY()) {
    const day = startOfDay(value) || startOfDay(TODAY());
    day.setDate(day.getDate() - ((day.getDay() + 6) % 7));
    return day;
}

function lastDayOfWeek(value = TODAY()) {
    const day = firstDayOfWeek(value);
    day.setDate(day.getDate() + 6);
    return day;
}

function firstDayOfMonth(value = TODAY()) {
    const day = startOfDay(value) || startOfDay(TODAY());
    return new Date(day.getFullYear(), day.getMonth(), 1);
}

function lastDayOfMonth(value = TODAY()) {
    const day = startOfDay(value) || startOfDay(TODAY());
    return new Date(day.getFullYear(), day.getMonth() + 1, 0);
}

function parseIsoDate(value) {
    const text = textValue(value);
    if (!ISO_DATE_RE.test(text)) return null;
    const date = parseDate(text);
    return date && dateKey(date) === text ? date : null;
}

function isIsoDate(value) {
    return Boolean(parseIsoDate(value));
}

function taskTextCategory(task) {
    const category = String(task?.category || '').trim();
    return category && category !== '未分类' ? category : '';
}

function taskPlanDateKey(task) {
    const plan = String(task?.plan_date || '').trim();
    if (isIsoDate(plan)) return plan;
    return zonedDateKey(task?.deadline_at);
}

function taskPrimaryStatus(task) {
    return TASK_STATUSES.has(task?.status) ? task.status : 'open';
}

function taskStatusBucket(task) {
    return ['done', 'cancelled'].includes(taskPrimaryStatus(task)) ? 'closed' : 'open';
}

function formatPlanDate(value) {
    const date = parseIsoDate(value);
    if (!date) return '未安排日期';
    return `${date.getMonth() + 1}/${date.getDate()}`;
}

function describePlanDate(value, todayKey = dateKey(TODAY())) {
    if (!value) return '未安排日期';
    if (value === todayKey) return `今天 · ${formatPlanDate(value)}`;
    if (value < todayKey) return `已滞后 · ${formatPlanDate(value)}`;
    return `计划 · ${formatPlanDate(value)}`;
}

function normalizePlanRange(start, end) {
    const safeStart = isIsoDate(start) ? start : '';
    const safeEnd   = isIsoDate(end) ? end : '';
    if (!safeStart || !safeEnd) return { start: safeStart, end: safeEnd };
    return safeStart <= safeEnd ? { start: safeStart, end: safeEnd } : { start: safeEnd, end: safeStart };
}

function defaultCustomPlanRange() {
    return {
        start: dateKey(firstDayOfMonth(TODAY())),
        end: dateKey(lastDayOfMonth(TODAY())),
    };
}

function planDateMatches(task, filterValue, todayKey = dateKey(TODAY()), customStart = '', customEnd = '') {
    if (!filterValue) return true;
    if (!PLAN_FILTER_VALUES.has(filterValue)) return false;

    const planKey = taskPlanDateKey(task);
    if (filterValue === 'undated') return !planKey;
    if (!planKey) return false;

    const anchor     = parseIsoDate(todayKey) || startOfDay(TODAY());
    const anchorKey  = dateKey(anchor);
    const weekStart  = dateKey(firstDayOfWeek(anchor));
    const weekEnd    = dateKey(lastDayOfWeek(anchor));
    const monthStart = dateKey(firstDayOfMonth(anchor));
    const monthEnd   = dateKey(lastDayOfMonth(anchor));
    if (filterValue === 'today') return planKey === anchorKey;
    if (filterValue === 'week') return planKey >= weekStart && planKey <= weekEnd;
    if (filterValue === 'month') return planKey >= monthStart && planKey <= monthEnd;
    if (filterValue === 'future') return planKey > monthEnd;
    if (filterValue === 'custom') {
        const range = normalizePlanRange(customStart, customEnd);
        if (!range.start || !range.end) return false;
        return planKey >= range.start && planKey <= range.end;
    }
    return false;
}

function formatShortDate(value) {
    const parts = zonedDateParts(value);
    if (!parts) return '未安排日期';
    return `${parts.month}/${parts.day} ${pad2(parts.hour)}:${pad2(parts.minute)}`;
}

function defaultTaskPlanDateValue(now = new Date()) {
    const parts  = zonedDateParts(now);
    const target = parseDate(zonedDateKey(now)) || TODAY();
    if ((parts?.hour ?? 0) >= 20) {
        target.setDate(target.getDate() + 1);
    }
    return dateKey(target);
}

function normalizeTaskPayload(formData, userTimeZone) {
    const source             = isRecord(formData) ? formData : {};
    const priority           = Number(source.priority);
    const status             = textValue(source.status);
    const planDate           = textValue(source.plan_date);
    const deadline           = textValue(source.deadline_at);
    const normalizedDeadline = deadline ? zonedInputToUtcIso(deadline, userTimeZone) : '';
    if (deadline && !normalizedDeadline) throw new RangeError('请输入有效的截止时间');

    return {
        title: textValue(source.title),
        content: textValue(source.content),
        category: textValue(source.category) || '未分类',
        status: TASK_STATUSES.has(status) ? status : 'open',
        priority: Number.isInteger(priority) && priority >= 1 && priority <= 5 ? priority : 3,
        plan_date: isIsoDate(planDate) ? planDate : null,
        deadline_at: normalizedDeadline || null,
    };
}

// 概览接口只需要 all_tasks；在这里统一字段类型并去重，后续渲染无需反复防御。
function normalizeTask(value) {
    if (!isRecord(value)) return null;

    const id = idValue(value.id);
    if (!id) return null;

    const priority = Number(value.priority);
    const status   = textValue(value.status);
    const planDate = textValue(value.plan_date);
    const deadline = textValue(value.deadline_at);
    const version  = Number(value.version);
    return {
        id,
        title: textValue(value.title),
        content: textValue(value.content),
        category: textValue(value.category) || '未分类',
        status: TASK_STATUSES.has(status) ? status : 'open',
        priority: Number.isInteger(priority) && priority >= 1 && priority <= 5 ? priority : 3,
        plan_date: isIsoDate(planDate) ? planDate : '',
        deadline_at: deadline && parseDate(deadline) ? deadline : '',
        completed_at: textValue(value.completed_at),
        cancelled_at: textValue(value.cancelled_at),
        created_at: textValue(value.created_at),
        updated_at: textValue(value.updated_at),
        version: Number.isInteger(version) && version >= 0 ? version : 0,
    };
}

function normalizeOverview(value) {
    const source  = isRecord(value) && Array.isArray(value.all_tasks) ? value.all_tasks : [];
    const tasks   = [];
    const seenIds = new Set();
    source.forEach((item) => {
        const task = normalizeTask(item);
        if (!task || seenIds.has(task.id)) return;
        seenIds.add(task.id);
        tasks.push(task);
    });
    return { all_tasks: tasks };
}

function itemPath(taskId) {
    return `/items/${encodeURIComponent(idValue(taskId))}`;
}

function emitTaskChanged() {
    window.dispatchEvent(new CustomEvent('pendo-data-changed', { detail: { type: 'task' } }));
}

function isOverdue(task, today = startOfDay(TODAY())) {
    if (taskStatusBucket(task) !== 'open') return false;
    const planDate = parseIsoDate(taskPlanDateKey(task));
    const anchor   = startOfDay(today) || startOfDay(TODAY());
    return planDate ? startOfDay(planDate) < anchor : false;
}

function taskSortKey(task) {
    const rawPriority = Number(task?.priority);
    const priority = Number.isInteger(rawPriority) && rawPriority >= 1 && rawPriority <= 5 ? rawPriority : 99;
    const plan = taskPlanDateKey(task) || '9999-12-31';
    const deadline = textValue(task?.deadline_at) || '9999-12-31T99:99:99';
    return [priority, plan, deadline, textValue(task?.created_at)];
}

function sortTasks(tasks) {
    const source = Array.isArray(tasks) ? tasks.filter(isRecord) : [];
    return [...source].sort((a, b) => {
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
    const source = Array.isArray(tasks) ? tasks.filter(isRecord) : [];
    return [...source].sort((a, b) => {
        const at = textValue(a.completed_at) || textValue(a.cancelled_at) || textValue(a.updated_at);
        const bt = textValue(b.completed_at) || textValue(b.cancelled_at) || textValue(b.updated_at);
        return bt.localeCompare(at);
    });
}

function filteredTasks() {
    const tasks         = Array.isArray(_overview?.all_tasks) ? _overview.all_tasks : [];
    const keyword       = textValue(_filters.search).toLowerCase();
    const todayKey      = dateKey(TODAY());
    const usePlanFilter = Boolean(_filters.plan);
    return tasks.filter((task) => {
        if (usePlanFilter) {
            if (!planDateMatches(task, _filters.plan, todayKey, _filters.customStart, _filters.customEnd)) return false;
        } else if (_filters.category && taskTextCategory(task) !== _filters.category) {
            return false;
        }
        if (_filters.status && taskPrimaryStatus(task) !== _filters.status) return false;
        if (!keyword) return true;
        const haystack = [
            task.title || '',
            task.content || '',
            task.category || '',
            taskTextCategory(task),
            taskPlanDateKey(task),
            task.deadline_at || '',
        ]
            .join('\n')
            .toLowerCase();
        return haystack.includes(keyword);
    });
}

// 筛选后在客户端重新分桶，保证列表、看板和概览数字使用同一份任务集合。
function deriveDisplayModel(tasks) {
    const source   = Array.isArray(tasks) ? tasks.filter(isRecord) : [];
    const today    = startOfDay(TODAY());
    const todayKey = dateKey(today);
    const next7End = new Date(today);
    next7End.setDate(next7End.getDate() + 7);
    const next7EndKey  = dateKey(next7End);
    const active       = source.filter((task) => taskStatusBucket(task) === 'open');
    const done         = source.filter((task) => taskPrimaryStatus(task) === 'done');
    const cancelled    = source.filter((task) => taskPrimaryStatus(task) === 'cancelled');
    const closed       = [...done, ...cancelled];
    const overdueTasks = [];
    const focusTasks   = [];
    const upNextTasks  = [];
    const laterTasks   = [];
    const backlogTasks = [];

    active.forEach((task) => {
        const planKey = taskPlanDateKey(task);
        if (planKey && planKey < todayKey) {
            overdueTasks.push(task);
            focusTasks.push(task);
        } else if (planKey === todayKey) {
            focusTasks.push(task);
        } else if (planKey && planKey <= next7EndKey) {
            upNextTasks.push(task);
        } else if (planKey) {
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

    const completionDays = Array.from({ length: 7 }, (_, index) => {
        const day = new Date(today);
        day.setDate(day.getDate() - (6 - index));
        const key = dateKey(day);
        const count = done.filter((task) => zonedDateKey(task.completed_at || task.updated_at) === key).length;
        return { date: key, label: `${day.getMonth() + 1}/${day.getDate()}`, count };
    });

    const textCategoryCount = new Set(active.map(taskTextCategory).filter(Boolean)).size;

    const planMap = new Map();
    active.forEach((task) => {
        const planKey = taskPlanDateKey(task);
        if (!planKey) return;
        planMap.set(planKey, (planMap.get(planKey) || 0) + 1);
    });
    const planLoad = [...planMap.entries()]
        .sort((a, b) => {
            if (a[0] === b[0]) return b[1] - a[1];
            return a[0].localeCompare(b[0]);
        })
        .slice(0, 6)
        .map(([plan, count]) => ({
            plan,
            label: formatPlanDate(plan),
            count,
            share: active.length ? count / active.length : 0,
            state: plan < todayKey ? 'overdue' : plan === todayKey ? 'today' : 'upcoming',
        }));

    return {
        summary: {
            active_count: active.length,
            focus_count: focusSorted.length,
            overdue_count: overdueTasks.length,
            done_count: done.length,
            cancelled_count: cancelled.length,
            closed_count: closed.length,
            completion_rate: active.length + done.length ? done.length / (active.length + done.length) : 0,
        },
        focus_tasks: focusSorted,
        up_next_tasks: sortTasks(upNextTasks),
        later_tasks: sortTasks(laterTasks),
        backlog_tasks: sortTasks(backlogTasks),
        closed_recent: sortDone(closed).slice(0, 8),
        plan_load: planLoad,
        text_category_count: textCategoryCount,
        completion_bars: completionDays,
        match_count: source.length,
        board_columns: {
            open: sortTasks(active),
            closed: sortDone(closed),
        },
    };
}

function ensureStyles() {
    injectStyles(
        CSS_ID,
        `
        ${pageShellCss('tasks-shell', { compactPadding: '20px 16px 30px', compactBreakpoint: BREAKPOINTS.MOBILE })}
        /* 页面头部与视图切换。 */
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
        /* 概览指标与计划分布。 */
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
            min-width: 0;
        }
        .tasks-stat-label { font-size: 12px; font-weight: 700; color: var(--color-text-secondary); }
        .tasks-stat-value {
            margin-top: 8px; font-size: clamp(24px, 1.9vw, 30px); font-weight: 800; color: var(--color-text); letter-spacing: -0.03em;
            line-height: 1.04; overflow-wrap: anywhere; word-break: break-word;
        }
        .tasks-stat-meta { margin-top: 8px; font-size: 12px; color: var(--color-text-secondary); overflow-wrap: anywhere; word-break: break-word; }
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
        /* 搜索及互斥筛选条件。 */
        .tasks-filter-bar {
            display: grid; grid-template-columns: minmax(0, 1.3fr) repeat(3, minmax(140px, 0.6fr));
            gap: 12px; margin-bottom: 18px; padding: 16px 18px; border-radius: 22px;
            background: linear-gradient(180deg, rgba(255,255,255,0.98), rgba(248,250,252,0.94));
            border: 1px solid rgba(226,232,240,0.92); box-shadow: 0 14px 30px rgba(15,23,42,0.04);
        }
        .tasks-filter-field { display: flex; flex-direction: column; gap: 6px; }
        .tasks-filter-field label { font-size: 11px; font-weight: 700; color: var(--color-text-secondary); letter-spacing: 0.04em; text-transform: uppercase; }
        .tasks-filter-field input { width: 100%; height: 40px; border-radius: 14px; border: 1px solid rgba(203,213,225,0.92); padding: 0 14px; }
        .tasks-filter-field--range { grid-column: span 2; }
        .tasks-filter-range {
            display: grid; grid-template-columns: minmax(0, 1fr) auto minmax(0, 1fr);
            gap: 8px; align-items: center;
        }
        .tasks-filter-range span { font-size: 12px; font-weight: 700; color: var(--color-text-secondary); }
        .tasks-filter-bar .pselect-trigger { height: 40px; padding: 0 14px; border-radius: 14px; background: rgba(255,255,255,0.92); }
        .tasks-filter-bar .pselect-panel { border-radius: 16px; }
        .tasks-filter-bar .pselect-label { min-width: 0; }
        /* 执行列表。 */
        .tasks-workspace {
            background: linear-gradient(180deg, rgba(255,255,255,0.98), rgba(248,250,252,0.95));
            border: 1px solid rgba(226,232,240,0.92); border-radius: 22px; box-shadow: 0 18px 34px rgba(15,23,42,0.04);
            overflow: hidden;
        }
        .tasks-workspace-head { display: flex; align-items: center; justify-content: space-between; gap: 12px; padding: 18px 20px; border-bottom: 1px solid rgba(226,232,240,0.75); }
        .tasks-workspace-title { margin: 0; font-size: 18px; font-weight: 760; color: var(--color-text); }
        .tasks-workspace-subtitle { margin: 6px 0 0; font-size: 13px; color: var(--color-text-secondary); }
        .tasks-content { padding: 8px 20px 18px; }
        .tasks-sections { display: flex; flex-direction: column; gap: 0; }
        .tasks-section {
            padding: 18px 0 20px; border-top: 1px solid rgba(226,232,240,0.78);
            background: transparent;
        }
        .tasks-section:first-child { border-top: none; padding-top: 8px; }
        .tasks-section-head { display: flex; align-items: end; justify-content: space-between; gap: 14px; margin-bottom: 8px; }
        .tasks-section-title { font-size: 16px; font-weight: 760; color: var(--color-text); }
        .tasks-section-meta { font-size: 12px; color: var(--color-text-secondary); }
        .tasks-section-count {
            font-size: 12px; font-weight: 760; color: var(--color-text-secondary);
            white-space: nowrap;
        }
        .tasks-task-list { display: flex; flex-direction: column; gap: 0; }
        .task-row {
            display: grid; grid-template-columns: auto minmax(0, 1fr) auto; gap: 14px; align-items: start;
            margin: 0 -8px; padding: 14px 8px; border-bottom: 1px solid rgba(226,232,240,0.66);
            background: transparent;
            transition: background 0.16s ease;
        }
        .task-row:last-child { border-bottom: none; }
        .task-row:hover { background: linear-gradient(90deg, rgba(236,253,245,0.78), transparent 76%); }
        .task-row-priority {
            width: 8px; height: 34px; border-radius: 999px; margin-top: 2px;
        }
        .task-row-main { min-width: 0; }
        .task-row-title-line { display: flex; align-items: center; gap: 10px; min-width: 0; }
        .task-row-title {
            margin: 0; padding: 0; border: 0; background: transparent; text-align: left; cursor: pointer;
            font: inherit; font-size: 15px; font-weight: 760; color: var(--color-text);
            min-width: 0; overflow-wrap: anywhere; word-break: break-word;
        }
        .task-row-note { margin-top: 6px; font-size: 13px; line-height: 1.6; color: var(--color-text-secondary); display: -webkit-box; -webkit-line-clamp: 2; -webkit-box-orient: vertical; overflow: hidden; }
        .task-row-meta {
            display: flex; flex-wrap: wrap; gap: 6px 10px; margin-top: 9px;
            font-size: 12px; font-weight: 700; color: var(--color-text-secondary);
        }
        .task-row-meta span { display: inline-flex; align-items: center; min-width: 0; overflow-wrap: anywhere; }
        .task-row-meta span:not(:last-child)::after {
            content: ""; width: 3px; height: 3px; border-radius: 999px;
            background: rgba(148,163,184,0.72); margin-left: 10px;
        }
        .task-pill {
            display: inline-flex; align-items: center; gap: 6px; height: 28px; padding: 0 10px; border-radius: 999px;
            font-size: 12px; font-weight: 700; background: rgba(241,245,249,0.92); color: var(--color-text-secondary);
            white-space: nowrap; max-width: 100%; overflow: hidden; text-overflow: ellipsis; flex-shrink: 0;
        }
        .task-pill.status { color: var(--task-pill-color); background: var(--task-pill-bg); }
        .task-status-badge {
            display: inline-flex; align-items: center; height: 24px; padding: 0 8px; border-radius: 999px;
            font-size: 12px; font-weight: 760; color: var(--task-pill-color); background: var(--task-pill-bg);
            white-space: nowrap; flex-shrink: 0;
        }
        .task-row-actions { display: flex; gap: 8px; align-items: center; }
        .task-action-btn {
            border: 1px solid transparent; background: transparent; color: var(--color-text-secondary);
            border-radius: 10px; padding: 8px 10px; font-size: 12px; font-weight: 700; cursor: pointer;
            transition: background 0.16s ease, border-color 0.16s ease, color 0.16s ease;
        }
        .task-action-btn:hover { background: rgba(241,245,249,0.9); border-color: rgba(203,213,225,0.8); }
        .task-action-btn.primary { color: #047857; border-color: rgba(16,185,129,0.18); background: rgba(236,253,245,0.86); }
        .tasks-empty {
            padding: 28px 18px; border-radius: 18px; text-align: center; background: rgba(248,250,252,0.82);
            border: 1px dashed rgba(148,163,184,0.26); color: var(--color-text-secondary);
        }
        /* 双栏看板。 */
        .tasks-board { display: grid; grid-template-columns: repeat(2, minmax(260px, 1fr)); gap: 14px; overflow-x: auto; padding-bottom: 4px; }
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
            cursor: grab; transition: box-shadow 0.16s ease, transform 0.16s ease; position: relative;
        }
        .tasks-board-card:hover { transform: translateY(-1px); box-shadow: 0 10px 22px rgba(15,23,42,0.08); }
        .tasks-board-card.dragging { opacity: 0.44; }
        .tasks-board-card-title {
            display: block; width: 100%; margin: 0; padding: 0; border: 0; background: transparent;
            text-align: left; cursor: pointer; font: inherit; font-size: 14px; font-weight: 760; color: var(--color-text);
        }
        .tasks-board-card p { margin: 8px 0 0; font-size: 12px; line-height: 1.6; color: var(--color-text-secondary); display: -webkit-box; -webkit-line-clamp: 2; -webkit-box-orient: vertical; overflow: hidden; }
        .tasks-board-meta { display: flex; flex-wrap: wrap; gap: 6px; margin-top: 10px; }
        .tasks-board-add { padding: 0 12px 12px; }
        .tasks-board-add button {
            width: 100%; border: 1px dashed rgba(148,163,184,0.5); background: transparent; color: var(--color-text-secondary);
            border-radius: 14px; padding: 10px 12px; cursor: pointer; font-weight: 700;
        }
        /* 模态框里的优先级选择器。 */
        .priority-selector { display: flex; align-items: center; gap: 10px; flex-wrap: wrap; }
        .priority-btn {
            width: 36px; height: 36px; display: inline-flex; align-items: center; justify-content: center;
            border-radius: 12px; border: 1px solid rgba(203,213,225,0.9); background: rgba(255,255,255,0.96);
            cursor: pointer; font-size: 18px; opacity: 0.92; transform: scale(1);
            transition: transform 0.16s ease, background 0.16s ease, border-color 0.16s ease, box-shadow 0.16s ease;
        }
        .priority-btn:hover { border-color: rgba(148,163,184,0.82); background: rgba(248,250,252,0.98); transform: translateY(-1px) scale(1.03); }
        .priority-btn.active { opacity: 1; transform: scale(1.06); }
        .priority-btn.priority-1.active { border-color: rgba(239,68,68,0.36); background: rgba(254,242,242,0.96); box-shadow: inset 0 0 0 1px rgba(239,68,68,0.12); }
        .priority-btn.priority-2.active { border-color: rgba(249,115,22,0.36); background: rgba(255,247,237,0.96); box-shadow: inset 0 0 0 1px rgba(249,115,22,0.12); }
        .priority-btn.priority-3.active { border-color: rgba(234,179,8,0.38); background: rgba(254,252,232,0.98); box-shadow: inset 0 0 0 1px rgba(234,179,8,0.14); }
        .priority-btn.priority-4.active { border-color: rgba(34,197,94,0.36); background: rgba(240,253,244,0.98); box-shadow: inset 0 0 0 1px rgba(34,197,94,0.12); }
        .priority-btn.priority-5.active { border-color: rgba(148,163,184,0.42); background: rgba(248,250,252,0.98); box-shadow: inset 0 0 0 1px rgba(148,163,184,0.12); }
        .tasks-toggle-btn:focus-visible,
        .tasks-filter-field input:focus-visible,
        .task-row-title:focus-visible,
        .task-action-btn:focus-visible,
        .tasks-board-card-title:focus-visible,
        .tasks-board-add button:focus-visible,
        .priority-btn:focus-visible {
            outline: 3px solid rgba(16,185,129,0.28);
            outline-offset: 2px;
        }
        ${mediaMax(
            BREAKPOINTS.XL,
            `
            .tasks-layout { grid-template-columns: 1fr; }
            .tasks-filter-bar { grid-template-columns: repeat(2, minmax(0, 1fr)); }
            .tasks-filter-field--range { grid-column: span 2; }
            .tasks-board { grid-template-columns: repeat(2, minmax(240px, 1fr)); }
        `,
        )}
        ${mediaMax(
            BREAKPOINTS.MOBILE,
            `
            .tasks-hero { grid-template-columns: 1fr; }
            .tasks-hero-actions { justify-content: flex-start; }
            .tasks-layout { gap: 14px; }
            .tasks-stat-grid { grid-template-columns: 1fr; }
            .tasks-filter-bar { grid-template-columns: 1fr; }
            .tasks-filter-field--range { grid-column: span 1; }
            .tasks-workspace-head { align-items: start; }
            .task-row { grid-template-columns: auto minmax(0, 1fr); }
            .task-row-actions { grid-column: 2; justify-content: flex-start; flex-wrap: wrap; }
            .tasks-board { grid-template-columns: 1fr; }
        `,
        )}
    `,
    );
}

async function fetchOverview() {
    const res = await api.get('/stats/tasks/overview', { today: dateKey(TODAY()) });
    return normalizeOverview(res?.data);
}

// 以下模板只接收规范化后的任务；所有用户文本仍在插值点显式转义。
function renderCategoryOptions(tasks) {
    const source = Array.isArray(tasks) ? tasks : [];
    const categories = [...new Set(source.map((task) => taskTextCategory(task)).filter(Boolean))].sort();
    return [{ value: '', label: '全部分类' }, ...categories.map((category) => ({ value: category, label: category }))];
}

function renderHero(model) {
    const summary = model.summary;
    return `
        <section class="tasks-hero">
            <div>
                <h2>✅ 待办</h2>
                <p>日期型分类改按计划日期筛选，文字分类单独筛选，状态只保留未完成、已完成、已取消三种口径。</p>
                <div class="tasks-hero-tags">
                    <span class="tasks-hero-tag">${summary.active_count} 项未完成</span>
                    <span class="tasks-hero-tag">${summary.focus_count} 项今天或已滞后</span>
                    <span class="tasks-hero-tag">${summary.closed_count} 项已结束</span>
                </div>
            </div>
            <div class="tasks-hero-actions">
                <div class="tasks-view-toggle" role="group" aria-label="待办视图">
                    <button type="button" class="tasks-toggle-btn ${_viewMode === 'list' ? 'active' : ''}" id="task-view-list" data-view="list" aria-pressed="${_viewMode === 'list'}">执行列表</button>
                    <button type="button" class="tasks-toggle-btn ${_viewMode === 'board' ? 'active' : ''}" id="task-view-board" data-view="board" aria-pressed="${_viewMode === 'board'}">双栏总览</button>
                </div>
                <button type="button" class="btn btn-primary" id="tasks-add-top">＋ 新建任务</button>
            </div>
        </section>`;
}

function renderInsights(model) {
    const summary    = model.summary;
    const completion = Math.round(summary.completion_rate * 100);
    const maxBar     = Math.max(1, ...model.completion_bars.map((item) => item.count || 0));
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
                            <div class="tasks-stat-label">未完成</div>
                            <div class="tasks-stat-value">${summary.active_count}</div>
                            <div class="tasks-stat-meta">包含所有仍需处理的任务</div>
                        </div>
                        <div class="tasks-stat-card">
                            <div class="tasks-stat-label">今天与滞后</div>
                            <div class="tasks-stat-value">${summary.focus_count}</div>
                            <div class="tasks-stat-meta">优先看今天计划和已经拖后的事项</div>
                        </div>
                        <div class="tasks-stat-card">
                            <div class="tasks-stat-label">已滞后</div>
                            <div class="tasks-stat-value">${summary.overdue_count}</div>
                            <div class="tasks-stat-meta">计划日期早于今天的未完成任务</div>
                        </div>
                        <div class="tasks-stat-card">
                            <div class="tasks-stat-label">已结束</div>
                            <div class="tasks-stat-value">${summary.closed_count}</div>
                            <div class="tasks-stat-meta">完成 ${summary.done_count} 项，取消 ${summary.cancelled_count} 项</div>
                        </div>
                    </div>
                    <div class="tasks-meter">
                        ${model.completion_bars
                            .map(
                                (item) => `
                            <div class="tasks-meter-bar">
                                <div class="tasks-meter-value">${item.count}</div>
                                <div class="tasks-meter-stick" style="height:${14 + (item.count / maxBar) * 66}px;"></div>
                                <div class="tasks-meter-label">${item.label}</div>
                            </div>`,
                            )
                            .join('')}
                    </div>
                </div>
            </div>
            <div class="tasks-panel">
                <div class="tasks-panel-head">
                    <div>
                        <h3>计划分布</h3>
                        <p>日期型分类按计划日期展示；文字分类留在筛选器里使用。</p>
                    </div>
                </div>
                <div class="tasks-panel-body">
                    ${
                        model.plan_load.length
                            ? `
                        <div class="tasks-category-list">
                            ${model.plan_load
                                .map(
                                    (item) => `
                                <div class="tasks-category-row">
                                    <div>
                                        <div class="tasks-category-top">
                                            <span class="tasks-category-name">${item.state === 'today' ? `今天 · ${item.label}` : item.state === 'overdue' ? `已滞后 · ${item.label}` : `计划 · ${item.label}`}</span>
                                            <span class="tasks-category-count">${item.count} 项</span>
                                        </div>
                                        <div class="tasks-category-track">
                                            <div class="tasks-category-fill" style="width:${Math.max(10, Math.round(item.share * 100))}%;"></div>
                                        </div>
                                    </div>
                                    <div class="task-pill">${Math.round(item.share * 100)}%</div>
                                </div>`,
                                )
                                .join('')}
                        </div>`
                            : `<div class="tasks-empty">当前没有带计划日期的未完成任务；如果录入了文字分类，可以直接用上方筛选器查看。</div>`
                    }
                    ${model.text_category_count ? `<div class="task-pill" style="margin-top:14px;">${model.text_category_count} 个文字分类可筛选</div>` : ''}
                </div>
            </div>
        </section>`;
}

function renderFilters(tasks) {
    const showCustomRange = _filters.plan === 'custom';
    return `
        <section class="tasks-filter-bar">
            <div class="tasks-filter-field">
                <label for="tasks-filter-search">搜索</label>
                <input id="tasks-filter-search" type="search" placeholder="标题、备注、分类" value="${escapeHtml(_filters.search)}" aria-label="搜索待办">
            </div>
            <div class="tasks-filter-field">
                <label id="tasks-filter-plan-label">计划日期</label>
                ${renderCustomSelect({
                    id: 'tasks-filter-plan',
                    options: PLAN_FILTER_OPTIONS,
                    selected: _filters.plan,
                    className: 'pselect-block pselect-theme-tasks',
                    placeholder: '全部计划日期',
                    labelledBy: 'tasks-filter-plan-label',
                })}
            </div>
            <div class="tasks-filter-field">
                <label id="tasks-filter-category-label">分类</label>
                ${renderCustomSelect({
                    id: 'tasks-filter-category',
                    options: renderCategoryOptions(tasks),
                    selected: _filters.category,
                    className: 'pselect-block pselect-theme-tasks',
                    placeholder: '全部分类',
                    labelledBy: 'tasks-filter-category-label',
                })}
            </div>
            <div class="tasks-filter-field">
                <label id="tasks-filter-status-label">状态</label>
                ${renderCustomSelect({
                    id: 'tasks-filter-status',
                    options: STATUS_FILTER_OPTIONS,
                    selected: _filters.status,
                    className: 'pselect-block pselect-theme-tasks',
                    placeholder: '全部状态',
                    labelledBy: 'tasks-filter-status-label',
                })}
            </div>
            ${
                showCustomRange
                    ? `
                <div class="tasks-filter-field tasks-filter-field--range">
                    <label>自定义范围</label>
                    <div class="tasks-filter-range">
                        <input id="tasks-filter-plan-start" type="text" inputmode="numeric" pattern="\\d{4}-\\d{2}-\\d{2}" maxlength="10" autocomplete="off" spellcheck="false" placeholder="YYYY-MM-DD" value="${escapeHtml(_filters.customStart)}" aria-label="计划开始日期">
                        <span>至</span>
                        <input id="tasks-filter-plan-end" type="text" inputmode="numeric" pattern="\\d{4}-\\d{2}-\\d{2}" maxlength="10" autocomplete="off" spellcheck="false" placeholder="YYYY-MM-DD" value="${escapeHtml(_filters.customEnd)}" aria-label="计划结束日期">
                    </div>
                </div>`
                    : ''
            }
        </section>`;
}

function taskRowHTML(task) {
    const priority         = PRIORITY_INFO[task.priority] || PRIORITY_INFO[3];
    const normalizedStatus = taskPrimaryStatus(task);
    const status           = STATUS_META[normalizedStatus] || STATUS_META.open;
    const textCategory     = taskTextCategory(task);
    const planKey          = taskPlanDateKey(task);
    const scheduleText     = taskStatusBucket(task) === 'closed'
            ? task.completed_at || task.cancelled_at || task.updated_at
                ? `${normalizedStatus === 'cancelled' ? '结束于' : '完成于'} · ${formatShortDate(task.completed_at || task.cancelled_at || task.updated_at)}`
                : normalizedStatus === 'cancelled'
                  ? '已取消'
                  : '已完成'
            : task.deadline_at
              ? `${isOverdue(task) ? '已滞后' : '截止'} · ${formatShortDate(task.deadline_at)}`
              : describePlanDate(planKey);
    const taskId = escapeHtml(task.id || '');
    const pending = _pendingTaskIds.has(task.id);
    const pendingAttrs = pending ? ' disabled aria-busy="true"' : '';
    const content = task.content ? `<div class="task-row-note">${escapeHtml(task.content)}</div>` : '';
    const metaParts = [
        `${priority.icon} ${priority.label}`,
        planKey ? formatPlanDate(planKey) : '',
        textCategory || (!planKey ? '未分类' : ''),
        scheduleText,
    ].filter(Boolean);
    return `
        <article class="task-row">
            <div class="task-row-priority" style="background:${priority.color};"></div>
            <div class="task-row-main">
                <div class="task-row-title-line">
                    <button type="button" class="task-row-title" data-action="edit" data-id="${taskId}"${pendingAttrs}>${escapeHtml(task.title || '(无标题)')}</button>
                    <span class="task-status-badge" style="--task-pill-color:${status.tone};--task-pill-bg:${status.bg};">${status.label}</span>
                </div>
                ${content}
                <div class="task-row-meta">
                    ${metaParts.map((part) => `<span>${escapeHtml(part)}</span>`).join('')}
                </div>
            </div>
            <div class="task-row-actions">
                ${taskStatusBucket(task) !== 'closed' ? `<button type="button" class="task-action-btn primary" data-action="done" data-id="${taskId}"${pendingAttrs}>完成</button>` : `<button type="button" class="task-action-btn" data-action="resume" data-id="${taskId}"${pendingAttrs}>恢复</button>`}
                ${taskStatusBucket(task) !== 'closed' ? `<button type="button" class="task-action-btn" data-action="cancel" data-id="${taskId}"${pendingAttrs}>取消</button>` : ''}
                <button type="button" class="task-action-btn" data-action="edit" data-id="${taskId}"${pendingAttrs}>编辑</button>
            </div>
        </article>`;
}

function renderSection(title, subtitle, tasks) {
    return `
        <section class="tasks-section">
            <div class="tasks-section-head">
                <div>
                    <div class="tasks-section-title">${escapeHtml(title)}</div>
                    <div class="tasks-section-meta">${escapeHtml(subtitle)}</div>
                </div>
                <div class="tasks-section-count">${tasks.length} 项</div>
            </div>
            ${
                tasks.length
                    ? `<div class="tasks-task-list">${tasks.map(taskRowHTML).join('')}</div>`
                    : `<div class="tasks-empty">当前没有内容。</div>`
            }
        </section>`;
}

function renderListView(model) {
    const showActive = !_filters.status || _filters.status === 'open';
    const showClosed = !_filters.status || _filters.status === 'done' || _filters.status === 'cancelled';
    return `
        <div class="tasks-sections">
            ${showActive ? renderSection('今天与滞后', '优先清理今天计划和已经拖后的任务。', model.focus_tasks) : ''}
            ${showActive ? renderSection('未来 7 天', '接下来一周内需要继续跟进的事项。', model.up_next_tasks) : ''}
            ${showActive ? renderSection('更晚与未安排', '更晚的计划日期，以及尚未放进日期桶的任务。', [...model.later_tasks, ...model.backlog_tasks]) : ''}
            ${showClosed ? renderSection('最近结束', '已完成和已取消的任务。', model.closed_recent) : ''}
        </div>`;
}

function boardCardHTML(task) {
    const priority     = PRIORITY_INFO[task.priority] || PRIORITY_INFO[3];
    const status       = STATUS_META[taskPrimaryStatus(task)] || STATUS_META.open;
    const textCategory = taskTextCategory(task);
    const planKey      = taskPlanDateKey(task);
    const taskId       = escapeHtml(task.id || '');
    const pending      = _pendingTaskIds.has(task.id);
    const pendingAttrs = pending ? ' disabled aria-busy="true"' : '';
    return `
        <div class="tasks-board-card" data-task-id="${taskId}" draggable="${!pending}">
            <button type="button" class="tasks-board-card-title" data-action="edit" data-id="${taskId}"${pendingAttrs}>${escapeHtml(task.title || '(无标题)')}</button>
            ${task.content ? `<p>${escapeHtml(task.content)}</p>` : ''}
            <div class="tasks-board-meta">
                <span class="task-pill">${priority.icon} ${priority.label}</span>
                ${planKey ? `<span class="task-pill">${formatPlanDate(planKey)}</span>` : ''}
                ${textCategory ? `<span class="task-pill">${escapeHtml(textCategory)}</span>` : ''}
                <span class="task-pill status" style="--task-pill-color:${status.tone};--task-pill-bg:${status.bg};">${status.label}</span>
            </div>
        </div>`;
}

function renderBoardView(model) {
    const columns = [
        { key: 'open', label: '未完成', color: '#F59E0B' },
        { key: 'closed', label: '已结束', color: '#16A34A' },
    ];
    return `
        <div class="tasks-board">
            ${columns
                .map(
                    (column) => `
                <section class="tasks-board-col">
                    <div class="tasks-board-head">
                        <div class="tasks-board-title">
                            <span class="tasks-board-dot" style="background:${column.color};"></span>
                            ${column.label}
                        </div>
                        <span class="tasks-board-count">${model.board_columns[column.key].length}</span>
                    </div>
                    <div class="tasks-board-list" data-col="${column.key}">
                        ${
                            model.board_columns[column.key].length
                                ? model.board_columns[column.key].map(boardCardHTML).join('')
                                : `<div class="tasks-empty">暂无任务</div>`
                        }
                    </div>
                    ${column.key === 'open' ? `<div class="tasks-board-add"><button type="button" id="tasks-add-board">＋ 添加任务</button></div>` : ''}
                </section>`,
                )
                .join('')}
        </div>`;
}

function renderWorkspace(model) {
    const subtitle =
        _viewMode === 'list' ? '按计划日期和完成情况安排接下来要处理的事。' : '把未完成和已结束分成两栏快速浏览。';
    return `
        <section class="tasks-workspace">
            <div class="tasks-workspace-head">
                <div>
                    <h3 class="tasks-workspace-title">${_viewMode === 'list' ? '执行列表' : '双栏总览'}</h3>
                    <p class="tasks-workspace-subtitle">${subtitle}</p>
                </div>
                <div class="task-pill">${model.match_count} 项匹配</div>
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
        _container.innerHTML = `<div class="tasks-shell" aria-busy="true"><div class="tasks-empty" role="status" aria-live="polite">正在加载待办...</div></div>`;
        return;
    }

    if (_loadError && !_overview) {
        _container.innerHTML = `
            <div class="tasks-shell">
                <div class="tasks-empty" role="alert">
                    <p>待办加载失败：${escapeHtml(_loadError)}</p>
                    <button type="button" class="btn btn-secondary" id="tasks-retry">重新加载</button>
                </div>
            </div>`;
        _container.querySelector('#tasks-retry')?.addEventListener('click', () => void loadAndRender());
        return;
    }

    const tasks = filteredTasks();
    const model = deriveDisplayModel(tasks);

    _container.innerHTML = `
        <div class="tasks-shell" aria-busy="${_loading}">
            ${renderHero(model)}
            ${renderInsights(model)}
            ${renderFilters(_overview?.all_tasks || [])}
            ${renderWorkspace(model)}
        </div>
    `;

    attachListeners();
}

async function updateTaskStatus(taskId, status) {
    const id = idValue(taskId);
    if (!id || !TASK_STATUSES.has(status) || _pendingTaskIds.has(id)) return false;

    const task = (_overview?.all_tasks || []).find((item) => item.id === id);
    if (!task || taskPrimaryStatus(task) === status) return false;

    _pendingTaskIds.add(id);
    renderPage();
    try {
        await api.put(itemPath(id), { status, version: task.version });
        showToast('任务状态已更新', 'success');
        emitTaskChanged();
        return true;
    } catch (error) {
        showToast(`状态更新失败：${errorMessage(error)}`, 'error');
        return false;
    } finally {
        _pendingTaskIds.delete(id);
        renderPage();
    }
}

// 新建与编辑共用字段定义，但保存和删除各自锁定，防止双击产生重复写入。
export async function openTaskModal(existing = null) {
    let userTimeZone;
    try {
        userTimeZone = await fetchUserTimeZone();
    } catch (error) {
        showToast(`无法读取用户时区：${errorMessage(error)}`, 'error');
        return;
    }
    ensureStyles();
    const task = normalizeTask(existing);
    const isEdit = Boolean(task);
    const deadlineValue = task?.deadline_at ? zonedDateTimeToInput(task.deadline_at, userTimeZone) : '';
    if (task?.deadline_at && !deadlineValue) {
        showToast('截止时间格式无效，已阻止编辑以免覆盖原值', 'error');
        return;
    }
    const fields = TASK_FIELDS.map((field) => {
        let value = field.value ?? '';
        if (task) {
            if (field.name === 'deadline_at') value = deadlineValue;
            else if (field.name === 'status') value = taskPrimaryStatus(task);
            else if (field.name === 'category') value = taskTextCategory(task);
            else value = task[field.name] ?? field.value ?? '';
        } else if (field.name === 'plan_date') {
            value = defaultTaskPlanDateValue();
        }
        return { ...field, value };
    });

    const content = showModal(
        isEdit ? '编辑任务' : '新建任务',
        safeHtml(`<form id="task-form">${buildFormHTML(fields)}</form>`),
        {
            footer: safeHtml(`
            ${isEdit ? `<button type="button" class="btn btn-danger btn-sm" id="task-delete" style="margin-right:auto;">删除</button>` : ''}
            <button type="button" class="btn btn-secondary" id="task-cancel">取消</button>
            <button type="button" class="btn btn-primary" id="task-save">保存</button>
        `),
        },
    );

    initFormInteractions(content);
    content.querySelector('#task-cancel')?.addEventListener('click', closeModal);
    let submitting = false;

    if (task) {
        content.querySelector('#task-delete')?.addEventListener('click', async () => {
            if (submitting) return;
            submitting = true;
            closeModal();
            const confirmed = await showConfirmModal({
                title: '删除任务',
                message: `确定要删除“${task.title || '这个任务'}”吗？删除后会从执行列表和看板里一起移除。`,
                confirmText: '删除',
                cancelText: '返回编辑',
                tone: 'danger',
            });
            if (!confirmed) {
                openTaskModal(task);
                return;
            }
            try {
                await api.delete(itemPath(task.id));
                showToast('任务已删除', 'success');
                emitTaskChanged();
            } catch (error) {
                showToast(`删除失败：${errorMessage(error)}`, 'error');
            } finally {
                submitting = false;
            }
        });
    }

    const saveButton = content.querySelector('#task-save');
    saveButton?.addEventListener('click', async () => {
        if (submitting) return;
        const form = content.querySelector('#task-form');
        let payload;
        try {
            payload = normalizeTaskPayload(getFormData(form), userTimeZone);
        } catch (error) {
            showToast(errorMessage(error), 'warning');
            return;
        }
        if (!payload.title) {
            showToast('请填写标题', 'warning');
            return;
        }
        submitting = true;
        saveButton.disabled = true;
        saveButton.setAttribute('aria-busy', 'true');
        try {
            if (task) {
                await api.put(itemPath(task.id), { ...payload, version: task.version });
                showToast('任务已更新', 'success');
            } else {
                await api.post('/items', { type: 'task', ...payload });
                showToast('任务已创建', 'success');
            }
            closeModal();
            emitTaskChanged();
        } catch (error) {
            showToast(`保存失败：${errorMessage(error)}`, 'error');
        } finally {
            submitting = false;
            if (saveButton.isConnected) {
                saveButton.disabled = false;
                saveButton.removeAttribute('aria-busy');
            }
        }
    });
}

function attachListeners() {
    if (!_container) return;

    const handlePlanChange = (value) => {
        const plan = PLAN_FILTER_VALUES.has(value) ? value : '';
        _filters.plan = plan;
        if (plan) {
            _filters.category = '';
        } else {
            _filters.customStart = '';
            _filters.customEnd = '';
        }
        if (plan === 'custom' && (!_filters.customStart || !_filters.customEnd)) {
            const range = defaultCustomPlanRange();
            _filters.customStart = range.start;
            _filters.customEnd = range.end;
        }
        renderPage();
    };

    initCustomSelects(_container, {
        'tasks-filter-plan': handlePlanChange,
        'tasks-filter-category': (value) => {
            const categories = new Set(renderCategoryOptions(_overview?.all_tasks).map((option) => option.value));
            const category = categories.has(value) ? value : '';
            _filters.category = category;
            if (category) {
                _filters.plan = '';
                _filters.customStart = '';
                _filters.customEnd = '';
            }
            renderPage();
        },
        'tasks-filter-status': (value) => {
            _filters.status = value === '' || TASK_STATUSES.has(value) ? value : '';
            renderPage();
        },
    });

    _container.querySelectorAll('[data-view]').forEach((button) => {
        button.onclick = () => {
            const view = button.dataset.view;
            if (!VIEW_MODES.has(view) || view === _viewMode) return;
            _viewMode = view;
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
            _filters.search = textValue(search.value);
            renderPage();
        };
    }

    const updateCustomDate = (key, input) => {
        const value = textValue(input.value);
        if (!isIsoDate(value)) {
            input.setCustomValidity('请输入有效日期，格式为 YYYY-MM-DD');
            input.reportValidity();
            return;
        }
        input.setCustomValidity('');
        _filters[key] = value;
        renderPage();
    };
    const customStart = _container.querySelector('#tasks-filter-plan-start');
    if (customStart) {
        customStart.onchange = () => updateCustomDate('customStart', customStart);
    }

    const customEnd = _container.querySelector('#tasks-filter-plan-end');
    if (customEnd) {
        customEnd.onchange = () => updateCustomDate('customEnd', customEnd);
    }

    _container.onclick = async (event) => {
        const actionButton = event.target?.closest?.('[data-action]');
        if (actionButton) {
            event.stopPropagation();
            const taskId = idValue(actionButton.dataset.id);
            const action = actionButton.dataset.action;
            const task   = (_overview?.all_tasks || []).find((item) => item.id === taskId);
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
                await updateTaskStatus(taskId, 'open');
                return;
            }
            if (action === 'cancel') {
                await updateTaskStatus(taskId, 'cancelled');
                return;
            }
            return;
        }
    };

    if (_viewMode === 'board') {
        _container.querySelectorAll('.tasks-board-card').forEach((card) => {
            card.addEventListener('dragstart', (event) => {
                if (card.getAttribute('draggable') !== 'true') {
                    event.preventDefault();
                    return;
                }
                _dragTaskId = card.dataset.taskId;
                card.classList.add('dragging');
                if (event.dataTransfer) {
                    event.dataTransfer.effectAllowed = 'move';
                    event.dataTransfer.setData('text/plain', _dragTaskId || '');
                }
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
                const taskId = _dragTaskId || idValue(event.dataTransfer?.getData('text/plain'));
                const column = zone.dataset.col;
                const task   = (_overview?.all_tasks || []).find((item) => item.id === taskId);
                if (!task || !['open', 'closed'].includes(column)) return;
                const currentStatus = taskPrimaryStatus(task);
                const nextStatus = column === 'open' ? 'open' : currentStatus === 'open' ? 'done' : currentStatus;
                await updateTaskStatus(taskId, nextStatus);
            });
        });
    }
}

// 递增版本号实现 latest-wins；路由销毁也会递增版本，使迟到响应自动失效。
async function loadAndRender() {
    const loadVersion = ++_loadVersion;
    _loading = true;
    if (!_overview) _loadError = '';
    renderPage();
    try {
        const overview = await fetchOverview();
        if (!_container || loadVersion !== _loadVersion) return;
        _overview = overview;
        _loadError = '';
    } catch (error) {
        if (!_container || loadVersion !== _loadVersion) return;
        _loadError = errorMessage(error);
        showToast(`加载任务失败：${_loadError}`, 'error');
    } finally {
        if (_container && loadVersion === _loadVersion) {
            _loading = false;
            renderPage();
        }
    }
}

export function render(container) {
    _unsubscribeDataChanges?.();
    _unsubscribeDataChanges = null;
    _loadVersion += 1;
    _container = container;
    _overview = null;
    _loading = false;
    _loadError = '';
    _viewMode = 'list';
    _dragTaskId = null;
    _filters = { ...DEFAULT_FILTERS };
    _pendingTaskIds.clear();
    _unsubscribeDataChanges = subscribeDataChanges('task', loadAndRender);
    void loadAndRender();
}

export function destroy() {
    _unsubscribeDataChanges?.();
    _unsubscribeDataChanges = null;
    _loadVersion += 1;
    _container = null;
    _overview = null;
    _loading = false;
    _loadError = '';
    _dragTaskId = null;
    _filters = { ...DEFAULT_FILTERS };
    _pendingTaskIds.clear();
}
