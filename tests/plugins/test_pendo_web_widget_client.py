"""Pendo Scriptable 客户端的数据边界、刷新和日历同步回归。"""

from __future__ import annotations

from pathlib import Path
from typing import Final

from tests.helpers.node_esm import assert_node_esm_contract

ROOT: Final = Path(__file__).resolve().parents[2]
WIDGET_CLIENT: Final = ROOT / "plugins" / "pendo" / "web" / "scriptable" / "pendo_widget.js"

WIDGET_SETUP: Final = r"""
    globalThis.config = { widgetFamily: 'medium', runsInWidget: true };
    globalThis.args = { widgetParameter: 'auto' };
    globalThis.__keychain = new Map();
    globalThis.Keychain = {
        contains: (key) => __keychain.has(key),
        get: (key) => {
            if (!__keychain.has(key)) throw new Error('missing key');
            return __keychain.get(key);
        },
        set: (key, value) => __keychain.set(key, value),
        remove: (key) => __keychain.delete(key),
    };
    globalThis.Color = class Color {
        constructor(value, alpha = 1) { this.value = value; this.alpha = alpha; }
        static dynamic(light, dark) { return { light, dark }; }
    };
"""


def _widget_source_for_test() -> str:
    """移除 Scriptable 顶层执行段，仅向 Node 测试暴露内部行为边界。"""

    source = WIDGET_CLIENT.read_text(encoding="utf-8")
    marker = "\nlet widget;\n"
    assert marker in source
    source = source.split(marker, maxsplit=1)[0]
    return (
        source
        + r"""
export {
    agendaLeadLabel as __agendaLeadLabel,
    computeNextRefresh as __computeNextRefresh,
    createWidget as __createWidget,
    fetchData as __fetchData,
    loadWidgetToken as __loadWidgetToken,
    normalizeBaseUrl as __normalizeBaseUrl,
    normalizeWidgetData as __normalizeWidgetData,
    parseDateKey as __parseDateKey,
    parseItemStartDate as __parseItemStartDate,
    resolveWidgetSection as __resolveWidgetSection,
    renderLarge as __renderLarge,
    renderMedium as __renderMedium,
    renderSmall as __renderSmall,
    syncAgendaToCalendar as __syncAgendaToCalendar,
};
"""
    )


def _run_widget_client(script: str) -> None:
    """在 Node 中执行 Scriptable 客户端的真实函数实现。"""

    assert_node_esm_contract(
        _widget_source_for_test(),
        script,
        cwd=ROOT,
        setup=WIDGET_SETUP,
    )


def test_scriptable_header_and_transformed_module_parse() -> None:
    """Scriptable 必需元数据保持文件首行，客户端实现可由 ESM 解析。"""

    lines = WIDGET_CLIENT.read_text(encoding="utf-8").splitlines()
    assert lines[:3] == [
        "// Variables used by Scriptable.",
        "// These must be at the very top of the file. Do not edit.",
        "// icon-color: deep-purple; icon-glyph: magic;",
    ]
    source = WIDGET_CLIENT.read_text(encoding="utf-8")
    assert "const TOKEN =" not in source
    assert "Keychain.set(" in source
    assert "Keychain.get(" in source
    _run_widget_client("assert.equal(typeof client.__normalizeWidgetData, 'function');")


