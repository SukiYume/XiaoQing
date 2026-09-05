/** Pendo Web 通用编辑表单的安全标记、取值和互斥按钮交互。 */

import { initCustomSelects, renderCustomSelect } from './custom_select.js';
import { escapeAttr, escapeHtml } from '../utils/ui.js';

const PRIORITY_OPTIONS = [
    { value: '1', label: '紧急', emoji: '🔴' },
    { value: '2', label: '高优先', emoji: '🟠' },
    { value: '3', label: '中优先', emoji: '🟡' },
    { value: '4', label: '低优先', emoji: '🟢' },
    { value: '5', label: '最低', emoji: '⚪' },
];

const DEFAULT_MOOD_OPTIONS = [
    { value: 'happy', label: '开心', emoji: '😊' },
    { value: 'sad', label: '难过', emoji: '😢' },
    { value: 'calm', label: '平静', emoji: '😌' },
    { value: 'excited', label: '兴奋', emoji: '🤩' },
    { value: 'angry', label: '生气', emoji: '😠' },
];

export function buildFormHTML(fields) {
    if (!Array.isArray(fields)) throw new TypeError('表单字段必须是数组');

    return fields
        .map((rawField, index) => {
            const field           = rawField ?? {};
            const rawName         = String(field.name ?? '');
            const rawFieldId      = String(field.id ?? `form-field-${rawName || 'field'}-${index}`);
            const rawLabelId      = `${rawFieldId}-label`;
            const name            = escapeAttr(rawName);
            const fieldId         = escapeAttr(rawFieldId);
            const labelId         = escapeAttr(rawLabelId);
            const label           = escapeHtml(field.label ?? '');
            const placeholderText = String(field.placeholder ?? '');
            const placeholder     = escapeAttr(placeholderText);
            const value           = field.value ?? '';
            const required        = field.required ? ' required' : '';
            let labelForId        = rawFieldId;
            let input;

            switch (field.type) {
                case 'select': {
                    const themeClass = field.selectThemeClass || 'pselect-theme-ledger';
                    input = renderCustomSelect({
                        id: rawFieldId,
                        name: rawName,
                        options: field.options || [],
                        selected: value,
                        placeholder: placeholderText || '请选择',
                        className: `pselect-form pselect-block ${themeClass}`,
                        labelledBy: rawLabelId,
                    });
                    labelForId = `${rawFieldId}-trigger`;
                    break;
                }
                case 'textarea':
                    input = `<textarea id="${fieldId}" name="${name}" class="form-input" rows="${escapeAttr(field.rows || 4)}" placeholder="${placeholder}" aria-labelledby="${labelId}"${required}>${escapeHtml(value)}</textarea>`;
                    break;
                case 'datetime':
                    input = `<input id="${fieldId}" type="text" name="${name}" class="form-input" value="${escapeAttr(value)}" inputmode="numeric" placeholder="${escapeAttr(placeholderText || 'YYYY-MM-DD HH:mm')}" aria-labelledby="${labelId}"${required}>`;
                    break;
                case 'date':
                    input = `<input id="${fieldId}" type="text" name="${name}" class="form-input" value="${escapeAttr(value)}" inputmode="numeric" placeholder="${escapeAttr(placeholderText || 'YYYY-MM-DD')}" aria-labelledby="${labelId}"${required}>`;
                    break;
                case 'number': {
                    const min  = field.min == null ? '' : ` min="${escapeAttr(field.min)}"`;
                    const max  = field.max == null ? '' : ` max="${escapeAttr(field.max)}"`;
                    const step = escapeAttr(field.step ?? 'any');
                    input = `<input id="${fieldId}" type="number" name="${name}" class="form-input" value="${escapeAttr(value)}"${min}${max} step="${step}" placeholder="${placeholder}" aria-labelledby="${labelId}"${required}>`;
                    break;
                }
                case 'checkbox':
                    input = `<label class="form-checkbox"><input id="${fieldId}" type="checkbox" name="${name}" class="form-input" aria-labelledby="${labelId}"${value ? ' checked' : ''}${required}> <span>${escapeHtml(field.checkboxLabel || '启用')}</span></label>`;
                    break;
                case 'priority': {
                    const selectedPriority = String(value ?? '');
                    input = `<div class="priority-selector" data-name="${name}" role="group" aria-labelledby="${labelId}">
                        ${PRIORITY_OPTIONS.map((option) => {
                            const active = selectedPriority === option.value;
                            return `<button type="button" class="priority-btn priority-${option.value}${active ? ' active' : ''}" data-value="${option.value}" title="${option.label}" aria-label="${option.label}" aria-pressed="${active ? 'true' : 'false'}">${option.emoji}</button>`;
                        }).join('')}
                    </div>`;
                    labelForId = '';
                    break;
                }
                case 'mood': {
                    const rawMoodOptions =
                        Array.isArray(field.options) && field.options.length ? field.options : DEFAULT_MOOD_OPTIONS;
                    const moodOptions = rawMoodOptions.map((option) => ({
                        value: String(option?.value ?? option?.id ?? option?.emoji ?? option ?? ''),
                        label: String(option?.label ?? option?.value ?? option ?? ''),
                        emoji: String(option?.emoji ?? option?.value ?? option ?? ''),
                    }));
                    const selectedMood      = String(value ?? '');
                    const selectedMoodIndex = moodOptions.findIndex(
                        (option) => selectedMood === option.value || selectedMood === option.emoji,
                    );
                    input = `<div class="mood-selector" data-name="${name}" role="group" aria-labelledby="${labelId}">
                        ${moodOptions
                            .map((option, optionIndex) => {
                                const active = optionIndex === selectedMoodIndex;
                                return `<button type="button" class="mood-btn${active ? ' active' : ''}" data-value="${escapeAttr(option.value)}" title="${escapeAttr(option.label)}" aria-label="${escapeAttr(option.label)}" aria-pressed="${active ? 'true' : 'false'}">${escapeHtml(option.emoji)}</button>`;
                            })
                            .join('')}
                    </div>`;
                    labelForId = '';
                    break;
                }
                default:
                    input = `<input id="${fieldId}" type="text" name="${name}" class="form-input" value="${escapeAttr(value)}" placeholder="${placeholder}" aria-labelledby="${labelId}"${required}>`;
            }

            const forAttribute = labelForId ? ` for="${escapeAttr(labelForId)}"` : '';
            return `<div class="form-group">
                <label class="form-label" id="${labelId}"${forAttribute}>${label}${field.required ? ' <span class="text-danger">*</span>' : ''}</label>
                ${input}
            </div>`;
        })
        .join('');
}

