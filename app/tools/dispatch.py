"""Schema validation and service dispatch shared by domain entry points."""
import json
import re
from pydantic import ValidationError
from app.tools.catalog import BY_NAME
from app.services.commerce import service
from app.services.commerce.common import BusinessError,digest,dumps,failure,ok
from app.tasks.runtime import current_task
from app.execution.task_proposals import PreparationRejected
from app.tools.request_context import get_current_user_id


def invoke(name,arguments):
    tool=BY_NAME[name]
    try:
        args=tool.arguments.model_validate(arguments).model_dump(by_alias=True)
        ctx=current_task()
        if not get_current_user_id(): raise BusinessError('invalid_arguments','需要登录身份')
        if tool.task_only and ctx is None: raise BusinessError('unsupported_capability','此工具需要可信事项上下文')
        if ctx:
            ctx.check()
            if ctx.runs.user!=get_current_user_id(): raise BusinessError('not_found','事项不属于当前用户')
        if tool.domain=='flow': return flow(name,args,ctx)
        commerce=service()
        if tool.effect=='quote':
            return commerce.quote_checkout(args)
        if tool.effect=='prepare':
            from app.tasks.operations import TaskOperations
            result=TaskOperations(ctx.runs.store,ctx.runs.user,scope=ctx.runs.candidates.scope).prepare(
                ctx.request.session_id,ctx.record.task_id,ctx.record.version,name,
                arguments=args,fence=(ctx.request,ctx.payload),timeout=ctx.call_timeout())
            ctx.operation_id=result['operation_id']
            return ok(dict(result,next_state='needs_confirmation'))
        if tool.effect in {'claim','dispatch'}:
            from app.memory.task_context import task_history
            history=task_history(ctx.runs,ctx.record)
            source=next((s['text'] for s in history['sources'] if s['session_id']==ctx.request.source.session_id and s['sequence']==ctx.request.source.sequence),'')
            # This direct write requires an explicit source-message command, not a generated task goal.
            pattern=r'(帮我|给我|我要|我想|请).{0,12}(领|领取).{0,20}券|^(领券|领取这张券)[。！!]*$' if tool.effect=='claim' else r'(帮我|给我|我要|我想|请).{0,12}催|^(催单|催发货)[。！!]*$'
            if not re.search(pattern,source) or re.search(r'不要|别领|不领|别催|不催|能否|能不能|是否|[?？]',source):
                raise BusinessError('not_eligible','需要用户明确要求办理此操作')
            request_id=tool.effect+':'+digest([ctx.record.task_id,ctx.record.version,ctx.request.source.model_dump(),args])
            return commerce.claim_coupon(args,request_id) if tool.effect=='claim' else commerce.request_dispatch(args,request_id)
        return commerce.read(name,args)
    except ValidationError:
        return failure(BusinessError('invalid_arguments','参数类型、范围或必填字段不正确'))
    except PreparationRejected as error:
        return failure(BusinessError('insufficient_data', str(error)))
    except BusinessError as error:
        return failure(error)


def flow(name,args,ctx):
    if name=='read_task_context':
        result=ctx.read_progress({})
        with ctx.runs.store.transaction(read_only=True) as tx:
            result['handoffs']=tx.all('''SELECT t.ticket_id,t.status,t.assignee,t.created_at,t.resolved_at
                FROM task_handoffs h JOIN support_tickets t ON t.ticket_id=h.ticket_id
                WHERE h.user_id=? AND h.task_id=? ORDER BY t.created_at DESC LIMIT 20''',(ctx.runs.user,ctx.record.task_id))
        return ok(result)
    if name=='ask_user':
        if re.search(r'密码|验证码|银行卡号|密钥|\btoken\b|api.?key',args['question'],re.I):
            raise BusinessError('invalid_arguments','不能索要登录或支付凭证')
        def contains(value,needle):
            if isinstance(value,dict): return any(contains(v,needle) for v in value.values())
            if isinstance(value,list): return any(contains(v,needle) for v in value)
            return value==needle
        if any(v.get('ref') and not contains(ctx.receipts,v['ref']) for v in args.get('choices') or []):
            raise BusinessError('invalid_arguments','选项引用必须来自已查询的结果')
        options=[{'label':v['label'],'value':v.get('ref') or v['label']} for v in args.get('choices') or []]
        result=ctx.save_progress({'expected_version':ctx.record.version,'state':'waiting_user',
            'question':{'field':'details','prompt':args['question'],'options':options}})
        if not result.get('success'): raise BusinessError('invalid_arguments','澄清问题不符合事项约束')
        return ok({'next_state':'waiting_user','question':args['question']})
    result=ctx.runs.finish(ctx.request,ctx.payload,{'state':'needs_human','reply':'已进入人工队列，客服将继续核实处理。',
        'needs_human':True,'reason':args['reason'],'task_version':ctx.record.version,
        'model_calls':ctx.model_calls,'tool_calls':ctx.tool_calls,'evidence_ids':[r['event_id'] for r in ctx.receipts]})
    return ok({'next_state':'needs_human','ticket_id':result.get('ticket_id'),'reply':result['reply']})


def bind(name):
    def call(**arguments): return invoke(name,arguments)
    call.__name__=name
    call.__doc__=BY_NAME[name].description
    return call
