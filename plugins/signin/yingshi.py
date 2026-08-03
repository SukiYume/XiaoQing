"""通过有赞接口执行影视飓风签到。"""

import json
import logging
import time
from collections.abc import Mapping
from typing import Any, cast

from core.bounded_http import (
    JSON_MIME_POLICY,
    BodyLimits,
    JsonLimits,
    ResponseFormatError,
    aiohttp_request_bounded,
    parse_bounded_json,
)
from core.plugin_base import bounded_external_text
from core.public_errors import public_error_response

from .types import Context, MessageSegments, segments

logger = logging.getLogger(__name__)

BASE_URL = "https://h5.youzan.com"
_RESPONSE_LIMITS = BodyLimits(
    max_wire_bytes=512 * 1024,
    max_decoded_bytes=1024 * 1024,
    max_decompression_ratio=20,
)
_JSON_LIMITS = JsonLimits(
    max_bytes=1024 * 1024,
    max_depth=24,
    max_nodes=10_000,
    max_string_chars=256_000,
)
_SUCCESS_STATUSES = range(200, 300)
_FIELD_CHAR_LIMIT = 256
_MAX_REWARD_LINES = 20
# These values mirror the current official WeChat client request observed for
# the YouZan endpoint.  If the endpoint starts rejecting otherwise valid
# credentials, update this pair together with the upstream client contract.
_CLIENT_VERSION = "2.210.8.101"
_UUID_PREFIX = "xncgEoy8XBh9siy"


def _get_config(context: Context) -> Mapping[str, Any]:
    """从当前原子设置代取得影视飓风配置。"""

    signin = context.get_settings_snapshot().plugin_secrets("signin")
    config = signin.get("yingshijufeng")
    return config if isinstance(config, Mapping) else {}


def _safe_text(value: object, default: str = "") -> str:
    """按签到字段预算调用统一的第三方文本边界。"""

    return bounded_external_text(
        value,
        max_chars=_FIELD_CHAR_LIMIT,
        max_bytes=_FIELD_CHAR_LIMIT * 4,
        default=default,
    )


def _credential_text(value: object) -> str | None:
    """只接受非空字符串或整数凭据字段。"""

    if isinstance(value, bool) or not isinstance(value, (str, int)):
        return None
    text = str(value).strip()
    return text or None


def _build_headers(app_id: str) -> dict[str, str]:
    return {
        "content-type": "application/json",
        "User-Agent": (
            "Mozilla/5.0 (iPhone; CPU iPhone OS 18_7_1 like Mac OS X) "
            "AppleWebKit/605.1.15 (KHTML, like Gecko) Mobile/15E148 "
            "MicroMessenger/8.0.64(0x1800402b) NetType/WIFI Language/zh_CN"
        ),
        "Referer": f"https://servicewechat.com/{app_id}/16/page-frame.html",
    }


def _build_extra_data(sid: str) -> dict[str, str | int]:
    timestamp = int(time.time() * 1000)
    return {
        "is_weapp": 1,
        "sid": sid,
        "version": _CLIENT_VERSION,
        "client": "weapp",
        "bizEnv": "wsc",
        "uuid": f"{_UUID_PREFIX}{timestamp}",
        "ftime": timestamp,
    }


async def _request_json(
    session: Any,
    path: str,
    *,
    params: Mapping[str, str],
    access_token: str,
    headers: Mapping[str, str],
) -> dict[str, Any]:
    """使用统一的有界传输和 JSON 结构契约请求接口。"""

    request_headers = {**headers, "Authorization": f"Bearer {access_token}"}
    response = await aiohttp_request_bounded(
        session,
        "GET",
        f"{BASE_URL}{path}",
        limits=_RESPONSE_LIMITS,
        mime_policy=JSON_MIME_POLICY,
        success_statuses=_SUCCESS_STATUSES,
        headers=request_headers,
        request_kwargs={"params": dict(params), "timeout": 20},
    )
    payload = parse_bounded_json(response, limits=_JSON_LIMITS)
    if not isinstance(payload, dict):
        raise ResponseFormatError("YouZan response must be a JSON object")
    return payload


