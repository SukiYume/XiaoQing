"""
arXiv 数据获取模块

提供从 arXiv 获取论文信息的功能，支持网页爬取和 API 两种方式。
"""

import json
import os
import re
from datetime import datetime, timedelta, timezone
from typing import Optional, Any

import pandas as pd
import requests
import feedparser
import urllib3
from bs4 import BeautifulSoup

def _load_config() -> dict[str, Any]:
    """加载配置文件"""
    config_path = os.path.join(os.path.dirname(__file__), "config.json")
    if os.path.exists(config_path):
        with open(config_path, 'r', encoding='utf-8') as f:
            return json.load(f)
    return {}

def _get_request_params() -> tuple:
    """获取请求参数（代理、SSL验证、超时）"""
    config = _load_config()
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

def get_today_arxiv(url: Optional[str] = None) -> pd.DataFrame:
    """
    从 arXiv 网页获取今日论文列表（网页爬取方式）
    
    Args:
        url: arXiv 列表页 URL，默认使用配置文件中的 URL
        
    Returns:
        包含 'arXiv ID' 和 'Title' 列的 DataFrame
    """
    if url is None:
        config = _load_config()
        url = config.get("arxiv", {}).get("url", "https://arxiv.org/list/astro-ph/new")
    
    proxies, verify, timeout = _get_request_params()
    
    try:
        response = requests.get(
            url, 
            headers={'User-Agent': 'Mozilla/5.0'}, 
            timeout=timeout, 
            proxies=proxies, 
            verify=verify
        )
        response.raise_for_status()
    except requests.RequestException as err:
        print(f'Error fetching page: {err}')
        return pd.DataFrame(columns=['arXiv ID', 'Title'])

    soup = BeautifulSoup(response.content, 'html.parser')
    dl_element = soup.find('dl')
    if not dl_element:
        print('No <dl> element found on the page.')
        return pd.DataFrame(columns=['arXiv ID', 'Title'])

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

        records.append({'arXiv ID': arxiv_id, 'Title': title})

    if not records:
        print('No articles found.')
        return pd.DataFrame(columns=['arXiv ID', 'Title'])

    data = pd.DataFrame.from_records(records, columns=['arXiv ID', 'Title'])
    print(f'Found {len(data)} articles for today.')

    return data

def get_today_arxiv_api(days: Optional[int] = None) -> pd.DataFrame:
    """
    从 arXiv API 获取最近几日的论文列表
    
    Args:
        days: 查询最近多少天的论文，默认使用配置文件中的值（默认2天）
        
    Returns:
        包含 'arXiv ID' 和 'Title' 列的 DataFrame
    """
    if days is None:
        config = _load_config()
        days = config.get("arxiv", {}).get("api_days", 2)
    
    proxies, verify, timeout = _get_request_params()

    BASE_URL = 'http://export.arxiv.org/api/query?'
    HEADERS = {'User-Agent': 'arxiv-scraper/1.0 (echo@escape.ac.cn)'}

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
        print(f'Error fetching from API: {err}')
        return pd.DataFrame(columns=['arXiv ID', 'Title'])
    
    feed = feedparser.parse(r.content)
    total_results = int(feed.feed.opensearch_totalresults)
    print(f"该时间段总共有 {total_results} 篇文章")

    batch_data = []
    for entry in feed.entries:
        arxiv_id = entry.id.split('/')[-1]
        batch_data.append([arxiv_id, entry.title.strip()])

    data = pd.DataFrame({
        'arXiv ID': [i[0] for i in batch_data],
        'Title': [i[1] for i in batch_data],
    })
    data.loc[:, 'Title'] = data.loc[:, 'Title'].str.replace('\n', '').str.replace('  ', ' ')

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
    if url is None:
        config = _load_config()
        url = config.get("arxiv", {}).get("url", "https://arxiv.org/list/astro-ph/new")
    
    proxies, verify, timeout = _get_request_params()
    
    try:
        response = requests.get(
            url, 
            headers={'User-Agent': 'Mozilla/5.0'}, 
            timeout=timeout, 
            proxies=proxies, 
            verify=verify
        )
        response.raise_for_status()
    except requests.RequestException as err:
        print(f'Error fetching page for date check: {err}')
        return None

    soup = BeautifulSoup(response.content, 'html.parser')
    
    # 查找包含日期信息的 h3 标签
    # 例如: "Showing new listings for Wednesday, 4 February 2026"
    h3_elements = soup.find_all('h3')
    for h3 in h3_elements:
        text = h3.get_text(strip=True)
        if 'Showing new listings for' in text:
            # 提取日期部分，例如 "Wednesday, 4 February 2026"
            match = re.search(r'Showing new listings for\s+\w+,\s+(\d+)\s+(\w+)\s+(\d{4})', text)
            if match:
                day = match.group(1)
                month_name = match.group(2)
                year = match.group(3)
                
                # 将月份名称转换为数字
                month_map = {
                    'January': '01', 'February': '02', 'March': '03', 'April': '04',
                    'May': '05', 'June': '06', 'July': '07', 'August': '08',
                    'September': '09', 'October': '10', 'November': '11', 'December': '12'
                }
                month = month_map.get(month_name)
                
                if month:
                    # 返回标准格式的日期字符串
                    date_str = f"{year}-{month}-{day.zfill(2)}"
                    print(f'Found arXiv update date: {date_str}')
                    return date_str
    
    print('Could not find arXiv update date in page')
    return None
