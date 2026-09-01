import { api, apiDownload, apiUpload } from '../api.js';
import { showToast } from '../components/toast.js';
import {
    errorMessage,
    isRecord,
    isValidDateInput,
    nonNegativeInteger,
    trimmedTextValue as textValue,
} from '../utils/format.js';
import { formatZonedDateTime, getUserTimeZone } from '../utils/timezone.js';
import { BREAKPOINTS, escapeHtml, injectStyles, mediaMax, pageShellCss } from '../utils/ui.js';

const CSS_ID = 'pendo-transfer-page-styles';
const TYPE_OPTIONS = [
    { value: 'event', label: '日程', hint: '安排与提醒' },
    { value: 'task', label: '待办', hint: '执行清单' },
    { value: 'ledger', label: '记账', hint: '收支记录' },
    { value: 'note', label: '笔记', hint: '知识沉淀' },
    { value: 'diary', label: '日记', hint: '书写记录' },
];
const PRESETS = [
    { value: 'week', label: '本周' },
    { value: 'month', label: '本月' },
    { value: 'quarter', label: '本季' },
    { value: 'year', label: '今年' },
    { value: 'last_year', label: '去年' },
    { value: 'custom', label: '自定义' },
    { value: 'all', label: '全部' },
];
const CONFLICT_POLICIES = [
    {
        value: 'isolate',
        label: '隔离导入',
        desc: '在本 bundle 命名空间生成内部 UUID，不与已有来源记录合并。',
    },
    {
        value: 'skip',
        label: '跳过同来源',
        desc: '当前用户、同类型且 import.source_id 相同的记录已存在时跳过。',
    },
    {
        value: 'overwrite',
        label: '覆盖同来源',
        desc: '仅覆盖此前导入的同用户、同类型、同来源记录，不按外部 ID 直接定位。',
    },
    {
        value: 'duplicate',
        label: '生成副本',
        desc: '始终生成新的内部 UUID，并保留来源 ID 元数据。',
    },
];
const INVALID_POLICIES = [
    { value: 'abort', label: '有错误则终止', desc: '发现非法记录就停止导入。' },
    {
        value: 'skip_invalid',
        label: '跳过非法记录',
        desc: '只导入通过校验的记录。',
    },
];
const TABS = new Set(['export', 'import', 'history']);
const TYPE_VALUES = new Set(TYPE_OPTIONS.map((item) => item.value));
const DISPLAY_TYPE_VALUES = new Set([...TYPE_VALUES, 'event_collection']);
const TYPE_LABELS = new Map(TYPE_OPTIONS.map((item) => [item.value, item.label]));
const PRESET_VALUES = new Set(PRESETS.map((item) => item.value));
const CONFLICT_POLICY_VALUES = new Set(CONFLICT_POLICIES.map((item) => item.value));
const INVALID_POLICY_VALUES = new Set(INVALID_POLICIES.map((item) => item.value));
const RESULT_KINDS = ['inserted', 'updated', 'skipped', 'failed'];
const MAX_RENDERED_ERRORS = 100;
const MAX_RENDERED_RESULTS = 100;

let _container = null;
let _state = null;

// 所有接口响应先收敛到稳定结构，模板和动作层不再反复判断畸形数据。
function positiveInteger(value, fallback, maximum = 100) {
    try {
        const number = Number(value);
        return Number.isInteger(number) && number >= 1 ? Math.min(number, maximum) : fallback;
    } catch {
        return fallback;
    }
}

function normalizeTypes(value, includeEventCollection = false) {
    if (!Array.isArray(value)) return [];
    const allowed = includeEventCollection ? DISPLAY_TYPE_VALUES : TYPE_VALUES;
    const seen = new Set();
    return value.reduce((types, rawType) => {
        const type = textValue(rawType);
        if (allowed.has(type) && !seen.has(type)) {
            seen.add(type);
            types.push(type);
        }
        return types;
    }, []);
}

function normalizeSample(value) {
    if (!isRecord(value)) return null;
    const type = textValue(value.type);
    if (!DISPLAY_TYPE_VALUES.has(type)) return null;
    return {
        type,
        id: textValue(value.id),
        title: textValue(value.title),
        reason: textValue(value.reason),
    };
}

function normalizeMessages(value) {
    if (!Array.isArray(value)) return [];
    return value
        .map((message) => textValue(message))
        .filter(Boolean)
        .slice(0, MAX_RENDERED_ERRORS);
}

function normalizeValidationErrors(value) {
    if (!Array.isArray(value)) return [];
    return value
        .filter(isRecord)
        .map((error) => ({
            path: textValue(error.path) || '未知文件',
            line: nonNegativeInteger(error.line),
            message: textValue(error.message) || '未知校验错误',
        }))
        .slice(0, MAX_RENDERED_ERRORS);
}

function normalizeExportPreview(value) {
    if (!isRecord(value)) return null;
    const selection = isRecord(value.selection) ? value.selection : {};
    const types = normalizeTypes(selection.types);
    const rawCounts = isRecord(value.counts) ? value.counts : {};
    const counts = types.map((type) => ({
        type,
        count: nonNegativeInteger(rawCounts[type]),
    }));
    const rawStart = textValue(selection.start);
    const rawEnd = textValue(selection.end);
    const start = isValidDateInput(rawStart) ? rawStart : '';
    const end = isValidDateInput(rawEnd) ? rawEnd : '';
    const range = start && end && start <= end ? { start, end } : { start: '', end: '' };
    return {
        selection: {
            types,
            preset: PRESET_VALUES.has(selection.preset) ? selection.preset : 'all',
            ...range,
        },
        counts,
        total: counts.reduce((sum, item) => sum + item.count, 0),
        warnings: normalizeMessages(value.warnings),
    };
}

function normalizeInspect(value) {
    if (!isRecord(value)) return null;
    const summary = isRecord(value.summary) ? value.summary : {};
    const counts = isRecord(value.counts) ? value.counts : {};
    const files = Array.isArray(value.files)
        ? value.files.filter(isRecord).map((file) => {
              const type = textValue(file.type);
              return {
                  path: textValue(file.path) || '未知文件',
                  type: DISPLAY_TYPE_VALUES.has(type) ? type : '',
                  count: nonNegativeInteger(file.count),
                  valid: nonNegativeInteger(file.valid),
              };
          })
        : [];
    const samples = Array.isArray(value.samples) ? value.samples.map(normalizeSample).filter(Boolean).slice(0, 5) : [];
    return {
        summary: {
            types: normalizeTypes(summary.types),
            files: nonNegativeInteger(summary.files),
        },
        files,
        counts: {
            valid: nonNegativeInteger(counts.valid),
            errors: nonNegativeInteger(counts.errors),
            total_samples: nonNegativeInteger(counts.total_samples),
        },
        bundle_id: textValue(value.bundle_id),
        already_imported: value.already_imported === true,
        warnings: normalizeMessages(value.warnings),
        errors: normalizeValidationErrors(value.errors),
        samples,
    };
}

function normalizeSamplePage(value) {
    if (!isRecord(value)) return null;
    const pageSize = positiveInteger(value.page_size, 5);
    return {
        samples: Array.isArray(value.samples)
            ? value.samples.map(normalizeSample).filter(Boolean).slice(0, pageSize)
            : [],
        page: positiveInteger(value.page, 1, Number.MAX_SAFE_INTEGER),
        page_size: pageSize,
        total: nonNegativeInteger(value.total),
    };
}

function normalizeImportResult(value) {
    if (!isRecord(value)) return null;
    const rawCounts = isRecord(value.counts) ? value.counts : {};
    const rawResults = isRecord(value.results) ? value.results : {};
    const rawDetails = isRecord(value.details) ? value.details : {};
    const details = {};
    RESULT_KINDS.forEach((kind) => {
        details[kind] = Array.isArray(rawDetails[kind])
            ? rawDetails[kind].map(normalizeSample).filter(Boolean).slice(0, MAX_RENDERED_RESULTS)
            : [];
    });
    return {
        summary: {
            types: normalizeTypes(isRecord(value.summary) ? value.summary.types : []),
        },
        counts: {
            valid: nonNegativeInteger(rawCounts.valid),
            errors: nonNegativeInteger(rawCounts.errors),
        },
        bundle_id: textValue(value.bundle_id),
        warnings: normalizeMessages(value.warnings),
        errors: normalizeValidationErrors(value.errors),
        results: Object.fromEntries(RESULT_KINDS.map((kind) => [kind, nonNegativeInteger(rawResults[kind])])),
        details,
    };
}

function normalizeLogs(value) {
    if (!Array.isArray(value)) return [];
    return value.reduce((logs, rawLog) => {
        if (!isRecord(rawLog)) return logs;
        const action = textValue(rawLog.action);
        if (!['import', 'export'].includes(action)) return logs;
        const summary = isRecord(rawLog.result_summary) ? rawLog.result_summary : {};
        logs.push({
            action,
            filename: textValue(rawLog.filename),
            types: normalizeTypes(rawLog.types),
            record_count: nonNegativeInteger(rawLog.record_count),
            has_result_counts: ['inserted', 'updated', 'skipped'].some((key) => Object.hasOwn(summary, key)),
            result_summary: {
                inserted: nonNegativeInteger(summary.inserted),
                updated: nonNegativeInteger(summary.updated),
                skipped: nonNegativeInteger(summary.skipped),
            },
            created_at: textValue(rawLog.created_at),
        });
        return logs;
    }, []);
}

