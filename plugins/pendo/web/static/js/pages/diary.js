import { api } from '../api.js';
import { showToast } from '../components/toast.js';
import { showModal, closeModal, showConfirmModal } from '../components/modal.js';
import { buildFormHTML, getFormData, initFormInteractions } from '../components/form.js';
import { renderCustomSelect, initCustomSelects } from '../components/custom_select.js';

// ── constants ─────────────────────────────────────────────────────────────────

const CSS_ID = 'pendo-diary-styles';

const WEATHER_OPTIONS = ['☀️ 晴', '⛅ 多云', '🌧️ 雨', '❄️ 雪', '🌫️ 雾', '💨 风'];

const DIARY_FIELDS = [
    { name: 'diary_date', label: '日期',     type: 'date',     required: true },
    { name: 'mood',       label: '心情',     type: 'mood' },
    {
        name: 'weather',
        label: '天气',
        type: 'select',
        options: [{ value: '', label: '未设置' }, ...WEATHER_OPTIONS],
        selectThemeClass: 'pselect-theme-diary',
    },
    { name: 'location',   label: '地点',     type: 'text' },
    { name: 'title',      label: '标题',     type: 'text',     placeholder: '可选标题' },
    { name: 'content',    label: '日记内容', type: 'textarea', rows: 8, required: true },
];

// ── module state ──────────────────────────────────────────────────────────────

let _container          = null;
let _items              = [];
let _filterYear         = 0;
let _filterMonth        = 0;   // 1-based
let _templates          = [];
let _templatesLoaded    = false;
let _dataChangedHandler = null;

// ── helpers ───────────────────────────────────────────────────────────────────

function padZ(n) { return String(n).padStart(2, '0'); }

function todayStr() {
    const d = new Date();
    return `${d.getFullYear()}-${padZ(d.getMonth() + 1)}-${padZ(d.getDate())}`;
}

function monthStart(year, month) {
    return `${year}-${padZ(month)}-01`;
}

function monthEnd(year, month) {
    const last = new Date(year, month, 0).getDate();
    return `${year}-${padZ(month)}-${padZ(last)}`;
}

function contentPreview(content) {
    if (!content) return '';
    const text = content.trim();
    return text.length <= 150 ? text : text.slice(0, 150) + '...';
}

