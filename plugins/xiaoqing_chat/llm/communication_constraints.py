"""识别需要语义审查的明确交流要求。"""

import re

_NO_QUESTION_REQUEST = re.compile(
    r"(?:别|不要|不用|不必|不许|请勿)(?:再|总是|一直)?"
    r"(?:反问|追问|提问|问(?:我|问题)?|用问题(?:来)?收尾|以问题收尾)"
)


def forbids_followup_questions(text: str) -> bool:
    """仅提示本轮可能有交流约束，是否违规交给语义审查。"""
    return _NO_QUESTION_REQUEST.search(text) is not None
