import { api } from '../api.js';
import { showToast } from '../components/toast.js';
import { showModal, closeModal, showConfirmModal } from '../components/modal.js';
import { buildFormHTML, getFormData, initFormInteractions } from '../components/form.js';
import { renderCustomSelect, initCustomSelects } from '../components/custom_select.js';
import { isoDate, pad2, parseDate, previewText, todayStr as sharedTodayStr } from '../utils/format.js';
import { BREAKPOINTS, escapeHtml, injectStyles, mediaMax, pageShellCss } from '../utils/ui.js';

const CSS_ID = 'pendo-diary-redesign-styles';
const WEATHER_OPTIONS = ['☀️ 晴', '⛅ 多云', '🌧️ 雨', '❄️ 雪', '🌫️ 雾', '💨 风'];
const MOOD_SWATCHES = ['#EC4899', '#8B5CF6', '#3B82F6', '#F59E0B', '#10B981', '#F97316'];
const DIARY_YEAR_START = 2000;
const DIARY_YEAR_COUNT = 100;
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
let _dataChangedHandler = null;
let _moodEmojis = { ...DEFAULT_MOOD_EMOJIS };
let _moodLabels = { ...DEFAULT_MOOD_LABELS };
let _moodOptions = [...DEFAULT_MOOD_OPTIONS];

function todayDate() { return new Date(); }

function todayStr() {
    return sharedTodayStr();
}

function monthStart(year, month) { return `${year}-${pad2(month)}-01`; }

function monthEnd(year, month) {
    const lastDay = new Date(year, month, 0).getDate();
    return `${year}-${pad2(month)}-${pad2(lastDay)}`;
}

function monthInputValue(year, month) { return `${year}-${pad2(month)}`; }

function monthLabel(year, month) { return `${year}年${month}月`; }

async function updateMonth(year, month) {
    if (!Number.isInteger(year) || !Number.isInteger(month) || month < 1 || month > 12) {
        return false;
    }
    if (_year === year && _month === month) return true;
    _year = year;
    _month = month;
    _selectedDate = '';
    await loadAndRender();
    return true;
}

function parseMonthInput(value) {
    const matched = String(value || '').trim().match(/^(\d{4})-(\d{1,2})$/);
    if (!matched) return null;
    const year = Number(matched[1]);
    const month = Number(matched[2]);
    const maxYear = DIARY_YEAR_START + DIARY_YEAR_COUNT - 1;
    if (!Number.isInteger(year) || !Number.isInteger(month)) return null;
    if (year < DIARY_YEAR_START || year > maxYear) return null;
    if (month < 1 || month > 12) return null;
    return { year, month };
}

function diaryWordCount(item) {
    return String(item?.content || '').trim().length;
}

function moodEmoji(mood) {
    const normalized = String(mood || '').trim().toLowerCase();
    return normalized ? (_moodEmojis[normalized] || '') : '';
}

function moodBadgeText(mood) {
    const normalized = String(mood || '').trim();
    if (!normalized) return '';
    const emoji = moodEmoji(normalized);
    const label = _moodLabels[normalized.toLowerCase()] || normalized;
    return emoji ? `${emoji} ${label}` : label;
}

function itemEntryTimestamp(item) {
    return item?.entry_time || item?.created_at || item?.updated_at || (item?.diary_date ? `${item.diary_date}T00:00:00` : '');
}

function formatEntryTime(item, options = {}) {
    const raw = itemEntryTimestamp(item);
    if (!raw) return item?.diary_date || '';
    const text = String(raw).trim();
    const time = text.match(/T(\d{2}:\d{2})/)?.[1] || '';
    if (options.timeOnly) return time || '全天';
    const date = item?.diary_date || text.slice(0, 10);
    return time ? `${date} ${time}` : date;
}

function defaultEntryTime(dateStr) {
    const now = new Date();
    return `${dateStr || todayStr()}T${pad2(now.getHours())}:${pad2(now.getMinutes())}`;
}

