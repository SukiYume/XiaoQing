"""
颜色图片生成模块
使用 matplotlib 生成颜色示例图片
"""
import re
from pathlib import Path
from typing import Optional

from core.plugin_base import ensure_dir, run_sync

# 检查可选依赖
try:
    import numpy as np
    import matplotlib
    matplotlib.use('Agg')  # 使用非交互式后端
    import matplotlib.pyplot as plt
    MATPLOTLIB_AVAILABLE = True
except ImportError:
    MATPLOTLIB_AVAILABLE = False

async def generate_color_image(name: str, rgb: list[int], output_dir: Path, context) -> Optional[str]:
    """生成颜色示例图片
    
    Args:
        name: 颜色名称
        rgb: RGB 值列表
        output_dir: 输出目录
        context: 插件上下文
        
    Returns:
        图片路径，失败时返回 None
    """
    if not MATPLOTLIB_AVAILABLE:
        context.logger.warning("图片生成失败：缺少 matplotlib 和 numpy 依赖")
        return None
    
    try:
        ensure_dir(output_dir)
        # 使用安全的文件名
        safe_name = re.sub(r'[<>:"/\\|?*]', '_', name)
        img_path = output_dir / f"{safe_name}.png"
        
        def _generate():
            import numpy as np
            import matplotlib.pyplot as plt
            
            img = np.zeros([100, 200, 3], np.uint8)
            img[:] = [int(x) for x in rgb]
            
            fig, ax = plt.subplots(figsize=(2, 1))
            ax.imshow(img)
            ax.axis('off')
            try:
                ax.set_title(name, fontsize=10, fontproperties='SimHei')
            except:
                # 如果 SimHei 字体不可用，使用默认字体
                ax.set_title(name, fontsize=10)
            
            plt.savefig(str(img_path), dpi=72, bbox_inches='tight', pad_inches=0.1)
            plt.close(fig)
        
        await run_sync(_generate)
        context.logger.debug(f"生成颜色图片: {name} -> {img_path}")
        return str(img_path)
        
    except Exception as exc:
        context.logger.error(f"生成颜色图片失败: {name}, 错误: {exc}", exc_info=True)
        return None
