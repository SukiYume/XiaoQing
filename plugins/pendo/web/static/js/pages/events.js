import { api } from '../api.js';
import { showToast } from '../components/toast.js';
import { showModal, closeModal } from '../components/modal.js';
import { buildFormHTML, getFormData, initFormInteractions } from '../components/form.js';
import { navigate } from '../router.js';

// ── constants ─────────────────────────────────────────────────────────────────

const DAY_NAMES = ['周一', '周二', '周三', '周四', '周五', '周六', '周日'];

const EVENT_FIELDS = [
    { name: 'title',      label: '标题',    type: 'text',     required: true },
    { name: 'start_time', label: '开始时间', type: 'datetime', required: true },
    { name: 'end_time',   label: '结束时间', type: 'datetime' },
    { name: 'location',   label: '地点',    type: 'text' },
    { name: 'notes',      label: '备注',    type: 'textarea' },
    { name: 'category',   label: '分类',    type: 'text',     placeholder: '未分类' },
];

// ── module state ──────────────────────────────────────────────────────────────

let _container        = null;
let _dataChangedHandler = null;

// calendar state
let _viewMode         = 'calendar'; // 'calendar' | 'list'
let _calYear          = 0;
let _calMonth         = 0;  // 0-based
let _selectedDate     = null; // 'YYYY-MM-DD'
let _monthEvents      = [];   // events for current month

// list-view state
let _listFilter       = 'week'; // 'today' | 'week' | 'month'

// ── date helpers ──────────────────────────────────────────────────────────────

function padZ(n) { return String(n).padStart(2, '0'); }

function toDateStr(date) {
    return `${date.getFullYear()}-${padZ(date.getMonth() + 1)}-${padZ(date.getDate())}`;
}

function formatDate(iso) {
    if (!iso) return '';
    return iso.slice(0, 10);
}

function formatTime(iso) {
    if (!iso) return '';
    const d = new Date(iso);
    return `${padZ(d.getHours())}:${padZ(d.getMinutes())}`;
}

function formatDateTime(iso) {
    if (!iso) return '';
    return `${formatDate(iso)} ${formatTime(iso)}`;
}

/** Convert ISO string to datetime-local input value (YYYY-MM-DDTHH:MM) */
function toDatetimeLocal(iso) {
    if (!iso) return '';
    return iso.slice(0, 16);
}

/** Monday-first weekday index 0-6 from a Date object */
function weekdayMon(date) {
    return (date.getDay() + 6) % 7;
}

/** First day of a month, returns Date */
function firstOfMonth(year, month) {
    return new Date(year, month, 1);
}

/** Last date number of a month */
function daysInMonth(year, month) {
    return new Date(year, month + 1, 0).getDate();
}

/** Get start/end ISO strings for list-view filter */
function listFilterRange(filter) {
    const today = new Date();
    today.setHours(0, 0, 0, 0);
    let start, end;
    if (filter === 'today') {
        start = new Date(today);
        end   = new Date(today);
        end.setHours(23, 59, 59, 999);
    } else if (filter === 'week') {
        // Mon to Sun of current week
        const dow = weekdayMon(today);
        start = new Date(today);
        start.setDate(today.getDate() - dow);
        end = new Date(start);
        end.setDate(start.getDate() + 6);
        end.setHours(23, 59, 59, 999);
    } else {
        // month
        start = new Date(today.getFullYear(), today.getMonth(), 1);
        end   = new Date(today.getFullYear(), today.getMonth() + 1, 0);
        end.setHours(23, 59, 59, 999);
    }
    return { start: start.toISOString(), end: end.toISOString() };
}

// ── API helpers ───────────────────────────────────────────────────────────────

async function fetchMonthEvents(year, month) {
    const start = new Date(year, month, 1);
    const end   = new Date(year, month + 1, 0);
    end.setHours(23, 59, 59, 999);
    const res = await api.get('/items', {
        type: 'event',
        start_date: start.toISOString(),
        end_date:   end.toISOString(),
        date_field: 'start_time',
        page_size:  200,
    });
    return (res.data && res.data.items) ? res.data.items : [];
}

async function fetchListEvents(filter) {
    const { start, end } = listFilterRange(filter);
    const res = await api.get('/items', {
        type: 'event',
        start_date: start,
        end_date:   end,
        date_field: 'start_time',
        page_size:  200,
    });
    return (res.data && res.data.items) ? res.data.items : [];
}

// ── render helpers ────────────────────────────────────────────────────────────

