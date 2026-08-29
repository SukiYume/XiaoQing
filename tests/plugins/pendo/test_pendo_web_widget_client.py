"""Pendo Scriptable 客户端的数据边界、刷新和日历同步回归。"""

from __future__ import annotations

from typing import Final

from tests.helpers.node_esm import assert_node_esm_contract
from tests.helpers.paths import REPOSITORY_ROOT

ROOT: Final = REPOSITORY_ROOT
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
    applyAgendaItemsToCalendar as __applyAgendaItemsToCalendar,
    buildCalendarSyncWindow as __buildCalendarSyncWindow,
    computeNextRefresh as __computeNextRefresh,
    createWidget as __createWidget,
    fetchData as __fetchData,
    normalizeBaseUrl as __normalizeBaseUrl,
    normalizeWidgetData as __normalizeWidgetData,
    parseDateKey as __parseDateKey,
    parseItemStartDate as __parseItemStartDate,
    resolveWidgetSection as __resolveWidgetSection,
    renderLarge as __renderLarge,
    renderMedium as __renderMedium,
    renderSmall as __renderSmall,
    syncCalendarFromServer as __syncCalendarFromServer,
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
    assert "const TOKEN = 'PASTE_WIDGET_TOKEN_HERE';" in source
    assert "TOKEN_KEYCHAIN_KEY" not in source
    assert "new Alert()" not in source
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

        Keychain.set('calendar-cursor', '2026-08-01');
        __requestResponse = { status: 401, body: JSON.stringify({ ok: false, message: 'revoked' }) };
        await assert.rejects(
            () => client.__fetchData('auto', 'expired-token'),
            /失效或被吊销/,
        );
        assert.equal(Keychain.get('calendar-cursor'), '2026-08-01');
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


def test_scriptable_calendar_sync_reconciles_managed_events() -> None:
    """同步窗口新增、更新和清理托管日程，但不接管用户事件。"""

    _run_widget_client(
        r"""
        const targetCalendar = { title: 'Pendo', allowsContentModifications: true };
        let removed = 0;
        let updated = 0;
        const editedEvent = {
            title: '会议旧标题',
            startDate: new Date('2026-05-01T09:00:00'),
            endDate: new Date('2026-05-01T10:00:00'),
            isAllDay: false,
            location: '旧会议室',
            notes: '[由 Pendo Widget 同步]\nPendo-ID: edited-meeting\n保留这行备注',
            save() { updated += 1; },
            remove() { removed += 1; },
        };
        const unchangedEvent = {
            title: '没有修改的会议',
            startDate: new Date('2026-05-03T10:00:00'),
            endDate: new Date('2026-05-03T11:00:00'),
            isAllDay: false,
            location: '会议室 B',
            notes: '[由 Pendo Widget 同步]\nPendo-ID: unchanged-meeting',
            save() { updated += 1; },
            remove() { removed += 1; },
        };
        const staleEvent = {
            title: '已删除的 Pendo 日程',
            startDate: new Date('2026-05-04T10:00:00'),
            endDate: new Date('2026-05-04T11:00:00'),
            isAllDay: false,
            location: '',
            notes: '[由 Pendo Widget 同步]\nPendo-ID: stale-meeting',
            save() { updated += 1; },
            remove() { removed += 1; },
        };
        const duplicateManagedEvent = {
            title: '没有修改的会议',
            startDate: new Date('2026-05-03T10:00:00'),
            endDate: new Date('2026-05-03T11:00:00'),
            isAllDay: false,
            location: '会议室 B',
            notes: '[由 Pendo Widget 同步]\nPendo-ID: unchanged-meeting',
            save() { updated += 1; },
            remove() { removed += 1; },
        };
        const userEvent = {
            title: '用户自己的会议',
            startDate: new Date('2026-05-05T10:00:00'),
            endDate: new Date('2026-05-05T11:00:00'),
            isAllDay: false,
            location: '',
            notes: '私人日程',
            save() { throw new Error('must not update user event'); },
            remove() { throw new Error('must not remove user event'); },
        };
        const markerMentionEvent = {
            title: '只在正文提到同步标记',
            startDate: new Date('2026-05-06T10:00:00'),
            endDate: new Date('2026-05-06T11:00:00'),
            isAllDay: false,
            location: '',
            notes: '普通说明中提到 [由 Pendo Widget 同步]\nPendo-ID: not-managed',
            save() { throw new Error('must not update marker mention'); },
            remove() { throw new Error('must not remove marker mention'); },
        };
        const existing = [
            editedEvent,
            unchangedEvent,
            duplicateManagedEvent,
            staleEvent,
            userEvent,
            markerMentionEvent,
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
                    {
                        id: 'edited-meeting', title: '会议新标题', day: '2026-05-01',
                        start_time: '2026-05-01T10:00:00',
                        end_time: '2026-05-01T12:00:00', location: '',
                    },
                    {
                        id: 'unchanged-meeting', title: '没有修改的会议', day: '2026-05-03',
                        start_time: '2026-05-03T10:00:00',
                        end_time: '2026-05-03T11:00:00', location: '会议室 B',
                    },
                    {
                        id: 'new-meeting', title: '新会议', day: '2026-05-01',
                        start_time: '2026-05-01T12:00:00', end_time: '2026-05-01T11:00:00',
                        location: '会议室',
                    },
                    { id: 'all-day', title: '全天事项', day: '2026-05-02' },
                    {
                        id: 'manual-collision', title: '用户自己的会议', day: '2026-05-05',
                        start_time: '2026-05-05T10:00:00',
                    },
                ],
            },
        });
        const rangeStart = new Date('2026-05-01T00:00:00');
        const rangeEnd = new Date('2026-05-31T00:00:00');
        const result = await client.__applyAgendaItemsToCalendar(
            data.agenda.items,
            rangeStart,
            rangeEnd,
        );
        assert.equal(result.message, '同步完成：新增 2，更新 1，删除 2，未变化 1，跳过 1');
        assert.equal(__createdEvents.length, 2);
        assert.equal(updated, 1);
        assert.equal(removed, 2);
        assert.equal(editedEvent.title, '会议新标题');
        assert.equal(editedEvent.startDate.getTime(), new Date('2026-05-01T10:00:00').getTime());
        assert.equal(editedEvent.endDate.getTime(), new Date('2026-05-01T12:00:00').getTime());
        assert.equal(editedEvent.location, '');
        assert.match(editedEvent.notes, /保留这行备注/);
        assert.equal(__createdEvents[0].saved, true);
        assert.match(__createdEvents[0].notes, /Pendo-ID: new-meeting/);
        assert.equal(__createdEvents[0].endDate.getTime() > __createdEvents[0].startDate.getTime(), true);
        assert.equal(__createdEvents[0].location, '会议室');
        assert.equal(__createdEvents[1].isAllDay, true);
        assert.equal(
            __createdEvents[1].endDate.getTime() - __createdEvents[1].startDate.getTime(),
            24 * 60 * 60 * 1000,
        );
        """
    )


