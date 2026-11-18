import { api } from '../api.js';
import { showToast } from '../components/toast.js';
import { showModal, closeModal, showConfirmModal, safeHtml } from '../components/modal.js';
import { renderCustomSelect, initCustomSelects } from '../components/custom_select.js';
import { initFormInteractions } from '../components/form.js';
import { derivePresetRange, fetchItemRangeBounds, RANGE_PRESET_OPTIONS, todayRangeKey } from '../utils/date_ranges.js';
import { isoDate, isValidDateInput, pad2, parseDate } from '../utils/format.js';
import {
    fetchUserTimeZone,
    formatZonedDateTime,
    zonedDateTimeToInput,
    zonedInputToUtcIso,
    zonedInstantEpoch,
} from '../utils/timezone.js';
import { createInstantMemory } from '../utils/timezone.js';
import {
    bindEnterAction,
    bindFormSubmit,
    escapeHtml,
    subscribeDataChanges,
} from '../utils/ui.js';

const CALENDAR_VISIBLE_ITEM_LIMIT = 3;
const WEEKDAYS                    = Object.freeze(['周一', '周二', '周三', '周四', '周五', '周六', '周日']);
const EVENT_KINDS                 = new Set(['single', 'multi_node', 'recurring']);
const REMINDER_STATUSES           = new Set(['pending', 'sent', 'confirmed']);
const EVENT_FILTER_KINDS          = new Set(['', 'all', ...EVENT_KINDS]);
const REMINDER_FILTERS            = new Set(['', 'all', 'with', 'none', ...REMINDER_STATUSES]);

let _container              = null;
let _unsubscribeDataChanges = null;
let _loadVersion            = 0;
let _state                  = {
    viewMode: 'calendar',
    monthCursor: firstDayOfMonth(parseDate('1970-01-01')),
    selectedDate: '',
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
    const remindAt = zonedInstantEpoch(value);
    return Number.isFinite(remindAt) && remindAt > nowMs;
}

