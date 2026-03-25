import { api } from '../api.js';
import { showToast } from '../components/toast.js';
import { showModal, closeModal } from '../components/modal.js';
import { buildFormHTML, getFormData, initFormInteractions } from '../components/form.js';

// ── constants ─────────────────────────────────────────────────────────────────

const COLUMNS = [
    { key: 'todo',        label: '待办',   color: '#F59E0B' },
    { key: 'in_progress', label: '进行中', color: '#3B82F6' },
    { key: 'done',        label: '已完成', color: '#10B981' },
    { key: 'cancelled',   label: '已取消', color: '#9CA3AF' },
];

const PRIORITY_INFO = {
    1: { icon: '🔴', label: '紧急',  color: '#EF4444' },
    2: { icon: '🟠', label: '高',    color: '#F97316' },
    3: { icon: '🟡', label: '中',    color: '#EAB308' },
    4: { icon: '🟢', label: '低',    color: '#22C55E' },
    5: { icon: '⚪', label: '最低',  color: '#9CA3AF' },
};

const TASK_FIELDS = [
    { name: 'title',    label: '标题',    type: 'text',     required: true },
    { name: 'priority', label: '优先级',  type: 'priority', value: 3 },
    { name: 'status',   label: '状态',    type: 'select', options: [
        { value: 'todo',        label: '待办' },
        { value: 'in_progress', label: '进行中' },
        { value: 'done',        label: '已完成' },
        { value: 'cancelled',   label: '已取消' },
    ]},
    { name: 'due_time',  label: '截止时间', type: 'datetime' },
    { name: 'category',  label: '分类',     type: 'text', placeholder: '未分类' },
    { name: 'content',   label: '备注',     type: 'textarea' },
];

const CSS_ID = 'pendo-tasks-styles';

// ── module state ──────────────────────────────────────────────────────────────

let _container          = null;
let _tasks              = [];
let _dataChangedHandler = null;
let _filterCategory     = '';
let _filterPriority     = '';
let _collapsedCols      = new Set(['cancelled']); // cancelled collapsed by default
let _dragTaskId         = null;

// ── helpers ───────────────────────────────────────────────────────────────────

function padZ(n) { return String(n).padStart(2, '0'); }

function formatDueDate(iso) {
    if (!iso) return '';
    const d = new Date(iso);
    return `${d.getFullYear()}-${padZ(d.getMonth() + 1)}-${padZ(d.getDate())} ${padZ(d.getHours())}:${padZ(d.getMinutes())}`;
}

function toDatetimeLocal(iso) {
    if (!iso) return '';
    return iso.slice(0, 16);
}

function isOverdue(task) {
    if (!task.due_time) return false;
    if (task.status !== 'todo' && task.status !== 'in_progress') return false;
    return new Date(task.due_time) < new Date();
}

function getCategories() {
    const cats = new Set();
    _tasks.forEach(t => { if (t.category) cats.add(t.category); });
    return Array.from(cats).sort();
}

function filteredTasks() {
    return _tasks.filter(t => {
        if (_filterCategory && t.category !== _filterCategory) return false;
        if (_filterPriority && String(t.priority) !== String(_filterPriority)) return false;
        return true;
    });
}

function sortedForColumn(tasks, colKey) {
    return tasks
        .filter(t => t.status === colKey)
        .sort((a, b) => {
            const pa = a.priority || 5;
            const pb = b.priority || 5;
            if (pa !== pb) return pa - pb;
            if (a.due_time && b.due_time) return new Date(a.due_time) - new Date(b.due_time);
            if (a.due_time) return -1;
            if (b.due_time) return 1;
            return 0;
        });
}

// ── CSS ───────────────────────────────────────────────────────────────────────

