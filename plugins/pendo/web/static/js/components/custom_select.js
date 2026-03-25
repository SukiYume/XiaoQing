const CHEVRON_SVG = `<svg class="pselect-chevron" width="13" height="13" viewBox="0 0 13 13" fill="none">
    <path d="M3 5l3.5 3.5L10 5" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round"/>
</svg>`;

let docClickAttached = false;

function escAttr(value) {
    return String(value ?? '')
        .replace(/&/g, '&amp;')
        .replace(/"/g, '&quot;')
        .replace(/</g, '&lt;')
        .replace(/>/g, '&gt;');
}

function escHtml(value) {
    return String(value ?? '')
        .replace(/&/g, '&amp;')
        .replace(/</g, '&lt;')
        .replace(/>/g, '&gt;')
        .replace(/"/g, '&quot;');
}

function normalizeOptions(options = []) {
    return options.map(option => {
        if (typeof option === 'string') {
            return { value: option, label: option };
        }
        return {
            value: option?.value ?? '',
            label: option?.label ?? option?.value ?? '',
        };
    });
}

function closeAllCustomSelects(except = null) {
    document.querySelectorAll('.pselect.pselect-open').forEach(el => {
        if (el === except) return;
        el.classList.remove('pselect-open');
        const trigger = el.querySelector('.pselect-trigger');
        if (trigger) {
            trigger.setAttribute('aria-expanded', 'false');
        }
    });
}

export function renderCustomSelect({
    id,
    name = '',
    options = [],
    selected = '',
    className = '',
    placeholder = '请选择',
}) {
    const normalized = normalizeOptions(options);
    const current = normalized.find(o => String(o.value) === String(selected))
        || normalized[0]
        || { value: '', label: placeholder };
    const optionHtml = normalized.map(option => {
        const isSelected = String(option.value) === String(current.value);
        return `
            <div class="pselect-option${isSelected ? ' pselect-selected' : ''}" data-value="${escAttr(option.value)}">
                ${escHtml(option.label)}
            </div>`;
    }).join('');

    const hiddenInput = name
        ? `<input type="hidden" class="form-input pselect-input" name="${escAttr(name)}" value="${escAttr(current.value)}">`
        : '';

    return `
        <div class="pselect ${className}" id="${escAttr(id)}" data-value="${escAttr(current.value)}">
            ${hiddenInput}
            <div class="pselect-trigger" role="button" tabindex="0" aria-haspopup="listbox" aria-expanded="false">
                <span class="pselect-label">${escHtml(current.label)}</span>
                ${CHEVRON_SVG}
            </div>
            <div class="pselect-panel" role="listbox">${optionHtml}</div>
        </div>`;
}

export function initCustomSelects(container, callbacks = {}) {
    if (!container) return;

    if (!docClickAttached) {
        document.addEventListener('click', () => closeAllCustomSelects());
        docClickAttached = true;
    }

    container.querySelectorAll('.pselect').forEach(selectEl => {
        const callback = callbacks[selectEl.id];
        if (callback) {
            selectEl._onCustomSelectChange = callback;
        }

        if (selectEl.dataset.initialized === 'true') return;
        selectEl.dataset.initialized = 'true';

        const trigger = selectEl.querySelector('.pselect-trigger');
        const panel = selectEl.querySelector('.pselect-panel');
        const label = selectEl.querySelector('.pselect-label');
        const input = selectEl.querySelector('.pselect-input');

        if (!trigger || !panel || !label) return;

        const setExpanded = (expanded) => {
            trigger.setAttribute('aria-expanded', expanded ? 'true' : 'false');
        };

        trigger.addEventListener('click', (event) => {
            event.stopPropagation();
            const willOpen = !selectEl.classList.contains('pselect-open');
            closeAllCustomSelects(selectEl);
            selectEl.classList.toggle('pselect-open', willOpen);
            setExpanded(willOpen);
        });

        trigger.addEventListener('keydown', (event) => {
            if (event.key !== 'Enter' && event.key !== ' ') return;
            event.preventDefault();
            trigger.click();
        });

        panel.addEventListener('click', (event) => {
            event.stopPropagation();
            const optionEl = event.target.closest('.pselect-option');
            if (!optionEl) return;

            const value = optionEl.dataset.value ?? '';
            const text = optionEl.textContent.trim();
            selectEl.dataset.value = value;
            label.textContent = text;
            if (input) input.value = value;

            panel.querySelectorAll('.pselect-option').forEach(optionNode => {
                optionNode.classList.toggle('pselect-selected', optionNode === optionEl);
            });

            selectEl.classList.remove('pselect-open');
            setExpanded(false);

            const cb = selectEl._onCustomSelectChange;
            if (cb) cb(value, selectEl);
        });
    });
}
