"""
Jupyter 代码执行插件 v2.0

功能：
- /jupyter (py): 执行 Python 代码
- /jupyter_kernel (kernel): 管理内核
- /jupyter repl: 交互式 REPL 模式

架构：
- main.py: 命令处理
- manager.py: 内核管理和执行逻辑
- models.py: 数据模型
- config.py: 配置常量
"""
import asyncio
import logging
import re
import sys
from pathlib import Path
from typing import Any

from core.plugin_base import segments, text, image, PluginContextProtocol
from core.args import parse

# 使用相对导入
from .jupyter_manager import JupyterKernelManager, lazy_import_jupyter, JUPYTER_AVAILABLE, IMPORT_ERROR
from .jupyter_config import DEFAULT_TIMEOUT

logger = logging.getLogger(__name__)

# ============================================================
# 插件初始化
# ============================================================

def init(context=None) -> None:
    """插件初始化"""
    lazy_import_jupyter()
    if JUPYTER_AVAILABLE:
        logger.info("Jupyter plugin initialized (jupyter_client available)")
    else:
        logger.warning("Jupyter plugin initialized (jupyter_client NOT available: %s)", IMPORT_ERROR)

# ============================================================
# 命令处理
# ============================================================

async def handle(
    command: str,
    args: str,
    event: dict[str, Any],
    context: PluginContextProtocol
) -> list[dict[str, Any]]:
    """命令处理入口"""
    
    try:
        # 检查依赖
        lazy_import_jupyter()
        if not JUPYTER_AVAILABLE:
            # 再次检查状态，避免误报
            from .jupyter_manager import JUPYTER_AVAILABLE as READY, IMPORT_ERROR as ERR
            if not READY:
                return segments(f"❌ jupyter_client 加载失败: {ERR}\n请运行: pip install jupyter_client ipykernel")
        
        parsed = parse(args)
        
        # 主 Jupyter 命令
        if command in {"jupyter", "py", "python", "exec"}:
            # 检查子命令
            if parsed and parsed.first and parsed.first.lower() in {
                "help", "帮助", "?", "repl", "interactive", "交互"
            }:
                subcommand = parsed.first.lower()
                
                if subcommand in {"help", "帮助", "?"}:
                    return segments(_show_help())
                elif subcommand in {"repl", "interactive", "交互"}:
                    return await _start_repl_session(context)
            
            # 默认：执行代码
            return await _handle_execute(args, context)
        
        # 内核管理命令
        elif command in {"kernel", "内核", "jupyter_kernel"}:
            return await _handle_kernel(args, context)
        
        return segments("未知命令")
        
    except Exception as e:
        logger.exception("Jupyter handle error: %s", e)
        return segments(f"处理请求时出错: {str(e)}")

async def _handle_execute(args: str, context: PluginContextProtocol) -> list[dict[str, Any]]:
    """处理代码执行命令"""
    code, timeout = extract_code_and_timeout(args)
    if not code:
        return segments("请输入要执行的代码\n用法: /py print('hello')\n\n输入 /py help 查看完整帮助")
    
    try:
        # 获取内核管理器
        km = JupyterKernelManager.get_instance(context.data_dir, _owner_key(context))
        
        logger.info("Executing Jupyter code: %s (timeout=%s)", code[:50], timeout)
        
        # 执行代码
        result = await km.execute(code, timeout=timeout)
        
        # 构建响应
        response: list[dict[str, Any]] = []
        
        # 文本输出
        output_text = result.format_output()
        response.append(text(f"```\n{output_text}\n```"))
        
        # 图片输出
        for img_path in result.images:
            response.append(image(str(img_path)))
        
        return response
        
    except Exception as e:
        logger.exception("Jupyter execution failed")
        return segments(f"❌ 执行失败: {e}")

async def _handle_kernel(args: str, context: PluginContextProtocol) -> list[dict[str, Any]]:
    """处理内核管理命令"""
    action = args.strip().lower()
    
    if not action or action in ["status", "状态"]:
        km = JupyterKernelManager.get_instance(context.data_dir, _owner_key(context))
        status = km.get_status()
        
        if status["running"]:
            return segments(f"🟢 {status['message']}")
        else:
            return segments(f"⚫ {status['message']}")
    
    elif action in ["start", "启动"]:
        try:
            km = JupyterKernelManager.get_instance(context.data_dir, _owner_key(context))
            await asyncio.to_thread(km.start_kernel)
            logger.info("Jupyter kernel started")
            return segments("🟢 内核已启动")
        except Exception as e:
            logger.error("Kernel start failed: %s", e)
            return segments(f"❌ 启动失败: {e}")
    
    elif action in ["restart", "重启"]:
        try:
            km = JupyterKernelManager.get_instance(context.data_dir, _owner_key(context))
            await asyncio.to_thread(km.restart_kernel)
            logger.info("Jupyter kernel restarted")
            return segments("🔄 内核已重启")
        except Exception as e:
            logger.error("Kernel restart failed: %s", e)
            return segments(f"❌ 重启失败: {e}")
    
    elif action in ["shutdown", "stop", "关闭", "停止"]:
        km = JupyterKernelManager.get_instance(context.data_dir, _owner_key(context))
        await asyncio.to_thread(km.shutdown_kernel)
        logger.info("Jupyter kernel shutdown")
        return segments("⚫ 内核已关闭")
    
    elif action in ["help", "帮助", "-h", "?"]:
        return segments(_show_kernel_help())
    
    else:
        return segments(f"未知操作: {action}\n使用 /kernel help 查看帮助")

