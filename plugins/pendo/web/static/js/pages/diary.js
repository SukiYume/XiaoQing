import { api } from '../api.js';
import { showToast } from '../components/toast.js';
import { showModal, closeModal, showConfirmModal, safeHtml } from '../components/modal.js';
import { buildFormHTML, getFormData, initFormInteractions } from '../components/form.js';
import { renderCustomSelect, initCustomSelects } from '../components/custom_select.js';
import {
    errorMessage,
    finiteNumber,
    isRecord,
    isoDate,
    nonNegativeInteger,
    pad2,
    parseDate,
    previewText,
    records,
    todayStr,
} from '../utils/format.js';
import { fetchUserTimeZone, zonedDateTimeToInput, zonedInputToUtcIso } from '../utils/timezone.js';
import { BREAKPOINTS, escapeHtml, injectStyles, mediaMax, pageShellCss, subscribeDataChanges } from '../utils/ui.js';

const CSS_ID = 'pendo-diary-redesign-styles';
const WEATHER_OPTIONS = Object.freeze(['☀️ 晴', '⛅ 多云', '🌧️ 雨', '❄️ 雪', '🌫️ 雾', '💨 风']);
const MOOD_SWATCHES = Object.freeze(['#EC4899', '#8B5CF6', '#3B82F6', '#F59E0B', '#10B981', '#F97316']);
const DIARY_YEAR_START = 2000;
const DIARY_YEAR_COUNT = 100;
const DIARY_YEAR_END = DIARY_YEAR_START + DIARY_YEAR_COUNT - 1;
const WEEK_NAMES = Object.freeze(['周日', '周一', '周二', '周三', '周四', '周五', '周六']);
const CALENDAR_WEEKDAYS = Object.freeze(['周一', '周二', '周三', '周四', '周五', '周六', '周日']);
const DIARY_NAV_PREV_ICON = `
    <svg viewBox="0 0 16 16" aria-hidden="true" focusable="false">
        <path d="M9.75 3.5 5.5 8l4.25 4.5" fill="none" stroke="currentColor" stroke-width="1.7" stroke-linecap="round" stroke-linejoin="round"/>
    </svg>`;
const DIARY_NAV_NEXT_ICON = `
    <svg viewBox="0 0 16 16" aria-hidden="true" focusable="false">
        <path d="M6.25 3.5 10.5 8l-4.25 4.5" fill="none" stroke="currentColor" stroke-width="1.7" stroke-linecap="round" stroke-linejoin="round"/>
    </svg>`;
const DEFAULT_MOOD_EMOJIS = {
    happy: '😊',
    sad: '😢',
    calm: '😌',
    excited: '🤩',
    angry: '😠',
    tired: '😴',
    anxious: '😰',
    grateful: '🙏',
    neutral: '😐',
};
const DEFAULT_MOOD_LABELS = {
    happy: '开心',
    calm: '平静',
    excited: '兴奋',
    sad: '难过',
    angry: '生气',
    tired: '疲惫',
    anxious: '焦虑',
    grateful: '感恩',
    neutral: '普通',
};
const DEFAULT_MOOD_OPTIONS = Object.keys(DEFAULT_MOOD_LABELS).map((id) => ({
    value: id,
    label: DEFAULT_MOOD_LABELS[id],
    emoji: DEFAULT_MOOD_EMOJIS[id],
}));

const DIARY_FIELDS = [
    { name: 'diary_date', label: '日期', type: 'date', required: true },
    { name: 'mood', label: '心情', type: 'mood' },
    { name: 'mood_score', label: '心情分数', type: 'number', min: 1, max: 10, step: 1, placeholder: '1-10' },
    { name: 'entry_time', label: '记录时间', type: 'datetime' },
    {
        name: 'weather',
        label: '天气',
        type: 'select',
        options: [{ value: '', label: '未设置' }, ...WEATHER_OPTIONS],
        selectThemeClass: 'pselect-theme-diary',
    },
    { name: 'location', label: '地点', type: 'text' },
    { name: 'title', label: '标题', type: 'text', placeholder: '可选标题' },
    { name: 'is_favorite', label: '标记', type: 'checkbox', checkboxLabel: '收藏这篇日记' },
    { name: 'content', label: '日记内容', type: 'textarea', rows: 10, required: true },
];

let _container = null;
let _items = [];
let _overview = null;
let _year = 0;
let _month = 0;
let _selectedDate = '';
let _loading = false;
let _templates = [];
let _templatesLoaded = false;
let _unsubscribeDataChanges = null;
let _moodEmojis = { ...DEFAULT_MOOD_EMOJIS };
let _moodLabels = { ...DEFAULT_MOOD_LABELS };
let _moodOptions = [...DEFAULT_MOOD_OPTIONS];
let _loadVersion = 0;
let _lifecycleVersion = 0;
let _formOpening = false;

// ---------- 接口边界、日期与展示格式 ----------

function monthStart(year, month) {
    return `${year}-${pad2(month)}-01`;
}

function monthEnd(year, month) {
    const lastDay = new Date(year, month, 0).getDate();
    return `${year}-${pad2(month)}-${pad2(lastDay)}`;
}

function monthInputValue(year, month) {
    return `${year}-${pad2(month)}`;
}

function monthLabel(year, month) {
    return `${year}年${month}月`;
}

async function updateMonth(year, month, selectedDate = '') {
    if (
        !Number.isInteger(year) ||
        year < DIARY_YEAR_START ||
        year > DIARY_YEAR_END ||
        !Number.isInteger(month) ||
        month < 1 ||
        month > 12
    ) {
        return false;
    }

    const parsedSelectedDate = parseDate(selectedDate);
    const selectedDateKey = parsedSelectedDate ? isoDate(parsedSelectedDate) : '';
    const targetPrefix = `${year}-${pad2(month)}`;
    const nextSelectedDate = selectedDateKey.startsWith(targetPrefix) ? selectedDateKey : '';
    if (_year === year && _month === month) {
        if (nextSelectedDate && nextSelectedDate !== _selectedDate) {
            _selectedDate = nextSelectedDate;
            renderPage();
        }
        return true;
    }

    _year = year;
    _month = month;
    _items = [];
    _overview = null;
    _selectedDate = nextSelectedDate;
    await loadAndRender();
    return true;
}

function parseMonthInput(value) {
    const matched = String(value || '')
        .trim()
        .match(/^(\d{4})-(\d{1,2})$/);
    if (!matched) return null;
    const year = Number(matched[1]);
    const month = Number(matched[2]);
    if (!Number.isInteger(year) || !Number.isInteger(month)) return null;
    if (year < DIARY_YEAR_START || year > DIARY_YEAR_END) return null;
    if (month < 1 || month > 12) return null;
    return { year, month };
}

function diaryWordCount(item) {
    return Array.from(String(item?.content || '').trim()).length;
}

function moodEmoji(mood) {
    const normalized = String(mood || '')
        .trim()
        .toLowerCase();
    return normalized && Object.hasOwn(_moodEmojis, normalized) ? String(_moodEmojis[normalized] || '') : '';
}

function moodBadgeText(mood) {
    const normalized = String(mood || '').trim();
    if (!normalized) return '';
    const emoji = moodEmoji(normalized);
    const moodId = normalized.toLowerCase();
    const label = Object.hasOwn(_moodLabels, moodId) ? _moodLabels[moodId] : normalized;
    return emoji ? `${emoji} ${label}` : label;
}

function itemEntryTimestamp(item) {
    if (item?.entry_time) return item.entry_time;
    if (item?.created_at) return item.created_at;
    if (item?.updated_at) return item.updated_at;
    const diaryDate = parseDate(item?.diary_date);
    return diaryDate ? `${isoDate(diaryDate)}T00:00:00` : '';
}

function parseDiaryTimestamp(value) {
    const text = String(value || '').trim();
    if (!text) return null;
    const datePrefix = text.match(/^(\d{4}-\d{2}-\d{2})/)?.[1];
    if (datePrefix && !parseDate(datePrefix)) return null;
    return parseDate(text);
}

function formatEntryTime(item, { timeOnly = false } = {}) {
    const raw = itemEntryTimestamp(item);
    const text = String(raw).trim();
    const timestamp = parseDiaryTimestamp(text);
    const explicitTime = /[T ]\d{2}:\d{2}/.test(text);
    const time = timestamp && explicitTime ? `${pad2(timestamp.getHours())}:${pad2(timestamp.getMinutes())}` : '';
    if (timeOnly) return time || '全天';

    const diaryDate = parseDate(item?.diary_date);
    const date = diaryDate ? isoDate(diaryDate) : timestamp ? isoDate(timestamp) : '未知日期';
    return time ? `${date} ${time}` : date;
}

