import { api } from '../api.js';
import { showToast } from '../components/toast.js';
import { showModal, closeModal } from '../components/modal.js';
import { buildFormHTML, getFormData, initFormInteractions } from '../components/form.js';
import { renderPagination } from '../components/pagination.js';

// ── constants ─────────────────────────────────────────────────────────────────

const PAGE_SIZE = 20;

const NOTE_FIELDS = [
    { name: 'title',    label: '标题', type: 'text',     required: true },
    { name: 'content',  label: '内容', type: 'textarea', rows: 8 },
    { name: 'category', label: '分类', type: 'text',     placeholder: '未分类' },
    { name: 'tags',     label: '标签', type: 'text',     placeholder: '逗号分隔，如：工作,学习' },
];

const CSS_ID = 'pendo-notes-styles';

// ── module state ──────────────────────────────────────────────────────────────

let _container          = null;
let _items              = [];
let _total              = 0;
let _page               = 1;
let _filterCategory     = '';
let _filterTag          = '';
let _dataChangedHandler = null;

// ── helpers ───────────────────────────────────────────────────────────────────

function padZ(n) { return String(n).padStart(2, '0'); }

function formatDate(iso) {
    if (!iso) return '';
    const d = new Date(iso);
    return `${d.getFullYear()}-${padZ(d.getMonth() + 1)}-${padZ(d.getDate())}`;
}

function contentPreview(content) {
    if (!content) return '';
    const text = content.trim();
    if (text.length <= 100) return text;
    return text.slice(0, 100) + '...';
}

function tagsToArray(tagsStr) {
    if (!tagsStr) return [];
    return tagsStr.split(',').map(t => t.trim()).filter(Boolean);
}

function tagsToString(tagsArr) {
    if (!Array.isArray(tagsArr)) return '';
    return tagsArr.join(', ');
}

function getCategories() {
    const cats = new Set();
    _items.forEach(item => { if (item.category) cats.add(item.category); });
    return Array.from(cats).sort();
}

function filteredItems() {
    return _items.filter(item => {
        if (_filterCategory && item.category !== _filterCategory) return false;
        if (_filterTag) {
            const tags = Array.isArray(item.tags) ? item.tags : [];
            const tagLower = _filterTag.toLowerCase();
            if (!tags.some(t => t.toLowerCase().includes(tagLower))) return false;
        }
        return true;
    });
}

// ── CSS ───────────────────────────────────────────────────────────────────────

