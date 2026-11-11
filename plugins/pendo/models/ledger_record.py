"""一次性记账的结构化输入（``/pendo ledger record <JSON>``）。"""

from __future__ import annotations

import re
from typing import Literal, Self

from pydantic import BaseModel, ConfigDict, Field, model_validator

from ..config import LEDGER_EXPENSE_CATEGORIES, LEDGER_INCOME_CATEGORIES

_CATEGORIES      = {item["name"] for item in LEDGER_EXPENSE_CATEGORIES + LEDGER_INCOME_CATEGORIES}
_CATEGORY_PHRASE = re.compile(r"(.+?)(?:类(?:收入|支出|消费)?|收入|支出|消费)")


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
        description = "分类名称；收支类型单独用transaction_type填写。留空采用其他。",
    )
    account      : str = Field(default="", description="账户名称。留空采用现金。")
    category_from: str = Field(
        default="", description="用户要求沿用历史分类时填写已查询到的账目ID，category留空。"
    )
    account_from: str = Field(
        default="", description="用户要求沿用历史账户时填写已查询到的账目ID，account留空。"
    )
    to_account: str = Field(default="", description="转账的转入账户，转账时必填。")
    merchant  : str = Field(default="", description="明确提供的商户，可留空。")
    date      : str = Field(default="", description="YYYY-MM-DD，留空采用用户当地今天。")
    currency  : str = Field(default="CNY", description="币种代码，例如CNY、USD。")
    remark    : str = ""

    @model_validator(mode="after")
    def check_fields(self) -> Self:
        if self.transaction_type == "transfer" and not self.to_account:
            raise ValueError("转账需要填写转入账户 to_account，本次没有保存账目。")
        if self.category_from and self.category or self.account_from and self.account:
            raise ValueError(
                "字段值与历史来源分别选用一种：category/category_from，account/account_from。"
            )
        # “餐饮类支出”“工资收入”这类写法归到已有分类，不另建一个分类。
        phrase = _CATEGORY_PHRASE.fullmatch(self.category)
        if phrase and phrase[1] in _CATEGORIES:
            self.category = phrase[1]
        return self
