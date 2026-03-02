"""
A岛匿名版 API 封装模块

优化改进:
1. 使用 aiohttp 实现真正的异步 HTTP 请求
2. 统一的 API 配置管理
3. 数据类型使用 dataclass 更清晰
4. 集中的错误处理
5. 更好的图片下载与缓存机制
"""

import re
import time
import aiohttp
from dataclasses import dataclass
from typing import Optional, Any
from pathlib import Path
import logging

logger = logging.getLogger(__name__)

# ============================================================
# 配置常量
# ============================================================

API_HOST = "https://www.nmbxd1.com"
IMAGE_CDN = "https://image.nmb.best"
APP_ID = "A-Island-IOS-App"
# Default UUID, can be overridden via config
DEFAULT_UUID = "CD768A98-4C28-4A31-A055-6E0E4AB04E2C"

# API 端点
ENDPOINTS = {
    "forum_list": "/Api/getForumList",
    "timeline": "/Api/timeline",
    "forum": "/Api/showf",
    "thread": "/Api/thread",
    "ref": "/Api/ref",
    "feed": "/Api/feed",
    "add_feed": "/Api/addFeed",
    "del_feed": "/Api/delFeed",
}

# ============================================================
# 数据结构
# ============================================================

@dataclass
class Post:
    """帖子/回复数据结构"""
    id: str
    time: str
    user_id: str
    content: str
    img: str = ""
    
    @classmethod
    def from_json(cls, data: dict[str, Any]) -> "Post":
        """从 API JSON 响应构建 Post 对象"""
        content = data.get("content", "")
        # 清理 HTML 标签
        content = re.sub(r'<[^>]+>', '', content)
        content = content.replace('&gt;', '>').replace('&bull;', '')
        
        img = ""
        if data.get("img") and data.get("ext"):
            img = f"{data['img']}{data['ext']}"
        
        return cls(
            id=str(data.get("id", "")),
            time=data.get("now", data.get("time", "")),
            user_id=data.get("user_hash", data.get("userid", "")),
            content=content,
            img=img
        )
    
    def format_text(self) -> str:
        """格式化为可读文本"""
        return f"{self.id} - {self.user_id}\n{self.time}\n{self.content}"

@dataclass
class Thread:
    """串数据结构（包含主帖和回复）"""
    main_post: Post
    replies: list[Post]
    
    @classmethod
    def from_json(cls, data: dict[str, Any]) -> "Thread":
        """从 API JSON 响应构建 Thread 对象"""
        main_post = Post.from_json(data)
        replies = []
        
        for reply_data in data.get("Replies", []):
            # 跳过 Admin 回复和特殊 ID (9999999)
            if reply_data.get("user_hash") == "Admin" or reply_data.get("id") == 9999999:
                continue
            replies.append(Post.from_json(reply_data))
        
        return cls(main_post=main_post, replies=replies)

# ============================================================
# API 客户端
# ============================================================

class AdnmbClient:
    """A岛 API 异步客户端"""
    
    def __init__(self, session: aiohttp.ClientSession, cache_dir: Path, uuid: str = ""):
        self.session = session
        self.cache_dir = cache_dir
        self.uuid = uuid or DEFAULT_UUID
        self._forum_cache: Optional[dict[str, str]] = None
    
    def _build_params(self, **kwargs) -> dict[str, Any]:
        """构建 API 请求参数"""
        params = {
            "appid": APP_ID,
            "__t": int(time.time() * 1000)
        }
        params.update(kwargs)
        return params
    
    async def _get(self, endpoint: str, **params) -> Any:
        """发送 GET 请求"""
        url = f"{API_HOST}{ENDPOINTS.get(endpoint, endpoint)}"
        async with self.session.get(url, params=self._build_params(**params)) as resp:
            return await resp.json()
    
    async def get_forum_list(self) -> dict[str, str]:
        """获取板块列表"""
        if self._forum_cache:
            return self._forum_cache
        
        data = await self._get("forum_list")
        forum_list = {}
        for group in data:
            for forum in group.get("forums", []):
                forum_list[forum["name"]] = forum["id"]
        
        self._forum_cache = forum_list
        return forum_list
    
    async def get_timeline(self, page: int = 1) -> list[Thread]:
        """获取时间线"""
        data = await self._get("timeline", id="-1", page=page)
        return [Thread.from_json(item) for item in data if item.get("user_hash") != "Admin"]
    
    async def get_forum(self, forum_name: str, page: int = 1) -> list[Thread]:
        """获取板块内容"""
        forum_list = await self.get_forum_list()
        forum_id = forum_list.get(forum_name)
        
        if not forum_id:
            return []
        
        data = await self._get("forum", id=forum_id, page=page)
        if data == "该板块不存在":
            return []
        
        return [Thread.from_json(item) for item in data if item.get("user_hash") != "Admin"]
    
    async def get_thread(self, thread_id: str, page: int = 1) -> Optional[Thread]:
        """获取串内容"""
        data = await self._get("thread", id=thread_id, page=page)
        if data == "该主题不存在" or not isinstance(data, dict):
            return None
        return Thread.from_json(data)
    
    async def get_ref(self, ref_id: str) -> Optional[Post]:
        """获取单条回复"""
        data = await self._get("ref", id=ref_id, page=1)
        if not isinstance(data, dict) or "id" not in data:
            return None
        return Post.from_json(data)
    
    async def get_feed(self, page: int = 1) -> list[Post]:
        """获取订阅"""
        data = await self._get("feed", page=page, uuid=self.uuid)
        if not isinstance(data, list):
            return []
        return [Post.from_json(item) for item in data]
    
    async def add_feed(self, thread_id: str) -> str:
        """添加订阅"""
        result = await self._get("add_feed", tid=thread_id, uuid=self.uuid)
        return str(result) if result else "添加订阅失败"
    
    async def del_feed(self, thread_id: str) -> str:
        """删除订阅"""
        result = await self._get("del_feed", tid=thread_id, uuid=self.uuid)
        return str(result) if result else "删除订阅失败"
    
    async def download_image(self, img_path: str, use_thumb: bool = False) -> Optional[Path]:
        """下载图片到本地缓存"""
        if not img_path:
            return None
        
        # 选择 CDN 路径
        cdn_type = "thumb" if use_thumb else "image"
        url = f"{IMAGE_CDN}/{cdn_type}/{img_path}"
        
        # 本地文件路径
        filename = img_path.split("/")[-1]
        local_path = self.cache_dir / filename
        
        # 如果已缓存，直接返回
        if local_path.exists():
            return local_path
        
        try:
            async with self.session.get(url) as resp:
                if resp.status == 200:
                    content = await resp.read()
                    local_path.write_bytes(content)
                    return local_path
                else:
                    logger.warning("Image download failed (status=%s): %s", resp.status, url)
        except Exception as exc:
            logger.warning("Image download error for %s: %s", url, exc)

        return None