function renderPageHeader() {
    const calActive  = _viewMode === 'calendar' ? ' btn-primary' : ' btn-secondary';
    const listActive = _viewMode === 'list'     ? ' btn-primary' : ' btn-secondary';
    return `
        <div style="display:flex;align-items:center;justify-content:space-between;margin-bottom:20px;flex-wrap:wrap;gap:12px;">
            <div>
                <h2 style="font-size:20px;font-weight:700;color:var(--color-events);">📅 日程</h2>
                <p style="font-size:13px;color:var(--color-text-secondary);margin-top:2px;">管理你的日程与事件</p>
            </div>
            <div style="display:flex;gap:8px;align-items:center;">
                <div style="display:flex;gap:4px;background:var(--color-surface);border:1px solid var(--color-border);border-radius:var(--radius-sm);padding:3px;">
                    <button id="view-calendar" class="btn btn-sm${calActive}" style="min-width:72px;">📅 日历</button>
                    <button id="view-list"     class="btn btn-sm${listActive}" style="min-width:72px;">☰ 列表</button>
                </div>
                <button id="btn-add-event" class="btn btn-primary btn-sm">＋ 添加日程</button>
            </div>
        </div>
    `;
}

function renderCalendarView() {
    const today    = toDateStr(new Date());
    const first    = firstOfMonth(_calYear, _calMonth);
    const offset   = weekdayMon(first);      // cells before day 1
    const total    = daysInMonth(_calYear, _calMonth);
    const totalCells = Math.ceil((offset + total) / 7) * 7;

    // Build a Set of date strings that have events
    const eventDates = new Set(_monthEvents.map(ev => formatDate(ev.start_time)));

    // Header: month nav
    const monthLabel = `${_calYear}年${padZ(_calMonth + 1)}月`;

    let cells = '';
    for (let i = 0; i < totalCells; i++) {
        const dayNum = i - offset + 1;
        if (dayNum < 1 || dayNum > total) {
            cells += `<div class="cal-cell cal-cell--empty"></div>`;
            continue;
        }
        const dateStr  = `${_calYear}-${padZ(_calMonth + 1)}-${padZ(dayNum)}`;
        const isToday  = dateStr === today;
        const isSelected = dateStr === _selectedDate;
        const hasDot   = eventDates.has(dateStr);

        let cls = 'cal-cell';
        if (isToday)    cls += ' cal-cell--today';
        if (isSelected) cls += ' cal-cell--selected';

        cells += `
            <div class="${cls}" data-date="${dateStr}" title="${dateStr}">
                <span class="cal-day-num">${dayNum}</span>
                ${hasDot ? `<span class="cal-dot" style="background:var(--color-events);"></span>` : ''}
            </div>
        `;
    }

    const dayHeaders = DAY_NAMES.map(d =>
        `<div class="cal-header-cell">${d}</div>`
    ).join('');

    // Day detail panel
    let dayDetail = '';
    if (_selectedDate) {
        const dayEvs = _monthEvents
            .filter(ev => formatDate(ev.start_time) === _selectedDate)
            .sort((a, b) => new Date(a.start_time) - new Date(b.start_time));

        const dayLabel = _selectedDate;
        dayDetail = `
            <div class="card" style="margin-top:16px;">
                <div class="card-header">
                    <h3 style="color:var(--color-events);font-size:15px;">📋 ${dayLabel} 的日程</h3>
                    <button id="btn-add-day-event" class="btn btn-sm btn-secondary" data-date="${_selectedDate}">＋ 添加</button>
                </div>
                <div id="day-events-list">
                    ${dayEvs.length === 0
                        ? `<div class="empty-state"><p>当天暂无日程，点击"添加"新建</p></div>`
                        : renderEventRows(dayEvs)
                    }
                </div>
            </div>
        `;
    }

    return `
        <div class="card">
            <!-- Month navigation -->
            <div class="card-header" style="padding-bottom:12px;">
                <button id="cal-prev" class="btn btn-icon btn-sm">‹</button>
                <h3 style="font-size:16px;font-weight:700;color:var(--color-text);">${monthLabel}</h3>
                <button id="cal-next" class="btn btn-icon btn-sm">›</button>
            </div>

            <!-- Day-name header row -->
            <div class="cal-grid" style="margin-bottom:0;">
                ${dayHeaders}
            </div>

            <!-- Day cells -->
            <div class="cal-grid">
                ${cells}
            </div>
        </div>

        ${dayDetail}
    `;
}

