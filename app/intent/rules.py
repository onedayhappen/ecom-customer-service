"""Conservative, conversation-aware fallback for the 31-label contract.

Rules classify goals only; they never authorize or execute business actions.
"""
from __future__ import annotations
import re
from app.data_plan.enums import Intent

_ORDER_ID_RE = re.compile(r"\bord-[a-z0-9-]+\b", re.I)
_DIALOG = {Intent.GREETINGS, Intent.CLOSURE, Intent.OTHERS}
_AFTER_SALE = {Intent.REFUND_SERVICE, Intent.EXCHANGE_SERVICE, Intent.PRICE_PROTECTION_SERVICE}
_NEGATION = re.compile(r"^(?:先|暂时)?(?:别|不要|不用|不再|停止|不想|不)(?:再)?(?:查|看|退|换|申请|办理|提交|催|投诉|转)")


def has_order_id(user_input: str) -> bool:
    return bool(_ORDER_ID_RE.search(user_input or ""))


def conversation_messages(user_input: str, messages: list[dict] | None) -> list[dict]:
    """Normalize history without mutation or leaking answers after the current turn."""
    history = [dict(m) for m in (messages or [])
               if isinstance(m, dict) and m.get("role") in {"user", "assistant"}
               and isinstance(m.get("content"), str)]
    last_user = next((i for i in range(len(history) - 1, -1, -1)
                      if history[i]["role"] == "user"), None)
    if last_user is not None and history[last_user]["content"].strip() == user_input.strip():
        return history[:last_user + 1]
    return [*history, {"role": "user", "content": user_input}]


def active_user_text(user_input: str) -> str:
    """Prefer the first explicit request; discard negated/background goals."""
    text = (user_input or "").strip()
    clauses = [c.strip() for c in re.split(r"[，,。；;！!\n]|然后|再(?=查|算|推荐|转人工|开发票)", text) if c.strip()]
    usable = [c for c in clauses if not _NEGATION.search(c)]
    if not usable:
        return text
    explicit = [c for c in usable if re.search(r"帮我|请|我要|我想|申请|办理|查|推荐|转人工|投诉|算一下|算算", c)
                and not re.match(r"如果|假如|要是|(?:我)?(?:昨天|之前|上次|上周).*(?:申请|办理)", c)]
    if explicit:
        return explicit[0]
    meaningful = [c for c in usable if not re.fullmatch(r"好的|谢谢|您好?|明白了|收到|辛苦了", c)]
    return "，".join(meaningful or usable)


def _match(pattern: str, text: str) -> bool:
    return bool(re.search(pattern, text, re.I))


