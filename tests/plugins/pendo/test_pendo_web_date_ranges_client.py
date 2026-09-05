"""Pendo Web 日期范围工具的自然周期和数据边界回归。"""

from __future__ import annotations

from typing import Final

from tests.helpers.node_esm import assert_node_esm_contract
from tests.helpers.paths import REPOSITORY_ROOT

ROOT: Final               = REPOSITORY_ROOT
DATE_RANGES_CLIENT: Final = (
    ROOT / "plugins" / "pendo" / "web" / "static" / "js" / "utils" / "date_ranges.js"
)

DATE_RANGES_SETUP: Final = r"""
    const pad2 = (value) => String(value).padStart(2, '0');
    globalThis.__format = {
        todayInUserTimeZone: () => '2024-02-29',
        isoDate: (date) => (
            `${date.getFullYear()}-${pad2(date.getMonth() + 1)}-${pad2(date.getDate())}`
        ),
    };
"""


def _date_ranges_source_for_test() -> str:
    """只替换相邻格式化依赖，保留真实日期范围实现。"""

    source = DATE_RANGES_CLIENT.read_text(encoding="utf-8")
    format_import   = "import { isoDate } from './format.js';"
    timezone_import = "import { todayInUserTimeZone } from './timezone.js';"
    assert format_import in source
    assert timezone_import in source
    return source.replace(
        format_import,
        "const { isoDate } = globalThis.__format;",
    ).replace(
        timezone_import,
        "const { todayInUserTimeZone } = globalThis.__format;",
    )


def _run_date_ranges_client(script: str) -> None:
    """在固定本地日期的 Node 环境中执行真实 ESM 工具。"""

    assert_node_esm_contract(
        _date_ranges_source_for_test(),
        script,
        cwd   = ROOT,
        setup = DATE_RANGES_SETUP,
    )


def test_date_range_presets_cover_complete_natural_periods() -> None:
    """周、月、季度、年份和去年预设应覆盖完整自然周期。"""

    _run_date_ranges_client(
        r"""
        assert.equal(client.todayRangeKey(), '2024-02-29');
        assert.deepEqual(
            client.RANGE_PRESET_OPTIONS.map(({ key }) => key),
            ['week', 'month', 'quarter', 'year', 'last_year', 'custom', 'all'],
        );
        assert.deepEqual(client.derivePresetRange('week', { today: '2024-02-29' }), {
            start: '2024-02-26', end: '2024-03-03',
        });
        assert.deepEqual(client.derivePresetRange('week', { today: '2024-01-01' }), {
            start: '2024-01-01', end: '2024-01-07',
        });
        assert.deepEqual(client.derivePresetRange('month', { today: '2024-02-29' }), {
            start: '2024-02-01', end: '2024-02-29',
        });
        assert.deepEqual(client.derivePresetRange('quarter', { today: '2024-02-29' }), {
            start: '2024-01-01', end: '2024-03-31',
        });
        assert.deepEqual(client.derivePresetRange('year', { today: '2024-02-29' }), {
            start: '2024-01-01', end: '2024-12-31',
        });
        assert.deepEqual(client.derivePresetRange('last_year', { today: '2024-02-29' }), {
            start: '2023-01-01', end: '2023-12-31',
        });
        """
    )


def test_date_range_custom_and_fallback_inputs_are_normalized() -> None:
    """自定义、全部和未知预设不能把畸形日期带入后续字典序比较。"""

    _run_date_ranges_client(
        r"""
        assert.deepEqual(client.derivePresetRange('custom', {
            today: '2024-02-29',
            customStart: ' 2024-01-02T08:30:00 ',
            customEnd: '2024-01-31',
        }), { start: '2024-01-02', end: '2024-01-31' });

        assert.deepEqual(client.derivePresetRange('custom', {
            today: '2024-02-29',
            customStart: '2024-02-30',
            customEnd: '2024-03-10',
            customFallback: 'month',
        }), { start: '2024-02-01', end: '2024-02-29' });
        assert.deepEqual(client.derivePresetRange('custom', {
            today: '2024-02-29',
            customStart: 'invalid',
            customEnd: '2024-03-10',
            customFallback: '',
        }), { start: '', end: '' });

        assert.deepEqual(client.derivePresetRange('all', {
            today: 'not-a-date',
            allStart: 'also-invalid',
            allEnd: '2024-12-31T23:59:59',
        }), { start: '1970-01-01', end: '2024-12-31' });
        assert.deepEqual(client.derivePresetRange('unknown', { today: 'not-a-date' }), {
            start: '2024-02-29', end: '2024-02-29',
        });
        """
    )


def test_item_range_bounds_normalize_responses_and_never_reverse_the_range() -> None:
    """首尾查询应保持固定请求形状，并对空值、损坏值和最小结束日安全降级。"""

    _run_date_ranges_client(
        r"""
        const calls = [];
        const api = {
            async get(path, params) {
                calls.push({ path, params });
                if (params.order === 'asc') {
                    return { data: { items: [{ start_time: ' 2022-05-01T08:00:00 ' }] } };
                }
                return { data: { items: [{
                    start_time: '2024-03-02T08:00:00',
                    end_time: '2024-03-03T10:00:00',
                }] } };
            },
        };

        assert.deepEqual(await client.fetchItemRangeBounds(api, {
            type: 'event',
            sortField: 'start_time',
            startField: 'start_time',
            endField: 'end_time',
            fallbackEnd: 'invalid',
            minimumEnd: '2025-01-01',
        }), { start: '2022-05-01', end: '2025-01-01' });
        assert.deepEqual(calls, [
            {
                path: '/items',
                params: {
                    type: 'event', sort: 'start_time', order: 'asc', page: 1, page_size: 1,
                },
            },
            {
                path: '/items',
                params: {
                    type: 'event', sort: 'start_time', order: 'desc', page: 1, page_size: 1,
                },
            },
        ]);

        const corruptApi = {
            async get(_path, params) {
                if (params.order === 'asc') {
                    return { data: { items: [{ created_at: '2030-01-01' }] } };
                }
                return { data: { items: [{ created_at: 42 }] } };
            },
        };
        assert.deepEqual(await client.fetchItemRangeBounds(corruptApi, {
            type: 'note', sortField: 'created_at', fallbackEnd: 'bad',
        }), { start: '2030-01-01', end: '2030-01-01' });

        const emptyApi = { async get() { return { data: { items: [] } }; } };
        assert.deepEqual(await client.fetchItemRangeBounds(emptyApi, {
            type: 'diary', sortField: 'diary_date', fallbackEnd: '',
        }), { start: '2024-02-29', end: '2024-02-29' });
        """
    )