def test_scriptable_calendar_sync_preserves_distinct_same_time_items() -> None:
    """Pendo ID 区分同名同刻条目，并识别已经同步的同一条目。"""

    _run_widget_client(
        r"""
        const start = new Date('2026-05-01T10:00:00');
        const legacyStart = new Date('2026-05-01T11:00:00');
        const targetCalendar = { title: 'Pendo', allowsContentModifications: true };
        globalThis.Calendar = {
            forEventsByTitle: async () => targetCalendar,
            forEvents: async () => [targetCalendar],
        };
        globalThis.__createdEvents = [];
        globalThis.CalendarEvent = class CalendarEvent {
            constructor() { __createdEvents.push(this); }
            static async between() {
                return [
                    {
                        title: '同步会议',
                        startDate: start,
                        endDate: new Date('2026-05-01T11:00:00'),
                        isAllDay: false,
                        location: '',
                        notes: '[由 Pendo Widget 同步]\nPendo-ID: already-synced',
                    },
                    {
                        title: '旧键会议',
                        startDate: legacyStart,
                        endDate: new Date('2026-05-01T12:00:00'),
                        isAllDay: false,
                        location: '',
                        notes: '[由 Pendo Widget 同步]',
                        save() { this.saved = true; },
                    },
                ];
            }
            save() { this.saved = true; }
        };

        const items = [
            { id: 'already-synced', title: '同步会议', start_time: '2026-05-01T10:00:00' },
            { id: 'distinct-a', title: '同步会议', start_time: '2026-05-01T10:00:00' },
            { id: 'distinct-b', title: '同步会议', start_time: '2026-05-01T10:00:00' },
            { id: 'legacy-a', title: '旧键会议', start_time: '2026-05-01T11:00:00' },
            { id: 'legacy-b', title: '旧键会议', start_time: '2026-05-01T11:00:00' },
        ];
        const result = await client.__applyAgendaItemsToCalendar(
            items,
            new Date('2026-05-01T00:00:00'),
            new Date('2026-05-02T00:00:00'),
        );

        assert.equal(result.message, '同步完成：新增 3，更新 1，未变化 1');
        assert.equal(__createdEvents.length, 3);
        assert.match(__createdEvents[0].notes, /Pendo-ID: distinct-a/);
        assert.match(__createdEvents[1].notes, /Pendo-ID: distinct-b/);
        assert.match(__createdEvents[2].notes, /Pendo-ID: legacy-b/);
        """
    )


