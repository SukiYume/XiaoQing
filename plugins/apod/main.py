"""
每日一天文图插件 (APOD)
提供每日天文图片的获取和展示功能
"""

# 标准库
import asyncio
import hashlib
import io
import logging
import re
from pathlib import Path
from typing import Any
from urllib.parse import urljoin, urlsplit

# 第三方库
from bs4 import BeautifulSoup
from PIL import Image

# 本地导入
from core.args import parse
from core.plugin_base import atomic_write_bytes, image, segments, text
from core.public_errors import public_error_message, public_error_response
from core.safe_http import UnsafeUrlError, fetch_public_bytes, fetch_public_html


logger = logging.getLogger(__name__)

# ============================================================
# 常量配置
# ============================================================

HEADERS = {
    'user-agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/77.0.3865.90 Safari/537.36',
    'accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,image/apng,*/*;q=0.8,application/signed-exchange;v=b3;q=0.9',
    'accept-encoding': 'gzip, deflate, br'
}

DEFAULT_APOD_URL = 'https://apod.nasa.gov/apod/astropix.html'
HTML_TIMEOUT_SECONDS = 15
IMAGE_TIMEOUT_SECONDS = 20
MAX_IMAGE_BYTES = 12 * 1024 * 1024
MAX_IMAGE_PIXELS = 40_000_000
DEFAULT_FALLBACK_TITLE = "Today's Astronomy Picture of the Day"
NO_EXPLANATION_TEXT = "No explanation found."
EXPLANATION_UNAVAILABLE = "Explanation unavailable."


# ============================================================
# 配置获取
# ============================================================

def _get_config(context) -> dict:
    """获取插件配置"""
    return context.config.get("plugins", {}).get("apod", {})


def _get_proxy(context) -> str | None:
    """获取代理配置，返回 None 表示不使用代理"""
    return _get_config(context).get("proxy")


# ============================================================
# 辅助函数
# ============================================================

def _sanitize_filename(url: str) -> str:
    """从 URL 提取并清理文件名"""
    from urllib.parse import urlparse, unquote
    
    parsed = urlparse(url)
    filename = unquote(parsed.path.split('/')[-1])
    # 移除非法字符
    filename = re.sub(r'[<>:"/\\|?*]', '_', filename)
    # 确保有扩展名
    if '.' not in filename:
        filename += '.jpg'
    return filename


def _cache_filename(url: str, content_type: str) -> str:
    """Build a Windows-safe, collision-resistant cache name."""
    extension = {
        "image/jpeg": ".jpg",
        "image/png": ".png",
        "image/gif": ".gif",
        "image/webp": ".webp",
    }.get(content_type.split(";", 1)[0].strip().lower(), ".img")
    return f"{hashlib.sha256(url.encode('utf-8')).hexdigest()}{extension}"


def _allowed_hosts(context) -> set[str]:
    configured = _get_config(context).get("allowed_hosts", [])
    hosts = {"apod.nasa.gov"}
    if isinstance(configured, list):
        hosts.update(str(host).strip().rstrip(".").lower() for host in configured if host)
    return hosts


def _require_allowed_url(url: str, context) -> str:
    parsed = urlsplit(url)
    host = (parsed.hostname or "").rstrip(".").lower()
    if parsed.scheme.lower() != "https" or host not in _allowed_hosts(context):
        raise UnsafeUrlError("APOD URL host is not allowed")
    return url


def _require_https_display_url(url: str) -> str:
    """Validate a link that is displayed but never fetched by the bot."""
    parsed = urlsplit(url)
    if parsed.scheme.lower() != "https" or not parsed.hostname:
        raise UnsafeUrlError("media link is not an absolute HTTPS URL")
    return url


async def _safe_download_image(url: str, images_dir: Path, context) -> Path | None:
    _require_allowed_url(url, context)
    fetched = await fetch_public_bytes(
        url,
        headers={**HEADERS, "accept-encoding": "identity"},
        timeout_seconds=IMAGE_TIMEOUT_SECONDS,
        max_bytes=MAX_IMAGE_BYTES,
        allowed_content_type_prefixes=("image/",),
        allowed_hosts=_allowed_hosts(context),
    )
    if fetched is None:
        return None

    def validate() -> None:
        with Image.open(io.BytesIO(fetched.body)) as candidate:
            width, height = candidate.size
            if width <= 0 or height <= 0 or width * height > MAX_IMAGE_PIXELS:
                raise ValueError("image pixel budget exceeded")
            candidate.verify()

    await asyncio.to_thread(validate)
    content_type = fetched.headers.get("Content-Type", "")
    target = images_dir / _cache_filename(fetched.url, content_type)
    if not target.exists() or target.stat().st_size != len(fetched.body):
        await asyncio.to_thread(atomic_write_bytes, target, fetched.body)
    return target


