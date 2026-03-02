"""
URL 解析插件

自动提取消息中的 URL 并生成预览信息。
"""

# 标准库
import logging

# 第三方库
import aiohttp
from bs4 import BeautifulSoup

# 本地导入
from core.plugin_base import run_sync, segments


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
        
        # 使用 aiohttp 异步获取内容
        headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36"
        }
        timeout = aiohttp.ClientTimeout(total=REQUEST_TIMEOUT, sock_read=5)
        
        async with context.http_session.get(url, headers=headers, timeout=timeout) as response:
            if response.status != 200:
                logger.debug(f"URL 请求失败: {response.status} - {url}")
                return []

            content = await response.content.read(MAX_CONTENT_SIZE + 1)
            if len(content) > MAX_CONTENT_SIZE:
                logger.warning(f"URL 响应过大，已跳过: {url}")
                return []

            charset = response.charset or "utf-8"
            try:
                html = content.decode(charset, errors="ignore")
            except LookupError:
                html = content.decode("utf-8", errors="ignore")

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
                image_url = meta_img.get('content', '')

            return title, desc, image_url

        title, desc, image_url = await run_sync(_parse, html)
        
        if not title and not desc:
            logger.debug(f"未找到标题和描述: {url}")
            return []

        # 构建回复
        msg = f"🔗 {title}\n"
        if desc:
            # 截断过长的描述
            if len(desc) > MAX_DESC_LENGTH:
                desc = desc[:MAX_DESC_LENGTH] + "..."
            msg += f"{desc}\n"
        
        msg += f"\n链接: {url}"
        
        logger.info(f"解析 URL 成功: {title[:30] if title else url}")
        return segments(msg)

    except aiohttp.ClientError as exc:
        logger.debug(f"URL 请求错误: {exc}")
        return []
    except Exception as exc:
        logger.exception(f"URL 解析失败: {exc}")
        return []


# ============================================================
# 主处理函数（占位符）
# ============================================================

async def handle(command: str, args: str, event: dict, context) -> list:
    """占位符，避免 PluginManager 警告
    
    此插件通过 dispatcher 直接调用 handle_url() 处理消息中的链接
    """
    return []