function ensureStyles() {
    if (document.getElementById(CSS_ID)) return;
    const style = document.createElement('style');
    style.id = CSS_ID;
    style.textContent = `
        .notes-page { padding: 24px; }

        .notes-page-header {
            display: flex;
            align-items: center;
            justify-content: space-between;
            margin-bottom: 16px;
            flex-wrap: wrap;
            gap: 12px;
        }

        .notes-filter-bar {
            display: flex;
            gap: 10px;
            align-items: center;
            flex-wrap: wrap;
            background: var(--color-surface);
            border: 1px solid var(--color-border);
            border-radius: var(--radius);
            padding: 10px 14px;
            margin-bottom: 20px;
        }
        .notes-filter-bar label {
            font-size: 12px;
            font-weight: 500;
            color: var(--color-text-secondary);
            white-space: nowrap;
        }
        .notes-filter-bar select {
            font-size: 13px;
            height: 30px;
            padding: 0 30px 0 12px;
            border-radius: 20px;
            border: 1px solid var(--color-border);
            background-position: right 9px center;
            font-weight: 500;
            min-width: 80px;
        }
        .notes-filter-bar input[type="text"] {
            font-size: 13px;
            height: 30px;
            padding: 0 10px;
            border-radius: 20px;
            border: 1px solid var(--color-border);
            outline: none;
        }
        .notes-filter-bar select:focus,
        .notes-filter-bar input[type="text"]:focus {
            border-color: var(--color-notes, #3B82F6);
            box-shadow: 0 0 0 3px rgba(59,130,246,0.1);
            outline: none;
        }
        .notes-filter-tag { width: 160px; cursor: text !important; }

        .notes-grid {
            display: grid;
            grid-template-columns: repeat(3, 1fr);
            gap: 16px;
            margin-bottom: 20px;
        }
        @media (max-width: 900px) {
            .notes-grid { grid-template-columns: repeat(2, 1fr); }
        }
        @media (max-width: 560px) {
            .notes-grid { grid-template-columns: 1fr; }
        }

        .note-card {
            background: var(--color-surface);
            border: 1px solid var(--color-border);
            border-radius: var(--radius);
            display: flex;
            flex-direction: column;
            transition: box-shadow 0.15s, border-color 0.15s;
            overflow: hidden;
        }
        .note-card:hover {
            box-shadow: 0 4px 12px rgba(0,0,0,0.10);
            border-color: var(--color-notes, #3B82F6);
        }
        .note-card-body {
            flex: 1;
            padding: 14px 16px 10px;
            cursor: pointer;
        }
        .note-card-header-row {
            display: flex;
            align-items: flex-start;
            justify-content: space-between;
            gap: 8px;
            margin-bottom: 6px;
        }
        .note-card-title {
            font-size: 15px;
            font-weight: 600;
            color: var(--color-text);
            overflow: hidden;
            text-overflow: ellipsis;
            white-space: nowrap;
            flex: 1;
        }
        .note-card-category {
            font-size: 11px;
            background: var(--color-notes, #3B82F6);
            color: #fff;
            border-radius: 4px;
            padding: 2px 7px;
            white-space: nowrap;
            flex-shrink: 0;
            font-weight: 500;
        }
        .note-card-preview {
            font-size: 13px;
            color: var(--color-text-secondary);
            line-height: 1.55;
            margin-bottom: 10px;
            word-break: break-word;
            min-height: 40px;
        }
        .note-card-footer {
            padding: 8px 16px 12px;
            display: flex;
            align-items: center;
            justify-content: space-between;
            gap: 8px;
            flex-wrap: wrap;
        }
        .note-card-tags {
            display: flex;
            flex-wrap: wrap;
            gap: 4px;
        }
        .note-tag {
            font-size: 11px;
            background: var(--color-border);
            color: var(--color-text-secondary);
            border-radius: 4px;
            padding: 2px 6px;
        }
        .note-card-date {
            font-size: 11px;
            color: var(--color-text-tertiary);
            white-space: nowrap;
        }

        .notes-empty {
            text-align: center;
            padding: 60px 0;
            color: var(--color-text-tertiary);
            font-size: 14px;
        }

        .notes-pagination {
            margin-top: 8px;
        }

        .note-view-content {
            white-space: pre-wrap;
            word-break: break-word;
            font-size: 14px;
            color: var(--color-text);
            line-height: 1.7;
            max-height: 60vh;
            overflow-y: auto;
            padding: 2px 0;
        }
        .note-view-meta {
            display: flex;
            flex-wrap: wrap;
            gap: 8px;
            align-items: center;
            padding: 10px 0 14px;
            border-bottom: 1px solid var(--color-border);
            margin-bottom: 14px;
        }
    `;
    document.head.appendChild(style);
}

// ── API ───────────────────────────────────────────────────────────────────────

async function fetchItems(page) {
    const params = { type: 'note', page, page_size: PAGE_SIZE };
    if (_filterCategory) params.category = _filterCategory;
    const res = await api.get('/items', params);
    return {
        items: (res.data && res.data.items) ? res.data.items : [],
        total: (res.data && res.data.total != null) ? res.data.total : 0,
    };
}

// ── render ────────────────────────────────────────────────────────────────────

function renderFilterBar() {
    const categories = getCategories();
    const catOptions = ['', ...categories]
        .map(c => `<option value="${c}"${_filterCategory === c ? ' selected' : ''}>${c || '全部分类'}</option>`)
        .join('');

    return `
        <div class="notes-filter-bar">
            <label>分类：</label>
            <select id="notes-filter-category">${catOptions}</select>
            <label style="margin-left:8px;">标签：</label>
            <input type="text"
                   id="notes-filter-tag"
                   class="notes-filter-tag"
                   placeholder="输入标签筛选"
                   value="${_filterTag.replace(/"/g, '&quot;')}">
        </div>
    `;
}

