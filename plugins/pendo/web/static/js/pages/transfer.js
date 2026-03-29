import { api, apiDownload, apiUpload } from '../api.js';
import { showToast } from '../components/toast.js';
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
    { value: 'year', label: '今年' },
    { value: 'last_year', label: '去年' },
    { value: 'custom', label: '自定义' },
    { value: 'all', label: '全部' },
];
const CONFLICT_POLICIES = [
    { value: 'skip', label: '跳过同 ID', desc: '最安全，已有记录保持不动。' },
    { value: 'overwrite', label: '覆盖同 ID', desc: '用文件内容覆盖现有记录。' },
    { value: 'duplicate', label: '生成副本', desc: '保留原记录，并生成新 ID 的副本。' },
];
const INVALID_POLICIES = [
    { value: 'abort', label: '有错误则终止', desc: '发现非法记录就停止导入。' },
    { value: 'skip_invalid', label: '跳过非法记录', desc: '只导入通过校验的记录。' },
];

let _container = null;
let _state = null;

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
            file: null,
            inspect: null,
            selectedTypes: [],
            conflictPolicy: 'skip',
            invalidPolicy: 'abort',
            forceReimport: false,
            inspecting: false,
            executing: false,
            result: null,
            samplePage: 1,
            samplePageSize: 20,
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

