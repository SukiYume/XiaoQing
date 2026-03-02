"""
arXiv 论文筛选插件
基于 BERT 模型筛选感兴趣的 arXiv 论文
"""
import datetime
import json
import logging
import os

from core.plugin_base import segments, run_sync
from core.args import parse


logger = logging.getLogger(__name__)


# ============================================================
# 配置管理
# ============================================================

def _load_config(plugin_dir: str) -> dict:
    """加载插件配置文件"""
    config_path = os.path.join(plugin_dir, "config.json")
    logger.debug(f"尝试加载配置文件: {config_path}")
    if os.path.exists(config_path):
        try:
            with open(config_path, 'r', encoding='utf-8') as f:
                config = json.load(f)
                logger.info(f"成功加载配置文件，包含 {len(config)} 个配置项")
                return config
        except Exception as e:
            logger.warning(f"加载配置文件失败: {e}，使用默认配置")
    else:
        logger.warning(f"配置文件不存在: {config_path}，使用默认配置")
    return {}


# ============================================================
# 模块加载
# ============================================================

_inference_func = None


def _load_inference(plugin_dir: str, force_reload: bool = False):
    """
    动态加载推理模块
    
    Args:
        plugin_dir: 插件目录路径
        force_reload: 是否强制重新加载
        
    Returns:
        推理函数或 None
    """
    global _inference_func
    
    if _inference_func is not None and not force_reload:
        logger.debug("使用已缓存的推理函数")
        return _inference_func

    logger.info(f"开始加载推理模块，force_reload={force_reload}")
    
    # 强制重新加载时，先清除已导入的模块
    if force_reload:
        import importlib
        try:
            from . import arxiv_title_inference
            importlib.reload(arxiv_title_inference)
            logger.info("已重新加载 arxiv_title_inference 模块")
        except ImportError as e:
            logger.debug(f"未找到已加载的模块: {e}")

    try:
        from .arxiv_title_inference import get_positive_arxiv_today_as_string
        _inference_func = get_positive_arxiv_today_as_string
        logger.info("成功加载 arxiv_title_inference 模块")
        return _inference_func
    except ImportError as e:
        logger.error(f"导入 arxiv_title_inference 模块失败: {e}")
        logger.error(f"请确保安装了所需依赖: tensorflow, transformers")
        return None
    except Exception as e:
        logger.exception(f"加载推理模块时发生异常: {e}")
        return None


def init(context=None) -> None:
    """
    插件初始化
    
    清除模块缓存，下次调用时重新加载推理模块。
    这确保插件更新后能立即生效。
    
    Args:
        context: 插件上下文（可选）
    """
    global _inference_func
    _inference_func = None
    logger.info("arXiv Filter 插件已初始化")


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
        
        return await _run_filter(context)
        
    except Exception as e:
        logger.exception("ArXiv Filter handle error: %s", e)
        return segments(f"处理请求时出错: {str(e)}")


async def scheduled(context) -> list:
    """定时任务入口"""
    return await _run_filter(context)


async def scheduled_check(context) -> list:
    """定时检查 arXiv 是否更新"""
    return await _check_arxiv_update(context, is_final_check=False)


async def scheduled_final_check(context) -> list:
    """最后一次检查（12点），如果仍未更新则发送停更通知"""
    return await _check_arxiv_update(context, is_final_check=True)


async def _run_filter(context) -> list:
    """
    执行论文筛选
    
    Args:
        context: 插件上下文
        
    Returns:
        消息段列表
    """
    plugin_dir = str(context.plugin_dir)
    
    # 加载配置
    config = _load_config(plugin_dir)
    model_config = config.get("model", {})
    model_path = os.path.join(plugin_dir, model_config.get("path", "best_model"))

    # 加载推理函数
    inference = _load_inference(plugin_dir)
    if inference is None:
        error_msg = "⚠️ 无法加载AI模型，请检查插件配置。"
        logger.error("加载推理模块失败")
        return segments(error_msg)

    # 检查模型路径
    if not os.path.exists(model_path):
        error_msg = f"⚠️ AI模型未找到，请联系管理员。"
        logger.error(f"模型路径不存在: {model_path}")
        return segments(error_msg)

    def _do_inference():
        """执行推理（阻塞操作）"""
        return inference(model_path=model_path)

    try:
        logger.info(f"开始执行 arXiv 论文筛选，模型路径: {model_path}")
        import time
        start_time = time.time()
        arxiv_text = await run_sync(_do_inference)
        elapsed = time.time() - start_time
        logger.info(f"arXiv 论文筛选完成，耗时 {elapsed:.2f} 秒，返回内容长度: {len(arxiv_text)} 字符")
        
        # 检查是否有错误消息
        if arxiv_text.startswith("Error:"):
            logger.error(f"推理过程返回错误: {arxiv_text}")
            return segments("❌ 论文获取失败，请稍后再试。")
        
        # 检查是否没有结果
        if "No positive predictions" in arxiv_text:
            logger.info("今日没有符合条件的论文")
            today = datetime.date.today()
            return segments(f"📚 今天是 {today}，暂时没有发现感兴趣的论文。")
        
        logger.debug(f"筛选结果预览: {arxiv_text[:200]}...")
        
    except FileNotFoundError as e:
        logger.error(f"文件未找到: {e}")
        logger.error(f"检查的路径: {model_path}")
        return segments("❌ 模型文件不完整，请联系管理员。")
    except ImportError as e:
        logger.error(f"缺少依赖库: {e}")
        logger.error("请安装: pip install tensorflow transformers")
        return segments("❌ 系统依赖不完整，请联系管理员。")
    except Exception as exc:
        logger.exception(f"arXiv 筛选器运行异常: {exc}")
        logger.error(f"模型路径: {model_path}")
        return segments("❌ 论文筛选服务暂时不可用，请稍后再试。")

    # 格式化输出
    today = datetime.date.today()
    header = f"📚 今天是 {today}，以下是你可能感兴趣的论文：\n"
    return segments(header + arxiv_text)


