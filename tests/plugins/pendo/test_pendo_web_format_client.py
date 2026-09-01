"""Pendo Web 日期、预览文本和金额格式化工具回归。"""

from __future__ import annotations

from typing import Final

from tests.helpers.node_esm import assert_node_esm_contract
from tests.helpers.paths import REPOSITORY_ROOT

ROOT: Final = REPOSITORY_ROOT
FORMAT_CLIENT: Final = ROOT / "plugins" / "pendo" / "web" / "static" / "js" / "utils" / "format.js"

FIXED_DATE_SETUP: Final = r"""
    const NativeDate = globalThis.Date;
    class FixedDate extends NativeDate {
        constructor(...args) {
            if (args.length === 0) super(2024, 1, 29, 12, 34, 56, 0);
            else super(...args);
        }
        static now() { return new NativeDate(2024, 1, 29, 12, 34, 56, 0).getTime(); }
    }
    globalThis.Date = FixedDate;
"""


def _run_format_client(script: str) -> None:
    """在固定本地时间的 Node 环境中导入真实格式化模块。"""

    assert_node_esm_contract(
        FORMAT_CLIENT.read_text(encoding="utf-8"),
        script,
        cwd=ROOT,
        setup=FIXED_DATE_SETUP,
    )


def test_format_client_parses_local_dates_without_accepting_impossible_days() -> None:
    """纯日期保持本地语义，不存在的日期和不支持的输入类型应明确失败。"""

    _run_format_client(
        r"""
        assert.equal(client.pad2(3), '03');
        const leapDay = client.parseDate(' 2024-02-29 ');
        assert.ok(leapDay instanceof Date);
        assert.deepEqual(
            [leapDay.getFullYear(), leapDay.getMonth(), leapDay.getDate(), leapDay.getHours()],
            [2024, 1, 29, 0],
        );
        assert.equal(client.parseDate('2024-02-30'), null);
        assert.equal(client.parseDate(''), null);
        assert.equal(client.parseDate(false), null);
        assert.equal(client.parseDate({}), null);
        assert.equal(client.parseDate(Number.NaN), null);

        const epoch = client.parseDate(0);
        assert.equal(epoch.getTime(), 0);
        const original = new Date(2024, 4, 6, 7, 8, 9, 123);
        const cloned = client.parseDate(original);
        assert.notEqual(cloned, original);
        assert.equal(cloned.getTime(), original.getTime());

        assert.equal(client.isoDate(new Date(2024, 1, 29)), '2024-02-29');
        assert.equal(client.isoDate(new Date('invalid')), '');
        assert.equal(client.isoDate('2024-02-29'), '');
        assert.equal(client.isValidDateInput(' 2024-02-29 '), true);
        assert.equal(client.isValidDateInput('2023-02-29'), false);
        assert.equal(client.isValidDateInput('2024-2-9'), false);
        """
    )


def test_format_client_keeps_timestamp_display_out_and_unicode_previews_safe() -> None:
    """通用格式模块不提供浏览器时区展示接口，预览文本保持 Unicode 完整。"""

    _run_format_client(
        r"""
        assert.equal('todayStr' in client, false);
        assert.equal('formatMonthDay' in client, false);
        assert.equal('formatDateTime' in client, false);

        assert.equal(client.previewText('  简短内容  ', 10), '简短内容');
        assert.equal(client.previewText('😀甲乙', 2), '😀甲...');
        assert.equal(client.previewText(0, 2), '0');
        assert.equal(client.previewText('abc', 0), '...');
        assert.equal(client.previewText('abc', -1), 'abc');
        assert.equal(client.previewText(null), '');
        """
    )


def test_format_client_never_renders_non_finite_money_values() -> None:
    """损坏数值应降级为零，正常金额继续保留原有完整与紧凑格式。"""

    _run_format_client(
        r"""
        assert.equal(client.formatAmount(1234.5), '¥1,234.50');
        assert.equal(client.formatAmount(-12), '-¥12.00');
        assert.equal(client.formatAmount('bad'), '¥0.00');
        assert.equal(client.formatAmount(Number.POSITIVE_INFINITY), '¥0.00');
        assert.equal(client.formatAmount(-0), '¥0.00');
        assert.equal(client.formatAmount(Symbol('bad')), '¥0.00');

        assert.equal(client.formatMoneyCompact(999), '¥999');
        assert.equal(client.formatMoneyCompact(1500), '¥1.5k');
        assert.equal(client.formatMoneyCompact(12500), '¥1.3万');
        assert.equal(client.formatMoneyCompact(-15000), '-¥1.5万');
        assert.equal(client.formatMoneyCompact('bad'), '¥0');
        """
    )


def test_format_client_normalizes_shared_api_boundary_values() -> None:
    """页面共用的数据守卫应统一处理畸形记录、数值、文本和错误。"""

    _run_format_client(
        r"""
        assert.equal(client.isRecord({ key: 1 }), true);
        assert.equal(client.isRecord([]), false);
        assert.deepEqual(client.records([{}, null, [], { id: 1 }]), [{}, { id: 1 }]);

        assert.equal(client.finiteNumber('3.5'), 3.5);
        assert.equal(client.finiteNumber(Symbol('bad'), 7), 7);
        assert.equal(Object.is(client.finiteNumber(-0), -0), false);
        assert.equal(client.nonNegativeInteger(3.9), 3);
        assert.equal(client.nonNegativeInteger(-2), 0);

        assert.equal(client.textValue(12), '12');
        assert.equal(client.textValue({}), '');
        assert.equal(client.trimmedTextValue('  标题  '), '标题');
        assert.equal(client.trimmedTextValue(12, 'fallback'), 'fallback');
        assert.equal(client.nonEmptyTextValue('   ', '默认'), '默认');

        assert.equal(client.errorMessage({ message: '  失败原因  ' }), '失败原因');
        assert.equal(client.errorMessage('  直接失败  '), '直接失败');
        assert.equal(client.errorMessage({}, '兜底'), '兜底');
        """
    )
