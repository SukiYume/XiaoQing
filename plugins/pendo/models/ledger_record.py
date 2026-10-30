"""一次性记账的结构化输入；命令与 Agent 工具共享字段契约。"""

from __future__ import annotations

import re
from typing import Literal, Self

from pydantic import BaseModel, ConfigDict, Field, model_validator

from ..config import LEDGER_EXPENSE_CATEGORIES, LEDGER_INCOME_CATEGORIES


class LedgerRecord(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, str_strip_whitespace=True)

    amount_cents: int = Field(
        gt=0, description="金额的最小货币单位，人民币为分。例如99.47元填9947。"
    )
    title: str = Field(
        min_length  = 1,
        max_length  = 200,
        description = "交易对象或用途，保留原词；分类、账户分别填写独立字段。",
    )
    transaction_type: Literal["expense", "income", "transfer"] = "expense"
    category        : str                                      = Field(
        default     = "",
        description = "用户给出的分类名称；收支类型单独用transaction_type填写。未指定时留空，采用其他。",
    )
    account      : str = Field(default="", description="用户给出的账户名称。未指定时留空，采用现金。")
    category_from: str = Field(
        default="", description="用户要求沿用历史分类时填写已查询到的账目ID，category留空。"
    )
    account_from: str = Field(
        default="", description="用户要求沿用历史账户时填写已查询到的账目ID，account留空。"
    )
    to_account : str = Field(default="", description="转账的转入账户，转账时必填。")
    merchant   : str = Field(default="", description="明确提供的商户，可留空。")
    date       : str = Field(default="", description="YYYY-MM-DD，留空采用用户当地今天。")
    currency   : str = Field(default="CNY", description="币种代码，例如CNY、USD。")
    remark     : str = ""
    source_text: str = Field(
        default="", exclude=True, description="执行器传入的发起人原话，用于校验字段来源。"
    )

    @model_validator(mode="after")
    def ground_fields(self) -> Self:
        if self.transaction_type == "transfer" and not self.to_account:
            raise ValueError(
                "转账的to_account转入账户必填。根据原话填写；用户尚未指定时先确认，本次没有保存账目。"
            )
        if self.category_from and self.category or self.account_from and self.account:
            raise ValueError(
                "字段值与历史来源分别选用一种：category/category_from，account/account_from。"
            )
        if not self.source_text:
            return self
        categories = {item["name"] for item in LEDGER_EXPENSE_CATEGORIES + LEDGER_INCOME_CATEGORIES}
        for label, value, default in (
            ("分类", self.category, "其他"),
            ("账户", self.account, "现金"),
            ("转入账户", self.to_account, ""),
        ):
            if value and value != default and value.casefold() not in self.source_text.casefold():
                if label == "转入账户":
                    raise ValueError(
                        f"转入账户“{value}”缺少原话依据。根据原话填写；用户尚未指定时先确认，本次没有保存账目。"
                    )
                raise ValueError(
                    f"{label}“{value}”缺少原话依据。填写用户给出的名称；用户未指定时留空采用默认值。"
                )
        literal_category = any(
            left + self.category + right in self.source_text
            for left, right in [('"', '"'), ("“", "”"), ("‘", "’")]
        )
        category_phrase = re.fullmatch(
            r"(.+?)(?:类(?:收入|支出|消费)?|收入|支出|消费)", self.category
        )
        if not literal_category and category_phrase and category_phrase[1] in categories:
            self.category = category_phrase[1]
        quoted = any(
            left + self.title + right in self.source_text
            for left, right in [('"', '"'), ("“", "”"), ("‘", "’")]
        )
        if not quoted:
            parts = re.split(r"[\s,，;；|]+", self.title)
            if not self.category and not self.category_from and len(parts) > 1:
                candidates = {part for part in (parts[0], parts[-1]) if part in categories}
                if len(candidates) == 1:
                    category = candidates.pop()
                    if re.search(
                        rf"(?:^|[\s,，;；|]){re.escape(category)}(?:$|[\s,，;；|])",
                        self.source_text,
                    ):
                        self.category = category
            metadata = {value for value in (self.category, self.account, self.to_account) if value}
            while len(parts) > 1 and parts[0] in metadata:
                parts.pop(0)
            while len(parts) > 1 and parts[-1] in metadata:
                parts.pop()
            self.title = " ".join(parts)
        return self
