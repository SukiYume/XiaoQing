"""
arXiv 当日列表获取模块。

运行时只读取 astro-ph/new 网页；训练数据使用独立的数据准备脚本，避免两套抓取入口并存。
"""

import logging
import math
import os
import re
from collections.abc import Mapping
from datetime import datetime
from typing import Any

import pandas as pd
import requests
from bs4 import BeautifulSoup

from core.bounded_http import (
    HTML_MIME_POLICY,
    BodyLimits,
    BoundedHttpError,
    RedirectPolicy,
    requests_request_bounded,
)

from .utils import load_plugin_config

logger = logging.getLogger(__name__)

# 空结果的列定义（避免魔法字面量重复）
_EMPTY_COLUMNS  = ["arXiv ID", "Title", "Abstract"]
_ENGLISH_MONTHS = {
    "january": 1,
    "february": 2,
    "march": 3,
    "april": 4,
    "may": 5,
    "june": 6,
    "july": 7,
    "august": 8,
    "september": 9,
    "october": 10,
    "november": 11,
    "december": 12,
}
_HTML_BODY_LIMITS = BodyLimits(
    max_wire_bytes          = 4 * 1024 * 1024,
    max_decoded_bytes       = 8 * 1024 * 1024,
    max_decompression_ratio = 20,
)
_ARXIV_REDIRECT_POLICY = RedirectPolicy(
    max_hops                      = 3,
    allowed_schemes               = frozenset({"http", "https"}),
    same_origin_only              = True,
    allow_https_upgrade_same_host = True,
)


def _get_request_params(
    config: Mapping[str, Any] | None = None,
) -> tuple[dict[str, str] | None, bool, float]:
    """
    获取请求参数（代理、SSL验证、超时）

    Args:
        config: 插件配置字典，为 None 时自动加载
    """
    if config is None:
        config = load_plugin_config()
    arxiv_config = config.get("arxiv", {})
    if not isinstance(arxiv_config, Mapping):
        raise ValueError("arxiv config must be a JSON object")

    # 代理配置：优先使用环境变量，其次使用配置文件
    proxy = os.getenv("ARXIV_PROXY") or arxiv_config.get("proxy")
    if proxy is not None and (not isinstance(proxy, str) or not proxy.strip()):
        raise ValueError("arxiv.proxy must be null or a non-empty string")
    proxy   = proxy.strip() if isinstance(proxy, str) else None
    proxies = {"http": proxy, "https": proxy} if proxy else None

    # SSL 验证：如果使用代理且配置允许，可以禁用
    use_ssl_verify = arxiv_config.get("use_ssl_verify", True)
    if type(use_ssl_verify) is not bool:
        raise ValueError("arxiv.use_ssl_verify must be a boolean")
    verify = True
    if proxies and not use_ssl_verify:
        verify = False
        logger.warning(
            "arXiv TLS certificate verification is disabled for the configured proxy; "
            "use only with a trusted inspection proxy"
        )

    timeout = arxiv_config.get("timeout", 30)
    if (
        isinstance(timeout, bool)
        or not isinstance(timeout, (int, float))
        or not math.isfinite(float(timeout))
        or float(timeout) <= 0
    ):
        raise ValueError("arxiv.timeout must be a finite positive number")

    return proxies, verify, float(timeout)


def _fetch_arxiv_page(
    url: str | None                  = None,
    config: Mapping[str, Any] | None = None,
) -> BeautifulSoup | None:
    """
    获取 arXiv 页面并解析为 BeautifulSoup 对象。

    Args:
        url: arXiv 列表页 URL，默认使用配置中的 URL
        config: 插件配置字典，为 None 时自动加载

    Returns:
        解析后的 BeautifulSoup 对象，失败返回 None
    """
    if config is None:
        config = load_plugin_config()
    arxiv_config = config.get("arxiv", {})
    if not isinstance(arxiv_config, Mapping):
        raise ValueError("arxiv config must be a JSON object")
    if url is None:
        url = arxiv_config.get("url", "https://arxiv.org/list/astro-ph/new")
    if not isinstance(url, str) or not url.strip():
        raise ValueError("arxiv.url must be a non-empty string")
    url = url.strip()

    proxies, verify, timeout = _get_request_params(config)

    try:
        response = requests_request_bounded(
            "GET",
            url,
            limits          = _HTML_BODY_LIMITS,
            mime_policy     = HTML_MIME_POLICY,
            redirect_policy = _ARXIV_REDIRECT_POLICY,
            headers         = {"User-Agent": "Mozilla/5.0"},
            request_kwargs  = {
                "timeout": timeout,
                "proxies": proxies,
                "verify": verify,
            },
        )
    except (requests.RequestException, BoundedHttpError) as err:
        logger.error(
            "Error fetching arXiv page error_type=%s",
            type(err).__name__,
        )
        return None

    return BeautifulSoup(response.body, "html.parser")


