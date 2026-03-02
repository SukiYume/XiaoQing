import logging
import asyncio
from typing import Any, Optional
import aiohttp

logger = logging.getLogger(__name__)

async def chat_completions_with_fallback_paths(
    session: Optional[aiohttp.ClientSession],
    api_base: str,
    api_key: str,
    model: str,
    messages: list[dict[str, str]],
    temperature: float = 0.7,
    max_tokens: int = 1000,
    timeout_seconds: int = 30,
    max_retry: int = 2,
    retry_interval_seconds: int = 1,
    proxy: Optional[str] = None,
) -> tuple[Optional[str], str]:
    """
    通用聊天补全逻辑，包含简单的重试机制
    """
    url = f"{api_base.rstrip('/')}/chat/completions"
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json"
    }
    
    payload = {
        "model": model,
        "messages": messages,
        "temperature": temperature,
        "max_tokens": max_tokens
    }

    local_session = None
    if session is None:
        local_session = aiohttp.ClientSession()
        session = local_session

    try:
        for attempt in range(max_retry + 1):
            try:
                async with session.post(
                    url, 
                    headers=headers, 
                    json=payload, 
                    proxy=proxy, 
                    timeout=aiohttp.ClientTimeout(total=timeout_seconds)
                ) as resp:
                    if resp.status == 200:
                        data = await resp.json()
                        content = data.get("choices", [{}])[0].get("message", {}).get("content", "")
                        return content, "main_path"
                    else:
                        error_text = await resp.text()
                        logger.warning("LLM API Error (Attempt %s): %s - %s", attempt+1, resp.status, error_text)
            except Exception as e:
                logger.warning("LLM Request Error (Attempt %s): %s", attempt+1, e)
            
            if attempt < max_retry:
                await asyncio.sleep(retry_interval_seconds)
        
        return None, "error"
    finally:
        if local_session:
            await local_session.close()