function renderListView() {
    return `
        <div class="card">
            <div class="card-header">
                <h3 style="color:var(--color-events);">日程列表</h3>
            </div>
            <!-- Filter bar -->
            <div class="filter-bar" style="padding:12px 16px;">
                <div class="filter-group">
                    <button class="btn btn-sm${_listFilter === 'today' ? ' btn-primary' : ' btn-secondary'}" data-filter="today">今天</button>
                    <button class="btn btn-sm${_listFilter === 'week'  ? ' btn-primary' : ' btn-secondary'}" data-filter="week">本周</button>
                    <button class="btn btn-sm${_listFilter === 'month' ? ' btn-primary' : ' btn-secondary'}" data-filter="month">本月</button>
                </div>
            </div>
            <div id="list-events-content">
                <div class="empty-state"><p>加载中...</p></div>
            </div>
        </div>
    `;
}

function renderEventRows(events) {
    return `
        <div style="display:flex;flex-direction:column;gap:0;">
            ${events.map(ev => `
                <div class="event-row" style="display:flex;align-items:center;gap:12px;padding:10px 16px;border-bottom:1px solid var(--color-border);">
                    <div style="flex-shrink:0;width:90px;text-align:right;color:var(--color-events);font-size:13px;font-weight:600;">
                        ${formatTime(ev.start_time)}
                        ${ev.end_time ? `<br><span style="color:var(--color-text-tertiary);font-size:11px;">– ${formatTime(ev.end_time)}</span>` : ''}
                    </div>
                    <div style="flex:1;min-width:0;">
                        <div style="font-weight:600;color:var(--color-text);overflow:hidden;text-overflow:ellipsis;white-space:nowrap;">${ev.title || '(无标题)'}</div>
                        <div style="display:flex;gap:8px;margin-top:3px;flex-wrap:wrap;align-items:center;">
                            ${ev.location ? `<span style="font-size:12px;color:var(--color-text-secondary);">📍 ${ev.location}</span>` : ''}
                            ${ev.category ? `<span class="badge">${ev.category}</span>` : ''}
                        </div>
                    </div>
                    <div style="display:flex;gap:6px;flex-shrink:0;">
                        <button class="btn btn-icon btn-sm btn-edit-event" data-id="${ev.id}" title="编辑">✏️</button>
                        <button class="btn btn-icon btn-sm btn-delete-event" data-id="${ev.id}" title="删除">🗑️</button>
                    </div>
                </div>
            `).join('')}
        </div>
    `;
}

// ── calendar grid styles (injected once) ──────────────────────────────────────

const CALENDAR_CSS_ID = 'pendo-events-cal-styles';

function ensureCalendarStyles() {
    if (document.getElementById(CALENDAR_CSS_ID)) return;
    const style = document.createElement('style');
    style.id = CALENDAR_CSS_ID;
    style.textContent = `
        .cal-grid {
            display: grid;
            grid-template-columns: repeat(7, 1fr);
            gap: 2px;
            padding: 0 8px 8px;
        }
        .cal-header-cell {
            text-align: center;
            font-size: 12px;
            font-weight: 600;
            color: var(--color-text-secondary);
            padding: 6px 0;
        }
        .cal-cell {
            min-height: 52px;
            border-radius: var(--radius-sm);
            border: 1px solid transparent;
            display: flex;
            flex-direction: column;
            align-items: center;
            justify-content: flex-start;
            padding: 4px;
            cursor: pointer;
            position: relative;
            transition: background 0.15s;
            user-select: none;
        }
        .cal-cell:hover {
            background: var(--color-hover, rgba(0,0,0,0.04));
        }
        .cal-cell--empty {
            cursor: default;
            background: transparent !important;
            border-color: transparent !important;
        }
        .cal-cell--today .cal-day-num {
            background: var(--color-events);
            color: #fff;
            border-radius: 50%;
            width: 24px;
            height: 24px;
            display: flex;
            align-items: center;
            justify-content: center;
        }
        .cal-cell--selected {
            border-color: var(--color-events);
            background: rgba(245, 158, 11, 0.08);
        }
        .cal-day-num {
            font-size: 13px;
            font-weight: 500;
            color: var(--color-text);
            width: 24px;
            height: 24px;
            display: flex;
            align-items: center;
            justify-content: center;
        }
        .cal-dot {
            width: 6px;
            height: 6px;
            border-radius: 50%;
            margin-top: 3px;
        }
    `;
    document.head.appendChild(style);
}

// ── modal helpers ─────────────────────────────────────────────────────────────

