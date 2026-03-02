import logging
from typing import Any

import aiohttp

logger = logging.getLogger(__name__)

async def generate_summary(
    session: aiohttp.ClientSession,
    api_base: str,
    api_key: str,
    model: str,
    title: str,
    abstract: str
) -> str:
    url = f"{api_base.rstrip('/')}/chat/completions"
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json"
    }

    prompt = f"""请用中文总结以下论文的要点，包括：
1. 研究背景和动机
2. 主要方法和创新点
3. 关键结果和结论
4. 研究意义

论文标题: {title}

摘要:
{abstract}

请用简洁清晰的语言总结，不超过300字。"""

    payload = {
        "model": model,
        "messages": [
            {"role": "user", "content": prompt}
        ],
        "temperature": 0.7,
        "max_tokens": 500
    }

    async with session.post(url, headers=headers, json=payload) as resp:
        if resp.status != 200:
            text = await resp.text()
            raise RuntimeError(f"LLM API error: {resp.status} - {text}")

        data = await resp.json()
        choices = data.get("choices", [])
        if not choices:
            raise RuntimeError("Empty response from LLM")

        return choices[0].get("message", {}).get("content", "").strip()