function ensureStyles() {
    injectStyles(CSS_ID, `
        ${pageShellCss('transfer-shell', { padding: '24px 24px 36px', compactPadding: '18px 14px 28px', compactBreakpoint: BREAKPOINTS.NARROW })}
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
        .transfer-type-card { border: 1px solid rgba(226,232,240,0.96); border-radius: 18px; padding: 14px 12px; background: rgba(255,255,255,0.9); cursor: pointer; transition: transform .18s ease, border-color .18s ease, box-shadow .18s ease, background .18s ease; }
        .transfer-type-card:hover { transform: translateY(-1px); border-color: rgba(59,130,246,0.34); }
        .transfer-type-card.active { border-color: rgba(37,99,235,0.52); background: rgba(59,130,246,0.08); box-shadow: 0 10px 22px rgba(59,130,246,0.08); }
        .transfer-type-title { font-size: 14px; font-weight: 760; color: #0f172a; }
        .transfer-type-hint { margin-top: 6px; font-size: 12px; line-height: 1.55; color: var(--color-text-secondary); }
        .transfer-preset-row, .transfer-actions, .transfer-inline-checks { display: flex; flex-wrap: wrap; gap: 10px; }
        .transfer-pill { border: 1px solid rgba(203,213,225,0.96); background: #fff; color: #334155; border-radius: 999px; height: 38px; padding: 0 16px; font-size: 13px; font-weight: 730; cursor: pointer; }
        .transfer-pill.active { border-color: rgba(37,99,235,0.52); background: rgba(59,130,246,0.10); color: #1d4ed8; }
        .transfer-date-row, .transfer-summary-grid { display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 12px; }
        .transfer-summary-grid.three { grid-template-columns: repeat(3, minmax(0, 1fr)); }
        .transfer-field { display: flex; flex-direction: column; gap: 6px; }
        .transfer-field label { font-size: 12px; font-weight: 760; color: var(--color-text-secondary); }
        .transfer-field input { width: 100%; box-sizing: border-box; height: 42px; border-radius: 14px; border: 1px solid rgba(203,213,225,0.92); background: rgba(255,255,255,0.94); padding: 0 14px; font-size: 14px; color: var(--color-text); }
        .transfer-btn { border: none; border-radius: 16px; height: 44px; padding: 0 18px; font-size: 14px; font-weight: 780; cursor: pointer; transition: transform .18s ease, opacity .18s ease; }
        .transfer-btn:hover:not(:disabled) { transform: translateY(-1px); }
        .transfer-btn:disabled { opacity: 0.55; cursor: not-allowed; }
        .transfer-btn.primary { background: linear-gradient(135deg, #2563eb, #1d4ed8); color: #fff; box-shadow: 0 12px 24px rgba(37,99,235,0.18); }
        .transfer-btn.secondary { background: rgba(255,255,255,0.92); color: #0f172a; border: 1px solid rgba(203,213,225,0.96); }
        .transfer-btn.sm { height: 34px; padding: 0 14px; font-size: 12px; border-radius: 12px; }
        .transfer-summary-card, .transfer-list-row, .transfer-check, .transfer-error-row, .transfer-sample-row, .transfer-empty, .transfer-note, .transfer-upload { border-radius: 18px; }
        .transfer-summary-card, .transfer-list-row, .transfer-check, .transfer-sample-row { background: rgba(255,255,255,0.9); border: 1px solid rgba(226,232,240,0.88); }
        .transfer-summary-card { padding: 16px; }
        .transfer-status-banner {
            display: grid; grid-template-columns: auto minmax(0, 1fr) auto; gap: 14px; align-items: center;
            padding: 16px 18px; border-radius: 22px; border: 1px solid rgba(34,197,94,0.20);
            background: linear-gradient(135deg, rgba(240,253,244,0.98), rgba(236,253,245,0.94));
            box-shadow: 0 14px 30px rgba(34,197,94,0.08);
        }
        .transfer-status-banner.warning {
            border-color: rgba(245,158,11,0.24);
            background: linear-gradient(135deg, rgba(255,251,235,0.98), rgba(255,247,237,0.94));
            box-shadow: 0 14px 30px rgba(245,158,11,0.08);
        }
        .transfer-status-banner.duplicate-warn {
            border-color: rgba(245,158,11,0.24);
            background: linear-gradient(135deg, rgba(255,251,235,0.98), rgba(255,247,237,0.94));
            box-shadow: 0 14px 30px rgba(245,158,11,0.08);
        }
        .transfer-status-icon {
            width: 42px; height: 42px; border-radius: 14px; display: inline-flex; align-items: center; justify-content: center;
            background: rgba(34,197,94,0.14); color: #166534; font-size: 20px; font-weight: 900;
        }
        .transfer-status-banner.warning .transfer-status-icon,
        .transfer-status-banner.duplicate-warn .transfer-status-icon { background: rgba(245,158,11,0.16); color: #b45309; }
        .transfer-status-title { font-size: 15px; font-weight: 820; color: #0f172a; }
        .transfer-status-meta { margin-top: 4px; font-size: 12px; line-height: 1.7; color: #475569; }
        .transfer-status-pills { display: flex; flex-wrap: wrap; gap: 8px; justify-content: flex-end; }
        .transfer-status-pill {
            display: inline-flex; align-items: center; height: 32px; padding: 0 12px; border-radius: 999px;
            background: rgba(255,255,255,0.86); border: 1px solid rgba(203,213,225,0.82); color: #0f172a; font-size: 12px; font-weight: 760;
        }
        .transfer-summary-label { font-size: 12px; font-weight: 760; color: var(--color-text-secondary); }
        .transfer-summary-value { margin-top: 10px; font-size: 28px; font-weight: 840; letter-spacing: -0.04em; color: #0f172a; }
        .transfer-summary-meta { margin-top: 8px; font-size: 12px; color: var(--color-text-secondary); }
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
            transform: scale(0); transition: transform .16s ease;
        }
        .transfer-check input[type="radio"]:checked::before { transform: scale(1); }
        .transfer-check input[type="radio"]:focus-visible { box-shadow: 0 0 0 3px rgba(37,99,235,0.12); }
        .transfer-check input[type="checkbox"] { accent-color: #2563eb; margin: 0; }
        .transfer-error-row { padding: 12px 14px; border: 1px solid rgba(248,113,113,0.24); background: rgba(254,242,242,0.92); }
        .transfer-error-title, .transfer-sample-title { font-size: 13px; font-weight: 780; color: #0f172a; }
        .transfer-sample-row, .transfer-empty, .transfer-note { padding: 12px 14px; }
        .transfer-note { background: rgba(239,246,255,0.92); border: 1px solid rgba(191,219,254,0.72); font-size: 13px; line-height: 1.7; color: #1e3a8a; }
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
        ${mediaMax(BREAKPOINTS.DESKTOP, `.transfer-grid { grid-template-columns: 1fr; } .transfer-type-grid { grid-template-columns: repeat(3, minmax(0, 1fr)); }`)}
        ${mediaMax(BREAKPOINTS.NARROW, `.transfer-hero { padding: 22px 20px; } .transfer-hero h2 { font-size: 30px; } .transfer-tabs { width: 100%; } .transfer-tab { flex: 1 1 0; min-width: 0; } .transfer-type-grid { grid-template-columns: repeat(2, minmax(0, 1fr)); } .transfer-date-row, .transfer-summary-grid, .transfer-summary-grid.three { grid-template-columns: 1fr; } .transfer-actions { flex-direction: column; align-items: stretch; } .transfer-btn { width: 100%; }`)}
        ${mediaMax(BREAKPOINTS.PHONE, `.transfer-type-grid { grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 8px; } .transfer-type-card { padding: 12px 10px; border-radius: 16px; } .transfer-type-title { font-size: 13px; } .transfer-type-hint { margin-top: 4px; font-size: 11px; line-height: 1.4; }`)}
    `);
}

function renderPage() {
    if (!_container) return;
    ensureStyles();
    _container.innerHTML = `
        <div class="transfer-shell">
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
                <div class="transfer-tabs">
                    <button class="transfer-tab ${_state.tab === 'export' ? 'active' : ''}" data-transfer-tab="export">导出</button>
                    <button class="transfer-tab ${_state.tab === 'import' ? 'active' : ''}" data-transfer-tab="import">导入</button>
                    <button class="transfer-tab ${_state.tab === 'history' ? 'active' : ''}" data-transfer-tab="history">操作记录</button>
                </div>
                ${_state.tab === 'export' ? renderExportTab() : _state.tab === 'import' ? renderImportTab() : renderHistoryTab()}
            </div>
        </div>
    `;
    attachListeners();
}

