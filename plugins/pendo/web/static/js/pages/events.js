import { api } from '../api.js';
import { showToast } from '../components/toast.js';
import { showModal, closeModal, showConfirmModal, safeHtml } from '../components/modal.js';
import { renderCustomSelect, initCustomSelects } from '../components/custom_select.js';
import { derivePresetRange, fetchItemRangeBounds, RANGE_PRESET_OPTIONS, todayRangeKey } from '../utils/date_ranges.js';
import { formatDateTime as formatSharedDateTime, isoDate, isValidDateInput, pad2, parseDate } from '../utils/format.js';
import { fetchUserTimeZone, zonedDateTimeToInput, zonedInputToUtcIso } from '../utils/timezone.js';
import {
    bindEnterAction,
    bindFormSubmit,
    BREAKPOINTS,
    escapeHtml,
    injectStyles,
    mediaMax,
    pageShellCss,
    subscribeDataChanges,
} from '../utils/ui.js';

const CSS_ID = 'pendo-events-redesign-styles';
const CALENDAR_VISIBLE_ITEM_LIMIT = 3;
const WEEKDAYS = Object.freeze(['周一', '周二', '周三', '周四', '周五', '周六', '周日']);
const EVENT_KINDS = new Set(['single', 'multi_node', 'recurring']);
const REMINDER_STATUSES = new Set(['pending', 'sent', 'confirmed']);
const EVENT_FILTER_KINDS = new Set(['', 'all', ...EVENT_KINDS]);
const REMINDER_FILTERS = new Set(['', 'all', 'with', 'none', ...REMINDER_STATUSES]);

let _container = null;
let _unsubscribeDataChanges = null;
let _loadVersion = 0;
let _state = {
    viewMode: 'calendar',
    monthCursor: firstDayOfMonth(new Date()),
    selectedDate: isoDate(new Date()),
    listRange: 'month',
    customStart: '',
    customEnd: '',
    allRangeStart: '',
    allRangeEnd: '',
    filters: {
        keyword: '',
        category: '',
        kind: 'all',
        reminder: 'all',
    },
    overview: null,
    loading: false,
};

function firstDayOfMonth(value) {
    return new Date(value.getFullYear(), value.getMonth(), 1);
}

function lastDayOfMonth(value) {
    return new Date(value.getFullYear(), value.getMonth() + 1, 0);
}

function addMonths(base, offset) {
    return new Date(base.getFullYear(), base.getMonth() + offset, 1);
}

function weekdayIndexMonday(date) {
    return (date.getDay() + 6) % 7;
}

function formatMonthLabel(value) {
    return `${value.getFullYear()}年${pad2(value.getMonth() + 1)}月`;
}

function formatDateLabel(day) {
    const date = parseDate(day);
    return date ? `${date.getMonth() + 1}月${date.getDate()}日` : '未知日期';
}

function formatWeekday(day) {
    const date = parseDate(day);
    return date ? WEEKDAYS[weekdayIndexMonday(date)] : '未知星期';
}

function finiteCount(value) {
    try {
        const number = Number(value);
        return Number.isFinite(number) && number > 0 ? Math.floor(number) : 0;
    } catch {
        return 0;
    }
}

function normalizedKind(value) {
    return EVENT_KINDS.has(value) ? value : 'single';
}

function normalizedReminderStatus(value) {
    return REMINDER_STATUSES.has(value) ? value : 'pending';
}

function canToggleReminder(value, nowMs = Date.now()) {
    const remindAt = parseDate(value)?.getTime();
    return Number.isFinite(remindAt) && remindAt > nowMs;
}

function watchReminderExpiry(content) {
    let timerId = null;
    const refresh = () => {
        timerId = null;
        const nowMs = Date.now();
        let nearestFuture = Number.POSITIVE_INFINITY;
        const buttons = content?.querySelectorAll?.('[data-toggle-reminder]') ?? [];
        for (const button of buttons) {
            const remindAt = parseDate(button.dataset.reminderTime)?.getTime();
            if (!Number.isFinite(remindAt) || remindAt <= nowMs) {
                button.disabled = true;
                continue;
            }
            nearestFuture = Math.min(nearestFuture, remindAt);
        }
        if (Number.isFinite(nearestFuture)) {
            const delay = Math.min(Math.max(nearestFuture - nowMs + 50, 50), 2_147_000_000);
            timerId = setTimeout(refresh, delay);
        }
    };

    refresh();
    return () => {
        if (timerId !== null) clearTimeout(timerId);
    };
}

function reminderStatusLabel(status) {
    if (status === 'confirmed') return '已确认';
    if (status === 'sent') return '已发送';
    return '待发送';
}

function reminderMetaLabel(status, repeatCount, wasSent = false) {
    if (status === 'confirmed') {
        return wasSent ? '已发送并确认' : '已提前确认，本次不会发送';
    }
    if (status === 'sent') return repeatCount ? `已重复 ${repeatCount} 次` : '已发送，等待确认';
    return '等待发送';
}

function hasInvalidDatePrefix(value) {
    const text = typeof value === 'string' ? value.trim() : '';
    return /^\d{4}-\d{2}-\d{2}/.test(text) && !isValidDateInput(text.slice(0, 10));
}

function formatEventDateTime(value) {
    return hasInvalidDatePrefix(value) ? '未知时间' : formatSharedDateTime(value);
}

/** 把接口日期转成用户设置时区的 YYYY-MM-DDTHH:mm，非法值不进入表单。 */
function toInputDateTime(value, userTimeZone) {
    if (hasInvalidDatePrefix(value)) return '';
    return zonedDateTimeToInput(value, userTimeZone);
}

/** 严格把用户设置时区的墙钟转换为唯一 UTC 时刻。 */
function inputToIso(value, userTimeZone) {
    return zonedInputToUtcIso(value, userTimeZone);
}

function reminderRulesFromTimes(startTime, remindTimes) {
    const start = parseDate(startTime)?.getTime();
    if (!Number.isFinite(start)) return [];
    const offsets = new Set();
    for (const value of Array.isArray(remindTimes) ? remindTimes : []) {
        const remind = parseDate(value)?.getTime();
        if (!Number.isFinite(remind)) continue;
        const offset = Math.round((start - remind) / 1000);
        if (offset >= 0) offsets.add(offset);
    }
    return [...offsets].sort((a, b) => b - a).map((offset) => ({ offset_seconds: offset }));
}

/** 接口边界只保留渲染所需结构，异常字段统一收敛为空值或非负整数。 */
function normalizeOverview(value) {
    const raw = value && typeof value === 'object' ? value : {};
    const summary = raw.summary && typeof raw.summary === 'object' ? raw.summary : {};
    const categories = Array.isArray(raw.categories)
        ? [...new Set(raw.categories.map((item) => String(item ?? '').trim()).filter(Boolean))]
        : [];
    const calendarDays =
        raw.calendar_days && typeof raw.calendar_days === 'object' && !Array.isArray(raw.calendar_days)
            ? raw.calendar_days
            : {};
    const events = Array.isArray(raw.events) ? raw.events.filter((item) => item && typeof item === 'object') : [];
    const timelineDays = Array.isArray(raw.timeline_days)
        ? raw.timeline_days
              .filter((row) => row && isValidDateInput(row.date))
              .map((row) => ({
                  ...row,
                  date: row.date,
                  items: Array.isArray(row.items) ? row.items.filter((item) => item && typeof item === 'object') : [],
              }))
        : [];
    return {
        summary: {
            event_count: finiteCount(summary.event_count),
            multi_node_count: finiteCount(summary.multi_node_count),
            reminder_count: finiteCount(summary.reminder_count),
        },
        categories,
        calendar_days: calendarDays,
        timeline_days: timelineDays,
        events,
    };
}

function currentRange() {
    if (_state.viewMode === 'calendar') {
        return {
            start: isoDate(firstDayOfMonth(_state.monthCursor)),
            end: isoDate(lastDayOfMonth(_state.monthCursor)),
        };
    }
    if (_state.listRange === 'all' && _state.allRangeStart && _state.allRangeEnd) {
        return { start: _state.allRangeStart, end: _state.allRangeEnd };
    }
    return derivePresetRange(_state.listRange, {
        today: todayRangeKey(),
        customStart: _state.customStart,
        customEnd: _state.customEnd,
        customFallback: '',
    });
}

function restoreScrollPosition(scrollY) {
    // 首次滚动后布局可能仍在收敛，下一帧再校准一次可避免筛选时页面跳动。
    window.requestAnimationFrame(() => {
        window.scrollTo({ top: scrollY });
        window.requestAnimationFrame(() => window.scrollTo({ top: scrollY }));
    });
}