async def _start_repl_session(context: PluginContextProtocol) -> list[dict[str, Any]]:
    """启动交互式 REPL 会话"""
    # 检查是否已有会话
    existing_session = await context.get_session()
    if existing_session:
        code_buffer = existing_session.get("code_buffer", [])
        buffer_preview = "\n".join(code_buffer[-5:]) if code_buffer else "（空）"
        return segments(
            "📝 你已在 Jupyter REPL 会话中\n"
            f"当前缓冲区（最后5行）:\n```python\n{buffer_preview}\n```\n\n"
            "继续输入代码，或:\n"
            "• 输入「run」/「执行」运行代码\n"
            "• 输入「clear」/「清空」清空缓冲区\n"
            "• 输入「show」/「显示」查看完整缓冲区\n"
            "• 输入「退出」/「取消」结束会话"
        )
    
    # 创建新会话
    await context.create_session(
        initial_data={
            "code_buffer": [],
            "execution_count": 0,
        },
        timeout=600.0,  # 10分钟超时
    )
    
    logger.info("Started Jupyter REPL session: user=%s", context.current_user_id)
    
    return segments(
        "📝 Jupyter 交互式 REPL 已启动\n"
        "━━━━━━━━━━━━━━━━━━\n"
        "💡 连续输入多行代码\n"
        "💡 输入「run」/「执行」运行代码\n"
        "💡 输入「clear」/「清空」清空缓冲区\n"
        "💡 输入「show」/「显示」查看缓冲区\n"
        "💡 输入「退出」/「取消」结束会话\n"
        "━━━━━━━━━━━━━━━━━━\n"
        "现在可以开始输入代码..."
    )

# ============================================================
# 会话处理（多轮对话核心）
# ============================================================

async def handle_session(
    user_text: str,
    event: dict[str, Any],
    context: PluginContextProtocol,
    session,
) -> list[dict[str, Any]]:
    """
    处理会话消息
    
    当用户有活跃会话时，Dispatcher 会调用这个函数处理后续消息。
    框架已自动处理退出命令（退出/取消/exit/quit/q），插件无需再处理。
    
    参数:
        user_text: 用户发送的原始文本
        event: OneBot 事件
        context: 插件上下文
        session: 当前会话对象
    """
    user_input = user_text.strip()
    code_buffer = session.get("code_buffer", [])
    execution_count = session.get("execution_count", 0)
    
    # 特殊命令
    if user_input.lower() in {"run", "执行", "运行"}:
        if not code_buffer:
            return segments("⚠️ 缓冲区为空，请先输入代码")
        
        # 执行缓冲区中的代码
        code = "\n".join(code_buffer)
        
        try:
            km = JupyterKernelManager.get_instance(context.data_dir, _owner_key(context))
            
            logger.info("Executing REPL code: %d lines, user=%s", len(code_buffer), context.current_user_id)
            
            result = await km.execute(code, timeout=DEFAULT_TIMEOUT)
            
            # 清空缓冲区并更新计数
            session.set("code_buffer", [])
            session.set("execution_count", execution_count + 1)
            
            # 构建响应
            response: list[dict[str, Any]] = []
            response.append(text(f"✅ 执行完成 (#{execution_count + 1})"))
            
            # 文本输出
            output_text = result.format_output()
            response.append(text(f"```\n{output_text}\n```"))
            
            # 图片输出
            for img_path in result.images:
                response.append(image(str(img_path)))
            
            response.append(text("继续输入代码，或输入「退出」结束会话"))
            
            return response
            
        except Exception as e:
            logger.exception("REPL execution failed")
            return segments(f"❌ 执行失败: {e}\n\n缓冲区已保留，可以修改后重试")
    
    elif user_input.lower() in {"clear", "清空", "reset"}:
        session.set("code_buffer", [])
        return segments("🗑️ 缓冲区已清空")
    
    elif user_input.lower() in {"show", "显示", "buffer", "缓冲区"}:
        if not code_buffer:
            return segments("📄 缓冲区为空")
        
        code = "\n".join(code_buffer)
        return segments(
            f"📄 当前缓冲区 ({len(code_buffer)} 行):\n"
            f"```python\n{code}\n```\n\n"
            "输入「run」执行，「clear」清空"
        )
    
    elif user_input.lower() in {"help", "帮助", "?"}:
        return segments(
            "📝 Jupyter REPL 帮助\n"
            "━━━━━━━━━━━━━━━━━━\n"
            "💡 直接输入代码添加到缓冲区\n"
            "💡 输入「run」/「执行」运行代码\n"
            "💡 输入「show」/「显示」查看缓冲区\n"
            "💡 输入「clear」/「清空」清空缓冲区\n"
            "💡 输入「退出」/「取消」结束会话\n"
            "━━━━━━━━━━━━━━━━━━"
        )
    
    # 默认：添加到代码缓冲区
    code_buffer.append(user_input)
    session.set("code_buffer", code_buffer)
    
    return segments(
        f"✓ 已添加 (共 {len(code_buffer)} 行)\n"
        "输入「run」执行，「show」查看，「clear」清空"
    )

