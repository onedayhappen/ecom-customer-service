"""Opt-in live association smoke evaluation using synthetic retail conversations.

Run: python -m scripts.eval_association --live
No business tools, customer data or production database are used. The model sees
only synthetic source events in a temporary SQLite database.
"""
import argparse
from datetime import datetime, timezone
import json
import time
from collections import Counter
from pathlib import Path
from tempfile import TemporaryDirectory
from uuid import uuid4

from app.agents.engine import AgentEngine
from app.config.settings import settings
from app.data_plan.enums import Intent
from app.observability.llm_client import LlmClient
from app.support.store import SupportStore
from app.tasks.association import AssociationRejected, AssociationService
from app.tasks.intake import MessageIntake
from app.tasks.repository import TaskRepository


CASES = [
    dict(name="two_goals", tasks=[], text="耳机退款，鞋子查物流", expected=["create", "create"]),
    dict(name="answer_reason", tasks=[dict(key="ear", goal="耳机退货", order="ORD-DEMO-A", ask=True)],
         text="左耳没声音", expected=["update:ear"], detail=("reason", "左耳没声音")),
    dict(name="ambiguous_yes", tasks=[dict(key="ear", goal="耳机退货", order="ORD-DEMO-A", ask=True),
                                    dict(key="shoe", goal="鞋子退货", order="ORD-DEMO-B", ask=True)],
         text="是的", expected=["clarify"]),
    dict(name="cross_session", tasks=[dict(key="ear", goal="耳机退货", order="ORD-DEMO-A", old=True)],
         text="继续上次 ORD-DEMO-A 的耳机退货", expected=["update:ear"]),
    dict(name="new_goal_same_order", tasks=[dict(key="ear", goal="耳机退货", order="ORD-DEMO-A")],
         text="ORD-DEMO-A 另外还要开发票", expected=["create"]),
    dict(name="explicit_order", tasks=[dict(key="ear-a", goal="耳机退货", order="ORD-DEMO-A"),
                                     dict(key="ear-b", goal="耳机退货", order="ORD-DEMO-B")],
         text="ORD-DEMO-B 左耳没声音", expected=["update:ear-b"]),
    dict(name="similar_history", tasks=[dict(key="ear-a", goal="耳机退货", order="ORD-DEMO-A", old=True),
                                      dict(key="ear-b", goal="耳机退货", order="ORD-DEMO-B", old=True)],
         text="继续上次那个耳机退货", expected=["clarify"]),
    dict(name="goal_change", tasks=[dict(key="ear", goal="排查耳机左耳没声音", order="ORD-DEMO-A")],
         text="ORD-DEMO-A 这副耳机还是退货吧，不排查了", expected=["update:ear"]),
    dict(name="conditional_quantity", tasks=[], text="ORD-DEMO-A 耳机只退一件，只有免运费才退，否则先不退。",
         expected=["create"], detail=("quantity", 1), conditions=True),
    dict(name="correct_reason", tasks=[dict(key="ear", goal="耳机退货", order="ORD-DEMO-A")],
         text="ORD-DEMO-A 更正退货原因：不是不想要了，是左耳没声音。", expected=["update:ear"],
         detail=("reason", "左耳没声音")),
    dict(name="missing_business_details", tasks=[], text="帮我退货", expected=["create"], empty_details=True),
    dict(name="generic_question_selection", tasks=[dict(
        key="ear", goal="耳机退货", order="ORD-DEMO-A", question=dict(
            field="details", prompt="这副耳机是什么问题？1. 左耳没声音；2. 右耳没声音。",
            options=[dict(label="左耳没声音", value="receipt-option-left"),
                     dict(label="右耳没声音", value="receipt-option-right")]))],
         text="第二个", expected=["update:ear"], empty_details=True,
         goal_contains=["耳机", "右耳没声音"], effect="answer"),
    dict(name="append_conditions", tasks=[dict(
        key="ear", goal="耳机退货，只有免运费才退", order="ORD-DEMO-A",
        details=dict(conditions="只有免运费才退"))],
         text="ORD-DEMO-A 还必须原路退款", expected=["update:ear"],
         detail=("conditions", "只有免运费才退"), goal_contains=["免运费", "原路退款"]),
    dict(name="verbatim_invoice_type", tasks=[dict(
        key="invoice", goal="ORD-DEMO-A 开发票", order="ORD-DEMO-A", question=dict(
            field="invoice_type", prompt="发票类型是个人还是公司？", options=[]))],
         text="公司", expected=["update:invoice"], detail=("invoice_type", "公司"), effect="answer"),
    dict(name="cancel_order_is_business_goal", tasks=[], text="帮我取消 ORD-DEMO-A 订单",
         expected=["create"], effect="new", goal_contains=["取消"]),
    dict(name="incomplete_candidate_context", tasks=[], text="帮我退货", expected=["clarify"],
         gaps=["history_unavailable"]),
]