function ensureStyles() {
    injectStyles(
        CSS_ID,
        `
        ${pageShellCss('events-page', { padding: '26px 24px 34px', compactPadding: '20px 16px 28px', compactBreakpoint: BREAKPOINTS.MOBILE })}
        .events-hero {
            display: grid; grid-template-columns: minmax(0, 1fr) auto; gap: 18px; align-items: center;
            padding: 22px 24px; border-radius: 24px; margin-bottom: 18px;
            background:
                radial-gradient(circle at top right, rgba(245,158,11,0.20), transparent 32%),
                radial-gradient(circle at bottom left, rgba(99,102,241,0.14), transparent 24%),
                linear-gradient(145deg, rgba(255,255,255,0.98), rgba(255,251,235,0.92));
            border: 1px solid rgba(245,158,11,0.16);
            box-shadow: 0 18px 40px rgba(15,23,42,0.05);
        }
        .events-hero h2 { margin: 0; font-size: 26px; font-weight: 800; color: var(--color-events); letter-spacing: -0.02em; }
        .events-hero p { margin: 8px 0 0; font-size: 14px; line-height: 1.7; color: var(--color-text-secondary); }
        .events-hero-actions { display: flex; gap: 8px; flex-wrap: wrap; justify-content: flex-end; align-items: center; }
        .events-view-toggle {
            display: inline-flex; gap: 4px; padding: 4px; border-radius: 999px; background: rgba(255,255,255,0.8);
            border: 1px solid rgba(226,232,240,0.92);
        }
        .events-toggle-btn {
            border: none; background: transparent; color: var(--color-text-secondary); cursor: pointer;
            border-radius: 999px; padding: 9px 14px; font-size: 13px; font-weight: 700; transition: all 0.16s ease;
        }
        .events-toggle-btn.active { background: var(--color-events); color: #fff; box-shadow: 0 10px 24px rgba(245,158,11,0.26); }
        .events-filters {
            display: grid; grid-template-columns: minmax(0, 1.2fr) repeat(3, minmax(140px, 0.6fr));
            gap: 12px; margin-bottom: 18px; padding: 16px 18px; border-radius: 22px;
            background: linear-gradient(180deg, rgba(255,255,255,0.98), rgba(248,250,252,0.94));
            border: 1px solid rgba(226,232,240,0.92); box-shadow: 0 14px 30px rgba(15,23,42,0.04);
        }
        .events-filter-field { display: flex; flex-direction: column; gap: 6px; }
        .events-filter-field label { font-size: 11px; font-weight: 700; color: var(--color-text-secondary); letter-spacing: 0.04em; text-transform: uppercase; }
        .events-filter-field input,
        .events-inline-date {
            width: 100%; height: 40px; border-radius: 14px; border: 1px solid rgba(203,213,225,0.92);
            background: rgba(255,255,255,0.92); padding: 0 14px; font-size: 14px; color: var(--color-text);
        }
        .events-filters .pselect-trigger {
            height: 40px; padding: 0 14px; border-radius: 14px; background: rgba(255,255,255,0.92);
        }
        .events-filters .pselect-label {
            min-width: 0;
        }
        .events-filters .pselect-panel { border-radius: 16px; }
        .pselect-theme-events {
            --pselect-accent: var(--color-events);
            --pselect-accent-text: #B45309;
            --pselect-accent-soft: rgba(245, 158, 11, 0.08);
            --pselect-accent-shadow: rgba(245, 158, 11, 0.12);
        }
        .events-inline-date { appearance: none; }
        .events-inline-separator { font-size: 12px; color: var(--color-text-secondary); }
        .events-list-range { display: inline-flex; gap: 6px; flex-wrap: wrap; }
        .events-timeline-tools { display: flex; flex-direction: column; gap: 10px; align-items: flex-end; }
        .events-timeline-toolbar { display: flex; gap: 10px; align-items: center; flex-wrap: wrap; justify-content: flex-end; }
        .events-range-btn {
            border: 1px solid rgba(226,232,240,0.92); background: rgba(255,255,255,0.86); color: var(--color-text-secondary);
            border-radius: 999px; padding: 9px 12px; font-size: 12px; font-weight: 700; cursor: pointer;
        }
        .events-range-btn.active { background: rgba(245,158,11,0.12); border-color: rgba(245,158,11,0.32); color: var(--color-events); }
        .events-inline-dates { display: inline-flex; gap: 8px; align-items: center; flex-wrap: wrap; justify-content: flex-end; }
        .events-inline-dates .events-inline-date { width: 164px; }
        .events-grid { display: grid; grid-template-columns: minmax(0, 1.16fr) minmax(320px, 0.84fr); gap: 16px; align-items: start; }
        .events-panel {
            background: linear-gradient(180deg, rgba(255,255,255,0.98), rgba(248,250,252,0.94));
            border: 1px solid rgba(226,232,240,0.92); border-radius: 24px;
            box-shadow: 0 18px 36px rgba(15,23,42,0.04); overflow: hidden;
        }
        .events-panel-header { display: flex; align-items: center; justify-content: space-between; gap: 12px; padding: 18px 20px 0; }
        .events-panel-title { margin: 0; font-size: 18px; font-weight: 760; color: var(--color-text); letter-spacing: -0.02em; }
        .events-panel-subtitle { margin: 6px 0 0; font-size: 13px; color: var(--color-text-secondary); }
        .events-panel-body { padding: 16px 20px 20px; }
        .events-month-nav { display: flex; align-items: center; gap: 12px; }
        .events-month-btn {
            width: 34px; height: 34px; border-radius: 12px; border: 1px solid rgba(226,232,240,0.92); background: rgba(255,255,255,0.88);
            cursor: pointer; font-size: 18px; color: var(--color-text);
        }
        .events-summary-chips { display: flex; flex-wrap: wrap; gap: 8px; }
        .events-summary-chip {
            height: 44px; padding: 0 16px; border-radius: 999px; display: inline-flex; align-items: center; justify-content: center;
            background: rgba(245,158,11,0.08); color: #B45309; font-size: 12px; font-weight: 700; white-space: nowrap;
        }
        .events-calendar-shell { margin-top: 16px; }
        .events-calendar-weekdays,
        .events-calendar-grid { display: grid; grid-template-columns: repeat(7, minmax(0, 1fr)); gap: 8px; }
        .events-calendar-weekdays { margin-bottom: 8px; }
        .events-weekday {
            text-align: center; font-size: 12px; font-weight: 700; color: var(--color-text-secondary); padding: 6px 0;
        }
        .events-calendar-cell {
            min-height: 128px; padding: 10px; border-radius: 18px; position: relative;
            border: 1px solid rgba(226,232,240,0.92); background: rgba(255,255,255,0.86); transition: transform 0.16s ease, border-color 0.16s ease, box-shadow 0.16s ease;
        }
        .events-calendar-cell:not(.outside):hover { transform: translateY(-1px); border-color: rgba(245,158,11,0.26); box-shadow: 0 12px 24px rgba(245,158,11,0.08); }
        .events-calendar-cell.outside { opacity: 0.28; background: rgba(248,250,252,0.7); }
        .events-calendar-cell.selected { border-color: rgba(245,158,11,0.38); box-shadow: inset 0 0 0 1px rgba(245,158,11,0.18); background: linear-gradient(180deg, rgba(255,251,235,0.96), rgba(255,255,255,0.92)); }
        .events-calendar-cell.today .events-calendar-date {
            background: var(--color-events); color: #fff; box-shadow: 0 8px 18px rgba(245,158,11,0.24);
        }
        .events-calendar-head {
            display: flex; width: 100%; align-items: center; justify-content: flex-start; gap: 8px;
            margin-bottom: 8px; min-height: 28px; padding: 0 34px 0 0; border: 0;
            background: transparent; color: inherit; text-align: left; font: inherit; cursor: pointer;
        }
        .events-calendar-head:disabled { cursor: default; }
        .events-calendar-date {
            width: 28px; min-width: 28px; max-width: 28px; height: 28px; display: inline-flex; align-items: center; justify-content: center; flex: 0 0 28px; aspect-ratio: 1 / 1;
            border-radius: 999px; font-size: 13px; font-weight: 700; color: var(--color-text);
        }
        .events-calendar-items { display: flex; flex-direction: column; gap: 3px; margin-top: auto; min-height: 0; overflow: hidden; }
        .events-calendar-chip {
            --events-calendar-chip-color: rgba(245,158,11,0.9);
            display: flex; width: 100%; text-align: left; border: none; cursor: pointer; align-items: center; gap: 6px;
            min-height: 20px; padding: 2px 4px 2px 0; border-radius: 0; background: transparent;
            color: var(--color-text); font-size: 11px; font-weight: 600; line-height: 1.25;
            overflow: hidden;
        }
        .events-calendar-chip::before {
            content: ''; flex: 0 0 auto; width: 6px; height: 6px; border-radius: 999px; background: var(--events-calendar-chip-color);
        }
        .events-calendar-chip.multi_node { --events-calendar-chip-color: #818CF8; }
        .events-calendar-chip.recurring { --events-calendar-chip-color: #86EFAC; }
        .events-calendar-chip-text {
            display: block; min-width: 0; overflow: hidden; text-overflow: ellipsis; white-space: nowrap;
        }
        .events-calendar-overflow {
            font-size: 11px; font-weight: 700; color: var(--color-text-secondary);
            padding-left: 12px; line-height: 1.25;
        }
        .events-calendar-overflow-suffix { display: inline; }
        .events-day-shell { display: flex; flex-direction: column; gap: 14px; }
        .events-day-head {
            display: flex; align-items: center; justify-content: space-between; gap: 10px; padding: 16px 18px;
            border-radius: 20px; background: linear-gradient(180deg, rgba(255,251,235,0.94), rgba(255,255,255,0.98));
            border: 1px solid rgba(245,158,11,0.16);
        }
        .events-day-title { margin: 0; font-size: 18px; font-weight: 760; color: var(--color-text); }
        .events-day-subtitle { margin: 6px 0 0; font-size: 13px; color: var(--color-text-secondary); }
        .events-timeline { display: flex; flex-direction: column; gap: 8px; }
        .events-timeline-continuous { gap: 2px; }
        .events-timeline-day-marker {
            display: grid; grid-template-columns: 68px 14px minmax(0, 1fr) auto; gap: 10px; align-items: center;
            padding: 10px 0 4px;
        }
        .events-timeline-day-label {
            grid-column: 1 / 3; font-size: 13px; font-weight: 800; color: var(--color-text);
            display: inline-flex; align-items: center; gap: 8px;
        }
        .events-timeline-day-meta { font-size: 12px; color: var(--color-text-secondary); }
        .events-timeline-item {
            position: relative; display: grid; grid-template-columns: 68px 14px minmax(0, 1fr) auto; gap: 10px;
            align-items: start; padding: 4px 0;
        }
        .events-timeline-time { font-size: 12px; font-weight: 800; color: var(--color-events); padding-top: 10px; text-align: right; }
        .events-timeline-track { position: relative; min-height: 100%; display: flex; justify-content: center; }
        .events-timeline-track::before {
            content: ''; position: absolute; left: 50%; top: 0; bottom: -8px; width: 2px; transform: translateX(-50%);
            background: linear-gradient(180deg, rgba(245,158,11,0.24), rgba(226,232,240,0.16));
        }
        .events-timeline-item:last-child .events-timeline-track::before { bottom: 14px; }
        .events-timeline-dot {
            position: relative; z-index: 1; width: 10px; height: 10px; border-radius: 999px; margin-top: 12px;
            background: var(--color-events); box-shadow: 0 0 0 4px rgba(245,158,11,0.12);
        }
        .events-timeline-dot.multi-node { background: #6366F1; box-shadow: 0 0 0 4px rgba(99,102,241,0.12); }
        .events-timeline-dot.recurring { background: #10B981; box-shadow: 0 0 0 4px rgba(16,185,129,0.12); }
        .events-timeline-card {
            width: 100%; padding: 10px 12px; border-radius: 16px; border: 1px solid rgba(226,232,240,0.92);
            background: linear-gradient(180deg, rgba(255,255,255,0.98), rgba(248,250,252,0.94));
            text-align: left; font: inherit; cursor: pointer;
        }
        .events-timeline-title { display: block; font-size: 15px; font-weight: 700; color: var(--color-text); line-height: 1.35; }
        .events-timeline-subtitle { display: block; margin-top: 4px; font-size: 12px; color: var(--color-text-secondary); }
        .events-timeline-meta { display: flex; flex-wrap: wrap; gap: 6px; margin-top: 8px; }
        .events-pill {
            display: inline-flex; align-items: center; gap: 4px; padding: 5px 8px; border-radius: 999px;
            font-size: 11px; font-weight: 700; background: rgba(148,163,184,0.08); color: var(--color-text-secondary);
        }
        .events-pill.kind-multi-node { background: rgba(99,102,241,0.12); color: #4338CA; }
        .events-pill.kind-recurring { background: rgba(16,185,129,0.12); color: #0F766E; }
        .events-pill.kind-single { background: rgba(245,158,11,0.12); color: #B45309; }
        .events-timeline-empty { padding: 14px 0 0 0; }
        .events-empty {
            padding: 28px 18px; border-radius: 18px; background: rgba(248,250,252,0.84); border: 1px dashed rgba(148,163,184,0.28); text-align: center;
        }
        .events-empty strong { display: block; font-size: 14px; color: var(--color-text); }
        .events-empty p { margin: 8px 0 0; font-size: 12px; line-height: 1.7; color: var(--color-text-secondary); }
        .events-detail-shell { display: flex; flex-direction: column; gap: 18px; }
        .events-detail-summary {
            padding: 18px; border-radius: 18px; background: linear-gradient(180deg, rgba(255,251,235,0.94), rgba(255,255,255,0.98));
            border: 1px solid rgba(245,158,11,0.16);
        }
        .events-detail-title { margin: 0; font-size: 28px; font-weight: 820; color: var(--color-text); letter-spacing: -0.03em; }
        .events-detail-time { margin-top: 10px; font-size: 14px; color: var(--color-text-secondary); }
        .events-detail-meta { display: flex; flex-wrap: wrap; gap: 8px; margin-bottom: 12px; }
        .events-detail-collection { margin-bottom: 8px; font-size: 13px; font-weight: 800; color: var(--color-events); }
        .events-detail-notes { margin-top: 12px; font-size: 13px; line-height: 1.8; color: var(--color-text-secondary); white-space: pre-wrap; }
        .events-detail-block { padding: 16px 18px; border-radius: 18px; background: rgba(248,250,252,0.72); border: 1px solid rgba(226,232,240,0.92); }
        .events-detail-block h4 { margin: 0 0 12px; font-size: 14px; font-weight: 800; color: var(--color-text); }
        .events-detail-list { display: flex; flex-direction: column; gap: 10px; }
        .events-detail-row {
            display: flex; align-items: center; justify-content: space-between; gap: 12px; padding: 10px 0;
            border-bottom: 1px solid rgba(226,232,240,0.9);
        }
        .events-detail-row:last-child { border-bottom: none; padding-bottom: 0; }
        .events-detail-row:first-child { padding-top: 0; }
        .events-detail-row-title { font-weight: 700; color: var(--color-text); }
        .events-detail-row-meta { margin-top: 4px; font-size: 12px; color: var(--color-text-secondary); }
        .events-detail-empty { font-size: 13px; color: var(--color-text-secondary); }
        .events-detail-reminder-actions {
            display: flex; align-items: center; justify-content: flex-end; gap: 8px; flex-wrap: wrap;
        }
        .events-reminder-toggle { min-width: 76px; }
        .events-reminder-toggle:disabled { opacity: 0.42; cursor: not-allowed; box-shadow: none; }
        .events-status {
            padding: 5px 8px; border-radius: 999px; font-size: 11px; font-weight: 800;
            background: rgba(148,163,184,0.08); color: var(--color-text-secondary);
        }
        .events-status.pending { background: rgba(245,158,11,0.12); color: #B45309; }
        .events-status.sent { background: rgba(99,102,241,0.12); color: #4338CA; }
        .events-status.confirmed { background: rgba(16,185,129,0.12); color: #0F766E; }
        .events-editor-grid { display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 12px; }
        .events-editor-field { display: flex; flex-direction: column; gap: 6px; }
        .events-editor-field.full { grid-column: 1 / -1; }
        .events-editor-field label { font-size: 12px; font-weight: 700; color: var(--color-text-secondary); }
        .events-editor-field input,
        .events-editor-field textarea,
        .events-editor-field select {
            width: 100%; border-radius: 14px; border: 1px solid rgba(203,213,225,0.92); background: rgba(255,255,255,0.96);
            padding: 10px 12px; font-size: 14px; color: var(--color-text);
        }
        .events-editor-field textarea { min-height: 100px; resize: vertical; }
        .events-editor-mode {
            display: inline-flex; gap: 6px; flex-wrap: wrap; padding: 4px; border-radius: 999px;
            background: rgba(248,250,252,0.94); border: 1px solid rgba(226,232,240,0.92);
        }
        .events-editor-mode button {
            border: none; background: transparent; color: var(--color-text-secondary); cursor: pointer;
            border-radius: 999px; padding: 8px 12px; font-size: 12px; font-weight: 800;
        }
        .events-editor-mode button.active { background: rgba(245,158,11,0.14); color: var(--color-events); }
        .events-editor-rows { display: flex; flex-direction: column; gap: 8px; }
        .events-editor-row { display: grid; gap: 8px; align-items: center; }
        .events-editor-row[data-reminder-row] { grid-template-columns: minmax(0, 1fr) auto; }
        .events-editor-row[data-node-row] {
            grid-template-columns: minmax(0, 1fr) minmax(170px, 0.8fr) minmax(0, 1fr) auto;
        }
        .events-editor-row button {
            width: 34px; height: 34px; border-radius: 12px; border: 1px solid rgba(226,232,240,0.92); background: rgba(255,255,255,0.92);
            cursor: pointer; color: var(--color-text-secondary);
        }
        .events-editor-add {
            margin-top: 8px; border: 1px dashed rgba(245,158,11,0.28); background: rgba(255,251,235,0.84);
            color: var(--color-events); border-radius: 14px; padding: 10px 12px; cursor: pointer; font-weight: 700; width: 100%;
        }
        .events-editor-note {
            font-size: 12px; line-height: 1.7; color: var(--color-text-secondary); padding: 10px 12px;
            border-radius: 14px; background: rgba(248,250,252,0.78); border: 1px solid rgba(226,232,240,0.92);
        }
        .events-editor-field[hidden] { display: none; }
        .events-toggle-btn:focus-visible,
        .events-range-btn:focus-visible,
        .events-month-btn:focus-visible,
        .events-calendar-head:focus-visible,
        .events-calendar-chip:focus-visible,
        .events-timeline-card:focus-visible,
        .events-reminder-toggle:focus-visible,
        .events-editor-mode button:focus-visible,
        .events-editor-row button:focus-visible,
        .events-editor-add:focus-visible {
            outline: 3px solid rgba(245,158,11,0.32); outline-offset: 2px;
        }
        ${mediaMax(
            BREAKPOINTS.XL,
            `
            .events-grid { grid-template-columns: 1fr; }
            .events-filters { grid-template-columns: repeat(2, minmax(0, 1fr)); }
        `,
        )}
        ${mediaMax(
            BREAKPOINTS.MOBILE,
            `
            .events-hero { grid-template-columns: 1fr; }
            .events-hero-actions { justify-content: flex-start; row-gap: 8px; }
            .events-hero-actions .events-summary-chip { width: auto; flex: 0 0 auto; }
            .events-filters, .events-editor-grid { grid-template-columns: 1fr; }
            .events-editor-row { grid-template-columns: minmax(0, 1fr); }
            .events-panel-header { align-items: start; }
            .events-panel-subtitle { display: none; }
            .events-timeline-tools, .events-timeline-toolbar { width: 100%; align-items: stretch; justify-content: flex-start; }
            .events-inline-dates { display: grid; grid-template-columns: 1fr; width: 100%; }
            .events-inline-dates .events-inline-date { width: 100%; }
            .events-summary-chips { gap: 6px; }
            .events-summary-chip { height: 38px; padding: 0 12px; font-size: 11px; }
            .events-calendar-shell { margin-top: 14px; }
            .events-calendar-grid, .events-calendar-weekdays { gap: 6px; }
            .events-calendar-cell {
                min-height: 78px; aspect-ratio: auto; padding: 6px 5px; border-radius: 16px;
                display: flex; flex-direction: column; justify-content: flex-start;
            }
            .events-calendar-head { margin-bottom: 4px; align-items: flex-start; position: relative; min-height: 24px; padding-right: 0; }
            .events-calendar-date { width: 24px; min-width: 24px; max-width: 24px; height: 24px; flex: 0 0 24px; font-size: 12px; }
            .events-calendar-items { gap: 4px; margin-top: 0; }
            .events-calendar-chip {
                min-height: 0; padding: 0; font-size: 10px; line-height: 1; gap: 0; align-items: stretch;
            }
            .events-calendar-chip::before { width: calc(100% - 12px); height: 6px; border-radius: 999px; margin-left: 2px; }
            .events-calendar-chip-text { display: none; }
            .events-calendar-overflow { font-size: 9px; padding-left: 0; text-align: right; line-height: 1; }
            .events-calendar-overflow-suffix { display: none; }
            .events-weekday { font-size: 11px; padding: 2px 0 4px; }
            .events-timeline-day-marker { grid-template-columns: 52px 14px minmax(0, 1fr); padding-top: 8px; }
            .events-timeline-item > :last-child { display: none; }
            .events-timeline-day-label { font-size: 12px; }
            .events-timeline-item { grid-template-columns: 52px 14px minmax(0, 1fr); gap: 8px; }
            .events-timeline-time { padding-top: 8px; font-size: 11px; }
            .events-timeline-card { padding: 9px 10px; }
            .events-detail-row { align-items: flex-start; }
            .events-detail-reminder-actions { flex: 0 0 auto; }
        `,
        )}
        ${mediaMax(
            BREAKPOINTS.PHONE,
            `
            .events-summary-chips { display: grid; grid-template-columns: repeat(3, minmax(0, 1fr)); }
            .events-summary-chips .events-summary-chip { width: 100%; justify-content: center; padding: 0 8px; font-size: 10px; }
            .events-weekday { font-size: 10px; letter-spacing: 0; }
            .events-calendar-grid, .events-calendar-weekdays { gap: 4px; }
            .events-calendar-shell { margin-top: 12px; }
            .events-calendar-cell { min-height: 62px; padding: 4px; border-radius: 14px; }
            .events-calendar-date { width: 22px; min-width: 22px; max-width: 22px; height: 22px; flex: 0 0 22px; font-size: 11px; }
            .events-calendar-head { margin-bottom: 3px; min-height: 22px; }
            .events-calendar-items { gap: 3px; }
            .events-calendar-chip::before { height: 5px; }
            .events-calendar-overflow { font-size: 8px; }
        `,
        )}
    `,
    );
}