function toDatetimeLocal(value) {
    const text = String(value || '').trim();
    if (!text) return '';
    const matched = text.match(/^(\d{4}-\d{2}-\d{2})[T ](\d{2}:\d{2})/);
    return matched ? `${matched[1]}T${matched[2]}` : text;
}

function compactDiaryCellLabel(entry, maxChars = 8) {
    const text = String(previewText(entry?.content, maxChars * 2) || entry?.title || '')
        .replace(/^[\s\u3000]+/, '')
        .replace(/\s+/g, ' ')
        .trim();
    if (!text) return '';
    const chars = Array.from(text);
    if (chars.length <= maxChars) return text;
    return `${chars.slice(0, maxChars).join('')}…`;
}

function formatDateLabel(value) {
    const date = parseDate(value);
    if (!date) return value || '未知日期';
    const weekNames = ['周日', '周一', '周二', '周三', '周四', '周五', '周六'];
    return `${date.getFullYear()}年${date.getMonth() + 1}月${date.getDate()}日 · ${weekNames[date.getDay()]}`;
}

function formatWordMetric(value) {
    const words = Number(value || 0);
    if (!words) return '0';
    if (words >= 1000) {
        const compact = words >= 10000 ? (words / 1000).toFixed(0) : (words / 1000).toFixed(1);
        return `${compact}k`;
    }
    return String(words);
}

function sortItems(items) {
    return [...items].sort((a, b) => {
        const dateA = `${a.diary_date || ''}|${itemEntryTimestamp(a)}`;
        const dateB = `${b.diary_date || ''}|${itemEntryTimestamp(b)}`;
        return dateB.localeCompare(dateA);
    });
}

function itemsByDate() {
    const map = new Map();
    sortItems(_items).forEach((item) => {
        const key = item.diary_date || '';
        if (!map.has(key)) map.set(key, []);
        map.get(key).push(item);
    });
    return map;
}

function dayEntries(dateStr) {
    return itemsByDate().get(dateStr) || [];
}

function ensureSelectedDate() {
    const dates = new Set(_items.map((item) => item.diary_date).filter(Boolean));
    const today = todayStr();
    const inCurrentMonth = `${_year}-${pad2(_month)}` === today.slice(0, 7);
    if (_selectedDate && _selectedDate.startsWith(`${_year}-${pad2(_month)}`)) return;
    if (inCurrentMonth) {
        _selectedDate = today;
        return;
    }
    const sortedDates = [...dates].sort((a, b) => b.localeCompare(a));
    _selectedDate = sortedDates[0] || monthStart(_year, _month);
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
        const batch = res?.data?.items || [];
        items.push(...batch);
        if (batch.length < pageSize) break;
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
    return res?.data || null;
}

async function loadTemplates() {
    if (_templatesLoaded) return;
    try {
        const res = await api.get('/config/diary/templates');
        _templates = res?.data?.templates || [];
    } catch (_) {
        _templates = [];
    }
    _templatesLoaded = true;
}

async function loadMoodEmojis() {
    try {
        const res = await api.get('/config/diary/moods');
        const fetched = res?.data?.mood_emojis;
        const fetchedLabels = res?.data?.mood_labels;
        const fetchedMoods = res?.data?.moods;
        if (fetched && typeof fetched === 'object') {
            _moodEmojis = { ...DEFAULT_MOOD_EMOJIS, ...fetched };
        }
        if (fetchedLabels && typeof fetchedLabels === 'object') {
            _moodLabels = { ...DEFAULT_MOOD_LABELS, ...fetchedLabels };
        }
        if (Array.isArray(fetchedMoods) && fetchedMoods.length) {
            _moodOptions = fetchedMoods.map((item) => ({
                value: item.id,
                label: item.label || item.id,
                emoji: item.emoji || _moodEmojis[item.id] || item.id,
            }));
            return;
        }
        if (fetched || fetchedLabels) {
            _moodOptions = Object.keys(_moodLabels).map((id) => ({
                value: id,
                label: _moodLabels[id],
                emoji: _moodEmojis[id] || id,
            }));
            return;
        }
    } catch (_) {
        // Fallback to defaults when config endpoint is unavailable.
    }
    _moodEmojis = { ...DEFAULT_MOOD_EMOJIS };
    _moodLabels = { ...DEFAULT_MOOD_LABELS };
    _moodOptions = [...DEFAULT_MOOD_OPTIONS];
}

