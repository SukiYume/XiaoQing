/** 无构建前端共用的可访问单选下拉框渲染与交互。 */

import { escapeAttr, escapeHtml } from '../utils/ui.js';

const CHEVRON_SVG = `<svg class="pselect-chevron" width="13" height="13" viewBox="0 0 13 13" fill="none" aria-hidden="true">
    <path d="M3 5l3.5 3.5L10 5" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round"/>
</svg>`;

const changeCallbacks = new WeakMap();
let documentClickAttached = false;

function normalizeOptions(options) {
    if (!Array.isArray(options)) return [];
    return options.map((option) => {
        if (typeof option !== 'object' || option === null) {
            const value = option ?? '';
            return { value, label: value };
        }
        return {
            value: option.value ?? '',
            label: option.label ?? option.value ?? '',
        };
    });
}

function normalizeClassName(className) {
    return String(className ?? '')
        .split(/\s+/)
        .filter((token) => /^[a-zA-Z0-9_-]+$/.test(token))
        .join(' ');
}

function closeCustomSelect(selectEl) {
    selectEl.classList.remove('pselect-open');
    const trigger = selectEl.querySelector('.pselect-trigger');
    const panel = selectEl.querySelector('.pselect-panel');
    trigger?.setAttribute('aria-expanded', 'false');
    trigger?.removeAttribute('aria-activedescendant');
    panel?.setAttribute('aria-hidden', 'true');
    panel?.querySelectorAll('.pselect-option').forEach((option) => {
        option.classList.remove('pselect-active');
    });
}

function closeAllCustomSelects(except = null) {
    document.querySelectorAll('.pselect.pselect-open').forEach((selectEl) => {
        if (selectEl !== except) closeCustomSelect(selectEl);
    });
}

function invokeChangeCallback(selectEl, value) {
    const callback = changeCallbacks.get(selectEl);
    if (!callback) return;

    try {
        const result = callback(value, selectEl);
        result?.catch?.((cause) => {
            console.error('Pendo Web 自定义选择回调失败:', cause);
        });
    } catch (cause) {
        console.error('Pendo Web 自定义选择回调失败:', cause);
    }
}

export function renderCustomSelect({
    id,
    name = '',
    options = [],
    selected = '',
    className = '',
    placeholder = '请选择',
    ariaLabel = '',
    labelledBy = '',
}) {
    const selectId = String(id ?? '').trim();
    if (!selectId) throw new TypeError('自定义选择框必须提供 id');

    const normalized = normalizeOptions(options);
    let selectedIndex = normalized.findIndex((option) => String(option.value) === String(selected));
    if (selectedIndex < 0 && normalized.length) selectedIndex = 0;
    const current = normalized[selectedIndex] ?? { value: '', label: placeholder };
    const disabled = normalized.length === 0;
    const safeClassName = normalizeClassName(className);
    const triggerId = `${selectId}-trigger`;
    const panelId = `${selectId}-panel`;

    const optionHtml = normalized
        .map((option, index) => {
            const isSelected = index === selectedIndex;
            return `
                <div class="pselect-option${isSelected ? ' pselect-selected' : ''}"
                     id="${escapeAttr(`${selectId}-option-${index}`)}"
                     role="option"
                     aria-selected="${isSelected ? 'true' : 'false'}"
                     data-value="${escapeAttr(option.value)}">
                    ${escapeHtml(option.label)}
                </div>`;
        })
        .join('');
    const hiddenInput = name
        ? `<input type="hidden" class="form-input pselect-input" name="${escapeAttr(name)}" value="${escapeAttr(current.value)}">`
        : '';
    const rootClasses = `pselect${safeClassName ? ` ${safeClassName}` : ''}${disabled ? ' pselect-disabled' : ''}`;
    const accessibleName = labelledBy
        ? ` aria-labelledby="${escapeAttr(labelledBy)}"`
        : ` aria-label="${escapeAttr(ariaLabel || placeholder)}"`;

    return `
        <div class="${rootClasses}" id="${escapeAttr(selectId)}" data-value="${escapeAttr(current.value)}">
            ${hiddenInput}
            <button class="pselect-trigger"
                 id="${escapeAttr(triggerId)}"
                 type="button"
                 role="combobox"
                 tabindex="${disabled ? '-1' : '0'}"
                 aria-haspopup="listbox"
                 aria-expanded="false"
                 aria-controls="${escapeAttr(panelId)}"
                 aria-disabled="${disabled ? 'true' : 'false'}"${disabled ? ' disabled' : ''}${accessibleName}>
                <span class="pselect-label">${escapeHtml(current.label)}</span>
                ${CHEVRON_SVG}
            </button>
            <div class="pselect-panel"
                 id="${escapeAttr(panelId)}"
                 role="listbox"
                 aria-labelledby="${escapeAttr(triggerId)}"
                 aria-hidden="true">${optionHtml}</div>
        </div>`;
}

