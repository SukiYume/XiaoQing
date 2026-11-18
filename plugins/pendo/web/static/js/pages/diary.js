import { api } from '../api.js';
import { showToast } from '../components/toast.js';
import { showModal, closeModal, showConfirmModal, safeHtml } from '../components/modal.js';
import { buildFormHTML, getFormData, initFormInteractions } from '../components/form.js';
import { captureFormDraft, reopenWithDraft } from '../components/form.js';
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
} from '../utils/format.js';
import {
    fetchUserTimeZone,
    todayInUserTimeZone,
    zonedDateKey,
    zonedDateParts,
    zonedDateTimeToInput,
    zonedInputToUtcIso,
} from '../utils/timezone.js';
import { createInstantMemory } from '../utils/timezone.js';
import { moodIcon, pixelIcon, weatherIcon } from '../utils/icons.js';
import { categoryColors } from '../utils/palette.js';
import { escapeHtml, subscribeDataChanges } from '../utils/ui.js';

const WEATHER_OPTIONS     = Object.freeze(['晴', '多云', '雨', '雪', '雾', '风']);
const MOOD_SWATCHES       = Object.freeze(categoryColors('pink'));
const DIARY_YEAR_START    = 2000;
const DIARY_YEAR_COUNT    = 100;
const DIARY_YEAR_END      = DIARY_YEAR_START + DIARY_YEAR_COUNT - 1;
const WEEK_NAMES          = Object.freeze(['周日', '周一', '周二', '周三', '周四', '周五', '周六']);
const CALENDAR_WEEKDAYS   = Object.freeze(['周一', '周二', '周三', '周四', '周五', '周六', '周日']);
const DIARY_NAV_PREV_ICON = `
    <svg viewBox="0 0 16 16" aria-hidden="true" focusable="false">
        <path d="M9.75 3.5 5.5 8l4.25 4.5" fill="none" stroke="currentColor" stroke-width="1.7" stroke-linecap="round" stroke-linejoin="round"/>
    </svg>`;
const DIARY_NAV_NEXT_ICON = `
    <svg viewBox="0 0 16 16" aria-hidden="true" focusable="false">
        <path d="M6.25 3.5 10.5 8l-4.25 4.5" fill="none" stroke="currentColor" stroke-width="1.7" stroke-linecap="round" stroke-linejoin="round"/>
    </svg>`;
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

let _container              = null;
let _items                  = [];
let _overview               = null;
let _year                   = 0;
let _month                  = 0;
let _selectedDate           = '';
let _loading                = false;
let _templates              = [];
let _templatesLoaded        = false;
let _unsubscribeDataChanges = null;
let _moodLabels             = { ...DEFAULT_MOOD_LABELS };
let _moodOptions            = [...DEFAULT_MOOD_OPTIONS];
let _loadVersion            = 0;
let _lifecycleVersion       = 0;
let _formOpening            = false;

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
    const selectedDateKey    = parsedSelectedDate ? isoDate(parsedSelectedDate) : '';
    const targetPrefix       = `${year}-${pad2(month)}`;
    const nextSelectedDate   = selectedDateKey.startsWith(targetPrefix) ? selectedDateKey : '';
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
    const year  = Number(matched[1]);
    const month = Number(matched[2]);
    if (!Number.isInteger(year) || !Number.isInteger(month)) return null;
    if (year < DIARY_YEAR_START || year > DIARY_YEAR_END) return null;
    if (month < 1 || month > 12) return null;
    return { year, month };
}

function diaryWordCount(item) {
    return Array.from(String(item?.content || '').trim()).length;
}

function moodLabelText(mood) {
    const normalized = String(mood || '').trim();
    const moodId     = normalized.toLowerCase();
    return Object.hasOwn(_moodLabels, moodId) ? _moodLabels[moodId] : normalized;
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
    return zonedDateParts(text);
}

function formatEntryTime(item, { timeOnly = false } = {}) {
    const raw = itemEntryTimestamp(item);
    const text = String(raw).trim();
    const timestamp = parseDiaryTimestamp(text);
    const explicitTime = /[T ]\d{2}:\d{2}/.test(text);
    const time = timestamp && explicitTime ? `${pad2(timestamp.hour)}:${pad2(timestamp.minute)}` : '';
    if (timeOnly) return time || '全天';

    const diaryDate = parseDate(item?.diary_date);
    const date      = diaryDate ? isoDate(diaryDate) : timestamp ? zonedDateKey(text) : '未知日期';
    return time ? `${date} ${time}` : date;
}

