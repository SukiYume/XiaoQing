import { api, logout } from '../api.js';
import { showToast } from '../components/toast.js';
import { navigate } from '../router.js';
import { errorMessage, isRecord, nonEmptyTextValue as textValue } from '../utils/format.js';
import { setUserTimeZone } from '../utils/timezone.js';
import { escapeHtml } from '../utils/ui.js';

const DEFAULT_SETTINGS = Object.freeze({
    timezone: 'Asia/Shanghai',
    quiet_hours_start: '23:00',
    quiet_hours_end: '07:00',
    daily_report_time: '08:00',
    diary_remind_time: '21:30',
    default_category: '未分类',
    settings_json: Object.freeze({
        reminder_enabled: true,
        daily_briefing_enabled: true,
    }),
});

let _container        = null;
let _settings         = null;
let _saving           = false;
let _loggingOut       = false;
let _lifecycleVersion = 0;
let _loadError        = '';

// 数据边界：只接收可解释的后端值，并为旧数据补齐稳定默认值。
function booleanValue(value, fallback) {
    if (typeof value === 'boolean') return value;
    if (value === 1 || value === '1') return true;
    if (value === 0 || value === '0') return false;
    if (typeof value !== 'string') return fallback;

    const normalized = value.trim().toLowerCase();
    if (['true', 'yes', 'on'].includes(normalized)) return true;
    if (['false', 'no', 'off'].includes(normalized)) return false;
    return fallback;
}

function normalizeToggleSettings(settingsJson) {
    const raw    = isRecord(settingsJson) ? settingsJson : {};
    const active = { ...raw };
    delete active.privacy_mode;
    return {
        ...active,
        reminder_enabled: booleanValue(raw.reminder_enabled, DEFAULT_SETTINGS.settings_json.reminder_enabled),
        daily_briefing_enabled: booleanValue(
            raw.daily_briefing_enabled,
            DEFAULT_SETTINGS.settings_json.daily_briefing_enabled,
        ),
    };
}

function normalizeSettings(value) {
    const raw = isRecord(value) ? value : {};
    return {
        ...raw,
        timezone: textValue(raw.timezone, DEFAULT_SETTINGS.timezone),
        quiet_hours_start: textValue(raw.quiet_hours_start, DEFAULT_SETTINGS.quiet_hours_start),
        quiet_hours_end: textValue(raw.quiet_hours_end, DEFAULT_SETTINGS.quiet_hours_end),
        daily_report_time: textValue(raw.daily_report_time, DEFAULT_SETTINGS.daily_report_time),
        diary_remind_time: textValue(raw.diary_remind_time, DEFAULT_SETTINGS.diary_remind_time),
        default_category: textValue(raw.default_category, DEFAULT_SETTINGS.default_category),
        settings_json: normalizeToggleSettings(raw.settings_json),
    };
}

function mergeSavedSettings(responseData, payload) {
    const current    = normalizeSettings(_settings);
    const optimistic = normalizeSettings({
        ...current,
        ...payload,
        settings_json: {
            ...current.settings_json,
            ...payload.settings_json,
        },
    });
    if (!isRecord(responseData)) return optimistic;

    return normalizeSettings({
        ...optimistic,
        ...responseData,
        settings_json: {
            ...optimistic.settings_json,
            ...(isRecord(responseData.settings_json) ? responseData.settings_json : {}),
        },
    });
}

function isCurrentLifecycle(container, version) {
    return _container === container && _lifecycleVersion === version;
}

// 页面样式按视觉区域排列，开关保留原生焦点能力。

// 渲染函数只消费已归一化状态，所有插值都经过转义。
function toggleRow(id, title, description, checked) {
    const safeId    = escapeHtml(id);
    const safeTitle = escapeHtml(title);
    return `
        <div class="settings-toggle-row">
            <div>
                <div class="settings-toggle-title">${safeTitle}</div>
                <div class="settings-toggle-desc">${escapeHtml(description)}</div>
            </div>
            <label class="settings-switch">
                <input type="checkbox" id="${safeId}" aria-label="${safeTitle}" ${checked ? 'checked' : ''}>
                <span class="settings-slider" aria-hidden="true"></span>
            </label>
        </div>
    `;
}