function defaultState() {
    return {
        tab: 'export',
        export: {
            selectedTypes: TYPE_OPTIONS.map((item) => item.value),
            preset: 'month',
            start: '',
            end: '',
            preview: null,
            loading: false,
            downloading: false,
        },
        import: {
            generation: 0,
            file: null,
            inspect: null,
            selectedTypes: [],
            conflictPolicy: 'isolate',
            invalidPolicy: 'abort',
            forceReimport: false,
            inspecting: false,
            executing: false,
            result: null,
            samplePage: 1,
            samplePageSize: 5,
            samplesLoading: false,
            paginatedSamples: null,
        },
        history: {
            logs: [],
            loading: false,
            loaded: false,
        },
    };
}

function isCurrentState(state) {
    return Boolean(_container && _state === state);
}

function setImportFile(file) {
    if (!_state) return;
    const nextFile = file && typeof file.name === 'string' ? file : null;
    Object.assign(_state.import, {
        generation: _state.import.generation + 1,
        file: nextFile,
        inspect: null,
        selectedTypes: [],
        forceReimport: false,
        inspecting: false,
        executing: false,
        result: null,
        samplePage: 1,
        samplesLoading: false,
        paginatedSamples: null,
    });
}

function transferFilename(file) {
    return (textValue(file?.name) || 'bundle.pendo.zip').replace(/[\r\n]/g, '_').slice(0, 255);
}

function exportSelection() {
    const state = _state?.export;
    if (!state) return { error: '导出页面已关闭', payload: null, signature: '' };

    const types = normalizeTypes(state.selectedTypes);
    const preset = PRESET_VALUES.has(state.preset) ? state.preset : 'month';
    const customStart = textValue(state.start);
    const customEnd = textValue(state.end);
    let start = null;
    let end = null;
    if (!types.length) return { error: '至少保留一个导出类别', payload: null, signature: '' };
    if (preset === 'custom') {
        if (!isValidDateInput(customStart) || !isValidDateInput(customEnd)) {
            return {
                error: '自定义范围需要填写两个有效日期',
                payload: null,
                signature: '',
            };
        }
        if (customStart > customEnd) {
            return {
                error: '开始日期不能晚于结束日期',
                payload: null,
                signature: '',
            };
        }
        start = customStart;
        end = customEnd;
    }

    const timezone = getUserTimeZone();
    const payload = { types, preset, start, end, timezone };
    return { error: '', payload, signature: JSON.stringify(payload) };
}

