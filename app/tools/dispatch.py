# 文件说明：校验工具输入并注入服务端身份，分发业务查询、准备和事项流程。
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
        if tool.effect in {'prepare','claim','dispatch'}:
            from app.tasks.operations import TaskOperations
            result=TaskOperations(ctx.runs.store,ctx.runs.user,scope=ctx.runs.candidates.scope).prepare(
                ctx.request.session_id,ctx.record.task_id,ctx.record.version,name,
                arguments=args,fence=(ctx.request,ctx.payload),timeout=ctx.call_timeout())
            ctx.operation_id=result['operation_id']
            return ok(dict(result,next_state='needs_confirmation'))
        return commerce.read(name,args)
    except ValidationError as error:
        # Field paths and stable error types let the model repair a call. Never
        # expose Pydantic input/ctx/msg: they can contain private user values.
        errors = [dict(path=list(item['loc']) if item['type'] != 'extra_forbidden' else [],
                       type=item['type'])
                  for item in error.errors(include_input=False, include_context=False, include_url=False)[:20]]
        return failure(BusinessError('invalid_arguments','参数类型、范围或必填字段不正确',
                                     validation_errors=errors))
    except PreparationRejected as error:
        return failure(BusinessError('insufficient_data', str(error)))
    except BusinessError as error:
        return failure(error)


def flow(name,args,ctx):
    if name=='recall_user_memory':
        return ok(ctx.recall_memory(**args))
    if name=='inspect_image':
        return ok(ctx.inspect_image(**args))
    if name=='read_task_context':
        return ok(ctx.read_progress({}))
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