function defaultEntryTime(dateStr, userTimeZone) {
    const date = parseDate(dateStr);
    const nowInput = zonedDateTimeToInput(new Date().toISOString(), userTimeZone);
    return `${date ? isoDate(date) : todayStr()}T${nowInput.slice(11, 16)}`;
}

function compactDiaryCellLabel(entry, maxChars = 8) {
    const text = String(entry?.content || entry?.title || '')
        .replace(/^[\s\u3000]+/, '')
        .replace(/\s+/g, ' ')
        .trim();
    if (!text) return '';
    const limit = Number.isInteger(maxChars) && maxChars > 0 ? maxChars : 8;
    const chars = Array.from(text);
    if (chars.length <= limit) return text;
    return `${chars.slice(0, limit).join('')}…`;
}

function formatDateLabel(value) {
    const date = parseDate(value);
    if (!date) return '未知日期';
    return `${date.getFullYear()}年${date.getMonth() + 1}月${date.getDate()}日 · ${WEEK_NAMES[date.getDay()]}`;
}

function formatWordMetric(value) {
    const words = Math.max(0, finiteNumber(value));
    if (words >= 1000) {
        const compact = words >= 10000 ? (words / 1000).toFixed(0) : (words / 1000).toFixed(1);
        return `${compact}k`;
    }
    return String(Math.round(words));
}

function sortItems(items) {
    return records(items).sort((a, b) => {
        const dateA = `${a.diary_date || ''}|${itemEntryTimestamp(a)}`;
        const dateB = `${b.diary_date || ''}|${itemEntryTimestamp(b)}`;
        return dateB.localeCompare(dateA);
    });
}

function buildItemsByDate(items) {
    const map = new Map();
    sortItems(items).forEach((item) => {
        const key = item.diary_date || '';
        if (!parseDate(key)) return;
        if (!map.has(key)) map.set(key, []);
        map.get(key).push(item);
    });
    return map;
}

function ensureSelectedDate() {
    const monthPrefix = `${_year}-${pad2(_month)}`;
    const dates = new Set(
        _items
            .map((item) => item.diary_date)
            .filter((value) => typeof value === 'string' && value.startsWith(monthPrefix)),
    );
    const today = todayStr();
    const inCurrentMonth = monthPrefix === today.slice(0, 7);
    if (_selectedDate && _selectedDate.startsWith(monthPrefix)) return;
    if (inCurrentMonth) {
        _selectedDate = today;
        return;
    }
    const sortedDates = [...dates].sort((a, b) => b.localeCompare(a));
    _selectedDate = sortedDates[0] || monthStart(_year, _month);
}

function normalizeDiaryOverview(payload) {
    const overview = isRecord(payload) ? payload : {};
    const summary = isRecord(overview.summary) ? overview.summary : {};
    const busiestDay = isRecord(summary.busiest_day) ? summary.busiest_day : null;

    return {
        summary: {
            entry_count: nonNegativeInteger(summary.entry_count),
            active_days: nonNegativeInteger(summary.active_days),
            current_streak: nonNegativeInteger(summary.current_streak),
            longest_streak: nonNegativeInteger(summary.longest_streak),
            average_length: Math.max(0, finiteNumber(summary.average_length)),
            total_words: nonNegativeInteger(summary.total_words),
            fill_rate: Math.min(1, Math.max(0, finiteNumber(summary.fill_rate))),
            busiest_day: busiestDay
                ? {
                      date: typeof busiestDay.date === 'string' ? busiestDay.date : '',
                      words: nonNegativeInteger(busiestDay.words),
                  }
                : null,
        },
        mood_breakdown: records(overview.mood_breakdown)
            .map((item) => ({
                mood: String(item.mood || '').trim(),
                count: nonNegativeInteger(item.count),
                share: Math.min(1, Math.max(0, finiteNumber(item.share))),
            }))
            .filter((item) => item.mood && item.count > 0),
        cadence: records(overview.cadence).map((item) => ({
            date: typeof item.date === 'string' ? item.date : '',
            label: typeof item.label === 'string' ? item.label : '',
            words: nonNegativeInteger(item.words),
        })),
        recent_entries: records(overview.recent_entries),
        template_usage: records(overview.template_usage)
            .map((item) => ({
                template_id: String(item.template_id || '').trim(),
                count: nonNegativeInteger(item.count),
            }))
            .filter((item) => item.template_id && item.count > 0),
    };
}

function normalizeTemplates(value) {
    return records(value)
        .map((template) => {
            const id = String(template.id || '').trim();
            const prompts = Array.isArray(template.prompts)
                ? [...new Set(template.prompts.map((prompt) => String(prompt || '').trim()).filter(Boolean))]
                : [];
            return {
                id,
                name: String(template.name || id).trim() || id,
                prompts,
            };
        })
        .filter((template) => template.id);
}

async function fetchItems(year, month) {
    const items = [];
    let page = 1;
    const pageSize = 80;
    while (true) {
        const res = await api.get('/items', {
            type: 'diary',
            date_field: 'diary_date',
            start_date: monthStart(year, month),
            end_date: monthEnd(year, month),
            page,
            page_size: pageSize,
            sort: 'entry_time',
            order: 'desc',
        });
        const data = isRecord(res?.data) ? res.data : {};
        const batch = records(data.items);
        const total = nonNegativeInteger(data.total);
        items.push(...batch);
        if (items.length >= total || batch.length < pageSize) break;
        page += 1;
    }
    return sortItems(items);
}

async function fetchOverview(year, month) {
    const res = await api.get('/stats/diary/overview', {
        year,
        month,
        today: todayStr(),
    });
    return normalizeDiaryOverview(res?.data);
}

async function loadTemplates() {
    if (_templatesLoaded) return;
    try {
        const res = await api.get('/config/diary/templates');
        _templates = normalizeTemplates(res?.data?.templates);
        _templatesLoaded = true;
    } catch {
        _templates = [];
    }
}

async function loadMoodEmojis() {
    try {
        const res = await api.get('/config/diary/moods');
        const data = isRecord(res?.data) ? res.data : {};
        const fetchedEmojis = isRecord(data.mood_emojis) ? data.mood_emojis : {};
        const fetchedLabels = isRecord(data.mood_labels) ? data.mood_labels : {};
        const fetchedMoods = records(data.moods);
        const validMoodId = (value) => {
            const id = String(value || '')
                .trim()
                .toLowerCase();
            return /^[a-z0-9_-]+$/.test(id) && !['__proto__', 'constructor', 'prototype'].includes(id) ? id : '';
        };

        const emojis = { ...DEFAULT_MOOD_EMOJIS };
        const labels = { ...DEFAULT_MOOD_LABELS };
        for (const [rawId, emoji] of Object.entries(fetchedEmojis)) {
            const id = validMoodId(rawId);
            if (id) emojis[id] = String(emoji || '').trim();
        }
        for (const [rawId, label] of Object.entries(fetchedLabels)) {
            const id = validMoodId(rawId);
            if (id) labels[id] = String(label || '').trim() || id;
        }

        const configuredOptions = fetchedMoods
            .map((item) => {
                const id = validMoodId(item.id);
                return id
                    ? {
                          value: id,
                          label: String(item.label || labels[id] || id),
                          emoji: String(item.emoji || emojis[id] || id),
                      }
                    : null;
            })
            .filter((item) => item !== null);
        _moodEmojis = emojis;
        _moodLabels = labels;
        _moodOptions = configuredOptions.length
            ? configuredOptions
            : Object.keys(labels).map((id) => ({
                  value: id,
                  label: labels[id],
                  emoji: emojis[id] || id,
              }));
        return;
    } catch {
        // 配置接口临时不可用时继续使用与后端一致的内置选项。
    }
    _moodEmojis = { ...DEFAULT_MOOD_EMOJIS };
    _moodLabels = { ...DEFAULT_MOOD_LABELS };
    _moodOptions = [...DEFAULT_MOOD_OPTIONS];
}