function renderNoteCard(note) {
    const preview     = contentPreview(note.content);
    const tags        = Array.isArray(note.tags) ? note.tags : [];
    const tagsHTML    = tags.map(t => `<span class="note-tag">${t}</span>`).join('');
    const dateLabel   = formatDate(note.updated_at || note.created_at);
    const catBadge    = note.category
        ? `<span class="note-card-category">${note.category}</span>`
        : '';

    return `
        <div class="note-card" data-id="${note.id}">
            <div class="note-card-body" data-action="view" data-id="${note.id}">
                <div class="note-card-header-row">
                    <span class="note-card-title" title="${(note.title || '').replace(/"/g, '&quot;')}">${note.title || '(无标题)'}</span>
                    ${catBadge}
                </div>
                <div class="note-card-preview">${preview || '<span style="opacity:0.45;">（无内容）</span>'}</div>
            </div>
            <div class="note-card-footer">
                <div class="note-card-tags">${tagsHTML}</div>
                <span class="note-card-date">${dateLabel}</span>
            </div>
        </div>
    `;
}

function renderGrid(items) {
    if (items.length === 0) {
        return `<div class="notes-empty">暂无笔记，点击右上角添加一条吧</div>`;
    }
    return `
        <div class="notes-grid" id="notes-grid">
            ${items.map(renderNoteCard).join('')}
        </div>
    `;
}

function renderPage() {
    if (!_container) return;

    ensureStyles();
    const visible = filteredItems();

    _container.innerHTML = `
        <div class="notes-page">
            <div class="notes-page-header">
                <div>
                    <h2 style="font-size:20px;font-weight:700;color:var(--color-notes,#3B82F6);">📝 笔记</h2>
                    <p style="font-size:13px;color:var(--color-text-secondary);margin-top:2px;">随手记录你的想法与知识</p>
                </div>
                <button class="btn btn-primary" id="btn-add-note">＋ 添加笔记</button>
            </div>

            ${renderFilterBar()}

            <div id="notes-list-area">
                ${renderGrid(visible)}
            </div>

            <div id="notes-pagination" class="notes-pagination"></div>
        </div>
    `;

    const paginationEl = _container.querySelector('#notes-pagination');
    if (paginationEl) {
        renderPagination(paginationEl, {
            page:     _page,
            pageSize: PAGE_SIZE,
            total:    _total,
            onChange: async (newPage) => {
                _page = newPage;
                await loadAndRender();
            },
        });
    }

    attachListeners();
}

// ── listeners ─────────────────────────────────────────────────────────────────

function attachListeners() {
    if (!_container) return;

    const addBtn = _container.querySelector('#btn-add-note');
    if (addBtn) {
        addBtn.addEventListener('click', () => openNoteFormModal(null));
    }

    const catSel = _container.querySelector('#notes-filter-category');
    if (catSel) {
        catSel.addEventListener('change', async () => {
            _filterCategory = catSel.value;
            _page = 1;
            await loadAndRender();
        });
    }

    const tagInput = _container.querySelector('#notes-filter-tag');
    if (tagInput) {
        let debounceTimer = null;
        tagInput.addEventListener('input', () => {
            clearTimeout(debounceTimer);
            debounceTimer = setTimeout(() => {
                _filterTag = tagInput.value.trim();
                renderPage();
            }, 250);
        });
    }

    const listArea = _container.querySelector('#notes-list-area');
    if (listArea) {
        listArea.addEventListener('click', (e) => {
            const viewTarget = e.target.closest('[data-action="view"]');
            if (viewTarget) {
                const id   = viewTarget.dataset.id;
                const note = _items.find(n => String(n.id) === String(id));
                if (note) openNoteViewModal(note);
            }
        });
    }
}

// ── view modal (read-only) ────────────────────────────────────────────────────

