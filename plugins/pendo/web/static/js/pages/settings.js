import { api, clearToken } from '../api.js';
import { showToast } from '../components/toast.js';

// ── constants ─────────────────────────────────────────────────────────────────

const CSS_ID = 'pendo-settings-styles';

// ── module state ──────────────────────────────────────────────────────────────

let _container = null;

// ── CSS ───────────────────────────────────────────────────────────────────────

function ensureStyles() {
    if (document.getElementById(CSS_ID)) return;
    const style = document.createElement('style');
    style.id = CSS_ID;
    style.textContent = `
        .settings-page {
            padding: 24px;
            max-width: 680px;
            margin: 0 auto;
        }

        .settings-page-header {
            margin-bottom: 24px;
        }

        .settings-section {
            margin-bottom: 20px;
        }

        .settings-section .card {
            padding: 20px 24px;
        }

        .settings-section-title {
            font-size: 13px;
            font-weight: 600;
            color: var(--color-dashboard, #6366F1);
            text-transform: uppercase;
            letter-spacing: 0.06em;
            margin-bottom: 16px;
        }

        .form-group {
            margin-bottom: 16px;
        }

        .form-group:last-child {
            margin-bottom: 0;
        }

        .form-label {
            display: block;
            font-size: 13px;
            font-weight: 500;
            color: var(--color-text-secondary);
            margin-bottom: 6px;
        }

        .form-input {
            width: 100%;
            padding: 8px 12px;
            font-size: 14px;
            color: var(--color-text);
            background: var(--color-bg);
            border: 1px solid var(--color-border);
            border-radius: var(--radius-sm);
            outline: none;
            box-sizing: border-box;
            transition: border-color 0.15s;
        }

        .form-input:focus {
            border-color: var(--color-dashboard, #6366F1);
        }

        .settings-time-row {
            display: flex;
            gap: 12px;
            align-items: center;
        }

        .settings-time-row .form-input {
            flex: 1;
        }

        .settings-time-sep {
            font-size: 13px;
            color: var(--color-text-tertiary);
            white-space: nowrap;
        }

        /* Toggle switch */
        .toggle-row {
            display: flex;
            align-items: center;
            justify-content: space-between;
            padding: 10px 0;
            border-bottom: 1px solid var(--color-border);
        }

        .toggle-row:last-child {
            border-bottom: none;
        }

        .toggle-label-group {
            display: flex;
            flex-direction: column;
            gap: 2px;
        }

        .toggle-label-text {
            font-size: 14px;
            font-weight: 500;
            color: var(--color-text);
        }

        .toggle-switch {
            position: relative;
            display: inline-block;
            width: 42px;
            height: 24px;
            flex-shrink: 0;
        }

        .toggle-switch input {
            opacity: 0;
            width: 0;
            height: 0;
            position: absolute;
        }

        .toggle-slider {
            position: absolute;
            cursor: pointer;
            inset: 0;
            background: var(--color-border);
            border-radius: 24px;
            transition: background 0.2s;
        }

        .toggle-slider::before {
            content: '';
            position: absolute;
            width: 18px;
            height: 18px;
            left: 3px;
            top: 3px;
            background: #fff;
            border-radius: 50%;
            transition: transform 0.2s;
            box-shadow: 0 1px 3px rgba(0,0,0,0.2);
        }

        .toggle-switch input:checked + .toggle-slider {
            background: var(--color-dashboard, #6366F1);
        }

        .toggle-switch input:checked + .toggle-slider::before {
            transform: translateX(18px);
        }

        /* Token section */
        .settings-token-info {
            background: var(--color-bg);
            border: 1px solid var(--color-border);
            border-radius: var(--radius-sm);
            padding: 12px 14px;
            font-size: 13px;
            color: var(--color-text-secondary);
            margin-bottom: 14px;
            line-height: 1.6;
        }

        .settings-token-info code {
            font-family: monospace;
            font-size: 12px;
            background: var(--color-border);
            color: var(--color-dashboard, #6366F1);
            padding: 2px 6px;
            border-radius: 3px;
        }

        .settings-save-row {
            margin-top: 24px;
            display: flex;
            justify-content: flex-end;
        }
    `;
    document.head.appendChild(style);
}

// ── render ────────────────────────────────────────────────────────────────────