function ensureStyles() {
    injectStyles(
        CSS_ID,
        `
        ${pageShellCss('transfer-shell', { padding: '24px 24px 36px', compactPadding: '18px 14px 28px', compactBreakpoint: BREAKPOINTS.MOBILE })}
        /* 页面标题、标签页与主布局。 */
        .transfer-stack { display: flex; flex-direction: column; gap: 18px; }
        .transfer-hero { padding: 26px 28px; border-radius: 28px; background: linear-gradient(145deg, rgba(255,255,255,0.98), rgba(247,250,255,0.94)); border: 1px solid rgba(148,163,184,0.18); box-shadow: 0 18px 40px rgba(15,23,42,0.05); }
        .transfer-kicker { font-size: 13px; font-weight: 800; color: #2563eb; letter-spacing: 0.08em; text-transform: uppercase; }
        .transfer-hero h2 { margin: 10px 0 0; font-size: 34px; font-weight: 840; letter-spacing: -0.04em; color: #0f172a; }
        .transfer-hero p { margin: 10px 0 0; max-width: 780px; font-size: 14px; line-height: 1.8; color: var(--color-text-secondary); }
        .transfer-chip-row { margin-top: 16px; display: flex; flex-wrap: wrap; gap: 10px; }
        .transfer-chip { display: inline-flex; align-items: center; height: 34px; padding: 0 14px; border-radius: 999px; background: rgba(59,130,246,0.08); color: #1d4ed8; font-size: 12px; font-weight: 700; }
        .transfer-tabs { display: inline-flex; gap: 8px; padding: 8px; border-radius: 999px; background: rgba(255,255,255,0.84); border: 1px solid rgba(226,232,240,0.92); }
        .transfer-tab { border: none; background: transparent; color: var(--color-text-secondary); border-radius: 999px; min-width: 110px; height: 42px; padding: 0 18px; font-size: 14px; font-weight: 760; cursor: pointer; }
        .transfer-tab.active { background: #0f172a; color: #fff; box-shadow: 0 10px 22px rgba(15,23,42,0.14); }
        /* 导出、导入共用卡片和表单控件。 */
        .transfer-grid { display: grid; grid-template-columns: minmax(0, 1.15fr) minmax(300px, 0.85fr); gap: 18px; }
        .transfer-grid > * { min-width: 0; }
        .transfer-card, .transfer-sidecard { background: linear-gradient(180deg, rgba(255,255,255,0.98), rgba(248,250,252,0.94)); border: 1px solid rgba(226,232,240,0.88); border-radius: 26px; box-shadow: 0 14px 30px rgba(15,23,42,0.04); }
        .transfer-card { padding: 22px 22px 24px; }
        .transfer-sidecard { padding: 20px; display: flex; flex-direction: column; gap: 14px; }
        .transfer-card h3, .transfer-sidecard h3 { margin: 0; font-size: 22px; font-weight: 820; letter-spacing: -0.03em; color: #0f172a; }
        .transfer-card p, .transfer-sidecard p { margin: 6px 0 0; font-size: 13px; line-height: 1.7; color: var(--color-text-secondary); }
        .transfer-section { margin-top: 18px; display: flex; flex-direction: column; gap: 12px; }
        .transfer-section-label { font-size: 12px; font-weight: 800; color: var(--color-text-secondary); text-transform: uppercase; letter-spacing: 0.06em; }
        .transfer-type-grid { display: grid; grid-template-columns: repeat(5, minmax(0, 1fr)); gap: 10px; }
        .transfer-type-card { border: 1px solid rgba(226,232,240,0.96); border-radius: 18px; padding: 14px 12px; background: rgba(255,255,255,0.9); cursor: pointer; transition: transform 0.18s ease, border-color 0.18s ease, box-shadow 0.18s ease, background 0.18s ease; }
        .transfer-type-card:hover:not(:disabled) { transform: translateY(-1px); border-color: rgba(59,130,246,0.34); }
        .transfer-type-card.active { border-color: rgba(37,99,235,0.52); background: rgba(59,130,246,0.08); box-shadow: 0 10px 22px rgba(59,130,246,0.08); }
        .transfer-type-title { font-size: 14px; font-weight: 760; color: #0f172a; }
        .transfer-type-hint { margin-top: 6px; font-size: 12px; line-height: 1.55; color: var(--color-text-secondary); }
        .transfer-preset-row, .transfer-actions, .transfer-inline-checks { display: flex; flex-wrap: wrap; gap: 10px; }
        .transfer-pill { border: 1px solid rgba(203,213,225,0.96); background: #fff; color: #334155; border-radius: 999px; height: 38px; padding: 0 16px; font-size: 13px; font-weight: 730; cursor: pointer; }
        .transfer-pill.active { border-color: rgba(37,99,235,0.52); background: rgba(59,130,246,0.10); color: #1d4ed8; }
        .transfer-type-card:disabled, .transfer-pill:disabled { opacity: 0.58; cursor: not-allowed; }
        .transfer-date-row, .transfer-summary-grid { display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 12px; }
        .transfer-summary-grid.three { grid-template-columns: repeat(3, minmax(0, 1fr)); }
        .transfer-field { display: flex; flex-direction: column; gap: 6px; }
        .transfer-field label { font-size: 12px; font-weight: 760; color: var(--color-text-secondary); }
        .transfer-field input { width: 100%; box-sizing: border-box; height: 42px; border-radius: 14px; border: 1px solid rgba(203,213,225,0.92); background: rgba(255,255,255,0.94); padding: 0 14px; font-size: 14px; color: var(--color-text); }
        .transfer-file-input { display: none; }
        .transfer-btn { border: none; border-radius: 16px; height: 44px; padding: 0 18px; font-size: 14px; font-weight: 780; cursor: pointer; transition: transform 0.18s ease, opacity 0.18s ease; }
        .transfer-btn:hover:not(:disabled) { transform: translateY(-1px); }
        .transfer-btn:disabled { opacity: 0.55; cursor: not-allowed; }
        .transfer-btn.primary { background: linear-gradient(135deg, #2563eb, #1d4ed8); color: #fff; box-shadow: 0 12px 24px rgba(37,99,235,0.18); }
        .transfer-btn.secondary { background: rgba(255,255,255,0.92); color: #0f172a; border: 1px solid rgba(203,213,225,0.96); }
        .transfer-btn.sm { height: 34px; padding: 0 14px; font-size: 12px; border-radius: 12px; }
        .transfer-tab:focus-visible, .transfer-type-card:focus-visible, .transfer-pill:focus-visible,
        .transfer-field input:focus-visible, .transfer-btn:focus-visible {
            outline: 3px solid rgba(37,99,235,0.22);
            outline-offset: 2px;
        }
        /* 预览、预检与执行结果。 */
        .transfer-summary-card, .transfer-list-row, .transfer-check, .transfer-error-row, .transfer-sample-row, .transfer-empty, .transfer-note, .transfer-upload { border-radius: 18px; }
        .transfer-summary-card, .transfer-list-row, .transfer-check, .transfer-sample-row { background: rgba(255,255,255,0.9); border: 1px solid rgba(226,232,240,0.88); }
        .transfer-summary-card { padding: 16px; min-width: 0; }
        .transfer-status-banner {
            display: grid; grid-template-columns: auto minmax(0, 1fr) auto; gap: 14px; align-items: center;
            padding: 16px 18px; border-radius: 22px; border: 1px solid rgba(34,197,94,0.20);
            background: linear-gradient(135deg, rgba(240,253,244,0.98), rgba(236,253,245,0.94));
            box-shadow: 0 14px 30px rgba(34,197,94,0.08);
        }
        .transfer-status-banner.warning,
        .transfer-status-banner.duplicate-warn {
            border-color: rgba(245,158,11,0.24);
            background: linear-gradient(135deg, rgba(255,251,235,0.98), rgba(255,247,237,0.94));
            box-shadow: 0 14px 30px rgba(245,158,11,0.08);
        }
        .transfer-status-icon {
            width: 42px; height: 42px; border-radius: 14px; display: inline-flex; align-items: center; justify-content: center;
            background: rgba(34,197,94,0.14); color: #166534; font-size: 20px; font-weight: 900;
        }
        .transfer-status-copy { min-width: 0; }
        .transfer-status-banner.warning .transfer-status-icon,
        .transfer-status-banner.duplicate-warn .transfer-status-icon { background: rgba(245,158,11,0.16); color: #b45309; }
        .transfer-status-title { font-size: 15px; font-weight: 820; color: #0f172a; line-height: 1.35; overflow-wrap: anywhere; word-break: break-word; }
        .transfer-status-meta { margin-top: 4px; font-size: 12px; line-height: 1.7; color: #475569; overflow-wrap: anywhere; word-break: break-word; }
        .transfer-status-pills { display: flex; flex-wrap: wrap; gap: 8px; justify-content: flex-end; }
        .transfer-status-pill {
            display: inline-flex; align-items: center; height: 32px; padding: 0 12px; border-radius: 999px;
            background: rgba(255,255,255,0.86); border: 1px solid rgba(203,213,225,0.82); color: #0f172a; font-size: 12px; font-weight: 760;
        }
        .transfer-summary-label { font-size: 12px; font-weight: 760; color: var(--color-text-secondary); }
        .transfer-summary-value {
            margin-top: 10px; font-size: clamp(22px, 1.75vw, 28px); font-weight: 840; letter-spacing: -0.04em; color: #0f172a;
            line-height: 1.08; overflow-wrap: anywhere; word-break: break-word;
        }
        .transfer-summary-value.range { font-size: 18px; line-height: 1.3; }
        .transfer-summary-meta { margin-top: 8px; font-size: 12px; color: var(--color-text-secondary); overflow-wrap: anywhere; word-break: break-word; }
        .transfer-list, .transfer-errors, .transfer-samples { display: flex; flex-direction: column; gap: 10px; }
        .transfer-list-row { display: grid; grid-template-columns: minmax(0, 1fr) auto; gap: 10px; align-items: start; padding: 13px 14px; }
        .transfer-list-title { font-size: 14px; font-weight: 760; color: #0f172a; }
        .transfer-list-meta, .transfer-error-meta, .transfer-sample-meta { margin-top: 5px; font-size: 12px; line-height: 1.65; color: var(--color-text-secondary); }
        .transfer-list-value { font-size: 18px; font-weight: 820; color: #0f172a; }
        .transfer-upload { border: 1.5px dashed rgba(148,163,184,0.56); padding: 24px; background: rgba(248,250,252,0.94); display: flex; flex-direction: column; align-items: center; gap: 10px; text-align: center; }
        .transfer-upload.dragover { border-color: rgba(37,99,235,0.56); background: rgba(59,130,246,0.08); }
        .transfer-upload-title { font-size: 18px; font-weight: 800; color: #0f172a; }
        .transfer-upload-hint, .transfer-empty { font-size: 13px; line-height: 1.7; color: var(--color-text-secondary); }
        .transfer-file-name { font-size: 12px; font-weight: 700; color: #1d4ed8; }
        .transfer-check { display: inline-flex; align-items: center; gap: 8px; padding: 10px 12px; font-size: 13px; color: #334155; }
        .transfer-check input[type="radio"] {
            appearance: none; -webkit-appearance: none; margin: 0; flex: 0 0 auto;
            width: 18px; height: 18px; border-radius: 999px; border: 1.5px solid rgba(37,99,235,0.42);
            background: #fff; display: grid; place-items: center; outline: none;
        }
        .transfer-check input[type="radio"]::before {
            content: ''; width: 9px; height: 9px; border-radius: 999px; background: #2563eb;
            transform: scale(0); transition: transform 0.16s ease;
        }
        .transfer-check input[type="radio"]:checked::before { transform: scale(1); }
        .transfer-check input[type="radio"]:focus-visible { box-shadow: 0 0 0 3px rgba(37,99,235,0.12); }
        .transfer-check input[type="checkbox"] { accent-color: #2563eb; margin: 0; }
        .transfer-check input[type="checkbox"]:focus-visible { outline: 3px solid rgba(37,99,235,0.18); outline-offset: 2px; }
        .transfer-error-row { padding: 12px 14px; border: 1px solid rgba(248,113,113,0.24); background: rgba(254,242,242,0.92); }
        .transfer-error-title, .transfer-sample-title { font-size: 13px; font-weight: 780; color: #0f172a; }
        .transfer-sample-row, .transfer-empty, .transfer-note { padding: 12px 14px; }
        .transfer-note { background: rgba(239,246,255,0.92); border: 1px solid rgba(191,219,254,0.72); font-size: 13px; line-height: 1.7; color: #1e3a8a; }
        .transfer-note.preview-warnings { margin-top: 10px; }
        .transfer-policy-desc { margin: -2px 0 4px 6px; }
        /* 静态格式示例与操作记录。 */
        .transfer-example-block { padding: 14px 16px; border-radius: 18px; background: rgba(248,250,252,0.96); border: 1px solid rgba(226,232,240,0.92); }
        .transfer-example-title { font-size: 13px; font-weight: 800; color: #0f172a; }
        .transfer-example-tip { margin-top: 6px; font-size: 12px; line-height: 1.65; color: var(--color-text-secondary); }
        .transfer-code {
            margin: 10px 0 0; padding: 12px 14px; border-radius: 14px; overflow: auto;
            background: #0f172a; color: #e2e8f0; font-size: 12px; line-height: 1.7;
            font-family: 'SFMono-Regular', Consolas, 'Liberation Mono', Menlo, monospace;
        }
        .transfer-result-group { display: flex; flex-direction: column; gap: 10px; }
        .transfer-result-title { font-size: 13px; font-weight: 800; color: #0f172a; }
        .transfer-result-row {
            display: grid; grid-template-columns: auto minmax(0, 1fr); gap: 12px; align-items: start;
            padding: 12px 14px; border-radius: 16px; border: 1px solid rgba(226,232,240,0.88); background: rgba(255,255,255,0.92);
        }
        .transfer-result-badge {
            display: inline-flex; align-items: center; justify-content: center; min-width: 56px; height: 28px; padding: 0 10px;
            border-radius: 999px; font-size: 12px; font-weight: 800;
        }
        .transfer-result-badge.inserted { background: rgba(34,197,94,0.12); color: #166534; }
        .transfer-result-badge.updated { background: rgba(59,130,246,0.12); color: #1d4ed8; }
        .transfer-result-badge.skipped { background: rgba(245,158,11,0.14); color: #b45309; }
        .transfer-result-badge.failed { background: rgba(248,113,113,0.14); color: #b91c1c; }
        .transfer-result-name { font-size: 13px; font-weight: 760; color: #0f172a; }
        .transfer-result-meta { margin-top: 4px; font-size: 12px; line-height: 1.65; color: var(--color-text-secondary); }
        .transfer-pager { display: flex; align-items: center; gap: 10px; justify-content: center; margin-top: 8px; }
        .transfer-pager-info { font-size: 12px; color: var(--color-text-secondary); }
        .transfer-log-row {
            display: grid; grid-template-columns: auto minmax(0, 1fr) auto; gap: 12px; align-items: center;
            padding: 14px 16px; border-radius: 18px; background: rgba(255,255,255,0.9); border: 1px solid rgba(226,232,240,0.88);
        }
        .transfer-log-icon {
            width: 36px; height: 36px; border-radius: 12px; display: inline-flex; align-items: center; justify-content: center;
            font-size: 16px; font-weight: 900;
        }
        .transfer-log-icon.export { background: rgba(59,130,246,0.12); color: #1d4ed8; }
        .transfer-log-icon.import { background: rgba(34,197,94,0.12); color: #166534; }
        .transfer-log-title { font-size: 13px; font-weight: 760; color: #0f172a; }
        .transfer-log-meta { margin-top: 4px; font-size: 12px; line-height: 1.55; color: var(--color-text-secondary); }
        .transfer-log-time { font-size: 12px; font-weight: 700; color: var(--color-text-secondary); white-space: nowrap; }
        ${mediaMax(BREAKPOINTS.XL, `.transfer-grid { grid-template-columns: 1fr; } .transfer-type-grid { grid-template-columns: repeat(3, minmax(0, 1fr)); }`)}
        ${mediaMax(BREAKPOINTS.MOBILE, `.transfer-hero { padding: 22px 20px; } .transfer-hero h2 { font-size: 30px; } .transfer-tabs { width: 100%; } .transfer-tab { flex: 1 1 0; min-width: 0; } .transfer-type-grid { grid-template-columns: repeat(2, minmax(0, 1fr)); } .transfer-date-row, .transfer-summary-grid, .transfer-summary-grid.three { grid-template-columns: 1fr; } .transfer-actions { flex-direction: column; align-items: stretch; } .transfer-btn { width: 100%; } .transfer-status-banner { grid-template-columns: 1fr; align-items: start; } .transfer-status-copy { width: 100%; } .transfer-status-pills { width: 100%; justify-content: flex-start; }`)}
        ${mediaMax(BREAKPOINTS.PHONE, `.transfer-type-grid { grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 8px; } .transfer-type-card { padding: 12px 10px; border-radius: 16px; } .transfer-type-title { font-size: 13px; } .transfer-type-hint { margin-top: 4px; font-size: 11px; line-height: 1.4; } .transfer-status-banner { padding: 14px; gap: 12px; } .transfer-status-icon { width: 36px; height: 36px; border-radius: 12px; font-size: 18px; } .transfer-status-title { font-size: 14px; } .transfer-status-meta { font-size: 11px; line-height: 1.6; } .transfer-status-pills { display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 8px; } .transfer-status-pill { justify-content: center; min-width: 0; padding: 0 10px; font-size: 11px; }`)}
    `,
    );
}

