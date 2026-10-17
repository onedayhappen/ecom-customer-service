"""Server-side preparation of concrete, reviewable business mutations."""
from dataclasses import dataclass, field
import json

from pydantic import ValidationError

from app.execution.types import ToolStep


MUTATION_INTENTS = {
    'apply_refund': 'refund_service',
    'issue_invoice': 'invoice_request',
    'reissue_invoice': 'invoice_request',
}


@dataclass
class Proposal:
    summary: str = ''
    error: str = ''
    steps: list[ToolStep] = field(default_factory=list)


def prepare_proposal(name, arguments, execute) -> Proposal:
    """Validate fields and current ownership before issuing a confirmation token.

    Actual writes must still recheck their business preconditions atomically.
    No model-provided confirmation flag or intent label is trusted here.
    """
    proposal = Proposal()
    def read(tool, args):
        raw = execute(tool, args)
        result = json.loads(raw) if isinstance(raw, str) else raw
        if not isinstance(result, dict):
            result = {'success': False, 'error': '查询返回格式异常'}
        proposal.steps.append(ToolStep(tool, args, result))
        return result

    if name not in MUTATION_INTENTS:
        proposal.error = '该写操作尚未接入确认预检，不能执行'
        return proposal
    order_id = arguments.get('order_id')
    if not isinstance(order_id, str) or not order_id.strip() or len(order_id) > 40:
        proposal.error = '需要先明确订单号'
        return proposal
    if name == 'apply_refund':
        reason = arguments.get('reason')
        if (set(arguments) != {'order_id', 'reason'} or not isinstance(reason, str)
                or not reason.strip() or len(reason) > 255):
            proposal.error = '退款需要明确订单号和退款原因'
            return proposal
        result = read('query_order', {'order_id': order_id})
        order = result.get('order') or {}
        if not result.get('success') or order.get('order_id') != order_id:
            proposal.error = '无法核实该订单属于当前用户，不能申请退款'
            return proposal
        proposal.summary = f'请确认：为订单 {order_id} 申请退款，原因：{reason}。确认后提交申请，是否受理以业务系统结果为准。'
        return proposal

    from app.tools.commerce import CONTRACTS
    try:
        parsed = CONTRACTS[name][0].model_validate(arguments)
    except ValidationError as exc:
        fields = ', '.join('.'.join(map(str, e['loc'])) for e in exc.errors())
        proposal.error = f'发票字段不完整或不合法：{fields}'
        return proposal
    args = {'order_id': order_id, 'mode': 'preview', 'fields': parsed.fields.model_dump()}
    if name == 'reissue_invoice':
        args['invoice_id'] = parsed.invoice_id
    result = read('query_invoice', args)
    if not result.get('success') or not result.get('can_issue'):
        proposal.error = '当前无法通过发票预检，不能提交申请'
        return proposal
    if result.get('preview_version') != parsed.preview_version:
        proposal.error = '订单或发票规则已变化，请使用最新预检结果重新准备申请'
        return proposal
    fields = parsed.fields
    kind = {'personal': '个人', 'company': '公司', 'vat_special': '增值税专用'}.get(fields.invoice_type, fields.invoice_type)
    tax_id = f'，税号：{fields.tax_id}' if fields.tax_id else ''
    action = '重开' if name == 'reissue_invoice' else '创建'
    original = f'，原票：{parsed.invoice_id}' if name == 'reissue_invoice' else ''
    proposal.summary = f'请确认：为订单 {order_id} {action}{kind}发票，抬头：{fields.title}{tax_id}{original}。本次仅生成本地演示记录，不开具真实税务发票。'
    return proposal
