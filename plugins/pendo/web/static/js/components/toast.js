import { escapeHtml } from '../utils/ui.js';

export function showToast(message, type = 'info', duration = 3000, options = {}) {
    const container = document.getElementById('toast-container');
    const toast = document.createElement('div');
    const safeType = String(type || 'info').replace(/[^a-zA-Z0-9_-]/g, '') || 'info';
    toast.className = `toast toast-${safeType}`;

    let html = `<span class="toast-message">${escapeHtml(message)}</span>`;
    if (options.undoCallback) {
        html += `<button class="toast-undo">撤销</button>`;
    }
    html += `<button class="toast-dismiss">&times;</button>`;
    toast.innerHTML = html;

    container.appendChild(toast);

    // Trigger animation
    requestAnimationFrame(() => toast.classList.add('toast-show'));

    const dismiss = () => {
        toast.classList.remove('toast-show');
        setTimeout(() => toast.remove(), 300);
    };

    toast.querySelector('.toast-dismiss').onclick = dismiss;
    if (options.undoCallback) {
        toast.querySelector('.toast-undo').onclick = () => {
            options.undoCallback();
            dismiss();
        };
    }

    if (duration > 0) {
        setTimeout(dismiss, duration);
    }
}