// 页面模板只读取规范化后的状态；所有外部文本都在插入 HTML 前转义。
function renderPage() {
    if (!_container || !_state) return;
    ensureStyles();
    const busy =
        (_state.tab === 'export' && (_state.export.loading || _state.export.downloading)) ||
        (_state.tab === 'import' &&
            (_state.import.inspecting || _state.import.executing || _state.import.samplesLoading)) ||
        (_state.tab === 'history' && _state.history.loading);
    _container.innerHTML = `
        <div class="transfer-shell" aria-busy="${busy}">
            <div class="transfer-stack">
                <section class="transfer-hero">
                    <div class="transfer-kicker">Transfer</div>
                    <h2>数据迁移</h2>
                    <p>把日程、待办、记账、笔记和日记导出成正式 bundle，或从 bundle 预检后安全导入。这里优先做可回放、可校验、可筛选的迁移，不做模糊文本导入。</p>
                    <div class="transfer-chip-row">
                        <span class="transfer-chip"><code>.pendo.zip</code> 正式格式</span>
                        <span class="transfer-chip">导入前必须预检</span>
                        <span class="transfer-chip">支持多类别与时间范围</span>
                    </div>
                </section>
                <div class="transfer-tabs" role="group" aria-label="数据迁移视图">
                    <button type="button" class="transfer-tab ${_state.tab === 'export' ? 'active' : ''}" data-transfer-tab="export" aria-pressed="${_state.tab === 'export'}">导出</button>
                    <button type="button" class="transfer-tab ${_state.tab === 'import' ? 'active' : ''}" data-transfer-tab="import" aria-pressed="${_state.tab === 'import'}">导入</button>
                    <button type="button" class="transfer-tab ${_state.tab === 'history' ? 'active' : ''}" data-transfer-tab="history" aria-pressed="${_state.tab === 'history'}">操作记录</button>
                </div>
                ${_state.tab === 'export' ? renderExportTab() : _state.tab === 'import' ? renderImportTab() : renderHistoryTab()}
            </div>
        </div>
    `;
    attachListeners();
}

function renderExportTab() {
    const state = _state.export;
    const busy = state.loading || state.downloading;
    return `
        <section class="transfer-grid">
            <div class="transfer-card">
                <h3>导出备份</h3>
                <p>按类别和时间范围打包数据，生成一个可回放、可校验的 <code>.pendo.zip</code> 文件。建议先预览一次，确认条数和时间口径再下载。</p>
                <div class="transfer-section">
                    <div class="transfer-section-label">导出类别</div>
                    <div class="transfer-type-grid">${TYPE_OPTIONS.map(renderExportTypeCard).join('')}</div>
                </div>
                <div class="transfer-section">
                    <div class="transfer-section-label">时间范围</div>
                    <div class="transfer-preset-row" role="group" aria-label="导出时间范围">${PRESETS.map((item) => `<button type="button" class="transfer-pill ${state.preset === item.value ? 'active' : ''}" data-export-preset="${item.value}" aria-pressed="${state.preset === item.value}" ${busy ? 'disabled' : ''}>${item.label}</button>`).join('')}</div>
                    ${
                        state.preset === 'custom'
                            ? `
                        <div class="transfer-date-row">
                            <div class="transfer-field"><label for="transfer-export-start">开始日期</label><input id="transfer-export-start" type="text" inputmode="numeric" pattern="\\d{4}-\\d{2}-\\d{2}" maxlength="10" autocomplete="off" spellcheck="false" placeholder="YYYY-MM-DD" value="${escapeHtml(state.start)}" ${busy ? 'disabled' : ''}></div>
                            <div class="transfer-field"><label for="transfer-export-end">结束日期</label><input id="transfer-export-end" type="text" inputmode="numeric" pattern="\\d{4}-\\d{2}-\\d{2}" maxlength="10" autocomplete="off" spellcheck="false" placeholder="YYYY-MM-DD" value="${escapeHtml(state.end)}" ${busy ? 'disabled' : ''}></div>
                        </div>`
                            : ''
                    }
                </div>
                <div class="transfer-section">
                    <div class="transfer-actions">
                        <button type="button" class="transfer-btn secondary" id="btn-transfer-preview" ${busy ? 'disabled' : ''} aria-busy="${state.loading}">${state.loading ? '预览中...' : '预览导出'}</button>
                        <button type="button" class="transfer-btn primary" id="btn-transfer-download" ${busy ? 'disabled' : ''} aria-busy="${state.downloading}">${state.downloading ? '生成中...' : '下载备份'}</button>
                    </div>
                </div>
                ${renderExportPreview(state.preview)}
            </div>
            <aside class="transfer-sidecard">
                <h3>格式说明</h3>
                <p>导出文件内部包含 <code>manifest.json</code> 和按类型分开的 <code>ndjson</code> 数据文件，后续可以按全部类别或部分类别重新导入。</p>
                <div class="transfer-note">时间范围按各模块主业务时间字段筛选：日程按开始时间，待办按截止时间或创建时间，记账按账目日期，笔记按创建时间，日记按日记日期。时间以设置页保存的用户时区为准。</div>
            </aside>
        </section>
    `;
}

function renderExportTypeCard(item) {
    const active = _state.export.selectedTypes.includes(item.value);
    const busy = _state.export.loading || _state.export.downloading;
    return `<button type="button" class="transfer-type-card ${active ? 'active' : ''}" data-export-type="${item.value}" aria-pressed="${active}" ${busy ? 'disabled' : ''}><div class="transfer-type-title">${item.label}</div><div class="transfer-type-hint">${item.hint}</div></button>`;
}

function renderExportPreview(preview) {
    if (!preview) {
        return `<div class="transfer-section"><div class="transfer-empty">先点"预览导出"。这里会显示命中的类别数量、总条数和本次 bundle 的时间范围，避免下载后才发现口径选错。</div></div>`;
    }
    const rangeText =
        preview.selection.start && preview.selection.end
            ? `${escapeHtml(preview.selection.start)} 至 ${escapeHtml(preview.selection.end)}`
            : '全部时间';
    const warningsHtml = preview.warnings.length
        ? `<div class="transfer-note preview-warnings">导出校验警告（${preview.warnings.length} 条）：${preview.warnings
              .slice(0, 5)
              .map((warning) => escapeHtml(warning))
              .join('；')}${preview.warnings.length > 5 ? '……' : ''}</div>`
        : '';
    return `
        <div class="transfer-section">
            <div class="transfer-section-label">预览结果</div>
            <div class="transfer-summary-grid three">
                <article class="transfer-summary-card"><div class="transfer-summary-label">总条数</div><div class="transfer-summary-value">${preview.total}</div><div class="transfer-summary-meta">本次 bundle 预计导出记录总数</div></article>
                <article class="transfer-summary-card"><div class="transfer-summary-label">类别数</div><div class="transfer-summary-value">${preview.selection.types.length}</div><div class="transfer-summary-meta">当前勾选参与导出的模块</div></article>
                <article class="transfer-summary-card"><div class="transfer-summary-label">时间范围</div><div class="transfer-summary-value range">${rangeText}</div><div class="transfer-summary-meta">按这个口径打包数据</div></article>
            </div>
            <div class="transfer-list">${preview.counts.map(({ type, count }) => `<div class="transfer-list-row"><div><div class="transfer-list-title">${typeLabel(type)}</div><div class="transfer-list-meta">命中的 ${typeLabel(type)} 记录</div></div><div class="transfer-list-value">${count}</div></div>`).join('')}</div>
            ${warningsHtml}
        </div>`;
}