function renderExportTab() {
    const state = _state.export;
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
                    <div class="transfer-preset-row">${PRESETS.map((item) => `<button type="button" class="transfer-pill ${state.preset === item.value ? 'active' : ''}" data-export-preset="${item.value}">${item.label}</button>`).join('')}</div>
                    ${state.preset === 'custom' ? `
                        <div class="transfer-date-row">
                            <div class="transfer-field"><label for="transfer-export-start">开始日期</label><input id="transfer-export-start" type="date" value="${escapeHtml(state.start)}"></div>
                            <div class="transfer-field"><label for="transfer-export-end">结束日期</label><input id="transfer-export-end" type="date" value="${escapeHtml(state.end)}"></div>
                        </div>` : ''}
                </div>
                <div class="transfer-section">
                    <div class="transfer-actions">
                        <button class="transfer-btn secondary" id="btn-transfer-preview" ${state.loading ? 'disabled' : ''}>${state.loading ? '预览中...' : '预览导出'}</button>
                        <button class="transfer-btn primary" id="btn-transfer-download" ${state.downloading ? 'disabled' : ''}>${state.downloading ? '生成中...' : '下载备份'}</button>
                    </div>
                </div>
                ${renderExportPreview(state.preview)}
            </div>
            <aside class="transfer-sidecard">
                <h3>格式说明</h3>
                <p>导出文件内部包含 <code>manifest.json</code> 和按类型分开的 <code>ndjson</code> 数据文件，后续可以按全部类别或部分类别重新导入。</p>
                <div class="transfer-note">时间范围按各模块主业务时间字段筛选：日程按开始时间，待办按截止时间或创建时间，记账按账目日期，笔记按创建时间，日记按日记日期。时间以你浏览器的时区为准。</div>
            </aside>
        </section>
    `;
}

function renderExportTypeCard(item) {
    const active = _state.export.selectedTypes.includes(item.value);
    return `<button type="button" class="transfer-type-card ${active ? 'active' : ''}" data-export-type="${item.value}"><div class="transfer-type-title">${item.label}</div><div class="transfer-type-hint">${item.hint}</div></button>`;
}

function renderExportPreview(preview) {
    if (!preview) {
        return `<div class="transfer-section"><div class="transfer-empty">先点"预览导出"。这里会显示命中的类别数量、总条数和本次 bundle 的时间范围，避免下载后才发现口径选错。</div></div>`;
    }
    const rangeText = preview.selection.start && preview.selection.end ? `${escapeHtml(preview.selection.start)} 至 ${escapeHtml(preview.selection.end)}` : '全部时间';
    const warningsHtml = (preview.warnings && preview.warnings.length)
        ? `<div class="transfer-note" style="margin-top: 10px;">导出校验警告（${preview.warnings.length} 条）：${preview.warnings.slice(0, 5).map((w) => escapeHtml(w)).join('；')}${preview.warnings.length > 5 ? '……' : ''}</div>`
        : '';
    return `
        <div class="transfer-section">
            <div class="transfer-section-label">预览结果</div>
            <div class="transfer-summary-grid three">
                <article class="transfer-summary-card"><div class="transfer-summary-label">总条数</div><div class="transfer-summary-value">${preview.total}</div><div class="transfer-summary-meta">本次 bundle 预计导出记录总数</div></article>
                <article class="transfer-summary-card"><div class="transfer-summary-label">类别数</div><div class="transfer-summary-value">${preview.selection.types.length}</div><div class="transfer-summary-meta">当前勾选参与导出的模块</div></article>
                <article class="transfer-summary-card"><div class="transfer-summary-label">时间范围</div><div class="transfer-summary-value" style="font-size:18px;line-height:1.3;">${rangeText}</div><div class="transfer-summary-meta">按这个口径打包数据</div></article>
            </div>
            <div class="transfer-list">${Object.entries(preview.counts).map(([type, count]) => `<div class="transfer-list-row"><div><div class="transfer-list-title">${typeLabel(type)}</div><div class="transfer-list-meta">命中的 ${typeLabel(type)} 记录</div></div><div class="transfer-list-value">${count}</div></div>`).join('')}</div>
            ${warningsHtml}
        </div>`;
}

function renderImportTab() {
    const state = _state.import;
    return `
        <section class="transfer-grid">
            <div class="transfer-card">
                <h3>导入 bundle</h3>
                <p>先上传 <code>.pendo.zip</code> 做预检，再按需要选中部分类别和冲突策略执行导入。预检不会改数据库，执行导入才会真正写入。最大支持 100 MB。</p>
                <div class="transfer-section">
                    <div class="transfer-upload" id="transfer-upload-zone">
                        <div class="transfer-upload-title">拖进来，或手动选择文件</div>
                        <div class="transfer-upload-hint">只接受 <code>.pendo.zip</code>。预检会先校验 manifest、checksum、schema 和逐行记录合法性。</div>
                        <div class="transfer-file-name">${state.file ? escapeHtml(state.file.name) : '尚未选择文件'}</div>
                        <input id="transfer-file-input" type="file" accept=".zip,.pendo.zip" style="display:none;">
                        <button type="button" class="transfer-btn secondary" id="btn-transfer-pick-file">选择文件</button>
                    </div>
                </div>
                ${state.inspect?.already_imported ? renderDuplicateBundleWarning() : ''}
                ${state.result ? renderImportStatusBanner(state.result) : ''}
                <div class="transfer-section"><div class="transfer-actions"><button class="transfer-btn secondary" id="btn-transfer-inspect" ${state.inspecting ? 'disabled' : ''}>${state.inspecting ? '预检中...' : '预检文件'}</button><button class="transfer-btn primary" id="btn-transfer-execute" ${(state.executing || !state.inspect) ? 'disabled' : ''}>${state.executing ? '导入中...' : '开始导入'}</button></div></div>
                ${renderImportInspect(state)}
            </div>
            <aside class="transfer-sidecard">
                <h3>导入策略</h3>
                <p>默认最安全的是"跳过同 ID"。如果你确定当前数据应被文件覆盖，再选"覆盖同 ID"。</p>
                <div class="transfer-section"><div class="transfer-section-label">冲突策略</div>${CONFLICT_POLICIES.map((item) => renderPolicy(item, 'transfer-conflict-policy', state.conflictPolicy)).join('')}</div>
                <div class="transfer-section"><div class="transfer-section-label">非法记录处理</div>${INVALID_POLICIES.map((item) => renderPolicy(item, 'transfer-invalid-policy', state.invalidPolicy)).join('')}</div>
                ${state.inspect?.already_imported ? `<div class="transfer-section"><label class="transfer-check"><input type="checkbox" id="transfer-force-reimport" ${state.forceReimport ? 'checked' : ''}><span>强制重新导入（此 bundle 已导入过）</span></label></div>` : ''}
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
                <div class="transfer-status-icon">!</div>
                <div>
                    <div class="transfer-status-title">此 bundle 已导入过</div>
                    <div class="transfer-status-meta">系统检测到这个 bundle 之前已成功导入。如果你确实需要重新导入，请在右侧勾选"强制重新导入"。</div>
                </div>
                <div></div>
            </div>
        </div>
    `;
}

function renderPolicy(item, name, current) {
    return `<label class="transfer-check"><input type="radio" name="${name}" value="${item.value}" ${current === item.value ? 'checked' : ''}><span>${item.label}</span></label><div class="transfer-list-meta" style="margin:-2px 0 4px 6px;">${item.desc}</div>`;
}

function renderExampleBlock(title, tip, code) {
    return `
        <div class="transfer-example-block">
            <div class="transfer-example-title">${title}</div>
            <div class="transfer-example-tip">${tip}</div>
            <pre class="transfer-code"><code>${escapeHtml(code)}</code></pre>
        </div>
    `;
}

function renderImportExamples() {
    const bundleTree = [
        'your-backup.pendo.zip',
        '  manifest.json',
        '  data/',
        '    tasks.ndjson',
        '    ledger.ndjson',
        '    diary.ndjson',
    ].join('\n');
    const minimalManifest = JSON.stringify({
        format: 'pendo-bundle',
        version: 1,
        bundle_id: 'my-first-import',
        exported_at: '2026-03-29T20:00:00+08:00',
        source: { app: 'external-tool', timezone: 'Asia/Shanghai' },
        selection: { types: ['task', 'ledger'], preset: 'all' },
        files: [
            { path: 'data/tasks.ndjson', type: 'task' },
            { path: 'data/ledger.ndjson', type: 'ledger' },
        ],
    }, null, 2);
    const minimalTask = '{"id": "t001", "title": "买牛奶", "status": "todo", "due_time": "2026-03-30T18:00:00+08:00"}\n{"id": "t002", "title": "交报告", "status": "todo", "priority": 2, "category": "工作"}';
    const minimalLedger = '{"id": "l001", "title": "午饭", "amount": 32.5, "direction": "expense", "ledger_category": "餐饮", "ledger_date": "2026-03-18"}';
    const fullManifest = JSON.stringify({
        format: 'pendo-bundle',
        version: 1,
        bundle_id: 'a1b2c3d4e5f6...',
        exported_at: '2026-03-29T20:00:00+08:00',
        source: { app: 'external-tool', timezone: 'Asia/Shanghai' },
        selection: { types: ['task', 'ledger', 'diary'], preset: 'all', start: null, end: null },
        files: [
            { path: 'data/tasks.ndjson', type: 'task', count: 2, sha256: '<sha256>' },
            { path: 'data/ledger.ndjson', type: 'ledger', count: 18, sha256: '<sha256>' },
            { path: 'data/diary.ndjson', type: 'diary', count: 6, sha256: '<sha256>' },
        ],
        attachments_mode: 'metadata_only',
    }, null, 2);
    const fullTask = JSON.stringify({
        _type: 'task',
        _schema: 1,
        id: 'task_20260329_review',
        title: '补完迁移文档',
        content: '整理导入导出字段说明',
        category: '工作',
        priority: 3,
        status: 'todo',
        due_time: '2026-03-30T18:00:00+08:00',
        created_at: '2026-03-29T10:30:00+08:00',
        updated_at: '2026-03-29T10:30:00+08:00',
    }, null, 2);
    return [
        `<div class="transfer-note">支持两种构造方式：<strong>精简模式</strong>适合从外部工具快速导入；<strong>完整模式</strong>是 Pendo 自身导出的标准格式，带 SHA256 校验和行数验证。</div>`,
        renderExampleBlock('1. 压缩包目录', 'zip 根目录必须包含 manifest.json，数据文件放在 data/ 下。', bundleTree),
        renderExampleBlock('2. manifest.json（精简模式）', '精简模式下，files 只需要声明 path 和 type。sha256、count 省略后系统会跳过校验并给出提示。bundle_id 可以用任意唯一字符串，例如稳定自定义字符串。', minimalManifest),
        renderExampleBlock('3. 精简 ndjson 示例', '每条记录只需要写业务字段，不需要 _type 和 _schema（系统会根据文件名自动推断）。没有 created_at 时系统会自动填充当前时间；没有 id 时会默认生成短随机 ID。', minimalTask + '\n\n// ledger.ndjson:\n' + minimalLedger),
        renderExampleBlock('4. manifest.json（完整模式）', 'Pendo 导出的标准格式。files 里带 count（精确行数）和 sha256（文件校验和），导入时会严格校验完整性。', fullManifest),
        renderExampleBlock('5. 完整 ndjson 示例', '完整模式下每条记录带 _type 和 _schema 元字段，便于跨版本兼容。', fullTask),
        `<div class="transfer-note">改造规则：1. 日期时间统一用 ISO 8601 并带时区偏移。2. 纯日期字段用 <code>YYYY-MM-DD</code>。3. 每个 <code>ndjson</code> 文件一行一条 JSON。4. ID 可以自定义，导入时会按这个 ID 做跳过 / 覆盖判断；不传 ID 时系统自动生成。5. 外部工具带来的未知字段会被自动存入 <code>context.import.extra</code>，不会丢失。</div>`,
    ].join('');
}

function renderImportInspect(state) {
    if (!state.inspect) {
        return `<div class="transfer-section"><div class="transfer-empty">上传 bundle 并点击"预检文件"后，这里会显示文件摘要、可导入类别、样例记录和校验错误。没有预检结果前，导入按钮会保持禁用。</div></div>`;
    }
    const inspect = state.inspect;
    return `
        <div class="transfer-section">
            <div class="transfer-section-label">文件摘要</div>
            <div class="transfer-summary-grid three">
                <article class="transfer-summary-card"><div class="transfer-summary-label">有效记录</div><div class="transfer-summary-value">${inspect.counts.valid}</div><div class="transfer-summary-meta">通过 schema 和字段校验的记录数</div></article>
                <article class="transfer-summary-card"><div class="transfer-summary-label">错误记录</div><div class="transfer-summary-value">${inspect.counts.errors}</div><div class="transfer-summary-meta">非法记录会在下面展开说明</div></article>
                <article class="transfer-summary-card"><div class="transfer-summary-label">文件数</div><div class="transfer-summary-value">${inspect.summary.files}</div><div class="transfer-summary-meta">bundle 中实际包含的数据文件数</div></article>
            </div>
        </div>
        <div class="transfer-section"><div class="transfer-section-label">要导入的类别</div><div class="transfer-inline-checks">${inspect.summary.types.map((type) => `<label class="transfer-check"><input type="checkbox" data-import-type="${type}" ${state.selectedTypes.includes(type) ? 'checked' : ''}><span>${typeLabel(type)}</span></label>`).join('')}</div></div>
        <div class="transfer-section"><div class="transfer-section-label">文件明细</div><div class="transfer-list">${inspect.files.map((file) => `<div class="transfer-list-row"><div><div class="transfer-list-title">${escapeHtml(file.path)}</div><div class="transfer-list-meta">${typeLabel(file.type)}，共 ${file.count} 行，合法 ${file.valid} 行</div></div><div class="transfer-list-value">${file.count}</div></div>`).join('')}</div></div>
        <div class="transfer-section">
            <div class="transfer-section-label">样例记录${inspect.counts.total_samples > 5 ? ` (共 ${inspect.counts.total_samples} 条)` : ''}</div>
            ${renderSamplesWithPager(state)}
        </div>
        <div class="transfer-section"><div class="transfer-section-label">校验错误</div><div class="transfer-errors">${inspect.errors.length ? inspect.errors.map((err) => `<div class="transfer-error-row"><div class="transfer-error-title">${escapeHtml(err.path)} · 第 ${err.line} 行</div><div class="transfer-error-meta">${escapeHtml(err.message)}</div></div>`).join('') : '<div class="transfer-empty">当前文件没有发现结构性错误，可以直接继续导入。</div>'}</div></div>
        ${state.result ? renderImportResult(state.result) : ''}
    `;
}

function renderSamplesWithPager(state) {
    const inspect = state.inspect;
    const totalSamples = inspect.counts.total_samples || 0;
    const displaySamples = state.paginatedSamples?.samples || inspect.samples || [];
    const samplesHtml = displaySamples.length
        ? displaySamples.map((sample) => `<div class="transfer-sample-row"><div class="transfer-sample-title">${typeLabel(sample.type)} · ${escapeHtml(sample.title || '无标题')}</div><div class="transfer-sample-meta">ID: ${escapeHtml(sample.id || 'N/A')}</div></div>`).join('')
        : '<div class="transfer-empty">没有可展示的样例记录。</div>';

    let pagerHtml = '';
    if (totalSamples > 5) {
        const currentPage = state.paginatedSamples?.page || 1;
        const pageSize = state.paginatedSamples?.page_size || state.samplePageSize;
        const totalPages = Math.ceil(totalSamples / pageSize);
        pagerHtml = `
            <div class="transfer-pager">
                <button class="transfer-btn sm secondary" id="btn-samples-prev" ${currentPage <= 1 || state.samplesLoading ? 'disabled' : ''}>上一页</button>
                <span class="transfer-pager-info">${state.samplesLoading ? '加载中...' : `第 ${currentPage} / ${totalPages} 页`}</span>
                <button class="transfer-btn sm secondary" id="btn-samples-next" ${currentPage >= totalPages || state.samplesLoading ? 'disabled' : ''}>下一页</button>
            </div>
        `;
    }

    return `<div class="transfer-samples">${samplesHtml}</div>${pagerHtml}`;
}

function renderImportStatusBanner(result) {
    const hasFailures = (result.results.failed || 0) > 0;
    const processed = (result.results.inserted || 0) + (result.results.updated || 0);
    const kindClass = hasFailures ? 'warning' : '';
    const icon = hasFailures ? '!' : '✓';
    const title = hasFailures ? '导入已完成，但有部分记录失败' : '导入已完成';
    const summary = hasFailures
        ? `本次成功处理 ${processed} 条，失败 ${result.results.failed} 条。下方已经按动作展开明细，可以直接定位失败原因。`
        : `本次成功处理 ${processed} 条记录，已写入 ${result.summary.types.map(typeLabel).join('、')}。下方保留了完整导入明细。`;
    return `
        <div class="transfer-section">
            <div class="transfer-status-banner ${kindClass}">
                <div class="transfer-status-icon">${icon}</div>
                <div>
                    <div class="transfer-status-title">${title}</div>
                    <div class="transfer-status-meta">${summary}</div>
                </div>
                <div class="transfer-status-pills">
                    <span class="transfer-status-pill">新增 ${result.results.inserted}</span>
                    <span class="transfer-status-pill">更新 ${result.results.updated}</span>
                    <span class="transfer-status-pill">跳过 ${result.results.skipped}</span>
                    <span class="transfer-status-pill">失败 ${result.results.failed}</span>
                </div>
            </div>
        </div>
    `;
}

function renderImportResult(result) {
    return `
        <div class="transfer-section">
            <div class="transfer-section-label">导入结果</div>
            <div class="transfer-summary-grid three">
                <article class="transfer-summary-card"><div class="transfer-summary-label">新增</div><div class="transfer-summary-value">${result.results.inserted}</div><div class="transfer-summary-meta">本次新写入的记录</div></article>
                <article class="transfer-summary-card"><div class="transfer-summary-label">更新</div><div class="transfer-summary-value">${result.results.updated}</div><div class="transfer-summary-meta">按同 ID 覆盖的记录</div></article>
                <article class="transfer-summary-card"><div class="transfer-summary-label">跳过 / 失败</div><div class="transfer-summary-value">${result.results.skipped + result.results.failed}</div><div class="transfer-summary-meta">包括跳过冲突和写入失败</div></article>
            </div>
            <div class="transfer-note">这次实际处理了 ${result.summary.types.map(typeLabel).join('、')}。下面按动作展开最近处理过的记录。</div>
            <div class="transfer-result-group">${renderResultRows('新增', 'inserted', result.details?.inserted || [])}</div>
            <div class="transfer-result-group">${renderResultRows('更新', 'updated', result.details?.updated || [])}</div>
            <div class="transfer-result-group">${renderResultRows('跳过', 'skipped', result.details?.skipped || [])}</div>
            <div class="transfer-result-group">${renderResultRows('失败', 'failed', result.details?.failed || [])}</div>
        </div>
    `;
}

function renderResultRows(title, kind, rows) {
    if (!rows.length) {
        return `<div class="transfer-empty"><div class="transfer-result-title">${title}</div><div class="transfer-result-meta">这次没有 ${title} 记录。</div></div>`;
    }
    return `
        <div class="transfer-result-title">${title}</div>
        ${rows.map((row) => `
            <div class="transfer-result-row">
                <div class="transfer-result-badge ${kind}">${title}</div>
                <div>
                    <div class="transfer-result-name">${typeLabel(row.type)} · ${escapeHtml(row.title || '无标题')}</div>
                    <div class="transfer-result-meta">ID: ${escapeHtml(row.id || 'N/A')}${row.reason ? ` · ${escapeHtml(row.reason)}` : ''}</div>
                </div>
            </div>
        `).join('')}
    `;
}

function renderHistoryTab() {
    const state = _state.history;
    if (state.loading) {
        return `<div class="transfer-card"><div class="transfer-empty">正在加载操作记录...</div></div>`;
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
    if (isImport && summary.inserted != null) {
        detail = `新增 ${summary.inserted}，更新 ${summary.updated || 0}，跳过 ${summary.skipped || 0}`;
    }
    const time = log.created_at ? new Date(log.created_at).toLocaleString('zh-CN', { month: '2-digit', day: '2-digit', hour: '2-digit', minute: '2-digit' }) : '';
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

function attachListeners() {
    if (!_container) return;
    _container.querySelectorAll('[data-transfer-tab]').forEach((button) => button.onclick = () => {
        _state.tab = button.dataset.transferTab;
        if (_state.tab === 'history' && !_state.history.loaded) loadHistory();
        renderPage();
    });
    if (_state.tab === 'export') return attachExportListeners();
    if (_state.tab === 'import') return attachImportListeners();
}

function attachExportListeners() {
    _container.querySelectorAll('[data-export-type]').forEach((button) => button.onclick = () => toggleExportType(button.dataset.exportType));
    _container.querySelectorAll('[data-export-preset]').forEach((button) => button.onclick = () => { _state.export.preset = button.dataset.exportPreset; if (_state.export.preset !== 'custom') { _state.export.start = ''; _state.export.end = ''; } renderPage(); });
    const startInput = _container.querySelector('#transfer-export-start');
    const endInput = _container.querySelector('#transfer-export-end');
    if (startInput) startInput.oninput = (event) => { _state.export.start = event.target.value; };
    if (endInput) endInput.oninput = (event) => { _state.export.end = event.target.value; };
    _container.querySelector('#btn-transfer-preview')?.addEventListener('click', previewExport);
    _container.querySelector('#btn-transfer-download')?.addEventListener('click', downloadExport);
}

function attachImportListeners() {
    const fileInput = _container.querySelector('#transfer-file-input');
    const uploadZone = _container.querySelector('#transfer-upload-zone');
    _container.querySelector('#btn-transfer-pick-file')?.addEventListener('click', () => fileInput?.click());
    if (fileInput) {
        fileInput.onchange = () => {
            _state.import.file = fileInput.files?.[0] || null;
            _state.import.inspect = null;
            _state.import.result = null;
            _state.import.paginatedSamples = null;
            _state.import.forceReimport = false;
            renderPage();
        };
    }
    if (uploadZone) {
        uploadZone.ondragover = (event) => { event.preventDefault(); uploadZone.classList.add('dragover'); };
        uploadZone.ondragleave = () => uploadZone.classList.remove('dragover');
        uploadZone.ondrop = (event) => {
            event.preventDefault();
            uploadZone.classList.remove('dragover');
            const file = event.dataTransfer?.files?.[0] || null;
            if (file) {
                _state.import.file = file;
                _state.import.inspect = null;
                _state.import.result = null;
                _state.import.paginatedSamples = null;
                _state.import.forceReimport = false;
                renderPage();
            }
        };
    }
    _container.querySelector('#btn-transfer-inspect')?.addEventListener('click', inspectImportFile);
    _container.querySelector('#btn-transfer-execute')?.addEventListener('click', executeImportFile);
    _container.querySelectorAll('input[name="transfer-conflict-policy"]').forEach((input) => input.onchange = (event) => { _state.import.conflictPolicy = event.target.value; });
    _container.querySelectorAll('input[name="transfer-invalid-policy"]').forEach((input) => input.onchange = (event) => { _state.import.invalidPolicy = event.target.value; });
    _container.querySelectorAll('[data-import-type]').forEach((input) => input.onchange = () => updateImportType(input.dataset.importType, input.checked));
    const forceCheckbox = _container.querySelector('#transfer-force-reimport');
    if (forceCheckbox) forceCheckbox.onchange = () => { _state.import.forceReimport = forceCheckbox.checked; };
    _container.querySelector('#btn-samples-prev')?.addEventListener('click', () => loadSamplePage(_state.import.samplePage - 1));
    _container.querySelector('#btn-samples-next')?.addEventListener('click', () => loadSamplePage(_state.import.samplePage + 1));
}

function updateImportType(type, checked) {
    if (checked) {
        if (!_state.import.selectedTypes.includes(type)) _state.import.selectedTypes.push(type);
        return;
    }
    _state.import.selectedTypes = _state.import.selectedTypes.filter((value) => value !== type);
}

function typeLabel(type) {
    return TYPE_OPTIONS.find((item) => item.value === type)?.label || type;
}

function toggleExportType(type) {
    const selected = _state.export.selectedTypes;
    if (selected.includes(type)) {
        if (selected.length === 1) return showToast('至少保留一个导出类别', 'warning');
        _state.export.selectedTypes = selected.filter((value) => value !== type);
    } else {
        _state.export.selectedTypes = [...selected, type];
    }
    renderPage();
}

function exportSelectionPayload() {
    return {
        types: _state.export.selectedTypes,
        preset: _state.export.preset,
        start: _state.export.preset === 'custom' ? _state.export.start || null : null,
        end: _state.export.preset === 'custom' ? _state.export.end || null : null,
        timezone: Intl.DateTimeFormat().resolvedOptions().timeZone || 'Asia/Shanghai',
    };
}

async function previewExport() {
    if (_state.export.preset === 'custom' && (!_state.export.start || !_state.export.end)) return showToast('自定义范围需要同时填写开始和结束日期', 'warning');
    _state.export.loading = true;
    renderPage();
    try {
        const res = await api.post('/transfer/export/preview', { selection: exportSelectionPayload() });
        _state.export.preview = res?.data || null;
    } catch (err) {
        showToast(`导出预览失败：${err.message}`, 'error');
    } finally {
        _state.export.loading = false;
        renderPage();
    }
}

async function downloadExport() {
    if (_state.export.preset === 'custom' && (!_state.export.start || !_state.export.end)) return showToast('自定义范围需要同时填写开始和结束日期', 'warning');
    _state.export.downloading = true;
    renderPage();
    try {
        const { blob, filename } = await apiDownload('/transfer/export/download', { selection: exportSelectionPayload() });
        const url = URL.createObjectURL(blob);
        const anchor = document.createElement('a');
        anchor.href = url;
        anchor.download = filename;
        document.body.appendChild(anchor);
        anchor.click();
        anchor.remove();
        URL.revokeObjectURL(url);
        showToast('备份已开始下载', 'success');
    } catch (err) {
        showToast(`导出失败：${err.message}`, 'error');
    } finally {
        _state.export.downloading = false;
        renderPage();
    }
}

async function inspectImportFile() {
    if (!_state.import.file) return showToast('请先选择一个 .pendo.zip 文件', 'warning');
    _state.import.inspecting = true;
    _state.import.result = null;
    _state.import.paginatedSamples = null;
    renderPage();
    try {
        const res = await apiUpload('/transfer/import/inspect', _state.import.file, {
            'Content-Type': 'application/zip',
            'X-Transfer-Filename': _state.import.file.name,
        });
        _state.import.inspect = res?.data || null;
        _state.import.selectedTypes = [...(_state.import.inspect?.summary?.types || [])];
        _state.import.forceReimport = false;
        showToast('文件预检完成', 'success');
    } catch (err) {
        showToast(`预检失败：${err.message}`, 'error');
    } finally {
        _state.import.inspecting = false;
        renderPage();
    }
}

async function executeImportFile() {
    if (!_state.import.file || !_state.import.inspect) return showToast('请先完成文件预检', 'warning');
    if (!_state.import.selectedTypes.length) return showToast('至少选择一个要导入的类别', 'warning');
    _state.import.executing = true;
    renderPage();
    try {
        const res = await apiUpload('/transfer/import/execute', _state.import.file, {
            'Content-Type': 'application/zip',
            'X-Transfer-Filename': _state.import.file.name,
            'X-Transfer-Options': JSON.stringify({
                types: _state.import.selectedTypes,
                conflict_policy: _state.import.conflictPolicy,
                invalid_policy: _state.import.invalidPolicy,
                force: _state.import.forceReimport,
            }),
        });
        _state.import.result = res?.data || null;
        showToast('导入已执行', 'success');
        // 刷新操作记录缓存
        _state.history.loaded = false;
    } catch (err) {
        showToast(`导入失败：${err.message}`, 'error');
    } finally {
        _state.import.executing = false;
        renderPage();
    }
}

async function loadSamplePage(page) {
    if (!_state.import.file || _state.import.samplesLoading) return;
    _state.import.samplesLoading = true;
    _state.import.samplePage = page;
    renderPage();
    try {
        const res = await apiUpload('/transfer/import/samples', _state.import.file, {
            'Content-Type': 'application/zip',
            'X-Transfer-Filename': _state.import.file.name,
            'X-Transfer-Page': String(page),
            'X-Transfer-Page-Size': String(_state.import.samplePageSize),
        });
        _state.import.paginatedSamples = res?.data || null;
    } catch (err) {
        showToast(`加载样例失败：${err.message}`, 'error');
    } finally {
        _state.import.samplesLoading = false;
        renderPage();
    }
}

async function loadHistory() {
    _state.history.loading = true;
    renderPage();
    try {
        const res = await api.get('/transfer/logs', { limit: 50 });
        _state.history.logs = res?.data?.logs || [];
        _state.history.loaded = true;
    } catch (err) {
        showToast(`加载操作记录失败：${err.message}`, 'error');
    } finally {
        _state.history.loading = false;
        renderPage();
    }
}

export function render(container) {
    _container = container;
    _state = defaultState();
    renderPage();
}

export function destroy() {
    _container = null;
    _state = null;
}

export function onRouteEnter() {}