async def _fetch_with_retry(
    session: Any,
    url: str,
    proxy: str | None,
    timeout: Any,
    is_binary: bool = False,
    context=None,
) -> bytes | None:
    """Compatibility entry point backed exclusively by the pinned safe client."""
    del session, proxy, timeout
    _require_allowed_url(url, context)
    if is_binary:
        response = await fetch_public_bytes(
            url,
            headers={**HEADERS, "accept-encoding": "identity"},
            timeout_seconds=IMAGE_TIMEOUT_SECONDS,
            max_bytes=MAX_IMAGE_BYTES,
            allowed_content_type_prefixes=("image/",),
            allowed_hosts=_allowed_hosts(context),
            allowed_schemes=("https",),
        )
    else:
        response = await fetch_public_html(
            url,
            headers={**HEADERS, "accept-encoding": "identity"},
            timeout_seconds=HTML_TIMEOUT_SECONDS,
            allowed_hosts=_allowed_hosts(context),
        )
    return response.body if response else None

def _extract_title(soup: BeautifulSoup, context) -> str:
    """提取标题，使用多种策略增强鲁棒性"""
    try:
        # 策略1: 查找第二个 center 标签
        centers = soup.find_all('center')
        if len(centers) > 1 and centers[1].b:
            title_text = centers[1].b.string
            if title_text:
                return title_text.strip()
        
        # 策略2: 查找任何有内容的 center 标签中的 b 标签
        for center in centers:
            if center.b and center.b.string:
                return center.b.string.strip()
        
        # 策略3: 使用 title 标签
        if soup.title and soup.title.string:
            return soup.title.string.strip()
            
    except Exception as exc:
        public_error_message(
            context,
            exc,
            logger=context.logger,
            component="apod.extract_title",
        )
    
    return DEFAULT_FALLBACK_TITLE

async def get_explanation(soup: BeautifulSoup, context) -> str:
    """从页面提取解释文本"""
    if not soup:
        return NO_EXPLANATION_TEXT
    
    try:
        paragraphs = soup.find_all('p')
        for paragraph in paragraphs:
            bold = paragraph.find('b')
            if bold and bold.string and bold.string.strip() == 'Explanation:':
                text = paragraph.get_text().strip()
                # 移除 "Tomorrow's picture:" 之后的内容
                parts = re.split(r'Tomorrow\'s picture:', text)
                return parts[0].strip() if parts else text
        return NO_EXPLANATION_TEXT
    except (AttributeError, IndexError) as exc:
        public_error_message(
            context,
            exc,
            logger=context.logger,
            component="apod.parse_explanation",
        )
        return EXPLANATION_UNAVAILABLE


async def download_image(
    session: Any,
    url: str,
    file_path: Path,
    proxy: str | None,
    timeout: Any,
    context,
) -> bool:
    """Persist an APOD image fetched through the pinned bounded client."""
    try:
        content = await _fetch_with_retry(
            session=session,
            url=url,
            proxy=proxy,
            timeout=timeout,
            is_binary=True,
            context=context,
        )
        if not content:
            return False
        file_path.parent.mkdir(parents=True, exist_ok=True)
        await asyncio.to_thread(atomic_write_bytes, file_path, content)
        return True
    except Exception as exc:
        public_error_message(
            context,
            exc,
            logger=context.logger,
            component="apod.download_image",
        )
        return False

# ============================================================
# 插件初始化
# ============================================================

def init(context=None) -> None:
    """插件初始化"""
    pass


# ============================================================
# 主处理函数
# ============================================================