def test_scriptable_calendar_sync_accepts_events_overlapping_window() -> None:
    """跨天日程开始在窗口外时，仍按完整起止时间写入。"""

    _run_widget_client(
        r"""
        const targetCalendar = { title: 'Pendo', allowsContentModifications: true };
        globalThis.Calendar = {
            forEventsByTitle: async () => targetCalendar,
            forEvents: async () => [targetCalendar],
        };
        globalThis.__createdEvents = [];
        globalThis.CalendarEvent = class CalendarEvent {
            constructor() { __createdEvents.push(this); }
            static async between() { return []; }
            save() { this.saved = true; }
        };

        const result = await client.__applyAgendaItemsToCalendar(
            [{
                id: 'cross-window',
                title: '跨窗口日程',
                start_time: '2026-04-30T22:00:00',
                end_time: '2026-05-01T02:00:00',
            }],
            new Date('2026-05-01T00:00:00'),
            new Date('2026-05-02T00:00:00'),
        );

        assert.equal(result.message, '同步完成：新增 1');
        assert.equal(__createdEvents.length, 1);
        assert.equal(
            __createdEvents[0].startDate.getTime(),
            new Date('2026-04-30T22:00:00').getTime(),
        );
        assert.equal(
            __createdEvents[0].endDate.getTime(),
            new Date('2026-05-01T02:00:00').getTime(),
        );
        """
    )


def test_scriptable_calendar_sync_handles_empty_and_missing_calendar() -> None:
    """空响应仍清理托管旧事件，目标日历缺失时不宣告成功。"""

    _run_widget_client(
        r"""
        const targetCalendar = { title: 'Pendo', allowsContentModifications: true };
        let calendarAvailable = true;
        let removed = 0;
        globalThis.Calendar = {
            forEventsByTitle: async () => calendarAvailable ? targetCalendar : null,
            forEvents: async () => calendarAvailable ? [targetCalendar] : [],
        };
        globalThis.CalendarEvent = class CalendarEvent {
            static async between() {
                return [{
                    title: '服务端已删除',
                    startDate: new Date('2026-05-01T10:00:00'),
                    notes: '[由 Pendo Widget 同步]\nPendo-ID: deleted-on-server',
                    remove() { removed += 1; },
                }];
            }
        };
        assert.equal(
            (await client.__applyAgendaItemsToCalendar(
                [],
                new Date('2026-05-01T00:00:00'),
                new Date('2026-05-31T00:00:00'),
            )).message,
            '同步完成：新增 0，删除 1',
        );
        assert.equal(removed, 1);

        calendarAvailable = false;
        assert.equal(
            (await client.__applyAgendaItemsToCalendar(
                [{ title: '会议', day: '2026-05-01' }],
                new Date('2026-05-01T00:00:00'),
                new Date('2026-05-31T00:00:00'),
            )).message,
            '未找到名为「Pendo」的日历，请先在系统日历 App 中创建',
        );
        """
    )


