"""
Twitter 图片抓取与随机发送插件

功能：
1. 从指定 Twitter 账号抓取图片
2. 存储到本地，避免重复下载
3. 随机发送一张未发送过的图片
4. 支持定时自动抓取

需要在 context.secrets 中显式配置:
- twitter.user_id: Twitter 用户 ID
- twitter.headers: 请求头（认证信息无代码内默认值）
- twitter.proxy: 代理地址（可选，显式配置才启用）
- twitter.max_pages: 最大检查页数（可选，默认50页）
"""

# 标准库
import asyncio
import hashlib
import json
import logging
import os
import random
import uuid
from pathlib import Path
from urllib.parse import urlparse

# 第三方库
import aiofiles
from PIL import Image, UnidentifiedImageError

# 本地导入
from core.args import parse
from core.plugin_base import ensure_dir, image, segments
from core.safe_http import fetch_public_bytes

logger = logging.getLogger(__name__)


# ============================================================
# 插件初始化
# ============================================================

def init(context=None) -> None:
    """插件初始化"""
    logger.info("Twitter 图片抓取插件已加载 (Twitter Plugin Loaded)")


def _show_help_twimg() -> str:
    """返回 twimg 命令的帮助信息"""
    return """
🎨 **推特图片命令**

**基本用法:**
• /twimg - 随机推特美图
• /twitter - 随机推特美图
• /推特 - 随机推特美图
• /twimg help - 显示帮助

**功能说明:**
自动从本地图片库中随机选择一张推特图片发送

输入 /twimg 获取随机推特美图
""".strip()


def _show_help_tw_fetch() -> str:
    """返回 tw_fetch 命令的帮助信息"""
    return """
🔄 **抓取推特图片**

**基本用法:**
• /tw_fetch - 手动抓取新图片
• /抓取推特 - 手动抓取新图片
• /tw_fetch help - 显示帮助

**功能说明:**
从配置的 Twitter 账号抓取最新图片到本地

**注意事项:**
⚠️ 此命令需要管理员权限
💡 插件每天凌晨3点会自动抓取

输入 /tw_fetch 开始抓取
""".strip()


# ============================================================
# 常量配置
# ============================================================

MAX_PAGES_WITHOUT_NEW_IMAGES = 2
MAX_PAGES_TO_CHECK = 50  # 增加最大检查页数
REQUEST_TIMEOUT_SECONDS = 30
MAX_API_BYTES = 5 * 1024 * 1024
MAX_IMAGE_BYTES = 10 * 1024 * 1024
MAX_IMAGES_PER_FETCH = 100
MAX_IMAGE_CACHE_BYTES = 512 * 1024 * 1024
_ALLOWED_TWITTER_MEDIA_HOSTS = {"pbs.twimg.com", "ton.twitter.com", "video.twimg.com"}
_POSTED_LOCK = asyncio.Lock()
_IMAGE_FORMAT_EXTENSIONS = {
    "JPEG": ".jpg",
    "PNG": ".png",
    "WEBP": ".webp",
}


# ============================================================
# 配置获取
# ============================================================

def _get_config(context) -> dict:
    """获取 Twitter 配置"""
    return context.secrets.get("plugins", {}).get("twitter", {})


def _get_headers(context) -> dict:
    """Return generic headers plus only explicitly configured authentication."""
    config = _get_config(context)

    # 默认请求头
    default_headers = {
        'Content-Type': 'application/json',
        'Accept': 'application/json',
        'user-agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/136.0.0.0 Safari/537.36',
    }

    # 合并自定义头
    custom_headers = config.get("headers", {})
    if isinstance(custom_headers, dict):
        default_headers.update(
            {
                str(key): str(value)
                for key, value in custom_headers.items()
                if str(key).strip() and str(value).strip()
            }
        )

    return default_headers


def _get_proxy(context) -> str | None:
    """获取代理配置"""
    config = _get_config(context)
    proxy = config.get("proxy")
    return str(proxy).strip() if proxy else None


def _is_allowed_media_url(url: str) -> bool:
    parsed = urlparse(url)
    return parsed.scheme == "https" and parsed.hostname in _ALLOWED_TWITTER_MEDIA_HOSTS


def _get_user_id(context) -> str:
    """获取要抓取的 Twitter 用户 ID"""
    config = _get_config(context)
    return config.get("user_id", "123456789012345678")


def _get_cookies(context) -> dict:
    """获取 Cookie 配置"""
    config = _get_config(context)
    # 支持直接配置 cookies 字典，或者从 secrets.json 的 cookies 字段读取
    return config.get("cookies", {})