async def handle(command: str, args: str, event: dict, context) -> list:
    """命令处理入口"""
    try:
        parsed = parse(args)
        
        # 解析子命令
        if parsed and parsed.first:
            subcommand = parsed.first.lower()
            
            if subcommand == "help" or subcommand == "帮助":
                return segments(_show_help())
        
        logger.info("开始获取 APOD...")
        
        # 从配置获取 URL
        url = _get_config(context).get("url", DEFAULT_APOD_URL)
        
        # 准备图片存储目录
        images_dir = context.data_dir / "images"
        images_dir.mkdir(parents=True, exist_ok=True)
        
        proxy = _get_proxy(context)
        page_url = _require_allowed_url(url, context)
        safe_response = await fetch_public_html(
            page_url,
            headers={**HEADERS, "accept-encoding": "identity"},
            timeout_seconds=HTML_TIMEOUT_SECONDS,
            allowed_hosts=_allowed_hosts(context),
        )
        html = safe_response.body if safe_response else None
        
        if not html:
            error_msg = "❌ 获取失败: 网络错误" + ("且未配置代理" if not proxy else "")
            logger.error(error_msg)
            return segments(error_msg)

        # 解析 HTML
        soup = await asyncio.to_thread(BeautifulSoup, html, 'html.parser')
        
        # 获取标题（使用增强的提取函数）
        title = _extract_title(soup, context)
        
        # 获取解释
        explanation = await get_explanation(soup, context)
        
        # -------------------------------------------------------------
        # Case A: Image
        # -------------------------------------------------------------
        if soup.find('img'):
            img_src = soup.find('img').attrs.get('src', '')
            if not img_src:
                return segments("❌ 无法获取图片链接")
            
            # 构造完整 URL
            base_url = safe_response.url if safe_response else page_url
            imgurl = urljoin(base_url, img_src)
            _require_allowed_url(imgurl, context)
            img_path: Path | None = None
            
            context.logger.info(f"发现图片: {imgurl}")
            
            img_path = await _safe_download_image(imgurl, images_dir, context)
            if img_path is None:
                return segments(f"❌ 图片下载失败\n\n{title}\n\n{explanation}")
            
            # 返回图片和文字，让框架统一处理发送
            return [
                image(str(img_path)),
                text(f"{title}\n\n{explanation}")
            ]
            
        # -------------------------------------------------------------
        # Case B: Iframe Video
        # -------------------------------------------------------------
        elif soup.find('iframe'):
            videourl = urljoin(page_url, soup.find('iframe').attrs.get('src', ''))
            _require_https_display_url(videourl)
            context.logger.info(f"发现 iframe 视频: {videourl}")
            return segments(f"{videourl}\n\n{title}\n\n{explanation}")
            
        # -------------------------------------------------------------
        # Case C: Video Tag
        # -------------------------------------------------------------
        elif soup.find('video'):
            context.logger.info("发现 video 标签视频")
            video_element = soup.find('video')
            video_src = None
            
            if video_element.find('source'):
                video_src = video_element.find('source').attrs.get('src', '')
            
            if not video_src and 'src' in video_element.attrs:
                video_src = video_element.attrs['src']
                
            if video_src and not (video_src.startswith('http://') or video_src.startswith('https://')):
                video_src = urljoin(url, video_src)
                
            if video_src:
                _require_https_display_url(video_src)
                return segments(f"{video_src}\n\n{title}\n\n{explanation}")
            else:
                return segments(f"[视频无法获取链接]\n\n{title}\n\n{explanation}\n\n原网址: {url}")
        
        # -------------------------------------------------------------
        # Case D: Other
        # -------------------------------------------------------------
        else:
            return segments(f"今天的 APOD 内容格式不支持，请直接访问: {url}")
             
    except Exception as exc:
        return public_error_response(context, exc, logger=logger, component="apod.handle")


def _show_help() -> str:
    """显示帮助信息"""
    return """
🌌 **每日一天文图 (APOD)**

**基本用法:**
• /apod - 获取今日天文图片
• /apod help - 显示帮助信息

**功能特点:**
✨ 自动获取 NASA 每日天文图片
📷 支持图片自动下载与缓存
📝 提供图片说明和描述
🎬 支持视频链接展示
🔄 智能代理重试机制

输入 /apod 获取今日天文美图
""".strip()


# ============================================================
# 定时任务
# ============================================================

async def scheduled(context) -> list:
    """
    定时任务入口
    
    每天 13:30 自动推送 APOD 到配置的群组
    """
    context.logger.info("执行 APOD 定时任务...")
    
    # 构造事件对象，包含消息类型信息
    # group_id 将由定时任务框架根据 plugin.json 中的配置自动填充
    event = {
        "message_type": "group",
        "group_id": None,
        "user_id": None
    }
    
    return await handle(
        command="apod",
        args="",
        event=event,
        context=context
    )