function renderPage(settings) {
    if (!_container) return;

    ensureStyles();

    const s = settings || {};
    const sj = s.settings_json || {};

    const val = (v) => (v !== null && v !== undefined) ? String(v) : '';

    _container.innerHTML = `
        <div class="settings-page">

            <div class="settings-page-header">
                <h2 style="font-size:20px;font-weight:700;color:var(--color-dashboard,#6366F1);">⚙️ 设置</h2>
                <p style="font-size:13px;color:var(--color-text-secondary);margin-top:2px;">管理你的偏好与通知</p>
            </div>

            <!-- 时区 -->
            <div class="settings-section">
                <div class="card">
                    <div class="settings-section-title">时区</div>
                    <div class="form-group">
                        <label class="form-label" for="setting-timezone">时区</label>
                        <input
                            type="text"
                            id="setting-timezone"
                            class="form-input"
                            placeholder="Asia/Shanghai"
                            value="${val(s.timezone).replace(/"/g, '&quot;')}"
                        />
                    </div>
                </div>
            </div>

            <!-- 时间设置 -->
            <div class="settings-section">
                <div class="card">
                    <div class="settings-section-title">时间设置</div>

                    <div class="form-group">
                        <label class="form-label">静默时段</label>
                        <div class="settings-time-row">
                            <input
                                type="time"
                                id="setting-quiet-start"
                                class="form-input"
                                value="${val(s.quiet_hours_start)}"
                            />
                            <span class="settings-time-sep">至</span>
                            <input
                                type="time"
                                id="setting-quiet-end"
                                class="form-input"
                                value="${val(s.quiet_hours_end)}"
                            />
                        </div>
                    </div>

                    <div class="form-group">
                        <label class="form-label" for="setting-daily-report-time">每日简报时间</label>
                        <input
                            type="time"
                            id="setting-daily-report-time"
                            class="form-input"
                            value="${val(s.daily_report_time)}"
                        />
                    </div>

                    <div class="form-group">
                        <label class="form-label" for="setting-diary-remind-time">日记提醒时间</label>
                        <input
                            type="time"
                            id="setting-diary-remind-time"
                            class="form-input"
                            value="${val(s.diary_remind_time)}"
                        />
                    </div>
                </div>
            </div>

            <!-- 默认分类 -->
            <div class="settings-section">
                <div class="card">
                    <div class="settings-section-title">默认分类</div>
                    <div class="form-group">
                        <label class="form-label" for="setting-default-category">默认分类</label>
                        <input
                            type="text"
                            id="setting-default-category"
                            class="form-input"
                            placeholder="未分类"
                            value="${val(s.default_category).replace(/"/g, '&quot;')}"
                        />
                    </div>
                </div>
            </div>

            <!-- 开关项 -->
            <div class="settings-section">
                <div class="card">
                    <div class="settings-section-title">功能开关</div>

                    <div class="toggle-row">
                        <div class="toggle-label-group">
                            <span class="toggle-label-text">提醒通知</span>
                        </div>
                        <label class="toggle-switch">
                            <input type="checkbox" id="toggle-reminder-enabled" ${sj.reminder_enabled ? 'checked' : ''} />
                            <span class="toggle-slider"></span>
                        </label>
                    </div>

                    <div class="toggle-row">
                        <div class="toggle-label-group">
                            <span class="toggle-label-text">每日简报</span>
                        </div>
                        <label class="toggle-switch">
                            <input type="checkbox" id="toggle-daily-report-enabled" ${sj.daily_report_enabled ? 'checked' : ''} />
                            <span class="toggle-slider"></span>
                        </label>
                    </div>

                    <div class="toggle-row">
                        <div class="toggle-label-group">
                            <span class="toggle-label-text">隐私模式</span>
                        </div>
                        <label class="toggle-switch">
                            <input type="checkbox" id="toggle-privacy-mode" ${sj.privacy_mode ? 'checked' : ''} />
                            <span class="toggle-slider"></span>
                        </label>
                    </div>
                </div>
            </div>

            <!-- Token 管理 -->
            <div class="settings-section">
                <div class="card">
                    <div class="settings-section-title">Token 管理</div>
                    <div class="settings-token-info">
                        当前已登录。如需重新生成登录令牌，请在聊天中执行：<br/>
                        <code>/pendo web token</code>
                    </div>
                    <button class="btn btn-secondary" id="btn-logout">退出登录</button>
                </div>
            </div>

            <!-- Save button -->
            <div class="settings-save-row">
                <button class="btn btn-primary" id="btn-save-settings">保存设置</button>
            </div>

        </div>
    `;

    attachListeners();
}

// ── listeners ─────────────────────────────────────────────────────────────────

function attachListeners() {
    if (!_container) return;

    const saveBtn = _container.querySelector('#btn-save-settings');
    if (saveBtn) {
        saveBtn.addEventListener('click', handleSave);
    }

    const logoutBtn = _container.querySelector('#btn-logout');
    if (logoutBtn) {
        logoutBtn.addEventListener('click', handleLogout);
    }
}

function collectFormData() {
    const get = (id) => {
        const el = _container.querySelector('#' + id);
        return el ? el.value.trim() : '';
    };
    const checked = (id) => {
        const el = _container.querySelector('#' + id);
        return el ? el.checked : false;
    };

    return {
        timezone:           get('setting-timezone') || null,
        quiet_hours_start:  get('setting-quiet-start') || null,
        quiet_hours_end:    get('setting-quiet-end') || null,
        daily_report_time:  get('setting-daily-report-time') || null,
        diary_remind_time:  get('setting-diary-remind-time') || null,
        default_category:   get('setting-default-category') || null,
        settings_json: {
            reminder_enabled:     checked('toggle-reminder-enabled'),
            daily_report_enabled: checked('toggle-daily-report-enabled'),
            privacy_mode:         checked('toggle-privacy-mode'),
        },
    };
}

async function handleSave() {
    const saveBtn = _container && _container.querySelector('#btn-save-settings');
    if (saveBtn) {
        saveBtn.disabled = true;
        saveBtn.textContent = '保存中...';
    }

    try {
        const data = collectFormData();
        await api.put('/settings', data);
        showToast('设置已更新', 'success');
    } catch (err) {
        showToast('保存失败：' + err.message, 'error');
    } finally {
        if (saveBtn) {
            saveBtn.disabled = false;
            saveBtn.textContent = '保存设置';
        }
    }
}

function handleLogout() {
    clearToken();
    window.location.reload();
}

// ── data load ─────────────────────────────────────────────────────────────────

async function loadAndRender() {
    if (!_container) return;

    _container.innerHTML = `<div class="empty-state"><p>加载中...</p></div>`;

    let settings = null;
    try {
        const res = await api.get('/settings');
        settings = (res && res.data) ? res.data : {};
    } catch (err) {
        showToast('加载设置失败：' + err.message, 'error');
        settings = {};
    }

    renderPage(settings);
}

// ── page module exports ───────────────────────────────────────────────────────

export function render(container) {
    _container = container;
    loadAndRender();
}

export function destroy() {
    _container = null;
}

export function onRouteEnter(_params) {
    // Nothing special needed on route enter; render() is called by router
}
