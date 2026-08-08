"""Pendo Web 设置页的数据边界、交互与异步生命周期回归。"""

from __future__ import annotations

from typing import Final

from tests.helpers.node_esm import assert_node_esm_contract
from tests.helpers.paths import REPOSITORY_ROOT

ROOT: Final = REPOSITORY_ROOT
SETTINGS_CLIENT: Final = (
    ROOT / "plugins" / "pendo" / "web" / "static" / "js" / "pages" / "settings.js"
)

SETTINGS_SETUP: Final = r"""
    globalThis.__api = {
        get: async () => ({ data: {} }),
        put: async (_path, payload) => ({ data: payload }),
    };
    globalThis.__logoutCalls = 0;
    globalThis.__logout = async () => { __logoutCalls += 1; };
    globalThis.__toastCalls = [];
    globalThis.__navigateCalls = [];
    globalThis.__styleCalls = [];
    globalThis.__reloadCount = 0;
    globalThis.window = {
        location: { reload() { __reloadCount += 1; } },
    };
    globalThis.__flushPromises = async () => {
        await Promise.resolve();
        await Promise.resolve();
        await Promise.resolve();
    };
    globalThis.__deferred = () => {
        let resolve;
        let reject;
        const promise = new Promise((ok, fail) => { resolve = ok; reject = fail; });
        return { promise, resolve, reject };
    };
    globalThis.__makeControl = (extra = {}) => ({
        value: '',
        checked: false,
        disabled: false,
        onclick: null,
        ...extra,
    });
    globalThis.__makeRoot = ({ nodes = {}, onHtml = null } = {}) => {
        let html = '';
        return {
            get innerHTML() { return html; },
            set innerHTML(value) {
                html = value;
                if (onHtml) onHtml(value);
            },
            querySelector(selector) { return nodes[selector] || null; },
        };
    };
"""


def _settings_source_for_test() -> str:
    """替换浏览器相邻依赖，并仅为测试暴露内部纯函数和动作。"""

    source = SETTINGS_CLIENT.read_text(encoding="utf-8")
    replacements = (
        (
            "import { api, logout } from '../api.js';",
            "const api = globalThis.__api;\nconst logout = (...args) => globalThis.__logout(...args);",
        ),
        (
            "import { showToast } from '../components/toast.js';",
            "const showToast = (...args) => globalThis.__toastCalls.push(args);",
        ),
        (
            "import { navigate } from '../router.js';",
            "const navigate = (...args) => globalThis.__navigateCalls.push(args);",
        ),
        (
            "import { errorMessage, isRecord, nonEmptyTextValue as textValue } "
            "from '../utils/format.js';",
            """const isRecord = (value) => value !== null
    && typeof value === 'object'
    && !Array.isArray(value);
const textValue = (value, fallback = '') => {
    const normalized = typeof value === 'string' ? value.trim() : '';
    return normalized || fallback;
};
const errorMessage = (error, fallback = '未知错误') => textValue(error?.message, fallback);""",
        ),
        (
            "import { BREAKPOINTS, escapeHtml, injectStyles, mediaMax, pageShellCss } "
            "from '../utils/ui.js';",
            """const BREAKPOINTS = { XL: '1200px', MOBILE: '720px' };
const escapeHtml = (value) => String(value ?? '')
    .replaceAll('&', '&amp;')
    .replaceAll('<', '&lt;')
    .replaceAll('>', '&gt;')
    .replaceAll('"', '&quot;')
    .replaceAll("'", '&#39;');
const injectStyles = (...args) => globalThis.__styleCalls.push(args);
const mediaMax = (breakpoint, cssText) => `@media (max-width:${breakpoint}) {${cssText}}`;
const pageShellCss = () => '';""",
        ),
    )
    for original, replacement in replacements:
        assert original in source
        source = source.replace(original, replacement)

    return (
        source
        + r"""
export {
    DEFAULT_SETTINGS,
    attachListeners as __attachListeners,
    collectFormData,
    handleLogout as __handleLogout,
    handleSave as __handleSave,
    mergeSavedSettings,
    normalizeSettings,
    normalizeToggleSettings,
    renderPage as __renderPage,
    toggleRow,
};
export function __setSettingsTestState(state = {}) {
    if ('container' in state) _container = state.container;
    if ('settings' in state) _settings = state.settings;
    if ('saving' in state) _saving = state.saving;
    if ('loggingOut' in state) _loggingOut = state.loggingOut;
    if ('lifecycleVersion' in state) _lifecycleVersion = state.lifecycleVersion;
}
export function __getSettingsTestState() {
    return {
        container: _container,
        settings: _settings,
        saving: _saving,
        loggingOut: _loggingOut,
        lifecycleVersion: _lifecycleVersion,
    };
}
"""
    )


def _run_settings_client(script: str) -> None:
    """在 Node 中执行设置页真实 ESM 数据、渲染和生命周期。"""

    assert_node_esm_contract(
        _settings_source_for_test(),
        script,
        cwd=ROOT,
        setup=SETTINGS_SETUP,
    )


