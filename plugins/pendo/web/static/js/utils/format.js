export function pad2(value) {
    return String(value).padStart(2, '0');
}

export function todayStr() {
    const today = new Date();
    return `${today.getFullYear()}-${pad2(today.getMonth() + 1)}-${pad2(today.getDate())}`;
}

export function parseDate(value) {
    if (!value) return null;
    const text = String(value);
    const date = new Date(text.length === 10 ? `${text}T00:00:00` : text);
    return Number.isNaN(date.getTime()) ? null : date;
}

export function isoDate(date) {
    return `${date.getFullYear()}-${pad2(date.getMonth() + 1)}-${pad2(date.getDate())}`;
}

export function formatDateInput(value) {
    if (value instanceof Date) return isoDate(value);
    return String(value || '');
}

export function isValidDateInput(value) {
    const text = String(value || '').trim();
    if (!/^\d{4}-\d{2}-\d{2}$/.test(text)) return false;
    const date = new Date(`${text}T00:00:00`);
    return !Number.isNaN(date.getTime()) && formatDateInput(date) === text;
}

export function formatMonthDay(value, fallback = '未知时间') {
    const date = parseDate(value);
    if (!date) return fallback;
    return `${date.getMonth() + 1}/${date.getDate()}`;
}

export function formatDateTime(value, fallback = '未知时间') {
    const date = parseDate(value);
    if (!date) return fallback;
    return `${date.getFullYear()}-${pad2(date.getMonth() + 1)}-${pad2(date.getDate())} ${pad2(date.getHours())}:${pad2(date.getMinutes())}`;
}

export function previewText(value, maxLength = 100) {
    const text = String(value || '').trim();
    if (!text) return '';
    return text.length <= maxLength ? text : `${text.slice(0, maxLength)}...`;
}
