# 文件说明：跨真实 API 进程测试消息接收、回复发布与 SSE 重连补拉。
"""Real HTTP/SSE replay across API processes using isolated PostgreSQL and Redis."""
import asyncio
import json
import os
from types import SimpleNamespace
from uuid import uuid4

import httpx
import pytest

from app.tasks.conversation_worker import ConversationWorker
from app.tasks.replies import ReplyCoordinator
from tests.test_platform_http import platform
from tests.test_task_execution import engine, local_budgets
from tests.test_task_scheduling import QueryClient, drain, result
from tests.test_message_association import new, proposal

pytestmark = pytest.mark.skipif(os.getenv("ECOM_INTEGRATION") != "1", reason="isolated real infrastructure required")


def test_intake_publication_and_reconnect_from_another_api_instance(platform):
    _, url = platform["start"]("api", TASK_INTAKE_ENABLED="true")
    sid = str(uuid4())
    client, headers, store = platform["client"], platform["headers"](), platform["store"]
    bodies = [dict(session_id=sid, request_id=str(uuid4()), message=oid + " 查询订单") for oid in ["ORD-A", "ORD-B"]]
    for body in bodies:
        assert client.post(url + "/api/v1/task-messages", headers=headers, json=body).status_code == 202
    assert client.post(url + "/api/v1/task-messages", headers=headers, json=bodies[0]).status_code == 202
    before = client.get(url + f"/api/v1/sessions/{sid}/transcript", headers=headers).json()["cursor"]
    agent = engine(QueryClient())
    def associate(context):
        batch = SimpleNamespace(session_id=context.session_id, messages=context.current_messages)
        return proposal(*(new(batch, index=i, goal="查询订单", details={"order_id": m["content"].split()[0]})
                          for i, m in enumerate(batch.messages)))
    agent.associate = associate
    worker = ConversationWorker(store, agent, tools=result)
    try:
        worker._associate(worker.intake.claim_next())
        worker.reconcile()
        with store.transaction(read_only=True) as tx:
            ids = [r["job_id"] for r in tx.all("SELECT job_id FROM runtime_jobs WHERE session_id=? AND kind='task_execution'", (sid,))]
        drain(worker.executions, ids, user="customer")
        sent = ReplyCoordinator(store, "customer").publish(sid)
        assert len(sent["result_ids"]) == 2
        async def replay():
            async with httpx.AsyncClient(trust_env=False, timeout=15) as connection:
                async with connection.stream("GET", platform["urls"][1] + f"/api/v1/sessions/{sid}/support-events",
                    headers={**headers, "Last-Event-ID": str(before)}) as response:
                    assert response.status_code == 200
                    async for line in response.aiter_lines():
                        if line.startswith("data: "):
                            return json.loads(line[6:])
        recovered = asyncio.run(replay())
        visible = [e for e in recovered["events"] if e["kind"] == "message" and e["payload"]["role"] == "assistant"]
        assert [e["payload"]["event_id"] for e in visible] == [sent["event_id"]]
        again = client.get(url + f"/api/v1/sessions/{sid}/messages?after={recovered['cursor']}", headers=headers).json()
        assert again["events"] == []
        refreshed = client.get(url + f"/api/v1/sessions/{sid}/transcript", headers=headers).json()
        assert len([e for e in refreshed["events"] if e["payload"].get("event_id") == sent["event_id"]]) == 1
        assert client.get(url + f"/api/v1/task-messages/{sid}", headers=headers).json()["processing"] == "idle"
    finally:
        worker.close()
