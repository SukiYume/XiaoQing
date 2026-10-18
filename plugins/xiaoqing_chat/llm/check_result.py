"""回复审查的结果类型，以及协议不可用、证据缺失时的标准拒绝结果。"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class ReplyCheckResult:
    suitable: bool
    reason: str
    need_replan: bool
    # hard：上下文、说话人、人物经历、事实或结构错误，任何情况下都不能发送。
    # soft：口癖、措辞、节奏等风格问题，重生成耗尽后可在强制回复场景谨慎采用。
    # infra：远程检查器超时、不可用或返回了无效协议；确定性检查已通过，可受控放行。
    severity: str = "hard"
    # 只标识调用方能够安全恢复的通用失败类别，不传递具体话题或个案。
    failure_code: str        = ""
    persona_claim_count: int = 0
    context_claim_count: int = 0

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
    def __init__(self, reason: str, need_replan: bool) -> None:
        super().__init__(reason)
        self.reason      = reason
        self.need_replan = need_replan


def persona_contract_rejection(reason: str) -> ReplyCheckResult:
    """人物证据协议不完整时失败关闭，使明确点名场景可安全承接。"""

    return ReplyCheckResult(
        suitable     = False,
        reason       = reason,
        need_replan  = True,
        severity     = "hard",
        failure_code = "persona_grounding",
    )


def context_contract_rejection(reason: str) -> ReplyCheckResult:
    """对话事实来源协议不完整时失败关闭。"""

    return ReplyCheckResult(
        suitable     = False,
        reason       = reason,
        need_replan  = True,
        severity     = "hard",
        failure_code = "context_grounding",
    )