def test_settings_page_real_module_imports() -> None:
    """生产模块及其真实依赖图必须可由 ESM 正常解析。"""

    assert_node_esm_contract(
        "export {};",
        "await import('./plugins/pendo/web/static/js/pages/settings.js');",
        cwd=ROOT,
    )


def test_settings_normalizes_boolean_strings_arrays_and_defaults() -> None:
    """旧字符串布尔值必须正确收敛，数组和畸形字段不得污染页面状态。"""

    _run_settings_client(
        r"""
        const toggles = client.normalizeToggleSettings({
            reminder_enabled: 'false',
            daily_briefing_enabled: '0',
            privacy_mode: 'off',
            extension_flag: '保留',
        });
        assert.equal(toggles.reminder_enabled, false);
        assert.equal(toggles.daily_briefing_enabled, false);
        assert.equal('privacy_mode' in toggles, false);
        assert.equal(toggles.extension_flag, '保留');

        const arrayToggles = client.normalizeToggleSettings(['false']);
        assert.deepEqual(arrayToggles, {
            reminder_enabled: true,
            daily_briefing_enabled: true,
        });
        const normalized = client.normalizeSettings({
            timezone: '  ',
            quiet_hours_start: 2300,
            default_category: ' 工作 ',
            settings_json: { reminder_enabled: 'no', privacy_mode: 'unexpected' },
        });
        assert.equal(normalized.timezone, 'Asia/Shanghai');
        assert.equal(normalized.quiet_hours_start, '23:00');
        assert.equal(normalized.default_category, '工作');
        assert.equal(normalized.settings_json.reminder_enabled, false);
        assert.equal('privacy_mode' in normalized.settings_json, false);
        """
    )


def test_settings_render_is_safe_accessible_and_binds_native_buttons() -> None:
    """首屏应转义后端文本，保留开关焦点，并绑定明确类型的原生按钮。"""

    _run_settings_client(
        r"""
        const saveButton = __makeControl();
        const logoutButton = __makeControl();
        const transferButton = __makeControl();
        const root = __makeRoot({ nodes: {
            '#btn-save-settings': saveButton,
            '#btn-logout': logoutButton,
            '#btn-open-transfer': transferButton,
        } });
        __api.get = async () => ({ data: {
            timezone: '<script>时区</script>',
            settings_json: { reminder_enabled: 'false' },
        } });
        await client.render(root);

        assert.ok(root.innerHTML.includes('&lt;script&gt;时区&lt;/script&gt;'));
        assert.ok(!root.innerHTML.includes('<script>时区</script>'));
        assert.ok(root.innerHTML.includes('提醒关闭'));
        assert.equal((root.innerHTML.match(/type="button"/g) || []).length, 3);
        assert.equal(typeof saveButton.onclick, 'function');
        assert.equal(typeof logoutButton.onclick, 'function');
        assert.equal(typeof transferButton.onclick, 'function');
        assert.equal(__styleCalls.length > 0, true);
        assert.ok(__styleCalls.at(-1)[1].includes('input:focus-visible + .settings-slider'));
        assert.ok(__styleCalls.at(-1)[1].includes('font-size: clamp(18px, 1.55vw, 24px)'));

        transferButton.onclick();
        assert.deepEqual(__navigateCalls, [['transfer']]);
        client.destroy();
        await assert.rejects(() => client.render(null), /有效的容器元素/);
        """
    )


def test_settings_collects_before_render_and_preserves_extension_keys() -> None:
    """保存载荷应先读取表单，并保留不属于本页面的 JSON 扩展键。"""

    _run_settings_client(
        r"""
        const controls = {
            '#setting-timezone': __makeControl({ value: ' Europe/Paris ' }),
            '#setting-quiet-start': __makeControl({ value: '22:15' }),
            '#setting-quiet-end': __makeControl({ value: '' }),
            '#setting-daily-report-time': __makeControl({ value: '07:45' }),
            '#setting-diary-remind-time': __makeControl({ value: '20:30' }),
            '#setting-default-category': __makeControl({ value: '  ' }),
            '#toggle-reminder-enabled': __makeControl({ checked: false }),
            '#toggle-daily-report-enabled': __makeControl({ checked: true }),
        };
        let formCleared = false;
        const root = __makeRoot({
            nodes: controls,
            onHtml() {
                formCleared = true;
                for (const control of Object.values(controls)) control.value = '';
            },
        });
        client.__setSettingsTestState({
            container: root,
            lifecycleVersion: 7,
            settings: {
                timezone: 'Asia/Shanghai',
                quiet_hours_end: '06:30',
                settings_json: {
                    reminder_enabled: true,
                    daily_briefing_enabled: false,
                    privacy_mode: true,
                    extension_flag: { nested: true },
                },
            },
        });
        const pending = __deferred();
        const payloads = [];
        __api.put = (_path, payload) => {
            payloads.push(payload);
            return pending.promise;
        };
        const first = client.__handleSave();
        const duplicate = client.__handleSave();
        assert.equal(formCleared, true);
        assert.equal(payloads.length, 1);
        assert.equal(payloads[0].timezone, 'Europe/Paris');
        assert.equal(payloads[0].quiet_hours_end, '06:30');
        assert.equal(payloads[0].default_category, '未分类');
        assert.deepEqual(payloads[0].settings_json.extension_flag, { nested: true });
        assert.equal(payloads[0].settings_json.reminder_enabled, false);
        assert.equal('privacy_mode' in payloads[0].settings_json, false);

        pending.resolve({ data: {
            timezone: 'Europe/Paris',
            settings_json: { reminder_enabled: 'false' },
        } });
        await Promise.all([first, duplicate]);
        const state = client.__getSettingsTestState();
        assert.equal(state.saving, false);
        assert.equal(state.settings.settings_json.reminder_enabled, false);
        assert.deepEqual(state.settings.settings_json.extension_flag, { nested: true });
        assert.deepEqual(__toastCalls, [['设置已更新', 'success']]);
        """
    )


