"""
中国传统色彩查询插件
提供颜色查询、转换和可视化功能
"""
import re
import logging
from pathlib import Path

from core.plugin_base import segments, text, image, ensure_dir
from core.args import parse

# 使用相对导入引入各个功能模块
from . import convert
from . import data_manager
from . import query
from . import image_gen
from . import stellar


logger = logging.getLogger(__name__)


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
        
        logger.info(f"颜色查询命令: {command} {args[:50]}{'...' if len(args) > 50 else ''}")
        
        # 加载颜色数据
        colors = data_manager.load_colors(context)
        if not colors:
            return segments("❌ 颜色数据加载失败，请检查插件配置")
        
        # 确保输出目录存在
        img_dir = context.data_dir / "images"
        ensure_dir(img_dir)
        
        # 空命令或帮助信息
        if not parsed or parsed.first.lower() in ['help', 'h', 'list', 'l', '帮助']:
            return segments(_get_help())
        
        # 按名称查询
        if parsed.has('n') or parsed.has('name'):
            name = parsed.opt('n') or parsed.opt('name')
            color = query.find_by_name(colors, name)
            if color:
                result = [text(data_manager.format_color_info(color))]
                if parsed.has('p') or parsed.has('picture'):
                    img_path = await image_gen.generate_color_image(color['name'], color['RGB'], img_dir, context)
                    if img_path:
                        result.append(image(img_path))
                logger.info(f"查询颜色名称: {name} - 找到")
                return result
            logger.info(f"查询颜色名称: {name} - 未找到")
            return segments(f"中国色里没有收录「{name}」这个颜色哦。")
        
        # 按 RGB 查询
        if parsed.has('r') or parsed.has('rgb'):
            rgb_str = parsed.opt('r') or parsed.opt('rgb')
            try:
                rgb = [int(i) for i in re.split(r'[,\s]+', rgb_str) if i]
                is_valid, error_msg = convert.validate_rgb(rgb)
                if not is_valid:
                    return segments(f"❌ {error_msg}")
            except ValueError as e:
                return segments(f"❌ RGB 格式错误：{e}\n请使用格式: 255,128,0 或 255 128 0")
            
            color = query.find_by_rgb(colors, rgb)
            if color:
                result = [text(data_manager.format_color_info(color))]
                if parsed.has('p') or parsed.has('picture'):
                    img_path = await image_gen.generate_color_image(color['name'], rgb, img_dir, context)
                    if img_path:
                        result.append(image(img_path))
                logger.info(f"查询 RGB: {rgb} - 找到: {color['name']}")
                return result
            
            # 没找到，但可以显示这个颜色
            logger.info(f"查询 RGB: {rgb} - 未找到，显示预览")
            img_path = await image_gen.generate_color_image('自定义颜色', rgb, img_dir, context)
            result = [text(f"中国色里没有收录这个颜色哦。\nRGB: {rgb}\nHEX: {convert.rgb_to_hex(rgb)}")]
            if img_path:
                result.insert(0, text("虽然没有收录，这个颜色长这个样子："))
                result.append(image(img_path))
            return result
        
        # 按 HEX 查询
        if parsed.has('x') or parsed.has('hex'):
            hex_value = parsed.opt('x') or parsed.opt('hex')
            try:
                rgb = convert.hex_to_rgb(hex_value)  # 验证格式
            except ValueError as e:
                return segments(f"❌ HEX 格式错误：{e}\n请使用格式: #FF5733 或 FF5733")
            
            color = query.find_by_hex(colors, hex_value)
            if color:
                result = [text(data_manager.format_color_info(color))]
                if parsed.has('p') or parsed.has('picture'):
                    img_path = await image_gen.generate_color_image(color['name'], color['RGB'], img_dir, context)
                    if img_path:
                        result.append(image(img_path))
                logger.info(f"查询 HEX: {hex_value} - 找到: {color['name']}")
                return result
            
            logger.info(f"查询 HEX: {hex_value} - 未找到")
            return segments(f"中国色里没有收录这个颜色哦。\nHEX: {hex_value}\nRGB: {rgb}")
        
        # 按 CMYK 查询
        if parsed.has('c') or parsed.has('cmyk'):
            cmyk_str = parsed.opt('c') or parsed.opt('cmyk')
            try:
                cmyk = [int(i) for i in re.split(r'[,\s]+', cmyk_str) if i]
                is_valid, error_msg = convert.validate_cmyk(cmyk)
                if not is_valid:
                    return segments(f"❌ {error_msg}")
            except ValueError as e:
                return segments(f"❌ CMYK 格式错误：{e}\n请使用格式: 0,100,100,0")
            
            color = query.find_by_cmyk(colors, cmyk)
            if color:
                result = [text(data_manager.format_color_info(color))]
                if parsed.has('p') or parsed.has('picture'):
                    img_path = await image_gen.generate_color_image(color['name'], color['RGB'], img_dir, context)
                    if img_path:
                        result.append(image(img_path))
                context.logger.info(f"查询 CMYK: {cmyk} - 找到: {color['name']}")
                return result
            
            context.logger.info(f"查询 CMYK: {cmyk} - 未找到")
            return segments("中国色里没有收录这个颜色哦。")
        
        # 按色系搜索
        if parsed.has('a') or parsed.has('accord'):
            keyword = parsed.opt('a') or parsed.opt('accord')
            matches = query.find_by_keyword(colors, keyword)
            if matches:
                names = [c['name'] for c in matches[:query.MAX_SEARCH_RESULTS]]
                suffix = f"\n... 共 {len(matches)} 个" if len(matches) > query.MAX_SEARCH_RESULTS else ""
                logger.info(f"搜索色系: {keyword} - 找到 {len(matches)} 个")
                return segments("，".join(names) + suffix)
            logger.info(f"搜索色系: {keyword} - 未找到")
            return segments(f"中国色里没有收录「{keyword}」色系哦。")
        
        # 添加自定义颜色
        if parsed.has('w') or parsed.has('write'):
            definition = parsed.opt('w') or parsed.opt('write')
            return await _add_custom_color(definition, context, img_dir)
        
        # 删除自定义颜色
        if parsed.has('d') or parsed.has('delete'):
            name = parsed.opt('d') or parsed.opt('delete')
            return _delete_custom_color(name, context)
        
        # 恒星光谱颜色查询
        if parsed.has('s') or parsed.has('stellar'):
            spec_type = parsed.opt('s') or parsed.opt('stellar')
            return await stellar.query_stellar_color(spec_type, context, img_dir)
        
        # 列出光谱型
        if parsed.has('t') or parsed.has('spectype'):
            prefix = parsed.opt('t') or parsed.opt('spectype') or ''
            return stellar.list_spectral_types(prefix, context)
        
        # 默认：显示帮助
        return segments(_get_help())
        
    except Exception as e:
        logger.exception("Color handle error: %s", e)
        return segments(f"处理请求时出错: {str(e)}")



