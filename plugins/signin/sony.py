"""Sony 官方商城签到 (Deprecated)"""

import logging
import warnings

from core.bounded_http import (
    JSON_MIME_POLICY,
    BodyLimits,
    JsonLimits,
    ResponseFormatError,
    aiohttp_request_bounded,
    parse_bounded_json,
)
from core.plugin_base import segments
from core.public_errors import public_error_response

logger = logging.getLogger(__name__)

SONY_LOGIN_URL = "https://www.sonystyle.com.cn/eSolverOmniChannel/account/login.do"
SONY_SIGN_URL = "https://www.sonystyle.com.cn/eSolverOmniChannel/account/signupPoints.do"
_RESPONSE_LIMITS = BodyLimits(
    max_wire_bytes=512 * 1024,
    max_decoded_bytes=1024 * 1024,
    max_decompression_ratio=20,
)
_JSON_LIMITS = JsonLimits(max_bytes=1024 * 1024, max_depth=24, max_nodes=10_000)
_SUCCESS_STATUSES = range(200, 300)


async def _request_json(session, method: str, url: str, **request_kwargs) -> dict:
    response = await aiohttp_request_bounded(
        session,
        method,
        url,
        limits=_RESPONSE_LIMITS,
        mime_policy=JSON_MIME_POLICY,
        success_statuses=_SUCCESS_STATUSES,
        headers=request_kwargs.pop("headers", None),
        request_kwargs=request_kwargs,
    )
    payload = parse_bounded_json(response, limits=_JSON_LIMITS)
    if not isinstance(payload, dict):
        raise ResponseFormatError("Sony response must be a JSON object")
    return payload


async def sony_sign(context) -> list[dict]:
    warnings.warn(
        "sony_sign is deprecated and no longer maintained.",
        DeprecationWarning,
        stacklevel=2,
    )

    config = context.secrets.get("plugins", {}).get("signin", {})
    creds = config.get("sony", {})
    login_id = creds.get("login_id")
    password = creds.get("password")

    session = context.http_session
    if not session:
        return segments("❌ HTTP 会话未初始化")

    if not login_id or not password:
        return segments("❌ Sony 签到未配置")

    try:
        data = {"channel": "WEB", "loginID": login_id, "password": password}
        headers = {"User-Agent": "Mozilla/5.0"}
        payload = await _request_json(
            session,
            "POST",
            SONY_LOGIN_URL,
            json=data,
            headers=headers,
            timeout=20,
        )
        result_data = payload.get("resultData")
        if not isinstance(result_data, dict):
            return segments("❌ Sony 登录响应异常")
        token = result_data.get("access_token")
        if not token:
            return segments("❌ Sony 登录失败: 未获取到 token")

        headers["Authorization"] = f"Bearer {token}"
        sign_payload = await _request_json(
            session,
            "POST",
            SONY_SIGN_URL,
            headers=headers,
            timeout=20,
        )
        result_messages = sign_payload.get("resultMsg")
        if (
            isinstance(result_messages, list)
            and result_messages
            and isinstance(result_messages[0], dict)
        ):
            msg = str(result_messages[0].get("message") or "签到完成")
        else:
            msg = "签到完成"
        return segments(f"✅ Sony 签到: {msg}")
    except Exception as exc:
        return public_error_response(
            context,
            exc,
            logger=logger,
            component="signin.sony",
        )