function buildCalendarDays(year, month) {
    const first = new Date(year, month - 1, 1);
    const offset = (first.getDay() + 6) % 7;
    const start = new Date(year, month - 1, 1 - offset);
    const today = todayStr();
    const days = [];
    for (let index = 0; index < 42; index += 1) {
        const current = new Date(start);
        current.setDate(start.getDate() + index);
        days.push({
            date: current,
            key: isoDate(current),
            inMonth: current.getMonth() + 1 === month,
            isToday: isoDate(current) === today,
        });
    }
    return days;
}

function moodPalette(index) {
    return MOOD_SWATCHES[index % MOOD_SWATCHES.length];
}

// 页面样式由页面入口或外部打开的日记模态框确保存在。
function ensureStyles() {
    injectStyles(
        CSS_ID,
        `
        ${pageShellCss('diary-shell', { compactPadding: '20px 16px 30px', compactBreakpoint: BREAKPOINTS.MOBILE })}
        .diary-stack { display: flex; flex-direction: column; gap: 18px; }
        .diary-hero {
            display: grid; grid-template-columns: minmax(0, 1fr) auto; gap: 18px; align-items: center;
            padding: 24px 26px; border-radius: 28px; margin-bottom: 18px;
            background:
                radial-gradient(circle at top right, rgba(236,72,153,0.18), transparent 30%),
                radial-gradient(circle at bottom left, rgba(244,114,182,0.12), transparent 22%),
                linear-gradient(145deg, rgba(255,255,255,0.98), rgba(253,242,248,0.95));
            border: 1px solid rgba(236,72,153,0.14); box-shadow: 0 18px 40px rgba(236,72,153,0.07);
        }
        .diary-hero h2 { margin: 0; font-size: 30px; font-weight: 820; letter-spacing: -0.03em; color: var(--color-diary, #EC4899); }
        .diary-hero p { margin: 8px 0 0; font-size: 14px; line-height: 1.75; color: var(--color-text-secondary); }
        .diary-hero-tags, .diary-template-chips { display: flex; gap: 8px; flex-wrap: wrap; margin-top: 14px; }
        .diary-hero-tag, .diary-template-chip {
            display: inline-flex; align-items: center; gap: 6px; height: 34px; padding: 0 14px; border-radius: 999px;
            background: rgba(236,72,153,0.08); color: #be185d; font-size: 12px; font-weight: 700;
        }
        .diary-hero-actions { display: flex; flex-direction: column; gap: 10px; align-items: flex-end; }
        .diary-hero-note {
            display: inline-flex; align-items: center; padding: 8px 12px; border-radius: 999px;
            background: rgba(255,255,255,0.84); border: 1px solid rgba(236,72,153,0.12); color: #9d174d; font-size: 12px; font-weight: 700;
        }
        .diary-summary-grid { display: grid; grid-template-columns: repeat(4, minmax(0, 1fr)); gap: 14px; }
        .diary-summary-card, .diary-panel, .diary-workspace {
            background: linear-gradient(180deg, rgba(255,255,255,0.98), rgba(253,242,248,0.95));
            border: 1px solid rgba(236,72,153,0.12); border-radius: 24px; box-shadow: 0 16px 34px rgba(236,72,153,0.05);
        }
        .diary-summary-card { padding: 18px; min-width: 0; }
        .diary-summary-label { font-size: 12px; font-weight: 700; color: var(--color-text-secondary); }
        .diary-summary-value {
            margin-top: 10px; font-size: clamp(24px, 1.9vw, 30px); font-weight: 820; line-height: 1.04; color: #0f172a; letter-spacing: -0.03em;
            overflow-wrap: anywhere; word-break: break-word;
        }
        .diary-summary-meta { margin-top: 8px; font-size: 12px; color: var(--color-text-secondary); overflow-wrap: anywhere; word-break: break-word; }
        .diary-layout { display: grid; grid-template-columns: minmax(0, 1.08fr) minmax(300px, 0.92fr); gap: 16px; }
        .diary-side-stack { display: flex; flex-direction: column; gap: 16px; }
        .diary-panel-head, .diary-workspace-head { display: flex; align-items: flex-start; justify-content: space-between; gap: 12px; padding: 18px 20px 0; }
        .diary-panel-head h3, .diary-workspace-title { margin: 0; font-size: 18px; font-weight: 780; color: var(--color-text); letter-spacing: -0.02em; }
        .diary-panel-head p, .diary-workspace-subtitle { margin: 6px 0 0; font-size: 13px; color: var(--color-text-secondary); line-height: 1.7; }
        .diary-panel-body, .diary-workspace-body { padding: 16px 20px 20px; }
        .diary-month-nav {
            position: relative;
            display: inline-grid; grid-template-columns: 32px minmax(108px, auto) 32px; align-items: center; column-gap: 4px;
            min-height: 42px; padding: 4px 6px;
            border-radius: 999px; background: rgba(255,255,255,0.88); border: 1px solid rgba(236,72,153,0.14);
        }
        .diary-month-label {
            position: absolute; width: 1px; height: 1px; padding: 0; margin: -1px;
            overflow: hidden; clip: rect(0, 0, 0, 0); white-space: nowrap; border: 0;
        }
        .diary-month-nav:focus-within {
            box-shadow: 0 0 0 3px rgba(244,114,182,0.12);
        }
        .diary-month-nav button {
            width: 32px; height: 32px; padding: 0; margin: 0; display: inline-flex; align-items: center; justify-content: center; align-self: center;
            border: none; background: transparent; border-radius: 999px; cursor: pointer;
            color: var(--color-text-secondary); line-height: 1;
        }
        .diary-month-nav button svg {
            width: 14px; height: 14px; display: block; flex: 0 0 auto;
        }
        .diary-month-nav button:hover { background: rgba(236,72,153,0.08); color: var(--color-diary, #EC4899); }
        .diary-month-nav button:disabled { cursor: not-allowed; opacity: 0.35; background: transparent; }
        .diary-month-nav input.diary-month-text {
            width: 100%; min-width: 0; height: 32px; box-sizing: border-box;
            margin: 0; padding: 0 8px; border: 0 !important; border-color: transparent !important; border-radius: 0; background: transparent !important;
            text-align: center; font-size: 13px; font-weight: 800; color: var(--color-text);
            box-shadow: none !important; outline: none; appearance: none; -webkit-appearance: none; caret-color: var(--color-diary, #EC4899);
        }
        .diary-month-nav input.diary-month-text::placeholder {
            color: rgba(100,116,139,0.72);
            font-weight: 700;
        }
        .diary-month-nav input.diary-month-text:hover {
            color: #0f172a;
        }
        .diary-month-nav input.diary-month-text:focus {
            background: transparent !important;
            border-color: transparent !important;
            box-shadow: none !important;
        }
        .mood-selector { display: flex; align-items: center; gap: 10px; flex-wrap: wrap; }
        .mood-btn {
            width: 42px; height: 42px; display: inline-flex; align-items: center; justify-content: center;
            border-radius: 14px; border: 1px solid rgba(244,114,182,0.22); background: rgba(255,255,255,0.96);
            cursor: pointer; font-size: 20px; opacity: 0.94; transform: scale(1);
            transition: transform 0.16s ease, border-color 0.16s ease, background 0.16s ease, box-shadow 0.16s ease;
        }
        .mood-btn:hover { border-color: rgba(244,114,182,0.34); background: rgba(253,242,248,0.66); transform: translateY(-1px) scale(1.03); }
        .mood-btn.active {
            opacity: 1; background: rgba(253,242,248,0.94); transform: scale(1.06);
            border-color: rgba(236,72,153,0.34); box-shadow: inset 0 0 0 1px rgba(236,72,153,0.12);
        }
        .diary-calendar-weekdays, .diary-calendar-grid { display: grid; grid-template-columns: repeat(7, minmax(0, 1fr)); gap: 10px; }
        .diary-calendar-weekdays { margin-bottom: 10px; }
        .diary-calendar-weekdays span { text-align: center; font-size: 12px; font-weight: 800; color: var(--color-text-secondary); letter-spacing: 0.04em; }
        .diary-day {
            min-height: 112px; border-radius: 20px; border: 1px solid rgba(236,72,153,0.10); background: rgba(255,255,255,0.92);
            padding: 12px; text-align: left; cursor: pointer; display: flex; flex-direction: column; gap: 4px;
            transition: transform 0.16s ease, box-shadow 0.16s ease, border-color 0.16s ease; position: relative; min-width: 0;
        }
        .diary-day:hover { transform: translateY(-1px); border-color: rgba(236,72,153,0.24); box-shadow: 0 12px 24px rgba(236,72,153,0.08); }
        .diary-day:focus-visible, .diary-recent-item:focus-visible, .diary-stream-item:focus-visible {
            outline: 2px solid var(--color-diary, #EC4899); outline-offset: 3px;
        }
        .diary-day.is-selected { border-color: rgba(236,72,153,0.42); box-shadow: inset 0 0 0 1px rgba(236,72,153,0.12), 0 16px 30px rgba(236,72,153,0.08); background: linear-gradient(180deg, rgba(255,255,255,0.96), rgba(253,242,248,0.9)); }
        .diary-day.is-outside { opacity: 0.38; background: rgba(248,250,252,0.7); }
        .diary-day.is-today { box-shadow: inset 0 0 0 1px rgba(244,114,182,0.32); }
        .diary-day-top { display: flex; align-items: center; justify-content: flex-start; gap: 8px; min-height: 28px; }
        .diary-day-number { font-size: 18px; font-weight: 800; color: var(--color-text); }
        .diary-day-body { display: flex; flex-direction: column; gap: 4px; min-height: 0; margin-top: 0; align-items: flex-start; text-align: left; }
        .diary-day-mood { font-size: 18px; line-height: 1; }
        .diary-day-copy {
            font-size: 12px; line-height: 1.5; color: var(--color-text-secondary);
            display: -webkit-box; -webkit-line-clamp: 2; -webkit-box-orient: vertical; overflow: hidden; word-break: break-word; overflow-wrap: anywhere;
        }
        .diary-mood-list, .diary-recent-list, .diary-entry-stack, .diary-stream-list { display: flex; flex-direction: column; gap: 12px; }
        .diary-mood-row { display: flex; flex-direction: column; gap: 6px; }
        .diary-mood-top { display: flex; align-items: center; justify-content: space-between; gap: 10px; font-size: 13px; }
        .diary-mood-name { display: inline-flex; align-items: center; gap: 8px; font-weight: 700; color: var(--color-text); }
        .diary-mood-dot { width: 10px; height: 10px; border-radius: 999px; }
        .diary-mood-count { color: var(--color-text-secondary); font-weight: 700; }
        .diary-mood-track { height: 10px; border-radius: 999px; background: rgba(251,207,232,0.4); overflow: hidden; }
        .diary-mood-fill { display: block; height: 100%; border-radius: inherit; }
        .diary-cadence {
            display: grid; grid-auto-flow: column; grid-auto-columns: minmax(26px, 1fr);
            gap: 8px; align-items: end; overflow-x: auto; padding: 2px 2px 8px;
        }
        .diary-cadence-col { display: flex; flex-direction: column; align-items: center; gap: 6px; min-width: 0; }
        .diary-cadence-value { font-size: 10px; font-weight: 700; color: var(--color-text); }
        .diary-cadence-bar { width: 100%; min-height: 10px; border-radius: 999px 999px 12px 12px; background: linear-gradient(180deg, rgba(236,72,153,0.88), rgba(244,114,182,0.46)); }
        .diary-cadence-label { font-size: 10px; color: var(--color-text-secondary); }
        .diary-recent-item, .diary-stream-item {
            border: 1px solid rgba(236,72,153,0.10); background: rgba(255,255,255,0.88); border-radius: 18px; padding: 12px 14px; text-align: left; cursor: pointer;
        }
        .diary-recent-item:hover, .diary-stream-item:hover { border-color: rgba(236,72,153,0.22); background: rgba(255,255,255,0.96); }
        .diary-recent-top { display: flex; justify-content: space-between; gap: 8px; width: 100%; font-size: 12px; color: var(--color-text-secondary); }
        .diary-recent-top strong { font-size: 13px; color: var(--color-text); }
        .diary-recent-preview { margin-top: 8px; font-size: 12px; line-height: 1.6; color: var(--color-text-secondary); }
        .diary-template-usage { margin-top: 14px; padding-top: 14px; border-top: 1px solid rgba(251,207,232,0.54); }
        .diary-mini-label { font-size: 11px; font-weight: 800; color: var(--color-text-secondary); letter-spacing: 0.04em; text-transform: uppercase; margin-bottom: 10px; }
        .diary-entry-card { padding: 16px; border-radius: 20px; border: 1px solid rgba(236,72,153,0.12); background: linear-gradient(180deg, rgba(255,255,255,0.96), rgba(253,242,248,0.9)); box-shadow: 0 12px 26px rgba(236,72,153,0.04); }
        .diary-entry-head { display: flex; justify-content: space-between; gap: 16px; align-items: flex-start; }
        .diary-entry-title-row { display: flex; align-items: center; gap: 10px; }
        .diary-entry-mood { font-size: 26px; line-height: 1; }
        .diary-entry-head h4 { margin: 0; font-size: 18px; font-weight: 780; color: var(--color-text); }
        .diary-entry-meta { display: flex; flex-wrap: wrap; gap: 8px; margin-top: 8px; font-size: 12px; color: var(--color-text-secondary); }
        .diary-entry-actions { display: flex; gap: 8px; flex-wrap: wrap; }
        .diary-entry-preview { margin-top: 14px; font-size: 14px; line-height: 1.8; color: var(--color-text); white-space: pre-wrap; word-break: break-word; }
        .diary-empty-card { padding: 22px 18px; border-radius: 18px; text-align: center; background: rgba(255,255,255,0.82); border: 1px dashed rgba(244,114,182,0.26); color: var(--color-text-secondary); }
        .diary-empty-large { padding: 40px 18px; }
        .diary-empty-icon { font-size: 34px; margin-bottom: 10px; }
        .diary-stream-item { display: flex; gap: 12px; align-items: flex-start; }
        .diary-stream-date { width: 78px; flex-shrink: 0; font-size: 12px; font-weight: 800; color: #9d174d; padding-top: 2px; }
        .diary-stream-main { flex: 1; display: flex; flex-direction: column; gap: 6px; min-width: 0; }
        .diary-stream-main strong { font-size: 14px; color: var(--color-text); display: block; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
        .diary-stream-main span { font-size: 12px; line-height: 1.6; color: var(--color-text-secondary); display: -webkit-box; -webkit-line-clamp: 2; -webkit-box-orient: vertical; overflow: hidden; }
        .diary-stream-words { font-size: 11px; font-weight: 700; color: var(--color-text-secondary); flex-shrink: 0; }
        .diary-view-meta { display: flex; flex-wrap: wrap; gap: 8px; margin-bottom: 14px; padding-bottom: 14px; border-bottom: 1px solid rgba(251,207,232,0.54); }
        .diary-view-chip {
            display: inline-flex; align-items: center; gap: 6px; padding: 6px 10px; border-radius: 999px;
            background: rgba(236,72,153,0.08); color: #be185d; font-size: 12px; font-weight: 700;
        }
        .diary-view-content { white-space: pre-wrap; word-break: break-word; font-size: 14px; line-height: 1.8; color: var(--color-text); max-height: 60vh; overflow-y: auto; }
        .diary-template-select { margin-top: 2px; }
        .diary-template-hint { font-size: 12px; color: var(--color-text-secondary); margin-top: 6px; padding: 8px 10px; background: rgba(251,207,232,0.28); border-radius: 14px; line-height: 1.6; white-space: pre-wrap; }
        .diary-template-hint[hidden] { display: none; }
        .diary-template-answer-stack { display: flex; flex-direction: column; gap: 10px; margin-top: 10px; }
        .diary-template-answer label { display: block; margin-bottom: 6px; font-size: 12px; font-weight: 800; color: var(--color-text-secondary); }
        .diary-template-answer textarea { min-height: 74px; }
        .diary-template-answer-view { display: flex; flex-direction: column; gap: 10px; margin-bottom: 14px; }
        .diary-template-answer-item { padding: 12px 14px; border-radius: 16px; background: rgba(253,242,248,0.72); border: 1px solid rgba(251,207,232,0.7); }
        .diary-template-answer-item strong { display: block; font-size: 12px; color: #be185d; margin-bottom: 6px; }
        .diary-template-answer-item p { margin: 0; white-space: pre-wrap; word-break: break-word; color: var(--color-text); line-height: 1.7; }
        .form-checkbox { display: inline-flex; align-items: center; gap: 8px; color: var(--color-text-secondary); font-size: 13px; font-weight: 700; }
        .form-checkbox input { width: 16px; height: 16px; accent-color: var(--color-diary, #EC4899); }
        .diary-delete-action { margin-right: auto; }
        ${mediaMax(
            BREAKPOINTS.XL,
            `
            .diary-summary-grid { grid-template-columns: repeat(2, minmax(0, 1fr)); }
            .diary-layout { grid-template-columns: 1fr; }
        `,
        )}
        ${mediaMax(
            BREAKPOINTS.MOBILE,
            `
            .diary-hero { grid-template-columns: 1fr; padding: 22px 20px; }
            .diary-hero-actions { align-items: flex-start; }
            .diary-summary-grid { grid-template-columns: 1fr; }
            .diary-summary-card { padding: 14px 16px; border-radius: 20px; }
            .diary-summary-value { margin-top: 6px; font-size: 22px; }
            .diary-summary-meta { margin-top: 4px; font-size: 11px; }
            .diary-calendar-weekdays, .diary-calendar-grid { gap: 6px; }
            .diary-day { min-height: 80px; padding: 8px; border-radius: 16px; gap: 2px; }
            .diary-day-top { min-height: 24px; }
            .diary-day-number { font-size: 16px; }
            .diary-day-body { gap: 3px; margin-top: 0; align-items: flex-start; text-align: left; }
            .diary-day-copy { font-size: 11px; line-height: 1.4; }
            .diary-entry-head, .diary-workspace-head, .diary-panel-head { flex-direction: column; }
            .diary-month-nav { align-self: stretch; grid-template-columns: 32px minmax(0, 1fr) 32px; width: 100%; }
            .diary-stream-item { flex-direction: column; }
            .diary-stream-date { width: auto; }
        `,
        )}
        ${mediaMax(
            BREAKPOINTS.PHONE,
            `
            .diary-calendar-weekdays, .diary-calendar-grid { gap: 4px; }
            .diary-calendar-weekdays span { font-size: 10px; letter-spacing: 0; }
            .diary-day { min-height: 72px; padding: 6px; border-radius: 14px; gap: 1px; }
            .diary-day-top { min-height: 22px; }
            .diary-day-number { font-size: 15px; }
            .diary-day-body { gap: 2px; }
            .diary-day-mood { font-size: 15px; }
            .diary-day-copy { font-size: 10px; line-height: 1.35; }
        `,
        )}
    `,
    );
}