async def _get_checkin_id(
    session: Any,
    app_id: str,
    kdt_id: str,
    access_token: str,
    headers: Mapping[str, str],
) -> tuple[bool, str, str]:
    payload = await _request_json(
        session,
        "/wscump/checkin/check-in-info.json",
        params={"app_id": app_id, "kdt_id": kdt_id},
        access_token=access_token,
        headers=headers,
    )
    if payload.get("code") != 0:
        return False, "", _safe_text(payload.get("msg"), "获取签到信息失败")

    data = payload.get("data")
    if not isinstance(data, Mapping):
        return False, "", "获取签到信息失败"
    checkin_id = data.get("checkInId")
    if isinstance(checkin_id, bool) or not isinstance(checkin_id, (str, int)):
        return False, "", "获取签到信息失败"
    normalized_id = str(checkin_id).strip()
    if not normalized_id:
        return False, "", "获取签到信息失败"
    return True, normalized_id, "获取签到信息成功"


async def _do_checkin(
    session: Any,
    checkin_id: str,
    app_id: str,
    kdt_id: str,
    access_token: str,
    headers: Mapping[str, str],
) -> tuple[bool, str]:
    payload = await _request_json(
        session,
        "/wscump/checkin/checkinV2.json",
        params={"checkinId": checkin_id, "app_id": app_id, "kdt_id": kdt_id},
        access_token=access_token,
        headers=headers,
    )
    if payload.get("code") != 0:
        return False, _safe_text(payload.get("msg"), "签到失败")

    data = payload.get("data")
    if not isinstance(data, Mapping):
        return False, "签到响应格式异常"

    description = _safe_text(data.get("desc"))
    total = _safe_text(data.get("times"), "0")
    lines = [f"签到成功！{description}", f"📅 累计签到: {total} 次"]
    rewards = data.get("list")
    if isinstance(rewards, list):
        reward_count = 0
        for item in rewards:
            if not isinstance(item, Mapping) or item.get("isSuccess") is not True:
                continue
            infos = item.get("infos")
            if not isinstance(infos, Mapping):
                infos = {}
            reward_day = _safe_text(item.get("times"))
            title = _safe_text(infos.get("title"))
            detail = _safe_text(infos.get("desc"))
            if not any((reward_day, title, detail)):
                continue
            reward_line = f"🎁 奖励{f' {reward_day}' if reward_day else ''}"
            if title:
                reward_line += f": {title}"
            if detail:
                reward_line += f"{' - ' if title else ': '}{detail}"
            lines.append(reward_line)
            reward_count += 1
            if reward_count == _MAX_REWARD_LINES:
                break
    return True, "\n".join(lines)


async def yingshi_sign(context: Context) -> MessageSegments:
    """读取部署凭据并完成查询、签到两个远端步骤。"""

    config = _get_config(context)
    app_id = _credential_text(config.get("app_id"))
    kdt_id = _credential_text(config.get("kdt_id"))
    access_token = _credential_text(config.get("access_token"))
    sid = _credential_text(config.get("sid"))
    if app_id is None or kdt_id is None or access_token is None or sid is None:
        return segments("❌ 影视签到未配置")

    session = context.http_session
    if session is None:
        return segments("❌ HTTP 会话未初始化")

    try:
        headers = _build_headers(app_id)
        headers["Extra-Data"] = json.dumps(
            _build_extra_data(sid),
            ensure_ascii=False,
            separators=(",", ":"),
        )
        ok, checkin_id, message = await _get_checkin_id(
            session,
            app_id,
            kdt_id,
            access_token,
            headers,
        )
        if not ok:
            return segments(f"❌ 影视签到\n{message}")

        ok, message = await _do_checkin(
            session,
            checkin_id,
            app_id,
            kdt_id,
            access_token,
            headers,
        )
        prefix = "✅" if ok else "❌"
        return segments(f"{prefix} 影视签到\n{message}")
    except Exception as exc:
        return cast(
            MessageSegments,
            public_error_response(
                context,
                exc,
                logger=logger,
                component="signin.yingshi",
            ),
        )