function watchReminderExpiry(content) {
    let timerId   = null;
    const refresh = () => {
        timerId = null;
        const nowMs       = Date.now();
        let nearestFuture = Number.POSITIVE_INFINITY;
        const buttons     = content?.querySelectorAll?.('[data-toggle-reminder]') ?? [];
        for (const button of buttons) {
            const remindAt = zonedInstantEpoch(button.dataset.reminderTime);
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

function reminderStatusLabel(status, missed = false) {
    if (missed) return '已过期';
    if (status === 'confirmed') return '已确认';
    if (status === 'sent') return '已发送';
    return '待发送';
}

function reminderMetaLabel(status, repeatCount, wasSent = false, missed = false) {
    if (missed) return '提醒时间已过，未发送';
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
    return hasInvalidDatePrefix(value) ? '未知时间' : formatZonedDateTime(value);
}

// 当前编辑器的原始时刻记忆：未改动的开始、结束和提醒时间原样提交，不做分钟级往返转换。
let _editorInstants = createInstantMemory();

/** 把接口日期转成用户设置时区的 YYYY-MM-DDTHH:mm，非法值不进入表单。 */
function toInputDateTime(value, userTimeZone) {
    if (hasInvalidDatePrefix(value)) return '';
    return _editorInstants.display(value, userTimeZone);
}

/** 未改动的字段沿用原时刻；改动过的字段严格把用户时区墙钟转换为唯一 UTC 时刻。 */
function inputToIso(value, userTimeZone) {
    return _editorInstants.resolve(value, userTimeZone);
}

function reminderRulesFromTimes(startTime, remindTimes) {
    const start = zonedInstantEpoch(startTime);
    if (!Number.isFinite(start)) return [];
    const offsets = new Set();
    for (const value of Array.isArray(remindTimes) ? remindTimes : []) {
        const remind = zonedInstantEpoch(value);
        if (!Number.isFinite(remind)) continue;
        const offset = Math.round((start - remind) / 1000);
        if (offset >= 0) offsets.add(offset);
    }
    return [...offsets].sort((a, b) => b - a).map((offset) => ({ offset_seconds: offset }));
}

/** 接口边界只保留渲染所需结构，异常字段统一收敛为空值或非负整数。 */
function normalizeOverview(value) {
    const raw        = value && typeof value === 'object' ? value : {};
    const summary    = raw.summary && typeof raw.summary === 'object' ? raw.summary : {};
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

function renderHero() {
    const summary = _state.overview?.summary || {};
    const range   = currentRange();
    return `
        <section class="events-hero pendo-page-header">
            <div>
                <span class="eyebrow"><i></i>SCHEDULE</span>
                <h2>日程</h2>
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
    const filters             = _state.filters;
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
    const outside   = day.getMonth() !== currentMonth;
    const selected  = _state.selectedDate === dayString;
    const classes   = ['events-calendar-cell'];
    if (outside) classes.push('outside');
    if (selected) classes.push('selected');
    if (today === dayString) classes.push('today');

    const items = Array.isArray(calendarDay?.items)
        ? calendarDay.items.filter((item) => item && typeof item === 'object')
        : [];
    const count          = Math.max(finiteCount(calendarDay?.count), items.length);
    const visibleItems   = items.slice(0, CALENDAR_VISIBLE_ITEM_LIMIT);
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
                        const label   = String(item.label ?? '(无标题)');
                        return `<button type="button" class="events-calendar-chip ${normalizedKind(item.kind)}" data-event-id="${escapeHtml(eventId)}" title="${escapeHtml(label)}" aria-label="查看日程：${escapeHtml(label)}" ${eventId ? '' : 'disabled'}><span class="events-calendar-chip-text">${escapeHtml(label)}</span></button>`;
                    })
                    .join('')}
                ${count > visibleItems.length ? `<div class="events-calendar-overflow">+${count - visibleItems.length}<span class="events-calendar-overflow-suffix"> 更多</span></div>` : ''}
            </div>
        </div>`;
}

function renderCalendarPanel() {
    const overview        = _state.overview;
    const calendarDays    = overview?.calendar_days || {};
    const start           = firstDayOfMonth(_state.monthCursor);
    const daysInThisMonth = lastDayOfMonth(_state.monthCursor).getDate();
    const offset          = weekdayIndexMonday(start);
    const totalCells      = Math.ceil((offset + daysInThisMonth) / 7) * 7;
    const cells           = [];
    const currentMonth    = _state.monthCursor.getMonth();
    const today           = todayRangeKey();

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

function renderTimelineEntries(items, events = new Map((_state.overview?.events || []).map((event) => [String(event.id ?? ''), event]))) {
    if (!Array.isArray(items) || !items.length) return '';

    return items
        .filter((item) => item && typeof item === 'object')
        .map((item) => {
            const eventId = String(item.event_id ?? '');
            const event = events.get(eventId);
            const itemKind = normalizedKind(item.kind);
            const dotKind = itemKind === 'multi_node' ? 'multi-node' : itemKind === 'recurring' ? 'recurring' : '';
            const collectionTitle = item.collection?.title || event?.collection?.title || '';
            // 重复日程的实例与集合同名，只写一次。
            const nodeTitle = item.title || event?.title || '(无标题)';
            const title     = collectionTitle && collectionTitle !== nodeTitle
                ? `${collectionTitle} · ${nodeTitle}`
                : nodeTitle;
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
                            ${item.location ? `<span class="events-pill"><span class="pixel-icon" data-icon="pin" aria-hidden="true"></span>${escapeHtml(item.location)}</span>` : ''}
                            ${reminderTotal ? `<span class="events-pill"><span class="pixel-icon" data-icon="bell" aria-hidden="true"></span>${reminderTotal} 个提醒</span>` : ''}
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
    const events = new Map(
        (_state.overview?.events || [])
            .filter((event) => event && typeof event === 'object')
            .map((event) => [String(event.id ?? ''), event]),
    );

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
                            ${renderTimelineEntries(row.items, events)}
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
            _state.listRange === 'all'
        ) {
            const today  = todayRangeKey();
            const bounds = await fetchItemRangeBounds(api, {
                type: 'event',
                filters: { category: _state.filters.category, keyword: _state.filters.keyword },
                fallbackEnd: today,
            });
            if (_container !== container || requestVersion !== _loadVersion) return;
            _state.allRangeStart = bounds.start;
            _state.allRangeEnd = bounds.end;
        }
        const range = currentRange();
        const res   = await api.get('/events/overview', {
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
    const startValue  = existing?.start_time
        ? toInputDateTime(existing.start_time, userTimeZone)
        : defaultDate
          ? `${defaultDate}T09:00`
          : '';
    const endValue  = existing?.end_time ? toInputDateTime(existing.end_time, userTimeZone) : '';
    const reminders = (Array.isArray(existing?.reminders) ? existing.reminders : [])
        .map((row) => toInputDateTime(row?.time, userTimeZone))
        .filter(Boolean);
    const initialReminderValues = reminders.length ? reminders : existing ? [] : [startValue];
    const initialNodes          = [
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
                    <textarea id="events-editor-notes" name="notes" data-text-limit="50000" placeholder="可选，用来记录背景、材料链接或执行说明">${escapeHtml(existing?.notes || '')}</textarea>
                    <small class="form-text-count" aria-live="polite"></small>
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
    const mode           = form.dataset.mode === 'multi_node' ? 'multi_node' : 'single';
    const title          = form.querySelector('[name="title"]')?.value.trim() || '';
    const category       = form.querySelector('[name="category"]')?.value.trim() || '未分类';
    const location       = form.querySelector('[name="location"]')?.value.trim() || '';
    const notes          = form.querySelector('[name="notes"]')?.value.trim() || '';
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
            const name      = row.querySelector('.events-editor-node-name')?.value.trim() || '';
            const rawTime   = row.querySelector('.events-editor-node-time')?.value.trim() || '';
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
    _editorInstants = createInstantMemory();
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
    const title = existing ? '编辑日程' : '新建日程';
    const content = showModal(title, safeHtml(editorModalHTML(existing, prefillDate, userTimeZone)), {
        footer: safeHtml(`
            <button type="button" class="btn btn-secondary" id="events-editor-cancel">取消</button>
            <button type="button" class="btn btn-primary" id="events-editor-save">保存</button>
        `),
    });

    initFormInteractions(content);
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
                await api.put(`/items/${encodeURIComponent(eventId)}`, { ...payload, version: existing.version });
                showToast('日程已更新', 'success');
            } else if (payload.children?.length) {
                await api.post('/events/collections', payload);
                showToast('多节点日程已创建', 'success');
            } else {
                await api.post('/items', { type: 'event', ...payload });
                showToast('日程已创建', 'success');
            }
            closeModal(content);
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
                <textarea id="events-collection-notes" name="notes" data-text-limit="50000">${escapeHtml(collection.notes || '')}</textarea>
                <small class="form-text-count" aria-live="polite"></small>
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
    initFormInteractions(content);
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
            version: Number(collection.version),
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
            closeModal(content);
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
                    ${collection.location ? `<span class="events-pill"><span class="pixel-icon" data-icon="pin" aria-hidden="true"></span>${escapeHtml(collection.location)}</span>` : ''}
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
        const content   = showModal('多节点日程', safeHtml(renderCollectionDetailBody(detail)), {
            footer: safeHtml(`
                <button type="button" class="btn btn-secondary" id="events-collection-close">关闭</button>
                <button type="button" class="btn btn-primary" id="events-collection-edit" ${canMutate ? '' : 'disabled'}>编辑整体</button>
                <button type="button" class="btn btn-danger" id="events-collection-delete" ${canMutate ? '' : 'disabled'}>删除整体</button>
            `),
        });
        content.querySelector('#events-collection-close').onclick = closeModal;
        content.querySelector('#events-collection-edit').onclick = () => {
            closeModal(content);
            openCollectionEditor(collection);
        };
        content.querySelector('#events-collection-delete').onclick = async () => {
            closeModal(content);
            try {
                await deleteCollection(collection.id, collection.title || '这个日程集合');
            } catch (err) {
                showToast(`删除失败：${err?.message || '未知错误'}`, 'error');
            }
        };
        content.addEventListener('click', async (event) => {
            const trigger = event.target.closest('[data-open-detail]');
            if (trigger?.dataset.openDetail) {
                closeModal(content);
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
                    ${event.location ? `<span class="events-pill"><span class="pixel-icon" data-icon="pin" aria-hidden="true"></span>${escapeHtml(event.location)}</span>` : ''}
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
                                const status      = normalizedReminderStatus(row.status);
                                const repeatCount = finiteCount(row.repeat_count);
                                const canToggle   = canToggleReminder(row.time);
                                const isConfirmed = status === 'confirmed';
                                return `
                            <div class="events-detail-row">
                                <div>
                                    <div class="events-detail-row-title">${escapeHtml(formatEventDateTime(row.time))}</div>
                                    <div class="events-detail-row-meta" data-reminder-meta>${escapeHtml(reminderMetaLabel(status, repeatCount, Boolean(row.sent_at), row.missed === true))}</div>
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
                                    <span class="events-status ${status}" data-reminder-status>${reminderStatusLabel(status, row.missed === true)}</span>
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
    const normalizedId = String(eventId ?? '');
    if (!normalizedId) {
        showToast('无法加载缺少编号的日程', 'error');
        return;
    }
    try {
        const res    = await api.get(`/events/${encodeURIComponent(normalizedId)}/detail`);
        const detail = res?.data && typeof res.data === 'object' ? res.data : {};
        const event  = detail.event;
        if (!event || typeof event !== 'object') throw new Error('日程数据不完整');
        const actionNoun              = detailActionNoun(event);
        const canMutate               = Boolean(String(event.id ?? ''));
        const hasCollection           = Boolean(String(event.collection?.id ?? ''));
        let stopReminderExpiryWatcher = () => {};
        const content                 = showModal('日程详情', safeHtml(renderDetailBody(detail)), {
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
                closeModal(content);
                await openCollectionDetail(event.collection.id);
            };
        }
        content.querySelector('#events-detail-edit').onclick = () => {
            closeModal(content);
            openEventEditor(event, _state.selectedDate);
        };
        content.querySelector('#events-detail-delete').onclick = async () => {
            closeModal(content);
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
                    const reminder      = response?.data?.reminder;
                    const status        = normalizedReminderStatus(reminder?.status);
                    const repeatCount   = finiteCount(reminder?.repeat_count);
                    const row           = reminderButton.closest('.events-detail-row');
                    const statusElement = row?.querySelector('[data-reminder-status]');
                    const metaElement   = row?.querySelector('[data-reminder-meta]');

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
                closeModal(content);
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

    const customStart      = root.querySelector('#events-custom-start');
    const customEnd        = root.querySelector('#events-custom-end');
    const customApply      = root.querySelector('#events-custom-apply');
    const applyCustomRange = async () => {
        const nextStart = customStart?.value.trim() || '';
        const nextEnd   = customEnd?.value.trim() || '';
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
    const todayKey = todayRangeKey();
    const today    = parseDate(todayKey);
    _state = {
        viewMode: 'calendar',
        monthCursor: firstDayOfMonth(today),
        selectedDate: todayKey,
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