// ---------- 页面区域渲染 ----------

function renderHero() {
    const summary = _overview?.summary || {};
    const busiest = summary.busiest_day
        ? `${formatDateLabel(summary.busiest_day.date).split(' · ')[0]} · ${summary.busiest_day.words} 字`
        : '本月还没有高峰日';
    return `
        <section class="diary-hero">
            <div>
                <h2>📔 日记</h2>
                <p>按月记录每天的心情、天气和当天见闻。</p>
                <div class="diary-hero-tags">
                    <span class="diary-hero-tag">${monthLabel(_year, _month)}</span>
                    <span class="diary-hero-tag">${summary.entry_count || 0} 篇记录</span>
                    <span class="diary-hero-tag">${summary.current_streak || 0} 天连续</span>
                </div>
            </div>
            <div class="diary-hero-actions">
                <span class="diary-hero-note">最密集的一天：${busiest}</span>
                <button class="btn btn-primary" id="diary-add-top" type="button">＋ 写日记</button>
            </div>
        </section>
    `;
}

function renderSummaryCards() {
    const summary = _overview?.summary || {};
    const fillRate = Math.round((summary.fill_rate || 0) * 100);
    return `
        <section class="diary-summary-grid">
            <article class="diary-summary-card">
                <div class="diary-summary-label">本月记录</div>
                <div class="diary-summary-value">${summary.entry_count || 0}</div>
                <div class="diary-summary-meta">${summary.active_days || 0} 天有写作痕迹</div>
            </article>
            <article class="diary-summary-card">
                <div class="diary-summary-label">连续记录</div>
                <div class="diary-summary-value">${summary.current_streak || 0}</div>
                <div class="diary-summary-meta">最长连续 ${summary.longest_streak || 0} 天</div>
            </article>
            <article class="diary-summary-card">
                <div class="diary-summary-label">平均字数</div>
                <div class="diary-summary-value">${Math.round(summary.average_length || 0)}</div>
                <div class="diary-summary-meta">总计 ${summary.total_words || 0} 字</div>
            </article>
            <article class="diary-summary-card">
                <div class="diary-summary-label">月度填充率</div>
                <div class="diary-summary-value">${fillRate}%</div>
                <div class="diary-summary-meta">${fillRate >= 50 ? '这个月已经形成节奏' : '还可以多留下一些片段'}</div>
            </article>
        </section>
    `;
}

