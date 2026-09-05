/** Pendo Web 单实例模态框、可信内容边界和确认对话框。 */

import { escapeHtml } from '../utils/ui.js';

const SAFE_HTML_VALUE    = Symbol('pendo.safe-html');
const FOCUSABLE_SELECTOR = [
    'button:not([disabled])',
    'a[href]',
    'input:not([disabled])',
    'select:not([disabled])',
    'textarea:not([disabled])',
    '[tabindex]:not([tabindex="-1"])',
].join(',');

let modalOpen      = false;
let currentOnClose = null;
let previousFocus  = null;

/**
 * 标记已经由调用方完成逐值转义的内部 HTML。
 * 此函数只建立显式信任边界，不会替调用方清洗任意外部 HTML。
 */
export function safeHtml(value) {
    if (typeof value !== 'string') {
        throw new TypeError('safeHtml requires a string');
    }
    return Object.freeze({
        [SAFE_HTML_VALUE]: true,
        value,
    });
}

function appendModalContent(parent, value, label) {
    if (typeof Node !== 'undefined' && value instanceof Node) {
        parent.appendChild(value);
        return;
    }
    if (!value || value[SAFE_HTML_VALUE] !== true || typeof value.value !== 'string') {
        throw new TypeError(`${label} must be a DOM Node or safeHtml(...) value`);
    }

    const template = document.createElement('template');
    template.innerHTML = value.value;
    parent.appendChild(template.content.cloneNode(true));
}

function handleModalKeydown(event) {
    if (!modalOpen || event.isComposing) return;
    if (event.key === 'Escape') {
        event.preventDefault();
        closeModal();
        return;
    }
    if (event.key !== 'Tab') return;

    const content   = document.getElementById('modal-content');
    const focusable = [...(content?.querySelectorAll(FOCUSABLE_SELECTOR) ?? [])];
    if (!focusable.length) {
        event.preventDefault();
        return;
    }

    const first  = focusable[0];
    const last   = focusable.at(-1);
    const active = document.activeElement;
    if (!focusable.includes(active)) {
        event.preventDefault();
        first.focus();
    } else if (event.shiftKey && active === first) {
        event.preventDefault();
        last.focus();
    } else if (!event.shiftKey && active === last) {
        event.preventDefault();
        first.focus();
    }
}

export function showModal(title, contentValue, options = {}) {
    if (options == null) options = {};
    if (typeof options !== 'object' || Array.isArray(options)) {
        throw new TypeError('modal options must be an object');
    }
    if (options.onClose != null && typeof options.onClose !== 'function') {
        throw new TypeError('modal onClose must be a function');
    }
    if (modalOpen) {
        throw new Error('已有模态框打开，请先关闭后再打开新模态框');
    }

    const overlay = document.getElementById('modal-overlay');
    const content = document.getElementById('modal-content');
    if (!overlay || !content) throw new Error('模态框挂载节点缺失');

    const header = document.createElement('div');
    header.className = 'modal-header';
    const titleElement = document.createElement('h3');
    titleElement.id = 'modal-title';
    titleElement.textContent = String(title ?? '');
    const closeButton = document.createElement('button');
    closeButton.type = 'button';
    closeButton.className = 'btn btn-icon modal-close';
    closeButton.setAttribute('aria-label', '关闭');
    closeButton.textContent = '×';
    header.append(titleElement, closeButton);

    const body = document.createElement('div');
    body.className = 'modal-body';
    appendModalContent(body, contentValue, 'modal content');

    const children = [header, body];
    if (options.footer != null) {
        const footer = document.createElement('div');
        footer.className = 'modal-footer';
        appendModalContent(footer, options.footer, 'modal footer');
        children.push(footer);
    }

    const focusTarget = document.activeElement;
    content.replaceChildren(...children);
    overlay.style.display = 'flex';
    overlay.setAttribute('aria-labelledby', 'modal-title');
    document.body.classList.add('modal-open');
    document.addEventListener('keydown', handleModalKeydown);
    previousFocus = focusTarget;
    currentOnClose = options.onClose ?? null;
    modalOpen = true;

    closeButton.onclick = closeModal;
    overlay.onclick = (event) => {
        if (event.target === overlay) closeModal();
    };
    closeButton.focus();
    return content;
}

export function closeModal() {
    if (!modalOpen) return;

    const overlay     = document.getElementById('modal-overlay');
    const content     = document.getElementById('modal-content');
    const onClose     = currentOnClose;
    const focusTarget = previousFocus;

    // 先清空单实例状态，保证 onClose 可以安全重入并打开下一弹窗。
    modalOpen = false;
    currentOnClose = null;
    previousFocus = null;
    document.removeEventListener('keydown', handleModalKeydown);
    document.body.classList.remove('modal-open');
    if (overlay) {
        overlay.style.display = 'none';
        overlay.onclick = null;
        overlay.removeAttribute('aria-labelledby');
    }
    content?.replaceChildren();

    if (focusTarget?.isConnected && typeof focusTarget.focus === 'function') {
        focusTarget.focus();
    }
    if (!onClose) return;
    try {
        const result = onClose();
        result?.catch?.((cause) => {
            console.error('Pendo Web 模态框关闭回调失败:', cause);
        });
    } catch (cause) {
        console.error('Pendo Web 模态框关闭回调失败:', cause);
    }
}

export function showConfirmModal(options = {}) {
    if (options == null) options = {};
    if (typeof options !== 'object' || Array.isArray(options)) {
        throw new TypeError('confirm modal options must be an object');
    }
    const {
        title = '确认操作',
        message = '确定要继续吗？',
        confirmText = '确认',
        cancelText = '取消',
        tone = 'danger',
    } = options;

    return new Promise((resolve) => {
        let settled  = false;
        const finish = (result) => {
            if (settled) return;
            settled = true;
            resolve(result);
            closeModal();
        };
        const isDanger  = tone === 'danger';
        const iconClass = isDanger ? 'confirm-modal-icon-danger' : 'confirm-modal-icon-info';
        const bodyHTML  = `
            <div class="confirm-modal-body">
                <div class="confirm-modal-icon ${iconClass}" aria-hidden="true">${isDanger ? '🗑️' : 'ℹ️'}</div>
                <p class="confirm-modal-message">${escapeHtml(message)}</p>
            </div>`;
        const footerHTML = `
            <button type="button" class="btn btn-secondary" id="confirm-cancel">${escapeHtml(cancelText)}</button>
            <button type="button" class="btn ${isDanger ? 'btn-danger' : 'btn-primary'}" id="confirm-ok">${escapeHtml(confirmText)}</button>`;

        const content = showModal(title, safeHtml(bodyHTML), {
            footer: safeHtml(footerHTML),
            onClose: () => {
                if (settled) return;
                settled = true;
                resolve(false);
            },
        });
        content.querySelector('#confirm-cancel').onclick = () => finish(false);
        content.querySelector('#confirm-ok').onclick = () => finish(true);
    });
}