function esc(str) {
    if (!str) return '';
    return String(str)
        .replace(/&/g, '&amp;')
        .replace(/</g, '&lt;')
        .replace(/>/g, '&gt;')
        .replace(/"/g, '&quot;');
}

/** Group items by diary_date (descending) */
function groupByDate(items) {
    const map = new Map();
    items.forEach(item => {
        const d = item.diary_date ? item.diary_date.slice(0, 10) : '未知日期';
        if (!map.has(d)) map.set(d, []);
        map.get(d).push(item);
    });
    // Sort dates descending
    const sorted = Array.from(map.entries()).sort((a, b) => b[0].localeCompare(a[0]));
    return sorted;
}

function formatDateLabel(dateStr) {
    if (!dateStr || dateStr === '未知日期') return dateStr;
    const d = new Date(dateStr + 'T00:00:00');
    const weekNames = ['周日', '周一', '周二', '周三', '周四', '周五', '周六'];
    return `${d.getFullYear()}年${d.getMonth() + 1}月${d.getDate()}日  ${weekNames[d.getDay()]}`;
}

// ── CSS ───────────────────────────────────────────────────────────────────────

function ensureStyles() {
    if (document.getElementById(CSS_ID)) return;
    const style = document.createElement('style');
    style.id = CSS_ID;
    style.textContent = `
        .diary-page { padding: 24px; }

        .diary-page-header {
            display: flex;
            align-items: center;
            justify-content: space-between;
            margin-bottom: 20px;
            flex-wrap: wrap;
            gap: 12px;
        }

        .diary-month-nav {
            display: flex;
            align-items: center;
            gap: 0;
            background: var(--color-surface);
            border: 1px solid var(--color-border);
            border-radius: var(--radius);
            overflow: hidden;
        }
        .diary-month-nav button {
            background: none;
            border: none;
            padding: 6px 14px;
            cursor: pointer;
            font-size: 15px;
            color: var(--color-text-secondary);
            transition: background 0.12s, color 0.12s;
        }
        .diary-month-nav button:hover {
            background: var(--color-border);
            color: var(--color-text);
        }
        .diary-month-nav .month-label {
            padding: 6px 16px;
            font-size: 14px;
            font-weight: 600;
            color: var(--color-text);
            border-left: 1px solid var(--color-border);
            border-right: 1px solid var(--color-border);
            min-width: 110px;
            text-align: center;
            user-select: none;
        }

        /* Timeline */
        .diary-timeline {
            position: relative;
            padding-left: 0;
        }

        .diary-date-group {
            margin-bottom: 28px;
        }

        .diary-date-header {
            display: flex;
            align-items: center;
            gap: 10px;
            margin-bottom: 12px;
        }
        .diary-date-dot {
            width: 10px;
            height: 10px;
            border-radius: 50%;
            background: var(--color-diary, #EC4899);
            flex-shrink: 0;
        }
        .diary-date-label {
            font-size: 14px;
            font-weight: 700;
            color: var(--color-diary, #EC4899);
            letter-spacing: 0.02em;
        }
        .diary-date-line {
            flex: 1;
            height: 1px;
            background: var(--color-border);
        }

        .diary-entry {
            background: var(--color-surface);
            border: 1px solid var(--color-border);
            border-radius: var(--radius);
            margin-bottom: 12px;
            margin-left: 20px;
            display: flex;
            gap: 0;
            overflow: hidden;
            transition: box-shadow 0.15s, border-color 0.15s;
        }
        .diary-entry:hover {
            box-shadow: 0 4px 16px rgba(236,72,153,0.10);
            border-color: var(--color-diary, #EC4899);
        }

        .diary-entry-mood {
            display: flex;
            align-items: center;
            justify-content: center;
            width: 56px;
            flex-shrink: 0;
            font-size: 26px;
            background: rgba(236,72,153,0.06);
            border-right: 1px solid var(--color-border);
            padding: 12px 0;
        }

        .diary-entry-body {
            flex: 1;
            padding: 12px 14px;
            cursor: pointer;
            min-width: 0;
        }

        .diary-entry-meta {
            display: flex;
            align-items: center;
            flex-wrap: wrap;
            gap: 6px;
            margin-bottom: 6px;
        }
        .diary-entry-weather {
            font-size: 13px;
            color: var(--color-text-secondary);
        }
        .diary-entry-location {
            font-size: 12px;
            color: var(--color-text-tertiary);
            display: flex;
            align-items: center;
            gap: 3px;
        }
        .diary-entry-title {
            font-size: 14px;
            font-weight: 600;
            color: var(--color-text);
            margin-bottom: 4px;
            overflow: hidden;
            text-overflow: ellipsis;
            white-space: nowrap;
        }
        .diary-entry-preview {
            font-size: 13px;
            color: var(--color-text-secondary);
            line-height: 1.6;
            word-break: break-word;
        }

        .diary-entry-actions {
            display: flex;
            flex-direction: column;
            align-items: center;
            justify-content: center;
            gap: 6px;
            padding: 10px 10px;
            flex-shrink: 0;
            border-left: 1px solid var(--color-border);
        }

        .diary-empty {
            text-align: center;
            padding: 70px 0;
            color: var(--color-text-tertiary);
            font-size: 14px;
        }
        .diary-empty-icon {
            font-size: 48px;
            margin-bottom: 12px;
            display: block;
        }

        /* View modal */
        .diary-view-header {
            display: flex;
            flex-wrap: wrap;
            align-items: center;
            gap: 8px;
            margin-bottom: 14px;
            padding-bottom: 12px;
            border-bottom: 1px solid var(--color-border);
        }
        .diary-view-mood {
            font-size: 28px;
            line-height: 1;
        }
        .diary-view-meta-items {
            display: flex;
            flex-wrap: wrap;
            gap: 6px;
            align-items: center;
        }
        .diary-view-badge {
            font-size: 12px;
            background: var(--color-border);
            color: var(--color-text-secondary);
            border-radius: 4px;
            padding: 2px 8px;
        }
        .diary-view-content {
            white-space: pre-wrap;
            word-break: break-word;
            font-size: 14px;
            color: var(--color-text);
            line-height: 1.8;
            max-height: 55vh;
            overflow-y: auto;
        }

        /* Template hint */
        .diary-template-hint {
            font-size: 12px;
            color: var(--color-text-tertiary);
            margin-top: 6px;
            padding: 8px 10px;
            background: var(--color-border);
            border-radius: var(--radius-sm);
            line-height: 1.6;
            white-space: pre-wrap;
        }
        .diary-template-select {
            margin-top: 2px;
        }
    `;
    document.head.appendChild(style);
}

// ── API ───────────────────────────────────────────────────────────────────────

async function fetchItems(year, month) {
    const res = await api.get('/items', {
        type:       'diary',
        date_field: 'diary_date',
        start_date: monthStart(year, month),
        end_date:   monthEnd(year, month),
        page_size:  50,
    });
    return (res.data && res.data.items) ? res.data.items : [];
}

async function loadTemplates() {
    if (_templatesLoaded) return;
    try {
        const res = await api.get('/config/diary/templates');
        _templates = (res.data && res.data.templates) ? res.data.templates : [];
    } catch (_) {
        _templates = [];
    }
    _templatesLoaded = true;
}

// ── render ────────────────────────────────────────────────────────────────────

function renderMonthNav() {
    return `
        <div class="diary-month-nav">
            <button id="diary-prev-month" title="上个月">&#9664;</button>
            <span class="month-label">${_filterYear}年${_filterMonth}月</span>
            <button id="diary-next-month" title="下个月">&#9654;</button>
        </div>
    `;
}

function renderEntry(item) {
    const mood     = item.mood || '📖';
    const preview  = esc(contentPreview(item.content));
    const weather  = item.weather ? `<span class="diary-entry-weather">${esc(item.weather)}</span>` : '';
    const location = item.location
        ? `<span class="diary-entry-location">📍 ${esc(item.location)}</span>`
        : '';
    const title    = item.title
        ? `<div class="diary-entry-title" title="${esc(item.title)}">${esc(item.title)}</div>`
        : '';

    return `
        <div class="diary-entry" data-id="${item.id}">
            <div class="diary-entry-mood">${mood}</div>
            <div class="diary-entry-body" data-action="view" data-id="${item.id}">
                ${(weather || location) ? `<div class="diary-entry-meta">${weather}${location}</div>` : ''}
                ${title}
                <div class="diary-entry-preview">${preview || '<span style="opacity:0.45;">（无内容）</span>'}</div>
            </div>
            <div class="diary-entry-actions">
                <button class="btn btn-sm btn-secondary btn-icon" data-action="edit" data-id="${item.id}" title="编辑">✏️</button>
                <button class="btn btn-sm btn-danger btn-icon"    data-action="delete" data-id="${item.id}" title="删除">🗑️</button>
            </div>
        </div>
    `;
}

function renderTimeline() {
    if (_items.length === 0) {
        return `
            <div class="diary-empty">
                <span class="diary-empty-icon">📔</span>
                本月暂无日记，点击右上角写一篇吧
            </div>
        `;
    }

    const groups = groupByDate(_items);
    return `
        <div class="diary-timeline" id="diary-timeline">
            ${groups.map(([date, entries]) => `
                <div class="diary-date-group">
                    <div class="diary-date-header">
                        <span class="diary-date-dot"></span>
                        <span class="diary-date-label">${formatDateLabel(date)}</span>
                        <span class="diary-date-line"></span>
                    </div>
                    ${entries.map(renderEntry).join('')}
                </div>
            `).join('')}
        </div>
    `;
}

function renderPage() {
    if (!_container) return;
    ensureStyles();

    _container.innerHTML = `
        <div class="diary-page">
            <div class="diary-page-header">
                <div>
                    <h2 style="font-size:20px;font-weight:700;color:var(--color-diary,#EC4899);">📔 日记</h2>
                    <p style="font-size:13px;color:var(--color-text-secondary);margin-top:2px;">记录每天的心情与故事</p>
                </div>
                <div style="display:flex;align-items:center;gap:12px;flex-wrap:wrap;">
                    ${renderMonthNav()}
                    <button class="btn btn-primary" id="btn-add-diary">＋ 写日记</button>
                </div>
            </div>

            <div id="diary-list-area">
                ${renderTimeline()}
            </div>
        </div>
    `;

    attachListeners();
}

// ── listeners ─────────────────────────────────────────────────────────────────

function attachListeners() {
    if (!_container) return;

    const addBtn = _container.querySelector('#btn-add-diary');
    if (addBtn) addBtn.addEventListener('click', () => openDiaryFormModal(null));

    const prevBtn = _container.querySelector('#diary-prev-month');
    if (prevBtn) {
        prevBtn.addEventListener('click', async () => {
            _filterMonth--;
            if (_filterMonth < 1) { _filterMonth = 12; _filterYear--; }
            await loadAndRender();
        });
    }

    const nextBtn = _container.querySelector('#diary-next-month');
    if (nextBtn) {
        nextBtn.addEventListener('click', async () => {
            _filterMonth++;
            if (_filterMonth > 12) { _filterMonth = 1; _filterYear++; }
            await loadAndRender();
        });
    }

    const listArea = _container.querySelector('#diary-list-area');
    if (listArea) {
        listArea.addEventListener('click', (e) => {
            const viewTarget   = e.target.closest('[data-action="view"]');
            const editTarget   = e.target.closest('[data-action="edit"]');
            const deleteTarget = e.target.closest('[data-action="delete"]');

            if (deleteTarget) {
                const id   = deleteTarget.dataset.id;
                const item = _items.find(i => String(i.id) === String(id));
                if (item) deleteItem(item);
                return;
            }
            if (editTarget) {
                const id   = editTarget.dataset.id;
                const item = _items.find(i => String(i.id) === String(id));
                if (item) openDiaryFormModal(item);
                return;
            }
            if (viewTarget) {
                const id   = viewTarget.dataset.id;
                const item = _items.find(i => String(i.id) === String(id));
                if (item) openDiaryViewModal(item);
            }
        });
    }
}

// ── view modal ────────────────────────────────────────────────────────────────

function openDiaryViewModal(item) {
    const mood     = item.mood ? `<span class="diary-view-mood">${item.mood}</span>` : '';
    const weather  = item.weather
        ? `<span class="diary-view-badge">${esc(item.weather)}</span>` : '';
    const location = item.location
        ? `<span class="diary-view-badge">📍 ${esc(item.location)}</span>` : '';
    const date     = item.diary_date
        ? `<span class="diary-view-badge">📅 ${esc(item.diary_date)}</span>` : '';

    const bodyHTML = `
        <div class="diary-view-header">
            ${mood}
            <div class="diary-view-meta-items">
                ${date}${weather}${location}
            </div>
        </div>
        <div class="diary-view-content">${esc(item.content || '')}</div>
    `;

    const footer = `
        <button class="btn btn-danger btn-sm" id="modal-delete" style="margin-right:auto;">删除</button>
        <button class="btn btn-secondary"     id="modal-cancel">关闭</button>
        <button class="btn btn-primary"       id="modal-edit">编辑</button>
    `;

    const content = showModal(item.title || formatDateLabel(item.diary_date) || '日记详情', bodyHTML, { footer });

    content.querySelector('#modal-cancel').onclick = closeModal;

    content.querySelector('#modal-edit').onclick = () => {
        closeModal();
        openDiaryFormModal(item);
    };

    content.querySelector('#modal-delete').onclick = () => {
        closeModal();
        deleteItem(item, () => openDiaryViewModal(item));
    };
}

// ── add/edit form modal ───────────────────────────────────────────────────────

async function openDiaryFormModal(existing = null) {
    const isEdit = !!existing;
    const title  = isEdit ? '编辑日记' : '写日记';

    // Prefill field values
    const fields = DIARY_FIELDS.map(f => {
        let value = '';
        if (isEdit) {
            value = (existing[f.name] !== undefined && existing[f.name] !== null)
                ? existing[f.name]
                : '';
        } else if (f.name === 'diary_date') {
            value = todayStr();
        }
        return { ...f, value };
    });

    let templateSectionHTML = '';

    // Try to load templates for new entries
    if (!isEdit) {
        await loadTemplates();
        if (_templates.length > 0) {
            templateSectionHTML = `
                <div class="form-group">
                    <label class="form-label">模板（可选）</label>
                    ${renderCustomSelect({
                        id: 'diary-template-sel',
                        name: 'template_id',
                        options: [
                            { value: '', label: '-- 不使用模板 --' },
                            ..._templates.map(t => ({ value: t.id, label: t.name })),
                        ],
                        selected: '',
                        className: 'pselect-form pselect-block pselect-theme-diary diary-template-select',
                    })}
                    <div id="diary-template-hint" class="diary-template-hint" style="display:none;"></div>
                </div>
            `;
        }
    }

    const bodyHTML = `<form id="diary-form">${templateSectionHTML}${buildFormHTML(fields)}</form>`;

    const deleteBtn = isEdit
        ? `<button class="btn btn-danger btn-sm" id="modal-delete" style="margin-right:auto;">删除</button>`
        : '';
    const footer = `
        ${deleteBtn}
        <button class="btn btn-secondary" id="modal-cancel">取消</button>
        <button class="btn btn-primary"   id="modal-save">保存</button>
    `;

    const content = showModal(title, bodyHTML, { footer });
    initFormInteractions(content);

    if (!isEdit && _templates.length > 0) {
        const templateHint = content.querySelector('#diary-template-hint');
        initCustomSelects(content, {
            'diary-template-sel': (value) => {
                if (!templateHint) return;
                const tpl = _templates.find(t => String(t.id) === String(value));
                if (tpl && tpl.prompts && tpl.prompts.length > 0) {
                    templateHint.textContent = tpl.prompts.join('\n');
                    templateHint.style.display = 'block';
                    return;
                }
                templateHint.textContent = '';
                templateHint.style.display = 'none';
            },
        });
    }

    content.querySelector('#modal-cancel').onclick = closeModal;

    if (isEdit) {
        content.querySelector('#modal-delete').onclick = () => {
            closeModal();
            deleteItem(existing, () => openDiaryFormModal(existing));
        };
    }

    content.querySelector('#modal-save').onclick = async () => {
        const form = content.querySelector('#diary-form');
        const data = getFormData(form);

        if (!data.content) {
            showToast('请填写日记内容', 'warning');
            return;
        }
        if (!data.diary_date) {
            showToast('请选择日期', 'warning');
            return;
        }

        if (!data.template_id) {
            delete data.template_id;
        }

        try {
            if (isEdit) {
                await api.put('/items/' + existing.id, data);
                showToast('日记已更新', 'success');
            } else {
                await api.post('/items', { type: 'diary', ...data });
                showToast('日记已添加', 'success');
            }
            closeModal();
            window.dispatchEvent(new CustomEvent('pendo-data-changed'));
            await loadAndRender();
        } catch (err) {
            showToast('保存失败：' + err.message, 'error');
        }
    };
}

// ── delete ────────────────────────────────────────────────────────────────────

async function deleteItem(item, onCancel = null) {
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
        await api.delete('/items/' + item.id);
        showToast('日记已删除', 'success');
        window.dispatchEvent(new CustomEvent('pendo-data-changed'));
        await loadAndRender();
    } catch (err) {
        showToast('删除失败：' + err.message, 'error');
    }
}

// ── data load ─────────────────────────────────────────────────────────────────

async function loadAndRender() {
    try {
        _items = await fetchItems(_filterYear, _filterMonth);
    } catch (err) {
        _items = [];
        showToast('加载日记失败：' + err.message, 'error');
    }
    renderPage();
}

// ── page module exports ───────────────────────────────────────────────────────

export function render(container) {
    _container = container;
    _items     = [];

    // Default to current month
    const now    = new Date();
    _filterYear  = now.getFullYear();
    _filterMonth = now.getMonth() + 1;

    renderPage();
    loadAndRender();

    _dataChangedHandler = () => loadAndRender();
    window.addEventListener('pendo-data-changed', _dataChangedHandler);
}

export function destroy() {
    if (_dataChangedHandler) {
        window.removeEventListener('pendo-data-changed', _dataChangedHandler);
        _dataChangedHandler = null;
    }
    _container = null;
    _items     = [];
}

export function onRouteEnter(_params) {
    // Nothing special; render() is called by the router
}
