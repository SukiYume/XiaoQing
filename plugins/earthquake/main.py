"""
地震快讯插件

从微博中国地震台网获取地震快讯。
仅推送 4 级及以上地震。
"""
import logging
import re
import uuid
from typing import Any, Optional

import requests

from core.plugin_base import atomic_write_bytes, build_action, image, load_json, run_sync, segments, text, write_json
from core.args import parse

logger = logging.getLogger(__name__)

# ============================================================
# 常量
# ============================================================

WEIBO_UID = "1904228041"
CONTAINER_ID = "1076031904228041"

# ============================================================
# 插件初始化
# ============================================================

def init(context=None) -> None:
    """插件初始化"""
    pass

# ============================================================
# 状态管理
# ============================================================

def _since_path(context):
    """获取状态文件路径"""
    return context.data_dir / "earthquake.json"

def _load_since(context) -> str:
    """加载上次处理的微博 ID"""
    path = _since_path(context)
    path.parent.mkdir(parents=True, exist_ok=True)
    if not path.exists():
        write_json(path, {"since_id": "0"})
        return "0"
    try:
        data = load_json(path, {}, raise_on_error=True)
    except (ValueError, OSError, TypeError) as exc:
        logger.warning("Invalid earthquake state file %s, resetting: %s", path, exc)
        write_json(path, {"since_id": "0"})
        return "0"
    return str(data.get("since_id", "0") or "0")

def _save_since(context, since_id: str) -> None:
    """保存最新处理的微博 ID"""
    path = _since_path(context)
    path.parent.mkdir(parents=True, exist_ok=True)
    write_json(path, {"since_id": since_id})

# ============================================================
# 微博 API
# ============================================================

def _create_session() -> requests.Session:
    """创建微博访客会话"""
    session = requests.Session()

    # 生成 request_id
    request_id = str(uuid.uuid4()).replace("-", "")

    # 获取访客 Cookie
    visitor_url = "https://visitor.passport.weibo.cn/visitor/genvisitor2"
    visitor_data = {
        "cb": "visitor_gray_callback",
        "ver": "20250916",
        "request_id": request_id,
        "tid": "",
        "from": "weibo",
        "webdriver": "false",
        "rid": "01Cn_5z8ew6CZHvNiTdPeyK2Qf740",
        "return_url": f"https://m.weibo.cn/u/{WEIBO_UID}",
    }
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
        "Referer": "https://m.weibo.cn/",
    }

    try:
        session.post(visitor_url, data=visitor_data, headers=headers, timeout=20)

        # 获取配置（包含 XSRF-TOKEN 等）
        config_url = "https://m.weibo.cn/api/config"
        session.get(config_url, headers=headers, timeout=20)
    except requests.RequestException as exc:
        logger.warning("Failed to bootstrap weibo visitor session: %s", exc)

    return session

def _fetch_weibo(session: requests.Session) -> dict[str, Any]:
    """获取微博列表"""
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
        "Referer": f"https://m.weibo.cn/u/{WEIBO_UID}",
        "X-Requested-With": "XMLHttpRequest",
    }
    params = {
        "type": "uid",
        "value": WEIBO_UID,
        "containerid": CONTAINER_ID,
    }
    response = session.get(
        "https://m.weibo.cn/api/container/getIndex",
        params=params,
        headers=headers,
        timeout=20,
    )
    return response.json()

def _extract_magnitude(text: str) -> Optional[float]:
    """从文本中提取震级"""
    match = re.search(r"发生(\d+\.?\d*)级地震", text)
    if match:
        return float(match.group(1))
    return None

def _extract_clean_text(raw_text: str) -> str:
    """提取纯净的地震信息文本"""
    # 清理空白字符
    clean = re.sub(r"\s+\u200b+", "", raw_text)
    # 提取核心内容
    match = re.search(r"</a>(.+)（ <a href=", clean)
    if match:
        return match.group(1)
    return clean

# ============================================================
# 主处理函数
# ============================================================

async def handle(command: str, args: str, event: dict, context) -> list:
    """命令处理入口"""
    try:
        parsed = parse(args)

        # help/帮助 仍然显示帮助；空参数则默认获取最新地震快讯
        if parsed and parsed.first and parsed.first.lower() in ["help", "帮助"]:
            return segments(_show_help())
        
        subcommand = parsed.first.lower() if parsed and parsed.first else None

        # 命令路由
        if subcommand in {"latest", "最新"}:
            return await _fetch_earthquake_news(context, force=True)

        # 默认行为：获取最新地震快讯
        return await _fetch_earthquake_news(context, force=True)
        
    except Exception as e:
        logger.exception("Earthquake handle error: %s", e)
        return segments(f"处理请求时出错: {str(e)}")

