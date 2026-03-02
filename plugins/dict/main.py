"""
综合词典插件
提供天文学专业术语的中英互译功能
"""
import re
import logging
from pathlib import Path
from functools import lru_cache

from core.plugin_base import segments, run_sync, load_json
from core.args import parse


logger = logging.getLogger(__name__)


# ============================================================
# 插件初始化
# ============================================================

def init(context=None) -> None:
    """插件初始化"""
    pass


# ============================================================
# 数据加载与缓存
# ============================================================

@lru_cache(maxsize=2)
def _load_dictionary(dict_file: Path):
    """
    加载词典数据文件（带缓存）
    
    Args:
        dict_file: 词典文件路径
        
    Returns:
        DataFrame 或 None（加载失败时）
    """
    try:
        import pandas as pd
        if not dict_file.exists():
            return None
        return pd.read_csv(dict_file, sep='\t', header=None, names=['src', 'dst'])
    except ImportError:
        raise ImportError("天文词典功能需要 pandas 库，请运行: pip install pandas")
    except Exception as e:
        raise RuntimeError(f"加载词典文件失败: {e}")


def _detect_language(text: str) -> str:
    """
    检测文本是中文还是英文
    
    Args:
        text: 待检测文本
        
    Returns:
        'chinese' 或 'english'
    """
    return 'chinese' if re.search(r'[\u4e00-\u9fff]', text) else 'english'


# ============================================================
# 天文学词典
# ============================================================

async def query_astrodict(
    query: str, 
    context,
    exact_match: bool = False,
    max_results: int = 10
) -> str:
    """
    查询天文学词典
    
    Args:
        query: 查询词汇
        context: 插件上下文
        exact_match: 是否精确匹配
        max_results: 最大返回结果数
        
    Returns:
        查询结果字符串
    """
    query = query.strip()
    if not query:
        return "请提供要查询的词汇"
    
    # 判断翻译方向
    lang = _detect_language(query)
    
    # 选择词典文件
    if lang == 'chinese':
        dict_file = context.plugin_dir / "data" / "astrodict_ce.txt"
        direction = "中译英"
    else:
        dict_file = context.plugin_dir / "data" / "astrodict_ec.txt"
        direction = "英译中"
    
    # 加载词典数据
    try:
        df = _load_dictionary(dict_file)
        if df is None:
            return f"天文学词典数据文件不存在: {dict_file.name}"
    except ImportError as e:
        context.logger.error(f"缺少依赖: {e}")
        return str(e)
    except Exception as e:
        context.logger.error(f"加载词典失败: {e}")
        return f"词典加载失败: {e}"
    
    # 执行搜索
    try:
        if exact_match:
            # 精确匹配
            matches = df[df['src'].str.lower() == query.lower()]
        else:
            # 模糊匹配（支持多关键词）
            keywords = query.lower().split()
            mask = df['src'].str.lower().str.contains(keywords[0], regex=False, na=False)
            for keyword in keywords[1:]:
                mask &= df['src'].str.lower().str.contains(keyword, regex=False, na=False)
            matches = df[mask]
        
        if matches.empty:
            return f"在天文学词典（{direction}）中未找到相关词条"
        
        # 限制结果数量
        total_found = len(matches)
        matches = matches.head(max_results)
        
        # 格式化输出
        result_lines = []
        for idx, (_, row) in enumerate(matches.iterrows(), 1):
            result_lines.append(f"{idx}. {row['src']} → {row['dst']}")
        
        # 添加统计信息
        if total_found > max_results:
            result_lines.append(f"\n共找到 {total_found} 条结果，仅显示前 {max_results} 条")
        else:
            result_lines.append(f"\n共找到 {total_found} 条结果")
        
        return "\n".join(result_lines)
        
    except Exception as e:
        context.logger.error(f"天文词典查询失败: {e}", exc_info=True)
        return f"查询失败: {e}"


# ============================================================
# 主处理函数
# ============================================================

async def handle(
    command: str, 
    args: str, 
    event: dict, 
    context
) -> list[dict]:
    """命令处理入口"""
    try:
        parsed = parse(args)
        
        # 空命令或帮助信息
        if not parsed or parsed.first.lower() in ['help', 'h', 'list', 'l', '帮助']:
            return segments(_get_help())
        
        # 获取查询词汇（使用 rest() 方法获取所有位置参数）
        query = parsed.rest() or args.strip()
        
        if not query:
            return segments(_get_help())
        
        # 获取参数
        exact_match = parsed.has('e') or parsed.has('exact')
        max_results_str = parsed.opt('n') or parsed.opt('num')
        try:
            max_results = int(max_results_str) if max_results_str else 10
        except ValueError:
            max_results = 10
        
        # 验证参数
        if max_results < 1:
            max_results = 10
        elif max_results > 100:
            max_results = 100
        
        # 执行查询
        logger.info(
            f"天文词典查询: query='{query}', exact={exact_match}, max={max_results}"
        )
        
        result = await query_astrodict(
            query=query,
            context=context,
            exact_match=exact_match,
            max_results=max_results
        )
        
        return segments(result)
        
    except Exception as e:
        logger.exception("Dict handle error: %s", e)
        return segments(f"处理请求时出错: {str(e)}")


def _get_help() -> str:
    """显示帮助信息"""
    return """
📖 **天文学词典**

查询天文学专业术语，支持中英互译

**基础用法:**
• /dict <词汇> - 查询词汇翻译
• /dict help - 显示此帮助

**高级选项:**
• /dict -e <词汇> - 精确匹配
• /dict -n <数量> <词汇> - 显示指定数量结果

**功能特点:**
- 自动识别中英文
- 支持模糊搜索
- 支持精确匹配
- 专业天文术语库

**示例:**
• /dict galaxy - 查询 "galaxy"
• /dict 星系 - 查询 "星系"
• /dict -e galaxy - 精确匹配 "galaxy"
• /dict -n 20 star - 显示最多 20 条结果
• /dict black hole - 支持多词查询

输入 /dict help 查看此帮助
""".strip()