function renderHero() {
    const summary = _state.overview?.summary || {};
    const range = currentRange();
    return `
        <section class="events-hero">
            <div>
                <h2>📅 日程</h2>
                <p>${_state.viewMode === 'calendar' ? `查看 ${range.start} 到 ${range.end} 的月度安排。` : '按时间顺序浏览当前范围内的日程。'}</p>
            </div>
            <div class="events-hero-actions">
                <div class="events-view-toggle" role="group" aria-label="日程视图">
                    <button type="button" class="events-toggle-btn ${_state.viewMode === 'calendar' ? 'active' : ''}" data-view-mode="calendar" aria-pressed="${_state.viewMode === 'calendar'}">月历</button>
                    <button type="button" class="events-toggle-btn ${_state.viewMode === 'timeline' ? 'active' : ''}" data-view-mode="timeline" aria-pressed="${_state.viewMode === 'timeline'}">时间线</button>
                </div>
                ${summary.event_count ? `<span class="events-summary-chip">${summary.event_count} 条日程</span>` : ''}
                <button type="button" class="btn btn-primary" id="events-add-top">＋ 新建日程</button>
            </div>
        </section>`;
}

function renderFilters() {
    const filters = _state.filters;
    const categoryOptionsList = [
        { value: '', label: '全部分类' },
        ...(_state.overview?.categories || []).map((category) => ({ value: category, label: category })),
    ];
    const kindOptions = [
        { value: 'all', label: '全部事件' },
        { value: 'single', label: '单次事件' },
        { value: 'multi_node', label: '多节点事件' },
        { value: 'recurring', label: '重复实例' },
    ];
    const reminderOptions = [
        { value: 'all', label: '全部提醒' },
        { value: 'with', label: '有提醒' },
        { value: 'pending', label: '待发送' },
        { value: 'sent', label: '已发送' },
        { value: 'confirmed', label: '已确认' },
        { value: 'none', label: '无提醒' },
    ];
    return `
        <section class="events-filters">
            <div class="events-filter-field">
                <label for="events-filter-keyword">搜索</label>
                <input id="events-filter-keyword" type="search" placeholder="标题、地点、备注、节点名称" value="${escapeHtml(filters.keyword)}" aria-label="搜索日程">
            </div>
            <div class="events-filter-field">
                <label id="events-filter-category-label">分类</label>
                ${renderCustomSelect({
                    id: 'events-filter-category',
                    options: categoryOptionsList,
                    selected: filters.category,
                    className: 'pselect-block pselect-theme-events',
                    placeholder: '全部分类',
                    labelledBy: 'events-filter-category-label',
                })}
            </div>
            <div class="events-filter-field">
                <label id="events-filter-kind-label">事件类型</label>
                ${renderCustomSelect({
                    id: 'events-filter-kind',
                    options: kindOptions,
                    selected: filters.kind,
                    className: 'pselect-block pselect-theme-events',
                    placeholder: '全部事件',
                    labelledBy: 'events-filter-kind-label',
                })}
            </div>
            <div class="events-filter-field">
                <label id="events-filter-reminder-label">提醒状态</label>
                ${renderCustomSelect({
                    id: 'events-filter-reminder',
                    options: reminderOptions,
                    selected: filters.reminder,
                    className: 'pselect-block pselect-theme-events',
                    placeholder: '全部提醒',
                    labelledBy: 'events-filter-reminder-label',
                })}
            </div>
        </section>`;
}