def test_settings_save_failure_recovers_and_destroy_ignores_late_response() -> None:
    """失败保存应恢复按钮；页面销毁后，迟到保存不得写状态或提示成功。"""

    _run_settings_client(
        r"""
        const root = __makeRoot();
        client.__setSettingsTestState({
            container: root,
            lifecycleVersion: 3,
            settings: { timezone: 'Asia/Shanghai', settings_json: { extension: 'keep' } },
        });
        __api.put = async () => { throw new Error('网络失败'); };
        await client.__handleSave();
        let state = client.__getSettingsTestState();
        assert.equal(state.saving, false);
        assert.equal(state.settings.timezone, 'Asia/Shanghai');
        assert.deepEqual(__toastCalls.at(-1), ['保存失败：网络失败', 'error']);

        __toastCalls.length = 0;
        const pending = __deferred();
        __api.put = () => pending.promise;
        const save = client.__handleSave();
        await __flushPromises();
        const htmlBeforeDestroy = root.innerHTML;
        client.destroy();
        pending.resolve({ data: { timezone: 'Etc/UTC' } });
        await save;
        state = client.__getSettingsTestState();
        assert.equal(state.container, null);
        assert.equal(state.settings, null);
        assert.equal(root.innerHTML, htmlBeforeDestroy);
        assert.deepEqual(__toastCalls, []);
        """
    )


def test_settings_latest_load_wins_and_current_load_failure_uses_defaults() -> None:
    """快速换页时只接收最新加载；当前加载失败时应显示默认配置并提示。"""

    _run_settings_client(
        r"""
        const oldRequest = __deferred();
        const newRequest = __deferred();
        let calls = 0;
        __api.get = () => (++calls === 1 ? oldRequest.promise : newRequest.promise);
        const oldRoot = __makeRoot();
        const newRoot = __makeRoot();
        const oldRender = client.render(oldRoot);
        const newRender = client.render(newRoot);
        newRequest.resolve({ data: { timezone: 'Etc/UTC' } });
        await newRender;
        const latestHtml = newRoot.innerHTML;
        assert.ok(latestHtml.includes('Etc/UTC'));

        oldRequest.resolve({ data: { timezone: 'Asia/Tokyo' } });
        await oldRender;
        assert.equal(newRoot.innerHTML, latestHtml);
        assert.ok(!newRoot.innerHTML.includes('Asia/Tokyo'));

        __api.get = async () => { throw {}; };
        const failedRoot = __makeRoot();
        await client.render(failedRoot);
        assert.ok(failedRoot.innerHTML.includes('Asia/Shanghai'));
        assert.deepEqual(__toastCalls.at(-1), ['加载设置失败：未知错误', 'error']);
        """
    )


def test_settings_logout_deduplicates_recovers_and_respects_destroy() -> None:
    """退出登录应防重复、报告失败，并在销毁后忽略迟到结果。"""

    _run_settings_client(
        r"""
        const root = __makeRoot();
        client.__setSettingsTestState({
            container: root,
            lifecycleVersion: 11,
            settings: client.normalizeSettings(null),
        });
        const firstLogout = __deferred();
        __logout = () => {
            __logoutCalls += 1;
            return firstLogout.promise;
        };
        const first = client.__handleLogout();
        const duplicate = client.__handleLogout();
        assert.equal(__logoutCalls, 1);
        firstLogout.resolve();
        await Promise.all([first, duplicate]);
        assert.equal(__reloadCount, 1);
        assert.equal(client.__getSettingsTestState().loggingOut, false);

        __logout = async () => {
            __logoutCalls += 1;
            throw new Error('会话服务不可用');
        };
        await client.__handleLogout();
        assert.deepEqual(__toastCalls.at(-1), ['退出失败：会话服务不可用', 'error']);
        assert.equal(client.__getSettingsTestState().loggingOut, false);

        __toastCalls.length = 0;
        const lateLogout = __deferred();
        __logout = () => lateLogout.promise;
        const late = client.__handleLogout();
        client.destroy();
        lateLogout.reject(new Error('迟到错误'));
        await late;
        assert.deepEqual(__toastCalls, []);
        assert.equal(__reloadCount, 1);
        """
    )