function openNoteViewModal(note) {
    const tags     = Array.isArray(note.tags) ? note.tags : [];
    const tagsHTML = tags.map(t => `<span class="note-tag" style="font-size:12px;">${t}</span>`).join('');
    const catBadge = note.category
        ? `<span class="note-card-category" style="font-size:12px;">${note.category}</span>`
        : '';
    const dateLabel = formatDate(note.updated_at || note.created_at);

    const bodyHTML = `
        <div class="note-view-meta">
            ${catBadge}
            <div style="display:flex;flex-wrap:wrap;gap:4px;">${tagsHTML}</div>
            <span style="font-size:12px;color:var(--color-text-tertiary);margin-left:auto;">更新于 ${dateLabel}</span>
        </div>
        <div class="note-view-content">${(note.content || '').replace(/</g, '&lt;').replace(/>/g, '&gt;')}</div>
    `;

    const footer = `
        <button class="btn btn-danger btn-sm" id="modal-delete" style="margin-right:auto;">删除</button>
        <button class="btn btn-secondary" id="modal-cancel">关闭</button>
        <button class="btn btn-primary"   id="modal-edit">编辑</button>
    `;

    const content = showModal(note.title || '(无标题)', bodyHTML, { footer });

    content.querySelector('#modal-cancel').onclick = closeModal;

    content.querySelector('#modal-edit').onclick = () => {
        closeModal();
        openNoteFormModal(note);
    };

    content.querySelector('#modal-delete').onclick = async () => {
        const confirmed = window.confirm('确定要删除这条笔记吗？');
        if (!confirmed) return;
        try {
            await api.delete('/items/' + note.id);
            showToast('笔记已删除', 'success');
            closeModal();
            window.dispatchEvent(new CustomEvent('pendo-data-changed'));
            await loadAndRender();
        } catch (err) {
            showToast('删除失败：' + err.message, 'error');
        }
    };
}

// ── add/edit form modal ───────────────────────────────────────────────────────

function openNoteFormModal(existing = null) {
    const isEdit = !!existing;
    const title  = isEdit ? '编辑笔记' : '添加笔记';

    const fields = NOTE_FIELDS.map(f => {
        let value = '';
        if (isEdit) {
            if (f.name === 'tags') {
                value = tagsToString(existing.tags);
            } else {
                value = existing[f.name] !== undefined && existing[f.name] !== null
                    ? existing[f.name]
                    : '';
            }
        }
        return { ...f, value };
    });

    const bodyHTML = `<form id="note-form">${buildFormHTML(fields)}</form>`;
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

    content.querySelector('#modal-cancel').onclick = closeModal;

    if (isEdit) {
        content.querySelector('#modal-delete').onclick = async () => {
            const confirmed = window.confirm('确定要删除这条笔记吗？');
            if (!confirmed) return;
            try {
                await api.delete('/items/' + existing.id);
                showToast('笔记已删除', 'success');
                closeModal();
                window.dispatchEvent(new CustomEvent('pendo-data-changed'));
                await loadAndRender();
            } catch (err) {
                showToast('删除失败：' + err.message, 'error');
            }
        };
    }

    content.querySelector('#modal-save').onclick = async () => {
        const form = content.querySelector('#note-form');
        const data = getFormData(form);

        if (!data.title) {
            showToast('请填写标题', 'warning');
            return;
        }

        // Convert comma-separated tags string to array
        data.tags = tagsToArray(data.tags || '');

        try {
            if (isEdit) {
                await api.put('/items/' + existing.id, data);
                showToast('笔记已更新', 'success');
            } else {
                await api.post('/items', { type: 'note', ...data });
                showToast('笔记已添加', 'success');
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
        const result = await fetchItems(_page);
        _items = result.items;
        _total = result.total;
    } catch (err) {
        _items = [];
        _total = 0;
        showToast('加载笔记失败：' + err.message, 'error');
    }
    renderPage();
}

// ── page module exports ───────────────────────────────────────────────────────

export function render(container) {
    _container = container;
    _items     = [];
    _total     = 0;
    _page      = 1;

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
    _items     = [];
    _total     = 0;
}

export function onRouteEnter(_params) {
    // Nothing special; render() is called by the router
}