def _show_help() -> str:
    """显示帮助信息"""
    return """
🌏 **地震快讯**

从微博中国地震台网获取地震快讯信息。

**命令:**
• /earthquake 或 /地震 - 获取最新地震快讯
• /earthquake latest - 获取最新地震快讯
• /earthquake help - 显示此帮助信息

**说明:**
• 定时任务仅推送 4 级及以上地震
• 手动查询显示最新地震信息（不限震级）
• 数据来源: 中国地震台网官方微博

输入 /earthquake help 查看此帮助
""".strip()

async def scheduled(context) -> list:
    """定时任务入口"""
    result = await _fetch_earthquake_news(context, force=False, advance_cursor=False)
    pending_since = getattr(context, "state", {}).pop("earthquake_pending_since", None)
    payload = segments(result)
    if not payload:
        if pending_since:
            _save_since(context, pending_since)
        return []
    targets = list(context.default_groups()) if hasattr(context, "default_groups") else []
    if not targets:
        logger.warning("Earthquake notification has no configured target group; cursor retained")
        return []
    delivered = True
    for group_id in targets:
        action = build_action(payload, None, int(group_id))
        delivered = bool(action) and bool(await context.send_action(action)) and delivered
    if delivered and pending_since:
        _save_since(context, pending_since)
    return []

async def _fetch_earthquake_news(
    context,
    force: bool = False,
    *,
    advance_cursor: bool = True,
) -> list:
    """
    获取地震快讯

    Args:
        context: 插件上下文
        force: 是否强制返回（手动触发模式）
    """
    since_id = _load_since(context)
    since_id_int = int(since_id)

    def _do_fetch() -> list:
        session = _create_session()
        data = _fetch_weibo(session)

        found_card = None
        found_cards = []
        newest_seen_id = since_id_int

        for card in data.get("data", {}).get("cards", []):
            mblog = card.get("mblog", {})
            raw_text = mblog.get("text", "")

            # 检查是否是地震快讯
            if "#地震快讯#" not in raw_text or "中国地震台网正式测定" not in raw_text:
                continue

            mid = str(mblog.get("id", ""))
            if not mid:
                continue
            try:
                mid_int = int(mid)
            except (TypeError, ValueError):
                continue

            is_new = mid_int > since_id_int
            if is_new:
                newest_seen_id = max(newest_seen_id, mid_int)

            # 手动触发：直接返回最新的有效地震信息，不论是否看过或震级大小
            if force:
                found_card = card
                break

            # 定时任务：遇到旧消息后停止处理更旧数据
            if not is_new:
                break

            clean_text = _extract_clean_text(raw_text)
            magnitude = _extract_magnitude(clean_text)

            if magnitude is not None and magnitude >= 4:
                found_cards.append(card)
                continue

            logger.info("Earthquake M%.1f < 4, skipping", magnitude or 0)

        if not force and newest_seen_id > since_id_int:
            if advance_cursor or not found_cards:
                _save_since(context, str(newest_seen_id))
            elif isinstance(getattr(context, "state", None), dict):
                context.state["earthquake_pending_since"] = str(newest_seen_id)

        cards_to_render = [found_card] if force and found_card else found_cards
        if not cards_to_render:
            if force:
                return segments("未获取到地震快讯数据")
            return []

        output = []
        for card in reversed(cards_to_render):
            mblog = card.get("mblog", {})
            raw_text = mblog.get("text", "")
            clean_text = _extract_clean_text(raw_text)
            magnitude = _extract_magnitude(clean_text)
            output.append(text(clean_text))
            figure_url = mblog.get("original_pic")
            if figure_url:
                try:
                    img_data = session.get(figure_url, timeout=20).content
                    figure_dir = context.data_dir / "EarthquakeFigures"
                    figure_dir.mkdir(parents=True, exist_ok=True)
                    file_path = figure_dir / f"{mblog.get('id', uuid.uuid4().hex)}.jpg"
                    atomic_write_bytes(file_path, img_data)
                    output.append(image(str(file_path)))
                except Exception as e:
                    logger.warning("Failed to download earthquake image: %s", e)
            logger.info("Earthquake: %s (M%.1f)", clean_text[:50], magnitude or 0)
        return output

    try:
        return await run_sync(_do_fetch)
    except Exception as exc:
        logger.exception("Earthquake fetch failed: %s", exc)
        return segments(f"地震快讯获取失败: {exc}")
