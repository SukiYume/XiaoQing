"""
Twitter 图片抓取与随机发送插件

功能：
1. 从指定 Twitter 账号抓取图片
2. 存储到本地，避免重复下载
3. 随机发送一张未发送过的图片
4. 支持定时自动抓取

需要配置:
- twitter.user_id: Twitter 用户 ID
- twitter.headers: 请求头（包含认证信息）
- twitter.proxy: 代理地址
- twitter.max_pages: 最大检查页数（可选，默认50页）
"""

# 标准库
import logging
import os
import random
import re
from pathlib import Path

# 第三方库
import aiofiles

# 本地导入
from core.args import parse
from core.plugin_base import ensure_dir, image, load_json, segments, text, write_json


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


# ============================================================
# 配置获取
# ============================================================

def _get_config(context) -> dict:
    """获取 Twitter 配置"""
    return context.secrets.get("plugins", {}).get("twitter", {})


def _get_headers(context) -> dict:
    """获取请求头"""
    config = _get_config(context)
    
    # 默认请求头
    default_headers = {
        'authorization': 'Bearer AAAAAAAAAAAAAAAAAAAAANRILgAAAAAAnNwIzUejRCOuH5E6I8xnZz4puTs%3D1Zv7ttfk8LF81IUq16cHjhLTvJu4FA33AGWWjCpTnA',
        'Content-Type': 'application/json',
        'Accept': 'application/json',
        'user-agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/136.0.0.0 Safari/537.36',
    }
    
    # 合并自定义头
    custom_headers = config.get("headers", {})
    default_headers.update(custom_headers)
    
    return default_headers


def _get_proxy(context) -> str | None:
    """获取代理配置"""
    config = _get_config(context)
    return config.get("proxy", "http://127.0.0.1:1080")


def _get_user_id(context) -> str:
    """获取要抓取的 Twitter 用户 ID"""
    config = _get_config(context)
    return config.get("user_id", "885295710848995329")


def _get_cookies(context) -> dict:
    """获取 Cookie 配置"""
    config = _get_config(context)
    # 支持直接配置 cookies 字典，或者从 secrets.json 的 cookies 字段读取
    return config.get("cookies", {})


def _get_max_pages(context) -> int:
    """获取最大检查页数配置"""
    config = _get_config(context)
    return config.get("max_pages", MAX_PAGES_TO_CHECK)


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
            ssl=False,
        ) as response:
            if response.status != 200:
                text = await response.text()
                logger.warning(f"Twitter API 返回 {response.status}: {text}")
                return [], None, False
            
            data = await response.json()
            
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


async def _download_image(url: str, save_dir: Path, context) -> bool:
    """下载单张图片"""
    filename = url.split('/')[-1]
    filepath = save_dir / filename
    
    if filepath.exists():
        return False  # 已存在，跳过
    
    # 请求高清原图
    orig_url = url.split('.jpg')[0] + '?format=jpg&name=4096x4096' if '.jpg' in url else url
    
    proxy = _get_proxy(context)
    headers = _get_headers(context)  # 使用相同的 headers，包含 User-Agent
    
    try:
        async with context.http_session.get(orig_url, proxy=proxy, headers=headers, timeout=30) as response:
            if response.status != 200:
                logger.warning(f"下载失败 {url}: Status {response.status}")
                return False
            
            content = await response.read()
            
            async with aiofiles.open(filepath, 'wb') as f:
                await f.write(content)
            
            logger.info(f"下载图片: {filename}")
            return True
            
    except Exception as exc:
        logger.warning(f"下载图片失败 {url}: {exc}")
        return False


# ============================================================
# 图片抓取
# ============================================================

async def _fetch_twitter_images(context) -> int:
    """抓取 Twitter 图片"""
    save_dir = context.data_dir / "images"
    ensure_dir(save_dir)
    
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
        for url in all_urls:
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
    
    # 读取已发送列表
    posted = set()
    if posted_file.exists():
        async with aiofiles.open(posted_file, 'r', encoding='utf-8') as f:
            content = await f.read()
            posted = set(line.strip() for line in content.split('\n') if line.strip())
    
    # 筛选未发送的图片
    available = [img for img in local_images if img not in posted]
    
    # 如果全都发送过，重置
    if not available:
        logger.info("所有图片都已发送过，重置列表")
        available = local_images
        async with aiofiles.open(posted_file, 'w', encoding='utf-8') as f:
            await f.write("")
    
    # 随机选择
    selected = random.choice(available)
    
    # 记录已发送
    async with aiofiles.open(posted_file, 'a', encoding='utf-8') as f:
        await f.write(f"{selected}\n")
    
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
            
            # 先尝试抓取新图片（但不等待太久）
            save_dir = context.data_dir / "images"
            if not save_dir.exists() or not os.listdir(save_dir):
                logger.info("本地无图片，尝试抓取...")
                await _fetch_twitter_images(context)
            
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
