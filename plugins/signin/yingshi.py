"""影视飓风签到 (有赞/YouZan 平台)"""

import json
import logging
import time

from core.plugin_base import segments

logger = logging.getLogger(__name__)

BASE_URL = "https://h5.youzan.com"


def _get_config(context) -> dict:
    return context.secrets.get("plugins", {}).get("signin", {}).get("yingshijufeng", {})


def _build_headers(app_id: str) -> dict:
    return {
        "Host": "h5.youzan.com",
        "Connection": "keep-alive",
        "content-type": "application/json",
        "Accept-Encoding": "gzip,compress,br,deflate",
        "User-Agent": (
            "Mozilla/5.0 (iPhone; CPU iPhone OS 18_7_1 like Mac OS X) "
            "AppleWebKit/605.1.15 (KHTML, like Gecko) Mobile/15E148 "
            "MicroMessenger/8.0.64(0x1800402b) NetType/WIFI Language/zh_CN"
        ),
        "Referer": f"https://servicewechat.com/{app_id}/16/page-frame.html",
    }


def _build_extra_data(sid: str) -> dict:
    timestamp = int(time.time() * 1000)
    return {
        "is_weapp": 1,
        "sid": sid,
        "version": "2.210.8.101",
        "client": "weapp",
        "bizEnv": "wsc",
        "uuid": f"xncgEoy8XBh9siy{timestamp}",
        "ftime": timestamp,
    }


async def _get_checkin_id(session, app_id, kdt_id, access_token, headers):
    url = f"{BASE_URL}/wscump/checkin/check-in-info.json"
    payload_body = {"app_id": app_id, "kdt_id": kdt_id, "access_token": access_token}
    async with session.get(url, params=payload_body, headers=headers, timeout=20) as resp:
        resp.raise_for_status()
        payload = await resp.json()
    if payload.get("code") != 0:
        return False, "", payload.get("msg", "获取签到信息失败")
    return True, payload["data"]["checkInId"], "获取签到信息成功"


async def _do_checkin(session, checkin_id, app_id, kdt_id, access_token, headers):
    url = f"{BASE_URL}/wscump/checkin/checkinV2.json"
    payload_body = {
        "checkinId": checkin_id,
        "app_id": app_id,
        "kdt_id": kdt_id,
        "access_token": access_token,
    }
    async with session.get(url, params=payload_body, headers=headers, timeout=20) as resp:
        resp.raise_for_status()
        payload = await resp.json()
    if payload.get("code") != 0:
        return False, payload.get("msg", "签到失败")

    data = payload.get("data", {})
    msg = f"签到成功！{data.get('desc', '')}\n"
    msg += f"📅 累计签到: {data.get('times', 0)} 次"
    for item in data.get("list", []):
        if item.get("isSuccess"):
            infos = item.get("infos", {})
            msg += f"\n🎁 奖励 {item.get('times', '')}: {infos.get('title', '')} - {infos.get('desc', '')}"
    return True, msg


async def yingshi_sign(context) -> list[dict]:
    """影视飓风签到"""
    data = _get_config(context)
    app_id = data.get("app_id")
    kdt_id = data.get("kdt_id")
    access_token = data.get("access_token")
    sid = data.get("sid")

    if not all([app_id, kdt_id, access_token, sid]):
        return segments("❌ 影视签到未配置")

    app_id = str(app_id)
    kdt_id = str(kdt_id)
    access_token = str(access_token)
    sid = str(sid)

    session = context.http_session
    if not session:
        return segments("❌ HTTP 会话未初始化")

    try:
        headers = _build_headers(app_id)
        headers["Extra-Data"] = json.dumps(_build_extra_data(sid))

        ok, checkin_id, msg = await _get_checkin_id(
            session, app_id, kdt_id, access_token, headers
        )
        if not ok:
            return segments(f"❌ 影视签到\n{msg}")

        ok, msg = await _do_checkin(
            session, checkin_id, app_id, kdt_id, access_token, headers
        )
        prefix = "✅" if ok else "❌"
        return segments(f"{prefix} 影视签到\n{msg}")
    except Exception as exc:
        logger.exception("Yingshi sign failed: %s", exc)
        return segments(f"❌ 影视签到异常: {exc}")