def _get_max_pages(context) -> int:
    """获取最大检查页数配置"""
    config = _get_config(context)
    try:
        return max(1, min(int(config.get("max_pages", MAX_PAGES_TO_CHECK)), MAX_PAGES_TO_CHECK))
    except (TypeError, ValueError):
        return MAX_PAGES_TO_CHECK


async def _response_json_limited(response, max_bytes: int = MAX_API_BYTES):
    content = getattr(response, "content", None)
    if content is None or not hasattr(content, "iter_chunked"):
        return await response.json()
    chunks = []
    total = 0
    async for chunk in content.iter_chunked(64 * 1024):
        total += len(chunk)
        if total > max_bytes:
            raise ValueError("Twitter API response too large")
        chunks.append(chunk)
    return json.loads(b"".join(chunks))


# ============================================================
# Twitter API 交互
# ============================================================

async def _fetch_timeline(context, cursor: str | None = None) -> tuple:
    """获取用户时间线"""
    url = 'https://x.com/i/api/graphql/mF05yo9gtSsl1tFPPHNEgQ/UserTweets'

    user_id = _get_user_id(context)

    variables = {
        'userId': user_id,
        'count': 100,
        'includePromotedContent': False,
        'withCommunity': False,
        'withVoice': False,
        'include_entities': True,
        'include_user_entities': True,
        'include_ext_media_availability': True,
        'include_ext_alt_text': True,
        'include_cards': True,
        'tweet_mode': 'extended'
    }

    if cursor:
        variables['cursor'] = cursor

    features = {
        'responsive_web_enhance_cards_enabled': True,
        'rweb_video_screen_enabled': False,
        'profile_label_improvements_pcf_label_in_post_enabled': False,
        'rweb_tipjar_consumption_enabled': False,
        'verified_phone_label_enabled': False,
        'creator_subscriptions_tweet_preview_api_enabled': False,
        'responsive_web_graphql_timeline_navigation_enabled': False,
        'responsive_web_graphql_skip_user_profile_image_extensions_enabled': False,
        'premium_content_api_read_enabled': False,
        'communities_web_enable_tweet_community_results_fetch': False,
        'c9s_tweet_anatomy_moderator_badge_enabled': False,
        'responsive_web_grok_analyze_button_fetch_trends_enabled': False,
        'responsive_web_grok_analyze_post_followups_enabled': False,
        'responsive_web_jetfuel_frame': False,
        'responsive_web_grok_share_attachment_enabled': False,
        'articles_preview_enabled': True,
        'responsive_web_edit_tweet_api_enabled': True,
        'graphql_is_translatable_rweb_tweet_is_translatable_enabled': True,
        'view_counts_everywhere_api_enabled': True,
        'longform_notetweets_consumption_enabled': True,
        'responsive_web_twitter_article_tweet_consumption_enabled': True,
        'tweet_awards_web_tipping_enabled': False,
        'responsive_web_grok_show_grok_translated_post': False,
        'responsive_web_grok_analysis_button_from_backend': False,
        'creator_subscriptions_quote_tweet_preview_enabled': False,
        'freedom_of_speech_not_reach_fetch_enabled': True,
        'standardized_nudges_misinfo': True,
        'tweet_with_visibility_results_prefer_gql_limited_actions_policy_enabled': True,
        'longform_notetweets_rich_text_read_enabled': True,
        'longform_notetweets_inline_media_enabled': True,
        'responsive_web_grok_image_annotation_enabled': False,
    }

    # 字段开关
    field_toggles = {
        'withArticlePlainText': False
    }

    params = {
        'variables': str(variables).replace("'", '"').replace('True', 'true').replace('False', 'false'),
        'features': str(features).replace("'", '"').replace('True', 'true').replace('False', 'false'),
        'fieldToggles': str(field_toggles).replace("'", '"').replace('True', 'true').replace('False', 'false'),
    }

    headers = _get_headers(context)
    cookies = _get_cookies(context)
    proxy = _get_proxy(context)

    try:
        async with context.http_session.get(
            url,
            params=params,
            headers=headers,
            cookies=cookies,
            proxy=proxy,
            timeout=REQUEST_TIMEOUT_SECONDS,
        ) as response:
            if response.status != 200:
                logger.warning("Twitter API 返回 %s", response.status)
                return [], None, False

            data = await _response_json_limited(response)

            timeline = data.get('data', {}).get('user', {}).get('result', {}).get('timeline', {}).get('timeline', {})
            instructions = timeline.get('instructions', [])

            # 找到 TimelineAddEntries
            entries = []
            for inst in instructions:
                if inst.get('type') == 'TimelineAddEntries':
                    entries = inst.get('entries', [])
                    break

            # 提取推文
            tweets = [e for e in entries if e.get('entryId', '').startswith('tweet-')]

            # 找到下一页 cursor
            next_cursor = None
            for entry in entries:
                if entry.get('entryId', '').startswith('cursor-bottom-'):
                    next_cursor = entry.get('content', {}).get('value')
                    break

            has_next = next_cursor is not None
            return tweets, next_cursor, has_next

    except Exception as exc:
        logger.error(f"Twitter API 请求失败: {exc}")
        return [], None, False


