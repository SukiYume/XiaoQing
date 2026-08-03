/** Pendo Web 列表分页的数值收敛、可访问标记和异步切页互斥。 */

function toInteger(value, fallback) {
    try {
        const number = Number(value);
        return Number.isFinite(number) ? Math.trunc(number) : fallback;
    } catch {
        return fallback;
    }
}

export function renderPagination(container, options = {}) {
    if (!container?.replaceChildren) throw new TypeError('分页容器无效');
    if (!options || typeof options !== 'object' || Array.isArray(options)) {
        throw new TypeError('分页参数必须是对象');
    }
    const { page, pageSize, total, onChange } = options;
    if (typeof onChange !== 'function') throw new TypeError('分页回调必须是函数');

    const safePageSize = Math.max(1, toInteger(pageSize, 1));
    const safeTotal = Math.max(0, toInteger(total, 0));
    const totalPages = Math.max(1, Math.ceil(safeTotal / safePageSize));
    const currentPage = Math.min(Math.max(1, toInteger(page, 1)), totalPages);
    if (totalPages <= 1) {
        container.replaceChildren();
        return;
    }

    const pagination = document.createElement('nav');
    pagination.className = 'pagination';
    pagination.setAttribute('aria-label', '分页');

    const previousButton = document.createElement('button');
    previousButton.type = 'button';
    previousButton.className = 'page-btn';
    previousButton.textContent = '‹ 上一页';
    previousButton.dataset.page = String(currentPage - 1);
    previousButton.disabled = currentPage <= 1;

    const pageInfo = document.createElement('span');
    pageInfo.className = 'page-info';
    pageInfo.setAttribute('aria-live', 'polite');
    pageInfo.textContent = `${currentPage} / ${totalPages}`;

    const nextButton = document.createElement('button');
    nextButton.type = 'button';
    nextButton.className = 'page-btn';
    nextButton.textContent = '下一页 ›';
    nextButton.dataset.page = String(currentPage + 1);
    nextButton.disabled = currentPage >= totalPages;

    const enabledButtons = [previousButton, nextButton].filter((button) => !button.disabled);
    let changing = false;
    for (const button of enabledButtons) {
        button.onclick = async () => {
            if (changing) return;
            changing = true;
            enabledButtons.forEach((candidate) => {
                candidate.disabled = true;
            });
            try {
                await onChange(Number(button.dataset.page));
            } catch (cause) {
                console.error('Pendo Web 分页切换失败:', cause);
            } finally {
                changing = false;
                enabledButtons.forEach((candidate) => {
                    candidate.disabled = false;
                });
            }
        };
    }

    pagination.append(previousButton, pageInfo, nextButton);
    container.replaceChildren(pagination);
}