function openEventModal(existing = null, prefillDate = null) {
    const isEdit = !!existing;
    const title  = isEdit ? '编辑日程' : '添加日程';

    let defaultStart = '';
    if (isEdit && existing.start_time) {
        defaultStart = toDatetimeLocal(existing.start_time);
    } else if (prefillDate) {
        defaultStart = `${prefillDate}T09:00`;
    }

    const fields = EVENT_FIELDS.map(f => {
        let value = '';
        if (isEdit) {
            if (f.name === 'start_time') value = toDatetimeLocal(existing.start_time);
            else if (f.name === 'end_time') value = toDatetimeLocal(existing.end_time);
            else value = existing[f.name] || '';
        } else if (f.name === 'start_time') {
            value = defaultStart;
        }
        return { ...f, value };
    });

    const bodyHTML = `<form id="event-form">${buildFormHTML(fields)}</form>`;
    const footer   = `
        <button class="btn btn-secondary" id="modal-cancel">取消</button>
        <button class="btn btn-primary"  id="modal-save">保存</button>
    `;

    const content = showModal(title, bodyHTML, { footer });
    initFormInteractions(content);

    content.querySelector('#modal-cancel').onclick = closeModal;
    content.querySelector('#modal-save').onclick = async () => {
        const form = content.querySelector('#event-form');
        const data = getFormData(form);

        if (!data.title) {
            showToast('请填写标题', 'warning');
            return;
        }
        if (!data.start_time) {
            showToast('请填写开始时间', 'warning');
            return;
        }

        try {
            if (isEdit) {
                await api.put('/items/' + existing.id, data);
                showToast('日程已更新', 'success');
            } else {
                await api.post('/items', { type: 'event', ...data });
                showToast('日程已添加', 'success');
            }
            closeModal();
            window.dispatchEvent(new CustomEvent('pendo-data-changed'));
            await refreshCurrentView();
        } catch (err) {
            showToast('保存失败：' + err.message, 'error');
        }
    };
}

async function deleteEvent(id) {
    const confirmed = window.confirm('确定要删除这条日程吗？');
    if (!confirmed) return;
    try {
        await api.delete('/items/' + id);
        showToast('日程已删除', 'success');
        window.dispatchEvent(new CustomEvent('pendo-data-changed'));
        await refreshCurrentView();
    } catch (err) {
        showToast('删除失败：' + err.message, 'error');
    }
}

// ── event delegation helpers ──────────────────────────────────────────────────

function findEventById(id) {
    return _monthEvents.find(ev => String(ev.id) === String(id)) || null;
}

function attachEventRowActions(container, eventsArray) {
    container.addEventListener('click', async (e) => {
        const editBtn   = e.target.closest('.btn-edit-event');
        const deleteBtn = e.target.closest('.btn-delete-event');
        if (editBtn) {
            const id  = editBtn.dataset.id;
            const ev  = eventsArray.find(x => String(x.id) === String(id));
            if (ev) openEventModal(ev);
        } else if (deleteBtn) {
            await deleteEvent(deleteBtn.dataset.id);
        }
    });
}

// ── view refresh ──────────────────────────────────────────────────────────────

async function refreshCurrentView() {
    if (_viewMode === 'calendar') {
        await loadCalendarMonth(_calYear, _calMonth);
    } else {
        await loadListView();
    }
}

async function loadCalendarMonth(year, month) {
    _calYear  = year;
    _calMonth = month;
    try {
        _monthEvents = await fetchMonthEvents(year, month);
    } catch (err) {
        _monthEvents = [];
        showToast('加载日程失败：' + err.message, 'error');
    }
    renderPage();
}

async function loadListView() {
    const listContent = document.getElementById('list-events-content');
    if (!listContent) return;

    listContent.innerHTML = `<div class="empty-state"><p>加载中...</p></div>`;
    try {
        const events = await fetchListEvents(_listFilter);
        const sorted = events.sort((a, b) => new Date(a.start_time) - new Date(b.start_time));
        if (sorted.length === 0) {
            listContent.innerHTML = `<div class="empty-state"><p>该时段暂无日程</p></div>`;
        } else {
            listContent.innerHTML = renderEventRows(sorted);
            attachEventRowActions(listContent, sorted);
        }
    } catch (err) {
        listContent.innerHTML = `<div class="empty-state"><p>加载失败：${err.message}</p></div>`;
    }
}

// ── main page render ──────────────────────────────────────────────────────────