function renderCalendarCell(day, currentMonth, calendarDay, today) {
    const dayString = isoDate(day);
    const outside = day.getMonth() !== currentMonth;
    const selected = _state.selectedDate === dayString;
    const classes = ['events-calendar-cell'];
    if (outside) classes.push('outside');
    if (selected) classes.push('selected');
    if (today === dayString) classes.push('today');

    const items = Array.isArray(calendarDay?.items)
        ? calendarDay.items.filter((item) => item && typeof item === 'object')
        : [];
    const count = Math.max(finiteCount(calendarDay?.count), items.length);
    const visibleItems = items.slice(0, CALENDAR_VISIBLE_ITEM_LIMIT);
    const selectionLabel = `${dayString}，${count} 条日程`;
    return `
        <div class="${classes.join(' ')}">
            <button type="button" class="events-calendar-head" data-select-day="${dayString}" aria-label="${selectionLabel}" aria-pressed="${selected}" ${outside ? 'disabled' : ''}>
                <span class="events-calendar-date">${day.getDate()}</span>
            </button>
            <div class="events-calendar-items">
                ${visibleItems
                    .map((item) => {
                        const eventId = String(item.event_id ?? '');
                        const label = String(item.label ?? '(无标题)');
                        return `<button type="button" class="events-calendar-chip ${normalizedKind(item.kind)}" data-event-id="${escapeHtml(eventId)}" title="${escapeHtml(label)}" aria-label="查看日程：${escapeHtml(label)}" ${eventId ? '' : 'disabled'}><span class="events-calendar-chip-text">${escapeHtml(label)}</span></button>`;
                    })
                    .join('')}
                ${count > visibleItems.length ? `<div class="events-calendar-overflow">+${count - visibleItems.length}<span class="events-calendar-overflow-suffix"> 更多</span></div>` : ''}
            </div>
        </div>`;
}

function renderCalendarPanel() {
    const overview = _state.overview;
    const calendarDays = overview?.calendar_days || {};
    const start = firstDayOfMonth(_state.monthCursor);
    const daysInThisMonth = lastDayOfMonth(_state.monthCursor).getDate();
    const offset = weekdayIndexMonday(start);
    const totalCells = Math.ceil((offset + daysInThisMonth) / 7) * 7;
    const cells = [];
    const currentMonth = _state.monthCursor.getMonth();
    const today = isoDate(new Date());

    for (let index = 0; index < totalCells; index += 1) {
        const dayNumber = index - offset + 1;
        const cellDate = new Date(_state.monthCursor.getFullYear(), _state.monthCursor.getMonth(), dayNumber);
        const key = isoDate(cellDate);
        cells.push(renderCalendarCell(cellDate, currentMonth, calendarDays[key], today));
    }

    return `
        <section class="events-panel">
            <div class="events-panel-header">
                <div>
                    <h3 class="events-panel-title">${formatMonthLabel(_state.monthCursor)}</h3>
                    <p class="events-panel-subtitle">按月查看每天的日程分布。</p>
                </div>
                <div class="events-month-nav">
                    <button type="button" class="events-month-btn" id="events-prev-month" aria-label="上个月">‹</button>
                    <button type="button" class="events-month-btn" id="events-next-month" aria-label="下个月">›</button>
                </div>
            </div>
            <div class="events-panel-body">
                <div class="events-summary-chips">
                    <span class="events-summary-chip">${overview?.summary?.event_count || 0} 条日程</span>
                    <span class="events-summary-chip">${overview?.summary?.multi_node_count || 0} 条多节点</span>
                    <span class="events-summary-chip">${overview?.summary?.reminder_count || 0} 个提醒</span>
                </div>
                <div class="events-calendar-shell">
                    <div class="events-calendar-weekdays">
                        ${WEEKDAYS.map((weekday) => `<div class="events-weekday">${weekday}</div>`).join('')}
                    </div>
                    <div class="events-calendar-grid">${cells.join('')}</div>
                </div>
            </div>
        </section>`;
}

function kindPill(kind) {
    if (kind === 'multi_node') return '<span class="events-pill kind-multi-node">多节点</span>';
    if (kind === 'recurring') return '<span class="events-pill kind-recurring">重复实例</span>';
    return '<span class="events-pill kind-single">单次事件</span>';
}

function detailActionNoun(event) {
    if (
        event?.kind === 'multi_node' ||
        event?.event_role === 'multi_node_child' ||
        event?.event_collection_kind === 'multi_node'
    ) {
        return '节点';
    }
    if (
        event?.kind === 'recurring' ||
        event?.event_role === 'recurring_occurrence' ||
        event?.event_collection_kind === 'recurring'
    ) {
        return '实例';
    }
    return '事件';
}

function renderTimelineEntries(items) {
    if (!Array.isArray(items) || !items.length) return '';

    const events = new Map(
        (_state.overview?.events || [])
            .filter((event) => event && typeof event === 'object')
            .map((event) => [String(event.id ?? ''), event]),
    );
    return items
        .filter((item) => item && typeof item === 'object')
        .map((item) => {
            const eventId = String(item.event_id ?? '');
            const event = events.get(eventId);
            const itemKind = normalizedKind(item.kind);
            const dotKind = itemKind === 'multi_node' ? 'multi-node' : itemKind === 'recurring' ? 'recurring' : '';
            const collectionTitle = item.collection?.title || event?.collection?.title || '';
            const title = collectionTitle
                ? `${collectionTitle} · ${item.title || event?.title || '(无标题)'}`
                : item.title || '(无标题)';
            const reminderTotal = finiteCount(item.reminder_total);
            return `
                <article class="events-timeline-item">
                    <div class="events-timeline-time">${escapeHtml(item.time_label || '全天')}</div>
                    <div class="events-timeline-track"><span class="events-timeline-dot ${dotKind}"></span></div>
                    <button type="button" class="events-timeline-card" data-event-id="${escapeHtml(eventId)}" aria-label="查看日程：${escapeHtml(title)}" ${eventId ? '' : 'disabled'}>
                        <span class="events-timeline-title">${escapeHtml(title)}</span>
                        <span class="events-timeline-subtitle">${escapeHtml(item.subtitle || event?.time_summary || '')}</span>
                        <span class="events-timeline-meta">
                            ${kindPill(event?.kind || itemKind)}
                            ${event?.category ? `<span class="events-pill">${escapeHtml(event.category)}</span>` : ''}
                            ${item.location ? `<span class="events-pill">📍 ${escapeHtml(item.location)}</span>` : ''}
                            ${reminderTotal ? `<span class="events-pill">🔔 ${reminderTotal} 个提醒</span>` : ''}
                        </span>
                    </button>
                    <button type="button" class="btn btn-secondary btn-sm" data-open-detail="${escapeHtml(eventId)}" ${eventId ? '' : 'disabled'}>详情</button>
                </article>`;
        })
        .join('');
}