function renderImportTab() {
    const state = _state.import;
    const busy = state.inspecting || state.executing;
    return `
        <section class="transfer-grid">
            <div class="transfer-card">
                <h3>导入 bundle</h3>
                <p>先上传 <code>.pendo.zip</code> 做预检，再按需要选中部分类别和冲突策略执行导入。预检不会改数据库，执行导入才会真正写入。最大支持 100 MB、全包 20,000 条记录。</p>
                <div class="transfer-section">
                    <div class="transfer-upload" id="transfer-upload-zone" aria-busy="${busy}">
                        <div class="transfer-upload-title">拖进来，或手动选择文件</div>
                        <div class="transfer-upload-hint">只接受 <code>.pendo.zip</code>。预检会先校验 manifest、checksum、schema 和逐行记录合法性。</div>
                        <div class="transfer-file-name">${state.file ? escapeHtml(state.file.name) : '尚未选择文件'}</div>
                        <input class="transfer-file-input" id="transfer-file-input" type="file" accept=".zip,.pendo.zip" aria-label="选择 Pendo bundle 文件" ${busy ? 'disabled' : ''}>
                        <button type="button" class="transfer-btn secondary" id="btn-transfer-pick-file" ${busy ? 'disabled' : ''}>选择文件</button>
                    </div>
                </div>
                ${state.inspect?.already_imported ? renderDuplicateBundleWarning() : ''}
                ${state.result ? renderImportStatusBanner(state.result) : ''}
                <div class="transfer-section"><div class="transfer-actions"><button type="button" class="transfer-btn secondary" id="btn-transfer-inspect" ${busy ? 'disabled' : ''} aria-busy="${state.inspecting}">${state.inspecting ? '预检中...' : '预检文件'}</button><button type="button" class="transfer-btn primary" id="btn-transfer-execute" ${busy || !state.inspect ? 'disabled' : ''} aria-busy="${state.executing}">${state.executing ? '导入中...' : '开始导入'}</button></div></div>
                ${renderImportInspect(state)}
            </div>
            <aside class="transfer-sidecard">
                <h3>导入策略</h3>
                <p>默认隔离导入；跳过/覆盖只匹配当前用户、同类型且此前导入时记录过的同一来源，不会拿外部 ID 直接覆盖现有内部记录。</p>
                <div class="transfer-section"><div class="transfer-section-label">冲突策略</div>${CONFLICT_POLICIES.map((item) => renderPolicy(item, 'transfer-conflict-policy', state.conflictPolicy)).join('')}</div>
                <div class="transfer-section"><div class="transfer-section-label">非法记录处理</div>${INVALID_POLICIES.map((item) => renderPolicy(item, 'transfer-invalid-policy', state.invalidPolicy)).join('')}</div>
                ${state.inspect?.already_imported ? `<div class="transfer-section"><label class="transfer-check"><input type="checkbox" id="transfer-force-reimport" aria-label="强制重新导入" ${state.forceReimport ? 'checked' : ''} ${busy ? 'disabled' : ''}><span>强制重新导入（此 bundle 已导入过）</span></label></div>` : ''}
                <div class="transfer-section">
                    <div class="transfer-section-label">导入示例</div>
                    <div class="transfer-note">如果你手里是别的工具导出的 JSON、CSV 或数据库结果，可以先把字段重组到下面这套 bundle 结构，再压缩成 <code>.pendo.zip</code> 导入。示例里的 JSON 做了换行便于阅读，真正的 <code>ndjson</code> 文件需要"一行一条记录"。</div>
                    ${renderImportExamples()}
                </div>
            </aside>
        </section>
    `;
}

function renderDuplicateBundleWarning() {
    return `
        <div class="transfer-section">
            <div class="transfer-status-banner duplicate-warn">
                <div class="transfer-status-icon" aria-hidden="true">!</div>
                <div class="transfer-status-copy">
                    <div class="transfer-status-title">此 bundle 已导入过</div>
                    <div class="transfer-status-meta">系统检测到这个 bundle 之前已成功导入。如果你确实需要重新导入，请在右侧勾选"强制重新导入"。</div>
                </div>
                <div></div>
            </div>
        </div>
    `;
}

function renderPolicy(item, name, current) {
    const disabled = _state.import.inspecting || _state.import.executing;
    return `<label class="transfer-check"><input type="radio" name="${name}" value="${item.value}" aria-label="${item.label}" ${current === item.value ? 'checked' : ''} ${disabled ? 'disabled' : ''}><span>${item.label}</span></label><div class="transfer-list-meta transfer-policy-desc">${item.desc}</div>`;
}

function renderExampleBlock(title, tip, code) {
    return `
        <div class="transfer-example-block">
            <div class="transfer-example-title">${escapeHtml(title)}</div>
            <div class="transfer-example-tip">${escapeHtml(tip)}</div>
            <pre class="transfer-code"><code>${escapeHtml(code)}</code></pre>
        </div>
    `;
}

function renderImportExamples() {
    const bundleTree = [
        'your-backup.pendo.zip',
        '  manifest.json',
        '  data/',
        '    events.ndjson',
        '    event_collections.ndjson',
        '    tasks.ndjson',
        '    ledger.ndjson',
        '    notes.ndjson',
        '    diary.ndjson',
    ].join('\n');
    const manifest = JSON.stringify(
        {
            format: 'pendo-bundle',
            version: 2,
            bundle_id: 'my-first-import',
            exported_at: '2026-03-29T20:00:00+08:00',
            source: { app: 'external-tool', timezone: 'Asia/Shanghai' },
            selection: {
                types: ['event', 'task', 'ledger', 'note', 'diary'],
                preset: 'all',
                start: null,
                end: null,
            },
            files: [
                { path: 'data/events.ndjson', type: 'event', count: 3 },
                {
                    path: 'data/event_collections.ndjson',
                    type: 'event_collection',
                    count: 2,
                },
                { path: 'data/tasks.ndjson', type: 'task', count: 2 },
                { path: 'data/ledger.ndjson', type: 'ledger', count: 3 },
                { path: 'data/notes.ndjson', type: 'note', count: 1 },
                { path: 'data/diary.ndjson', type: 'diary', count: 1 },
            ],
            attachments_mode: 'metadata_only',
        },
        null,
        2,
    );
    const eventCollections = [
        JSON.stringify({
            id: 'conf_2026',
            kind: 'multi_node',
            title: '学术会议',
            category: '工作',
            location: '北京',
            start_time: '2026-05-10T09:00:00+08:00',
            end_time: '2026-05-12T18:00:00+08:00',
            notes: '集合记录大标题、地点、备注；每个节点仍写在 events.ndjson',
        }),
        JSON.stringify({
            id: 'standup_2026',
            kind: 'recurring',
            title: '每周站会',
            category: '工作',
            rrule: 'FREQ=WEEKLY;COUNT=4',
            timezone: 'Asia/Shanghai',
            reminder_rules: [{ offset_seconds: 900 }],
        }),
    ].join('\n');
    const events = [
        JSON.stringify({
            id: 'event_single_001',
            title: '牙医复诊',
            start_time: '2026-05-03T15:00:00+08:00',
            end_time: '2026-05-03T16:00:00+08:00',
            timezone: 'Asia/Shanghai',
            location: '社区医院',
            notes: '单次日程。没有提醒时显式写空数组。',
            reminder_rules: [],
            remind_times: [],
        }),
        JSON.stringify({
            id: 'conf_2026_node_01',
            title: '摘要截止',
            start_time: '2026-05-10T09:00:00+08:00',
            event_role: 'multi_node_child',
            event_collection_id: 'conf_2026',
            event_collection_kind: 'multi_node',
            event_index: 1,
            event_node_key: 'abstract_deadline',
            reminder_rules: [{ offset_seconds: 86400 }, { offset_seconds: 0 }],
        }),
        JSON.stringify({
            id: 'standup_2026_occ_01',
            title: '每周站会',
            start_time: '2026-05-04T10:00:00+08:00',
            end_time: '2026-05-04T10:30:00+08:00',
            event_role: 'recurring_occurrence',
            event_collection_id: 'standup_2026',
            event_collection_kind: 'recurring',
            event_index: 1,
            source_item_id: 'standup_2026',
            reminder_rules: [{ offset_seconds: 900 }],
        }),
    ].join('\n');
    const tasks = [
        JSON.stringify({
            id: 'task_001',
            title: '交报告',
            content: '把周报发给项目组',
            status: 'open',
            priority: 2,
            category: '工作',
            plan_date: '2026-05-06',
            deadline_at: '2026-05-06T18:00:00+08:00',
            reminder_rules: [{ offset_seconds: 1800 }],
        }),
        JSON.stringify({
            id: 'task_002',
            title: '已完成任务',
            status: 'done',
            priority: 3,
            completed_at: '2026-05-01T20:10:00+08:00',
        }),
    ].join('\n');
    const ledger = [
        JSON.stringify({
            id: 'ledger_expense_001',
            title: '午饭',
            amount_cents: 3250,
            currency: 'CNY',
            transaction_type: 'expense',
            ledger_category: '餐饮',
            ledger_date: '2026-05-01',
            account_name: '微信',
            merchant: '食堂',
            remark: '金额优先写 amount_cents，单位是分。',
        }),
        JSON.stringify({
            id: 'ledger_income_001',
            title: '工资',
            amount_cents: 1800000,
            transaction_type: 'income',
            ledger_category: '工资',
            ledger_date: '2026-05-05',
            account_name: '招商银行卡',
        }),
        JSON.stringify({
            id: 'ledger_transfer_001',
            title: '转入储蓄账户',
            amount_cents: 200000,
            transaction_type: 'transfer',
            ledger_category: '转账',
            ledger_date: '2026-05-08',
            account_name: '招商银行卡',
            counter_account_name: '储蓄卡',
        }),
    ].join('\n');
    const notes = JSON.stringify({
        id: 'note_001',
        title: '迁移注意事项',
        content: '支持 Markdown/HTML 文本，前端会按文本安全渲染。',
        tags: ['迁移', 'pendo'],
        category: '知识',
        references: [{ kind: 'item', id: 'task_001', type: 'task', title: '交报告' }],
        related_items: ['task_001'],
    });
    const diary = JSON.stringify({
        id: 'diary_2026_05_01',
        title: '劳动节',
        content: '今天完成了数据整理。',
        diary_date: '2026-05-01',
        entry_time: '2026-05-01T22:10:00+08:00',
        mood: 'happy',
        mood_score: 8,
        weather: '晴',
        location: '上海',
        is_favorite: true,
        template_answers: [{ prompt: '今天最重要的事', answer: '补齐导入数据' }],
    });
    const fullRecord = JSON.stringify(
        {
            _type: 'task',
            _schema: 2,
            id: 'task_20260329_review',
            title: '补完迁移文档',
            content: '整理导入导出字段说明',
            category: '工作',
            priority: 3,
            status: 'open',
            plan_date: '2026-03-30',
            deadline_at: '2026-03-30T18:00:00+08:00',
            created_at: '2026-03-29T10:30:00+08:00',
            updated_at: '2026-03-29T10:30:00+08:00',
            tags: ['迁移', '重要'],
            visibility: 'private',
            context: { source: 'manual' },
            attachments: [],
            ai_meta: {},
            deleted: false,
        },
        null,
        2,
    );
    return [
        `<div class="transfer-note">支持 <strong>event、task、ledger、note、diary</strong> 五类条目，以及用于多节点/重复日程的 <strong>event_collection</strong>。精简模式只写业务字段；完整模式可以带 <code>_type</code>、<code>_schema: 2</code>、<code>count</code> 和 <code>sha256</code>。</div>`,
        renderExampleBlock(
            '1. 压缩包目录',
            'zip 根目录必须包含 manifest.json，数据文件必须放在 data/ 下；不用导入的类型可以不写对应文件。',
            bundleTree,
        ),
        renderExampleBlock(
            '2. manifest.json',
            'files 声明每个数据文件的 path 和 type。count、sha256 可省略；带上时导入会做行数和校验和验证。bundle_id 可以用任意唯一字符串，例如稳定自定义字符串，重复导入时会据此识别。',
            manifest,
        ),
        renderExampleBlock(
            '3. 日程集合 event_collections.ndjson',
            '多节点和重复日程先写集合，再在 events.ndjson 写具体节点/occurrence。kind 只能是 multi_node 或 recurring。',
            eventCollections,
        ),
        renderExampleBlock(
            '4. 日程 events.ndjson',
            '单次日程直接写 start_time；多节点/重复实例用 event_collection_id 关联集合。提醒跟着事件走：reminder_rules 的 offset_seconds 表示提前多少秒提醒，0 表示事件开始时提醒；没有提醒时写空数组。',
            events,
        ),
        renderExampleBlock(
            '5. 待办 tasks.ndjson',
            'status 可用 open、done、cancelled；priority 范围 1-5；deadline_at 用 ISO 时间。任务提醒通常按 deadline_at 计算。',
            tasks,
        ),
        renderExampleBlock(
            '6. 记账 ledger.ndjson',
            '金额优先使用 amount_cents，单位是分。transaction_type 可用 expense、income、transfer；transfer 必须写 counter_account_name。',
            ledger,
        ),
        renderExampleBlock(
            '7. 笔记 notes.ndjson',
            'title 必填；content、tags、category、references、related_items 都可以按需填写。references 必须带 id；外部网址请放在 content 中或转成一条 note 再引用。HTML/Markdown 会作为文本内容保存和安全展示。',
            notes,
        ),
        renderExampleBlock(
            '8. 日记 diary.ndjson',
            'diary_date 必填且格式为 YYYY-MM-DD；content 不能为空；mood_score 范围 1-10。',
            diary,
        ),
        renderExampleBlock(
            '9. 完整记录格式',
            '完整模式下每条记录可带 _type 和 _schema: 2 元字段，导入时会按当前重构后的字段集合严格校验。',
            fullRecord,
        ),
        `<div class="transfer-note">通用字段：<code>id、title、content、tags、category、created_at、updated_at、context、visibility、attachments、ai_meta、deleted、deleted_at</code>。日期时间优先使用带偏移的 ISO 8601；naive 时间会按 manifest 的 IANA <code>source.timezone</code> 解释并转为 UTC，DST 歧义或不存在时刻会报错；纯日期字段用 <code>YYYY-MM-DD</code>。每个 <code>ndjson</code> 文件一行一条 JSON。外部 ID 只作为来源元数据；新增/副本/隔离会生成内部 UUID，覆盖只复用此前同来源导入记录的内部 UUID。未知字段会在预检阶段报错。</div>`,
    ].join('');
}

