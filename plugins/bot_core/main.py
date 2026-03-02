"""
核心命令插件
提供 Bot 的基础管理功能
"""
import json
import logging
import re
from typing import Any, Optional

from core.plugin_base import segments
from core.args import parse

logger = logging.getLogger(__name__)

# ============================================================
# 常量配置
# ============================================================

DEFAULT_MUTE_MINUTES = 10  # 默认静音时长（分钟）
MAX_MUTE_MINUTES = 1440  # 最长静音时长（分钟，24小时）
SECRET_MASK_CHAR = "*"  # 密钥遮罩字符
METRICS_SEPARATOR = "─" * 20  # 指标显示分隔线

# ============================================================
# 插件初始化
# ============================================================

def init(context=None) -> None:
    """插件初始化"""
    pass

# ============================================================
# 主处理函数
# ============================================================

def mask_secret(value: Any) -> str:
    """遮罩敏感信息的显示
    
    Args:
        value: 要遮罩的值
        
    Returns:
        遮罩后的字符串
    """
    try:
        if isinstance(value, str):
            if len(value) <= 4:
                return SECRET_MASK_CHAR * 4
            return value[:2] + SECRET_MASK_CHAR * (len(value) - 4) + value[-2:]
        if isinstance(value, (int, float)):
            return SECRET_MASK_CHAR * 4
        if isinstance(value, list):
            return f"[<{len(value)} values>]"
        if isinstance(value, dict):
            return f"{{<{len(value)} keys>}}"
        return "[hidden]"
    except Exception as e:
        logger.error("遮罩密钥失败: %s", e)
        return "[error]"

async def handle(command: str, args: str, event: dict[str, Any], context) -> list[dict[str, Any]]:
    """命令处理入口
    
    Args:
        command: plugin.json 中定义的 command name
        args: 用户输入的参数字符串
        event: 原始事件数据
        context: 插件上下文
        
    Returns:
        消息段列表
    """
    try:
        logger.info("核心命令: %s, 参数: %s", command, args[:50] if args else '')  
        
        if command == "help":
            return _handle_help(args, context)

        if command == "reload":
            return _handle_reload(context)

        if command == "plugins":
            return _handle_plugins(context)

        if command == "闭嘴":
            return _handle_mute(args, event, context)

        if command == "说话":
            return _handle_unmute(event, context)

        if command == "set_secret":
            return _handle_set_secret(args, context)

        if command == "get_secret":
            return _handle_get_secret(args, context)

        if command == "metrics":
            return await _handle_metrics(context)

        logger.warning("未知命令: %s", command)
        return segments("❌ 未知命令")
        
    except Exception as e:
        logger.error("处理命令 %s 失败: %s", command, e, exc_info=True)
        return segments(f"❌ 命令执行失败: {e}")

# ============================================================
# 子命令处理函数
# ============================================================

def _handle_help(keyword: str, context) -> list[dict[str, Any]]:
    """显示帮助信息
    
    Args:
        keyword: 搜索关键词
        context: 插件上下文
        
    Returns:
        消息段列表
    """
    try:
        lines = context.list_commands()
        if not lines:
            logger.warning("查询帮助信息为空")
            return segments("❌ 暂无命令")

        keyword = keyword.strip().lower()

        # 如果指定了关键词，过滤相关命令
        if keyword:
            filtered_lines = _filter_help_lines(lines, keyword)

            if not filtered_lines:
                logger.info("未找到关键词 '%s' 相关的命令", keyword)
                return segments(f"❌ 未找到与 '{keyword}' 相关的命令\n💡 使用 /help 查看所有命令")

            header = f"🔍 '{keyword}' 相关命令\n{'─' * 20}\n"
            body = _format_help_lines(filtered_lines)
            logger.info("显示关键词 '%s' 的帮助: %d 行", keyword, len(filtered_lines))
            return segments(header + body)

        # 显示所有命令
        header = f"📖 命令帮助\n{'─' * 20}\n"
        body = _format_help_lines(lines)
        footer = f"\n{'─' * 20}\n💡 /help <关键词> 搜索命令"

        logger.debug("显示全部帮助: %d 行", len(lines))
        return segments(header + body + footer)
        
    except Exception as e:
        logger.error("处理 help 命令失败: %s", e, exc_info=True)
        return segments(f"❌ 查询帮助失败: {e}")

