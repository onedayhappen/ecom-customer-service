"""Validated entrypoints and schemas shared by registry and direct callers."""
from pydantic import ValidationError

from app.tools import commerce_calculations as calculations, commerce_service as service
from app.tools import commerce_models as m
from app.tools.commerce_calculations import failure
from app.data.commerce_dao import CommerceRejection
from app.tools.mutation_context import request_id_for
from app.tools.request_context import get_current_user_id


CONTRACTS = {
    'query_offers': (m.OfferQuery, service.query_offers, '查询 MySQL 中条件一致的有效报价；不以目录价代替'),
    'query_promotions': (m.PromotionQuery, service.query_promotions, '查询 MySQL 中有效的结构化优惠规则'),
    'query_user_coupons': (m.CouponQuery, service.query_user_coupons, '查询当前用户的券、规则和生命周期状态'),
    'query_delivery_quote': (m.Basket, service.query_delivery_quote, '按本地配送规则计算指定商品和地区的运费、预计时效'),
    'query_price_history': (m.HistoryQuery, service.query_price_history, '查询历史价格样本，明确覆盖范围与价格口径'),
    'query_after_sale': (m.AfterSaleQuery, service.query_after_sale, '查询当前用户订单的退款、退货、换货、价保记录'),
    'query_invoice': (m.InvoiceQuery, service.query_invoice, '查询本地发票台账或只读开票预检，不提交申请'),
    'issue_invoice': (m.InvoiceWrite, None, '确认后创建本地演示发票记录，不开具税务发票'),
    'reissue_invoice': (m.InvoiceReissue, None, '确认后重开本地演示发票记录，不开具税务发票'),
    'evaluate_order_action': (m.OrderActionQuery, service.evaluate_order_action, '只读核验订单状态是否允许改址、取消或催办'),
    'query_handoff_status': (m.Empty, service.query_handoff_status, '查询当前会话的真实人工工单状态，不新建工单'),
    'query_mutation_status': (m.MutationQuery, service.query_mutation_status, '按本地请求回执或当前会话操作号查询写入状态；未知不能重试'),
    'query_payment_status': (m.PaymentQuery, service.query_payment_status, '查询当前用户订单的本地支付交易记录'),
    'check_coupon_eligibility': (m.CouponCheck, calculations.check_coupon_eligibility, '逐项核验输入券的资格，不领券或用券'),
    'compare_offers': (m.OfferComparison, calculations.compare_offers, '比较同条件有效报价的完整费用'),
    'summarize_price_history': (m.HistorySummary, calculations.summarize_price_history, '统计历史观测低价和覆盖信息，不预测未来'),
    'validate_policy_evidence': (m.PolicyCheck, calculations.validate_policy_evidence, '核验政策适用范围、生效时间及版本冲突'),
    'calc_promotion': (m.Calculation, calculations.calc_promotion, '以整数分计算固定购买方案的优惠、运费及应付，不下单'),
}
WRITE_TOOLS = frozenset({'issue_invoice', 'reissue_invoice'})


def invoke(name, arguments):
    model, function, _ = CONTRACTS[name]
    try:
        parsed = model.model_validate(arguments)
    except ValidationError as exc:
        return failure('validation_error', '工具参数不符合契约',
                       fields=['.'.join(map(str, e['loc'])) for e in exc.errors()])
    request_id = None
    if name in WRITE_TOOLS:
        request_id = request_id_for(get_current_user_id(), name, arguments)
        if not request_id:
            return failure('confirmation_invalid', '需要服务端校验通过的操作确认，不能直接执行开票')
    try:
        if name in WRITE_TOOLS:
            return service.write_invoice(parsed, name, request_id)
        return function(parsed)
    except CommerceRejection as exc:
        return failure(exc.code, str(exc), **({'request_id': request_id} if request_id else {}))
    except (ValidationError, ValueError, KeyError, TypeError):
        # Invalid stored evidence must not look like valid business facts.
        return failure('evidence_conflict', '数据源记录不符合工具契约，需核实数据')
    except Exception:
        return failure('transient_failure', '本地业务数据源暂不可用，请检查 MySQL 连接和初始化表',
                       **({'request_id': request_id, 'status': 'unknown'} if request_id else {}))


def _entrypoint(name):
    def call(**kwargs):
        return invoke(name, kwargs)
    call.__name__ = name
    call.__doc__ = CONTRACTS[name][2]
    return call


TOOL_MAP = {name: _entrypoint(name) for name in CONTRACTS}
globals().update(TOOL_MAP)


def _schema(model):
    schema = model.model_json_schema()
    definitions = schema.get('$defs', {})
    def inline(value):
        if isinstance(value, list):
            return [inline(v) for v in value]
        if isinstance(value, dict):
            if '$ref' in value:
                return inline(definitions[value['$ref'].split('/')[-1]])
            return {k: inline(v) for k, v in value.items() if k not in ('$defs', 'title')}
        return value
    return inline(schema)


TOOL_DEFINITIONS = [
    {'type': 'function', 'function': {'name': name, 'description': description,
                                     'parameters': _schema(model)}}
    for name, (model, _, description) in CONTRACTS.items()
]