function renderImportInspect(state) {
    if (!state.inspect) {
        return `<div class="transfer-section"><div class="transfer-empty">上传 bundle 并点击"预检文件"后，这里会显示文件摘要、可导入类别、样例记录和校验错误。没有预检结果前，导入按钮会保持禁用。</div></div>`;
    }
    const inspect = state.inspect;
    const busy = state.inspecting || state.executing;
    const warningsHtml = inspect.warnings.length
        ? `<div class="transfer-section"><div class="transfer-section-label">预检警告</div><div class="transfer-note">${inspect.warnings.map((warning) => escapeHtml(warning)).join('；')}</div></div>`
        : '';
    return `
        <div class="transfer-section">
            <div class="transfer-section-label">文件摘要</div>
            <div class="transfer-summary-grid three">
                <article class="transfer-summary-card"><div class="transfer-summary-label">有效记录</div><div class="transfer-summary-value">${inspect.counts.valid}</div><div class="transfer-summary-meta">通过 schema 和字段校验的记录数</div></article>
                <article class="transfer-summary-card"><div class="transfer-summary-label">错误记录</div><div class="transfer-summary-value">${inspect.counts.errors}</div><div class="transfer-summary-meta">非法记录会在下面展开说明</div></article>
                <article class="transfer-summary-card"><div class="transfer-summary-label">文件数</div><div class="transfer-summary-value">${inspect.summary.files}</div><div class="transfer-summary-meta">bundle 中实际包含的数据文件数</div></article>
            </div>
        </div>
        <div class="transfer-section"><div class="transfer-section-label">要导入的类别</div><div class="transfer-inline-checks">${inspect.summary.types.map((type) => `<label class="transfer-check"><input type="checkbox" data-import-type="${type}" aria-label="导入${typeLabel(type)}" ${state.selectedTypes.includes(type) ? 'checked' : ''} ${busy ? 'disabled' : ''}><span>${typeLabel(type)}</span></label>`).join('')}</div></div>
        <div class="transfer-section"><div class="transfer-section-label">文件明细</div><div class="transfer-list">${inspect.files.map((file) => `<div class="transfer-list-row"><div><div class="transfer-list-title">${escapeHtml(file.path)}</div><div class="transfer-list-meta">${typeLabel(file.type)}，共 ${file.count} 行，合法 ${file.valid} 行</div></div><div class="transfer-list-value">${file.count}</div></div>`).join('')}</div></div>
        ${warningsHtml}
        <div class="transfer-section">
            <div class="transfer-section-label">样例记录${inspect.counts.total_samples > 5 ? ` (共 ${inspect.counts.total_samples} 条)` : ''}</div>
            ${renderSamplesWithPager(state)}
        </div>
        <div class="transfer-section"><div class="transfer-section-label">校验错误</div>${renderValidationErrors(inspect.errors, inspect.counts.errors, '当前文件没有发现结构性错误，可以直接继续导入。')}</div>
        ${state.result ? renderImportResult(state.result) : ''}
    `;
}

function renderValidationErrors(errors, total, emptyMessage) {
    const hiddenCount = Math.max(0, total - errors.length);
    const rows = errors.length
        ? errors
              .map(
                  (error) =>
                      `<div class="transfer-error-row"><div class="transfer-error-title">${escapeHtml(error.path)} · 第 ${error.line || '?'} 行</div><div class="transfer-error-meta">${escapeHtml(error.message)}</div></div>`,
              )
              .join('')
        : `<div class="transfer-empty">${escapeHtml(emptyMessage)}</div>`;
    return `<div class="transfer-errors">${rows}${hiddenCount ? `<div class="transfer-empty">另有 ${hiddenCount} 条错误未在页面展开，请修复已显示问题后重新预检。</div>` : ''}</div>`;
}

function renderSamplesWithPager(state) {
    const inspect = state.inspect;
    const totalSamples = state.paginatedSamples?.total ?? inspect.counts.total_samples;
    const displaySamples = state.paginatedSamples?.samples || inspect.samples || [];
    const samplesHtml = displaySamples.length
        ? displaySamples
              .map(
                  (sample) =>
                      `<div class="transfer-sample-row"><div class="transfer-sample-title">${typeLabel(sample.type)} · ${escapeHtml(sample.title || '无标题')}</div><div class="transfer-sample-meta">ID: ${escapeHtml(sample.id || 'N/A')}</div></div>`,
              )
              .join('')
        : '<div class="transfer-empty">没有可展示的样例记录。</div>';

    let pagerHtml = '';
    if (totalSamples > 5) {
        const currentPage = state.paginatedSamples?.page || 1;
        const pageSize = positiveInteger(state.paginatedSamples?.page_size || state.samplePageSize, 5);
        const totalPages = Math.ceil(totalSamples / pageSize);
        pagerHtml = `
            <div class="transfer-pager">
                <button type="button" class="transfer-btn sm secondary" id="btn-samples-prev" ${currentPage <= 1 || state.samplesLoading ? 'disabled' : ''}>上一页</button>
                <span class="transfer-pager-info">${state.samplesLoading ? '加载中...' : `第 ${currentPage} / ${totalPages} 页`}</span>
                <button type="button" class="transfer-btn sm secondary" id="btn-samples-next" ${currentPage >= totalPages || state.samplesLoading ? 'disabled' : ''}>下一页</button>
            </div>
        `;
    }

    return `<div class="transfer-samples">${samplesHtml}</div>${pagerHtml}`;
}

function renderImportStatusBanner(result) {
    const hasFailures = result.results.failed > 0 || result.counts.errors > 0;
    const processed = result.results.inserted + result.results.updated;
    const kindClass = hasFailures ? 'warning' : '';
    const icon = hasFailures ? '!' : '✓';
    const title = hasFailures ? '导入已完成，但有部分记录未导入' : '导入已完成';
    const typeText = result.summary.types.map(typeLabel).join('、') || '所选类别';
    const summary = hasFailures
        ? `本次成功处理 ${processed} 条，写入失败 ${result.results.failed} 条，校验错误 ${result.counts.errors} 条。下方保留了可定位的问题明细。`
        : `本次成功处理 ${processed} 条记录，已写入 ${typeText}。下方保留了导入明细。`;
    return `
        <div class="transfer-section">
            <div class="transfer-status-banner ${kindClass}">
                <div class="transfer-status-icon" aria-hidden="true">${icon}</div>
                <div class="transfer-status-copy">
                    <div class="transfer-status-title">${title}</div>
                    <div class="transfer-status-meta">${summary}</div>
                </div>
                <div class="transfer-status-pills">
                    <span class="transfer-status-pill">新增 ${result.results.inserted}</span>
                    <span class="transfer-status-pill">更新 ${result.results.updated}</span>
                    <span class="transfer-status-pill">跳过 ${result.results.skipped}</span>
                    <span class="transfer-status-pill">失败 ${result.results.failed}</span>
                    ${result.counts.errors ? `<span class="transfer-status-pill">校验错误 ${result.counts.errors}</span>` : ''}
                </div>
            </div>
        </div>
    `;
}

