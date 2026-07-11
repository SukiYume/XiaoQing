"""
URL 解析插件

自动提取消息中的 URL 并生成预览信息。
"""

# 标准库
import logging
import hashlib
from pathlib import Path
from urllib.parse import urljoin, urlsplit

# 第三方库
from bs4 import BeautifulSoup

# 本地导入
from core.plugin_base import image as image_segment
from core.plugin_base import atomic_write_bytes, run_sync, segments
from core.safe_http import (
    SafeHttpError,
    UnsafeUrlError,
    fetch_public_html,
    fetch_public_bytes,
    validate_public_fetch_target,
)

logger = logging.getLogger(__name__)


# ============================================================
# 常量配置
# ============================================================

MAX_CONTENT_SIZE = 2 * 1024 * 1024  # 2MB
MAX_DESC_LENGTH = 100
REQUEST_TIMEOUT = 10


# ============================================================
# 插件初始化
# ============================================================

def init(context=None) -> None:
    """插件初始化"""
    logger.info("URL 解析插件已加载 (URL Parser Plugin Loaded)")


# ============================================================
# URL 解析处理
# ============================================================

async def handle_url(url: str, event: dict, context) -> list:
    """处理 URL 解析"""
    try:
        if not context.http_session:
            logger.debug("HTTP session 不可用，跳过 URL 解析")
            return []

        # Do not use the application's shared session here.  The safe client
        # validates all DNS answers and pins the address used for the TCP
        # connection, which prevents DNS rebinding and unsafe redirects.
        headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36"
        }
        fetched = await fetch_public_html(url, headers=headers, timeout_seconds=REQUEST_TIMEOUT)
        if fetched is None:
            return []

        charset = fetched.charset or "utf-8"
        try:
            html = fetched.body.decode(charset, errors="ignore")
        except LookupError:
            html = fetched.body.decode("utf-8", errors="ignore")

        # 在线程池中解析 HTML
        def _parse(html_content):
            soup = BeautifulSoup(html_content, 'html.parser')

            title = str(soup.title.string).strip() if soup.title and soup.title.string else ''

            # 获取描述
            desc = ''
            meta_desc = soup.find('meta', attrs={'name': 'description'}) or \
                        soup.find('meta', attrs={'property': 'og:description'})
            if meta_desc:
                content = meta_desc.get('content')
                if isinstance(content, str):
                    desc = content.strip()

            # 获取图片
            image_url = ''
            meta_img = soup.find('meta', attrs={'property': 'og:image'}) or \
                       soup.find('meta', attrs={'name': 'twitter:image'})
            if meta_img:
                image_url = str(meta_img.get('content', '') or '').strip()

            return title, desc, image_url

        title, desc, image_url = await run_sync(_parse, html)
        if image_url:
            image_url = urljoin(fetched.url, image_url)
            try:
                await validate_public_fetch_target(image_url)
                fetched_image = await fetch_public_bytes(
                    image_url,
                    timeout_seconds=REQUEST_TIMEOUT,
                    max_bytes=5 * 1024 * 1024,
                )
                if fetched_image is None:
                    image_url = ""
                else:
                    content_type = fetched_image.headers.get("Content-Type", "image/jpeg").lower()
                    extension = ".png" if "png" in content_type else ".webp" if "webp" in content_type else ".jpg"
                    preview_dir = Path(context.data_dir) / "url_previews"
                    preview_dir.mkdir(parents=True, exist_ok=True)
                    digest = hashlib.sha256(fetched_image.url.encode("utf-8")).hexdigest()
                    image_path = preview_dir / f"{digest}{extension}"
                    await run_sync(atomic_write_bytes, image_path, fetched_image.body)
                    image_url = str(image_path)
            except UnsafeUrlError:
                logger.warning("URL preview image target was rejected")
                image_url = ""
            except SafeHttpError:
                logger.warning("URL preview image download failed safely")
                image_url = ""

        if not title and not desc and not image_url:
            logger.debug("URL 未提取到预览内容: host=%s", urlsplit(url).hostname or "")
            return []

        response = []

        if title or desc:
            # 构建回复
            msg = f"🔗 {title}\n"
            if desc:
                # 截断过长的描述
                if len(desc) > MAX_DESC_LENGTH:
                    desc = desc[:MAX_DESC_LENGTH] + "..."
                msg += f"{desc}\n"

            msg += f"\n链接: {url}"
            response.extend(segments(msg))

        if image_url:
            response.append(image_segment(image_url))

        logger.info("URL 解析成功: host=%s title_length=%d", urlsplit(url).hostname or "", len(title))
        return response

    except (SafeHttpError, UnsafeUrlError) as exc:
        logger.debug("URL 请求 rejected or failed safely: %s", exc)
        return []
    except Exception as exc:
        logger.exception("URL 解析失败: %s", type(exc).__name__)
        return []


# ============================================================
# 主处理函数（占位符）
# ============================================================

async def handle(command: str, args: str, event: dict, context) -> list:
    """占位符，避免 PluginManager 警告

    此插件通过 dispatcher 直接调用 handle_url() 处理消息中的链接
    """
    return []
