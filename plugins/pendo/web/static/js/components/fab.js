import { showModal, closeModal } from './modal.js';
import { buildFormHTML, getFormData, initFormInteractions } from './form.js';
import { api } from '../api.js';
import { showToast } from './toast.js';

const FAB_ITEMS = [
    { type: 'event', label: '新日程', icon: '🗓️', color: 'var(--color-events)' },
    { type: 'task', label: '新待办', icon: '✅', color: 'var(--color-tasks)' },
    { type: 'ledger', label: '记一笔', icon: '💰', color: 'var(--color-ledger)' },
    { type: 'note', label: '新笔记', icon: '📝', color: 'var(--color-notes)' },
    { type: 'diary', label: '写日记', icon: '📔', color: 'var(--color-diary)' },
];

// Minimal form fields per type for quick-add
const QUICK_FORMS = {
    event: [
        { name: 'title', label: '标题', type: 'text', required: true },
        { name: 'start_time', label: '开始时间', type: 'datetime', required: true },
        { name: 'end_time', label: '结束时间', type: 'datetime' },
        { name: 'location', label: '地点', type: 'text' },
    ],
    task: [
        { name: 'title', label: '标题', type: 'text', required: true },
        { name: 'priority', label: '优先级', type: 'priority', value: 3 },
        { name: 'due_time', label: '截止时间', type: 'datetime' },
        { name: 'category', label: '分类', type: 'text', placeholder: '未分类' },
    ],
    ledger: [
        { name: 'direction', label: '类型', type: 'select', options: [{value: 'expense', label: '支出'}, {value: 'income', label: '收入'}], value: 'expense' },
        { name: 'amount', label: '金额', type: 'number', required: true, placeholder: '0.00' },
        { name: 'title', label: '摘要', type: 'text', required: true },
        { name: 'ledger_category', label: '分类', type: 'text', placeholder: '其他' },
    ],
    note: [
        { name: 'title', label: '标题', type: 'text', required: true },
        { name: 'content', label: '内容', type: 'textarea' },
        { name: 'category', label: '分类', type: 'text', placeholder: '未分类' },
    ],
    diary: [
        { name: 'diary_date', label: '日期', type: 'date', value: new Date().toISOString().slice(0, 10) },
        { name: 'content', label: '内容', type: 'textarea', required: true },
        { name: 'mood', label: '心情', type: 'mood' },
    ],
};

let isOpen = false;

export function renderFab(container) {
    container.innerHTML = `
        <div class="fab-wrapper">
            <div class="fab-menu" style="display: none;">
                ${FAB_ITEMS.map(item => `
                    <button class="fab-item" data-type="${item.type}" title="${item.label}">
                        <span>${item.icon}</span>
                        <span class="fab-item-label">${item.label}</span>
                    </button>
                `).join('')}
            </div>
            <button class="fab" title="快捷添加">＋</button>
        </div>
    `;

    const fab = container.querySelector('.fab');
    const menu = container.querySelector('.fab-menu');

    fab.onclick = () => {
        isOpen = !isOpen;
        menu.style.display = isOpen ? 'flex' : 'none';
        fab.textContent = isOpen ? '✕' : '＋';
        fab.classList.toggle('fab-open', isOpen);
    };

    container.querySelectorAll('.fab-item').forEach(btn => {
        btn.onclick = () => {
            openQuickAdd(btn.dataset.type);
            isOpen = false;
            menu.style.display = 'none';
            fab.textContent = '＋';
            fab.classList.remove('fab-open');
        };
    });

    // Close on outside click
    document.addEventListener('click', (e) => {
        if (isOpen && !container.contains(e.target)) {
            isOpen = false;
            menu.style.display = 'none';
            fab.textContent = '＋';
            fab.classList.remove('fab-open');
        }
    });
}

function openQuickAdd(type) {
    const fields = QUICK_FORMS[type];
    if (!fields) return;

    const typeLabels = { event: '日程', task: '待办', ledger: '记账', note: '笔记', diary: '日记' };
    const formHTML = buildFormHTML(fields);

    const modal = showModal(`新建${typeLabels[type]}`, formHTML, {
        footer: `<button class="btn btn-primary" id="quick-add-submit">保存</button>
                 <button class="btn btn-secondary" id="quick-add-cancel">取消</button>`,
    });

    initFormInteractions(modal);

    modal.querySelector('#quick-add-submit').onclick = async () => {
        const data = getFormData(modal);
        data.type = type;
        try {
            await api.post('/items', data);
            showToast('创建成功', 'success');
            closeModal();
            // Trigger page refresh if on relevant page
            window.dispatchEvent(new CustomEvent('pendo-data-changed', { detail: { type } }));
        } catch (e) {
            showToast(e.message, 'error');
        }
    };
    modal.querySelector('#quick-add-cancel').onclick = closeModal;
}