def get_today_arxiv(url: str | None = None) -> pd.DataFrame:
    """
    从 arXiv 网页获取今日论文列表（含标题和摘要）

    从页面的 <p class="mathjax"> 中提取摘要，若某篇论文没有摘要则为空字符串。

    Args:
        url: arXiv 列表页 URL，默认使用配置文件中的 URL

    Returns:
        包含 'arXiv ID', 'Title', 'Abstract' 列的 DataFrame
    """
    soup = _fetch_arxiv_page(url)
    if soup is None:
        return pd.DataFrame(columns=_EMPTY_COLUMNS)

    dl_element = soup.find("dl")
    if not dl_element:
        logger.error("No <dl> element found on the page.")
        return pd.DataFrame(columns=_EMPTY_COLUMNS)

    records = []
    for dt in dl_element.find_all("dt"):
        link = dt.find("a", href=re.compile(r"/abs/\d{4}\.\d{4,5}"))
        if not link:
            continue

        match = re.search(r"(\d{4}\.\d{4,5})", link["href"])
        if not match:
            continue
        arxiv_id = match.group(1)

        dd = dt.find_next_sibling("dd")
        if not dd:
            continue

        title_div = dd.find("div", class_="list-title")
        if not title_div:
            continue

        title = title_div.get_text(strip=True)
        title = re.sub(r"^Title:\s*", "", title)
        title = re.sub(r"\s+", " ", title).strip()

        # 提取摘要（在 <p class="mathjax"> 中）
        abstract_p = dd.find("p", class_="mathjax")
        abstract = ""
        if abstract_p:
            abstract = abstract_p.get_text(strip=True)
            abstract = re.sub(r"\s+", " ", abstract).strip()

        records.append({"arXiv ID": arxiv_id, "Title": title, "Abstract": abstract})

    if not records:
        logger.warning("No articles found.")
        return pd.DataFrame(columns=_EMPTY_COLUMNS)

    data = pd.DataFrame.from_records(records, columns=_EMPTY_COLUMNS)
    logger.info("Found %d articles for today.", len(data))

    return data


def check_arxiv_update_date(url: str | None = None) -> str | None:
    """
    检查 arXiv 页面的更新日期

    从页面中提取类似 "Showing new listings for Wednesday, 4 February 2026" 的日期信息。

    Args:
        url: arXiv 列表页 URL，默认使用配置文件中的 URL

    Returns:
        日期字符串（格式如 "2026-02-04"），如果无法获取则返回 None
    """
    soup = _fetch_arxiv_page(url)
    if soup is None:
        return None

    # 查找包含日期信息的 h3 标签
    # 例如: "Showing new listings for Wednesday, 4 February 2026"
    h3_elements = soup.find_all("h3")
    for h3 in h3_elements:
        text = h3.get_text(strip=True)
        if "Showing new listings for" in text:
            # 提取日期部分，例如 "Wednesday, 4 February 2026"
            match = re.search(r"Showing new listings for\s+\w+,\s+(\d+)\s+(\w+)\s+(\d{4})", text)
            if match:
                day, month_name, year = match.group(1), match.group(2), match.group(3)
                try:
                    month = _ENGLISH_MONTHS.get(month_name.casefold())
                    if month is None:
                        raise ValueError("unsupported arXiv month name")
                    date_obj = datetime(int(year), month, int(day))
                    date_str = date_obj.strftime("%Y-%m-%d")
                    logger.info("Found arXiv update date: %s", date_str)
                    return date_str
                except ValueError:
                    pass  # 月份名无法解析，继续查找其他 h3
    logger.warning("Could not find arXiv update date in page")
    return None