# ============================================================
# 辅助函数
# ============================================================

def _get_help() -> str:
    """返回帮助信息"""
    return """
🎨 **中国传统色彩查询**

**基础用法:**
• /color - 显示此帮助
• /color help - 显示此帮助

**按颜色名查询:**
• /color -n <名称> - 查询颜色信息
• /color -n <名称> -p - 同时显示色卡图片
• 示例: /color -n 胭脂 -p

**按颜色值查询:**
• /color -r <R,G,B> - 按 RGB 值查询
• /color -x <HEX> - 按 HEX 值查询
• /color -c <C,M,Y,K> - 按 CMYK 值查询
• 示例: /color -r 255,87,51
• 示例: /color -x #FF5733

**按色系搜索:**
• /color -a <关键词> - 搜索色系
• 示例: /color -a 红

**自定义颜色:**
• /color -w <名称> <R> <G> <B> - 添加自定义颜色
• /color -w <名称> <#HEX> - 添加自定义颜色
• /color -d <名称> - 删除自定义颜色
• 示例: /color -w 我的红 255 0 0

**恒星颜色:**
• /color -s <光谱型> - 查询恒星颜色
• /color -t <前缀> - 列出光谱型
• 示例: /color -s O5V

输入 /color help 查看此帮助
""".strip()


async def _add_custom_color(definition: str, context, img_dir: Path) -> list[dict]:
    """添加自定义颜色
    
    Args:
        definition: 颜色定义字符串
        context: 插件上下文
        img_dir: 图片输出目录
        
    Returns:
        消息段列表
    """
    parts = [p for p in re.split(r'[\s,，]+', definition) if p]
    
    try:
        if len(parts) == 2:
            # 名称 + HEX
            name, hex_value = parts
            rgb = convert.hex_to_rgb(hex_value)
            cmyk = convert.rgb_to_cmyk(rgb)
            hex_value = convert.rgb_to_hex(rgb)
        elif len(parts) == 4:
            # 名称 + RGB
            name = parts[0]
            rgb = [int(parts[1]), int(parts[2]), int(parts[3])]
            is_valid, error_msg = convert.validate_rgb(rgb)
            if not is_valid:
                return segments(f"❌ {error_msg}")
            cmyk = convert.rgb_to_cmyk(rgb)
            hex_value = convert.rgb_to_hex(rgb)
        else:
            return segments("❌ 格式错误。\n\n支持格式：\n  颜色名 R G B\n  颜色名 #HEX\n\n示例：\n  /color -w 我的红 255 0 0\n  /color -w 我的蓝 #0000FF")
    except ValueError as e:
        return segments(f"❌ 解析颜色值失败：{e}")
    
    # 加载现有自定义颜色
    custom_colors = data_manager.load_custom_colors(context)
    
    # 检查是否已存在
    if any(c['name'] == name for c in custom_colors):
        return segments(f"❌ 「{name}」已经定义过了哦")
    
    # 添加新颜色
    new_color = {
        'name': name,
        'RGB': rgb,
        'CMYK': cmyk,
        'hex': hex_value,
        'pinyin': ''
    }
    custom_colors.append(new_color)
    
    data_manager.save_custom_colors(custom_colors, context)
    
    context.logger.info(f"添加自定义颜色: {name} = {rgb}")
    
    # 生成图片
    img_path = await image_gen.generate_color_image(name, rgb, img_dir, context)
    
    result = [text(f"✅ 颜色「{name}」添加成功！\n\n{data_manager.format_color_info(new_color)}")]
    if img_path:
        result.append(image(img_path))
    
    return result


def _delete_custom_color(name: str, context) -> list[dict]:
    """删除自定义颜色
    
    Args:
        name: 颜色名称
        context: 插件上下文
        
    Returns:
        消息段列表
    """
    custom_colors = data_manager.load_custom_colors(context)
    
    original_len = len(custom_colors)
    custom_colors = [c for c in custom_colors if c['name'] != name]
    
    if len(custom_colors) == original_len:
        return segments(f"❌ 自定义颜色中没有「{name}」")
    
    data_manager.save_custom_colors(custom_colors, context)
    context.logger.info(f"删除自定义颜色: {name}")
    return segments(f"✅ 颜色「{name}」已删除")
