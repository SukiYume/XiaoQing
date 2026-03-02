"""
A岛匿名版插件 (adnmb)

功能：
- 查看时间线
- 浏览板块列表和板块内容
- 查看串和回复
- 订阅管理（添加/删除/查看）

用法：
  /adnmb -h           查看帮助
  /adnmb -t           查看时间线
  /adnmb -f           查看板块列表
  /adnmb -m <板块名>   查看板块内容
  /adnmb -c <串号>     查看串内容
  /adnmb -r <回复号>   查看单条回复
  /adnmb -d           查看订阅
  /adnmb -a <串号>     添加订阅
  /adnmb -e <串号>     删除订阅
  /adnmb -p <页码>     指定页码（配合其他选项使用）

注意：用户登录/回复功能已禁用，仅保留浏览功能。
"""

import sys
import logging
from pathlib import Path

from core.plugin_base import segments, text, image, ensure_dir, PluginContextProtocol
from core.args import parse

# 使用相对导入
from .adapi import AdnmbClient, Post, Thread


logger = logging.getLogger(__name__)


def _get_plugin_runtime_state(context, *, create: bool = True) -> dict:
    if context is not None and hasattr(context, "state") and isinstance(context.state, dict):
        runtime_state = context.state.get("adnmb_runtime")
        if isinstance(runtime_state, dict):
            return runtime_state
        if create:
            context.state["adnmb_runtime"] = {}
            return context.state["adnmb_runtime"]
    return {}


def _get_client(context, cache_dir: Path) -> AdnmbClient:
    runtime_state = _get_plugin_runtime_state(context)
    cached_client = runtime_state.get("client")
    if isinstance(cached_client, AdnmbClient):
        if cached_client.session is context.http_session and cached_client.cache_dir == cache_dir:
            return cached_client

    plugin_cfg = context.secrets.get("plugins", {}).get("adnmb", {})
    client = AdnmbClient(
        context.http_session,
        cache_dir,
        uuid=str(plugin_cfg.get("uuid", "") or ""),
    )
    runtime_state["client"] = client
    return client


# ============================================================
# 插件初始化
# ============================================================

def init(context=None) -> None:
    """插件初始化"""
    logger.info("ADnmb 插件已初始化")


# ============================================================
# 消息格式化
# ============================================================

async def format_posts(
    posts: list, 
    client: AdnmbClient,
    max_items: int = 10,
    download_images: bool = True
) -> list:
    """
    将帖子列表格式化为消息段
    
    参数:
        posts: 帖子列表
        client: API 客户端（用于下载图片）
        max_items: 最大显示数量
        download_images: 是否下载图片
    
    返回:
        消息段列表
    """
    if not posts:
        return segments("暂无内容")
    
    result = []
    for post in posts[:max_items]:
        # 添加帖子文本
        result.append(text(post.format_text()))
        
        # 如果有图片，下载并添加
        if download_images and post.img:
            img_path = await client.download_image(post.img)
            if img_path:
                result.append(image(str(img_path)))
        
        result.append(text("\n\n"))
    
    # 移除最后的空行
    if result and result[-1].get("data", {}).get("text") == "\n\n":
        result.pop()
    
    return result


async def format_threads(
    threads: list,
    client: AdnmbClient,
    max_items: int = 10,
    show_replies: bool = True
) -> list:
    """
    将串列表格式化为消息段
    
    参数:
        threads: 串列表
        client: API 客户端
        max_items: 最大显示数量
        show_replies: 是否显示回复
    
    返回:
        消息段列表
    """
    if not threads:
        return segments("暂无内容")
    
    # 收集所有帖子
    all_posts = []
    for thread in threads[:max_items]:
        all_posts.append(thread.main_post)
        if show_replies:
            all_posts.extend(thread.replies[:3])  # 每串最多显示 3 条回复
    
    return await format_posts(all_posts, client, max_items=len(all_posts))


# ============================================================
# 命令处理
# ============================================================