function renderCalendarPanel(entries) {
    return `
        <section class="diary-panel">
            <div class="diary-panel-head">
                <div>
                    <h3>月历书写地图</h3>
                    <p>点击日期即可查看或继续书写当天内容。</p>
                </div>
                <div class="diary-month-nav">
                    <button type="button" id="diary-prev-month" aria-label="上个月"${_year === DIARY_YEAR_START && _month === 1 ? ' disabled' : ''}>${DIARY_NAV_PREV_ICON}</button>
                    <label class="diary-month-label" for="diary-month-text">月份</label>
                    <input
                        id="diary-month-text"
                        class="diary-month-text"
                        type="text"
                        inputmode="numeric"
                        autocomplete="off"
                        spellcheck="false"
                        placeholder="YYYY-MM"
                        value="${monthInputValue(_year, _month)}"
                        aria-label="输入月份，例如 2026-03"
                    >
                    <button type="button" id="diary-next-month" aria-label="下个月"${_year === DIARY_YEAR_END && _month === 12 ? ' disabled' : ''}>${DIARY_NAV_NEXT_ICON}</button>
                </div>
            </div>
            <div class="diary-panel-body">
                <div class="diary-calendar-weekdays">${CALENDAR_WEEKDAYS.map((label) => `<span>${label}</span>`).join('')}</div>
                <div class="diary-calendar-grid">
                    ${buildCalendarDays(_year, _month)
                        .map((day) => {
                            const list = entries.get(day.key) || [];
                            const first = list[0];
                            const totalWords = list.length
                                ? list.reduce((sum, item) => sum + diaryWordCount(item), 0)
                                : 0;
                            const metric = list.length > 1 ? `${list.length} 篇 ${totalWords} 字` : `${totalWords} 字`;
                            const meta = list.length > 1 ? '' : compactDiaryCellLabel(first);
                            const copy = meta ? `${metric} ${meta}` : metric;
                            const mood = first?.mood
                                ? `<span class="diary-day-mood">${escapeHtml(moodEmoji(first.mood) || first.mood)}</span>`
                                : '';
                            const selected = day.key === _selectedDate;
                            const ariaLabel = list.length
                                ? `${formatDateLabel(day.key)}，${metric}`
                                : `${formatDateLabel(day.key)}，没有日记`;
                            return `
                             <button
                                 type="button"
                                 class="diary-day${day.inMonth ? '' : ' is-outside'}${selected ? ' is-selected' : ''}${day.isToday ? ' is-today' : ''}"
                                 data-date="${day.key}"
                                 aria-label="${escapeHtml(ariaLabel)}"
                                 aria-pressed="${selected}"
                             >
                                <span class="diary-day-top">
                                    <span class="diary-day-number">${day.date.getDate()}</span>
                                </span>
                                ${
                                    day.inMonth && list.length
                                        ? `
                                    <span class="diary-day-body">
                                        ${mood}
                                        <span class="diary-day-copy">${escapeHtml(copy)}</span>
                                    </span>`
                                        : ''
                                }
                            </button>`;
                        })
                        .join('')}
                </div>
            </div>
        </section>
    `;
}

function renderMoodPanel() {
    const moods = _overview?.mood_breakdown || [];
    if (!moods.length) {
        return `
            <section class="diary-panel">
                <div class="diary-panel-head"><div><h3>心情分布</h3><p>记录心情后，这里会显示本月情绪分布。</p></div></div>
                <div class="diary-panel-body"><div class="diary-empty-card">当前还没有可统计的心情数据。</div></div>
            </section>`;
    }
    return `
        <section class="diary-panel">
            <div class="diary-panel-head"><div><h3>心情分布</h3><p>查看这个月的心情分布。</p></div></div>
            <div class="diary-panel-body">
                <div class="diary-mood-list">
                    ${moods
                        .map(
                            (item, index) => `
                        <div class="diary-mood-row">
                            <div class="diary-mood-top">
                                <span class="diary-mood-name"><span class="diary-mood-dot" style="background:${moodPalette(index)};"></span>${escapeHtml(moodBadgeText(item.mood) || item.mood)}</span>
                                <span class="diary-mood-count">${item.count} 条</span>
                            </div>
                            <div class="diary-mood-track"><span class="diary-mood-fill" style="width:${Math.round(item.share * 100)}%;background:${moodPalette(index)};"></span></div>
                        </div>`,
                        )
                        .join('')}
                </div>
            </div>
        </section>
    `;
}