function ensureStyles() {
    if (document.getElementById(CSS_ID)) return;
    const style = document.createElement('style');
    style.id = CSS_ID;
    style.textContent = `
        .tasks-page { padding: 24px; }
        .tasks-page-header {
            display: flex;
            align-items: center;
            justify-content: space-between;
            margin-bottom: 16px;
            flex-wrap: wrap;
            gap: 12px;
        }
        .tasks-filter-bar {
            display: flex;
            gap: 10px;
            align-items: center;
            flex-wrap: wrap;
            margin-bottom: 20px;
            background: var(--color-surface);
            border: 1px solid var(--color-border);
            border-radius: var(--radius);
            padding: 10px 14px;
        }
        .tasks-filter-bar label {
            font-size: 13px;
            color: var(--color-text-secondary);
            margin-right: 4px;
        }
        .tasks-filter-bar select {
            font-size: 13px;
            padding: 4px 8px;
            border-radius: var(--radius-sm);
            border: 1px solid var(--color-border);
            background: var(--color-bg);
            color: var(--color-text);
            cursor: pointer;
        }
        .kanban-board {
            display: flex;
            gap: 16px;
            align-items: flex-start;
            overflow-x: auto;
            padding-bottom: 8px;
        }
        .kanban-column {
            flex: 1 1 0;
            min-width: 240px;
            max-width: 320px;
            background: var(--color-surface);
            border: 1px solid var(--color-border);
            border-radius: var(--radius);
            display: flex;
            flex-direction: column;
        }
        .kanban-header {
            display: flex;
            align-items: center;
            justify-content: space-between;
            padding: 10px 14px;
            border-bottom: 1px solid var(--color-border);
            cursor: pointer;
            user-select: none;
            border-radius: var(--radius) var(--radius) 0 0;
        }
        .kanban-header:hover {
            background: var(--color-hover, rgba(0,0,0,0.03));
        }
        .kanban-header-left {
            display: flex;
            align-items: center;
            gap: 8px;
        }
        .kanban-col-dot {
            width: 10px;
            height: 10px;
            border-radius: 50%;
            flex-shrink: 0;
        }
        .kanban-col-label {
            font-size: 14px;
            font-weight: 600;
            color: var(--color-text);
        }
        .kanban-col-count {
            font-size: 12px;
            background: var(--color-border);
            color: var(--color-text-secondary);
            border-radius: 10px;
            padding: 1px 7px;
            font-weight: 600;
        }
        .kanban-col-toggle {
            font-size: 12px;
            color: var(--color-text-tertiary);
            transition: transform 0.2s;
        }
        .kanban-col-toggle.collapsed { transform: rotate(-90deg); }
        .kanban-cards {
            padding: 8px;
            display: flex;
            flex-direction: column;
            gap: 8px;
            min-height: 80px;
        }
        .kanban-cards.drag-over {
            background: rgba(16, 185, 129, 0.06);
            border-radius: 0 0 var(--radius) var(--radius);
        }
        .kanban-card {
            background: var(--color-bg);
            border: 1px solid var(--color-border);
            border-radius: var(--radius-sm);
            padding: 10px 12px;
            cursor: pointer;
            transition: box-shadow 0.15s, opacity 0.15s;
            border-left: 3px solid transparent;
        }
        .kanban-card:hover {
            box-shadow: 0 2px 8px rgba(0,0,0,0.10);
        }
        .kanban-card.dragging {
            opacity: 0.45;
        }
        .kanban-card-title {
            font-size: 14px;
            font-weight: 600;
            color: var(--color-text);
            margin-bottom: 6px;
            overflow: hidden;
            text-overflow: ellipsis;
            white-space: nowrap;
        }
        .kanban-card-meta {
            display: flex;
            flex-wrap: wrap;
            gap: 5px;
            align-items: center;
        }
        .kanban-card-priority {
            font-size: 12px;
        }
        .kanban-card-due {
            font-size: 11px;
            color: var(--color-text-tertiary);
        }
        .kanban-card-due.overdue {
            color: #EF4444;
            font-weight: 600;
        }
        .badge-overdue {
            font-size: 11px;
            background: #EF4444;
            color: #fff;
            border-radius: 4px;
            padding: 1px 5px;
            font-weight: 600;
        }
        .kanban-col-add {
            padding: 6px 8px 8px;
        }
        .kanban-col-add button {
            width: 100%;
            font-size: 13px;
            border: 1px dashed var(--color-border);
            background: transparent;
            color: var(--color-text-secondary);
            border-radius: var(--radius-sm);
            padding: 6px;
            cursor: pointer;
            transition: background 0.15s, color 0.15s;
        }
        .kanban-col-add button:hover {
            background: var(--color-hover, rgba(0,0,0,0.04));
            color: var(--color-text);
        }
        .kanban-empty {
            text-align: center;
            font-size: 12px;
            color: var(--color-text-tertiary);
            padding: 16px 0;
        }
    `;
    document.head.appendChild(style);
}

// ── API ───────────────────────────────────────────────────────────────────────

async function fetchTasks() {
    const res = await api.get('/items', { type: 'task', page_size: 200 });
    return (res.data && res.data.items) ? res.data.items : [];
}

// ── render ────────────────────────────────────────────────────────────────────