export function initCustomSelects(container, callbacks = {}) {
    if (!container?.querySelectorAll) return;

    if (!documentClickAttached) {
        document.addEventListener('click', () => closeAllCustomSelects());
        documentClickAttached = true;
    }

    container.querySelectorAll('.pselect').forEach((selectEl) => {
        const callback = callbacks?.[selectEl.id];
        if (typeof callback === 'function') changeCallbacks.set(selectEl, callback);
        else changeCallbacks.delete(selectEl);

        if (selectEl.dataset.initialized === 'true') return;

        const trigger = selectEl.querySelector('.pselect-trigger');
        const panel = selectEl.querySelector('.pselect-panel');
        const label = selectEl.querySelector('.pselect-label');
        const input = selectEl.querySelector('.pselect-input');
        if (!trigger || !panel || !label) return;

        const optionNodes = [...panel.querySelectorAll('.pselect-option')];
        selectEl.dataset.initialized = 'true';
        if (trigger.getAttribute('aria-disabled') === 'true' || !optionNodes.length) return;

        const markActive = (optionEl) => {
            optionNodes.forEach((option) => {
                option.classList.toggle('pselect-active', option === optionEl);
            });
            if (optionEl?.id) trigger.setAttribute('aria-activedescendant', optionEl.id);
            else trigger.removeAttribute('aria-activedescendant');
        };

        const openSelect = (preferredOption = null) => {
            closeAllCustomSelects(selectEl);
            selectEl.classList.add('pselect-open');
            trigger.setAttribute('aria-expanded', 'true');
            panel.setAttribute('aria-hidden', 'false');
            markActive(preferredOption || panel.querySelector('.pselect-selected') || optionNodes[0]);
        };

        const selectOption = (optionEl) => {
            const value = optionEl.dataset.value ?? '';
            selectEl.dataset.value = value;
            label.textContent = String(optionEl.textContent ?? '').trim();
            if (input) input.value = value;

            optionNodes.forEach((option) => {
                const isSelected = option === optionEl;
                option.classList.toggle('pselect-selected', isSelected);
                option.setAttribute('aria-selected', isSelected ? 'true' : 'false');
            });
            closeCustomSelect(selectEl);
            trigger.focus();
            invokeChangeCallback(selectEl, value);
        };

        const moveActive = (offset) => {
            const activeIndex = optionNodes.findIndex((option) => option.classList.contains('pselect-active'));
            const selectedIndex = optionNodes.findIndex((option) => option.classList.contains('pselect-selected'));
            const startIndex = activeIndex >= 0 ? activeIndex : Math.max(selectedIndex, 0);
            const nextIndex = Math.min(Math.max(startIndex + offset, 0), optionNodes.length - 1);
            markActive(optionNodes[nextIndex]);
        };

        trigger.addEventListener('click', (event) => {
            event.stopPropagation();
            if (selectEl.classList.contains('pselect-open')) closeCustomSelect(selectEl);
            else openSelect();
        });

        trigger.addEventListener('keydown', (event) => {
            const isOpen = selectEl.classList.contains('pselect-open');
            switch (event.key) {
                case 'ArrowDown':
                case 'ArrowUp':
                    event.preventDefault();
                    if (!isOpen) openSelect();
                    else moveActive(event.key === 'ArrowDown' ? 1 : -1);
                    break;
                case 'Home':
                case 'End':
                    event.preventDefault();
                    openSelect(optionNodes[event.key === 'Home' ? 0 : optionNodes.length - 1]);
                    break;
                case 'Enter':
                case ' ':
                    event.preventDefault();
                    if (!isOpen) {
                        openSelect();
                        break;
                    }
                    selectOption(panel.querySelector('.pselect-active') || panel.querySelector('.pselect-selected'));
                    break;
                case 'Escape':
                    if (isOpen) {
                        event.preventDefault();
                        closeCustomSelect(selectEl);
                    }
                    break;
                case 'Tab':
                    closeCustomSelect(selectEl);
                    break;
            }
        });

        panel.addEventListener('click', (event) => {
            event.stopPropagation();
            const optionEl = event.target?.closest?.('.pselect-option');
            if (optionEl && optionNodes.includes(optionEl)) selectOption(optionEl);
        });
    });
}