def _single(text: str, active: Intent | None = None) -> Intent | None:
    t = text.strip().lower()
    if not t:
        return None
    if _match(r"(?:取消|撤销).*(?:退款|退货|换货|价保)(?:单|申请)|订机票|订酒店|申请维修|开通会员|续费会员|补发.*券", t):
        return Intent.OTHERS
    if _match(r"(?:转|找|联系|接通).*人工|人工(?:客服|服务|回电)|转接.*(?:进度|状态)", t):
        return Intent.HUMAN_HANDOFF
    if _match(r"投诉|举报|正式维权|升级(?:处理|争议)", t) and not _match(r"不(?:想|要|用)?投诉|别投诉", t):
        return Intent.COMPLAINT
    if _match(r"券|优惠", t) and _match(r"不可用|不能用|用不了|领不到|灰的|灰色|报错|失败|未返|没退|没返|没减|少减|未扣减|(?:结算|提示).*不能.*叠加", t):
        return Intent.COUPON_ISSUE
    if active == Intent.COUPON_ISSUE and _match(r"为什么|不能一起用|还是不行", t):
        return Intent.COUPON_ISSUE
    if _match(r"支付失败|付不了|支付不了|无法支付|重复扣|扣了两次|扣款.*未支付|扣(?:了)?钱.*未支付|钱扣了.*未支付|付款.*报错", t):
        return Intent.PAYMENT_ISSUE
    if active == Intent.PAYMENT_ISSUE and _match(r"没到账|钱.*哪里|仍然|还是", t):
        return Intent.PAYMENT_ISSUE
    if _match(r"凑单|凑满|怎么凑|怎样凑|拆单|合单|分两单|分开下单|差.*满减.*买", t):
        return Intent.CART_OPTIMIZATION
    if _match(r"历史.*价|价格.*走势|近.*(?:天|月).*(?:价|低)|过去.*价|降价记录|以前.*多少钱|最近.*降过价", t):
        return Intent.PRICE_HISTORY
    if _match(r"同款.*(?:比|哪|便宜|差价)|(?:两家|两店|跨店|跨平台|别的平台|另一个平台).*(?:价|便宜|优惠)|比价|现在买.*等.*活动", t):
        return Intent.PRICE_COMPARISON
    if _match(r"用券后|优惠后|到手价|实付多少|一共多少|总共多少|合计多少|算.*优惠|开会员.*(?:值|划算)", t):
        return Intent.PROMOTION_CALCULATION
    if _match(r"发票|开票|抬头|电子票", t):
        if _match(r"帮.*开|请.*开|我要.*开|给我.*开|申请.*票|重开|重新开|修改.*抬头|改.*发票|开(?:个|一张|公司|电子|专用|普通|增值税).*票|^(?:开发票|开票)$", t):
            return Intent.INVOICE_REQUEST
        return Intent.INVOICE_INQUIRY
    if _match(r"(?:催|尽快|快点|优先|加急).*(?:发|送|配送|快递)|催一下|催一催|加急", t):
        return Intent.EXPEDITE
    if _match(r"(?:改|修改|更改|换).*(?:收货|地址|收件人|电话)|(?:收货地址|地址|收件人|电话).*(?:改|修改|填错|写错)", t):
        return Intent.ADDRESS_CHANGE
    if _match(r"(?:取消|撤销).*(?:订单|这单|这笔)|(?:订单|这单|ord-).*(?:取消|撤销)", t):
        return Intent.ORDER_CANCEL
    after = _match(r"退款|退货|换货|价保|退运费", t)
    purchased = has_order_id(t) or _match(r"这单|订单|昨天买|买的|收到|已购|已经买|上周买|前天买", t) or active in _AFTER_SALE | {Intent.ORDER_INQUIRY, Intent.AFTER_SALE_STATUS}
    general_policy = _match(r"政策|规则|条件|一般|七天无理由|保修|质保|运费谁|谁承担|拆封.*退|支持退货|支持换货", t)
    apply = _match(r"帮我|我要|我想|申请|办理|给我|退掉|退一下|换成|换一下", t)
    if (after or _match(r"能退|还能退|可以退|能换|能补|保修|质保", t)) and general_policy and not apply and not purchased:
        return Intent.SERVICE_POLICY_INQUIRY
    if after and _match(r"进度|状态|到哪|到账|还没到|什么时候到|寄出|审核|处理中|申请.*结果", t):
        return Intent.AFTER_SALE_STATUS
    if _match(r"没到账|钱.*(?:没到|还没收到)|退款单", t) and (after or active in _AFTER_SALE | {Intent.AFTER_SALE_STATUS}):
        return Intent.AFTER_SALE_STATUS
    if _match(r"价保|退差价|补.*差价|补差|降价", t) and (purchased or apply):
        return Intent.PRICE_PROTECTION_SERVICE
    if _match(r"换货|换新|换成.*(?:码|色)|改换.*码|能换|可以换", t) and not _match(r"推荐|还没买", t):
        return Intent.EXCHANGE_SERVICE
    if _match(r"退款|退货|退运费|退掉|退一下|不想要了|能退|还能退|可以退", t):
        return Intent.REFUND_SERVICE
    if _match(r"推荐|帮我挑|帮我选|怎么选|适合我|适合.*(?:通勤|跑步)|哪款.*(?:好|适合)|哪个.*适合", t):
        return Intent.PRODUCT_RECOMMENDATION
    if _match(r"(?:两款|两台|不同型号|不同规格|a.*b).*(?:区别|对比|比较|差|哪个贵)|对比.*(?:参数|性能)|哪款续航", t):
        return Intent.PRODUCT_COMPARISON
    if _match(r"坏了|破损|货损|货不对|质量问题|开不了机|故障|裂痕|裂了|碎了|脏了|连不上|没声音|没有声音|少件|少发|漏发|错发|尺码不合适|屏幕.*闪|签收.*(?:没|未).*收到|签收.*没拿到", t):
        return Intent.ISSUE_REPORT
    if _match(r"有货|库存|售罄|补货|还有.*码|有.*(?:颜色|灰色|黑色|白色).*吗", t):
        return Intent.INVENTORY_INQUIRY
    if _match(r"会员|积分|等级|权益", t) and not _match(r"券|活动|叠加", t):
        return Intent.MEMBERSHIP_INQUIRY
    if _match(r"优惠|活动|券|满减|赠品|叠加|折扣", t):
        return Intent.PROMOTION_INQUIRY
    logistics_context = active == Intent.LOGISTICS_INQUIRY or _match(r"物流|包裹|运单|快递|这单|我的订单|已发货", t)
    if _match(r"物流|快递|运单|包裹|轨迹|到哪|配送|送到|送达|寄到|到货|多久到|几天到|什么时候到|明天.*到|后天.*到", t):
        if _match(r"还没买|下单前|今天买|想买|买这", t) or _match(r"包邮|配送政策|配送范围", t):
            return Intent.DELIVERY_INQUIRY
        if logistics_context or has_order_id(t) or _match(r"查.*物流|物流到", t):
            return Intent.LOGISTICS_INQUIRY
        return Intent.DELIVERY_INQUIRY
    if _match(r"包邮|运费|偏远地区", t):
        return Intent.DELIVERY_INQUIRY
    if _match(r"订单|发货|支付成功|付了多少|买过哪些|买了什么|查.*(?:当前)?状态|这单.*状态", t) or has_order_id(t):
        return Intent.ORDER_INQUIRY
    if _match(r"商品|价格|多少钱|规格|颜色|尺码|内存|参数|耳机|手机|鞋|牛仔裤|型号|四件套|米床|适配|支持|续航|充电|兼容|怎么用", t):
        return Intent.PRODUCT_INQUIRY
    if _match(r"再见|拜拜|不聊了|先这样|没别的了|没有其他问题|结束对话|谢谢|感谢|辛苦了|^bye$|^goodbye$", t):
        return Intent.CLOSURE
    if _match(r"你好|您好|哈喽|早上好|晚上好|在吗|^嗨|\bhello\b|\bhi\b", t):
        return Intent.GREETINGS
    return None


