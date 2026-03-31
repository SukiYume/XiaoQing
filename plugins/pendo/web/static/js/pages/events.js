import { api } from '../api.js';
import { showToast } from '../components/toast.js';
import { showModal, closeModal, showConfirmModal } from '../components/modal.js';
import { renderCustomSelect, initCustomSelects } from '../components/custom_select.js';
import { derivePresetRange, fetchItemRangeBounds, RANGE_PRESET_OPTIONS, todayRangeKey } from '../utils/date_ranges.js';
import { isoDate as dateKey, isValidDateInput, pad2 as pad } from '../utils/format.js';
import { BREAKPOINTS, escapeHtml, injectStyles, mediaMax, pageShellCss } from '../utils/ui.js';

const CSS_ID = 'pendo-events-redesign-styles';
const WEEKDAYS = ['周一', '周二', '周三', '周四', '周五', '周六', '周日'];

let _container = null;
let _dataChangedHandler = null;
let _state = {
    viewMode: 'calendar',
    monthCursor: firstDayOfMonth(new Date()),
    selectedDate: dateKey(new Date()),
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
    return `${value.getFullYear()}年${pad(value.getMonth() + 1)}月`;
}

function formatDateLabel(day) {
    if (!day) return '';
    const date = new Date(day);
    return `${date.getMonth() + 1}月${date.getDate()}日`;
}

function formatWeekday(day) {
    if (!day) return '';
    const date = new Date(day);
    return WEEKDAYS[weekdayIndexMonday(date)];
}

function formatTime(iso) {
    if (!iso) return '全天';
    return iso.slice(11, 16);
}

function formatDateTime(iso) {
    if (!iso) return '';
    return `${iso.slice(0, 10)} ${formatTime(iso)}`;
}

function calendarVisibleItemLimit() {
    if (typeof window !== 'undefined') {
        if (window.matchMedia(`(max-width: ${BREAKPOINTS.PHONE})`).matches) return 1;
        if (window.matchMedia(`(max-width: ${BREAKPOINTS.MOBILE})`).matches) return 2;
    }
    return 3;
}

function toInputDateTime(iso) {
    return iso ? iso.slice(0, 16) : '';
}

function inputToIso(value) {
    if (!value) return '';
    return value.length === 16 ? `${value}:00` : value;
}