function defaultEntryTime(dateStr, userTimeZone) {
    const date     = parseDate(dateStr);
    const nowInput = zonedDateTimeToInput(new Date().toISOString(), userTimeZone);
    return `${date ? isoDate(date) : todayInUserTimeZone()}T${nowInput.slice(11, 16)}`;
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
    const dates       = new Set(
        _items
            .map((item) => item.diary_date)
            .filter((value) => typeof value === 'string' && value.startsWith(monthPrefix)),
    );
    const today          = todayInUserTimeZone();
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
    const overview   = isRecord(payload) ? payload : {};
    const summary    = isRecord(overview.summary) ? overview.summary : {};
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
            const id      = String(template.id || '').trim();
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
    const items    = [];
    let page       = 1;
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
        const data  = isRecord(res?.data) ? res.data : {};
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
        today: todayInUserTimeZone(),
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

async function loadMoodOptions() {
    try {
        const res           = await api.get('/config/diary/moods');
        const data          = isRecord(res?.data) ? res.data : {};
        const fetchedLabels = isRecord(data.mood_labels) ? data.mood_labels : {};
        const fetchedMoods  = records(data.moods);
        const validMoodId   = (value) => {
            const id = String(value || '')
                .trim()
                .toLowerCase();
            return /^[a-z0-9_-]+$/.test(id) && !['__proto__', 'constructor', 'prototype'].includes(id) ? id : '';
        };

        const labels = { ...DEFAULT_MOOD_LABELS };
        for (const [rawId, label] of Object.entries(fetchedLabels)) {
            const id = validMoodId(rawId);
            if (id) labels[id] = String(label || '').trim() || id;
        }

        const configuredOptions = fetchedMoods
            .map((item) => {
                const id = validMoodId(item.id);
                return id ? { value: id, label: String(item.label || labels[id] || id) } : null;
            })
            .filter((item) => item !== null);
        _moodLabels = labels;
        _moodOptions = configuredOptions.length
            ? configuredOptions
            : Object.keys(labels).map((id) => ({ value: id, label: labels[id] }));
        return;
    } catch {
        // 配置接口临时不可用时继续使用与后端一致的内置选项。
    }
    _moodLabels = { ...DEFAULT_MOOD_LABELS };
    _moodOptions = [...DEFAULT_MOOD_OPTIONS];
}

function buildCalendarDays(year, month) {
    const first  = new Date(year, month - 1, 1);
    const offset = (first.getDay() + 6) % 7;
    const start  = new Date(year, month - 1, 1 - offset);
    const today  = todayInUserTimeZone();
    const days   = [];
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

// ---------- 页面区域渲染 ----------

function renderHero() {
    const summary = _overview?.summary || {};
    const busiest = summary.busiest_day
        ? `${formatDateLabel(summary.busiest_day.date).split(' · ')[0]} · ${summary.busiest_day.words} 字`
        : '本月还没有高峰日';
    return `
        <section class="diary-hero pendo-page-header">
            <div>
                <span class="eyebrow"><i></i>JOURNAL</span>
                <h2>日记</h2>
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
    const summary  = _overview?.summary || {};
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
                            const list       = entries.get(day.key) || [];
                            const first      = list[0];
                            const totalWords = list.length
                                ? list.reduce((sum, item) => sum + diaryWordCount(item), 0)
                                : 0;
                            const metric = list.length > 1 ? `${list.length} 篇 ${totalWords} 字` : `${totalWords} 字`;
                            const meta = list.length > 1 ? '' : compactDiaryCellLabel(first);
                            const copy = meta ? `${metric} ${meta}` : metric;
                            const mood = first?.mood
                                ? moodIcon(first.mood, 'diary-day-mood')
                                : '';
                            const selected  = day.key === _selectedDate;
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
                                <span class="diary-mood-name"><span class="diary-mood-dot" style="background:${moodPalette(index)};"></span>${moodIcon(item.mood)}${escapeHtml(moodLabelText(item.mood))}</span>
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
    const cadence  = _overview?.cadence || [];
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
                            <span class="diary-cadence-bar" style="height:${(item.words / maxValue) * 76}px;"></span>
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
    const recent    = _overview?.recent_entries || [];
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
                                <span class="diary-recent-top"><strong>${item.mood ? moodIcon(item.mood) : pixelIcon('diary')}${escapeHtml(item.title || item.diary_date)}</strong><span>${escapeHtml(formatEntryTime(item, { timeOnly: true }))}</span></span>
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
                    <div class="diary-entry-title-row">${item.mood ? moodIcon(item.mood, 'diary-entry-mood') : pixelIcon('diary', 'diary-entry-mood')}<h4>${escapeHtml(item.title || '这一天的记录')}</h4></div>
                    <div class="diary-entry-meta">
                        <span>${escapeHtml(formatEntryTime(item, { timeOnly: true }))}</span>
                        <span>${weatherIcon(item.weather)}${escapeHtml(item.weather || '未记录天气')}</span>
                        ${item.location ? `<span>${pixelIcon('pin')}${escapeHtml(item.location)}</span>` : ''}
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
                    <div class="diary-empty-card writing-empty">
                        <img src="assets/diary-desk.webp" width="448" height="252" loading="lazy" alt="" />
                        <div class="writing-prompt"><strong>小青的手帐</strong><p>窗边的灯亮了。今天哪一幕，你想留给明天的自己？</p></div>
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
                                    <strong>${item.mood ? moodIcon(item.mood) : pixelIcon('diary')}${escapeHtml(item.title || previewText(item.content, 18) || '这一天')}</strong>
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
    const rows             = prompts.map((prompt) => {
        const answerIndex = remainingAnswers.findIndex((item) => item.prompt === prompt);
        const matched     = answerIndex >= 0 ? remainingAnswers.splice(answerIndex, 1)[0] : null;
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
    const moodScore = finiteNumber(item.mood_score);
    const bodyHTML  = `
        <div class="diary-view-meta">
            <span class="diary-view-chip">${pixelIcon('events')}${escapeHtml(item.diary_date)}</span>
            <span class="diary-view-chip">${pixelIcon('clock')}${escapeHtml(formatEntryTime(item, { timeOnly: true }))}</span>
            ${item.mood ? `<span class="diary-view-chip">${moodIcon(item.mood)}${escapeHtml(moodLabelText(item.mood))}</span>` : ''}
            ${moodScore >= 1 && moodScore <= 10 ? `<span class="diary-view-chip">心情 ${moodScore}/10</span>` : ''}
            ${item.weather ? `<span class="diary-view-chip">${weatherIcon(item.weather)}${escapeHtml(item.weather)}</span>` : ''}
            ${item.location ? `<span class="diary-view-chip">${pixelIcon('pin')}${escapeHtml(item.location)}</span>` : ''}
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
        closeModal(content);
        openDiaryFormModal(item);
    };
    content.querySelector('#diary-view-delete').onclick = async () => {
        closeModal(content);
        await deleteDiary(item, () => openDiaryViewModal(item));
    };
}

async function openDiaryFormModal(existing = null, presetDate = null) {
    if (_formOpening) return;
    _formOpening = true;
    try {
        const userTimeZone = await fetchUserTimeZone();
        const diary    = isRecord(existing) ? existing : null;
        const isEdit   = diary !== null;
        const formDate = diary?.diary_date || presetDate || _selectedDate || todayInUserTimeZone();
        const instants = createInstantMemory();
        const fields   = DIARY_FIELDS.map((field) => {
            let value = '';
            if (diary) value = diary[field.name] ?? '';
            else if (field.name === 'diary_date') value = formDate;
            else if (field.name === 'entry_time') value = defaultEntryTime(formDate, userTimeZone);
            if (field.name === 'entry_time') {
                const inputValue = diary ? instants.display(value, userTimeZone) : zonedDateTimeToInput(value, userTimeZone);
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
                const draft  = captureFormDraft(content.querySelector('#diary-form'));
                const reopen = () => reopenWithDraft(() => openDiaryFormModal(diary), draft, '#diary-form');
                closeModal(content);
                await deleteDiary(diary, reopen, reopen);
            };
        }

        const saveButton = content.querySelector('#diary-modal-save');
        saveButton.onclick = async () => {
            if (saveButton.disabled) return;
            const form                 = content.querySelector('#diary-form');
            const data                 = getFormData(form);
            const templateAnswerInputs = form.querySelectorAll('.diary-template-answer-input');
            const templateAnswers      = collectTemplateAnswers(form);
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
            const diaryDate       = parsedDiaryDate ? isoDate(parsedDiaryDate) : '';
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
            const shownKey = `${diaryDate}T${timeMatch[1]}${timeMatch[2] ? `:${timeMatch[2]}` : ''}`;
            try {
                // 记录时间与日期都没改时沿用原来的绝对时刻（保留秒数，避开回拨歧义）。
                data.entry_time = instants.resolve(shownKey, userTimeZone) || zonedInputToUtcIso(wallTime, userTimeZone);
            } catch (error) {
                showToast(errorMessage(error), 'warning');
                return;
            }
            saveButton.disabled = true;
            try {
                if (isEdit) {
                    const itemId = typeof diary.id === 'string' ? diary.id.trim() : '';
                    if (!itemId) throw new Error('日记缺少有效编号');
                    await api.put(`/items/${encodeURIComponent(itemId)}`, { ...data, version: diary.version });
                    showToast('日记已更新', 'success');
                } else {
                    await api.post('/items', { type: 'diary', ...data });
                    showToast('日记已添加', 'success');
                }
                closeModal(content);
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

async function deleteDiary(item, onCancel = null, onFailure = null) {
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
        if (typeof onFailure === 'function') await onFailure();
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
                void openDiaryFormModal(null, _selectedDate || todayInUserTimeZone());
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
    const loadVersion      = ++_loadVersion;
    const year             = _year;
    const month            = _month;
    const isCurrent        = () =>
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
    const now = parseDate(todayInUserTimeZone());
    _year = now.getFullYear();
    _month = now.getMonth() + 1;

    _unsubscribeDataChanges = subscribeDataChanges('diary', () => {
        void loadAndRender();
    });
    void loadAndRender();

    const lifecycleVersion = _lifecycleVersion;
    void loadMoodOptions().then(() => {
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