function renderCadencePanel() {
    const cadence = _overview?.cadence || [];
    const maxValue = cadence.reduce((maximum, item) => Math.max(maximum, item.words), 1);
    return `
        <section class="diary-panel">
            <div class="diary-panel-head"><div><h3>书写密度</h3><p>查看这个月每天写了多少字。</p></div></div>
            <div class="diary-panel-body">
                <div class="diary-cadence">
                    ${cadence
                        .map(
                            (item) => `
                        <div class="diary-cadence-col" title="${escapeHtml(`${item.date} · ${item.words} 字`)}">
                            <span class="diary-cadence-value">${formatWordMetric(item.words)}</span>
                            <span class="diary-cadence-bar" style="height:${12 + (item.words / maxValue) * 64}px;"></span>
                            <span class="diary-cadence-label">${escapeHtml(item.label)}</span>
                        </div>`,
                        )
                        .join('')}
                </div>
            </div>
        </section>
    `;
}

function renderRecentPanel() {
    const recent = _overview?.recent_entries || [];
    const templates = _overview?.template_usage || [];
    return `
        <section class="diary-panel">
            <div class="diary-panel-head"><div><h3>最近回看</h3><p>快速回到这个月最近写下的几天。</p></div></div>
            <div class="diary-panel-body">
                ${
                    recent.length
                        ? `
                    <div class="diary-recent-list">
                        ${recent
                            .slice(0, 4)
                            .map(
                                (item) => `
                            <button type="button" class="diary-recent-item" data-id="${escapeHtml(String(item.id))}" data-date="${escapeHtml(String(item.diary_date || ''))}">
                                <span class="diary-recent-top"><strong>${escapeHtml(moodEmoji(item.mood) || '📖')} ${escapeHtml(item.title || item.diary_date)}</strong><span>${escapeHtml(item.entry_label || formatEntryTime(item))}</span></span>
                                <span class="diary-recent-preview">${escapeHtml(item.content_preview || '点击查看详情')}</span>
                            </button>`,
                            )
                            .join('')}
                    </div>`
                        : '<div class="diary-empty-card">这个月还没有可以回看的日记。</div>'
                }
                ${
                    templates.length
                        ? `
                    <div class="diary-template-usage">
                        <div class="diary-mini-label">模板使用</div>
                        <div class="diary-template-chips">
                            ${templates
                                .slice(0, 4)
                                .map(
                                    (item) =>
                                        `<span class="diary-template-chip">${escapeHtml(item.template_id)} · ${item.count}</span>`,
                                )
                                .join('')}
                        </div>
                    </div>`
                        : ''
                }
            </div>
        </section>
    `;
}

function renderEntryCard(item) {
    const itemId = escapeHtml(String(item.id || ''));
    return `
        <article class="diary-entry-card" data-id="${itemId}">
            <div class="diary-entry-head">
                <div>
                    <div class="diary-entry-title-row"><span class="diary-entry-mood">${escapeHtml(moodEmoji(item.mood) || '📖')}</span><h4>${escapeHtml(item.title || '这一天的记录')}</h4></div>
                    <div class="diary-entry-meta">
                        <span>${escapeHtml(formatEntryTime(item, { timeOnly: true }))}</span>
                        <span>${escapeHtml(item.weather || '未记录天气')}</span>
                        ${item.location ? `<span>📍 ${escapeHtml(item.location)}</span>` : ''}
                        <span>${diaryWordCount(item)} 字</span>
                        ${item.is_favorite ? '<span>收藏</span>' : ''}
                    </div>
                </div>
                <div class="diary-entry-actions">
                    <button class="btn btn-sm btn-secondary" data-action="view" data-id="${itemId}" type="button">查看</button>
                    <button class="btn btn-sm btn-secondary" data-action="edit" data-id="${itemId}" type="button">编辑</button>
                    <button class="btn btn-sm btn-danger" data-action="delete" data-id="${itemId}" type="button">删除</button>
                </div>
            </div>
            <div class="diary-entry-preview">${escapeHtml(previewText(item.content, 220) || '（无内容）')}</div>
        </article>
    `;
}

function renderSelectedDay(entriesByDate) {
    const entries = entriesByDate.get(_selectedDate) || [];
    return `
        <section class="diary-workspace">
            <div class="diary-workspace-head">
                <div>
                    <h3 class="diary-workspace-title">${escapeHtml(formatDateLabel(_selectedDate))}</h3>
                    <p class="diary-workspace-subtitle">${entries.length ? '查看、编辑或删除当天的日记内容。' : '这一天还没有日记，可以直接开始记录。'}</p>
                </div>
                <button class="btn btn-secondary" id="diary-add-selected" type="button">${entries.length ? '继续写一篇' : '为这一天写日记'}</button>
            </div>
            <div class="diary-workspace-body">
                ${
                    entries.length
                        ? `<div class="diary-entry-stack">${entries.map(renderEntryCard).join('')}</div>`
                        : `
                    <div class="diary-empty-card diary-empty-large">
                        <div class="diary-empty-icon">🌙</div>
                        <div>这一天还没有留下文字。点右上角按钮，直接把片刻写下来。</div>
                    </div>`
                }
            </div>
        </section>
    `;
}

function renderMonthStream() {
    return `
        <section class="diary-panel">
            <div class="diary-panel-head"><div><h3>本月回看</h3><p>按日期倒序浏览这个月的记录。</p></div></div>
            <div class="diary-panel-body">
                ${
                    _items.length
                        ? `
                    <div class="diary-stream-list">
                        ${_items
                            .map(
                                (item) => `
                            <button type="button" class="diary-stream-item" data-id="${escapeHtml(String(item.id || ''))}">
                                <span class="diary-stream-date">${escapeHtml(formatEntryTime(item))}</span>
                                <span class="diary-stream-main">
                                    <strong>${escapeHtml(moodEmoji(item.mood) || '📖')} ${escapeHtml(item.title || previewText(item.content, 18) || '这一天')}</strong>
                                    <span>${escapeHtml(previewText(item.content, 70) || '点击查看详情')}</span>
                                </span>
                                <span class="diary-stream-words">${diaryWordCount(item)} 字</span>
                            </button>`,
                            )
                            .join('')}
                    </div>`
                        : '<div class="diary-empty-card">本月还没有任何日记记录。</div>'
                }
            </div>
        </section>
    `;
}

function renderPage() {
    if (!_container) return;
    if (_loading && !_overview) {
        _container.innerHTML =
            '<div class="diary-shell"><div class="diary-empty-card diary-empty-large">正在加载日记空间...</div></div>';
        return;
    }
    const entriesByDate = buildItemsByDate(_items);
    _container.innerHTML = `
        <div class="diary-shell">
            ${renderHero()}
            <div class="diary-stack">
                ${renderSummaryCards()}
                <section class="diary-layout">
                    ${renderCalendarPanel(entriesByDate)}
                    <div class="diary-side-stack">
                        ${renderMoodPanel()}
                        ${renderCadencePanel()}
                        ${renderRecentPanel()}
                    </div>
                </section>
                ${renderSelectedDay(entriesByDate)}
                ${renderMonthStream()}
            </div>
        </div>`;
    attachListeners();
}

// ---------- 模板回答与日记模态框 ----------

function normalizeTemplateAnswers(raw) {
    if (!Array.isArray(raw)) return [];
    return raw
        .map((item) => ({
            prompt: String(item?.prompt || '').trim(),
            answer: String(item?.answer || '').trim(),
        }))
        .filter((item) => item.prompt || item.answer);
}