function renderCard(task) {
    const pInfo = PRIORITY_INFO[task.priority] || PRIORITY_INFO[3];
    const overdue = isOverdue(task);
    const dueLabel = task.due_time ? formatDueDate(task.due_time) : '';
    const categoryBadge = task.category
        ? `<span class="badge" style="font-size:11px;">${task.category}</span>`
        : '';
    const overdueBadge = overdue
        ? `<span class="badge-overdue">逾期</span>`
        : '';
    const dueLine = dueLabel
        ? `<span class="kanban-card-due${overdue ? ' overdue' : ''}">⏰ ${dueLabel}</span>`
        : '';

    return `
        <div class="kanban-card"
             data-id="${task.id}"
             draggable="true"
             style="border-left-color: ${pInfo.color};"
             title="${task.title || ''}">
            <div class="kanban-card-title">${task.title || '(无标题)'}</div>
            <div class="kanban-card-meta">
                <span class="kanban-card-priority">${pInfo.icon} ${pInfo.label}</span>
                ${categoryBadge}
                ${dueLine}
                ${overdueBadge}
            </div>
        </div>
    `;
}

function renderColumn(col, tasks) {
    const colTasks = sortedForColumn(tasks, col.key);
    const count    = colTasks.length;
    const isCollapsed = _collapsedCols.has(col.key);
    const toggleCls   = isCollapsed ? ' collapsed' : '';
    const isAddCol    = col.key === 'todo';

    const cardsHTML = isCollapsed ? '' : `
        <div class="kanban-cards" data-col="${col.key}">
            ${colTasks.length === 0
                ? `<div class="kanban-empty">暂无任务</div>`
                : colTasks.map(renderCard).join('')
            }
        </div>
        ${isAddCol ? `<div class="kanban-col-add"><button id="btn-add-task">＋ 添加任务</button></div>` : ''}
    `;

    return `
        <div class="kanban-column" data-col="${col.key}">
            <div class="kanban-header" data-toggle="${col.key}">
                <div class="kanban-header-left">
                    <div class="kanban-col-dot" style="background:${col.color};"></div>
                    <span class="kanban-col-label">${col.label}</span>
                    <span class="kanban-col-count">${count}</span>
                </div>
                <span class="kanban-col-toggle${toggleCls}">▼</span>
            </div>
            ${cardsHTML}
        </div>
    `;
}

function renderFilterBar() {
    const categories = getCategories();
    const catOptions = ['', ...categories]
        .map(c => `<option value="${c}"${_filterCategory === c ? ' selected' : ''}>${c || '全部分类'}</option>`)
        .join('');
    const prioOptions = [
        { value: '', label: '全部优先级' },
        { value: '1', label: '🔴 紧急' },
        { value: '2', label: '🟠 高' },
        { value: '3', label: '🟡 中' },
        { value: '4', label: '🟢 低' },
        { value: '5', label: '⚪ 最低' },
    ].map(o => `<option value="${o.value}"${_filterPriority === o.value ? ' selected' : ''}>${o.label}</option>`)
     .join('');

    return `
        <div class="tasks-filter-bar">
            <label>分类：</label>
            <select id="filter-category">${catOptions}</select>
            <label style="margin-left:8px;">优先级：</label>
            <select id="filter-priority">${prioOptions}</select>
        </div>
    `;
}

function renderPage() {
    if (!_container) return;

    ensureStyles();
    const tasks = filteredTasks();

    _container.innerHTML = `
        <div class="tasks-page">
            <div class="tasks-page-header">
                <div>
                    <h2 style="font-size:20px;font-weight:700;color:var(--color-tasks);">✅ 任务</h2>
                    <p style="font-size:13px;color:var(--color-text-secondary);margin-top:2px;">用看板管理你的任务</p>
                </div>
            </div>
            ${renderFilterBar()}
            <div class="kanban-board" id="kanban-board">
                ${COLUMNS.map(col => renderColumn(col, tasks)).join('')}
            </div>
        </div>
    `;

    attachListeners();
}

// ── listeners ─────────────────────────────────────────────────────────────────