async def handle(command: str, args: str, event: dict, context: PluginContextProtocol) -> list:
    """命令处理入口"""
    try:
        logger.info(f"收到 ADnmb 命令: {command} {args}")
        parsed = parse(args)

        # 初始化缓存目录
        cache_dir = context.plugin_dir / "cache"
        ensure_dir(cache_dir)
        logger.debug(f"缓存目录: {cache_dir}")
        
        client = _get_client(context, cache_dir)
        logger.debug("API 客户端已创建")

        # 获取页码（默认 1）
        page = 1
        if parsed.has('p') or parsed.has('page'):
            try:
                page = int(parsed.opt('p') or parsed.opt('page') or '1')
            except ValueError:
                page = 1

        # 帮助信息
        if parsed.has('h') or parsed.has('help') or parsed.has('l') or parsed.has('list'):
            return segments(_get_help())

        # 时间线
        if parsed.has('t') or parsed.has('timeline'):
            logger.info(f"获取时间线，页码: {page}")
            threads = await client.get_timeline(page)
            logger.info(f"获取到 {len(threads)} 个串")
            return await format_threads(threads, client, show_replies=False)

        # 板块列表
        if parsed.has('f') or parsed.has('forumlist'):
            forum_list = await client.get_forum_list()
            lines = ["A岛板块列表", "=" * 20]
            for name, fid in forum_list.items():
                lines.append(f"  {name} (ID: {fid})")
            return segments("\n".join(lines))

        # 板块内容
        if parsed.has('m') or parsed.has('showforum'):
            forum_name = parsed.opt('m') or parsed.opt('showforum')
            if not forum_name:
                logger.warning("未指定板块名称")
                return segments("请指定板块名称，如: /adnmb -m 综合版1")
            logger.info(f"获取板块: {forum_name}，页码: {page}")
            threads = await client.get_forum(forum_name, page)
            if not threads:
                logger.warning(f"板块 {forum_name} 不存在或无内容")
                return segments("该板块不存在或暂无内容")
            logger.info(f"获取到 {len(threads)} 个串")
            return await format_threads(threads, client, show_replies=False)

        # 串内容
        if parsed.has('c') or parsed.has('chuan'):
            thread_id = parsed.opt('c') or parsed.opt('chuan')
            if not thread_id:
                logger.warning("未指定串号")
                return segments("请指定串号，如: /adnmb -c 12345678")
            logger.info(f"获取串: {thread_id}，页码: {page}")
            thread = await client.get_thread(thread_id, page)
            if not thread:
                logger.warning(f"串 {thread_id} 不存在")
                return segments("该串不存在")
            logger.info(f"获取到串，回复数: {len(thread.replies)}")
            return await format_threads([thread], client, show_replies=True)

        # 单条回复
        if parsed.has('r') or parsed.has('ref'):
            ref_id = parsed.opt('r') or parsed.opt('ref')
            if not ref_id:
                return segments("请指定回复号，如: /adnmb -r 12345678")
            post = await client.get_ref(ref_id)
            if not post:
                return segments("该回复不存在")
            return await format_posts([post], client)

        # 查看订阅
        if parsed.has('d') or parsed.has('feed'):
            posts = await client.get_feed(page)
            if not posts:
                return segments("暂无订阅或订阅列表为空")
            return await format_posts(posts, client)

        # 添加订阅
        if parsed.has('a') or parsed.has('addfeed'):
            thread_id = parsed.opt('a') or parsed.opt('addfeed')
            if not thread_id:
                return segments("请指定要订阅的串号，如: /adnmb -a 12345678")
            result = await client.add_feed(thread_id)
            return segments(f"订阅结果: {result}")

        # 删除订阅
        if parsed.has('e') or parsed.has('delfeed'):
            thread_id = parsed.opt('e') or parsed.opt('delfeed')
            if not thread_id:
                return segments("请指定要取消订阅的串号，如: /adnmb -e 12345678")
            result = await client.del_feed(thread_id)
            return segments(f"取消订阅结果: {result}")

        # ============================================================
        # 以下功能已禁用（用户系统）
        # ============================================================

        # 验证码
        if parsed.has('v') or parsed.has('verify'):
            return segments("⚠️ 验证码功能已禁用")

        # 登录
        if parsed.has('i') or parsed.has('login'):
            return segments("⚠️ 登录功能已禁用")

        # 饼干列表
        if parsed.has('k') or parsed.has('cookie'):
            return segments("⚠️ 饼干列表功能已禁用")

        # 切换饼干
        if parsed.has('w') or parsed.has('switchcookie'):
            return segments("⚠️ 切换饼干功能已禁用")

        # 回复
        if parsed.has('y') or parsed.has('reply'):
            return segments("⚠️ 回复功能已禁用")

        # 退出登录
        if parsed.has('o') or parsed.has('logout'):
            return segments("⚠️ 退出登录功能已禁用")

        # 默认显示帮助
        return segments(_get_help())
        
    except Exception as e:
        logger.exception("adnmb handle error: %s", e)
        return segments(f"处理请求时出错: {str(e)}")


def _get_help() -> str:
    """返回帮助信息"""
    return """A岛匿名版 (adnmb) v2.0.0
══════════════════════════

📖 浏览功能:
  -t, --timeline      查看时间线
  -f, --forumlist     查看板块列表
  -m, --showforum     查看板块内容
  -c, --chuan         查看串内容
  -r, --ref           查看单条回复

📌 订阅功能:
  -d, --feed          查看订阅列表
  -a, --addfeed       添加订阅
  -e, --delfeed       删除订阅

⚙️ 通用选项:
  -p, --page          指定页码 (默认 1)
  -h, --help          显示帮助

📝 使用示例:
  /adnmb -t           查看时间线第一页
  /adnmb -t -p 2      查看时间线第二页
  /adnmb -f           查看所有板块
  /adnmb -m 综合版1   查看综合版1
  /adnmb -c 12345678  查看指定串
  /adnmb -a 12345678  订阅指定串

⚠️ 用户功能 (登录/回复等) 已禁用"""

# ============================================================
# 模块初始化
# ============================================================

# 注意：框架的 init() 钩子不传入 context，所以不使用 init() 函数
# 缓存目录在 handle() 函数中按需创建