export function getFormData(container) {
    const data = {};
    if (!container?.querySelectorAll) return data;

    container.querySelectorAll('.form-input').forEach((element) => {
        const name = String(element.name || '');
        if (!name) return;

        if (element.type === 'checkbox') {
            data[name] = Boolean(element.checked);
        } else if (element.type === 'number') {
            const text   = String(element.value ?? '').trim();
            const number = Number(text);
            data[name] = text && Number.isFinite(number) ? number : null;
        } else {
            data[name] = element.value === '' ? null : element.value;
        }
    });

    container.querySelectorAll('.priority-selector .active').forEach((element) => {
        const name  = element.closest('.priority-selector')?.dataset?.name;
        const value = Number(element.dataset.value);
        if (name && Number.isInteger(value)) data[name] = value;
    });
    container.querySelectorAll('.mood-selector .active').forEach((element) => {
        const name = element.closest('.mood-selector')?.dataset?.name;
        if (name) data[name] = element.dataset.value ?? '';
    });
    return data;
}

// 优先级与心情都属于单选按钮组，共用同一套状态同步逻辑。
function initExclusiveButtonGroups(container, groupSelector, buttonSelector) {
    container.querySelectorAll(groupSelector).forEach((group) => {
        const buttons = [...group.querySelectorAll(buttonSelector)];
        buttons.forEach((button) => {
            button.onclick = () => {
                buttons.forEach((candidate) => {
                    const active = candidate === button;
                    candidate.classList.toggle('active', active);
                    candidate.setAttribute('aria-pressed', active ? 'true' : 'false');
                });
            };
        });
    });
}

export function initFormInteractions(container) {
    if (!container?.querySelectorAll) return;

    initCustomSelects(container);
    initExclusiveButtonGroups(container, '.priority-selector', '.priority-btn');
    initExclusiveButtonGroups(container, '.mood-selector', '.mood-btn');
}
