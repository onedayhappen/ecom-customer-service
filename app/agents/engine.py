"""One Agent for message association and scoped task execution."""
from __future__ import annotations

from typing import Any

from app.data_plan.enums import Intent


class AgentEngine:
    """统一对话执行入口；分类结果不授予任何业务操作权限。"""

    def __init__(
        self,
        intent: Intent | str,
        *,
        client: Any | None = None,
        model: str = "",
        temperature: float = 0.0,
    ):
        self.intent = Intent(intent) if isinstance(intent, str) else intent
        self.client = client
        self.model = model
        self.temperature = temperature

    def associate(self, context):
        """Propose message ownership using the same client, never execute tools."""
        from app.agents.association import propose
        if self.client is None:
            raise ValueError("Association requires the configured conversation client")
        return propose(self.client, self.model, context)

    def execute_task(self, store, authenticated_user_id, request, *, tools=None, scope=None):
        """Advance one durable matter through this Agent's existing tool loop."""
        from app.tasks.execution import TaskExecutor
        return TaskExecutor(store, authenticated_user_id, tools=tools, scope=scope).run(request, self)
