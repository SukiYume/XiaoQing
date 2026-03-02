"""Sony 官方商城签到"""

import logging

from core.plugin_base import segments

logger = logging.getLogger(__name__)

SONY_LOGIN_URL = "https://www.sonystyle.com.cn/eSolverOmniChannel/account/login.do"
SONY_SIGN_URL = "https://www.sonystyle.com.cn/eSolverOmniChannel/account/signupPoints.do"


async def sony_sign(context) -> list[dict]:
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
        async with session.post(SONY_LOGIN_URL, json=data, headers=headers, timeout=20) as resp:
            resp.raise_for_status()
            payload = await resp.json()
        result_data = payload.get("resultData")
        if not isinstance(result_data, dict):
            return segments("❌ Sony 登录响应异常")
        token = result_data.get("access_token")
        if not token:
            return segments("❌ Sony 登录失败: 未获取到 token")

        headers["Authorization"] = f"Bearer {token}"
        async with session.post(SONY_SIGN_URL, headers=headers, timeout=20) as resp:
            resp.raise_for_status()
            sign_payload = await resp.json()
        msg = sign_payload.get("resultMsg", [{}])[0].get("message", "签到完成")
        return segments(f"✅ Sony 签到: {msg}")
    except Exception as exc:
        logger.exception("Sony sign failed: %s", exc)
        return segments(f"❌ Sony 签到失败: {exc}")