def _filter_help_lines(lines: list[str], keyword: str) -> list[str]:
    """
    过滤帮助行，以插件为单元进行过滤
    
    如果关键词匹配到插件名、任何命令或任何说明，
    则显示该插件的所有命令
    
    lines 格式:
        [插件名]        - 插件标题
          /命令         - 命令触发词  
            ↳ 说明      - 命令说明
    """
    # 第一步：按插件分组
    plugins: dict[str, list[str]] = {}
    current_plugin_name = None
    
    for line in lines:
        stripped = line.strip()
        
        # 插件标题行
        if stripped.startswith("[") and stripped.endswith("]"):
            current_plugin_name = stripped[1:-1]
            plugins[current_plugin_name] = [line]
            continue
        
        # 其他行归入当前插件
        if current_plugin_name and stripped:
            plugins[current_plugin_name].append(line)
    
    # 第二步：检查每个插件是否匹配
    filtered = []
    for plugin_name, plugin_lines in plugins.items():
        plugin_matches = False
        
        # 检查插件名是否匹配
        if keyword in plugin_name.lower():
            plugin_matches = True
        
        # 检查插件内任何行是否匹配
        if not plugin_matches:
            for line in plugin_lines[1:]:  # 跳过插件标题行
                if keyword in line.lower():
                    plugin_matches = True
                    break
        
        # 如果匹配，添加该插件的所有行
        if plugin_matches:
            filtered.extend(plugin_lines)
    
    return filtered

def _format_help_lines(lines: list[str]) -> str:
    """
    格式化帮助行，生成美观的纯文本输出
    """
    result = []
    
    for line in lines:
        stripped = line.strip()
        
        # 插件标题行 [插件名]
        if stripped.startswith("[") and stripped.endswith("]"):
            plugin_name = stripped[1:-1]
            result.append(f"\n📦 {plugin_name}")
            continue
        
        # 命令行
        if stripped.startswith("/") or (stripped and not "↳" in stripped):
            # 移除前导空格，添加命令图标
            result.append(f"  ⌘ {stripped}")
            continue
        
        # 说明行
        if "↳" in stripped:
            # 提取说明文本
            desc = stripped.replace("↳", "").strip()
            result.append(f"      {desc}")
            continue
        
        # 其他行原样保留
        if stripped:
            result.append(line)
    
    return "\n".join(result)

def _handle_reload(context) -> list[dict[str, Any]]:
    """重载配置和插件
    
    Args:
        context: 插件上下文
        
    Returns:
        消息段列表
    """
    try:
        logger.info("开始重载配置和插件")
        context.reload_config()
        context.reload_plugins()
        logger.info("配置和插件重载成功")
        return segments("✅ 配置与插件已重载")
    except Exception as e:
        logger.error("重载失败: %s", e, exc_info=True)
        return segments(f"❌ 重载失败: {e}")

def _handle_plugins(context) -> list[dict[str, Any]]:
    """列出已加载的插件
    
    Args:
        context: 插件上下文
        
    Returns:
        消息段列表
    """
    try:
        plugins = context.list_plugins()
        if not plugins:
            logger.warning("插件列表为空")
            return segments("❌ 暂无插件")

        header = f"🔌 已加载插件 ({len(plugins)}):\n"
        body = "\n".join(f"  • {name}" for name in plugins)
        logger.info("显示插件列表: %d 个", len(plugins))
        return segments(header + body)
    except Exception as e:
        logger.error("获取插件列表失败: %s", e, exc_info=True)
        return segments(f"❌ 获取插件列表失败: {e}")

def _handle_mute(args: str, event: dict[str, Any], context) -> list[dict[str, Any]]:
    """处理闭嘴命令
    
    用法:
        /闭嘴         - 默认静音 10 分钟
        /闭嘴 30      - 静音 30 分钟
        /闭嘴 1h      - 静音 1 小时
    
    Args:
        args: 命令参数
        event: 事件对象
        context: 插件上下文
        
    Returns:
        消息段列表
    """
    try:
        group_id = event.get("group_id")
        
        # 私聊不支持静音
        if group_id is None:
            logger.info("私聊不支持静音命令")
            return segments("❌ 私聊不支持此命令")
        
        # 解析时长
        duration = _parse_duration(args.strip())
        if duration <= 0:
            duration = DEFAULT_MUTE_MINUTES
        
        # 限制最大时长
        if duration > MAX_MUTE_MINUTES:
            logger.warning("静音时长超过限制: %s > %s", duration, MAX_MUTE_MINUTES)
            return segments(f"❌ 静音时长过长，最多支持 {MAX_MUTE_MINUTES//60} 小时")
        
        # 执行静音
        context.mute_group(group_id, duration)
        
        # 生成友好的时间显示
        if duration >= 60:
            hours = duration / 60
            time_str = f"{hours:.1f} 小时" if hours != int(hours) else f"{int(hours)} 小时"
        else:
            time_str = f"{int(duration)} 分钟"
        
        logger.info("群 %s 设置静音: %s 分钟", group_id, duration)
        return segments(f"🤐 好的，我会安静 {time_str}")
        
    except Exception as e:
        logger.error("处理静音命令失败: %s", e, exc_info=True)
        return segments(f"❌ 设置静音失败: {e}")