def _show_help() -> str:
    """显示帮助信息"""
    return """
📚 **arXiv 论文筛选**

基于 AI 模型自动筛选今日感兴趣的 arXiv 论文

**使用方法:**
• /arxiv - 获取今日筛选的论文
• /arxiv help - 显示帮助信息

**功能特点:**
- 基于 BERT 模型智能筛选
- 自动获取最新论文
- 根据研究兴趣推荐

输入 /arxiv 查看今日推荐论文
""".strip()


# ============================================================
# 状态管理
# ============================================================

def _get_status_file_path(plugin_dir: str) -> str:
    """获取状态文件路径"""
    data_dir = os.path.join(plugin_dir, "data")
    os.makedirs(data_dir, exist_ok=True)
    return os.path.join(data_dir, "update_status.json")


def _load_update_status(plugin_dir: str) -> dict:
    """加载今日更新状态"""
    status_file = _get_status_file_path(plugin_dir)
    if os.path.exists(status_file):
        try:
            with open(status_file, 'r', encoding='utf-8') as f:
                return json.load(f)
        except Exception as e:
            logger.warning(f"加载状态文件失败: {e}")
    return {}


def _save_update_status(plugin_dir: str, status: dict) -> None:
    """保存今日更新状态"""
    status_file = _get_status_file_path(plugin_dir)
    try:
        with open(status_file, 'w', encoding='utf-8') as f:
            json.dump(status, f, ensure_ascii=False, indent=2)
    except Exception as e:
        logger.error(f"保存状态文件失败: {e}")


def _should_send_today(plugin_dir: str) -> bool:
    """检查今天是否已经发送过"""
    status = _load_update_status(plugin_dir)
    today = datetime.date.today().isoformat()
    return status.get("last_sent_date") != today


def _mark_sent_today(plugin_dir: str) -> None:
    """标记今天已发送"""
    today = datetime.date.today().isoformat()
    status = {
        "last_sent_date": today,
        "last_sent_time": datetime.datetime.now().isoformat()
    }
    _save_update_status(plugin_dir, status)


# ============================================================
# arXiv 更新检查
# ============================================================

async def _check_arxiv_update(context, is_final_check: bool = False) -> list:
    """
    检查 arXiv 是否更新
    
    Args:
        context: 插件上下文
        is_final_check: 是否是最后一次检查（12点）
        
    Returns:
        消息段列表
    """
    plugin_dir = str(context.plugin_dir)
    
    # 检查今天是否已经发送过
    if not _should_send_today(plugin_dir):
        logger.info("今天已经发送过 arXiv 更新，跳过此次检查")
        return []
    
    # 检查 arXiv 页面日期
    def _check_date():
        """检查日期（阻塞操作）"""
        from .arxiv_today import check_arxiv_update_date
        return check_arxiv_update_date()
    
    try:
        arxiv_date = await run_sync(_check_date)
    except Exception as e:
        logger.error(f"检查 arXiv 日期时出错: {e}")
        return []
    
    today = datetime.date.today().isoformat()
    
    # 如果 arXiv 已更新到今天
    if arxiv_date == today:
        logger.info(f"检测到 arXiv 已更新到 {today}，开始筛选论文...")
        _mark_sent_today(plugin_dir)
        return await _run_filter(context)
    
    # 如果是最后一次检查且仍未更新
    if is_final_check:
        logger.info(f"最后检查时间已到，arXiv 仍未更新（当前日期: {arxiv_date}），发送停更通知")
        _mark_sent_today(plugin_dir)
        return segments(f"📚 arXiv 今日（{today}）暂未更新，可能稍后更新或今日停更。")
    
    # 还不是最后检查，继续等待
    logger.info(f"arXiv 尚未更新到今天（当前: {arxiv_date}，期望: {today}），等待下次检查")
    return []