def test_scriptable_normalizes_url_section_and_summary_shape() -> None:
    """地址、板块和摘要响应必须在渲染前收敛到有限稳定结构。"""

    _run_widget_client(
        r"""
        assert.equal(
            client.__normalizeBaseUrl(' https://host/pendo/api/widget/summary?section=tasks '),
            'https://host/pendo',
        );
        assert.equal(client.__resolveWidgetSection('large', 'tasks'), 'all');
        assert.equal(client.__resolveWidgetSection('small', 'ledger'), 'auto');
        assert.equal(client.__resolveWidgetSection('medium', 'LEDGER'), 'ledger');
        assert.equal(client.__resolveWidgetSection('medium', 'unknown'), 'auto');
        assert.equal(client.__parseDateKey('2026-02-30'), null);

        assert.throws(() => client.__normalizeWidgetData(null), /摘要结构无效/);
        const items = Array.from({ length: 7 }, (_, index) => ({
            title: index ? `事项 ${index}` : '',
            day: index === 0 ? '2026-02-30' : '2026-05-01',
            start_time: '2026-05-01T10:00:00',
        }));
        const data = client.__normalizeWidgetData({
            generated_at: '2026-05-01T09:00:00',
            section: 'all',
            agenda: {
                date: { weekday: ' 周五 ', day: 99 },
                today_count: '3.9', tomorrow_count: -1, items,
            },
            links: { dashboard: ' #/dashboard ' },
            panel: [],
            panels: {
                ledger: {
                    section: 'ledger', title: ' 财务 ', items: [
                        { title: '', transaction_type: '<bad>', amount_text: ' -¥20 ' },
                    ],
                },
            },
        });
        assert.equal(data.agenda.items.length, 5);
        assert.equal(data.agenda.items[0].title, '无标题');
        assert.equal(data.agenda.items[0].day, '');
        assert.equal(data.agenda.date.day, 0);
        assert.equal(data.agenda.today_count, 3);
        assert.equal(data.agenda.tomorrow_count, 0);
        assert.equal(data.panel, null);
        assert.equal(data.panels.ledger.title, '财务');
        assert.equal(data.panels.ledger.items[0].transaction_type, '');
        assert.equal(data.links.dashboard, '#/dashboard');
        assert.equal(
            client.__agendaLeadLabel(data.agenda.items[1], data),
            '今天',
        );
        """
    )


def test_scriptable_fetch_checks_http_envelope_and_normalizes_data() -> None:
    """请求必须携带只读令牌，并拒绝非 JSON、错误状态和畸形信封。"""

    _run_widget_client(
        r"""
        globalThis.__requestResponse = {
            status: 200,
            body: JSON.stringify({
                ok: true,
                data: { agenda: { date: {}, items: [], today_count: 0, tomorrow_count: 0 } },
            }),
        };
        globalThis.__requests = [];
        globalThis.Request = class Request {
            constructor(url) {
                this.url = url;
                this.response = { statusCode: __requestResponse.status };
                __requests.push(this);
            }
            async loadString() {
                this.response.statusCode = __requestResponse.status;
                return __requestResponse.body;
            }
        };

        const data = await client.__fetchData('tasks & notes', 'KEYCHAIN_WIDGET_TOKEN');
        assert.equal(data.agenda.items.length, 0);
        assert.ok(__requests[0].url.endsWith('section=tasks%20%26%20notes'));
        assert.equal(__requests[0].method, 'GET');
        assert.equal(__requests[0].timeoutInterval, 20);
        assert.equal(__requests[0].headers.Authorization, 'Bearer KEYCHAIN_WIDGET_TOKEN');

        __requestResponse = { status: 502, body: JSON.stringify({ ok: true, data: { agenda: {} } }) };
        await assert.rejects(() => client.__fetchData('auto'), /HTTP 502/);

        __requestResponse = { status: 200, body: 'not-json' };
        await assert.rejects(() => client.__fetchData('auto'), /接口未返回 JSON/);

        __requestResponse = { status: 200, body: JSON.stringify({ ok: true, data: [] }) };
        await assert.rejects(() => client.__fetchData('auto'), /摘要结构无效/);

        Keychain.set('pendo.widget-token:https://example.com/pendo', 'expired-token');
        __requestResponse = { status: 401, body: JSON.stringify({ ok: false, message: 'revoked' }) };
        await assert.rejects(
            () => client.__fetchData('auto', 'expired-token'),
            /失效或被吊销/,
        );
        assert.equal(__keychain.size, 0);
        """
    )


def test_scriptable_reads_existing_token_only_from_keychain() -> None:
    """已配置令牌直接从 Keychain 读取，脚本模块不需要令牌常量。"""

    _run_widget_client(
        r"""
        Keychain.set('pendo.widget-token:https://example.com/pendo', 'stored-widget-token');
        assert.equal(await client.__loadWidgetToken(), 'stored-widget-token');
        """
    )