def _extract_image_urls(tweet: dict) -> list:
    """从推文中提取图片 URL"""
    media = (
        tweet.get('content', {})
        .get('itemContent', {})
        .get('tweet_results', {})
        .get('result', {})
        .get('legacy', {})
        .get('extended_entities', {})
        .get('media', [])
    )
    return [m['media_url_https'] for m in media if m.get('type') == 'photo']


def _contained_cache_path(cache_root: Path, filename: str) -> Path:
    root = cache_root.resolve(strict=True)
    candidate = (root / filename).resolve(strict=False)
    if candidate.parent != root:
        raise ValueError("Twitter cache path escaped its root")
    return candidate


def _detect_image_extension(path: Path) -> str:
    try:
        with Image.open(path) as image_file:
            image_format = str(image_file.format or "").upper()
            image_file.verify()
    except (OSError, UnidentifiedImageError) as exc:
        raise ValueError("Twitter media response is not a valid supported image") from exc
    extension = _IMAGE_FORMAT_EXTENSIONS.get(image_format)
    if extension is None:
        raise ValueError(f"unsupported Twitter image format: {image_format or 'unknown'}")
    return extension


async def _download_image(url: str, save_dir: Path, context) -> bool:
    """下载单张图片"""
    if not _is_allowed_media_url(url):
        logger.warning(f"拒绝下载非 Twitter 媒体域名: {url}")
        return False

    save_dir.mkdir(parents=True, exist_ok=True)
    cache_root = save_dir.resolve(strict=True)

    # 请求高清原图
    orig_url = url.split('.jpg')[0] + '?format=jpg&name=4096x4096' if '.jpg' in url else url
    if not _is_allowed_media_url(orig_url):
        logger.warning(f"拒绝下载可疑原图地址: {orig_url}")
        return False

    headers = _get_headers(context)  # 使用相同的 headers，包含 User-Agent

    temp_path: Path | None = None
    try:
        fetched = await fetch_public_bytes(
            orig_url,
            headers=headers,
            timeout_seconds=REQUEST_TIMEOUT_SECONDS,
            max_bytes=MAX_IMAGE_BYTES,
            allowed_content_type_prefixes=("image/",),
            allowed_hosts=_ALLOWED_TWITTER_MEDIA_HOSTS,
            allowed_schemes=("https",),
        )
        if fetched is None:
            logger.warning("Twitter media request returned no successful response")
            return False
        content = fetched.body
        if not content:
            raise ValueError("Twitter image response is empty")
        if len(content) > MAX_IMAGE_BYTES:
            raise ValueError("Twitter image too large")

        temp_path = _contained_cache_path(cache_root, f".{uuid.uuid4().hex}.tmp")
        digest = hashlib.sha256(content)
        async with aiofiles.open(temp_path, "xb") as f:
            await f.write(content)

        extension = await asyncio.to_thread(_detect_image_extension, temp_path)
        filename = f"{digest.hexdigest()}{extension}"
        filepath = _contained_cache_path(cache_root, filename)
        if filepath.exists():
            temp_path.unlink(missing_ok=True)
            return False
        os.replace(temp_path, filepath)
        temp_path = None

        logger.info("下载图片: %s", filename)
        return True

    except Exception as exc:
        if temp_path is not None:
            temp_path.unlink(missing_ok=True)
        logger.warning(f"下载图片失败 {url}: {exc}")
        return False


# ============================================================
# 图片抓取
# ============================================================

