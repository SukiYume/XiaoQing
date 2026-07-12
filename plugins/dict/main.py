"""
综合词典插件
提供天文学专业术语的中英互译功能
"""
import re
import logging
import hashlib
from pathlib import Path
from functools import lru_cache

from core.plugin_base import segments, run_sync, load_json
from core.args import parse
from core.public_errors import public_error_message, public_error_response


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
        if not dict_file.exists():
            return None
        import pandas as pd
        frame = pd.read_csv(dict_file, sep='\t', header=None, names=['src', 'dst'])
        frame["_src_lower"] = frame["src"].astype(str).str.lower()
        return frame
    except ImportError:
        raise ImportError("天文词典功能需要 pandas 库，请运行: pip install pandas")
    except Exception as exc:
        raise RuntimeError("加载词典文件失败") from exc


def _detect_language(text: str) -> str:
    """
    检测文本是中文还是英文
    
    Args:
        text: 待检测文本
        
    Returns:
        'chinese' 或 'english'
    """
    return 'chinese' if re.search(r'[\u4e00-\u9fff]', text) else 'english'


def _extract_query(parsed, exact_match: bool) -> str:
    query = parsed.rest().strip()
    if query:
        return query

    if exact_match:
        for key in ("e", "exact"):
            value = parsed.opt(key).strip()
            if value and value.lower() != "true":
                return value

    return ""


def _query_astrodict_sync(
    query: str,
    plugin_dir: Path,
    exact_match: bool,
    max_results: int,
) -> str:
    lang = _detect_language(query)
    manifest_file = plugin_dir / "assets" / "manifest.json"
    manifest = load_json(manifest_file, {})
    files = manifest.get("files", {}) if isinstance(manifest, dict) else {}
    direction_key = "chinese_to_english" if lang == "chinese" else "english_to_chinese"
    file_spec = files.get(direction_key, {}) if isinstance(files, dict) else {}
    filename = file_spec.get("filename", "") if isinstance(file_spec, dict) else ""
    expected_sha256 = file_spec.get("sha256", "") if isinstance(file_spec, dict) else ""
    direction = "中译英" if lang == "chinese" else "英译中"
    if not filename or Path(filename).name != filename or not expected_sha256:
        return "天文学词典资源清单无效；请重新安装完整发行包"
    dict_file = plugin_dir / "assets" / filename
    if not dict_file.is_file():
        return f"天文学词典数据文件不存在: {filename}；请重新安装包含 package data 的发行包"
    actual_sha256 = hashlib.sha256(dict_file.read_bytes()).hexdigest()
    if actual_sha256.lower() != str(expected_sha256).lower():
        return f"天文学词典数据校验失败: {filename}"
    df = _load_dictionary(dict_file)
    if df is None:
        return f"天文学词典数据文件不存在: {filename}；请重新安装包含 package data 的发行包"
    lowered = query.lower()
    if exact_match:
        matches = df[df["_src_lower"] == lowered]
    else:
        keywords = lowered.split()
        mask = df["_src_lower"].str.contains(keywords[0], regex=False, na=False)
        for keyword in keywords[1:]:
            mask &= df["_src_lower"].str.contains(keyword, regex=False, na=False)
        matches = df[mask]
    if matches.empty:
        return f"在天文学词典（{direction}）中未找到相关词条"
    total_found = len(matches)
    lines = [
        f"{idx}. {row['src']} → {row['dst']}"
        for idx, (_, row) in enumerate(matches.head(max_results).iterrows(), 1)
    ]
    lines.append(
        f"\n共找到 {total_found} 条结果"
        + (f"，仅显示前 {max_results} 条" if total_found > max_results else "")
    )
    return "\n".join(lines)


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
    try:
        return await run_sync(
            _query_astrodict_sync,
            query,
            context.plugin_dir,
            exact_match,
            max_results,
        )
    except ImportError as exc:
        return public_error_message(context, exc, logger=context.logger, component="dict.search")
    except Exception as exc:
        return public_error_message(context, exc, logger=context.logger, component="dict.search")


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
        
        exact_match = parsed.has('e') or parsed.has('exact')
        # 精确匹配的 bare flag 可能把查询词吃进 option value，需要单独回收。
        query = _extract_query(parsed, exact_match)
        
        if not query:
            return segments(_get_help())
        
        # 获取参数
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
        
    except Exception as exc:
        return public_error_response(context, exc, logger=logger, component="dict.handle")


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