function buildCalendarDays(year, month) {
    const first = new Date(year, month - 1, 1);
    const offset = (first.getDay() + 6) % 7;
    const start = new Date(year, month - 1, 1 - offset);
    const days = [];
    for (let index = 0; index < 42; index += 1) {
        const current = new Date(start);
        current.setDate(start.getDate() + index);
        days.push({
            date: current,
            key: isoDate(current),
            inMonth: current.getMonth() + 1 === month,
            isToday: isoDate(current) === todayStr(),
        });
    }
    return days;
}

function moodPalette(index) { return MOOD_SWATCHES[index % MOOD_SWATCHES.length]; }

function ensureStyles() {
    injectStyles(CSS_ID, `
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
        .diary-cadence { display: grid; grid-template-columns: repeat(16, minmax(0, 1fr)); gap: 8px; align-items: end; overflow: hidden; }
        .diary-cadence-col { display: flex; flex-direction: column; align-items: center; gap: 6px; }
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
        .diary-template-answer-stack { display: flex; flex-direction: column; gap: 10px; margin-top: 10px; }
        .diary-template-answer label { display: block; margin-bottom: 6px; font-size: 12px; font-weight: 800; color: var(--color-text-secondary); }
        .diary-template-answer textarea { min-height: 74px; }
        .diary-template-answer-view { display: flex; flex-direction: column; gap: 10px; margin-bottom: 14px; }
        .diary-template-answer-item { padding: 12px 14px; border-radius: 16px; background: rgba(253,242,248,0.72); border: 1px solid rgba(251,207,232,0.7); }
        .diary-template-answer-item strong { display: block; font-size: 12px; color: #be185d; margin-bottom: 6px; }
        .diary-template-answer-item p { margin: 0; white-space: pre-wrap; word-break: break-word; color: var(--color-text); line-height: 1.7; }
        .form-checkbox { display: inline-flex; align-items: center; gap: 8px; color: var(--color-text-secondary); font-size: 13px; font-weight: 700; }
        .form-checkbox input { width: 16px; height: 16px; accent-color: var(--color-diary, #EC4899); }
        ${mediaMax(BREAKPOINTS.XL, `
            .diary-summary-grid { grid-template-columns: repeat(2, minmax(0, 1fr)); }
            .diary-layout { grid-template-columns: 1fr; }
        `)}
        ${mediaMax(BREAKPOINTS.MOBILE, `
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
            .diary-cadence { grid-template-columns: repeat(10, minmax(0, 1fr)); }
            .diary-stream-item { flex-direction: column; }
            .diary-stream-date { width: auto; }
        `)}
        ${mediaMax(BREAKPOINTS.PHONE, `
            .diary-calendar-weekdays, .diary-calendar-grid { gap: 4px; }
            .diary-calendar-weekdays span { font-size: 10px; letter-spacing: 0; }
            .diary-day { min-height: 72px; padding: 6px; border-radius: 14px; gap: 1px; }
            .diary-day-top { min-height: 22px; }
            .diary-day-number { font-size: 15px; }
            .diary-day-body { gap: 2px; }
            .diary-day-mood { font-size: 15px; }
            .diary-day-copy { font-size: 10px; line-height: 1.35; }
        `)}
    `);
}