function attachListeners() {
    if (!_container) return;

    // Filter selects
    const catSel = _container.querySelector('#filter-category');
    if (catSel) {
        catSel.addEventListener('change', () => {
            _filterCategory = catSel.value;
            renderPage();
        });
    }
    const prioSel = _container.querySelector('#filter-priority');
    if (prioSel) {
        prioSel.addEventListener('change', () => {
            _filterPriority = prioSel.value;
            renderPage();
        });
    }

    // Column toggle (collapse/expand)
    _container.querySelectorAll('[data-toggle]').forEach(header => {
        header.addEventListener('click', () => {
            const colKey = header.dataset.toggle;
            if (_collapsedCols.has(colKey)) {
                _collapsedCols.delete(colKey);
            } else {
                _collapsedCols.add(colKey);
            }
            renderPage();
        });
    });

    // Add task button (only in TODO column)
    const addBtn = _container.querySelector('#btn-add-task');
    if (addBtn) {
        addBtn.addEventListener('click', () => openTaskModal(null));
    }

    // Card click → open edit modal
    _container.querySelectorAll('.kanban-card').forEach(card => {
        card.addEventListener('click', () => {
            const id   = card.dataset.id;
            const task = _tasks.find(t => String(t.id) === String(id));
            if (task) openTaskModal(task);
        });
    });

    // Drag & drop
    _container.querySelectorAll('.kanban-card').forEach(card => {
        card.addEventListener('dragstart', (e) => {
            _dragTaskId = card.dataset.id;
            card.classList.add('dragging');
            e.dataTransfer.effectAllowed = 'move';
        });
        card.addEventListener('dragend', () => {
            card.classList.remove('dragging');
            _dragTaskId = null;
        });
    });

    _container.querySelectorAll('.kanban-cards').forEach(zone => {
        zone.addEventListener('dragover', (e) => {
            e.preventDefault();
            e.dataTransfer.dropEffect = 'move';
            zone.classList.add('drag-over');
        });
        zone.addEventListener('dragleave', (e) => {
            if (!zone.contains(e.relatedTarget)) {
                zone.classList.remove('drag-over');
            }
        });
        zone.addEventListener('drop', async (e) => {
            e.preventDefault();
            zone.classList.remove('drag-over');
            const newStatus = zone.dataset.col;
            if (!_dragTaskId || !newStatus) return;

            const task = _tasks.find(t => String(t.id) === String(_dragTaskId));
            if (!task || task.status === newStatus) return;

            try {
                await api.put('/items/' + task.id, { status: newStatus });
                task.status = newStatus;
                showToast('任务状态已更新', 'success');
                window.dispatchEvent(new CustomEvent('pendo-data-changed'));
                renderPage();
            } catch (err) {
                showToast('更新失败：' + err.message, 'error');
            }
        });
    });
}

// ── modal ─────────────────────────────────────────────────────────────────────

function openTaskModal(existing = null) {
    const isEdit  = !!existing;
    const title   = isEdit ? '编辑任务' : '添加任务';

    const fields = TASK_FIELDS.map(f => {
        let value = f.value !== undefined ? f.value : '';
        if (isEdit) {
            if (f.name === 'due_time') {
                value = toDatetimeLocal(existing.due_time);
            } else if (f.name === 'priority') {
                value = existing.priority || 3;
            } else {
                value = existing[f.name] !== undefined && existing[f.name] !== null
                    ? existing[f.name]
                    : (f.value !== undefined ? f.value : '');
            }
        }
        return { ...f, value };
    });

    const bodyHTML = `<form id="task-form">${buildFormHTML(fields)}</form>`;
    const deleteBtn = isEdit
        ? `<button class="btn btn-danger btn-sm" id="modal-delete" style="margin-right:auto;">删除</button>`
        : '';
    const footer = `
        ${deleteBtn}
        <button class="btn btn-secondary" id="modal-cancel">取消</button>
        <button class="btn btn-primary"  id="modal-save">保存</button>
    `;

    const content = showModal(title, bodyHTML, { footer });
    initFormInteractions(content);

    content.querySelector('#modal-cancel').onclick = closeModal;

    if (isEdit) {
        content.querySelector('#modal-delete').onclick = async () => {
            const confirmed = window.confirm('确定要删除这个任务吗？');
            if (!confirmed) return;
            try {
                await api.delete('/items/' + existing.id);
                showToast('任务已删除', 'success');
                closeModal();
                window.dispatchEvent(new CustomEvent('pendo-data-changed'));
                await loadAndRender();
            } catch (err) {
                showToast('删除失败：' + err.message, 'error');
            }
        };
    }

    content.querySelector('#modal-save').onclick = async () => {
        const form = content.querySelector('#task-form');
        const data = getFormData(form);

        if (!data.title) {
            showToast('请填写标题', 'warning');
            return;
        }

        // Default priority if not selected
        if (!data.priority) data.priority = 3;

        try {
            if (isEdit) {
                await api.put('/items/' + existing.id, data);
                showToast('任务已更新', 'success');
            } else {
                await api.post('/items', { type: 'task', status: 'todo', ...data });
                showToast('任务已添加', 'success');
            }
            closeModal();
            window.dispatchEvent(new CustomEvent('pendo-data-changed'));
            await loadAndRender();
        } catch (err) {
            showToast('保存失败：' + err.message, 'error');
        }
    };
}

// ── data load ─────────────────────────────────────────────────────────────────

async function loadAndRender() {
    try {
        _tasks = await fetchTasks();
    } catch (err) {
        _tasks = [];
        showToast('加载任务失败：' + err.message, 'error');
    }
    renderPage();
}

// ── page module exports ───────────────────────────────────────────────────────

export function render(container) {
    _container = container;
    _tasks     = [];

    // Render skeleton immediately, then load data
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
    _tasks     = [];
}

export function onRouteEnter(_params) {
    // Nothing special; render() is called by the router
}