def _handle_unmute(event: dict[str, Any], context) -> list[dict[str, Any]]:
    """处理说话命令
    
    Args:
        event: 事件对象
        context: 插件上下文
        
    Returns:
        消息段列表
    """
    try:
        group_id = event.get("group_id")
        
        if group_id is None:
            logger.info("私聊不支持说话命令")
            return segments("❌ 私聊不支持此命令")
        
        # 检查是否在静音中
        remaining = context.get_mute_remaining(group_id)
        if remaining <= 0:
            logger.info("群 %s 未在静音中", group_id)
            return segments("😊 我本来就没闭嘴啊~")
        
        # 解除静音
        context.unmute_group(group_id)
        logger.info("群 %s 解除静音，剩余 %.1f 分钟", group_id, remaining)
        return segments("😊 好的，我又可以说话啦！")
        
    except Exception as e:
        logger.error("处理说话命令失败: %s", e, exc_info=True)
        return segments(f"❌ 解除静音失败: {e}")

def _handle_set_secret(args: str, context) -> list[dict[str, Any]]:
    """设置 secrets 中的某个值
    
    用法:
        /set_secret plugins.signin.yingshijufeng.sid NEW_VALUE
        /设置密钥 plugins.signin.yingshijufeng.sid NEW_VALUE
    
    Args:
        args: 命令参数
        context: 插件上下文
        
    Returns:
        消息段列表
    """
    parts = args.strip().split(maxsplit=1)
    if len(parts) != 2:
        return segments(
            "❌ 用法: /set_secret <路径> <值>\n\n"
            "示例:\n"
            "  /set_secret plugins.signin.yingshijufeng.sid YZ123456\n"
            "  /set_secret admin_user_ids [123456,789012]\n\n"
            "💡 提示: 使用 /get_secret 查看现有配置路径"
        )
    
    path, value = parts
    
    try:
        # 验证路径格式
        if not re.match(r'^[a-zA-Z0-9_]+(\.[a-zA-Z0-9_]+)*$', path):
            return segments("❌ 路径格式错误，请使用 . 分隔，如: plugins.signin.sid")
        
        # 尝试解析为 JSON（支持设置数字、布尔值等）
        try:
            parsed_value = json.loads(value)
        except json.JSONDecodeError:
            # 如果不是有效 JSON，就作为字符串处理
            parsed_value = value
        
        # 检查 config_manager 是否可用
        if not context.config_manager:
            logger.error("ConfigManager 不可用")
            return segments("❌ ConfigManager 不可用")
        
        # 更新配置
        context.config_manager.update_secret(path, parsed_value)
        
        # 触发配置重载
        context.reload_config()
        
        logger.info("已更新配置: %s = %s", path, mask_secret(parsed_value))
        return segments(f"✅ 已更新 {path}\n新值: {mask_secret(parsed_value)}")
    except KeyError as exc:
        # 路径不存在
        logger.warning("配置路径不存在: %s", path)
        return segments(f"❌ {exc}\n\n💡 提示: 使用 /get_secret 查看现有配置路径")
    except ValueError as exc:
        # 路径类型错误
        logger.warning("配置路径类型错误: %s", path)
        return segments(f"❌ {exc}")
    except Exception as exc:
        logger.error("设置密钥失败: %s", exc, exc_info=True)
        return segments(f"❌ 更新失败: {exc}")