function renderHero() {
    const summary = _overview?.summary || {};
    const busiest = summary.busiest_day
        ? `${formatDateLabel(summary.busiest_day.date).split(' · ')[0]} · ${summary.busiest_day.words || 0} 字`
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
                <button class="btn btn-primary" id="diary-add-top">＋ 写日记</button>
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

function renderCalendarPanel() {
    const entries = itemsByDate();
    const weekdays = ['周一', '周二', '周三', '周四', '周五', '周六', '周日'];
    return `
        <section class="diary-panel">
            <div class="diary-panel-head">
                <div>
                    <h3>月历书写地图</h3>
                    <p>点击日期即可查看或继续书写当天内容。</p>
                </div>
                <div class="diary-month-nav">
                    <button type="button" id="diary-prev-month" aria-label="上个月">${DIARY_NAV_PREV_ICON}</button>
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
                    <button type="button" id="diary-next-month" aria-label="下个月">${DIARY_NAV_NEXT_ICON}</button>
                </div>
            </div>
            <div class="diary-panel-body">
                <div class="diary-calendar-weekdays">${weekdays.map((label) => `<span>${label}</span>`).join('')}</div>
                <div class="diary-calendar-grid">
                    ${buildCalendarDays(_year, _month).map((day) => {
                        const list = entries.get(day.key) || [];
                        const first = list[0];
                        const totalWords = list.length ? list.reduce((sum, item) => sum + diaryWordCount(item), 0) : 0;
                        const metric = list.length > 1 ? `${list.length} 篇 ${totalWords} 字` : `${totalWords}字`;
                        const meta = list.length > 1 ? '' : compactDiaryCellLabel(first);
                        const copy = meta ? `${metric} ${meta}` : metric;
                        const mood = first?.mood ? `<span class="diary-day-mood">${escapeHtml(moodEmoji(first.mood) || first.mood)}</span>` : '';
                        return `
                            <button
                                type="button"
                                class="diary-day${day.inMonth ? '' : ' is-outside'}${day.key === _selectedDate ? ' is-selected' : ''}${day.isToday ? ' is-today' : ''}"
                                data-date="${day.key}"
                            >
                                <span class="diary-day-top">
                                    <span class="diary-day-number">${day.date.getDate()}</span>
                                </span>
                                ${day.inMonth && list.length ? `
                                    <span class="diary-day-body">
                                        ${mood}
                                        <span class="diary-day-copy">${escapeHtml(copy)}</span>
                                    </span>` : ''}
                            </button>`;
                    }).join('')}
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
                    ${moods.map((item, index) => `
                        <div class="diary-mood-row">
                            <div class="diary-mood-top">
                                <span class="diary-mood-name"><span class="diary-mood-dot" style="background:${moodPalette(index)};"></span>${escapeHtml(moodBadgeText(item.mood) || item.mood)}</span>
                                <span class="diary-mood-count">${item.count} 条</span>
                            </div>
                            <div class="diary-mood-track"><span class="diary-mood-fill" style="width:${Math.max(10, Math.round(item.share * 100))}%;background:${moodPalette(index)};"></span></div>
                        </div>`).join('')}
                </div>
            </div>
        </section>
    `;
}

function renderCadencePanel() {
    const cadence = _overview?.cadence || [];
    const maxValue = Math.max(1, ...cadence.map((item) => item.words || 0));
    return `
        <section class="diary-panel">
            <div class="diary-panel-head"><div><h3>书写密度</h3><p>查看这个月每天写了多少字。</p></div></div>
            <div class="diary-panel-body">
                <div class="diary-cadence">
                    ${cadence.map((item) => `
                        <div class="diary-cadence-col" title="${item.date} · ${item.words} 字">
                            <span class="diary-cadence-value">${formatWordMetric(item.words)}</span>
                            <span class="diary-cadence-bar" style="height:${12 + (item.words / maxValue) * 64}px;"></span>
                            <span class="diary-cadence-label">${item.label}</span>
                        </div>`).join('')}
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
                ${recent.length ? `
                    <div class="diary-recent-list">
                        ${recent.slice(0, 4).map((item) => `
                            <button type="button" class="diary-recent-item" data-id="${escapeHtml(String(item.id))}" data-date="${escapeHtml(String(item.diary_date || ''))}">
                                <span class="diary-recent-top"><strong>${escapeHtml(moodEmoji(item.mood) || '📖')} ${escapeHtml(item.title || item.diary_date)}</strong><span>${escapeHtml(item.entry_label || formatEntryTime(item))}</span></span>
                                <span class="diary-recent-preview">${escapeHtml(item.content_preview || '点击查看详情')}</span>
                            </button>`).join('')}
                    </div>` : '<div class="diary-empty-card">这个月还没有可以回看的日记。</div>'}
                ${templates.length ? `
                    <div class="diary-template-usage">
                        <div class="diary-mini-label">模板使用</div>
                        <div class="diary-template-chips">
                            ${templates.slice(0, 4).map((item) => `<span class="diary-template-chip">${escapeHtml(item.template_id)} · ${item.count}</span>`).join('')}
                        </div>
                    </div>` : ''}
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

function renderSelectedDay() {
    const entries = dayEntries(_selectedDate);
    return `
        <section class="diary-workspace">
            <div class="diary-workspace-head">
                <div>
                    <h3 class="diary-workspace-title">${formatDateLabel(_selectedDate)}</h3>
                    <p class="diary-workspace-subtitle">${entries.length ? '查看、编辑或删除当天的日记内容。' : '这一天还没有日记，可以直接开始记录。'}</p>
                </div>
                <button class="btn btn-secondary" id="diary-add-selected" type="button">${entries.length ? '继续写一篇' : '为这一天写日记'}</button>
            </div>
            <div class="diary-workspace-body">
                ${entries.length ? `<div class="diary-entry-stack">${entries.map(renderEntryCard).join('')}</div>` : `
                    <div class="diary-empty-card diary-empty-large">
                        <div class="diary-empty-icon">🌙</div>
                        <div>这一天还没有留下文字。点右上角按钮，直接把片刻写下来。</div>
                    </div>`}
            </div>
        </section>
    `;
}

function renderMonthStream() {
    return `
        <section class="diary-panel">
            <div class="diary-panel-head"><div><h3>本月回看</h3><p>按日期倒序浏览这个月的记录。</p></div></div>
            <div class="diary-panel-body">
                ${_items.length ? `
                    <div class="diary-stream-list">
                        ${_items.map((item) => `
                            <button type="button" class="diary-stream-item" data-id="${escapeHtml(String(item.id || ''))}">
                                <span class="diary-stream-date">${escapeHtml(formatEntryTime(item))}</span>
                                <span class="diary-stream-main">
                                    <strong>${escapeHtml(moodEmoji(item.mood) || '📖')} ${escapeHtml(item.title || previewText(item.content, 18) || '这一天')}</strong>
                                    <span>${escapeHtml(previewText(item.content, 70) || '点击查看详情')}</span>
                                </span>
                                <span class="diary-stream-words">${diaryWordCount(item)} 字</span>
                            </button>`).join('')}
                    </div>` : '<div class="diary-empty-card">本月还没有任何日记记录。</div>'}
            </div>
        </section>
    `;
}

function renderPage() {
    if (!_container) return;
    ensureStyles();
    if (_loading && !_overview) {
        _container.innerHTML = '<div class="diary-shell"><div class="diary-empty-card diary-empty-large">正在加载日记空间...</div></div>';
        return;
    }
    _container.innerHTML = `
        <div class="diary-shell">
            ${renderHero()}
            <div class="diary-stack">
                ${renderSummaryCards()}
                <section class="diary-layout">
                    ${renderCalendarPanel()}
                    <div class="diary-side-stack">
                        ${renderMoodPanel()}
                        ${renderCadencePanel()}
                        ${renderRecentPanel()}
                    </div>
                </section>
                ${renderSelectedDay()}
                ${renderMonthStream()}
            </div>
        </div>`;
    attachListeners();
}

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
            ${normalized.map((item) => `
                <div class="diary-template-answer-item">
                    ${item.prompt ? `<strong>${escapeHtml(item.prompt)}</strong>` : ''}
                    ${item.answer ? `<p>${escapeHtml(item.answer)}</p>` : ''}
                </div>`).join('')}
        </div>`;
}

function templateAnswerInputRows(template, answers = []) {
    const normalizedAnswers = normalizeTemplateAnswers(answers);
    const prompts = Array.isArray(template?.prompts) ? template.prompts.map((prompt) => String(prompt || '').trim()).filter(Boolean) : [];
    normalizedAnswers.forEach((item) => {
        if (item.prompt && !prompts.includes(item.prompt)) prompts.push(item.prompt);
    });
    const answerByPrompt = new Map(normalizedAnswers.map((item) => [item.prompt, item.answer]));
    return prompts.map((prompt, index) => ({
        prompt,
        answer: answerByPrompt.get(prompt) || '',
        label: prompt || `问题 ${index + 1}`,
    }));
}

function renderTemplateAnswerInputs(template, answers = []) {
    const rows = templateAnswerInputRows(template, answers);
    if (!rows.length) return '';
    return `
        <div class="diary-template-answer-stack">
            ${rows.map((row, index) => `
                <div class="diary-template-answer">
                    <label for="diary-template-answer-${index}">${escapeHtml(row.label)}</label>
                    <textarea
                        id="diary-template-answer-${index}"
                        class="form-input diary-template-answer-input"
                        data-prompt="${escapeHtml(row.prompt)}"
                        rows="3"
                    >${escapeHtml(row.answer)}</textarea>
                </div>`).join('')}
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
        .map((item) => `${item.prompt}\n${item.answer}`)
        .join('\n\n');
}

export function openDiaryViewModal(item) {
    ensureStyles();
    const bodyHTML = `
        <div class="diary-view-meta">
            <span class="diary-view-chip">📅 ${escapeHtml(item.diary_date)}</span>
            <span class="diary-view-chip">🕒 ${escapeHtml(formatEntryTime(item, { timeOnly: true }))}</span>
            ${item.mood ? `<span class="diary-view-chip">${escapeHtml(moodBadgeText(item.mood))}</span>` : ''}
            ${item.mood_score ? `<span class="diary-view-chip">心情 ${escapeHtml(String(item.mood_score))}/10</span>` : ''}
            ${item.weather ? `<span class="diary-view-chip">${escapeHtml(item.weather)}</span>` : ''}
            ${item.location ? `<span class="diary-view-chip">📍 ${escapeHtml(item.location)}</span>` : ''}
            ${item.is_favorite ? '<span class="diary-view-chip">收藏</span>' : ''}
            <span class="diary-view-chip">${diaryWordCount(item)} 字</span>
        </div>
        ${renderTemplateAnswers(item.template_answers)}
        <div class="diary-view-content">${escapeHtml(item.content || '')}</div>
    `;
    const footer = `
        <button class="btn btn-danger btn-sm" id="diary-view-delete" style="margin-right:auto;">删除</button>
        <button class="btn btn-secondary" id="diary-view-close">关闭</button>
        <button class="btn btn-primary" id="diary-view-edit">编辑</button>
    `;
    const content = showModal(item.title || formatDateLabel(item.diary_date), bodyHTML, { footer });
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

export async function openDiaryFormModal(existing = null, presetDate = null) {
    ensureStyles();
    const isEdit = Boolean(existing);
    const formDate = existing?.diary_date || presetDate || _selectedDate || todayStr();
    const fields = DIARY_FIELDS.map((field) => {
        let value = '';
        if (existing) value = existing[field.name] ?? '';
        else if (field.name === 'diary_date') value = formDate;
        else if (field.name === 'entry_time') value = defaultEntryTime(formDate);
        if (field.name === 'entry_time') value = toDatetimeLocal(value);
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
                    options: [{ value: '', label: '-- 不使用模板 --' }, ..._templates.map((tpl) => ({ value: tpl.id, label: tpl.name }))],
                    selected: '',
                    className: 'pselect-form pselect-block pselect-theme-diary diary-template-select',
                })}
                <div id="diary-template-hint" class="diary-template-hint" style="display:none;"></div>
            </div>`;
    } else if (isEdit) {
        const existingAnswers = normalizeTemplateAnswers(existing?.template_answers);
        const template = _templates.find((tpl) => String(tpl.id) === String(existing?.template_id));
        const answerInputs = renderTemplateAnswerInputs(template, existingAnswers);
        if (answerInputs) {
            templateSectionHTML = `
                <div class="form-group">
                    <label class="form-label">模板回答</label>
                    <div class="diary-template-hint" style="display:block;">${answerInputs}</div>
                </div>`;
        }
    }

    const content = showModal(isEdit ? '编辑日记' : '写日记', `<form id="diary-form">${templateSectionHTML}${buildFormHTML(fields)}</form>`, {
        footer: `
            ${isEdit ? '<button class="btn btn-danger btn-sm" id="diary-modal-delete" style="margin-right:auto;">删除</button>' : ''}
            <button class="btn btn-secondary" id="diary-modal-cancel">取消</button>
            <button class="btn btn-primary" id="diary-modal-save">保存</button>
        `,
    });

    initFormInteractions(content);
    content.querySelector('#diary-modal-cancel').onclick = closeModal;

    if (!isEdit && _templates.length) {
        const templateHint = content.querySelector('#diary-template-hint');
        initCustomSelects(content, {
            'diary-template-sel': (value) => {
                const template = _templates.find((tpl) => String(tpl.id) === String(value));
                if (!template || !template.prompts?.length) {
                    templateHint.innerHTML = '';
                    templateHint.style.display = 'none';
                    return;
                }
                templateHint.innerHTML = renderTemplateAnswerInputs(template);
                templateHint.style.display = 'block';
            },
        });
    }

    if (isEdit) {
        content.querySelector('#diary-modal-delete').onclick = async () => {
            closeModal();
            await deleteDiary(existing, () => openDiaryFormModal(existing));
        };
    }

    content.querySelector('#diary-modal-save').onclick = async () => {
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
        if (data.diary_date) {
            const currentTime = String(data.entry_time || '').match(/T(\d{2}:\d{2})/)?.[1];
            data.entry_time = currentTime ? `${data.diary_date}T${currentTime}` : defaultEntryTime(data.diary_date);
        }
        if (!data.diary_date) {
            showToast('请选择日期', 'warning');
            return;
        }
        if (!String(data.content || '').trim()) {
            showToast('请填写日记内容', 'warning');
            return;
        }
        try {
            if (isEdit) {
                await api.put(`/items/${existing.id}`, data);
                showToast('日记已更新', 'success');
            } else {
                await api.post('/items', { type: 'diary', ...data });
                showToast('日记已添加', 'success');
            }
            closeModal();
            _selectedDate = data.diary_date;
            window.dispatchEvent(new CustomEvent('pendo-data-changed', { detail: { type: 'diary' } }));
            await loadAndRender();
        } catch (err) {
            showToast(`保存失败：${err.message}`, 'error');
        }
    };
}

