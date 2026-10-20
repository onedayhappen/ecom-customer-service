"""Conversation-first chat: label hint → Agent → guarded business tools.

There is deliberately no query-rewrite gate or classifier-driven task adoption.
"""
import json

from app.agents.engine import AgentEngine
from app.config.settings import settings
from app.data_plan.enums import Execution, Intent
from app.execution.types import ExecutionResult
from app.memory.case_manager import CaseResolution
from app.memory.case_postgres import create_case_manager
from app.observability.llm_client import LlmClient
from app.reliability import MutationGuard
from app.tools.registry import execute_tool


def associate_pending_messages(store, batch, *, engine=None):
    """Internal new-pipeline entry. No real request is sent to both pipelines."""
    from app.tasks.association import AssociationService
    if engine is None:
        from app.api.main import _get_openai_client
        engine = AgentEngine(Intent.GREETINGS, client=LlmClient(
            raw_client=_get_openai_client(), model=settings.deepseek_model_name,
            default_scene="association"), model=settings.deepseek_model_name)
    return AssociationService(store, batch.user_id).process(batch, engine)


def handle_conversation(request, user_id, tracer, observer):
    # Late import keeps the API models/helpers replaceable in integration tests.
    from app.api import main
    from app.intent import recognize
    from app.tools.memory_tool import memory_prompt_sections
    sid, history = main._build_engine(user_id, request.session_id, query=request.message)
    request.session_id = sid
    context = [*history, {'role': 'user', 'content': request.message}]
    observer.enter('INTENT')
    intent = recognize(request.message, messages=context)
    observer.leave('INTENT', to_state='PLAN', intent=intent.value)
    if tracer is not None:
        tracer.record('intent', intent=intent.value, source='classifier_hint')

    manager = create_case_manager(user_id)
    guard = MutationGuard(manager, sid)
    token = request.confirmation_token
    if request.message.strip().strip('。！! ') not in {'确认', '确认执行', '是', '是的', '同意'}:
        token = None
    pending = guard.peek_confirmation(token) if token else None
    turn_id = manager.begin_turn(sid, request.message, CaseResolution())
    observer.enter('EXECUTE', execution='react')
    if token and pending is None:
        result = ExecutionResult(intent, Execution.REACT,
            '确认已失效或操作内容已变化，请重新核对具体操作后再确认。', blocked=True)
    elif pending:
        # The confirmation resumes exactly the audited action; no model replanning.
        outcome = guard.execute(pending['intent'], pending['action'], pending['arguments_dict'], token, execute_tool)
        if outcome.result.get('success'):
            reply = outcome.result.get('message') or '操作已提交，请以业务系统返回的处理结果为准。'
            if pending['action'] in {'issue_invoice', 'reissue_invoice'}:
                reply = '本地演示发票记录已生成；本次未开具真实税务发票。'
        else:
            reply = f"操作未完成：{outcome.result.get('error', '结果待核实，请联系人工客服')}"
        result = ExecutionResult(Intent(pending['intent']), Execution.REACT, reply, replayed=outcome.replayed)
        if not outcome.result.get('success'):
            status = guard.query_idempotency_status(pending['intent'], pending['action'], pending['arguments_dict'])
            result.needs_human = status.get('status') == 'unknown' or bool(status.get('stale'))
        result.add_step(pending['action'], pending['arguments_dict'], outcome.result)
    else:
        context = [*memory_prompt_sections(request.message, context=context, session_id=sid), *context]
        engine = AgentEngine(intent, client=LlmClient(raw_client=main._get_openai_client(),
                             model=settings.deepseek_model_name), model=settings.deepseek_model_name)
        if tracer is not None:
            tracer.record('plan', execution='react', tools=list(engine.plan.tools), retrievable=True)
        result = engine.execute(request.message, messages=context, mutation_guard=guard,
                                historical_evidence=manager.recent_tool_evidence(sid))
    observer.leave('EXECUTE', to_state='RESPONSE', execution='react')
    for step in result.tool_steps:
        manager.record_tool_result(sid, turn_id, step.name, json.dumps(step.result, ensure_ascii=False))
    manager.record_response(sid, turn_id, result.reply)
    main._persist_session_turn(sid, user_id, request.message, result.reply)
    follow_up = '请核对操作内容后确认。' if result.needs_confirmation else None
    if result.needs_human:
        follow_up = '需要人工继续核实处理。'
    if tracer is not None:
        tracer.set_tool_steps(result.tool_steps)
        tracer.set_response(reply=result.reply, requires_human=result.needs_human,
                            follow_up_question=follow_up, intent=result.intent.value,
                            execution='react', confidence=0.80)
    return main.ChatResponse(session_id=sid, reply=result.reply, intent=result.intent.value,
                            confidence=0.80, requires_human=result.needs_human,
                            follow_up_question=follow_up, confirmation_token=result.confirmation_token,
                            question=result.question,
                            task_outcome='needs_human' if result.needs_human else 'paused')