function renderTemplateAnswers(answers) {
    const normalized = normalizeTemplateAnswers(answers);
    if (!normalized.length) return '';
    return `
        <div class="diary-template-answer-view">
            ${normalized
                .map(
                    (item) => `
                <div class="diary-template-answer-item">
                    ${item.prompt ? `<strong>${escapeHtml(item.prompt)}</strong>` : ''}
                    ${item.answer ? `<p>${escapeHtml(item.answer)}</p>` : ''}
                </div>`,
                )
                .join('')}
        </div>`;
}

function templateAnswerInputRows(template, answers = []) {
    const prompts = Array.isArray(template?.prompts)
        ? [...new Set(template.prompts.map((prompt) => String(prompt || '').trim()).filter(Boolean))]
        : [];
    const remainingAnswers = normalizeTemplateAnswers(answers);
    const rows = prompts.map((prompt) => {
        const answerIndex = remainingAnswers.findIndex((item) => item.prompt === prompt);
        const matched = answerIndex >= 0 ? remainingAnswers.splice(answerIndex, 1)[0] : null;
        return { prompt, answer: matched?.answer || '' };
    });
    rows.push(...remainingAnswers);
    return rows.map((row, index) => ({
        ...row,
        label: row.prompt || `问题 ${index + 1}`,
    }));
}

function renderTemplateAnswerInputs(template, answers = []) {
    const rows = templateAnswerInputRows(template, answers);
    if (!rows.length) return '';
    return `
        <div class="diary-template-answer-stack">
            ${rows
                .map(
                    (row, index) => `
                <div class="diary-template-answer">
                    <label for="diary-template-answer-${index}">${escapeHtml(row.label)}</label>
                    <textarea
                        id="diary-template-answer-${index}"
                        class="form-input diary-template-answer-input"
                        data-prompt="${escapeHtml(row.prompt)}"
                        rows="3"
                    >${escapeHtml(row.answer)}</textarea>
                </div>`,
                )
                .join('')}
        </div>`;
}

function collectTemplateAnswers(form) {
    return [...form.querySelectorAll('.diary-template-answer-input')]
        .map((el) => ({
            prompt: String(el.dataset.prompt || '').trim(),
            answer: String(el.value || '').trim(),
        }))
        .filter((item) => item.prompt || item.answer);
}

function templateAnswersToContent(answers) {
    return answers
        .filter((item) => item.answer)
        .map((item) => [item.prompt, item.answer].filter(Boolean).join('\n'))
        .join('\n\n');
}

function dispatchDiaryChange() {
    window.dispatchEvent(new CustomEvent('pendo-data-changed', { detail: { type: 'diary' } }));
}

export function openDiaryViewModal(item) {
    if (!isRecord(item)) {
        throw new TypeError('查看日记需要有效的条目');
    }
    ensureStyles();
    const moodScore = finiteNumber(item.mood_score);
    const bodyHTML = `
        <div class="diary-view-meta">
            <span class="diary-view-chip">📅 ${escapeHtml(item.diary_date)}</span>
            <span class="diary-view-chip">🕒 ${escapeHtml(formatEntryTime(item, { timeOnly: true }))}</span>
            ${item.mood ? `<span class="diary-view-chip">${escapeHtml(moodBadgeText(item.mood))}</span>` : ''}
            ${moodScore >= 1 && moodScore <= 10 ? `<span class="diary-view-chip">心情 ${moodScore}/10</span>` : ''}
            ${item.weather ? `<span class="diary-view-chip">${escapeHtml(item.weather)}</span>` : ''}
            ${item.location ? `<span class="diary-view-chip">📍 ${escapeHtml(item.location)}</span>` : ''}
            ${item.is_favorite ? '<span class="diary-view-chip">收藏</span>' : ''}
            <span class="diary-view-chip">${diaryWordCount(item)} 字</span>
        </div>
        ${renderTemplateAnswers(item.template_answers)}
        <div class="diary-view-content">${escapeHtml(item.content || '')}</div>
    `;
    const footer = `
        <button class="btn btn-danger btn-sm diary-delete-action" id="diary-view-delete" type="button">删除</button>
        <button class="btn btn-secondary" id="diary-view-close" type="button">关闭</button>
        <button class="btn btn-primary" id="diary-view-edit" type="button">编辑</button>
    `;
    const content = showModal(item.title || formatDateLabel(item.diary_date), safeHtml(bodyHTML), {
        footer: safeHtml(footer),
    });
    content.querySelector('#diary-view-close').onclick = closeModal;
    content.querySelector('#diary-view-edit').onclick = () => {
        closeModal();
        openDiaryFormModal(item);
    };
    content.querySelector('#diary-view-delete').onclick = async () => {
        closeModal();
        await deleteDiary(item, () => openDiaryViewModal(item));
    };
}

async function openDiaryFormModal(existing = null, presetDate = null) {
    if (_formOpening) return;
    _formOpening = true;
    try {
        const userTimeZone = await fetchUserTimeZone();
        ensureStyles();
        const diary = isRecord(existing) ? existing : null;
        const isEdit = diary !== null;
        const formDate = diary?.diary_date || presetDate || _selectedDate || todayStr();
        const fields = DIARY_FIELDS.map((field) => {
            let value = '';
            if (diary) value = diary[field.name] ?? '';
            else if (field.name === 'diary_date') value = formDate;
            else if (field.name === 'entry_time') value = defaultEntryTime(formDate, userTimeZone);
            if (field.name === 'entry_time') {
                const inputValue = zonedDateTimeToInput(value, userTimeZone);
                if (value && !inputValue) throw new Error('记录时间格式无效，已阻止编辑以免覆盖原值');
                value = inputValue;
            }
            const nextField = { ...field, value };
            if (field.name === 'mood') nextField.options = _moodOptions;
            return nextField;
        });

        let templateSectionHTML = '';
        await loadTemplates();
        if (!isEdit && _templates.length) {
            templateSectionHTML = `
                <div class="form-group">
                    <label class="form-label">模板（可选）</label>
                    ${renderCustomSelect({
                        id: 'diary-template-sel',
                        name: 'template_id',
                        options: [
                            { value: '', label: '-- 不使用模板 --' },
                            ..._templates.map((tpl) => ({ value: tpl.id, label: tpl.name })),
                        ],
                        selected: '',
                        className: 'pselect-form pselect-block pselect-theme-diary diary-template-select',
                    })}
                    <div id="diary-template-hint" class="diary-template-hint" hidden></div>
                </div>`;
        } else if (isEdit) {
            const existingAnswers = normalizeTemplateAnswers(diary.template_answers);
            const template = _templates.find((tpl) => String(tpl.id) === String(diary.template_id));
            const answerInputs = renderTemplateAnswerInputs(template, existingAnswers);
            if (answerInputs) {
                templateSectionHTML = `
                    <div class="form-group">
                        <label class="form-label">模板回答</label>
                        <div class="diary-template-hint">${answerInputs}</div>
                    </div>`;
            }
        }

        const content = showModal(
            isEdit ? '编辑日记' : '写日记',
            safeHtml(`<form id="diary-form">${templateSectionHTML}${buildFormHTML(fields)}</form>`),
            {
                footer: safeHtml(`
                ${isEdit ? '<button class="btn btn-danger btn-sm diary-delete-action" id="diary-modal-delete" type="button">删除</button>' : ''}
                <button class="btn btn-secondary" id="diary-modal-cancel" type="button">取消</button>
                <button class="btn btn-primary" id="diary-modal-save" type="button">保存</button>
            `),
            },
        );

        initFormInteractions(content);
        content.querySelector('#diary-modal-cancel').onclick = closeModal;

        if (!isEdit && _templates.length) {
            const templateHint = content.querySelector('#diary-template-hint');
            initCustomSelects(content, {
                'diary-template-sel': (value) => {
                    const template = _templates.find((tpl) => String(tpl.id) === String(value));
                    if (!template || !template.prompts?.length) {
                        templateHint.innerHTML = '';
                        templateHint.hidden = true;
                        return;
                    }
                    templateHint.innerHTML = renderTemplateAnswerInputs(template);
                    templateHint.hidden = false;
                },
            });
        }

        if (isEdit) {
            content.querySelector('#diary-modal-delete').onclick = async () => {
                closeModal();
                await deleteDiary(diary, () => openDiaryFormModal(diary));
            };
        }

        const saveButton = content.querySelector('#diary-modal-save');
        saveButton.onclick = async () => {
            if (saveButton.disabled) return;
            const form = content.querySelector('#diary-form');
            const data = getFormData(form);
            const templateAnswerInputs = form.querySelectorAll('.diary-template-answer-input');
            const templateAnswers = collectTemplateAnswers(form);
            if (templateAnswerInputs.length || templateAnswers.length) {
                data.template_answers = templateAnswers;
                if (!String(data.content || '').trim()) {
                    data.content = templateAnswersToContent(templateAnswers);
                }
            }
            const rawDiaryDate = String(data.diary_date || '').trim();
            if (!rawDiaryDate) {
                showToast('请选择日期', 'warning');
                return;
            }
            const parsedDiaryDate = parseDate(rawDiaryDate);
            const diaryDate = parsedDiaryDate ? isoDate(parsedDiaryDate) : '';
            if (diaryDate !== rawDiaryDate) {
                showToast('请选择有效日期', 'warning');
                return;
            }
            if (!String(data.content || '').trim()) {
                showToast('请填写日记内容', 'warning');
                return;
            }

            data.diary_date = diaryDate;
            const rawEntryTime = String(data.entry_time || '').trim() || defaultEntryTime(diaryDate, userTimeZone);
            const timeMatch = rawEntryTime.match(/T(\d{2}:\d{2})(?::(\d{2}))?$/);
            if (!timeMatch) {
                showToast('请输入有效的记录时间', 'warning');
                return;
            }
            const wallTime = `${diaryDate}T${timeMatch[1]}:${timeMatch[2] || '00'}`;
            try {
                data.entry_time = zonedInputToUtcIso(wallTime, userTimeZone);
            } catch (error) {
                showToast(errorMessage(error), 'warning');
                return;
            }
            saveButton.disabled = true;
            try {
                if (isEdit) {
                    const itemId = typeof diary.id === 'string' ? diary.id.trim() : '';
                    if (!itemId) throw new Error('日记缺少有效编号');
                    await api.put(`/items/${encodeURIComponent(itemId)}`, data);
                    showToast('日记已更新', 'success');
                } else {
                    await api.post('/items', { type: 'diary', ...data });
                    showToast('日记已添加', 'success');
                }
                closeModal();
                _selectedDate = diaryDate;
                dispatchDiaryChange();
            } catch (error) {
                showToast(`保存失败：${errorMessage(error)}`, 'error');
            } finally {
                saveButton.disabled = false;
            }
        };
    } catch (error) {
        showToast(`无法打开日记编辑器：${errorMessage(error)}`, 'error');
    } finally {
        _formOpening = false;
    }
}

