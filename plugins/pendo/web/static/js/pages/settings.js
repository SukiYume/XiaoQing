import { api, clearToken } from '../api.js';
import { showToast } from '../components/toast.js';
import { BREAKPOINTS, escapeHtml, injectStyles, mediaMax, pageShellCss } from '../utils/ui.js';
import { navigate } from '../router.js';

const CSS_ID = 'pendo-settings-redesign-styles';

let _container = null;
let _settings = null;
let _saving = false;

function normalizeToggleSettings(settingsJson) {
    const raw = settingsJson && typeof settingsJson === 'object' ? settingsJson : {};
    return {
        ...raw,
        reminder_enabled: raw.reminder_enabled !== undefined ? Boolean(raw.reminder_enabled) : true,
        daily_briefing_enabled: raw.daily_briefing_enabled !== undefined ? Boolean(raw.daily_briefing_enabled) : true,
        privacy_mode: raw.privacy_mode !== undefined ? Boolean(raw.privacy_mode) : true,
    };
}

function ensureStyles() {
    injectStyles(CSS_ID, `
        ${pageShellCss('settings-shell', { compactPadding: '20px 16px 30px', compactBreakpoint: BREAKPOINTS.MOBILE })}
        .settings-stack { display: flex; flex-direction: column; gap: 18px; }
        .settings-hero {
            display: grid; grid-template-columns: minmax(0, 1fr) auto; gap: 18px; align-items: center;
            padding: 24px 26px; border-radius: 28px;
            background:
                radial-gradient(circle at top right, rgba(14,165,233,0.16), transparent 32%),
                radial-gradient(circle at bottom left, rgba(99,102,241,0.10), transparent 22%),
                linear-gradient(145deg, rgba(255,255,255,0.98), rgba(240,249,255,0.95));
            border: 1px solid rgba(14,165,233,0.12); box-shadow: 0 18px 40px rgba(14,165,233,0.06);
        }
        .settings-hero h2 { margin: 0; font-size: 30px; font-weight: 820; letter-spacing: -0.03em; color: #0284c7; }
        .settings-hero p { margin: 8px 0 0; font-size: 14px; line-height: 1.75; color: var(--color-text-secondary); }
        .settings-chip-row { display: flex; flex-wrap: wrap; gap: 8px; }
        .settings-hero-tags { margin-top: 14px; }
        .settings-chip {
            display: inline-flex; align-items: center; gap: 6px; height: 34px; padding: 0 14px; border-radius: 999px;
            background: rgba(14,165,233,0.08); color: #0369a1; font-size: 12px; font-weight: 700;
        }
        .settings-summary-grid { display: grid; grid-template-columns: repeat(4, minmax(0, 1fr)); gap: 14px; }
        .settings-summary-card, .settings-panel {
            background: linear-gradient(180deg, rgba(255,255,255,0.98), rgba(248,250,252,0.95));
            border: 1px solid rgba(226,232,240,0.88); border-radius: 24px; box-shadow: 0 16px 34px rgba(15,23,42,0.04);
        }
        .settings-summary-card { padding: 18px; min-width: 0; }
        .settings-summary-label { font-size: 12px; font-weight: 700; color: var(--color-text-secondary); }
        .settings-summary-value {
            margin-top: 10px; font-size: clamp(18px, 1.55vw, 24px); font-weight: 820; color: #0f172a; letter-spacing: -0.03em;
            line-height: 1.18; overflow-wrap: anywhere; word-break: break-word;
        }
        .settings-summary-meta { margin-top: 8px; font-size: 12px; color: var(--color-text-secondary); overflow-wrap: anywhere; word-break: break-word; }
        .settings-layout { display: grid; grid-template-columns: minmax(0, 1.06fr) minmax(280px, 0.94fr); gap: 16px; }
        .settings-main-stack { display: flex; flex-direction: column; gap: 16px; }
        .settings-side-stack { display: flex; flex-direction: column; gap: 16px; }
        .settings-panel { padding: 18px 20px 20px; }
        .settings-panel h3 { margin: 0; font-size: 18px; font-weight: 780; color: var(--color-text); letter-spacing: -0.02em; }
        .settings-panel p { margin: 6px 0 0; font-size: 13px; color: var(--color-text-secondary); line-height: 1.7; }
        .settings-form-grid { display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 14px; margin-top: 16px; }
        .settings-field { display: flex; flex-direction: column; gap: 6px; }
        .settings-field label { font-size: 12px; font-weight: 800; color: var(--color-text-secondary); text-transform: uppercase; letter-spacing: 0.04em; }
        .settings-field input {
            width: 100%; height: 42px; border-radius: 14px; border: 1px solid rgba(203,213,225,0.92); background: rgba(255,255,255,0.92);
            padding: 0 14px; font-size: 14px; color: var(--color-text); box-sizing: border-box;
        }
        .settings-field input:focus { outline: none; border-color: #38bdf8; box-shadow: 0 0 0 3px rgba(56,189,248,0.12); }
        .settings-field.full { grid-column: 1 / -1; }
        .settings-toggle-list { display: flex; flex-direction: column; gap: 12px; margin-top: 16px; }
        .settings-toggle-row {
            display: grid; grid-template-columns: minmax(0, 1fr) auto; gap: 12px; align-items: center;
            padding: 14px; border-radius: 18px; border: 1px solid rgba(226,232,240,0.82); background: rgba(255,255,255,0.88);
        }
        .settings-toggle-title { font-size: 14px; font-weight: 760; color: var(--color-text); }
        .settings-toggle-desc { margin-top: 4px; font-size: 12px; line-height: 1.6; color: var(--color-text-secondary); }
        .settings-switch { position: relative; width: 46px; height: 28px; }
        .settings-switch input { opacity: 0; width: 0; height: 0; }
        .settings-slider {
            position: absolute; inset: 0; border-radius: 999px; background: rgba(203,213,225,0.9); cursor: pointer; transition: .2s ease;
        }
        .settings-slider::before {
            content: ''; position: absolute; width: 22px; height: 22px; left: 3px; top: 3px; border-radius: 999px;
            background: #fff; box-shadow: 0 1px 3px rgba(15,23,42,0.18); transition: .2s ease;
        }
        .settings-switch input:checked + .settings-slider { background: #0ea5e9; }
        .settings-switch input:checked + .settings-slider::before { transform: translateX(18px); }
        .settings-callout-card {
            display: grid; grid-template-columns: minmax(0, 1fr) auto; gap: 14px; align-items: center;
            margin-top: 16px; padding: 16px; border-radius: 20px;
            border: 1px solid rgba(191,219,254,0.8); background: rgba(239,246,255,0.84);
        }
        .settings-callout-copy { min-width: 0; }
        .settings-callout-title { font-size: 15px; font-weight: 780; color: #0f172a; }
        .settings-callout-desc { margin-top: 6px; font-size: 12px; line-height: 1.7; color: var(--color-text-secondary); }
        .settings-info-block {
            font-size: 13px; line-height: 1.7; color: var(--color-text-secondary);
        }
        .settings-info-block code {
            font-family: monospace; background: rgba(14,165,233,0.10); color: #0369a1; border-radius: 6px; padding: 2px 6px;
        }
        .settings-actions { display: flex; justify-content: space-between; gap: 12px; align-items: center; margin-top: 18px; }
        .settings-save-panel {
            display: grid;
            grid-template-columns: minmax(0, 1fr) auto;
            align-items: center;
            gap: 16px;
            padding: 16px 18px;
        }
        .settings-save-panel h3 { font-size: 16px; }
        .settings-save-panel p { margin-top: 4px; }
        .settings-save-actions {
            justify-content: flex-end;
            margin-top: 0;
            min-width: min(360px, 100%);
        }
        .settings-save-actions .settings-status { text-align: right; }
        .settings-save-actions .btn {
            flex: 0 0 auto;
            min-width: 112px;
        }
        .settings-status { font-size: 12px; color: var(--color-text-secondary); }
        .settings-danger { display: flex; flex-direction: column; gap: 12px; margin-top: 16px; }
        .settings-danger .btn { align-self: flex-start; }
        .settings-transfer-card {
            grid-template-columns: minmax(0, 1fr) auto;
        }
        .settings-transfer-btn {
            background: #fff;
            border-color: rgba(191,219,254,0.92);
            box-shadow: 0 8px 18px rgba(148,163,184,0.12);
            transition: background-color .18s ease, border-color .18s ease, box-shadow .18s ease, transform .18s ease;
        }
        .settings-transfer-btn:hover {
            background: linear-gradient(180deg, rgba(255,255,255,0.98), rgba(248,250,255,0.96));
            border-color: rgba(96,165,250,0.78);
            box-shadow: 0 12px 24px rgba(59,130,246,0.12);
            transform: translateY(-1px);
        }
        ${mediaMax(BREAKPOINTS.XL, `
            .settings-summary-grid { grid-template-columns: repeat(2, minmax(0, 1fr)); }
            .settings-layout { grid-template-columns: 1fr; }
        `)}
        ${mediaMax(BREAKPOINTS.MOBILE, `
            .settings-hero { grid-template-columns: 1fr; padding: 22px 20px; }
            .settings-summary-grid { grid-template-columns: 1fr; }
            .settings-form-grid { grid-template-columns: 1fr; }
            .settings-actions { flex-direction: column; align-items: stretch; }
            .settings-save-panel { grid-template-columns: 1fr; }
            .settings-save-actions { min-width: 0; }
            .settings-save-actions .settings-status { text-align: left; }
            .settings-callout-card { grid-template-columns: 1fr; }
        `)}
    `);
}