async function deleteDiary(item, onCancel = null) {
    const confirmed = await showConfirmModal({
        title: '删除日记',
        message: `确定要删除“${item.title || formatDateLabel(item.diary_date) || '这篇日记'}”吗？删除后内容将无法恢复。`,
        confirmText: '删除',
        cancelText: '取消',
        tone: 'danger',
    });
    if (!confirmed) {
        if (onCancel) onCancel();
        return;
    }
    try {
        await api.delete(`/items/${item.id}`);
        showToast('日记已删除', 'success');
        window.dispatchEvent(new CustomEvent('pendo-data-changed', { detail: { type: 'diary' } }));
        await loadAndRender();
    } catch (err) {
        showToast(`删除失败：${err.message}`, 'error');
    }
}

function attachListeners() {
    if (!_container) return;

    const addTop = _container.querySelector('#diary-add-top');
    if (addTop) {
        addTop.onclick = () => {
            openDiaryFormModal(null, _selectedDate || todayStr());
        };
    }

    const addSelected = _container.querySelector('#diary-add-selected');
    if (addSelected) {
        addSelected.onclick = () => {
            openDiaryFormModal(null, _selectedDate || todayStr());
        };
    }

    const prevBtn = _container.querySelector('#diary-prev-month');
    if (prevBtn) {
        prevBtn.onclick = async () => {
            let nextYear = _year;
            let nextMonth = _month - 1;
            if (nextMonth < 1) { nextMonth = 12; nextYear -= 1; }
            await updateMonth(nextYear, nextMonth);
        };
    }

    const nextBtn = _container.querySelector('#diary-next-month');
    if (nextBtn) {
        nextBtn.onclick = async () => {
            let nextYear = _year;
            let nextMonth = _month + 1;
            if (nextMonth > 12) { nextMonth = 1; nextYear += 1; }
            await updateMonth(nextYear, nextMonth);
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
            if (event.key !== 'Enter') return;
            event.preventDefault();
            await commitMonthText();
        });
        monthText.addEventListener('blur', () => {
            void commitMonthText();
        });
    }

    _container.querySelectorAll('.diary-day[data-date]').forEach((button) => {
        button.onclick = () => {
            _selectedDate = button.dataset.date || _selectedDate;
            renderPage();
        };
    });

    _container.querySelectorAll('.diary-recent-item').forEach((button) => {
        button.onclick = () => {
            const item = _items.find((entry) => String(entry.id) === String(button.dataset.id));
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
            const item = _items.find((entry) => String(entry.id) === String(button.dataset.id));
            if (!item) return;
            _selectedDate = item.diary_date || _selectedDate;
            openDiaryViewModal(item);
        };
    });

    _container.querySelectorAll('[data-action][data-id]').forEach((button) => {
        button.onclick = async (event) => {
            event.stopPropagation();
            const item = _items.find((entry) => String(entry.id) === String(button.dataset.id));
            if (!item) return;
            const action = button.dataset.action;
            if (action === 'view') openDiaryViewModal(item);
            if (action === 'edit') openDiaryFormModal(item);
            if (action === 'delete') await deleteDiary(item);
        };
    });
}

async function loadAndRender() {
    _loading = true;
    renderPage();
    try {
        const [overview, items] = await Promise.all([
            fetchOverview(_year, _month),
            fetchItems(_year, _month),
        ]);
        _overview = overview;
        _items = items;
        ensureSelectedDate();
    } catch (err) {
        _overview = null;
        _items = [];
        showToast(`加载日记失败：${err.message}`, 'error');
    } finally {
        _loading = false;
    }
    renderPage();
}

export function render(container) {
    _container = container;
    _items = [];
    _overview = null;
    _loading = false;
    _selectedDate = '';
    const now = todayDate();
    _year = now.getFullYear();
    _month = now.getMonth() + 1;
    renderPage();
    loadMoodEmojis().finally(() => {
        loadAndRender();
    });
    _dataChangedHandler = async (event) => {
        const changedType = event?.detail?.type;
        if (changedType && changedType !== 'diary') return;
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
    _items = [];
    _overview = null;
    _selectedDate = '';
}

export function onRouteEnter(_params) {}