def fixture(store, case):
    user, sid = "synthetic-eval-user", "eval-current"
    store.ensure(sid, user)
    store.ensure("eval-history", user)
    repo, records = TaskRepository(store, user), {}
    for spec in case["tasks"]:
        session = "eval-history" if spec.get("old") else sid
        with store.transaction() as tx:
            c = store._control(tx, session, user)
            seq = store._event(tx, c, "message", user, {"role": "user", "content": spec["order"] + " " + spec["goal"]})
        source = dict(kind="message", session_id=session, sequence=seq)
        record = repo.create(session, change_id=str(uuid4()), source=source, goal=spec["goal"],
                             details={"order_id": spec["order"], "item_name": "鞋子" if "鞋" in spec["goal"] else "耳机",
                                      **spec.get("details", {})})
        if spec.get("ask") or spec.get("question"):
            record = repo.ask(record.task_id, session, expected_version=record.version, change_id=str(uuid4()), source=source,
                              question=spec.get("question") or dict(field="reason", prompt="请说明这件商品哪里出了问题"))
            with store.transaction() as tx:
                c = store._control(tx, session, user)
                store._event(tx, c, "message", "bot", {
                    "role": "assistant", "content": record.waiting_for["prompt"],
                    "task_id": record.task_id, "task_version": record.version,
                    "question_id": record.waiting_for["question_id"],
                })
        records[record.task_id] = spec["key"]
    intake = MessageIntake(store)
    intake.receive(sid, user, request_id=str(uuid4()), content=case["text"])
    return intake.claim(sid, user), records


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--live", action="store_true", help="Explicitly authorize the synthetic model calls")
    parser.add_argument("--output", default="data/evaluation/association_smoke.json")
    args = parser.parse_args()
    if not args.live:
        parser.error("--live is required; no model call was made")
    from openai import OpenAI
    settings.memory_enabled = settings.short_term_memory_enabled = False
    raw = OpenAI(api_key=settings.openai_api_key, base_url=settings.openai_base_url, timeout=25, max_retries=0)
    from scripts.eval_measurement import Measurement
    measurement = Measurement(raw)
    client = LlmClient(raw_client=raw, model=settings.deepseek_model_name,
                       default_scene="association_eval", audit_errors_only=True)
    engine = AgentEngine(Intent.GREETINGS, client=client, model=settings.deepseek_model_name)
    results = []
    with TemporaryDirectory(prefix="ecom-association-eval-") as directory:
        for index, case in enumerate(CASES):
            store = SupportStore(f"sqlite:///{Path(directory) / (str(index) + '.db')}")
            batch, names = fixture(store, case)
            service = AssociationService(store, batch.user_id)
            service.intake.renew(batch, 60)
            snapshot = service.snapshot(batch)
            snapshot.context.gaps.extend(case.get("gaps", []))
            result = dict(name=case["name"], input=case["text"], expected=case["expected"])
            since, started = len(measurement.calls), time.monotonic()
            try:
                proposal = engine.associate(snapshot.context)
                result["structured_output"] = proposal.model_dump()
                for decision in result["structured_output"]["decisions"]:
                    if decision["task_id"]:
                        decision["task_id"] = names.get(decision["task_id"], "unknown")
                    decision["candidate_task_ids"] = [names.get(t, "unknown") for t in decision["candidate_task_ids"]]
                predicted = [d.action + (":" + names.get(d.task_id, "unknown") if d.action == "update" else "")
                             for d in proposal.decisions]
                result["predicted"] = predicted
                result['missing_decisions'] = sum((Counter(case['expected']) - Counter(predicted)).values())
                result["misassociation"] = sorted(predicted) != sorted(case["expected"]) and "clarify" not in predicted
                result["unnecessary_clarification"] = "clarify" in predicted and "clarify" not in case["expected"]
                committed = service.commit(batch, snapshot, proposal)
                correct = sorted(predicted) == sorted(case["expected"])
                written = [service.repo.get(d["task_id"]) for d in committed["decisions"] if d["task_id"]]
                if case.get("empty_details"):
                    correct &= all(not decision.details for decision in proposal.decisions)
                if "effect" in case:
                    correct &= all(decision.effect == case["effect"] for decision in proposal.decisions)
                if "goal_contains" in case:
                    correct &= any(all(word in record.goal for word in case["goal_contains"]) for record in written)
                if "detail" in case:
                    key, value = case["detail"]
                    correct &= any(record.details.get(key) == value for record in written)
                if case.get("conditions"):
                    correct &= any(all(word in str(record.details.get("conditions", "")) for word in ("免运费", "否则", "不退")) for record in written)
                result["passed"] = bool(correct)
                result["guard"] = "accepted"
            except Exception as exc:
                result.update(passed=False, guard=exc.code if isinstance(exc, AssociationRejected) else type(exc).__name__)
                from pydantic import ValidationError
                if isinstance(exc, ValidationError):
                    result["schema_errors"] = [dict(path=list(e["loc"]), error=e["msg"]) for e in exc.errors(include_input=False)]
            result['measurement'] = measurement.report(since)
            result['elapsed_seconds'] = round(time.monotonic() - started, 3)
            results.append(result)
            print(json.dumps({k: result.get(k) for k in ("name", "passed", "guard", "predicted")}, ensure_ascii=False), flush=True)
    report = dict(
        evaluated_at=datetime.now(timezone.utc).isoformat(), model=settings.deepseek_model_name,
        dataset="synthetic retail scenarios; not a statistical production accuracy estimate",
        total=len(results), passed=sum(r["passed"] for r in results),
        misassociations=sum(bool(r.get("misassociation")) for r in results),
        unnecessary_clarifications=sum(bool(r.get("unnecessary_clarification")) for r in results),
        missing_decisions=sum(r.get('missing_decisions', len(r['expected'])) for r in results),
        measurement=measurement.report(),
        results=results,
    )
    path = Path(args.output)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    raw.close()
    print(json.dumps({k: report[k] for k in ("total", "passed", "misassociations", "unnecessary_clarifications")}))
    return 0 if report["passed"] == report["total"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
