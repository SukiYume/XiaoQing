let currentOnClose = null;

export function showModal(title, contentHTML, options = {}) {
    const overlay = document.getElementById('modal-overlay');
    const content = document.getElementById('modal-content');

    content.innerHTML = `
        <div class="modal-header">
            <h3>${title}</h3>
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