async def _fetch_twitter_images(context) -> int:
    """抓取 Twitter 图片"""
    save_dir = context.data_dir / "images"
    ensure_dir(save_dir)
    files = sorted(
        (path for path in save_dir.iterdir() if path.is_file()),
        key=lambda path: path.stat().st_mtime,
    )
    total_bytes = sum(path.stat().st_size for path in files)
    while files and total_bytes > MAX_IMAGE_CACHE_BYTES:
        stale = files.pop(0)
        total_bytes -= stale.stat().st_size
        stale.unlink(missing_ok=True)

    cursor = None
    total_new = 0
    pages_checked = 0
    consecutive_empty = 0
    max_pages = _get_max_pages(context)  # 从配置读取最大页数

    while pages_checked < max_pages:
        pages_checked += 1
        logger.info(f"Twitter: 检查第 {pages_checked} 页...")

        tweets, next_cursor, has_next = await _fetch_timeline(context, cursor)

        if not tweets:
            break

        # 收集所有图片 URL
        all_urls = []
        for tweet in tweets:
            all_urls.extend(_extract_image_urls(tweet))

        # 下载新图片
        new_count = 0
        if total_new >= MAX_IMAGES_PER_FETCH:
            break
        for url in all_urls[:MAX_IMAGES_PER_FETCH - total_new]:
            if await _download_image(url, save_dir, context):
                new_count += 1

        total_new += new_count

        if new_count > 0:
            consecutive_empty = 0
        else:
            consecutive_empty += 1

        # 连续多页没有新图片则停止
        if consecutive_empty >= MAX_PAGES_WITHOUT_NEW_IMAGES:
            logger.info(f"连续 {consecutive_empty} 页没有新图片，停止抓取")
            break

        if not has_next:
            break

        cursor = next_cursor

    logger.info(f"Twitter: 共下载 {total_new} 张新图片")
    return total_new


# ============================================================
# 随机图片
# ============================================================

async def _get_random_image(context) -> str | None:
    """获取随机图片路径"""
    save_dir = context.data_dir / "images"
    posted_file = context.data_dir / "posted.txt"

    ensure_dir(save_dir)

    # 获取所有本地图片
    local_images = [f for f in os.listdir(save_dir) if f.endswith(('.jpg', '.png', '.jpeg', '.webp'))]

    if not local_images:
        return None

    async with _POSTED_LOCK:
        posted = set()
        if posted_file.exists():
            async with aiofiles.open(posted_file, encoding='utf-8') as f:
                content = await f.read()
                posted = {line.strip() for line in content.split('\n') if line.strip()}
        available = [img for img in local_images if img not in posted]
        if not available:
            logger.info("所有图片都已发送过，重置列表")
            available = local_images
            posted = set()
        selected = random.choice(available)
        posted.add(selected)
        temp_file = posted_file.with_suffix(".tmp")
        async with aiofiles.open(temp_file, 'w', encoding='utf-8') as f:
            await f.write("\n".join(sorted(posted)) + "\n")
        temp_file.replace(posted_file)

    return str(save_dir / selected)


# ============================================================
# 主处理函数
# ============================================================

async def handle(command: str, args: str, event: dict, context) -> list:
    """命令处理入口"""
    try:
        # 使用 parse 解析参数
        parsed = parse(args)

        # 手动抓取命令
        if command in ('tw_fetch', '抓取推特'):
            # 检查是否请求帮助
            if parsed and parsed.first.lower() in ["help", "帮助"]:
                return segments(_show_help_tw_fetch())

            # 导入 build_action
            from core.plugin_base import build_action

            # 发送开始消息
            start_msg = segments("🔄 开始抓取 Twitter 图片...")
            start_action = build_action(start_msg, context.current_user_id, context.current_group_id)
            if start_action:
                await context.send_action(start_action)

            # 执行抓取
            count = await _fetch_twitter_images(context)

            # 返回完成消息
            return segments(f"✅ Twitter 图片抓取完成，新下载 {count} 张图片")

        # 随机图片命令
        else:
            # 检查是否请求帮助
            if parsed and parsed.first.lower() in ["help", "帮助"]:
                return segments(_show_help_twimg())

            # 随机命令只读取本地缓存；抓取仅允许 admin 命令或定时任务。
            img_path = await _get_random_image(context)

            if img_path:
                return [image(img_path)]
            else:
                return segments("无法获取 Twitter 图片，请稍后再试")

    except Exception as exc:
        logger.error(f"处理命令时出错: {exc}", exc_info=True)
        return segments(f"❌ 处理失败: {str(exc)}")


async def scheduled_fetch(context) -> list:
    """定时抓取任务"""
    count = await _fetch_twitter_images(context)

    if count > 0:
        logger.info(f"Twitter 定时抓取: 下载了 {count} 张新图片")

    # 定时任务不发送消息
    return []