function renderPage() {
    if (!_container) return;

    ensureCalendarStyles();

    const viewHTML = _viewMode === 'calendar'
        ? renderCalendarView()
        : renderListView();

    _container.innerHTML = `
        <div style="padding:24px;max-width:900px;margin:0 auto;">
            ${renderPageHeader()}
            <div id="events-view-content">
                ${viewHTML}
            </div>
        </div>
    `;

    attachPageListeners();

    // If list view, kick off data load now that DOM exists
    if (_viewMode === 'list') {
        loadListView();
    }
}

function attachPageListeners() {
    const container = _container;

    // View toggle
    const calBtn  = container.querySelector('#view-calendar');
    const listBtn = container.querySelector('#view-list');
    if (calBtn) {
        calBtn.addEventListener('click', async () => {
            if (_viewMode !== 'calendar') {
                _viewMode = 'calendar';
                _selectedDate = null;
                renderPage();
                // Data was already loaded; re-render is enough
            }
        });
    }
    if (listBtn) {
        listBtn.addEventListener('click', () => {
            if (_viewMode !== 'list') {
                _viewMode = 'list';
                renderPage();
            }
        });
    }

    // Add event button (top bar)
    const addBtn = container.querySelector('#btn-add-event');
    if (addBtn) {
        addBtn.addEventListener('click', () => openEventModal(null, null));
    }

    // Calendar-specific listeners
    if (_viewMode === 'calendar') {
        const prevBtn = container.querySelector('#cal-prev');
        const nextBtn = container.querySelector('#cal-next');

        if (prevBtn) {
            prevBtn.addEventListener('click', async () => {
                let y = _calYear, m = _calMonth - 1;
                if (m < 0) { m = 11; y--; }
                _selectedDate = null;
                await loadCalendarMonth(y, m);
            });
        }
        if (nextBtn) {
            nextBtn.addEventListener('click', async () => {
                let y = _calYear, m = _calMonth + 1;
                if (m > 11) { m = 0; y++; }
                _selectedDate = null;
                await loadCalendarMonth(y, m);
            });
        }

        // Day cell click
        const calGrid = container.querySelector('.cal-grid:last-of-type');
        // Use the events-view-content div as delegate since it holds all cells
        const viewContent = container.querySelector('#events-view-content');
        if (viewContent) {
            // once:true is fine — renderPage() replaces #events-view-content and
            // calls attachPageListeners() again, so the listener is always fresh.
            viewContent.addEventListener('click', (e) => {
                const cell = e.target.closest('.cal-cell:not(.cal-cell--empty)');
                if (!cell) return;
                const date = cell.dataset.date;
                if (!date) return;

                if (_selectedDate === date) {
                    _selectedDate = null;
                } else {
                    _selectedDate = date;
                }
                renderPage();
            }, { once: true });
        }

        // "Add for day" button in day detail panel
        const addDayBtn = container.querySelector('#btn-add-day-event');
        if (addDayBtn) {
            addDayBtn.addEventListener('click', () => {
                openEventModal(null, addDayBtn.dataset.date);
            });
        }

        // Event row actions in day detail panel
        const dayList = container.querySelector('#day-events-list');
        if (dayList) {
            const dayEvs = _selectedDate
                ? _monthEvents.filter(ev => formatDate(ev.start_time) === _selectedDate)
                : [];
            attachEventRowActions(dayList, dayEvs);
        }
    }

    // List-view filter buttons
    if (_viewMode === 'list') {
        const filterBar = container.querySelector('.filter-bar');
        if (filterBar) {
            filterBar.addEventListener('click', async (e) => {
                const btn = e.target.closest('[data-filter]');
                if (!btn) return;
                _listFilter = btn.dataset.filter;
                renderPage();
            });
        }
    }
}

// ── page module exports ───────────────────────────────────────────────────────

export function render(container) {
    _container = container;

    // Initialise calendar to current month
    const now = new Date();
    _calYear  = now.getFullYear();
    _calMonth = now.getMonth();
    _selectedDate = null;

    // Start with an immediate render (skeleton), then load data
    _viewMode = 'calendar';
    renderPage();
    fetchMonthEvents(_calYear, _calMonth).then(events => {
        _monthEvents = events;
        renderPage();
    }).catch(err => {
        showToast('加载日程失败：' + err.message, 'error');
    });

    _dataChangedHandler = () => refreshCurrentView();
    window.addEventListener('pendo-data-changed', _dataChangedHandler);
}

export function destroy() {
    if (_dataChangedHandler) {
        window.removeEventListener('pendo-data-changed', _dataChangedHandler);
        _dataChangedHandler = null;
    }
    _container  = null;
    _monthEvents = [];
}

export function onRouteEnter(_params) {
    // Nothing special; render() is called by the router
}