function renderTimelineItems(items) {
    if (!items.length) {
        return `
            <div class="events-empty">
                <strong>这一天没有日程</strong>
                <p>可以直接新建一条，或者切到别的日期继续查看。</p>
            </div>`;
    }

    return `<div class="events-timeline">${renderTimelineEntries(items)}</div>`;
}

function renderSelectedDayPanel() {
    const selectedDate = _state.selectedDate;
    const items = (_state.overview?.timeline_days || []).find((row) => row.date === selectedDate)?.items || [];
    return `
        <section class="events-day-shell">
            <div class="events-day-head">
                <div>
                    <h3 class="events-day-title">${formatDateLabel(selectedDate)} · ${formatWeekday(selectedDate)}</h3>
                    <div class="events-day-subtitle">选中日期后查看当天日程、提醒和详情。</div>
                </div>
                <button type="button" class="btn btn-primary btn-sm" id="events-add-day" data-date="${selectedDate}">＋ 添加当天日程</button>
            </div>
            ${renderTimelineItems(items)}
        </section>`;
}

function renderTimelinePage() {
    const timelineDays = _state.overview?.timeline_days || [];
    return `
        <section class="events-panel">
            <div class="events-panel-header">
                <div>
                    <h3 class="events-panel-title">时间线列表</h3>
                    <p class="events-panel-subtitle">按时间顺序浏览当前范围内的日程。</p>
                </div>
                <div class="events-timeline-tools">
                    <div class="events-timeline-toolbar">
                        <div class="events-list-range" role="group" aria-label="时间范围">
                            ${RANGE_PRESET_OPTIONS.map((option) => `<button type="button" class="events-range-btn ${_state.listRange === option.key ? 'active' : ''}" data-list-range="${option.key}" aria-pressed="${_state.listRange === option.key}">${option.label}</button>`).join('')}
                        </div>
                    </div>
                    ${
                        _state.listRange === 'custom'
                            ? `
                        <div class="events-inline-dates">
                            <input class="events-inline-date" id="events-custom-start" type="text" inputmode="numeric" placeholder="YYYY-MM-DD" value="${escapeHtml(_state.customStart)}" aria-label="自定义开始日期">
                            <span class="events-inline-separator">至</span>
                            <input class="events-inline-date" id="events-custom-end" type="text" inputmode="numeric" placeholder="YYYY-MM-DD" value="${escapeHtml(_state.customEnd)}" aria-label="自定义结束日期">
                            <button type="button" class="events-range-btn" id="events-custom-apply">应用</button>
                        </div>`
                            : ''
                    }
                </div>
            </div>
            <div class="events-panel-body">
                ${
                    timelineDays.length
                        ? `
                    <div class="events-timeline events-timeline-continuous">
                        ${timelineDays
                            .map(
                                (row) => `
                            <div class="events-timeline-day-marker">
                                <div class="events-timeline-day-label">${formatDateLabel(row.date)}</div>
                                <div></div>
                                <div class="events-timeline-day-meta">${formatWeekday(row.date)} · ${row.items.length} 条</div>
                            </div>
                            ${renderTimelineEntries(row.items)}
                        `,
                            )
                            .join('')}
                    </div>
                `
                        : `
                    <div class="events-empty events-timeline-empty">
                        <strong>当前筛选下没有日程</strong>
                        <p>可以放宽筛选条件，或者直接新建一条新的日程。</p>
                    </div>
                `
                }
            </div>
        </section>`;
}

function renderLoading() {
    return `
        <div class="events-panel">
            <div class="events-panel-body">
                <div class="events-empty">
                    <strong>加载中</strong>
                    <p>正在加载当前日程和提醒。</p>
                </div>
            </div>
        </div>`;
}

function renderCalendarPage() {
    return `
        <div class="events-grid">
            <div>${renderCalendarPanel()}</div>
            <div>${renderSelectedDayPanel()}</div>
        </div>`;
}

function renderPage() {
    if (!_container) return;
    ensureStyles();
    _container.innerHTML = `
        <div class="events-page">
            ${renderHero()}
            ${renderFilters()}
            ${_state.loading || !_state.overview ? renderLoading() : _state.viewMode === 'calendar' ? renderCalendarPage() : renderTimelinePage()}
        </div>
    `;
    attachPageListeners();
}

async function loadOverview(options = {}) {
    if (!_container) return;
    const container = _container;
    const requestVersion = ++_loadVersion;
    const preserveScroll = Boolean(options.preserveScroll);
    const scrollY = preserveScroll && Number.isFinite(window.scrollY) ? window.scrollY : 0;
    const shouldShowLoading = !_state.overview && !preserveScroll;

    _state.loading = shouldShowLoading;
    if (shouldShowLoading) renderPage();

    try {
        if (
            _state.viewMode !== 'calendar' &&
            _state.listRange === 'all' &&
            (!_state.allRangeStart || !_state.allRangeEnd)
        ) {
            const today = isoDate(new Date());
            const bounds = await fetchItemRangeBounds(api, {
                type: 'event',
                sortField: 'start_time',
                startField: 'start_time',
                endField: 'end_time',
                fallbackEnd: today,
                minimumEnd: today,
            });
            if (_container !== container || requestVersion !== _loadVersion) return;
            _state.allRangeStart = bounds.start;
            _state.allRangeEnd = bounds.end;
        }
        const range = currentRange();
        const res = await api.get('/events/overview', {
            start_date: range.start,
            end_date: range.end,
            keyword: _state.filters.keyword,
            category: _state.filters.category,
            kind: _state.filters.kind,
            reminder: _state.filters.reminder,
        });
        if (_container !== container || requestVersion !== _loadVersion) return;
        _state.overview = normalizeOverview(res?.data);

        const availableDays = _state.overview.timeline_days.map((row) => row.date);
        if (_state.viewMode === 'calendar') {
            if (!_state.selectedDate || !_state.overview.calendar_days?.[_state.selectedDate]) {
                _state.selectedDate = availableDays[0] || range.start;
            }
        } else if (_state.listRange === 'custom' && !_state.customStart && !_state.customEnd) {
            _state.customStart = range.start;
            _state.customEnd = range.end;
        }
    } catch (err) {
        if (_container !== container || requestVersion !== _loadVersion) return;
        _state.overview = {
            summary: { event_count: 0, multi_node_count: 0, reminder_count: 0 },
            categories: [],
            calendar_days: {},
            timeline_days: [],
            events: [],
        };
        showToast(`加载日程失败：${err?.message || '未知错误'}`, 'error');
    } finally {
        if (_container !== container || requestVersion !== _loadVersion) return;
        _state.loading = false;
        renderPage();
        if (preserveScroll) restoreScrollPosition(scrollY);
    }
}

function reminderRowHTML(value = '') {
    return `
        <div class="events-editor-row" data-reminder-row>
            <input type="text" inputmode="numeric" placeholder="YYYY-MM-DD HH:mm" class="events-editor-reminder-input" value="${escapeHtml(value)}" aria-label="提醒时间">
            <button type="button" data-remove-row aria-label="删除提醒时间">×</button>
        </div>`;
}

function nodeRowHTML(name = '', time = '', notes = '') {
    return `
        <div class="events-editor-row" data-node-row>
            <input type="text" class="events-editor-node-name" placeholder="节点名称" value="${escapeHtml(name)}" aria-label="节点名称">
            <input type="text" inputmode="numeric" placeholder="YYYY-MM-DD HH:mm" class="events-editor-node-time" value="${escapeHtml(time)}" aria-label="节点时间">
            <input type="text" class="events-editor-node-notes" placeholder="节点备注（仅该节点提醒显示）" value="${escapeHtml(notes)}" aria-label="节点备注">
            <button type="button" data-remove-row aria-label="删除节点">×</button>
        </div>`;
}

