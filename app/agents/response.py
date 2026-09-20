"""Agent 对外返回的标准化响应模型（新链路）。

intent 字段使用 17 类业务任务级标签（:class:`app.data_plan.enums.Intent`），
与训练集 data_v3.jsonl 一致；旧 IntentType（3 通道路由的 12 类）已随旧代码移除。
"""

from typing import Optional

from pydantic import BaseModel, Field

from app.data_plan.enums import Intent


class CustomerServiceResponse(BaseModel):
    """客服回复的结构化输出"""

    intent: Intent = Field(description="识别到的用户意图（17 类业务任务级）")
    confidence: float = Field(
        description="意图识别的置信度，0.0-1.0", ge=0.0, le=1.0
    )
    reply: str = Field(description="给用户的回复内容")
    requires_human: bool = Field(
        description="是否需要转接人工客服", default=False
    )
    follow_up_question: Optional[str] = Field(
        description="需要向用户进一步确认的问题，如果不需要则为 null",
        default=None,
    )