def test_scriptable_calendar_cursor_fills_gap_and_advances_after_success() -> None:
    """成功日游标跨运行补齐缺口，并在全部写入后推进到本次运行日。"""

    _run_widget_client(
        r"""
        const cursorKey = 'pendo.calendar-last-success:https://example.com/pendo:Pendo';
        const first = client.__buildCalendarSyncWindow(new Date('2026-07-01T08:00:00'));
        assert.equal(first.startKey, '2026-06-01');
        assert.equal(first.endKey, '2026-07-31');

        Keychain.set(cursorKey, '2026-07-01');
        const resumed = client.__buildCalendarSyncWindow(new Date('2026-08-15T08:00:00'));
        assert.equal(resumed.startKey, '2026-07-01');
        assert.equal(resumed.endKey, '2026-09-14');

        Keychain.set(cursorKey, '2026-08-15');
        const rolling = client.__buildCalendarSyncWindow(new Date('2026-08-15T08:00:00'));
        assert.equal(rolling.startKey, '2026-07-16');
        assert.equal(rolling.endKey, '2026-09-14');

        Keychain.set(cursorKey, '2010-01-01');
        const bounded = client.__buildCalendarSyncWindow(new Date('2026-08-15T08:00:00'));
        const boundedDays = Math.round(
            (bounded.endDate.getTime() - bounded.startDate.getTime()) / (24 * 60 * 60 * 1000),
        ) + 1;
        assert.equal(boundedDays, 3660);
        Keychain.set(cursorKey, '2026-07-01');

        globalThis.__requests = [];
        globalThis.Request = class Request {
            constructor(url) {
                this.url = url;
                this.response = { statusCode: 200 };
                __requests.push(this);
            }
            async loadString() {
                return JSON.stringify({
                    ok: true,
                    data: {
                        start_date: '2026-07-01',
                        end_date: '2026-09-14',
                        items: [{
                            title: '补齐遗漏日程',
                            day: '2026-08-10',
                            start_time: '2026-08-10T10:00:00',
                            end_time: '2026-08-10T11:00:00',
                        }],
                    },
                });
            }
        };
        const targetCalendar = { title: 'Pendo', allowsContentModifications: true };
        globalThis.Calendar = {
            forEventsByTitle: async () => targetCalendar,
            forEvents: async () => [targetCalendar],
        };
        globalThis.__calendarQueries = [];
        globalThis.__createdEvents = [];
        globalThis.CalendarEvent = class CalendarEvent {
            constructor() { __createdEvents.push(this); }
            static async between(start, end) {
                __calendarQueries.push({ start, end });
                return [];
            }
            save() { this.saved = true; }
        };

        const result = await client.__syncCalendarFromServer(
            'widget-token',
            new Date('2026-08-15T08:00:00'),
        );
        assert.equal(result, '同步完成：新增 1');
        assert.ok(__requests[0].url.includes('start_date=2026-07-01'));
        assert.ok(__requests[0].url.includes('end_date=2026-09-14'));
        assert.equal(__createdEvents.length, 1);
        assert.equal(__createdEvents[0].title, '补齐遗漏日程');
        assert.equal(__calendarQueries.length, 1);
        assert.equal(__calendarQueries[0].start.getDate(), 1);
        assert.equal(__calendarQueries[0].end.getDate(), 15);
        assert.equal(Keychain.get(cursorKey), '2026-08-15');
        """
    )


def test_scriptable_calendar_cursor_does_not_advance_when_target_is_missing() -> None:
    """目标日历缺失时保留旧成功日，修复配置后可重试同一缺口。"""

    _run_widget_client(
        r"""
        const cursorKey = 'pendo.calendar-last-success:https://example.com/pendo:Pendo';
        Keychain.set(cursorKey, '2026-07-01');
        globalThis.Request = class Request {
            constructor() { this.response = { statusCode: 200 }; }
            async loadString() {
                return JSON.stringify({
                    ok: true,
                    data: {
                        start_date: '2026-07-01', end_date: '2026-09-14',
                        items: [{ title: '待同步', day: '2026-08-10' }],
                    },
                });
            }
        };
        globalThis.Calendar = {
            forEventsByTitle: async () => null,
            forEvents: async () => [],
        };
        globalThis.CalendarEvent = class CalendarEvent {};

        const result = await client.__syncCalendarFromServer(
            'widget-token',
            new Date('2026-08-15T08:00:00'),
        );
        assert.match(result, /未找到名为/);
        assert.equal(Keychain.get(cursorKey), '2026-07-01');
        """
    )


def test_scriptable_calendar_cursor_does_not_advance_when_update_fails() -> None:
    """已有事件更新失败时保留旧成功日，避免跳过尚未完成的对账。"""

    _run_widget_client(
        r"""
        const cursorKey = 'pendo.calendar-last-success:https://example.com/pendo:Pendo';
        Keychain.set(cursorKey, '2026-07-01');
        globalThis.Request = class Request {
            constructor() { this.response = { statusCode: 200 }; }
            async loadString() {
                return JSON.stringify({
                    ok: true,
                    data: {
                        start_date: '2026-07-01', end_date: '2026-09-14',
                        items: [{
                            id: 'edited-event',
                            title: '修改后的日程',
                            start_time: '2026-08-10T11:00:00',
                            end_time: '2026-08-10T12:00:00',
                        }],
                    },
                });
            }
        };
        const targetCalendar = { title: 'Pendo', allowsContentModifications: true };
        globalThis.Calendar = {
            forEventsByTitle: async () => targetCalendar,
            forEvents: async () => [targetCalendar],
        };
        globalThis.CalendarEvent = class CalendarEvent {
            static async between() {
                return [{
                    title: '修改前的日程',
                    startDate: new Date('2026-08-10T09:00:00'),
                    endDate: new Date('2026-08-10T10:00:00'),
                    isAllDay: false,
                    location: '',
                    notes: '[由 Pendo Widget 同步]\nPendo-ID: edited-event',
                    save() { throw new Error('calendar update failed'); },
                }];
            }
        };

        await assert.rejects(
            client.__syncCalendarFromServer('widget-token', new Date('2026-08-15T08:00:00')),
            /calendar update failed/,
        );
        assert.equal(Keychain.get(cursorKey), '2026-07-01');
        """
    )