function editorModalHTML(existing = null, prefillDate = '', userTimeZone) {
    const defaultDate = isValidDateInput(prefillDate) ? prefillDate : '';
    const startValue = existing?.start_time
        ? toInputDateTime(existing.start_time, userTimeZone)
        : defaultDate
          ? `${defaultDate}T09:00`
          : '';
    const endValue = existing?.end_time ? toInputDateTime(existing.end_time, userTimeZone) : '';
    const reminders = (Array.isArray(existing?.reminders) ? existing.reminders : [])
        .map((row) => toInputDateTime(row?.time, userTimeZone))
        .filter(Boolean);
    const initialReminderValues = reminders.length ? reminders : existing ? [] : [startValue];
    const initialNodes = [
        { time: defaultDate ? `${defaultDate}T09:00:00` : '' },
        { time: defaultDate ? `${defaultDate}T18:00:00` : '' },
    ];

    return `
        <form id="events-editor-form" data-mode="single">
            <div class="events-editor-grid">
                <div class="events-editor-field full">
                    <label for="events-editor-title">标题</label>
                    <input id="events-editor-title" name="title" type="text" value="${escapeHtml(existing?.title || '')}" placeholder="比如：产品评审、提审节点、出发前准备">
                </div>
                <div class="events-editor-field">
                    <label for="events-editor-category">分类</label>
                    <input id="events-editor-category" name="category" type="text" value="${escapeHtml(existing?.category || '')}" placeholder="会议、项目、个人等">
                </div>
                <div class="events-editor-field">
                    <label for="events-editor-location">地点</label>
                    <input id="events-editor-location" name="location" type="text" value="${escapeHtml(existing?.location || '')}" placeholder="可选">
                </div>
                ${
                    existing
                        ? ''
                        : `<div class="events-editor-field full">
                    <label>事件模式</label>
                    <div class="events-editor-mode" id="events-editor-mode">
                        <button type="button" class="active" data-editor-mode="single" aria-pressed="true">单次事件</button>
                        <button type="button" data-editor-mode="multi_node" aria-pressed="false">多节点事件</button>
                    </div>
                </div>`
                }
                <div class="events-editor-field" data-single-section>
                    <label for="events-editor-start-time">开始时间</label>
                    <input id="events-editor-start-time" name="start_time" type="text" inputmode="numeric" placeholder="YYYY-MM-DD HH:mm" value="${escapeHtml(startValue)}">
                </div>
                <div class="events-editor-field" data-single-section>
                    <label for="events-editor-end-time">结束时间</label>
                    <input id="events-editor-end-time" name="end_time" type="text" inputmode="numeric" placeholder="YYYY-MM-DD HH:mm" value="${escapeHtml(endValue)}">
                </div>
                ${
                    existing
                        ? ''
                        : `<div class="events-editor-field full" data-node-section hidden>
                    <label>时间节点</label>
                    <div class="events-editor-note">多节点事件会按节点时间生成当天时间线；保存时会自动用首尾节点推导整体起止时间。</div>
                    <div class="events-editor-rows" id="events-editor-nodes">
                        ${initialNodes.map((row) => nodeRowHTML('', toInputDateTime(row.time, userTimeZone), '')).join('')}
                    </div>
                    <button type="button" class="events-editor-add" id="events-add-node">＋ 添加节点</button>
                </div>`
                }
                <div class="events-editor-field full">
                    <label>提醒</label>
                    <div class="events-editor-rows" id="events-editor-reminders">
                        ${initialReminderValues
                            .filter(Boolean)
                            .map((value) => reminderRowHTML(value))
                            .join('')}
                    </div>
                    <button type="button" class="events-editor-add" id="events-add-reminder">＋ 添加提醒时间</button>
                </div>
                <div class="events-editor-field full">
                    <label for="events-editor-notes">备注</label>
                    <textarea id="events-editor-notes" name="notes" placeholder="可选，用来记录背景、材料链接或执行说明">${escapeHtml(existing?.notes || '')}</textarea>
                </div>
                ${
                    existing?.series_id
                        ? `
                    <div class="events-editor-field full">
                        <div class="events-editor-note">当前是重复日程实例。Web 端编辑和删除只作用于当前这一条，不会批量改整个系列。</div>
                    </div>`
                        : ''
                }
            </div>
        </form>`;
}

function setEditorMode(content, mode) {
    const form = content.querySelector('#events-editor-form');
    if (!form || !['single', 'multi_node'].includes(mode)) return;
    form.dataset.mode = mode;
    content.querySelectorAll('[data-editor-mode]').forEach((button) => {
        const active = button.dataset.editorMode === mode;
        button.classList.toggle('active', active);
        button.setAttribute('aria-pressed', String(active));
    });
    content.querySelectorAll('[data-single-section]').forEach((section) => {
        section.hidden = mode !== 'single';
    });
    content.querySelectorAll('[data-node-section]').forEach((section) => {
        section.hidden = mode !== 'multi_node';
    });
}

function collectEditorPayload(content, userTimeZone) {
    const form = content.querySelector('#events-editor-form');
    if (!form) throw new Error('日程编辑器未正确加载');
    const mode = form.dataset.mode === 'multi_node' ? 'multi_node' : 'single';
    const title = form.querySelector('[name="title"]')?.value.trim() || '';
    const category = form.querySelector('[name="category"]')?.value.trim() || '未分类';
    const location = form.querySelector('[name="location"]')?.value.trim() || '';
    const notes = form.querySelector('[name="notes"]')?.value.trim() || '';
    const reminderInputs = [...form.querySelectorAll('.events-editor-reminder-input')]
        .map((input) => input.value.trim())
        .filter(Boolean);
    const remindTimes = reminderInputs.map((value) => inputToIso(value, userTimeZone));

    if (!title) throw new Error('请填写标题');
    if (remindTimes.some((value) => !value)) throw new Error('提醒时间格式无效');
    const uniqueRemindTimes = [...new Set(remindTimes)].sort();

    const payload = { title, category, location, notes, timezone: userTimeZone };

    if (mode === 'multi_node') {
        const nodes = [];
        for (const row of form.querySelectorAll('[data-node-row]')) {
            const name = row.querySelector('.events-editor-node-name')?.value.trim() || '';
            const rawTime = row.querySelector('.events-editor-node-time')?.value.trim() || '';
            const nodeNotes = row.querySelector('.events-editor-node-notes')?.value.trim() || '';
            if (!name && !rawTime && !nodeNotes) continue;
            const time = inputToIso(rawTime, userTimeZone);
            if (!name) throw new Error('请填写节点名称');
            if (!time) throw new Error(`节点“${name}”的时间格式无效`);
            nodes.push({ name, time, ...(nodeNotes ? { notes: nodeNotes } : {}) });
        }
        nodes.sort((a, b) => a.time.localeCompare(b.time));

        if (nodes.length < 2) throw new Error('多节点事件至少需要 2 个有效节点');
        if (uniqueRemindTimes.some((value) => value > nodes[0].time)) {
            throw new Error('提醒时间不能晚于第一个节点');
        }
        payload.kind = 'multi_node';
        payload.reminder_rules = reminderRulesFromTimes(nodes[0].time, uniqueRemindTimes);
        payload.children = nodes.map((row) => ({
            title: row.name,
            start_time: row.time,
            ...(row.notes ? { notes: row.notes } : {}),
        }));
    } else {
        const startTime = inputToIso(form.querySelector('[name="start_time"]')?.value, userTimeZone);
        const rawEndTime = form.querySelector('[name="end_time"]')?.value.trim() || '';
        const endTime = inputToIso(rawEndTime, userTimeZone);
        if (!startTime) throw new Error('请填写有效的开始时间');
        if (rawEndTime && !endTime) throw new Error('结束时间格式无效');
        if (endTime && endTime < startTime) throw new Error('结束时间不能早于开始时间');
        if (uniqueRemindTimes.some((value) => value > startTime)) {
            throw new Error('提醒时间不能晚于开始时间');
        }
        payload.start_time = startTime;
        payload.end_time = endTime || null;
        payload.reminder_rules = reminderRulesFromTimes(startTime, uniqueRemindTimes);
    }

    return payload;
}

async function openEventEditor(existing = null, prefillDate = '') {
    let userTimeZone;
    try {
        userTimeZone = await fetchUserTimeZone();
    } catch (error) {
        showToast(`无法读取用户时区：${error?.message || '未知错误'}`, 'error');
        return;
    }
    const existingTimes = [
        existing?.start_time,
        existing?.end_time,
        ...(Array.isArray(existing?.reminders) ? existing.reminders.map((row) => row?.time) : []),
    ].filter(Boolean);
    if (existingTimes.some((value) => !toInputDateTime(value, userTimeZone))) {
        showToast('日程包含无法解析的时间，已阻止编辑以免覆盖原值', 'error');
        return;
    }
    ensureStyles();
    const title = existing ? '编辑日程' : '新建日程';
    const content = showModal(title, safeHtml(editorModalHTML(existing, prefillDate, userTimeZone)), {
        footer: safeHtml(`
            <button type="button" class="btn btn-secondary" id="events-editor-cancel">取消</button>
            <button type="button" class="btn btn-primary" id="events-editor-save">保存</button>
        `),
    });

    content.querySelector('#events-editor-cancel').onclick = closeModal;
    content.querySelector('#events-add-reminder').onclick = () => {
        content.querySelector('#events-editor-reminders').insertAdjacentHTML('beforeend', reminderRowHTML(''));
    };
    const addNodeButton = content.querySelector('#events-add-node');
    if (addNodeButton) {
        addNodeButton.onclick = () => {
            content.querySelector('#events-editor-nodes').insertAdjacentHTML('beforeend', nodeRowHTML('', '', ''));
        };
    }

    content.addEventListener('click', (event) => {
        const modeButton = event.target.closest('[data-editor-mode]');
        if (modeButton) {
            setEditorMode(content, modeButton.dataset.editorMode);
            return;
        }
        const removeButton = event.target.closest('[data-remove-row]');
        if (removeButton) {
            const row = removeButton.closest('[data-reminder-row], [data-node-row]');
            if (row) row.remove();
        }
    });

    const saveButton = content.querySelector('#events-editor-save');
    const editorForm = content.querySelector('#events-editor-form');
    bindFormSubmit(editorForm, saveButton);
    let saving = false;
    saveButton.onclick = async () => {
        if (saving) return;
        saving = true;
        saveButton.disabled = true;
        try {
            const payload = collectEditorPayload(content, userTimeZone);
            if (existing) {
                const eventId = String(existing.id ?? '');
                if (!eventId) throw new Error('无法更新缺少编号的日程');
                await api.put(`/items/${encodeURIComponent(eventId)}`, payload);
                showToast('日程已更新', 'success');
            } else if (payload.children?.length) {
                await api.post('/events/collections', payload);
                showToast('多节点日程已创建', 'success');
            } else {
                await api.post('/items', { type: 'event', ...payload });
                showToast('日程已创建', 'success');
            }
            closeModal();
            window.dispatchEvent(new CustomEvent('pendo-data-changed', { detail: { type: 'event' } }));
        } catch (err) {
            showToast(err?.message || '保存失败', 'error');
        } finally {
            saving = false;
            saveButton.disabled = false;
        }
    };
}

async function deleteEvent(eventId, title = '这条日程') {
    const normalizedId = String(eventId ?? '');
    if (!normalizedId) throw new Error('无法删除缺少编号的日程');
    const confirmed = await showConfirmModal({
        title: '删除日程',
        message: `确定要删除“${title}”吗？删除后当前页面和时间线会立即移除它。`,
        confirmText: '删除',
        cancelText: '取消',
        tone: 'danger',
    });
    if (!confirmed) return;
    await api.delete(`/items/${encodeURIComponent(normalizedId)}`);
    showToast('日程已删除', 'success');
    window.dispatchEvent(new CustomEvent('pendo-data-changed', { detail: { type: 'event' } }));
}

