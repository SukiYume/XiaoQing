"""Pendo Web 用户时区读取与墙钟转换回归。"""

from typing import Final

from tests.helpers.node_esm import assert_node_esm_contract
from tests.helpers.paths import REPOSITORY_ROOT

ROOT: Final = REPOSITORY_ROOT
TIMEZONE_CLIENT: Final = (
    ROOT / "plugins" / "pendo" / "web" / "static" / "js" / "utils" / "timezone.js"
)


def _run_timezone_client(script: str) -> None:
    source = TIMEZONE_CLIENT.read_text(encoding="utf-8").replace(
        "import { api } from '../api.js';",
        "const api = globalThis.__api;",
    )
    assert_node_esm_contract(
        source,
        script,
        cwd=ROOT,
        setup=(
            "process.env.TZ = 'America/Los_Angeles';\n"
            "globalThis.__api = { get: async () => "
            "({ data: { timezone: 'Asia/Shanghai' } }) };"
        ),
    )


def test_timezone_client_round_trips_in_configured_zone_not_browser_zone() -> None:
    """装载与回写必须成对使用显式 IANA 时区，并把存储值统一为 UTC。"""

    _run_timezone_client(
        r"""
        assert.equal(
            client.zonedDateTimeToInput('2026-05-01T10:00:00+00:00', 'Asia/Shanghai'),
            '2026-05-01T18:00',
        );
        assert.equal(
            client.zonedDateTimeToInput('2026-05-01T10:00:00', 'America/New_York'),
            '2026-05-01T10:00',
        );
        assert.equal(
            client.zonedInputToUtcIso('2026-05-01T18:00', 'Asia/Shanghai'),
            '2026-05-01T10:00:00+00:00',
        );
        assert.equal(client.zonedInputToUtcIso('2026-02-30T18:00', 'Asia/Shanghai'), '');
        assert.equal(await client.fetchUserTimeZone(), 'Asia/Shanghai');
        assert.equal(client.getUserTimeZone(), 'Asia/Shanghai');
        assert.equal(
            client.formatZonedDateTime('2026-05-01T10:00:00+00:00'),
            '2026-05-01 18:00',
        );
        assert.equal(
            client.formatZonedDateTime('2026-05-01T18:00:00'),
            '2026-05-01 18:00',
        );
        assert.equal(client.formatZonedDateTime('2026-05-01'), '2026-05-01');
        assert.equal(
            client.todayInUserTimeZone(new Date('2026-05-01T16:30:00Z')),
            '2026-05-02',
        );
        assert.equal(
            client.zonedInstantEpoch('2026-05-01T18:00:00'),
            Date.parse('2026-05-01T10:00:00Z'),
        );
        assert.equal(
            client.zonedInstantEpoch('2026-05-01T18:00:00.123456'),
            Date.parse('2026-05-01T10:00:00.123Z'),
        );
        """
    )


def test_timezone_client_rejects_dst_gaps_folds_and_invalid_settings() -> None:
    """不存在或歧义墙钟不得交给浏览器猜测，非法设置也不得静默回退。"""

    _run_timezone_client(
        r"""
        assert.throws(
            () => client.zonedInputToUtcIso('2026-03-08T02:30', 'America/New_York'),
            /不存在/,
        );
        assert.throws(
            () => client.zonedInputToUtcIso('2026-11-01T01:30', 'America/New_York'),
            /两个时刻/,
        );
        assert.throws(
            () => client.zonedInputToUtcIso('2026-05-01T10:00', 'Not/A_Zone'),
            /无效的用户时区/,
        );
        __api.get = async () => ({ data: { timezone: 'Not/A_Zone' } });
        await assert.rejects(client.fetchUserTimeZone(), /无效的用户时区/);
        """
    )
