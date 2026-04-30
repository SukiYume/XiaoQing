import { escapeHtml } from '../utils/ui.js';

let currentOnClose = null;

export function showModal(title, contentHTML, options = {}) {
    const overlay = document.getElementById('modal-overlay');
    const content = document.getElementById('modal-content');

    content.innerHTML = `
        <div class="modal-header">
            <h3>${escapeHtml(title)}</h3>
            <button class="btn btn-icon modal-close">&times;</button>
        </div>
        <div class="modal-body">${contentHTML}</div>
        ${options.footer ? `<div class="modal-footer">${options.footer}</div>` : ''}
    `;

    overlay.style.display = 'flex';
    currentOnClose = options.onClose || null;

    // Close handlers
    content.querySelector('.modal-close').onclick = closeModal;
    overlay.onclick = (e) => { if (e.target === overlay) closeModal(); };

    // Return content element for attaching event listeners
    return content;
}

export function closeModal() {
    document.getElementById('modal-overlay').style.display = 'none';
    if (currentOnClose) currentOnClose();
    currentOnClose = null;
}

export function showConfirmModal({
    title = '确认操作',
    message = '确定要继续吗？',
    confirmText = '确认',
    cancelText = '取消',
    tone = 'danger',
} = {}) {
    return new Promise((resolve) => {
        let settled = false;
        const finish = (result) => {
            if (settled) return;
            settled = true;
            resolve(result);
            closeModal();
        };

        const bodyHTML = `
            <div style="display:flex;flex-direction:column;gap:12px;padding:4px 0;">
                <div style="width:44px;height:44px;border-radius:14px;display:flex;align-items:center;justify-content:center;
                    background:${tone === 'danger' ? 'rgba(239,68,68,0.10)' : 'rgba(59,130,246,0.10)'};
                    color:${tone === 'danger' ? 'var(--color-ledger)' : 'var(--color-primary, #2563eb)'};
                    font-size:22px;">${tone === 'danger' ? '🗑️' : 'ℹ️'}</div>
                <p style="margin:0;font-size:14px;line-height:1.7;color:var(--color-text);">${escapeHtml(message)}</p>
            </div>`;

        const footer = `
            <button class="btn btn-secondary" id="confirm-cancel">${escapeHtml(cancelText)}</button>
            <button class="btn ${tone === 'danger' ? 'btn-danger' : 'btn-primary'}" id="confirm-ok">${escapeHtml(confirmText)}</button>`;

        const content = showModal(title, bodyHTML, {
            footer,
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