def _handle_get_secret(args: str, context) -> list[dict[str, Any]]:
    """查看 secrets 中的某个值
    
    用法:
        /get_secret plugins.signin.yingshijufeng.sid
        /查看密钥 plugins.signin.yingshijufeng.sid
    
    Args:
        args: 命令参数
        context: 插件上下文
        
    Returns:
        消息段列表
    """
    path = args.strip()
    if not path:
        return segments(
            "❌ 用法: /get_secret <路径>\n\n"
            "示例:\n"
            "  /get_secret plugins.signin.yingshijufeng.sid\n"
            "  /get_secret admin_user_ids\n\n"
            "💡 提示: 使用 /get_secret plugins 查看插件配置列表"
        )
    
    try:
        keys = path.split(".")
        current = context.secrets
        
        for i, key in enumerate(keys):
            if not isinstance(current, dict):
                current_path = ".".join(keys[:i])
                logger.warning("配置路径无效: %s 不是字典", current_path)
                return segments(f"❌ 路径 {current_path} 不是字典类型")
            if key not in current:
                logger.info("配置路径不存在: %s", path)
                return segments(f"❌ 路径 {path} 不存在")
            current = current[key]
        
        if isinstance(current, dict):
            keys_list = list(current.keys())
            if len(keys_list) > 20:
                display_keys = keys_list[:20]
                suffix = f"... 还有 {len(keys_list) - 20} 个"
            else:
                display_keys = keys_list
                suffix = ""
            logger.info("查询配置目录: %s, %d 个键", path, len(keys_list))
            return segments(f"🔑 {path} 包含以下键:\n  {', '.join(display_keys)}{suffix}")
        
        if isinstance(current, list):
            logger.info("查询配置列表: %s, %d 个元素", path, len(current))
            return segments(f"🔑 {path} = {mask_secret(current)}")
        
        logger.info("查询配置值: %s", path)
        return segments(f"🔑 {path} = {mask_secret(current)}")
    except Exception as exc:
        logger.error("查询密钥失败: %s", exc, exc_info=True)
        return segments(f"❌ 查询失败: {exc}")

async def _handle_metrics(context) -> list[dict[str, Any]]:
    """查看运行指标
    
    Args:
        context: 插件上下文
        
    Returns:
        消息段列表
    """
    try:
        metrics = getattr(context, "metrics", None)
        if not metrics:
            logger.warning("Metrics 未启用")
            return segments("❌ Metrics 未启用")

        summary = await metrics.get_summary()
        if not summary:
            logger.warning("无法获取 Metrics 数据")
            return segments("❌ 无法获取 Metrics 数据")
        
        global_stats = summary.get("global", {})
        lines = [
            "📈 运行指标",
            METRICS_SEPARATOR,
            f"⏱️ 运行时间: {summary.get('uptime_seconds', 0):.0f}s",
            f"📦 总调用: {global_stats.get('total_calls', 0)}",
            f"✅ 成功率: {global_stats.get('success_rate', 1)*100:.1f}%",
            f"⏳ 平均耗时: {global_stats.get('avg_time', 0):.3f}s",
            f"🐢 慢调用: {global_stats.get('slow_calls', 0)}",
            f"❌ 错误: {global_stats.get('errors', 0)}",
        ]

        top_slow = summary.get("top_slow_plugins", [])
        if top_slow:
            lines.append(METRICS_SEPARATOR)
            lines.append("⚠️ 最慢插件:")
            for item in top_slow[:5]:  # 限制显示5个
                plugin_name = item.get('plugin', '-')
                avg_time = item.get('avg_time', 0)
                lines.append(f"  • {plugin_name}: {avg_time:.3f}s")

        logger.info("查询运行指标")
        return segments("\n".join(lines))
        
    except Exception as e:
        logger.error("查询 Metrics 失败: %s", e, exc_info=True)
        return segments(f"❌ 查询 Metrics 失败: {e}")

def _parse_duration(text: str) -> float:
    """解析时长字符串
    
    支持格式:
        10      -> 10 分钟
        30m     -> 30 分钟
        1h      -> 60 分钟
        1.5h    -> 90 分钟
    
    Args:
        text: 时长字符串
        
    Returns:
        时长(分钟)，解析失败返回 0
    """
    if not text:
        return 0
    
    text = text.lower().strip()
    
    try:
        # 小时格式
        if text.endswith("h") or text.endswith("小时"):
            num = re.sub(r'[h小时]+$', '', text)
            hours = float(num)
            return hours * 60
        
        # 分钟格式
        if text.endswith("m") or text.endswith("分钟") or text.endswith("min"):
            num = re.sub(r'(min|分钟|m)$', '', text)
            return float(num)
        
        # 纯数字默认为分钟
        minutes = float(text)
        return minutes
    except ValueError as e:
        logger.warning("解析时长失败: %s, 错误: %s", text, e)
        return 0