def _continuation(text: str, question: str, active: Intent | None) -> bool:
    if active is None or active in _DIALOG:
        return False
    t = text.strip()
    pending = _match(r"请(?:问|提供|选择|告诉)|哪个|哪笔|什么原因|原因是|确认|对吗|是否|吗[？?]?$|[？?]", question)
    answer = bool(re.fullmatch(r"好的|好|可以|确认(?:执行|申请)?|同意|对|对的|是的|不|不要|不退了|算了|第?[一二两三四五1-5]个?", t.strip("。！!？?")))
    candidate = _single(active_user_text(t), active)
    if not answer and candidate is not None and candidate != active:
        reason_answer = candidate == Intent.ISSUE_REPORT and _match(r"原因", question)
        field_answer = (candidate in {Intent.PRODUCT_INQUIRY, Intent.INVOICE_INQUIRY}
                        and _match(r"颜色|规格|尺码|抬头", question)
                        and not _match(r"吗|[？?]|如何|怎么|能否", t))
        order_answer = (candidate == Intent.ORDER_INQUIRY and has_order_id(t)
                        and _match(r"订单号|哪笔|哪个订单", question)
                        and not _match(r"查|取消|退|换|申请|物流", t))
        if not reason_answer and not field_answer and not order_answer:
            return False
    slot = (
        (_match(r"订单号|哪笔|哪个订单", question) and has_order_id(t) and not _match(r"查|取消|退|换|申请|物流", t))
        or (_match(r"原因", question) and not _match(r"帮我|请|我要|我想|申请|改为|换货|查", t))
        or (_match(r"颜色|规格|尺码|地区|城市|地址|抬头|税号|电话|预算|用途", question)
            and not _match(r"帮我|请|我要|我想|查|申请|取消|不.*了.*换", t) and len(t) <= 60)
    )
    return pending and (answer or slot)


def recognize(user_input: str, messages: list[dict] | None = None) -> Intent:
    context = conversation_messages(user_input, messages)
    active: Intent | None = None
    assistant = ""
    for message in context[:-1]:
        if message["role"] == "assistant":
            assistant = message["content"]
            continue
        raw = message["content"]
        if _continuation(raw, assistant, active):
            continue
        found = _single(active_user_text(raw), active)
        if found in _DIALOG:
            active = None
        elif found is not None:
            active = found
        assistant = ""
    raw = (user_input or "").strip()
    if not raw:
        return Intent.OTHERS
    if _continuation(raw, assistant, active):
        return active  # type: ignore[return-value]
    return _single(active_user_text(raw), active) or Intent.OTHERS