function renderImportResult(result) {
    const typeText = result.summary.types.map(typeLabel).join('、') || '所选类别';
    const warningsHtml = result.warnings.length
        ? `<div class="transfer-note">导入警告（${result.warnings.length} 条）：${result.warnings.map((warning) => escapeHtml(warning)).join('；')}</div>`
        : '';
    const validationErrorsHtml = result.counts.errors
        ? `<div class="transfer-result-group"><div class="transfer-result-title">未导入的校验错误</div>${renderValidationErrors(result.errors, result.counts.errors, '接口没有返回可展示的校验错误明细。')}</div>`
        : '';
    return `
        <div class="transfer-section">
            <div class="transfer-section-label">导入结果</div>
            <div class="transfer-summary-grid three">
                <article class="transfer-summary-card"><div class="transfer-summary-label">新增</div><div class="transfer-summary-value">${result.results.inserted}</div><div class="transfer-summary-meta">本次新写入的记录</div></article>
                <article class="transfer-summary-card"><div class="transfer-summary-label">更新</div><div class="transfer-summary-value">${result.results.updated}</div><div class="transfer-summary-meta">按同用户、同类型、同来源覆盖的记录</div></article>
                <article class="transfer-summary-card"><div class="transfer-summary-label">跳过 / 未导入</div><div class="transfer-summary-value">${result.results.skipped + result.results.failed + result.counts.errors}</div><div class="transfer-summary-meta">包括跳过冲突、写入失败和校验错误</div></article>
            </div>
            <div class="transfer-note">这次实际处理了 ${typeText}。下面按动作展开最近处理过的记录，每类最多展示 ${MAX_RENDERED_RESULTS} 条。</div>
            ${warningsHtml}
            ${validationErrorsHtml}
            <div class="transfer-result-group">${renderResultRows('新增', 'inserted', result.details.inserted, result.results.inserted)}</div>
            <div class="transfer-result-group">${renderResultRows('更新', 'updated', result.details.updated, result.results.updated)}</div>
            <div class="transfer-result-group">${renderResultRows('跳过', 'skipped', result.details.skipped, result.results.skipped)}</div>
            <div class="transfer-result-group">${renderResultRows('失败', 'failed', result.details.failed, result.results.failed)}</div>
        </div>
    `;
}

function renderResultRows(title, kind, rows, total) {
    if (!rows.length) {
        const message = total ? `接口未返回可展示的${title}明细。` : `这次没有${title}记录。`;
        return `<div class="transfer-empty"><div class="transfer-result-title">${title}</div><div class="transfer-result-meta">${message}</div></div>`;
    }
    const hiddenCount = Math.max(0, total - rows.length);
    return `
        <div class="transfer-result-title">${title}</div>
        ${rows
            .map(
                (row) => `
            <div class="transfer-result-row">
                <div class="transfer-result-badge ${kind}">${title}</div>
                <div>
                    <div class="transfer-result-name">${typeLabel(row.type)} · ${escapeHtml(row.title || '无标题')}</div>
                    <div class="transfer-result-meta">ID: ${escapeHtml(row.id || 'N/A')}${row.reason ? ` · ${escapeHtml(row.reason)}` : ''}</div>
                </div>
            </div>
        `,
            )
            .join('')}
        ${hiddenCount ? `<div class="transfer-empty">另有 ${hiddenCount} 条${title}记录未展开。</div>` : ''}
    `;
}

function renderHistoryTab() {
    const state = _state.history;
    if (state.loading) {
        return `<div class="transfer-card"><div class="transfer-empty" role="status" aria-live="polite">正在加载操作记录...</div></div>`;
    }
    if (!state.logs.length) {
        return `<div class="transfer-card"><h3>操作记录</h3><p>这里会记录每次导入和导出操作的审计日志，方便追踪数据变更历史。</p><div class="transfer-section"><div class="transfer-empty">暂无操作记录。完成一次导入或导出后，这里会自动出现。</div></div></div>`;
    }
    return `
        <div class="transfer-card">
            <h3>操作记录</h3>
            <p>所有导入和导出操作的审计日志。</p>
            <div class="transfer-section">
                <div class="transfer-list">
                    ${state.logs.map(renderLogRow).join('')}
                </div>
            </div>
        </div>
    `;
}

function renderLogRow(log) {
    const isImport = log.action === 'import';
    const icon = isImport ? '↓' : '↑';
    const iconClass = isImport ? 'import' : 'export';
    const label = isImport ? '导入' : '导出';
    const types = Array.isArray(log.types) ? log.types : [];
    const typesText = types.map(typeLabel).join('、') || '未知';
    const summary = log.result_summary || {};
    let detail = `${log.record_count || 0} 条记录`;
    if (isImport && log.has_result_counts) {
        detail = `新增 ${summary.inserted}，更新 ${summary.updated || 0}，跳过 ${summary.skipped || 0}`;
    }
    const time = log.created_at ? formatZonedDateTime(log.created_at, '') : '';
    const filename = log.filename ? escapeHtml(log.filename) : '';
    return `
        <div class="transfer-log-row">
            <div class="transfer-log-icon ${iconClass}">${icon}</div>
            <div>
                <div class="transfer-log-title">${label} · ${typesText}</div>
                <div class="transfer-log-meta">${detail}${filename ? ` · ${filename}` : ''}</div>
            </div>
            <div class="transfer-log-time">${time}</div>
        </div>
    `;
}

// 页面每次重绘后重新绑定当前节点，避免保留已经脱离 DOM 的监听器引用。
function attachListeners() {
    if (!_container || !_state) return;
    _container.querySelectorAll('[data-transfer-tab]').forEach((button) => {
        button.onclick = () => {
            const tab = button.dataset.transferTab;
            if (!TABS.has(tab) || tab === _state.tab) return;
            _state.tab = tab;
            if (tab === 'history' && !_state.history.loaded) {
                if (_state.history.loading) renderPage();
                else void loadHistory();
                return;
            }
            renderPage();
        };
    });
    if (_state.tab === 'export') return attachExportListeners();
    if (_state.tab === 'import') return attachImportListeners();
}

function attachExportListeners() {
    _container.querySelectorAll('[data-export-type]').forEach((button) => {
        button.onclick = () => toggleExportType(button.dataset.exportType);
    });
    _container.querySelectorAll('[data-export-preset]').forEach((button) => {
        button.onclick = () => {
            const preset = button.dataset.exportPreset;
            if (
                !PRESET_VALUES.has(preset) ||
                preset === _state.export.preset ||
                _state.export.loading ||
                _state.export.downloading
            )
                return;
            _state.export.preset = preset;
            if (preset !== 'custom') {
                _state.export.start = '';
                _state.export.end = '';
            }
            renderPage();
        };
    });
    const startInput = _container.querySelector('#transfer-export-start');
    const endInput = _container.querySelector('#transfer-export-end');
    if (startInput) {
        startInput.oninput = () => {
            if (!_state.export.loading && !_state.export.downloading) _state.export.start = startInput.value;
        };
    }
    if (endInput) {
        endInput.oninput = () => {
            if (!_state.export.loading && !_state.export.downloading) _state.export.end = endInput.value;
        };
    }
    _container.querySelector('#btn-transfer-preview')?.addEventListener('click', previewExport);
    _container.querySelector('#btn-transfer-download')?.addEventListener('click', downloadExport);
}

function attachImportListeners() {
    const fileInput = _container.querySelector('#transfer-file-input');
    const uploadZone = _container.querySelector('#transfer-upload-zone');
    _container.querySelector('#btn-transfer-pick-file')?.addEventListener('click', () => fileInput?.click());
    if (fileInput) {
        fileInput.onchange = () => {
            setImportFile(fileInput.files?.[0] || null);
            renderPage();
        };
    }
    if (uploadZone) {
        uploadZone.ondragover = (event) => {
            event.preventDefault();
            if (!_state.import.inspecting && !_state.import.executing) uploadZone.classList.add('dragover');
        };
        uploadZone.ondragleave = () => uploadZone.classList.remove('dragover');
        uploadZone.ondrop = (event) => {
            event.preventDefault();
            uploadZone.classList.remove('dragover');
            if (_state.import.inspecting || _state.import.executing) return;
            const file = event.dataTransfer?.files?.[0] || null;
            if (file) {
                setImportFile(file);
                renderPage();
            }
        };
    }
    _container.querySelector('#btn-transfer-inspect')?.addEventListener('click', inspectImportFile);
    _container.querySelector('#btn-transfer-execute')?.addEventListener('click', executeImportFile);
    _container.querySelectorAll('input[name="transfer-conflict-policy"]').forEach((input) => {
        input.onchange = () => {
            if (!_state.import.inspecting && !_state.import.executing && CONFLICT_POLICY_VALUES.has(input.value))
                _state.import.conflictPolicy = input.value;
        };
    });
    _container.querySelectorAll('input[name="transfer-invalid-policy"]').forEach((input) => {
        input.onchange = () => {
            if (!_state.import.inspecting && !_state.import.executing && INVALID_POLICY_VALUES.has(input.value))
                _state.import.invalidPolicy = input.value;
        };
    });
    _container.querySelectorAll('[data-import-type]').forEach((input) => {
        input.onchange = () => updateImportType(input.dataset.importType, input.checked);
    });
    const forceCheckbox = _container.querySelector('#transfer-force-reimport');
    if (forceCheckbox) {
        forceCheckbox.onchange = () => {
            if (!_state.import.inspecting && !_state.import.executing) {
                _state.import.forceReimport = forceCheckbox.checked;
            }
        };
    }
    _container.querySelector('#btn-samples-prev')?.addEventListener('click', () => {
        void loadSamplePage(_state.import.samplePage - 1);
    });
    _container.querySelector('#btn-samples-next')?.addEventListener('click', () => {
        void loadSamplePage(_state.import.samplePage + 1);
    });
}

