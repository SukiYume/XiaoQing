export function renderPagination(container, { page, pageSize, total, onChange }) {
    const totalPages = Math.ceil(total / pageSize) || 1;
    if (totalPages <= 1) { container.innerHTML = ''; return; }

    const div = document.createElement('div');
    div.className = 'pagination';

    const prevDisabled = page <= 1 ? ' disabled' : '';
    const nextDisabled = page >= totalPages ? ' disabled' : '';

    div.innerHTML = `
        <button class="page-btn"${prevDisabled} data-page="${page - 1}">‹ 上一页</button>
        <span class="page-info">${page} / ${totalPages}</span>
        <button class="page-btn"${nextDisabled} data-page="${page + 1}">下一页 ›</button>
    `;

    div.querySelectorAll('.page-btn:not([disabled])').forEach(btn => {
        btn.onclick = () => onChange(parseInt(btn.dataset.page));
    });

    container.innerHTML = '';
    container.appendChild(div);
}
