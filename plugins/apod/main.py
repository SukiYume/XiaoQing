"""
每日一天文图插件 (APOD)
提供每日天文图片的获取和展示功能
"""

# 标准库
import asyncio
import logging
import re
from pathlib import Path

# 第三方库
import aiohttp
from bs4 import BeautifulSoup

# 本地导入
from core.args import parse
from core.plugin_base import image, segments, text


logger = logging.getLogger(__name__)

# ============================================================
# 常量配置
# ============================================================

HEADERS = {
    'user-agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/77.0.3865.90 Safari/537.36',
    'accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,image/apng,*/*;q=0.8,application/signed-exchange;v=b3;q=0.9',
    'accept-encoding': 'gzip, deflate, br'
}

DEFAULT_APOD_URL = 'http://www.star.ucl.ac.uk/~apod/apod'
TIMEOUT_SECONDS = 60
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
            
    except Exception as e:
        context.logger.warning(f"标题提取失败: {e}")
    
    return DEFAULT_FALLBACK_TITLE

async def _fetch_with_retry(
    session: aiohttp.ClientSession,
    url: str,
    proxy: str | None,
    timeout: aiohttp.ClientTimeout,
    is_binary: bool = False,
    context = None
) -> bytes | None:
    """
    统一的网络请求重试逻辑
    
    Args:
        session: HTTP 会话
        url: 请求的 URL
        proxy: 可选的代理地址
        timeout: 超时设置
        is_binary: True 返回 bytes，False 返回 text
        context: 上下文对象
    
    Returns:
        bytes 或 str，失败返回 None
    """
    # 1. 尝试直连
    try:
        context.logger.info(f"尝试直接访问: {url}")
        async with session.get(url, headers=HEADERS, timeout=timeout) as response:
            if response.status == 200:
                result = await response.read() if is_binary else await response.text()
                context.logger.info("直接访问成功")
                return result
            else:
                context.logger.warning(f"直接访问返回状态码: {response.status}")
    except (aiohttp.ClientError, asyncio.TimeoutError) as e:
        context.logger.warning(f"直接访问失败: {e}")
    
    # 2. 尝试代理
    if proxy:
        try:
            context.logger.info(f"使用代理访问: {proxy}")
            async with session.get(url, headers=HEADERS, proxy=proxy, timeout=timeout) as response:
                if response.status == 200:
                    result = await response.read() if is_binary else await response.text()
                    context.logger.info("代理访问成功")
                    return result
                else:
                    context.logger.error(f"代理访问返回状态码: {response.status}")
        except Exception as e:
            context.logger.error(f"代理访问失败: {e}")
    
    return None

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
    except (AttributeError, IndexError) as e:
        context.logger.warning(f"解析 explanation 失败: {e}")
        return EXPLANATION_UNAVAILABLE

async def download_image(
    session: aiohttp.ClientSession, 
    url: str, 
    file_path: Path, 
    proxy: str | None,
    timeout: aiohttp.ClientTimeout,
    context
) -> bool:
    """
    下载图片到本地文件
    
    Args:
        session: HTTP 会话
        url: 图片 URL
        file_path: 保存路径
        proxy: 可选的代理地址
        timeout: 超时设置
        context: 上下文对象
    
    Returns:
        下载是否成功
    """
    try:
        # 确保父目录存在
        file_path.parent.mkdir(parents=True, exist_ok=True)
        
        # 使用统一的重试逻辑下载
        content = await _fetch_with_retry(
            session=session,
            url=url,
            proxy=proxy,
            timeout=timeout,
            is_binary=True,
            context=context
        )
        
        if not content:
            context.logger.error(f"图片下载失败: {url}")
            return False
        
        # 写入文件
        with open(file_path, 'wb') as f:
            f.write(content)
        
        context.logger.info(f"图片下载成功: {file_path}")
        return True
        
    except IOError as e:
        context.logger.error(f"文件写入失败 {file_path}: {e}")
        return False
    except Exception as e:
        context.logger.error(f"下载图片时发生未知错误: {e}")
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
        session = context.http_session
        timeout = aiohttp.ClientTimeout(total=TIMEOUT_SECONDS)
        
        # 使用统一的重试逻辑获取 HTML
        html = await _fetch_with_retry(
            session=session,
            url=url,
            proxy=proxy,
            timeout=timeout,
            is_binary=False,
            context=context
        )
        
        if not html:
            error_msg = "❌ 获取失败: 网络错误" + ("且未配置代理" if not proxy else "")
            logger.error(error_msg)
            return segments(error_msg)

        # 解析 HTML
        soup = BeautifulSoup(html, 'html.parser')
        
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
            if 'http' not in img_src:
                imgurl = f'{url}/{img_src}'
            else:
                imgurl = img_src
            
            # 使用净化后的文件名
            imgname = _sanitize_filename(imgurl)
            img_path = images_dir / imgname
            
            context.logger.info(f"发现图片: {imgurl}")
            
            # 下载图片 (如果不存在或文件大小为0)
            if not img_path.exists() or img_path.stat().st_size == 0:
                context.logger.info("开始下载图片...")
                success = await download_image(
                    session=session,
                    url=imgurl,
                    file_path=img_path,
                    proxy=proxy,
                    timeout=timeout,
                    context=context
                )
                
                if not success:
                    error_msg = f"❌ 图片下载失败，请查看链接: {imgurl}\n\n{title}\n\n{explanation}"
                    context.logger.error(error_msg)
                    return segments(error_msg)
            
            # 返回图片和文字，让框架统一处理发送
            return [
                image(str(img_path)),
                text(f"{title}\n\n{explanation}")
            ]
            
        # -------------------------------------------------------------
        # Case B: Iframe Video
        # -------------------------------------------------------------
        elif soup.find('iframe'):
            videourl = soup.find('iframe').attrs.get('src', 'Video URL unavailable')
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
                video_src = f'{url}/{video_src}'
                
            if video_src:
                return segments(f"{video_src}\n\n{title}\n\n{explanation}")
            else:
                return segments(f"[视频无法获取链接]\n\n{title}\n\n{explanation}\n\n原网址: {url}")
        
        # -------------------------------------------------------------
        # Case D: Other
        # -------------------------------------------------------------
        else:
            return segments(f"今天的 APOD 内容格式不支持，请直接访问: {url}")
             
    except Exception as e:
        logger.exception("APOD handle error: %s", e)
        return segments(f"处理请求时出错: {str(e)}")


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