async function deleteDiary(item, onCancel = null) {
    if (!isRecord(item)) return;
    const confirmed = await showConfirmModal({
        title: '删除日记',
        message: `确定要删除“${item.title || formatDateLabel(item.diary_date)}”吗？删除后内容将无法恢复。`,
        confirmText: '删除',
        cancelText: '取消',
        tone: 'danger',
    });
    if (!confirmed) {
        if (typeof onCancel === 'function') await onCancel();
        return;
    }
    try {
        const itemId = typeof item.id === 'string' ? item.id.trim() : '';
        if (!itemId) throw new Error('日记缺少有效编号');
        await api.delete(`/items/${encodeURIComponent(itemId)}`);
        showToast('日记已删除', 'success');
        dispatchDiaryChange();
    } catch (error) {
        showToast(`删除失败：${errorMessage(error)}`, 'error');
    }
}

function findDiaryItem(itemId) {
    return _items.find((item) => String(item.id) === String(itemId)) || null;
}

// ---------- 页面交互与异步生命周期 ----------

async function shiftMonth(offset) {
    const target = new Date(_year, _month - 1 + offset, 1);
    await updateMonth(target.getFullYear(), target.getMonth() + 1);
}

function attachListeners() {
    if (!_container) return;

    for (const selector of ['#diary-add-top', '#diary-add-selected']) {
        const button = _container.querySelector(selector);
        if (button) {
            button.onclick = () => {
                void openDiaryFormModal(null, _selectedDate || todayStr());
            };
        }
    }

    const prevBtn = _container.querySelector('#diary-prev-month');
    if (prevBtn) {
        prevBtn.onclick = async () => {
            await shiftMonth(-1);
        };
    }

    const nextBtn = _container.querySelector('#diary-next-month');
    if (nextBtn) {
        nextBtn.onclick = async () => {
            await shiftMonth(1);
        };
    }

    const monthText = _container.querySelector('#diary-month-text');
    if (monthText) {
        const commitMonthText = async () => {
            const parsed = parseMonthInput(monthText.value);
            if (!parsed) {
                monthText.value = monthInputValue(_year, _month);
                showToast(`月份格式请写成 ${monthInputValue(_year, _month)} 这种形式`, 'warning');
                return;
            }
            monthText.value = monthInputValue(parsed.year, parsed.month);
            await updateMonth(parsed.year, parsed.month);
        };
        monthText.addEventListener('keydown', async (event) => {
            if (event.key !== 'Enter' || event.isComposing) return;
            event.preventDefault();
            await commitMonthText();
        });
        monthText.addEventListener('blur', () => {
            void commitMonthText();
        });
    }

    _container.querySelectorAll('.diary-day[data-date]').forEach((button) => {
        button.onclick = () => {
            const selected = parseDate(button.dataset.date);
            if (!selected) return;
            void updateMonth(selected.getFullYear(), selected.getMonth() + 1, isoDate(selected));
        };
    });

    _container.querySelectorAll('.diary-recent-item').forEach((button) => {
        button.onclick = () => {
            const item = findDiaryItem(button.dataset.id);
            if (item) {
                _selectedDate = item.diary_date || _selectedDate;
                openDiaryViewModal(item);
                return;
            }
            _selectedDate = button.dataset.date || _selectedDate;
            renderPage();
        };
    });

    _container.querySelectorAll('.diary-stream-item[data-id]').forEach((button) => {
        button.onclick = () => {
            const item = findDiaryItem(button.dataset.id);
            if (!item) return;
            _selectedDate = item.diary_date || _selectedDate;
            openDiaryViewModal(item);
        };
    });

    _container.querySelectorAll('[data-action][data-id]').forEach((button) => {
        button.onclick = async (event) => {
            event.stopPropagation();
            const item = findDiaryItem(button.dataset.id);
            if (!item) return;
            const action = button.dataset.action;
            if (action === 'view') {
                openDiaryViewModal(item);
            } else if (action === 'edit') {
                await openDiaryFormModal(item);
            } else if (action === 'delete') {
                await deleteDiary(item);
            }
        };
    });
}

async function loadAndRender() {
    const container = _container;
    if (!container) return;

    const lifecycleVersion = _lifecycleVersion;
    const loadVersion = ++_loadVersion;
    const year = _year;
    const month = _month;
    const isCurrent = () =>
        _container === container &&
        _lifecycleVersion === lifecycleVersion &&
        _loadVersion === loadVersion &&
        _year === year &&
        _month === month;

    _loading = true;
    renderPage();
    try {
        const [overview, items] = await Promise.all([fetchOverview(year, month), fetchItems(year, month)]);
        if (!isCurrent()) return;
        _overview = overview;
        _items = items;
        ensureSelectedDate();
    } catch (error) {
        if (!isCurrent()) return;
        _overview = null;
        _items = [];
        showToast(`加载日记失败：${errorMessage(error)}`, 'error');
    } finally {
        if (!isCurrent()) return;
        _loading = false;
        renderPage();
    }
}

export function render(container) {
    if (!container || typeof container.querySelector !== 'function') {
        throw new TypeError('日记页需要有效的 DOM 挂载容器');
    }

    _unsubscribeDataChanges?.();
    _lifecycleVersion += 1;
    _loadVersion += 1;
    _container = container;
    _items = [];
    _overview = null;
    _loading = false;
    _selectedDate = '';
    const now = new Date();
    _year = now.getFullYear();
    _month = now.getMonth() + 1;
    ensureStyles();

    _unsubscribeDataChanges = subscribeDataChanges('diary', () => {
        void loadAndRender();
    });
    void loadAndRender();

    const lifecycleVersion = _lifecycleVersion;
    void loadMoodEmojis().then(() => {
        if (_container === container && _lifecycleVersion === lifecycleVersion) renderPage();
    });
}

export function destroy() {
    _lifecycleVersion += 1;
    _loadVersion += 1;
    _unsubscribeDataChanges?.();
    _unsubscribeDataChanges = null;
    _container = null;
    _items = [];
    _overview = null;
    _selectedDate = '';
    _loading = false;
    _formOpening = false;
}
