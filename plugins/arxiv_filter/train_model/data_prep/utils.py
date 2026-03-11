"""
train_model 公共工具函数
"""

import re


def clean_arxiv_id(raw_id: str) -> str:
    """
    清理 arXiv ID：去版本号、去空白、归一化位数。

    arXiv 在 2015-01 起从 4 位 ID (YYMM.NNNN) 切换到 5 位 (YYMM.NNNNN)。
    用户笔记中有时对旧论文写成 5 位带尾零 (1208.44980)，但 API 返回 canonical
    4 位 ID (1208.4498)。此函数统一归一化：pre-2015 → 4 位，post-2015 → 5 位。

    Examples:
        '2301.12345v2'                       → '2301.12345'
        'http://arxiv.org/abs/2301.12345v1'  → '2301.12345'
        '1208.44980'                         → '1208.4498'   (pre-2015, strip trailing 0)
        '2301.1234'                          → '2301.12340'  (post-2015, pad to 5 digits)
    """
    aid = str(raw_id).strip()
    aid = re.sub(r"^https?://(?:www\.)?arxiv\.org/(?:abs|pdf)/", "", aid, flags=re.IGNORECASE)
    aid = re.sub(r"^arxiv:", "", aid, flags=re.IGNORECASE)
    aid = re.sub(r"\.pdf$", "", aid, flags=re.IGNORECASE)
    # 去版本号
    aid = re.sub(r"v\d+$", "", aid)
    # 去多余空白
    aid = re.sub(r"\s+", "", aid)
    legacy_match = re.match(r"^([a-z\-.]+)/([0-9]{7})$", aid, flags=re.IGNORECASE)
    if legacy_match:
        archive = legacy_match.group(1).lower()
        return f"{archive}/{legacy_match.group(2)}"
    # 归一化 ID 位数：pre-2015 → 4 位, post-2015 → 5 位
    m = re.match(r"^(\d{4})\.(\d{4,5})$", aid)
    if m:
        yymm, num = m.group(1), m.group(2)
        if int(yymm) < 1501:
            num = num[:4]  # pre-2015: canonical 4 digits
        elif len(num) < 5:
            num = num.ljust(5, "0")  # post-2015: canonical 5 digits
        aid = f"{yymm}.{num}"
    return aid