def test_scriptable_prompts_securely_and_persists_first_token() -> None:
    """App 内首次运行使用安全输入框，并把录入值写入 Keychain。"""

    _run_widget_client(
        r"""
        config.runsInWidget = false;
        globalThis.Alert = class Alert {
            addSecureTextField(label, value) {
                this.secureField = { label, value };
            }
            addAction(label) { this.actionLabel = label; }
            addCancelAction(label) { this.cancelLabel = label; }
            async presentAlert() { return 0; }
            textFieldValue(index) {
                assert.equal(index, 0);
                return 'new-widget-token';
            }
        };

        assert.equal(await client.__loadWidgetToken(), 'new-widget-token');
        assert.equal(
            Keychain.get('pendo.widget-token:https://example.com/pendo'),
            'new-widget-token',
        );
        """
    )


def test_scriptable_refresh_keeps_one_minute_candidate() -> None:
    """五分钟内的日程应建议一分钟后刷新，不能被边界过滤成半小时。"""

    _run_widget_client(
        r"""
        const now = new Date('2026-05-01T10:00:00Z');
        const nearStart = new Date(now.getTime() + 2 * 60 * 1000).toISOString();
        const refresh = client.__computeNextRefresh(
            { agenda: { items: [{ start_time: nearStart }] } },
            now,
        );
        assert.equal(refresh.getTime(), now.getTime() + 60 * 1000);

        const fallback = client.__computeNextRefresh({ agenda: { items: [] } }, now);
        assert.equal(fallback.getTime(), now.getTime() + 30 * 60 * 1000);
        assert.equal(client.__parseItemStartDate({ day: '2026-02-30' }), null);
        assert.equal(
            client.__parseItemStartDate({ day: '2026-05-01', meta: '25:99' }),
            null,
        );
        """
    )


def test_scriptable_renders_all_widget_families_with_normalized_data() -> None:
    """小、中、大三种布局都应只消费规范化模型并完成真实渲染调用。"""

    _run_widget_client(
        r"""
        class FakeStack {
            constructor(root = null) {
                this.root = root || this;
                if (!root) this.texts = [];
            }
            addStack() { return new FakeStack(this.root); }
            addSpacer() {}
            addText(value) {
                const node = {};
                this.root.texts.push(String(value));
                return node;
            }
            addImage() { return {}; }
            layoutHorizontally() {}
            layoutVertically() {}
            centerAlignContent() {}
            setPadding() {}
        }
        globalThis.Size = class Size { constructor(width, height) { this.width = width; this.height = height; } };
        globalThis.Rect = class Rect { constructor(...args) { this.args = args; } };
        globalThis.LinearGradient = class LinearGradient {};
        globalThis.DrawContext = class DrawContext {
            setFillColor() {}
            fillEllipse() {}
            setStrokeColor() {}
            setLineWidth() {}
            strokeEllipse() {}
            getImage() { return {}; }
        };
        globalThis.ListWidget = class ListWidget extends FakeStack {};
        globalThis.Font = {
            systemFont: (size) => ({ size }),
            mediumSystemFont: (size) => ({ size }),
            mediumMonospacedSystemFont: (size) => ({ size }),
            semiboldSystemFont: (size) => ({ size }),
        };
        globalThis.SFSymbol = { named: (name) => ({ name, image: {} }) };

        const data = client.__normalizeWidgetData({
            generated_at: '2026-05-01T09:00:00',
            agenda: {
                date: { weekday: '周五', day: 1 }, today_count: 1, tomorrow_count: 0,
                items: [{
                    title: '项目会议', day: '2026-05-01', meta: '10:00 · 会议室',
                    start_time: '2026-05-01T10:00:00',
                }],
            },
            links: {},
            panel: {
                section: 'tasks', title: '待办', summary: { primary: '1 项待办' },
                items: [{ title: '写报告', meta: '待办 · 今天' }],
            },
            panels: {
                tasks: {
                    section: 'tasks', title: '待办',
                    items: [{ title: '写报告', meta: '待办 · 今天' }],
                },
                ledger: {
                    section: 'ledger', title: '财务',
                    items: [{ title: '午饭', transaction_type: 'expense', amount_text: '-¥20' }],
                },
                notes: {
                    section: 'notes', title: '笔记', items: [{ title: '会议记录' }],
                },
            },
        });

        const small = new FakeStack();
        const medium = new FakeStack();
        const large = new FakeStack();
        client.__renderSmall(small, data);
        client.__renderMedium(medium, data);
        client.__renderLarge(large, data);
        for (const [root, expected] of [
            [small, '项目会议'], [medium, '写报告'], [large, '午饭'],
        ]) assert.ok(root.texts.includes(expected));

        const widget = client.__createWidget(data);
        assert.equal(widget instanceof ListWidget, true);
        assert.ok(widget.refreshAfterDate instanceof Date);
        assert.ok(widget.url.endsWith('/#/dashboard'));
        """
    )