# ============================================================
# 帮助信息
# ============================================================

def _show_help() -> str:
    """显示帮助信息"""
    return """
📓 **Jupyter 代码执行器**

**基本命令:**
• /py <代码> - 执行单行Python代码
• /py help - 显示此帮助
• /py repl - 启动交互式REPL模式

**执行选项:**
• /py -t 60 <代码> - 设置超时时间（秒）
• /py --timeout 30 <代码> - 同上

**交互式 REPL:**
• /py repl - 进入多行代码输入模式
• 在 REPL 中连续输入多行代码
• 输入「run」执行，「clear」清空
• 输入「退出」/「取消」结束会话

**内核管理:**
• /kernel status - 查看内核状态
• /kernel restart - 重启内核（清除所有变量）
• /kernel shutdown - 关闭内核

**示例:**
• /py print("Hello, World!")
• /py import numpy as np; np.random.rand(3)
• /py import matplotlib.pyplot as plt; plt.plot([1,2,3])
• /py repl （进入交互模式）

**特性:**
• 支持自动显示图表（matplotlib等）
• 变量在内核重启前持久保存
• 支持长时间运行的代码（可设置超时）
• 10分钟无操作自动关闭内核
""".strip()

def _show_kernel_help() -> str:
    """返回内核管理帮助"""
    return """
🔧 **Jupyter 内核管理**

**命令:**
• /kernel status - 查看内核状态
• /kernel start - 启动内核
• /kernel restart - 重启内核
• /kernel shutdown - 关闭内核

**说明:**
• 内核会自动按需启动
• 重启会清除所有变量
• 关闭后再执行会自动重启
• 10分钟无操作自动关闭
""".strip()

# ============================================================
# 生命周期钩子
# ============================================================

async def shutdown(context: PluginContextProtocol) -> None:
    """插件卸载/关闭时的清理"""
    try:
        lazy_import_jupyter()
        # 再次检查状态
        from .jupyter_manager import JUPYTER_AVAILABLE as READY, KernelManager as KM_CLS
        
        if READY and KM_CLS:
            logger.info("正在关闭 Jupyter 内核...")
            await asyncio.to_thread(JupyterKernelManager.shutdown_all)
    except Exception as e:
        logger.error("关闭 Jupyter 内核失败: %s", e)


def _owner_key(context: PluginContextProtocol) -> str:
    user_id = getattr(context, "current_user_id", None)
    return str(user_id) if user_id is not None else "global"


def extract_code_and_timeout(args: str) -> tuple[str, float]:
    """
    提取代码和超时参数

    参数必须在字符串开头，支持的格式：
    - /py -t 30 code...
    - /py --timeout 30 code...
    - /py --timeout=30 code...
    - /py -t 30 -t 60 code...  (多个参数时，最后一个生效)

    重要：参数只在开头解析，之后的所有内容都被视为代码。
    这样可以避免误删用户代码中的 -t 字符串。

    示例：
    - "/py -t 30 print('hello')" → timeout=30, code="print('hello')"
    - "/py print('-t 30')" → timeout=30(默认), code="print('-t 30')"
    - "/py -t 60 x = -t" → timeout=60, code="x = -t"
    """
    timeout = DEFAULT_TIMEOUT
    remaining = args.strip()

    # 循环处理开头的所有参数
    # 每次循环尝试从开头匹配一个参数
    # 一旦开头不是参数格式，就停止（剩余的都是代码）
    while remaining:
        # 匹配格式：-t <number> 或 --timeout <number> 或 --timeout=<number>
        # ^ 确保只匹配字符串开头
        match = re.match(
            r'^\s*(?:-t|--timeout)(?:\s+|=)(\d+(?:\.\d+)?)\s*',
            remaining,
            re.IGNORECASE
        )

        if not match:
            # 开头不是参数格式，停止解析
            break

        # 提取超时值（使用第一个捕获组）
        try:
            timeout = float(match.group(1))
        except (ValueError, IndexError):
            pass

        # 移除已处理的参数，继续检查是否还有参数
        remaining = remaining[match.end():]

    # remaining 就是代码部分（可能为空）
    return remaining, timeout