function currentSettings() {
    return _settings || {
        timezone: 'Asia/Shanghai',
        quiet_hours_start: '23:00',
        quiet_hours_end: '07:00',
        daily_report_time: '08:00',
        diary_remind_time: '21:30',
        default_category: '未分类',
        settings_json: normalizeToggleSettings({}),
    };
}

function renderPage() {
    if (!_container) return;
    ensureStyles();
    if (!_settings && _saving === false) {
        _container.innerHTML = '<div class="settings-shell"><div class="settings-panel"><h3>正在加载设置...</h3></div></div>';
        return;
    }
    const settings = currentSettings();
    const toggles = normalizeToggleSettings(settings.settings_json);
    _container.innerHTML = `
        <div class="settings-shell">
            <div class="settings-stack">
                <section class="settings-hero">
                    <div>
                        <h2>⚙️ 设置</h2>
                        <p>统一管理通知、时区、静默时段和默认设置。</p>
                        <div class="settings-chip-row settings-hero-tags">
                            <span class="settings-chip">${escapeHtml(settings.timezone)}</span>
                            <span class="settings-chip">${escapeHtml(settings.quiet_hours_start)} - ${escapeHtml(settings.quiet_hours_end)}</span>
                            <span class="settings-chip">${toggles.privacy_mode ? '隐私模式开启' : '隐私模式关闭'}</span>
                        </div>
                    </div>
                    <div class="settings-chip-row">
                        <span class="settings-chip">${toggles.reminder_enabled ? '提醒开启' : '提醒关闭'}</span>
                        <span class="settings-chip">${toggles.daily_briefing_enabled ? '简报开启' : '简报关闭'}</span>
                    </div>
                </section>

                <section class="settings-summary-grid">
                    <article class="settings-summary-card"><div class="settings-summary-label">静默时段</div><div class="settings-summary-value">${escapeHtml(settings.quiet_hours_start)} - ${escapeHtml(settings.quiet_hours_end)}</div><div class="settings-summary-meta">这段时间内提醒会更克制</div></article>
                    <article class="settings-summary-card"><div class="settings-summary-label">日报时间</div><div class="settings-summary-value">${escapeHtml(settings.daily_report_time)}</div><div class="settings-summary-meta">每天简报准备在这个时间触发</div></article>
                    <article class="settings-summary-card"><div class="settings-summary-label">日记提醒</div><div class="settings-summary-value">${escapeHtml(settings.diary_remind_time)}</div><div class="settings-summary-meta">更接近你写日记的真实时刻</div></article>
                    <article class="settings-summary-card"><div class="settings-summary-label">默认分类</div><div class="settings-summary-value">${escapeHtml(settings.default_category)}</div><div class="settings-summary-meta">新建内容默认归到这里</div></article>
                </section>

                <section class="settings-layout">
                    <div class="settings-main-stack">
                        <div class="settings-panel">
                            <h3>时间与默认项</h3>
                            <p>先把时区和关键时间窗口定准，后续提醒、日报和 diary 逻辑才会按你的日常节奏工作。</p>
                            <div class="settings-form-grid">
                                <div class="settings-field full">
                                    <label for="setting-timezone">时区</label>
                                    <input id="setting-timezone" type="text" value="${escapeHtml(settings.timezone)}" placeholder="Asia/Shanghai">
                                </div>
                                <div class="settings-field">
                                    <label for="setting-quiet-start">静默开始</label>
                                    <input id="setting-quiet-start" type="time" value="${escapeHtml(settings.quiet_hours_start)}">
                                </div>
                                <div class="settings-field">
                                    <label for="setting-quiet-end">静默结束</label>
                                    <input id="setting-quiet-end" type="time" value="${escapeHtml(settings.quiet_hours_end)}">
                                </div>
                                <div class="settings-field">
                                    <label for="setting-daily-report-time">日报时间</label>
                                    <input id="setting-daily-report-time" type="time" value="${escapeHtml(settings.daily_report_time)}">
                                </div>
                                <div class="settings-field">
                                    <label for="setting-diary-remind-time">日记提醒</label>
                                    <input id="setting-diary-remind-time" type="time" value="${escapeHtml(settings.diary_remind_time)}">
                                </div>
                                <div class="settings-field full">
                                    <label for="setting-default-category">默认分类</label>
                                    <input id="setting-default-category" type="text" value="${escapeHtml(settings.default_category)}" placeholder="未分类">
                                </div>
                            </div>
                        </div>

                        <section class="settings-panel">
                            <h3>数据迁移</h3>
                            <p>这里处理导出备份、导入 bundle 和跨设备迁移，不和日常偏好设置混在一起。</p>
                            <div class="settings-callout-card settings-transfer-card">
                                <div class="settings-callout-copy">
                                    <div class="settings-callout-title">打开数据迁移页</div>
                                    <div class="settings-callout-desc">导出正式 bundle 备份，或从 bundle 预检后按类别导入历史数据。</div>
                                </div>
                                <button class="btn btn-secondary settings-transfer-btn" id="btn-open-transfer">打开页面</button>
                            </div>
                        </section>
                    </div>

                    <div class="settings-side-stack">
                        <section class="settings-panel">
                            <h3>功能开关</h3>
                            <p>常用开关会直接保存到你的个人设置。</p>
                            <div class="settings-toggle-list">
                                ${toggleRow('toggle-reminder-enabled', '提醒通知', '控制事件提醒与待办提醒是否整体启用。', toggles.reminder_enabled)}
                                ${toggleRow('toggle-daily-report-enabled', '每日简报', '决定是否按配置时间生成每日简报。', toggles.daily_briefing_enabled)}
                                ${toggleRow('toggle-privacy-mode', '隐私模式', '减少不必要的暴露信息，更适合个人环境。', toggles.privacy_mode)}
                            </div>
                        </section>

                        <section class="settings-panel">
                            <h3>登录与令牌</h3>
                            <p>需要重新登录时，可以从这里退出当前状态。</p>
                            <div class="settings-callout-card">
                                <div class="settings-callout-copy settings-info-block">
                                    <div class="settings-callout-title">当前登录状态</div>
                                    <div class="settings-callout-desc">当前 Web 端已登录。如需重新生成令牌，请回到聊天里执行 <code>/pendo web token</code>。</div>
                                </div>
                            </div>
                            <div class="settings-danger">
                                <button class="btn btn-secondary" id="btn-logout">退出登录</button>
                            </div>
                        </section>
                    </div>
                </section>

                <section class="settings-panel settings-save-panel">
                    <div>
                        <h3>保存更改</h3>
                        <p>修改后统一写入当前设置。</p>
                    </div>
                    <div class="settings-actions settings-save-actions">
                        <div class="settings-status">${_saving ? '正在保存设置...' : '保存后会立即返回最新配置。'}</div>
                        <button class="btn btn-primary" id="btn-save-settings" ${_saving ? 'disabled' : ''}>${_saving ? '保存中...' : '保存设置'}</button>
                    </div>
                </section>
            </div>
        </div>
    `;
    attachListeners();
}