async function deleteCollection(collectionId, title = '这个日程集合') {
    const normalizedId = String(collectionId ?? '');
    if (!normalizedId) throw new Error('无法删除缺少编号的日程集合');
    const confirmed = await showConfirmModal({
        title: '删除整个多节点日程',
        message: `确定要删除“${title}”以及它的全部节点吗？`,
        confirmText: '删除全部',
        cancelText: '取消',
        tone: 'danger',
    });
    if (!confirmed) return;
    await api.delete(`/events/collections/${encodeURIComponent(normalizedId)}`);
    showToast('多节点日程已删除', 'success');
    window.dispatchEvent(new CustomEvent('pendo-data-changed', { detail: { type: 'event' } }));
}

function collectionEditorHTML(collection = {}) {
    return `
        <form id="events-collection-editor-form" class="events-editor-grid">
            <div class="events-editor-field full">
                <label for="events-collection-title">集合标题</label>
                <input id="events-collection-title" name="title" type="text" value="${escapeHtml(collection.title || '')}">
            </div>
            <div class="events-editor-field">
                <label for="events-collection-category">分类</label>
                <input id="events-collection-category" name="category" type="text" value="${escapeHtml(collection.category || '')}">
            </div>
            <div class="events-editor-field">
                <label for="events-collection-location">地点</label>
                <input id="events-collection-location" name="location" type="text" value="${escapeHtml(collection.location || '')}">
            </div>
            <div class="events-editor-field full">
                <label for="events-collection-notes">备注</label>
                <textarea id="events-collection-notes" name="notes">${escapeHtml(collection.notes || '')}</textarea>
            </div>
        </form>`;
}

function openCollectionEditor(collection) {
    const collectionId = String(collection?.id ?? '');
    if (!collectionId) {
        showToast('无法编辑缺少编号的日程集合', 'error');
        return;
    }
    const content = showModal('编辑多节点日程', safeHtml(collectionEditorHTML(collection)), {
        footer: safeHtml(`
            <button type="button" class="btn btn-secondary" id="events-collection-editor-cancel">取消</button>
            <button type="button" class="btn btn-primary" id="events-collection-editor-save">保存</button>
        `),
    });
    content.querySelector('#events-collection-editor-cancel').onclick = closeModal;
    const saveButton = content.querySelector('#events-collection-editor-save');
    const editorForm = content.querySelector('#events-collection-editor-form');
    bindFormSubmit(editorForm, saveButton);
    let saving = false;
    saveButton.onclick = async () => {
        if (saving) return;
        if (!editorForm) {
            showToast('日程集合编辑器未正确加载', 'error');
            return;
        }
        const payload = {
            title: editorForm.querySelector('[name="title"]')?.value.trim() || '',
            category: editorForm.querySelector('[name="category"]')?.value.trim() || '未分类',
            location: editorForm.querySelector('[name="location"]')?.value.trim() || '',
            notes: editorForm.querySelector('[name="notes"]')?.value.trim() || '',
        };
        if (!payload.title) {
            showToast('请填写集合标题', 'error');
            return;
        }
        saving = true;
        saveButton.disabled = true;
        try {
            await api.put(`/events/collections/${encodeURIComponent(collectionId)}`, payload);
            showToast('多节点日程已更新', 'success');
            closeModal();
            window.dispatchEvent(new CustomEvent('pendo-data-changed', { detail: { type: 'event' } }));
        } catch (err) {
            showToast(`保存失败：${err?.message || '未知错误'}`, 'error');
        } finally {
            saving = false;
            saveButton.disabled = false;
        }
    };
}

function renderCollectionDetailBody(detail) {
    const collection = detail?.collection && typeof detail.collection === 'object' ? detail.collection : {};
    const children = Array.isArray(detail?.children)
        ? detail.children.filter((child) => child && typeof child === 'object')
        : [];
    return `
        <div class="events-detail-shell">
            <section class="events-detail-summary">
                <div class="events-detail-meta">
                    <span class="events-pill kind-multi-node">多节点</span>
                    ${collection.category ? `<span class="events-pill">${escapeHtml(collection.category)}</span>` : ''}
                    ${collection.location ? `<span class="events-pill">📍 ${escapeHtml(collection.location)}</span>` : ''}
                </div>
                <h3 class="events-detail-title">${escapeHtml(collection.title || '(无标题)')}</h3>
                ${collection.notes ? `<div class="events-detail-notes">${escapeHtml(collection.notes)}</div>` : ''}
            </section>
            <section class="events-detail-block">
                <h4>节点</h4>
                <div class="events-detail-list">
                    ${children
                        .map((child) => {
                            const childId = String(child.id ?? '');
                            return `
                        <div class="events-detail-row">
                            <div>
                                <div class="events-detail-row-title">${escapeHtml(child.title || '(无标题)')}</div>
                                <div class="events-detail-row-meta">${escapeHtml(formatEventDateTime(child.start_time))}${childId ? ` · ${escapeHtml(childId)}` : ''}</div>
                            </div>
                            <button type="button" class="btn btn-secondary btn-sm" data-open-detail="${escapeHtml(childId)}" ${childId ? '' : 'disabled'}>详情</button>
                        </div>
                    `;
                        })
                        .join('')}
                </div>
            </section>
        </div>`;
}

async function openCollectionDetail(collectionId) {
    const normalizedId = String(collectionId ?? '');
    if (!normalizedId) {
        showToast('无法加载缺少编号的日程集合', 'error');
        return;
    }
    try {
        const res = await api.get(`/events/collections/${encodeURIComponent(normalizedId)}/detail`);
        const detail = res?.data && typeof res.data === 'object' ? res.data : {};
        const collection = detail.collection;
        if (!collection || typeof collection !== 'object') throw new Error('集合数据不完整');
        const canMutate = Boolean(String(collection.id ?? ''));
        const content = showModal('多节点日程', safeHtml(renderCollectionDetailBody(detail)), {
            footer: safeHtml(`
                <button type="button" class="btn btn-secondary" id="events-collection-close">关闭</button>
                <button type="button" class="btn btn-primary" id="events-collection-edit" ${canMutate ? '' : 'disabled'}>编辑整体</button>
                <button type="button" class="btn btn-danger" id="events-collection-delete" ${canMutate ? '' : 'disabled'}>删除整体</button>
            `),
        });
        content.querySelector('#events-collection-close').onclick = closeModal;
        content.querySelector('#events-collection-edit').onclick = () => {
            closeModal();
            openCollectionEditor(collection);
        };
        content.querySelector('#events-collection-delete').onclick = async () => {
            closeModal();
            try {
                await deleteCollection(collection.id, collection.title || '这个日程集合');
            } catch (err) {
                showToast(`删除失败：${err?.message || '未知错误'}`, 'error');
            }
        };
        content.addEventListener('click', async (event) => {
            const trigger = event.target.closest('[data-open-detail]');
            if (trigger?.dataset.openDetail) {
                closeModal();
                await openEventDetail(trigger.dataset.openDetail);
            }
        });
    } catch (err) {
        showToast(`加载集合失败：${err?.message || '未知错误'}`, 'error');
    }
}

function renderDetailBody(detail) {
    const event = detail?.event && typeof detail.event === 'object' ? detail.event : {};
    const collection = event.collection && typeof event.collection === 'object' ? event.collection : null;
    const reminders = Array.isArray(event.reminders)
        ? event.reminders.filter((row) => row && typeof row === 'object')
        : [];
    const relatedInstances = Array.isArray(detail?.related_instances)
        ? detail.related_instances.filter((item) => item && typeof item === 'object')
        : [];
    const collectionId = String(collection?.id ?? '');
    return `
        <div class="events-detail-shell">
            <section class="events-detail-summary">
                <div class="events-detail-meta">
                    ${kindPill(event.kind)}
                    ${event.category ? `<span class="events-pill">${escapeHtml(event.category)}</span>` : ''}
                    ${event.location ? `<span class="events-pill">📍 ${escapeHtml(event.location)}</span>` : ''}
                </div>
                ${collection ? `<div class="events-detail-collection">${escapeHtml(collection.title || '多节点日程')}</div>` : ''}
                <h3 class="events-detail-title">${escapeHtml(event.title || '(无标题)')}</h3>
                <div class="events-detail-time">${escapeHtml(formatEventDateTime(event.start_time))}${event.end_time ? ` - ${escapeHtml(formatEventDateTime(event.end_time))}` : ''}</div>
                ${event.notes ? `<div class="events-detail-notes">${escapeHtml(event.notes)}</div>` : ''}
            </section>
            <section class="events-detail-block">
                <h4>提醒</h4>
                ${
                    reminders.length
                        ? `
                    <div class="events-detail-list">
                        ${reminders
                            .map((row) => {
                                const status = normalizedReminderStatus(row.status);
                                const repeatCount = finiteCount(row.repeat_count);
                                const canToggle = canToggleReminder(row.time);
                                const isConfirmed = status === 'confirmed';
                                return `
                            <div class="events-detail-row">
                                <div>
                                    <div class="events-detail-row-title">${escapeHtml(formatEventDateTime(row.time))}</div>
                                    <div class="events-detail-row-meta" data-reminder-meta>${escapeHtml(reminderMetaLabel(status, repeatCount, Boolean(row.sent_at)))}</div>
                                </div>
                                <div class="events-detail-reminder-actions">
                                    <button
                                        type="button"
                                        class="btn btn-secondary btn-sm events-reminder-toggle"
                                        data-toggle-reminder
                                        data-reminder-time="${escapeHtml(row.time)}"
                                        data-reminder-confirmed="${isConfirmed ? 'true' : 'false'}"
                                        ${canToggle ? '' : 'disabled'}
                                    >${isConfirmed ? '重新开启' : '提前确认'}</button>
                                    <span class="events-status ${status}" data-reminder-status>${reminderStatusLabel(status)}</span>
                                </div>
                            </div>
                        `;
                            })
                            .join('')}
                    </div>`
                        : '<div class="events-detail-empty">当前没有提醒。</div>'
                }
            </section>
            ${
                relatedInstances.length
                    ? `
                <section class="events-detail-block">
                    <h4>同系列后续实例</h4>
                    <div class="events-detail-list">
                        ${relatedInstances
                            .map(
                                (item) => `
                            <div class="events-detail-row">
                                <div>
                                    <div class="events-detail-row-title">${escapeHtml(item.title || '(无标题)')}</div>
                                    <div class="events-detail-row-meta">${escapeHtml(formatEventDateTime(item.start_time))}</div>
                                </div>
                            </div>
                        `,
                            )
                            .join('')}
                    </div>
                </section>`
                    : ''
            }
            ${
                collection
                    ? `
                <section class="events-detail-block">
                    <h4>整体</h4>
                    <button type="button" class="btn btn-secondary btn-sm" data-open-collection="${escapeHtml(collectionId)}" ${collectionId ? '' : 'disabled'}>管理“${escapeHtml(collection.title || '多节点日程')}”</button>
                </section>`
                    : ''
            }
        </div>`;
}

