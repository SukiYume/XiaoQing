import { renderCustomSelect, initCustomSelects } from './custom_select.js';

export function buildFormHTML(fields) {
    return fields.map(field => {
        const required = field.required ? ' required' : '';
        const value = field.value ?? '';

        let input;
        switch (field.type) {
            case 'select': {
                const themeClass = field.selectThemeClass || 'pselect-theme-ledger';
                input = renderCustomSelect({
                    id: `form-select-${field.name}`,
                    name: field.name,
                    options: field.options || [],
                    selected: value,
                    placeholder: field.placeholder || '请选择',
                    className: `pselect-form pselect-block ${themeClass}`,
                });
                break;
            }
            case 'textarea':
                input = `<textarea name="${field.name}" class="form-input" rows="${field.rows || 4}" placeholder="${field.placeholder || ''}"${required}>${value}</textarea>`;
                break;
            case 'datetime':
                input = `<input type="datetime-local" name="${field.name}" class="form-input" value="${value}"${required}>`;
                break;
            case 'date':
                input = `<input type="text" name="${field.name}" class="form-input" value="${value}" inputmode="numeric" placeholder="${field.placeholder || 'YYYY-MM-DD'}"${required}>`;
                break;
            case 'number':
                input = `<input type="number" name="${field.name}" class="form-input" value="${value}" step="${field.step || 'any'}" placeholder="${field.placeholder || ''}"${required}>`;
                break;
            case 'tags':
                input = `<input type="text" name="${field.name}" class="form-input" value="${value}" placeholder="${field.placeholder || '回车添加标签'}">`;
                break;
            case 'priority':
                input = `<div class="priority-selector" data-name="${field.name}">
                    <button type="button" class="priority-btn priority-1${value == 1 ? ' active' : ''}" data-value="1">🔴</button>
                    <button type="button" class="priority-btn priority-2${value == 2 ? ' active' : ''}" data-value="2">🟠</button>
                    <button type="button" class="priority-btn priority-3${value == 3 ? ' active' : ''}" data-value="3">🟡</button>
                    <button type="button" class="priority-btn priority-4${value == 4 ? ' active' : ''}" data-value="4">🟢</button>
                    <button type="button" class="priority-btn priority-5${value == 5 ? ' active' : ''}" data-value="5">⚪</button>
                </div>`;
                break;
            case 'mood':
                input = `<div class="mood-selector" data-name="${field.name}">
                    ${['😊', '😢', '😌', '🤩', '😠'].map(m =>
                        `<button type="button" class="mood-btn${value === m ? ' active' : ''}" data-value="${m}">${m}</button>`
                    ).join('')}
                </div>`;
                break;
            default:
                input = `<input type="text" name="${field.name}" class="form-input" value="${value}" placeholder="${field.placeholder || ''}"${required}>`;
        }

        return `<div class="form-group">
            <label class="form-label">${field.label}${field.required ? ' <span class="text-danger">*</span>' : ''}</label>
            ${input}
        </div>`;
    }).join('');
}

export function getFormData(container) {
    const data = {};
    container.querySelectorAll('.form-input').forEach(el => {
        if (el.name) {
            if (el.type === 'number') {
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