function toggleRow(id, title, desc, checked) {
    return `
        <div class="settings-toggle-row">
            <div>
                <div class="settings-toggle-title">${title}</div>
                <div class="settings-toggle-desc">${desc}</div>
            </div>
            <label class="settings-switch">
                <input type="checkbox" id="${id}" aria-label="${escapeHtml(title)}" ${checked ? 'checked' : ''}>
                <span class="settings-slider"></span>
            </label>
        </div>
    `;
}

function collectFormData() {
    const get = (id) => _container.querySelector(`#${id}`)?.value.trim() || '';
    const checked = (id) => Boolean(_container.querySelector(`#${id}`)?.checked);
    return {
        timezone: get('setting-timezone') || null,
        quiet_hours_start: get('setting-quiet-start') || null,
        quiet_hours_end: get('setting-quiet-end') || null,
        daily_report_time: get('setting-daily-report-time') || null,
        diary_remind_time: get('setting-diary-remind-time') || null,
        default_category: get('setting-default-category') || null,
        settings_json: {
            reminder_enabled: checked('toggle-reminder-enabled'),
            daily_briefing_enabled: checked('toggle-daily-report-enabled'),
            privacy_mode: checked('toggle-privacy-mode'),
        },
    };
}

async function handleSave() {
    const payload = collectFormData();
    _saving = true;
    renderPage();
    try {
        const res = await api.put('/settings', payload);
        _settings = res?.data || payload;
        showToast('设置已更新', 'success');
    } catch (err) {
        showToast(`保存失败：${err.message}`, 'error');
    } finally {
        _saving = false;
        renderPage();
    }
}

function handleLogout() {
    clearToken();
    window.location.reload();
}

function handleOpenTransfer() {
    navigate('transfer');
}

function attachListeners() {
    if (!_container) return;
    const saveBtn = _container.querySelector('#btn-save-settings');
    if (saveBtn) saveBtn.onclick = handleSave;
    const logoutBtn = _container.querySelector('#btn-logout');
    if (logoutBtn) logoutBtn.onclick = handleLogout;
    const transferBtn = _container.querySelector('#btn-open-transfer');
    if (transferBtn) transferBtn.onclick = handleOpenTransfer;
}

async function loadAndRender() {
    if (!_container) return;
    try {
        const res = await api.get('/settings');
        _settings = res?.data || currentSettings();
    } catch (err) {
        _settings = currentSettings();
        showToast(`加载设置失败：${err.message}`, 'error');
    }
    renderPage();
}

export function render(container) {
    _container = container;
    _settings = null;
    _saving = false;
    renderPage();
    loadAndRender();
}

export function destroy() {
    _container = null;
    _settings = null;
}

export function onRouteEnter(_params) {}