function renderPage() {
    if (!_container) return;
    if (!_settings) {
        _container.innerHTML = `
            <div class="settings-shell" aria-busy="${_loadError ? 'false' : 'true'}">
                <div class="settings-panel" role="status" aria-live="polite">
                    <h3>${_loadError ? '设置加载失败' : '正在加载设置...'}</h3>
                    ${_loadError ? `<p>${escapeHtml(_loadError)}</p><button class="btn btn-primary" id="settings-retry" type="button">重新加载</button>` : ''}
                </div>
            </div>
        `;
        const retry = _container.querySelector('#settings-retry');
        if (retry) retry.onclick = () => {
            _loadError = '';
            renderPage();
            void loadAndRender(_container, _lifecycleVersion);
        };
        return;
    }

    const settings = normalizeSettings(_settings);
    const toggles  = settings.settings_json;
    _container.innerHTML = `
        <div class="settings-shell">
            <div class="settings-stack">
                <section class="settings-hero pendo-page-header">
                    <div>
                        <span class="eyebrow"><i></i>SETTINGS</span>
                        <h2>设置</h2>
                        <p>管理时区、提醒和默认设置。</p>
                    </div>
                    <button type="button" class="btn btn-primary" id="btn-save-settings" ${_saving ? 'disabled aria-busy="true"' : ''}>${_saving ? '保存中...' : '保存设置'}</button>
                </section>

                <section class="settings-layout">
                    <div class="settings-main-stack">
                        <div class="settings-panel">
                            <h3>时间与默认项</h3>
                            <p>默认时区为 Asia/Shanghai。日程与提醒采用你保存的时区。</p>
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
                            <p>导出完整备份，或从备份包导入历史记录。</p>
                            <div class="settings-callout-card settings-transfer-card">
                                <div class="settings-callout-copy">
                                    <div class="settings-callout-title">打开数据迁移页</div>
                                    <div class="settings-callout-desc">查看导出范围、预检备份内容，并选择需要导入的数据类型。</div>
                                </div>
                                <button type="button" class="btn btn-secondary settings-transfer-btn" id="btn-open-transfer">打开页面</button>
                            </div>
                        </section>
                    </div>

                    <div class="settings-side-stack">
                        <section class="settings-panel">
                            <h3>功能开关</h3>
                            <p>调整后点击页面顶部的“保存设置”。</p>
                            <div class="settings-toggle-list">
                                ${toggleRow('toggle-reminder-enabled', '提醒通知', '控制事件提醒与待办提醒是否整体启用。', toggles.reminder_enabled)}
                                ${toggleRow('toggle-daily-report-enabled', '每日简报', '决定是否按配置时间生成每日简报。', toggles.daily_briefing_enabled)}
                            </div>
                        </section>

                        <section class="settings-panel">
                            <h3>登录与令牌</h3>
                            <p>需要重新登录时，可以从这里退出当前状态。</p>
                            <div class="settings-callout-card">
                                <div class="settings-callout-copy">
                                    <div class="settings-callout-title">当前登录状态</div>
                                    <div class="settings-callout-desc">当前 Web 端已登录。如需重新生成令牌，请回到聊天里执行 <code>/pendo web token</code>。</div>
                                </div>
                            </div>
                            <div class="settings-danger">
                                <button type="button" class="btn btn-secondary" id="btn-logout" ${_loggingOut ? 'disabled aria-busy="true"' : ''}>${_loggingOut ? '正在退出...' : '退出登录'}</button>
                            </div>
                        </section>
                    </div>
                </section>

            </div>
        </div>
    `;
    attachListeners();
}

// 表单读取先于重渲染。缺失控件回退到当前值，避免意外清空设置。
// 只提交与页面加载时不同的字段：其他入口在此期间写入的设置（例如撤回 AI 授权）
// 不会被这份旧快照改回去，界面没有展示的扩展开关也不会被回传。
function collectFormData(container = _container) {
    const current = normalizeSettings(_settings);
    const form    = readFormData(container, current);
    const patch   = {};
    for (const [key, value] of Object.entries(form)) {
        if (key !== 'settings_json' && value !== current[key]) patch[key] = value;
    }
    const custom        = normalizeToggleSettings(current.settings_json);
    const changedCustom = {};
    for (const [key, value] of Object.entries(form.settings_json)) {
        if (value !== custom[key]) changedCustom[key] = value;
    }
    if (Object.keys(changedCustom).length) patch.settings_json = changedCustom;
    return patch;
}