def test_scriptable_calendar_sync_is_add_only_and_reuses_summary() -> None:
    """有限摘要只能增量写入日历，不得据此删除未出现在五条摘要中的旧事件。"""

    _run_widget_client(
        r"""
        const now = new Date('2026-05-01T08:00:00');
        const duplicateStart = new Date('2026-05-01T10:00:00');
        const targetCalendar = { title: 'Pendo', allowsContentModifications: true };
        let removed = 0;
        const existing = [
            { title: '已有会议', startDate: duplicateStart },
            {
                title: '摘要未覆盖但仍有效的日程',
                startDate: new Date('2026-05-20T09:00:00'),
                notes: '[由 Pendo Widget 同步]',
                remove() { removed += 1; },
            },
        ];
        globalThis.Calendar = {
            forEventsByTitle: async () => targetCalendar,
            forEvents: async () => [targetCalendar],
        };
        globalThis.__createdEvents = [];
        globalThis.CalendarEvent = class CalendarEvent {
            constructor() { __createdEvents.push(this); }
            static async between() { return existing; }
            save() { this.saved = true; }
        };

        const data = client.__normalizeWidgetData({
            generated_at: '2026-05-01T08:00:00',
            agenda: {
                date: {}, today_count: 0, tomorrow_count: 0,
                items: [
                    { title: '已有会议', day: '2026-05-01', start_time: '2026-05-01T10:00:00' },
                    {
                        title: '新会议', day: '2026-05-01',
                        start_time: '2026-05-01T12:00:00', end_time: '2026-05-01T11:00:00',
                        location: '会议室',
                    },
                    { title: '全天事项', day: '2026-05-02' },
                ],
            },
        });
        const result = await client.__syncAgendaToCalendar(data, now);
        assert.equal(result, '同步完成：新增 2，跳过 1');
        assert.equal(__createdEvents.length, 2);
        assert.equal(removed, 0);
        assert.equal(__createdEvents[0].saved, true);
        assert.equal(__createdEvents[0].endDate.getTime() > __createdEvents[0].startDate.getTime(), true);
        assert.equal(__createdEvents[0].location, '会议室');
        assert.equal(__createdEvents[1].isAllDay, true);
        assert.equal(
            __createdEvents[1].endDate.getTime() - __createdEvents[1].startDate.getTime(),
            24 * 60 * 60 * 1000,
        );
        """
    )


def test_scriptable_calendar_sync_handles_disabled_empty_and_missing_calendar() -> None:
    """没有摘要或没有可写日历时应稳定返回，不创建任何事件。"""

    _run_widget_client(
        r"""
        globalThis.Calendar = {
            forEventsByTitle: async () => null,
            forEvents: async () => [],
        };
        globalThis.CalendarEvent = class CalendarEvent {};
        assert.equal(
            await client.__syncAgendaToCalendar({ agenda: { items: [] } }),
            '没有需要同步的日程',
        );
        assert.equal(
            await client.__syncAgendaToCalendar({
                agenda: { items: [{ title: '会议', day: '2026-05-01' }] },
            }, new Date('2026-05-01T08:00:00')),
            '未找到名为「Pendo」的日历，请先在系统日历 App 中创建',
        );
        """
    )