function updateImportType(type, checked) {
    if (
        _state.import.inspecting ||
        _state.import.executing ||
        !TYPE_VALUES.has(type) ||
        !_state.import.inspect?.summary.types.includes(type)
    )
        return;
    if (checked) {
        if (!_state.import.selectedTypes.includes(type)) _state.import.selectedTypes.push(type);
        return;
    }
    _state.import.selectedTypes = _state.import.selectedTypes.filter((value) => value !== type);
}

function typeLabel(type) {
    if (type === 'event_collection') return '日程集合';
    return TYPE_LABELS.get(type) || '未知类型';
}

function toggleExportType(type) {
    if (!TYPE_VALUES.has(type) || _state.export.loading || _state.export.downloading) return;
    const selected = _state.export.selectedTypes;
    if (selected.includes(type)) {
        if (selected.length === 1) return showToast('至少保留一个导出类别', 'warning');
        _state.export.selectedTypes = selected.filter((value) => value !== type);
    } else {
        _state.export.selectedTypes = [...selected, type];
    }
    renderPage();
}

// 所有异步动作都捕获本轮状态，并在回写前确认页面、文件和选择条件仍然有效。
async function previewExport() {
    const state = _state;
    if (!state || state.export.loading || state.export.downloading) return false;
    const selection = exportSelection();
    if (selection.error) {
        showToast(selection.error, 'warning');
        return false;
    }

    state.export.loading = true;
    renderPage();
    try {
        const response = await api.post('/transfer/export/preview', {
            selection: selection.payload,
        });
        if (!isCurrentState(state) || exportSelection().signature !== selection.signature) return false;
        const preview = normalizeExportPreview(response?.data);
        if (!preview) throw new Error('预览响应格式无效');
        state.export.preview = preview;
        return true;
    } catch (error) {
        if (isCurrentState(state)) showToast(`导出预览失败：${errorMessage(error)}`, 'error');
        return false;
    } finally {
        if (isCurrentState(state)) {
            state.export.loading = false;
            renderPage();
        }
    }
}

async function downloadExport() {
    const state = _state;
    if (!state || state.export.loading || state.export.downloading) return false;
    const selection = exportSelection();
    if (selection.error) {
        showToast(selection.error, 'warning');
        return false;
    }

    state.export.downloading = true;
    renderPage();
    try {
        const { blob, filename } = await apiDownload('/transfer/export/download', {
            selection: selection.payload,
        });
        if (!isCurrentState(state) || exportSelection().signature !== selection.signature) return false;
        const url = URL.createObjectURL(blob);
        let anchor = null;
        try {
            anchor = document.createElement('a');
            anchor.href = url;
            anchor.download = textValue(filename) || 'pendo-export.pendo.zip';
            document.body.appendChild(anchor);
            anchor.click();
        } finally {
            anchor?.remove();
            URL.revokeObjectURL(url);
        }
        showToast('备份已开始下载', 'success');
        return true;
    } catch (error) {
        if (isCurrentState(state)) showToast(`导出失败：${errorMessage(error)}`, 'error');
        return false;
    } finally {
        if (isCurrentState(state)) {
            state.export.downloading = false;
            renderPage();
        }
    }
}

async function inspectImportFile() {
    const state = _state;
    if (!state || state.import.inspecting || state.import.executing) return false;
    const file = state.import.file;
    if (!file) {
        showToast('请先选择一个 .pendo.zip 文件', 'warning');
        return false;
    }

    const generation = state.import.generation;
    state.import.inspecting = true;
    state.import.inspect = null;
    state.import.selectedTypes = [];
    state.import.result = null;
    state.import.samplePage = 1;
    state.import.paginatedSamples = null;
    renderPage();
    try {
        const response = await apiUpload('/transfer/import/inspect', file, {
            'Content-Type': 'application/zip',
            'X-Transfer-Filename': transferFilename(file),
        });
        if (!isCurrentState(state) || state.import.file !== file || state.import.generation !== generation)
            return false;
        const inspect = normalizeInspect(response?.data);
        if (!inspect) throw new Error('预检响应格式无效');
        state.import.inspect = inspect;
        state.import.selectedTypes = [...inspect.summary.types];
        state.import.forceReimport = false;
        showToast('文件预检完成', 'success');
        return true;
    } catch (error) {
        if (isCurrentState(state) && state.import.file === file && state.import.generation === generation) {
            showToast(`预检失败：${errorMessage(error)}`, 'error');
        }
        return false;
    } finally {
        if (isCurrentState(state) && state.import.file === file && state.import.generation === generation) {
            state.import.inspecting = false;
            renderPage();
        }
    }
}

async function executeImportFile() {
    const state = _state;
    if (!state || state.import.inspecting || state.import.executing) return false;
    const file = state.import.file;
    const inspect = state.import.inspect;
    if (!file || !inspect) {
        showToast('请先完成文件预检', 'warning');
        return false;
    }
    const selectedTypes = normalizeTypes(state.import.selectedTypes).filter((type) =>
        inspect.summary.types.includes(type),
    );
    if (!selectedTypes.length) {
        showToast('至少选择一个要导入的类别', 'warning');
        return false;
    }
    if (
        !CONFLICT_POLICY_VALUES.has(state.import.conflictPolicy) ||
        !INVALID_POLICY_VALUES.has(state.import.invalidPolicy)
    ) {
        showToast('导入策略无效，请重新选择', 'warning');
        return false;
    }

    const generation = state.import.generation;
    state.import.executing = true;
    state.import.result = null;
    renderPage();
    try {
        const response = await apiUpload('/transfer/import/execute', file, {
            'Content-Type': 'application/zip',
            'X-Transfer-Filename': transferFilename(file),
            'X-Transfer-Options': JSON.stringify({
                types: selectedTypes,
                conflict_policy: state.import.conflictPolicy,
                invalid_policy: state.import.invalidPolicy,
                force: state.import.forceReimport === true,
            }),
        });
        if (
            !isCurrentState(state) ||
            state.import.file !== file ||
            state.import.inspect !== inspect ||
            state.import.generation !== generation
        )
            return false;
        const result = normalizeImportResult(response?.data);
        if (!result) throw new Error('导入响应格式无效');
        state.import.result = result;
        showToast('导入已执行', 'success');
        state.history.loaded = false;
        return true;
    } catch (error) {
        if (isCurrentState(state) && state.import.file === file && state.import.generation === generation) {
            showToast(`导入失败：${errorMessage(error)}`, 'error');
        }
        return false;
    } finally {
        if (isCurrentState(state) && state.import.file === file && state.import.generation === generation) {
            state.import.executing = false;
            renderPage();
        }
    }
}

async function loadSamplePage(page) {
    const state = _state;
    if (!state || !state.import.file || !state.import.inspect || state.import.samplesLoading) return false;
    const file = state.import.file;
    const generation = state.import.generation;
    const pageSize = positiveInteger(state.import.samplePageSize, 5);
    const totalPages = Math.max(1, Math.ceil(state.import.inspect.counts.total_samples / pageSize));
    const nextPage = Math.min(Math.max(1, Number.isInteger(page) ? page : 1), totalPages);
    const previousPage = state.import.samplePage;
    if (state.import.paginatedSamples?.page === nextPage) return false;

    state.import.samplesLoading = true;
    state.import.samplePage = nextPage;
    renderPage();
    try {
        const response = await apiUpload('/transfer/import/samples', file, {
            'Content-Type': 'application/zip',
            'X-Transfer-Filename': transferFilename(file),
            'X-Transfer-Page': String(nextPage),
            'X-Transfer-Page-Size': String(pageSize),
        });
        if (!isCurrentState(state) || state.import.file !== file || state.import.generation !== generation)
            return false;
        const samplePage = normalizeSamplePage(response?.data);
        if (!samplePage) throw new Error('样例响应格式无效');
        state.import.paginatedSamples = samplePage;
        state.import.samplePage = samplePage.page;
        return true;
    } catch (error) {
        if (isCurrentState(state) && state.import.file === file && state.import.generation === generation) {
            state.import.samplePage = previousPage;
            showToast(`加载样例失败：${errorMessage(error)}`, 'error');
        }
        return false;
    } finally {
        if (isCurrentState(state) && state.import.file === file && state.import.generation === generation) {
            state.import.samplesLoading = false;
            renderPage();
        }
    }
}

async function loadHistory() {
    const state = _state;
    if (!state || state.history.loading) return false;
    state.history.loading = true;
    renderPage();
    try {
        const response = await api.get('/transfer/logs', { limit: 50 });
        if (!isCurrentState(state)) return false;
        state.history.logs = normalizeLogs(response?.data?.logs);
        state.history.loaded = true;
        return true;
    } catch (error) {
        if (isCurrentState(state)) showToast(`加载操作记录失败：${errorMessage(error)}`, 'error');
        return false;
    } finally {
        if (isCurrentState(state)) {
            state.history.loading = false;
            renderPage();
        }
    }
}

// 路由生命周期只保留当前页面状态；销毁后，迟到的请求会被 isCurrentState 拦截。
export function render(container) {
    _container = container;
    _state = defaultState();
    renderPage();
}

export function destroy() {
    _container = null;
    _state = null;
}