export async function openEventDetail(eventId) {
    ensureStyles();
    const normalizedId = String(eventId ?? '');
    if (!normalizedId) {
        showToast('无法加载缺少编号的日程', 'error');
        return;
    }
    try {
        const res = await api.get(`/events/${encodeURIComponent(normalizedId)}/detail`);
        const detail = res?.data && typeof res.data === 'object' ? res.data : {};
        const event = detail.event;
        if (!event || typeof event !== 'object') throw new Error('日程数据不完整');
        const actionNoun = detailActionNoun(event);
        const canMutate = Boolean(String(event.id ?? ''));
        const hasCollection = Boolean(String(event.collection?.id ?? ''));
        let stopReminderExpiryWatcher = () => {};
        const content = showModal('日程详情', safeHtml(renderDetailBody(detail)), {
            footer: safeHtml(`
                <button type="button" class="btn btn-secondary" id="events-detail-close">关闭</button>
                ${hasCollection ? '<button type="button" class="btn btn-secondary" id="events-detail-group">管理整体</button>' : ''}
                <button type="button" class="btn btn-primary" id="events-detail-edit" ${canMutate ? '' : 'disabled'}>编辑${actionNoun}</button>
                <button type="button" class="btn btn-danger" id="events-detail-delete" ${canMutate ? '' : 'disabled'}>删除${actionNoun}</button>
            `),
            onClose: () => stopReminderExpiryWatcher(),
        });
        stopReminderExpiryWatcher = watchReminderExpiry(content);
        content.querySelector('#events-detail-close').onclick = closeModal;
        const groupButton = content.querySelector('#events-detail-group');
        if (groupButton) {
            groupButton.onclick = async () => {
                closeModal();
                await openCollectionDetail(event.collection.id);
            };
        }
        content.querySelector('#events-detail-edit').onclick = () => {
            closeModal();
            openEventEditor(event, _state.selectedDate);
        };
        content.querySelector('#events-detail-delete').onclick = async () => {
            closeModal();
            try {
                await deleteEvent(event.id, event.title || '这条日程');
            } catch (err) {
                showToast(`删除失败：${err?.message || '未知错误'}`, 'error');
            }
        };
        content.addEventListener('click', async (clickEvent) => {
            const reminderButton = clickEvent.target.closest('[data-toggle-reminder]');
            if (reminderButton) {
                const remindTime = String(reminderButton.dataset.reminderTime ?? '');
                if (!canToggleReminder(remindTime)) {
                    reminderButton.disabled = true;
                    showToast('提醒时间已到，不能再修改确认状态', 'error');
                    return;
                }

                const shouldConfirm = reminderButton.dataset.reminderConfirmed !== 'true';
                reminderButton.disabled = true;
                try {
                    const response = await api.put(
                        `/events/${encodeURIComponent(normalizedId)}/reminders/confirmation`,
                        { remind_time: remindTime, confirmed: shouldConfirm },
                    );
                    const reminder = response?.data?.reminder;
                    const status = normalizedReminderStatus(reminder?.status);
                    const repeatCount = finiteCount(reminder?.repeat_count);
                    const row = reminderButton.closest('.events-detail-row');
                    const statusElement = row?.querySelector('[data-reminder-status]');
                    const metaElement = row?.querySelector('[data-reminder-meta]');

                    reminderButton.dataset.reminderConfirmed = status === 'confirmed' ? 'true' : 'false';
                    reminderButton.textContent = status === 'confirmed' ? '重新开启' : '提前确认';
                    reminderButton.disabled = !canToggleReminder(remindTime);
                    if (statusElement) {
                        statusElement.className = `events-status ${status}`;
                        statusElement.textContent = reminderStatusLabel(status);
                    }
                    if (metaElement) {
                        metaElement.textContent = reminderMetaLabel(
                            status,
                            repeatCount,
                            Boolean(reminder?.sent_at),
                        );
                    }

                    window.dispatchEvent(new CustomEvent('pendo-data-changed', { detail: { type: 'event' } }));
                    showToast(shouldConfirm ? '已提前确认，本次提醒不会发送' : '提醒已重新开启', 'success');
                } catch (err) {
                    const message = err?.message || '未知错误';
                    const expired = String(message).includes('提醒时间已到');
                    reminderButton.disabled = expired || !canToggleReminder(remindTime);
                    showToast(`操作失败：${message}`, 'error');
                }
                return;
            }

            const trigger = clickEvent.target.closest('[data-open-collection]');
            if (trigger?.dataset.openCollection) {
                closeModal();
                await openCollectionDetail(trigger.dataset.openCollection);
            }
        });
    } catch (err) {
        showToast(`加载详情失败：${err?.message || '未知错误'}`, 'error');
    }
}

function attachPageListeners() {
    const root = _container;
    if (!root) return;

    initCustomSelects(root, {
        'events-filter-category': async (value) => {
            const categories = new Set(_state.overview?.categories || []);
            _state.filters.category = value === '' || categories.has(value) ? value : '';
            await loadOverview({ preserveScroll: true });
        },
        'events-filter-kind': async (value) => {
            _state.filters.kind = EVENT_FILTER_KINDS.has(value) ? value : 'all';
            await loadOverview({ preserveScroll: true });
        },
        'events-filter-reminder': async (value) => {
            _state.filters.reminder = REMINDER_FILTERS.has(value) ? value : 'all';
            await loadOverview({ preserveScroll: true });
        },
    });

    root.querySelectorAll('[data-view-mode]').forEach((button) => {
        button.onclick = async () => {
            const nextMode = button.dataset.viewMode;
            if (_state.viewMode === nextMode) return;
            _state.viewMode = nextMode;
            await loadOverview({ preserveScroll: true });
        };
    });

    const addTop = root.querySelector('#events-add-top');
    if (addTop) addTop.onclick = () => openEventEditor(null, _state.selectedDate);

    const keywordInput = root.querySelector('#events-filter-keyword');
    if (keywordInput) {
        keywordInput.addEventListener('change', async () => {
            _state.filters.keyword = keywordInput.value.trim();
            await loadOverview({ preserveScroll: true });
        });
    }

    const prevMonth = root.querySelector('#events-prev-month');
    if (prevMonth) {
        prevMonth.onclick = async () => {
            _state.monthCursor = addMonths(_state.monthCursor, -1);
            _state.selectedDate = isoDate(_state.monthCursor);
            await loadOverview({ preserveScroll: true });
        };
    }
    const nextMonth = root.querySelector('#events-next-month');
    if (nextMonth) {
        nextMonth.onclick = async () => {
            _state.monthCursor = addMonths(_state.monthCursor, 1);
            _state.selectedDate = isoDate(_state.monthCursor);
            await loadOverview({ preserveScroll: true });
        };
    }

    const addDay = root.querySelector('#events-add-day');
    if (addDay) addDay.onclick = () => openEventEditor(null, addDay.dataset.date);

    root.onclick = async (event) => {
        const dayButton = event.target.closest('[data-select-day]');
        if (dayButton?.dataset.selectDay) {
            _state.selectedDate = dayButton.dataset.selectDay;
            renderPage();
            return;
        }

        const detailTrigger = event.target.closest('[data-open-detail], [data-event-id]');
        if (detailTrigger) {
            const eventId = detailTrigger.dataset.openDetail || detailTrigger.dataset.eventId;
            if (eventId) await openEventDetail(eventId);
            return;
        }

        const rangeButton = event.target.closest('[data-list-range]');
        if (rangeButton) {
            const nextRange = rangeButton.dataset.listRange;
            if (_state.listRange !== nextRange) {
                _state.listRange = nextRange;
                if (nextRange === 'custom' && !_state.customStart && !_state.customEnd) {
                    const range = currentRange();
                    _state.customStart = range.start;
                    _state.customEnd = range.end;
                }
                await loadOverview({ preserveScroll: true });
            }
        }
    };

    const customStart = root.querySelector('#events-custom-start');
    const customEnd = root.querySelector('#events-custom-end');
    const customApply = root.querySelector('#events-custom-apply');
    const applyCustomRange = async () => {
        const nextStart = customStart?.value.trim() || '';
        const nextEnd = customEnd?.value.trim() || '';
        if (!isValidDateInput(nextStart) || !isValidDateInput(nextEnd)) {
            showToast('请输入有效日期，格式为 YYYY-MM-DD', 'error');
            return;
        }
        if (nextStart > nextEnd) {
            showToast('开始日期不能晚于结束日期', 'error');
            return;
        }
        _state.customStart = nextStart;
        _state.customEnd = nextEnd;
        await loadOverview({ preserveScroll: true });
    };
    bindEnterAction(customStart, applyCustomRange);
    bindEnterAction(customEnd, applyCustomRange);
    if (customApply) customApply.onclick = applyCustomRange;
}

export function render(container) {
    if (!container || typeof container.querySelector !== 'function') {
        throw new TypeError('日程页需要有效的 DOM 挂载容器');
    }
    _unsubscribeDataChanges?.();
    _unsubscribeDataChanges = null;
    _container = container;
    const today = new Date();
    _state = {
        viewMode: 'calendar',
        monthCursor: firstDayOfMonth(today),
        selectedDate: isoDate(today),
        listRange: 'month',
        customStart: '',
        customEnd: '',
        allRangeStart: '',
        allRangeEnd: '',
        overview: null,
        loading: false,
        filters: {
            keyword: '',
            category: '',
            kind: 'all',
            reminder: 'all',
        },
    };
    renderPage();
    void loadOverview();

    _unsubscribeDataChanges = subscribeDataChanges('event', () => loadOverview({ preserveScroll: true }));
}

export function destroy() {
    _loadVersion += 1;
    _unsubscribeDataChanges?.();
    _unsubscribeDataChanges = null;
    _container = null;
}