function readFormData(container, current) {
    if (!container || typeof container.querySelector !== 'function') {
        throw new TypeError('设置页面尚未挂载');
    }

    const custom = normalizeToggleSettings(current.settings_json);
    const readText = (id, fallback) => textValue(container.querySelector(`#${id}`)?.value, fallback);
    const readChecked = (id, fallback) => {
        const checked = container.querySelector(`#${id}`)?.checked;
        return typeof checked === 'boolean' ? checked : fallback;
    };

    return {
        timezone: readText('setting-timezone', current.timezone),
        quiet_hours_start: readText('setting-quiet-start', current.quiet_hours_start),
        quiet_hours_end: readText('setting-quiet-end', current.quiet_hours_end),
        daily_report_time: readText('setting-daily-report-time', current.daily_report_time),
        diary_remind_time: readText('setting-diary-remind-time', current.diary_remind_time),
        default_category: readText('setting-default-category', DEFAULT_SETTINGS.default_category),
        settings_json: {
            reminder_enabled: readChecked('toggle-reminder-enabled', custom.reminder_enabled),
            daily_briefing_enabled: readChecked('toggle-daily-report-enabled', custom.daily_briefing_enabled),
        },
    };
}

// 用户动作均带重复提交和生命周期保护，迟到响应不能覆盖新页面。
async function handleSave() {
    if (_saving || !_container || !_settings) return;

    const container = _container;
    const version   = _lifecycleVersion;
    const payload   = collectFormData(container);
    _saving = true;
    updateActionState(container);
    try {
        const response = await api.put('/settings', payload);
        if (!isCurrentLifecycle(container, version)) return;
        _settings = mergeSavedSettings(response?.data, payload);
        setUserTimeZone(_settings.timezone);
        showToast('设置已更新', 'success');
    } catch (error) {
        if (isCurrentLifecycle(container, version)) {
            showToast(`保存失败：${errorMessage(error, '未知错误')}`, 'error');
        }
    } finally {
        if (isCurrentLifecycle(container, version)) {
            _saving = false;
            updateActionState(container);
        }
    }
}

async function handleLogout() {
    if (_loggingOut || !_container) return;

    const container = _container;
    const version   = _lifecycleVersion;
    _loggingOut = true;
    updateActionState(container);
    try {
        await logout();
        if (isCurrentLifecycle(container, version)) window.location.reload();
    } catch (error) {
        if (isCurrentLifecycle(container, version)) {
            showToast(`退出失败：${errorMessage(error, '未知错误')}`, 'error');
        }
    } finally {
        if (isCurrentLifecycle(container, version)) {
            _loggingOut = false;
            updateActionState(container);
        }
    }
}

function updateActionState(container) {
    const save = container?.querySelector('#btn-save-settings');
    if (save) {
        save.disabled = _saving;
        save.textContent = _saving ? '保存中…' : '保存设置';
    }
    const exit = container?.querySelector('#btn-logout');
    if (exit) exit.disabled = _loggingOut;
}

function attachListeners() {
    if (!_container) return;
    const saveButton = _container.querySelector('#btn-save-settings');
    if (saveButton) saveButton.onclick = handleSave;
    const logoutButton = _container.querySelector('#btn-logout');
    if (logoutButton) logoutButton.onclick = handleLogout;
    const transferButton = _container.querySelector('#btn-open-transfer');
    if (transferButton) transferButton.onclick = () => navigate('transfer');
}

async function loadAndRender(container, version) {
    try {
        const response = await api.get('/settings');
        if (!isCurrentLifecycle(container, version)) return;
        if (!isRecord(response?.data)) throw new TypeError('设置响应格式无效');
        _settings = normalizeSettings(response?.data);
        _loadError = '';
        setUserTimeZone(_settings.timezone);
    } catch (error) {
        if (!isCurrentLifecycle(container, version)) return;
        _settings = null;
        _loadError = errorMessage(error, '未知错误');
        showToast(`加载设置失败：${_loadError}`, 'error');
    }
    if (isCurrentLifecycle(container, version)) renderPage();
}

// 路由生命周期：render 可等待首屏数据，destroy 使所有在途请求立即失效。
export async function render(container) {
    if (!container || typeof container.querySelector !== 'function') {
        throw new TypeError('设置页需要有效的容器元素');
    }

    const version = ++_lifecycleVersion;
    _container = container;
    _settings = null;
    _loadError = '';
    _saving = false;
    _loggingOut = false;
    renderPage();
    await loadAndRender(container, version);
}

export function destroy() {
    _lifecycleVersion += 1;
    _container = null;
    _settings = null;
    _saving = false;
    _loggingOut = false;
}
