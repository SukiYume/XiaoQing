"""
arXiv 数据获取模块

提供从 arXiv 获取论文信息的功能，支持网页爬取和 API 两种方式。
"""

import logging
import os
import re
from datetime import datetime, timedelta, timezone
from typing import Optional

import pandas as pd
import requests
import feedparser
import urllib3
from bs4 import BeautifulSoup

from .utils import load_plugin_config

logger = logging.getLogger(__name__)

# 空结果的列定义（避免魔法字面量重复）
_EMPTY_COLUMNS = ['arXiv ID', 'Title', 'Abstract']


def _get_request_params(config: Optional[dict] = None) -> tuple:
    """
    获取请求参数（代理、SSL验证、超时）

    Args:
        config: 插件配置字典，为 None 时自动加载
    """
    if config is None:
        config = load_plugin_config()
    arxiv_config = config.get("arxiv", {})

    # 代理配置：优先使用环境变量，其次使用配置文件
    proxy = os.getenv("ARXIV_PROXY") or arxiv_config.get("proxy")
    proxies = {"http": proxy, "https": proxy} if proxy else None

    # SSL 验证：如果使用代理且配置允许，可以禁用
    use_ssl_verify = arxiv_config.get("use_ssl_verify", True)
    verify = True
    if proxies and not use_ssl_verify:
        verify = False
        urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

    timeout = arxiv_config.get("timeout", 30)

    return proxies, verify, timeout


def _fetch_arxiv_page(url: Optional[str] = None, config: Optional[dict] = None) -> Optional[BeautifulSoup]:
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
    if url is None:
        url = config.get("arxiv", {}).get("url", "https://arxiv.org/list/astro-ph/new")

    proxies, verify, timeout = _get_request_params(config)

    try:
        response = requests.get(
            url,
            headers={'User-Agent': 'Mozilla/5.0'},
            timeout=timeout,
            proxies=proxies,
            verify=verify,
        )
        response.raise_for_status()
    except requests.RequestException as err:
        logger.error(f'Error fetching arXiv page: {err}')
        return None

    return BeautifulSoup(response.content, 'html.parser')


def get_today_arxiv(url: Optional[str] = None) -> pd.DataFrame:
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

    dl_element = soup.find('dl')
    if not dl_element:
        logger.error('No <dl> element found on the page.')
        return pd.DataFrame(columns=_EMPTY_COLUMNS)

    records = []
    for dt in dl_element.find_all('dt'):
        link = dt.find('a', href=re.compile(r'/abs/\d{4}\.\d{4,5}'))
        if not link:
            continue

        match = re.search(r'(\d{4}\.\d{4,5})', link['href'])
        if not match:
            continue
        arxiv_id = match.group(1)

        dd = dt.find_next_sibling('dd')
        if not dd:
            continue

        title_div = dd.find('div', class_='list-title')
        if not title_div:
            continue

        title = title_div.get_text(strip=True)
        title = re.sub(r'^Title:\s*', '', title)
        title = re.sub(r'\s+', ' ', title).strip()

        # 提取摘要（在 <p class="mathjax"> 中）
        abstract_p = dd.find('p', class_='mathjax')
        abstract = ''
        if abstract_p:
            abstract = abstract_p.get_text(strip=True)
            abstract = re.sub(r'\s+', ' ', abstract).strip()

        records.append({'arXiv ID': arxiv_id, 'Title': title, 'Abstract': abstract})

    if not records:
        logger.warning('No articles found.')
        return pd.DataFrame(columns=_EMPTY_COLUMNS)

    data = pd.DataFrame.from_records(records, columns=_EMPTY_COLUMNS)
    logger.info(f'Found {len(data)} articles for today.')

    return data

# 注意：此函数目前未被推理流程调用。若需启用，可在 config.json 中添加 "source": "api" 开关并在 arxiv_inference.py 中适配。
def get_today_arxiv_api(days: Optional[int] = None) -> pd.DataFrame:
    """
    从 arXiv API 获取最近几日的论文列表（含标题和摘要）
    
    Args:
        days: 查询最近多少天的论文，默认使用配置文件中的值（默认2天）
        
    Returns:
        包含 'arXiv ID', 'Title', 'Abstract' 列的 DataFrame
    """
    config = load_plugin_config()
    if days is None:
        days = config.get("arxiv", {}).get("api_days", 2)
    
    proxies, verify, timeout = _get_request_params(config)

    BASE_URL = 'http://export.arxiv.org/api/query?'
    HEADERS = {'User-Agent': 'arxiv-scraper/1.0 (SukiYume@users.noreply.github.com)'}

    today = datetime.now(timezone.utc).date()
    start_date = (today - timedelta(days=days)).strftime("%Y%m%d%H%M")
    end_date = today.strftime("%Y%m%d%H%M")

    search_query = f'astrophysics AND submittedDate:[{start_date} TO {end_date}]'
    params = {
        'search_query': search_query,
        'max_results': 1000,
        'sortBy': 'submittedDate',
        'sortOrder': 'ascending'
    }
    
    try:
        r = requests.get(
            BASE_URL, 
            params=params, 
            headers=HEADERS, 
            proxies=proxies, 
            verify=verify,
            timeout=timeout
        )
        r.raise_for_status()
    except requests.RequestException as err:
        logger.error(f'Error fetching from API: {err}')
        return pd.DataFrame(columns=_EMPTY_COLUMNS)
    
    feed = feedparser.parse(r.content)
    total_results = int(feed.feed.opensearch_totalresults)
    logger.info(f"该时间段总共有 {total_results} 篇文章")

    records = []
    for entry in feed.entries:
        arxiv_id = entry.id.split('/')[-1]
        title = re.sub(r'\s+', ' ', entry.title.strip())
        abstract = re.sub(r'\s+', ' ', entry.summary.strip())
        records.append({
            'arXiv ID': arxiv_id,
            'Title': title,
            'Abstract': abstract,
        })

    data = pd.DataFrame.from_records(records, columns=_EMPTY_COLUMNS)
    logger.info(f'Found {len(data)} articles from API.')

    return data

def check_arxiv_update_date(url: Optional[str] = None) -> Optional[str]:
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
    h3_elements = soup.find_all('h3')
    for h3 in h3_elements:
        text = h3.get_text(strip=True)
        if 'Showing new listings for' in text:
            # 提取日期部分，例如 "Wednesday, 4 February 2026"
            match = re.search(r'Showing new listings for\s+\w+,\s+(\d+)\s+(\w+)\s+(\d{4})', text)
            if match:
                day, month_name, year = match.group(1), match.group(2), match.group(3)
                try:
                    date_obj = datetime.strptime(f"{day} {month_name} {year}", "%d %B %Y")
                    date_str = date_obj.strftime("%Y-%m-%d")
                    logger.info(f'Found arXiv update date: {date_str}')
                    return date_str
                except ValueError:
                    pass  # 月份名无法解析，继续查找其他 h3
    logger.warning('Could not find arXiv update date in page')
    return None