function currentRange() {
    if (_state.viewMode === 'calendar') {
        return {
            start: dateKey(firstDayOfMonth(_state.monthCursor)),
            end: dateKey(lastDayOfMonth(_state.monthCursor)),
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

async function fetchAllRangeBounds() {
    return fetchItemRangeBounds(api, {
        type: 'event',
        sortField: 'start_time',
        startField: 'start_time',
        endField: 'end_time',
        fallbackEnd: dateKey(new Date()),
        minimumEnd: dateKey(new Date()),
    });
}

function eventMap() {
    const map = new Map();
    for (const event of _state.overview?.events || []) {
        map.set(String(event.id), event);
    }
    return map;
}

function dayTimeline(date) {
    const timelineDays = _state.overview?.timeline_days || [];
    return timelineDays.find((row) => row.date === date)?.items || [];
}

function categoryOptions() {
    return _state.overview?.categories || [];
}

function restoreScrollPosition(scrollY) {
    window.requestAnimationFrame(() => {
        window.scrollTo({ top: scrollY });
        window.requestAnimationFrame(() => window.scrollTo({ top: scrollY }));
    });
}

function ensureStyles() {
    injectStyles(CSS_ID, `
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
            --pselect-border-strong: rgba(245, 158, 11, 0.35);
        }
        .events-inline-date { appearance: none; }
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
        .events-calendar-weekdays,
        .events-calendar-grid { display: grid; grid-template-columns: repeat(7, minmax(0, 1fr)); gap: 8px; }
        .events-calendar-weekdays { margin-bottom: 8px; }
        .events-weekday {
            text-align: center; font-size: 12px; font-weight: 700; color: var(--color-text-secondary); padding: 6px 0;
        }
        .events-calendar-cell {
            min-height: 128px; padding: 10px; border-radius: 18px; position: relative; cursor: pointer;
            border: 1px solid rgba(226,232,240,0.92); background: rgba(255,255,255,0.86); transition: transform 0.16s ease, border-color 0.16s ease, box-shadow 0.16s ease;
        }
        .events-calendar-cell:hover { transform: translateY(-1px); border-color: rgba(245,158,11,0.26); box-shadow: 0 12px 24px rgba(245,158,11,0.08); }
        .events-calendar-cell.outside { opacity: 0.28; cursor: default; background: rgba(248,250,252,0.7); }
        .events-calendar-cell.selected { border-color: rgba(245,158,11,0.38); box-shadow: inset 0 0 0 1px rgba(245,158,11,0.18); background: linear-gradient(180deg, rgba(255,251,235,0.96), rgba(255,255,255,0.92)); }
        .events-calendar-cell.today .events-calendar-date {
            background: var(--color-events); color: #fff; box-shadow: 0 8px 18px rgba(245,158,11,0.24);
        }
        .events-calendar-head { display: flex; align-items: center; justify-content: flex-start; gap: 8px; margin-bottom: 8px; min-height: 28px; padding-right: 34px; }
        .events-calendar-date {
            width: 28px; min-width: 28px; max-width: 28px; height: 28px; display: inline-flex; align-items: center; justify-content: center; flex: 0 0 28px; aspect-ratio: 1 / 1;
            border-radius: 999px; font-size: 13px; font-weight: 700; color: var(--color-text);
        }
        .events-calendar-items { display: flex; flex-direction: column; gap: 3px; margin-top: auto; min-height: 0; overflow: hidden; }
        .events-calendar-chip {
            display: flex; width: 100%; text-align: left; border: none; cursor: pointer; align-items: center; gap: 6px;
            min-height: 20px; padding: 2px 4px 2px 0; border-radius: 0; background: transparent;
            color: var(--color-text); font-size: 11px; font-weight: 600; line-height: 1.25;
            overflow: hidden;
        }
        .events-calendar-chip::before {
            content: ''; flex: 0 0 auto; width: 6px; height: 6px; border-radius: 999px; background: rgba(245,158,11,0.9);
        }
        .events-calendar-chip.milestone::before { background: #6366F1; }
        .events-calendar-chip.recurring::before { background: #10B981; }
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
        .events-day-empty {
            padding: 26px 18px; border-radius: 18px; text-align: center; background: rgba(248,250,252,0.8);
            border: 1px dashed rgba(148,163,184,0.26); color: var(--color-text-secondary);
        }
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
            align-items: start; padding: 4px 0; cursor: pointer;
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
        .events-timeline-dot.milestone { background: #6366F1; box-shadow: 0 0 0 4px rgba(99,102,241,0.12); }
        .events-timeline-dot.recurring { background: #10B981; box-shadow: 0 0 0 4px rgba(16,185,129,0.12); }
        .events-timeline-card {
            padding: 10px 12px; border-radius: 16px; border: 1px solid rgba(226,232,240,0.92);
            background: linear-gradient(180deg, rgba(255,255,255,0.98), rgba(248,250,252,0.94));
        }
        .events-timeline-top { display: flex; align-items: center; justify-content: space-between; gap: 10px; }
        .events-timeline-title { font-size: 15px; font-weight: 700; color: var(--color-text); line-height: 1.35; }
        .events-timeline-subtitle { margin-top: 4px; font-size: 12px; color: var(--color-text-secondary); }
        .events-timeline-meta { display: flex; flex-wrap: wrap; gap: 6px; margin-top: 8px; }
        .events-pill {
            display: inline-flex; align-items: center; gap: 4px; padding: 5px 8px; border-radius: 999px;
            font-size: 11px; font-weight: 700; background: rgba(148,163,184,0.08); color: var(--color-text-secondary);
        }
        .events-pill.kind-milestone { background: rgba(99,102,241,0.12); color: #4338CA; }
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
        .events-detail-block { padding: 16px 18px; border-radius: 18px; background: rgba(248,250,252,0.72); border: 1px solid rgba(226,232,240,0.92); }
        .events-detail-block h4 { margin: 0 0 12px; font-size: 14px; font-weight: 800; color: var(--color-text); }
        .events-detail-list { display: flex; flex-direction: column; gap: 10px; }
        .events-detail-row {
            display: flex; align-items: center; justify-content: space-between; gap: 12px; padding: 10px 0;
            border-bottom: 1px solid rgba(226,232,240,0.9);
        }
        .events-detail-row:last-child { border-bottom: none; padding-bottom: 0; }
        .events-detail-row:first-child { padding-top: 0; }
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
        .events-editor-row { display: grid; grid-template-columns: minmax(0, 1fr) minmax(170px, 0.8fr) auto; gap: 8px; align-items: center; }
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
        ${mediaMax(BREAKPOINTS.XL, `
            .events-grid { grid-template-columns: 1fr; }
            .events-filters { grid-template-columns: repeat(2, minmax(0, 1fr)); }
        `)}
        ${mediaMax(BREAKPOINTS.MOBILE, `
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
            .events-calendar-grid, .events-calendar-weekdays { gap: 6px; }
            .events-calendar-cell {
                min-height: 88px; aspect-ratio: auto; padding: 6px; border-radius: 16px;
                display: flex; flex-direction: column; justify-content: flex-start;
            }
            .events-calendar-head { margin-bottom: 2px; align-items: flex-start; position: relative; min-height: 24px; padding-right: 0; }
            .events-calendar-date { width: 24px; min-width: 24px; max-width: 24px; height: 24px; flex: 0 0 24px; font-size: 12px; }
            .events-calendar-items { gap: 2px; margin-top: 0; }
            .events-calendar-chip { min-height: 18px; padding: 1px 2px 1px 0; font-size: 10px; line-height: 1.1; }
            .events-calendar-overflow { font-size: 10px; padding-left: 12px; }
            .events-calendar-overflow-suffix { display: none; }
            .events-weekday { font-size: 11px; padding: 2px 0 4px; }
            .events-timeline-day-marker { grid-template-columns: 52px 14px minmax(0, 1fr); padding-top: 8px; }
            .events-timeline-item > :last-child { display: none; }
            .events-timeline-day-label { font-size: 12px; }
            .events-timeline-item { grid-template-columns: 52px 14px minmax(0, 1fr); gap: 8px; }
            .events-timeline-time { padding-top: 8px; font-size: 11px; }
            .events-timeline-card { padding: 9px 10px; }
        `)}
        ${mediaMax(BREAKPOINTS.PHONE, `
            .events-summary-chips { display: grid; grid-template-columns: repeat(3, minmax(0, 1fr)); }
            .events-summary-chips .events-summary-chip { width: 100%; justify-content: center; padding: 0 8px; font-size: 10px; }
            .events-weekday { font-size: 10px; letter-spacing: 0; }
            .events-calendar-grid, .events-calendar-weekdays { gap: 4px; }
            .events-calendar-cell { min-height: 72px; padding: 5px; border-radius: 14px; }
            .events-calendar-date { width: 22px; min-width: 22px; max-width: 22px; height: 22px; flex: 0 0 22px; font-size: 11px; }
            .events-calendar-chip { min-height: 16px; font-size: 9.5px; }
            .events-calendar-overflow { font-size: 9px; }
        `)}
    `);
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
                <div class="events-view-toggle">
                    <button class="events-toggle-btn ${_state.viewMode === 'calendar' ? 'active' : ''}" data-view-mode="calendar">月历</button>
                    <button class="events-toggle-btn ${_state.viewMode === 'timeline' ? 'active' : ''}" data-view-mode="timeline">时间线</button>
                </div>
                ${summary.event_count ? `<span class="events-summary-chip">${summary.event_count} 条日程</span>` : ''}
                <button class="btn btn-primary" id="events-add-top">＋ 新建日程</button>
            </div>
        </section>`;
}

function renderFilters() {
    const filters = _state.filters;
    const categoryOptionsList = [{ value: '', label: '全部分类' }, ...categoryOptions().map((category) => ({ value: category, label: category }))];
    const kindOptions = [
        { value: 'all', label: '全部事件' },
        { value: 'single', label: '单次事件' },
        { value: 'milestone', label: '多节点事件' },
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
                <label>搜索</label>
                <input id="events-filter-keyword" type="search" placeholder="标题、地点、备注、节点名称" value="${escapeHtml(filters.keyword)}">
            </div>
            <div class="events-filter-field">
                <label>分类</label>
                ${renderCustomSelect({
                    id: 'events-filter-category',
                    options: categoryOptionsList,
                    selected: filters.category,
                    className: 'pselect-block pselect-theme-events',
                    placeholder: '全部分类',
                })}
            </div>
            <div class="events-filter-field">
                <label>事件类型</label>
                ${renderCustomSelect({
                    id: 'events-filter-kind',
                    options: kindOptions,
                    selected: filters.kind,
                    className: 'pselect-block pselect-theme-events',
                    placeholder: '全部事件',
                })}
            </div>
            <div class="events-filter-field">
                <label>提醒状态</label>
                ${renderCustomSelect({
                    id: 'events-filter-reminder',
                    options: reminderOptions,
                    selected: filters.reminder,
                    className: 'pselect-block pselect-theme-events',
                    placeholder: '全部提醒',
                })}
            </div>
        </section>`;
}

function renderCalendarCell(day, month, calendarDay) {
    const currentMonth = month.getMonth();
    const today = dateKey(new Date());
    const dayString = dateKey(day);
    const outside = day.getMonth() !== currentMonth;
    const selected = _state.selectedDate === dayString;
    const classes = ['events-calendar-cell'];
    if (outside) classes.push('outside');
    if (selected) classes.push('selected');
    if (today === dayString) classes.push('today');

    const items = calendarDay?.items || [];
    const count = calendarDay?.count || 0;
    const hasMilestone = items.some((item) => item.kind === 'milestone');
    const hasRecurring = items.some((item) => item.kind === 'recurring');
    const visibleLimit = calendarVisibleItemLimit();
    const visibleItems = items.slice(0, visibleLimit);
    return `
        <div class="${classes.join(' ')}" data-day="${dayString}">
            <div class="events-calendar-head">
                <span class="events-calendar-date">${day.getDate()}</span>
            </div>
            <div class="events-calendar-items">
                ${visibleItems.map((item) => `<button class="events-calendar-chip ${item.kind}" data-event-id="${item.event_id}" title="${escapeHtml(item.label)}"><span class="events-calendar-chip-text">${escapeHtml(item.label)}</span></button>`).join('')}
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

    for (let index = 0; index < totalCells; index += 1) {
        const dayNumber = index - offset + 1;
        const cellDate = new Date(_state.monthCursor.getFullYear(), _state.monthCursor.getMonth(), dayNumber);
        const key = dateKey(cellDate);
        cells.push(renderCalendarCell(cellDate, _state.monthCursor, calendarDays[key]));
    }

    return `
        <section class="events-panel">
            <div class="events-panel-header">
                <div>
                    <h3 class="events-panel-title">${formatMonthLabel(_state.monthCursor)}</h3>
                    <p class="events-panel-subtitle">按月查看每天的日程分布。</p>
                </div>
                <div class="events-month-nav">
                    <button class="events-month-btn" id="events-prev-month">‹</button>
                    <button class="events-month-btn" id="events-next-month">›</button>
                </div>
            </div>
            <div class="events-panel-body">
                <div class="events-summary-chips">
                    <span class="events-summary-chip">${overview?.summary?.event_count || 0} 条日程</span>
                    <span class="events-summary-chip">${overview?.summary?.milestone_count || 0} 条多节点</span>
                    <span class="events-summary-chip">${overview?.summary?.reminder_count || 0} 个提醒</span>
                </div>
                <div style="margin-top:16px;">
                    <div class="events-calendar-weekdays">
                        ${WEEKDAYS.map((weekday) => `<div class="events-weekday">${weekday}</div>`).join('')}
                    </div>
                    <div class="events-calendar-grid">${cells.join('')}</div>
                </div>
            </div>
        </section>`;
}

function kindPill(kind) {
    if (kind === 'milestone') return '<span class="events-pill kind-milestone">多节点</span>';
    if (kind === 'recurring') return '<span class="events-pill kind-recurring">重复实例</span>';
    return '<span class="events-pill kind-single">单次事件</span>';
}

function renderTimelineEntries(items) {
    if (!items.length) {
        return '';
    }

    const events = eventMap();
    return items.map((item) => {
        const event = events.get(String(item.event_id));
        const dotKind = item.kind.includes('milestone') ? 'milestone' : (item.kind === 'recurring' ? 'recurring' : '');
        return `
                <article class="events-timeline-item" data-event-id="${item.event_id}">
                    <div class="events-timeline-time">${escapeHtml(item.time_label || '全天')}</div>
                    <div class="events-timeline-track"><span class="events-timeline-dot ${dotKind}"></span></div>
                    <div class="events-timeline-card">
                        <div class="events-timeline-top">
                            <div class="events-timeline-title">${escapeHtml(item.title || '(无标题)')}</div>
                        </div>
                        <div class="events-timeline-subtitle">${escapeHtml(item.subtitle || event?.time_summary || '')}</div>
                        <div class="events-timeline-meta">
                            ${kindPill(event?.kind || 'single')}
                            ${event?.category ? `<span class="events-pill">${escapeHtml(event.category)}</span>` : ''}
                            ${item.location ? `<span class="events-pill">📍 ${escapeHtml(item.location)}</span>` : ''}
                            ${item.reminder_total ? `<span class="events-pill">🔔 ${item.reminder_total} 个提醒</span>` : ''}
                        </div>
                    </div>
                    <button class="btn btn-secondary btn-sm" data-open-detail="${item.event_id}">详情</button>
                </article>`;
    }).join('');
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
    const items = dayTimeline(selectedDate);
    return `
        <section class="events-day-shell">
            <div class="events-day-head">
                <div>
                    <h3 class="events-day-title">${formatDateLabel(selectedDate)} · ${formatWeekday(selectedDate)}</h3>
                    <div class="events-day-subtitle">选中日期后查看当天日程、提醒和详情。</div>
                </div>
                <button class="btn btn-primary btn-sm" id="events-add-day" data-date="${selectedDate}">＋ 添加当天日程</button>
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
                        <div class="events-list-range">
                            ${RANGE_PRESET_OPTIONS.map((option) => `<button class="events-range-btn ${_state.listRange === option.key ? 'active' : ''}" data-list-range="${option.key}">${option.label}</button>`).join('')}
                        </div>
                    </div>
                    ${_state.listRange === 'custom' ? `
                        <div class="events-inline-dates">
                            <input class="events-inline-date" id="events-custom-start" type="text" inputmode="numeric" placeholder="YYYY-MM-DD" value="${escapeHtml(_state.customStart)}">
                            <span style="font-size:12px;color:var(--color-text-secondary);">至</span>
                            <input class="events-inline-date" id="events-custom-end" type="text" inputmode="numeric" placeholder="YYYY-MM-DD" value="${escapeHtml(_state.customEnd)}">
                            <button type="button" class="events-range-btn" id="events-custom-apply">应用</button>
                        </div>` : ''}
                </div>
            </div>
            <div class="events-panel-body">
                ${timelineDays.length ? `
                    <div class="events-timeline events-timeline-continuous">
                        ${timelineDays.map((row) => `
                            <div class="events-timeline-day-marker">
                                <div class="events-timeline-day-label">${formatDateLabel(row.date)}</div>
                                <div></div>
                                <div class="events-timeline-day-meta">${formatWeekday(row.date)} · ${row.items.length} 条</div>
                            </div>
                            ${renderTimelineEntries(row.items)}
                        `).join('')}
                    </div>
                ` : `
                    <div class="events-empty events-timeline-empty">
                        <strong>当前筛选下没有日程</strong>
                        <p>可以放宽筛选条件，或者直接新建一条新的日程。</p>
                    </div>
                `}
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
            ${_state.loading || !_state.overview ? renderLoading() : (_state.viewMode === 'calendar' ? renderCalendarPage() : renderTimelinePage())}
        </div>
    `;
    attachPageListeners();
}

async function loadOverview(options = {}) {
    if (!_container) return;
    const preserveScroll = Boolean(options.preserveScroll);
    const scrollY = preserveScroll ? window.scrollY : 0;
    const shouldShowLoading = !_state.overview && !preserveScroll;

    _state.loading = shouldShowLoading;
    if (shouldShowLoading) renderPage();

    try {
        if (_state.viewMode !== 'calendar' && _state.listRange === 'all' && (!_state.allRangeStart || !_state.allRangeEnd)) {
            const bounds = await fetchAllRangeBounds();
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
        _state.overview = res.data || {};

        const availableDays = (_state.overview.timeline_days || []).map((row) => row.date);
        if (_state.viewMode === 'calendar') {
            if (!_state.selectedDate || !_state.overview.calendar_days?.[_state.selectedDate]) {
                _state.selectedDate = availableDays[0] || range.start;
            }
        } else if (_state.listRange === 'custom' && !_state.customStart && !_state.customEnd) {
            _state.customStart = range.start;
            _state.customEnd = range.end;
        }
    } catch (err) {
        _state.overview = {
            summary: {},
            categories: [],
            calendar_days: {},
            timeline_days: [],
            events: [],
        };
        showToast('加载日程失败：' + err.message, 'error');
    } finally {
        _state.loading = false;
        renderPage();
        if (preserveScroll) restoreScrollPosition(scrollY);
    }
}

function reminderRowHTML(value = '') {
    return `
        <div class="events-editor-row" data-reminder-row>
            <input type="datetime-local" class="events-editor-reminder-input" value="${escapeHtml(value)}">
            <div></div>
            <button type="button" data-remove-row>×</button>
        </div>`;
}

function milestoneRowHTML(name = '', time = '') {
    return `
        <div class="events-editor-row" data-milestone-row>
            <input type="text" class="events-editor-milestone-name" placeholder="节点名称" value="${escapeHtml(name)}">
            <input type="datetime-local" class="events-editor-milestone-time" value="${escapeHtml(time)}">
            <button type="button" data-remove-row>×</button>
        </div>`;
}

function editorModalHTML(existing = null, prefillDate = '') {
    const isMilestone = !!(existing?.milestones?.length >= 2);
    const startValue = existing?.start_time ? toInputDateTime(existing.start_time) : (prefillDate ? `${prefillDate}T09:00` : '');
    const endValue = existing?.end_time ? toInputDateTime(existing.end_time) : '';
    const reminders = (existing?.reminders || []).map((row) => toInputDateTime(row.time || '')).filter(Boolean);
    const milestones = existing?.milestones || [];

    return `
        <form id="events-editor-form" data-mode="${isMilestone ? 'milestone' : 'single'}">
            <div class="events-editor-grid">
                <div class="events-editor-field full">
                    <label>标题</label>
                    <input name="title" type="text" value="${escapeHtml(existing?.title || '')}" placeholder="比如：产品评审、提审节点、出发前准备">
                </div>
                <div class="events-editor-field">
                    <label>分类</label>
                    <input name="category" type="text" value="${escapeHtml(existing?.category || '')}" placeholder="会议、项目、个人等">
                </div>
                <div class="events-editor-field">
                    <label>地点</label>
                    <input name="location" type="text" value="${escapeHtml(existing?.location || '')}" placeholder="可选">
                </div>
                <div class="events-editor-field full">
                    <label>事件模式</label>
                    <div class="events-editor-mode" id="events-editor-mode">
                        <button type="button" class="${isMilestone ? '' : 'active'}" data-editor-mode="single">单次事件</button>
                        <button type="button" class="${isMilestone ? 'active' : ''}" data-editor-mode="milestone">多节点事件</button>
                    </div>
                </div>
                <div class="events-editor-field ${isMilestone ? 'full' : ''}" data-single-section ${isMilestone ? 'style="display:none;"' : ''}>
                    <label>开始时间</label>
                    <input name="start_time" type="datetime-local" value="${escapeHtml(startValue)}">
                </div>
                <div class="events-editor-field ${isMilestone ? 'full' : ''}" data-single-section ${isMilestone ? 'style="display:none;"' : ''}>
                    <label>结束时间</label>
                    <input name="end_time" type="datetime-local" value="${escapeHtml(endValue)}">
                </div>
                <div class="events-editor-field full" data-milestone-section ${isMilestone ? '' : 'style="display:none;"'}>
                    <label>时间节点</label>
                    <div class="events-editor-note">多节点事件会按节点时间生成当天时间线；保存时会自动用首尾节点推导整体起止时间。</div>
                    <div class="events-editor-rows" id="events-editor-milestones">
                        ${(milestones.length ? milestones : [{ name: '', time: prefillDate ? `${prefillDate}T09:00:00` : '' }, { name: '', time: prefillDate ? `${prefillDate}T18:00:00` : '' }])
                            .map((row) => milestoneRowHTML(row.name || '', toInputDateTime(row.time || ''))).join('')}
                    </div>
                    <button type="button" class="events-editor-add" id="events-add-milestone">＋ 添加节点</button>
                </div>
                <div class="events-editor-field full">
                    <label>提醒</label>
                    <div class="events-editor-rows" id="events-editor-reminders">
                        ${(reminders.length ? reminders : [startValue]).filter(Boolean).map((value) => reminderRowHTML(value)).join('')}
                    </div>
                    <button type="button" class="events-editor-add" id="events-add-reminder">＋ 添加提醒时间</button>
                </div>
                <div class="events-editor-field full">
                    <label>备注</label>
                    <textarea name="notes" placeholder="可选，用来记录背景、材料链接或执行说明">${escapeHtml(existing?.notes || '')}</textarea>
                </div>
                ${existing?.series_id ? `
                    <div class="events-editor-field full">
                        <div class="events-editor-note">当前是重复日程实例。Web 端编辑和删除只作用于当前这一条，不会批量改整个系列。</div>
                    </div>` : ''}
            </div>
        </form>`;
}

function setEditorMode(content, mode) {
    const form = content.querySelector('#events-editor-form');
    if (!form) return;
    form.dataset.mode = mode;
    content.querySelectorAll('[data-editor-mode]').forEach((button) => {
        button.classList.toggle('active', button.dataset.editorMode === mode);
    });
    content.querySelectorAll('[data-single-section]').forEach((section) => {
        section.style.display = mode === 'single' ? '' : 'none';
    });
    content.querySelectorAll('[data-milestone-section]').forEach((section) => {
        section.style.display = mode === 'milestone' ? '' : 'none';
    });
}

function collectEditorPayload(content) {
    const form = content.querySelector('#events-editor-form');
    const mode = form?.dataset.mode || 'single';
    const title = form.querySelector('[name="title"]').value.trim();
    const category = form.querySelector('[name="category"]').value.trim() || '未分类';
    const location = form.querySelector('[name="location"]').value.trim();
    const notes = form.querySelector('[name="notes"]').value.trim();
    const remindTimes = [...form.querySelectorAll('.events-editor-reminder-input')]
        .map((input) => inputToIso(input.value))
        .filter(Boolean)
        .sort()
        .filter((value, index, list) => list.indexOf(value) === index);

    if (!title) throw new Error('请填写标题');

    const payload = { title, category, location, notes, remind_times: remindTimes };

    if (mode === 'milestone') {
        const milestones = [...form.querySelectorAll('[data-milestone-row]')]
            .map((row) => {
                const name = row.querySelector('.events-editor-milestone-name').value.trim();
                const time = inputToIso(row.querySelector('.events-editor-milestone-time').value);
                return name && time ? { name, time } : null;
            })
            .filter(Boolean)
            .sort((a, b) => a.time.localeCompare(b.time));

        if (milestones.length < 2) throw new Error('多节点事件至少需要 2 个有效节点');
        payload.milestones = milestones;
        payload.start_time = milestones[0].time;
        payload.end_time = milestones[milestones.length - 1].time;
    } else {
        const startTime = inputToIso(form.querySelector('[name="start_time"]').value);
        const endTime = inputToIso(form.querySelector('[name="end_time"]').value);
        if (!startTime) throw new Error('请填写开始时间');
        payload.start_time = startTime;
        payload.end_time = endTime || null;
        payload.milestones = [];
    }

    return payload;
}

function openEventEditor(existing = null, prefillDate = '') {
    ensureStyles();
    const title = existing ? '编辑日程' : '新建日程';
    const content = showModal(title, editorModalHTML(existing, prefillDate), {
        footer: `
            <button class="btn btn-secondary" id="events-editor-cancel">取消</button>
            <button class="btn btn-primary" id="events-editor-save">保存</button>
        `,
    });

    content.querySelector('#events-editor-cancel').onclick = closeModal;
    content.querySelector('#events-add-reminder').onclick = () => {
        content.querySelector('#events-editor-reminders').insertAdjacentHTML('beforeend', reminderRowHTML(''));
    };
    content.querySelector('#events-add-milestone').onclick = () => {
        content.querySelector('#events-editor-milestones').insertAdjacentHTML('beforeend', milestoneRowHTML('', ''));
    };

    content.addEventListener('click', (event) => {
        const modeButton = event.target.closest('[data-editor-mode]');
        if (modeButton) {
            setEditorMode(content, modeButton.dataset.editorMode);
            return;
        }
        const removeButton = event.target.closest('[data-remove-row]');
        if (removeButton) {
            const row = removeButton.closest('[data-reminder-row], [data-milestone-row]');
            if (row) row.remove();
        }
    });

    content.querySelector('#events-editor-save').onclick = async () => {
        try {
            const payload = collectEditorPayload(content);
            if (existing) {
                await api.put(`/items/${existing.id}`, payload);
                showToast('日程已更新', 'success');
            } else {
                await api.post('/items', { type: 'event', ...payload });
                showToast('日程已创建', 'success');
            }
            closeModal();
            window.dispatchEvent(new CustomEvent('pendo-data-changed'));
            await loadOverview();
        } catch (err) {
            showToast(err.message || '保存失败', 'error');
        }
    };
}

async function deleteEvent(eventId, title = '这条日程') {
    const confirmed = await showConfirmModal({
        title: '删除日程',
        message: `确定要删除“${title}”吗？删除后当前页面和时间线会立即移除它。`,
        confirmText: '删除',
        cancelText: '取消',
        tone: 'danger',
    });
    if (!confirmed) return;
    await api.delete(`/items/${eventId}`);
    showToast('日程已删除', 'success');
    window.dispatchEvent(new CustomEvent('pendo-data-changed'));
    await loadOverview();
}

function renderDetailBody(detail) {
    const event = detail.event;
    return `
        <div class="events-detail-shell">
            <section class="events-detail-summary">
                <div style="display:flex;flex-wrap:wrap;gap:8px;margin-bottom:12px;">
                    ${kindPill(event.kind)}
                    ${event.category ? `<span class="events-pill">${escapeHtml(event.category)}</span>` : ''}
                    ${event.location ? `<span class="events-pill">📍 ${escapeHtml(event.location)}</span>` : ''}
                </div>
                <h3 class="events-detail-title">${escapeHtml(event.title || '(无标题)')}</h3>
                <div class="events-detail-time">${escapeHtml(formatDateTime(event.start_time))}${event.end_time ? ` - ${escapeHtml(formatDateTime(event.end_time))}` : ''}</div>
                ${event.notes ? `<div style="margin-top:12px;font-size:13px;line-height:1.8;color:var(--color-text-secondary);">${escapeHtml(event.notes)}</div>` : ''}
            </section>
            ${event.milestones?.length ? `
                <section class="events-detail-block">
                    <h4>时间节点</h4>
                    <div class="events-detail-list">
                        ${event.milestones.map((milestone) => `
                            <div class="events-detail-row">
                                <div>
                                    <div style="font-weight:700;color:var(--color-text);">${escapeHtml(milestone.name)}</div>
                                    <div style="margin-top:4px;font-size:12px;color:var(--color-text-secondary);">${escapeHtml(formatDateTime(milestone.time))}</div>
                                </div>
                            </div>
                        `).join('')}
                    </div>
                </section>` : ''}
            <section class="events-detail-block">
                <h4>提醒</h4>
                ${event.reminders?.length ? `
                    <div class="events-detail-list">
                        ${event.reminders.map((row) => `
                            <div class="events-detail-row">
                                <div>
                                    <div style="font-weight:700;color:var(--color-text);">${escapeHtml(formatDateTime(row.time))}</div>
                                    <div style="margin-top:4px;font-size:12px;color:var(--color-text-secondary);">${row.repeat_count ? `已重复 ${row.repeat_count} 次` : '等待发送或确认'}</div>
                                </div>
                                <span class="events-status ${row.status}">${row.status === 'confirmed' ? '已确认' : row.status === 'sent' ? '已发送' : '待发送'}</span>
                            </div>
                        `).join('')}
                    </div>` : `<div style="font-size:13px;color:var(--color-text-secondary);">当前没有提醒。</div>`}
            </section>
            ${detail.related_instances?.length ? `
                <section class="events-detail-block">
                    <h4>同系列后续实例</h4>
                    <div class="events-detail-list">
                        ${detail.related_instances.map((item) => `
                            <div class="events-detail-row">
                                <div>
                                    <div style="font-weight:700;color:var(--color-text);">${escapeHtml(item.title || '(无标题)')}</div>
                                    <div style="margin-top:4px;font-size:12px;color:var(--color-text-secondary);">${escapeHtml(formatDateTime(item.start_time))}</div>
                                </div>
                            </div>
                        `).join('')}
                    </div>
                </section>` : ''}
        </div>`;
}

export async function openEventDetail(eventId) {
    ensureStyles();
    try {
        const res = await api.get(`/events/${eventId}/detail`);
        const detail = res.data;
        const event = detail.event;
        const content = showModal('日程详情', renderDetailBody(detail), {
            footer: `
                <button class="btn btn-secondary" id="events-detail-close">关闭</button>
                <button class="btn btn-primary" id="events-detail-edit">编辑</button>
                <button class="btn btn-danger" id="events-detail-delete">删除</button>
            `,
        });
        content.querySelector('#events-detail-close').onclick = closeModal;
        content.querySelector('#events-detail-edit').onclick = () => {
            closeModal();
            openEventEditor(event, _state.selectedDate);
        };
        content.querySelector('#events-detail-delete').onclick = async () => {
            closeModal();
            try {
                await deleteEvent(event.id, event.title || '这条日程');
            } catch (err) {
                showToast('删除失败：' + err.message, 'error');
            }
        };
    } catch (err) {
        showToast('加载详情失败：' + err.message, 'error');
    }
}

function attachPageListeners() {
    const root = _container;
    if (!root) return;

    initCustomSelects(root, {
        'events-filter-category': async (value) => {
            _state.filters.category = value;
            await loadOverview({ preserveScroll: true });
        },
        'events-filter-kind': async (value) => {
            _state.filters.kind = value;
            await loadOverview({ preserveScroll: true });
        },
        'events-filter-reminder': async (value) => {
            _state.filters.reminder = value;
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
            _state.selectedDate = dateKey(_state.monthCursor);
            await loadOverview({ preserveScroll: true });
        };
    }
    const nextMonth = root.querySelector('#events-next-month');
    if (nextMonth) {
        nextMonth.onclick = async () => {
            _state.monthCursor = addMonths(_state.monthCursor, 1);
            _state.selectedDate = dateKey(_state.monthCursor);
            await loadOverview({ preserveScroll: true });
        };
    }

    const addDay = root.querySelector('#events-add-day');
    if (addDay) addDay.onclick = () => openEventEditor(null, addDay.dataset.date);

    root.onclick = async (event) => {
        const cell = event.target.closest('.events-calendar-cell[data-day]');
        if (cell && !event.target.closest('[data-event-id]')) {
            _state.selectedDate = cell.dataset.day;
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
    if (customStart) {
        customStart.onkeydown = async (event) => {
            if (event.key === 'Enter') {
                event.preventDefault();
                await applyCustomRange();
            }
        };
    }
    if (customEnd) {
        customEnd.onkeydown = async (event) => {
            if (event.key === 'Enter') {
                event.preventDefault();
                await applyCustomRange();
            }
        };
    }
    if (customApply) customApply.onclick = applyCustomRange;
}

export function render(container) {
    _container = container;
    const today = new Date();
    _state = {
        ..._state,
        viewMode: 'calendar',
        monthCursor: firstDayOfMonth(today),
        selectedDate: dateKey(today),
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
    loadOverview();

    _dataChangedHandler = async (event) => {
        const changedType = event?.detail?.type;
        if (changedType && changedType !== 'event') return;
        await loadOverview();
    };
    window.addEventListener('pendo-data-changed', _dataChangedHandler);
}

export function destroy() {
    if (_dataChangedHandler) {
        window.removeEventListener('pendo-data-changed', _dataChangedHandler);
        _dataChangedHandler = null;
    }
    _container = null;
}

export function onRouteEnter(_params) {}
