"""回复审查的结果类型，以及协议不可用、证据缺失时的标准拒绝结果。"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class ReplyCheckResult:
    suitable   : bool
    reason     : str
    need_replan: bool
    # hard：上下文、说话人、人物经历、事实或结构错误，任何情况下都不能发送。
    # soft：口癖、措辞、节奏等风格问题，重生成耗尽后可在强制回复场景谨慎采用。
    # infra：远程检查器超时、不可用或返回了无效协议；确定性检查已通过，可受控放行。
    severity: str = "hard"
    # 只标识调用方能够安全恢复的通用失败类别，不传递具体话题或个案。
    failure_code       : str = ""
    persona_claim_count: int = 0
    context_claim_count: int = 0
    # 只有这几处说法没依据、其余都审过时列出来：调用方可以删掉这几个气泡照发其余的。
    unsupported_fragments: tuple[str, ...] = ()

    @property
    def is_hard(self) -> bool:
        return not self.suitable and self.severity == "hard"


def checker_unavailable(reason: str) -> ReplyCheckResult:
    """检查服务故障时保留确定性门禁，但不把基础设施故障误判成内容错误。"""

    return ReplyCheckResult(
        suitable    = True,
        reason      = reason,
        need_replan = False,
        severity    = "infra",
    )


class ReplyRejected(RuntimeError):
    """这一稿被审查拒绝、没有可发送的回复，需要重新规划。"""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


def hard_rejection(reason: str, failure_code: str) -> ReplyCheckResult:
    """这一稿不能发送、需要重新规划：一类确定性的硬拒绝。"""

    return ReplyCheckResult(
        suitable     = False,
        reason       = reason,
        need_replan  = True,
        severity     = "hard",
        failure_code = failure_code,
    )


def persona_contract_rejection(reason: str) -> ReplyCheckResult:
    """人物证据协议不完整、或说法没有人物资料依据时拒绝，使明确点名场景可安全承接。"""

    return hard_rejection(reason, "persona_grounding")


def context_contract_rejection(reason: str) -> ReplyCheckResult:
    """对话事实来源协议不完整、或说法没有对话依据时拒绝。"""

    return hard_rejection(reason, "context_grounding")
