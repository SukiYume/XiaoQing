"""
GitHub Trending 插件

获取 GitHub 每日/每周/每月趋势项目。
"""

import json
import logging
from datetime import datetime
from typing import Any

from bs4 import BeautifulSoup

from core.plugin_base import segments
from core.args import parse

logger = logging.getLogger(__name__)

# ============================================================
# 常量
# ============================================================

VALID_RANGES: set[str] = {"daily", "weekly", "monthly"}
RANGE_NAMES = {"daily": "每日", "weekly": "每周", "monthly": "每月"}

# ============================================================
# 插件初始化
# ============================================================

def init(context=None) -> None:
    """插件初始化"""
    pass

# ============================================================
# 配置获取
# ============================================================

def _get_proxy(context) -> str:
    """获取代理配置"""
    return context.secrets.get("plugins", {}).get("github", {}).get("proxy", "")

# ============================================================
# 主处理函数
# ============================================================

async def handle(command: str, args: str, event: dict[str, Any], context) -> list[dict[str, Any]]:
    """命令处理入口"""
    try:
        parsed = parse(args)
        
        if not parsed:
            # 没有参数时默认显示每日趋势
            return await _fetch_trending("daily", context)
        
        subcommand = parsed.first.lower()
        
        # 命令路由
        if subcommand == "help" or subcommand == "帮助":
            return segments(_show_help())
        
        # 如果是有效的时间范围，获取对应趋势
        if subcommand in VALID_RANGES:
            return await _fetch_trending(subcommand, context)
        
        # 未知命令时显示帮助
        return segments(f"未知命令: {subcommand}\n{_show_help()}")
        
    except Exception as e:
        logger.exception("GitHub handle error: %s", e)
        return segments(f"处理请求时出错: {str(e)}")

async def scheduled(context) -> list[dict[str, Any]]:
    """定时任务入口"""
    return await _fetch_trending("daily", context)

def _show_help() -> str:
    """显示帮助信息"""
    return """
📈 **GitHub Trending**

**查看趋势:**
• /github - 查看今日热门项目
• /github daily - 查看每日热门项目
• /github weekly - 查看每周热门项目
• /github monthly - 查看每月热门项目

**其他:**
• /github help - 显示此帮助信息

每日早上 8:30 会自动推送当日热门项目到群聊。

输入 /github help 查看此帮助
""".strip()

async def _fetch_trending(time_range: str, context) -> list[dict[str, Any]]:
    """获取 GitHub Trending"""
    if time_range not in VALID_RANGES:
        time_range = "daily"

    url = f"https://github.com/trending?since={time_range}"
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"
    }

    # 发送请求
    proxy = _get_proxy(context)
    request_kwargs: dict[str, Any] = {"headers": headers}
    if proxy:
        request_kwargs["proxy"] = proxy

    try:
        async with context.http_session.get(url, **request_kwargs) as response:
            if response.status != 200:
                return segments(f"❌ HTTP {response.status}")
            html = await response.text()
    except Exception as exc:
        logger.exception("GitHub fetch failed: %s", exc)
        return segments(f"❌ 获取失败: {exc}")

    # 解析 HTML
    repos = _parse_trending_html(html)
    if not repos:
        return segments("❌ 未找到趋势项目")

    # 保存历史
    _save_history(repos, time_range, context)

    # 格式化输出
    today = datetime.now().strftime("%Y-%m-%d")
    period = RANGE_NAMES.get(time_range, time_range)

    lines = [f"📈 GitHub {period}趋势 ({today})\n"]
    for i, repo in enumerate(repos[:10], 1):
        stars = repo.get("stars_gained") or repo.get("stars", "")
        lines.append(
            f"{i}. {repo['full_name']} ({repo['language']})\n"
            f"   ⭐ {stars}\n"
            f"   📝 {repo['description']}\n"
            f"   🔗 {repo['url']}\n"
        )

    return segments("\n".join(lines))

# ============================================================
# HTML 解析
# ============================================================

def _parse_trending_html(html: str) -> list[dict[str, Any]]:
    """解析 Trending 页面 HTML"""
    soup = BeautifulSoup(html, "html.parser")
    articles = soup.select("article.Box-row")
    repos = []

    for article in articles:
        # 仓库链接
        repo_link = article.select_one("h2 a")
        if not repo_link:
            continue

        full_name = repo_link.get_text(strip=True).replace(" ", "")
        if "/" in full_name:
            owner, name = full_name.split("/", 1)
        else:
            owner, name = "", full_name

        url = "https://github.com" + str(repo_link.get("href", ""))

        # 描述
        desc_el = article.select_one("p")
        description = desc_el.get_text(strip=True) if desc_el else "无描述"

        # 语言
        lang_el = article.select_one("span[itemprop='programmingLanguage']")
        language = lang_el.get_text(strip=True) if lang_el else "未知"

        # Star 和 Fork
        stars = forks = "0"
        for stat in article.select("a.Link--muted"):
            txt = stat.get_text(strip=True).replace(",", "")
            if "star" in txt.lower() or "★" in txt:
                stars = txt
            elif "fork" in txt.lower():
                forks = txt

        # 今日 Star
        stars_today = ""
        gain_el = article.select_one("span.d-inline-block.float-sm-right")
        if gain_el:
            stars_today = gain_el.get_text(strip=True)

        repos.append({
            "owner": owner,
            "name": name,
            "full_name": f"{owner}/{name}",
            "url": url,
            "description": description,
            "language": language,
            "stars": stars,
            "forks": forks,
            "stars_gained": stars_today,
        })

    return repos

def _save_history(repos: list[dict[str, Any]], time_range: str, context) -> None:
    """保存历史记录"""
    data_dir = context.data_dir
    history_dir = data_dir / "history"
    history_dir.mkdir(parents=True, exist_ok=True)

    today = datetime.now().strftime("%Y-%m-%d")
    result = {
        "date": datetime.now().isoformat(),
        "time_range": time_range,
        "count": len(repos),
        "repositories": repos,
    }

    # 保存最新
    (data_dir / f"trending_{time_range}_latest.json").write_text(
        json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    # 保存历史
    (history_dir / f"trending_{time_range}_{today}.json").write_text(
        json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8"
    )
