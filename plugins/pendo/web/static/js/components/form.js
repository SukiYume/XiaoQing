import { renderCustomSelect, initCustomSelects } from './custom_select.js';
import { escapeAttr, escapeHtml } from '../utils/ui.js';

export function buildFormHTML(fields) {
    return fields.map((field, index) => {
        const required = field.required ? ' required' : '';
        const value = field.value ?? '';
        const name = escapeAttr(field.name || '');
        const label = escapeHtml(field.label || '');
        const labelText = field.label || field.placeholder || field.name || '表单字段';
        const fieldId = escapeAttr(field.id || `form-field-${field.name || 'field'}-${index}`);
        const labelId = escapeAttr(`${fieldId}-label`);
        const ariaLabel = escapeAttr(labelText);
        const placeholder = escapeAttr(field.placeholder || '');

        let input;
        switch (field.type) {
            case 'select': {
                const themeClass = field.selectThemeClass || 'pselect-theme-ledger';
                input = renderCustomSelect({
                    id: fieldId,
                    name: field.name,
                    options: field.options || [],
                    selected: value,
                    placeholder: field.placeholder || '请选择',
                    className: `pselect-form pselect-block ${themeClass}`,
                    labelledBy: labelId,
                });
                break;
            }
            case 'textarea':
                input = `<textarea id="${fieldId}" name="${name}" class="form-input" rows="${escapeAttr(field.rows || 4)}" placeholder="${placeholder}" aria-label="${ariaLabel}"${required}>${escapeHtml(value)}</textarea>`;
                break;
            case 'datetime':
                input = `<input id="${fieldId}" type="datetime-local" name="${name}" class="form-input" value="${escapeAttr(value)}" aria-label="${ariaLabel}"${required}>`;
                break;
            case 'date':
                input = `<input id="${fieldId}" type="text" name="${name}" class="form-input" value="${escapeAttr(value)}" inputmode="numeric" placeholder="${escapeAttr(field.placeholder || 'YYYY-MM-DD')}" aria-label="${ariaLabel}"${required}>`;
                break;
            case 'number':
                input = `<input id="${fieldId}" type="number" name="${name}" class="form-input" value="${escapeAttr(value)}" min="${escapeAttr(field.min ?? '')}" max="${escapeAttr(field.max ?? '')}" step="${escapeAttr(field.step || 'any')}" placeholder="${placeholder}" aria-label="${ariaLabel}"${required}>`;
                break;
            case 'checkbox':
                input = `<label class="form-checkbox"><input id="${fieldId}" type="checkbox" name="${name}" class="form-input" aria-label="${ariaLabel}" ${value ? 'checked' : ''}> <span>${escapeHtml(field.checkboxLabel || '启用')}</span></label>`;
                break;
            case 'tags':
                input = `<input id="${fieldId}" type="text" name="${name}" class="form-input" value="${escapeAttr(value)}" placeholder="${escapeAttr(field.placeholder || '回车添加标签')}" aria-label="${ariaLabel}">`;
                break;
            case 'priority':
                input = `<div class="priority-selector" data-name="${name}">
                    <button type="button" class="priority-btn priority-1${value == 1 ? ' active' : ''}" data-value="1">🔴</button>
                    <button type="button" class="priority-btn priority-2${value == 2 ? ' active' : ''}" data-value="2">🟠</button>
                    <button type="button" class="priority-btn priority-3${value == 3 ? ' active' : ''}" data-value="3">🟡</button>
                    <button type="button" class="priority-btn priority-4${value == 4 ? ' active' : ''}" data-value="4">🟢</button>
                    <button type="button" class="priority-btn priority-5${value == 5 ? ' active' : ''}" data-value="5">⚪</button>
                </div>`;
                break;
            case 'mood':
                const moodOptions = field.options?.length ? field.options : [
                    { value: 'happy', label: '开心', emoji: '😊' },
                    { value: 'sad', label: '难过', emoji: '😢' },
                    { value: 'calm', label: '平静', emoji: '😌' },
                    { value: 'excited', label: '兴奋', emoji: '🤩' },
                    { value: 'angry', label: '生气', emoji: '😠' },
                ];
                input = `<div class="mood-selector" data-name="${name}">
                    ${moodOptions.map(m => {
                        const optionValue = String(m.value ?? m.id ?? m.emoji ?? m);
                        const optionLabel = String(m.label ?? optionValue);
                        const optionEmoji = String(m.emoji ?? optionValue);
                        const active = value === optionValue || value === optionEmoji;
                        return `<button type="button" class="mood-btn${active ? ' active' : ''}" data-value="${escapeAttr(optionValue)}" title="${escapeAttr(optionLabel)}" aria-label="${escapeAttr(optionLabel)}">${escapeHtml(optionEmoji)}</button>`;
                    }
                    ).join('')}
                </div>`;
                break;
            default:
                input = `<input id="${fieldId}" type="text" name="${name}" class="form-input" value="${escapeAttr(value)}" placeholder="${placeholder}" aria-label="${ariaLabel}"${required}>`;
        }

        return `<div class="form-group">
            <label class="form-label" id="${labelId}" for="${fieldId}">${label}${field.required ? ' <span class="text-danger">*</span>' : ''}</label>
            ${input}
        </div>`;
    }).join('');
}

export function getFormData(container) {
    const data = {};
    container.querySelectorAll('.form-input').forEach(el => {
        if (el.name) {
            if (el.type === 'checkbox') {
                data[el.name] = el.checked;
            } else if (el.type === 'number') {
                data[el.name] = el.value ? parseFloat(el.value) : null;
            } else {
                data[el.name] = el.value || null;
            }
        }
    });
    // Priority selector
    container.querySelectorAll('.priority-selector .active').forEach(el => {
        data[el.closest('.priority-selector').dataset.name] = parseInt(el.dataset.value);
    });
    // Mood selector
    container.querySelectorAll('.mood-selector .active').forEach(el => {
        data[el.closest('.mood-selector').dataset.name] = el.dataset.value;
    });
    return data;
}

export function initFormInteractions(container) {
    initCustomSelects(container);

    // Priority buttons
    container.querySelectorAll('.priority-selector').forEach(sel => {
        sel.querySelectorAll('.priority-btn').forEach(btn => {
            btn.onclick = () => {
                sel.querySelectorAll('.priority-btn').forEach(b => b.classList.remove('active'));
                btn.classList.add('active');
            };
        });
    });
    // Mood buttons
    container.querySelectorAll('.mood-selector').forEach(sel => {
        sel.querySelectorAll('.mood-btn').forEach(btn => {
            btn.onclick = () => {
                sel.querySelectorAll('.mood-btn').forEach(b => b.classList.remove('active'));
                btn.classList.add('active');
            };
        });
    });
}
