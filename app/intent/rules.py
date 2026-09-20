"""规则意图识别：17 类业务任务级标签（Intent 枚举）。

新链路的第一阶段意图识别器：把旧 Router 的关键词规则集改造成 17 类映射，
返回 :class:`app.data_plan.enums.Intent` 枚举值。分类器仅输出 intent；
实体/槽位由执行层抽取。后续可替换为微调分类器（app/intent/ 的 Phase 1 规划），
本模块保持「输出 Intent 枚举」的同一接口。
"""

from __future__ import annotations

import re

from app.data_plan.enums import Intent

_ORDER_ID_RE = re.compile(r"\bord-[a-z0-9-]+\b", re.IGNORECASE)

# 多轮输入常在同一句里包含“上一件事的否定 + 当前新任务”。先把已明确
# 否定的子句和背景确认语句剥离，再让同一套规则识别最后一个有效动作。
_TASK_VERBS = ("查", "查询", "看", "取消", "申请", "办理", "修改", "开", "催", "推荐", "咨询")
_INTENT_CUES = (
    "订单", "物流", "快递", "轨迹", "运单", "到哪", "到货", "发货", "签收",
    "退款", "退货", "换货", "取消", "金额", "明细", "状态", "商品", "库存",
    "价格", "耳机", "手机", "鞋", "发票", "投诉",
)
_NEGATED_TASK_RE = re.compile(
    r"(?:先\s*)?(?:别|不要|不用|不再|停止)\s*[^，。；！？!?]{0,12}(?:查|查询|看|取消|申请|办理|修改|提交|催)",
)


def active_user_text(user_input: str) -> str:
    """提取当前有效任务，避免否定旧任务污染本轮意图。

    只根据句法信号做通用裁剪：明确否定的子句被丢弃，最后一个含任务动词
    的子句优先。若无法确定，则返回原文，让上层继续走保守分类。
    """
    text = (user_input or "").strip()
    if not text:
        return text
    clauses = [part.strip() for part in re.split(r"[，。；！？!?]+", text) if part.strip()]
    usable = [part for part in clauses if not _NEGATED_TASK_RE.search(part)]
    if not usable:
        return text
    actionable = [part for part in usable if any(verb in part for verb in _TASK_VERBS)]
    intent_clauses = [part for part in usable if any(cue in part for cue in _INTENT_CUES)]
    if actionable:
        candidate = actionable[-1]
        # 条件句常带“查不到/查不出”，而真正的请求在后一个分句：
        # “如果查不到轨迹，就给我快递公司和运单号”。后面的业务信号
        # 应覆盖条件句，不能把条件句当成最终任务。
        if intent_clauses and usable.index(intent_clauses[-1]) > usable.index(candidate):
            return intent_clauses[-1]
        # 句末常是补充条件（“明天能到吗”“我怕弄混”），不能覆盖前一
        # 个真正承载业务意图的分句。只有候选本身带业务信号时才采用它。
        if any(cue in candidate for cue in _INTENT_CUES):
            return candidate
    return intent_clauses[-1] if intent_clauses else usable[-1]

# 优先级从高到低；命中即返回（与旧 Router 的确定性路由一致）
_RULES: list[tuple[Intent, tuple[str, ...]]] = [
    # ---- 售后：投诉优先 ----
    (Intent.COMPLAINT, (
        "投诉", "举报", "消协", "赔偿", "质量太差", "服务态度",
        "敷衍", "不理人", "慢得离谱", "不满意", "差评",
    )),
    # ---- 售后：退款进度（"到哪了/进度/到账/还没到"）----
    (Intent.REFUND_STATUS, (
        "退款到哪", "退款进度", "退到哪", "到账了", "到账没",
        "退款状态", "钱到", "退款单", "退款怎么还没到", "退款还没到",
        "还没到账", "怎么还没到账", "几天到账",
    )),
    # ---- 售后：申请退换/价保 ----
    (Intent.AFTER_SALE_REQUEST, (
        "退款", "退货", "退换", "换货", "七天无理由", "退掉",
        "不想要了", "退货运费", "价保", "差价", "退差价",
        "降价", "换个", "换一个", "换成", "换新",
    )),
    # ---- 售后：问题反馈 ----
    (Intent.ISSUE_REPORT, (
        "坏了", "破损", "货损", "货不对", "质量问题", "有问题",
        "收到的东西", "开不了机", "故障", "裂了", "碎了", "脏了",
    )),
    # ---- 售后：发票 ----
    (Intent.INVOICE, (
        "发票", "开票", "抬头", "电子票",
    )),
    # ---- 订单：加急/催发货（先于物流，催发词更具体）----
    (Intent.EXPEDITE, (
        "催发货", "催一催", "催一下", "加急", "快点发", "尽快发", "怎么还没发",
    )),
    # ---- 售前：配送/政策（先于物流，避免"包邮/几天送到"被物流词抢走）----
    (Intent.PRODUCT_INQUIRY, (
        "包邮", "运费", "偏远地区", "配送政策", "时效",
        "几天送到", "几天到", "多久送到",
    )),
    # ---- 订单：物流 ----
    (Intent.LOGISTICS_INQUIRY, (
        "物流", "快递", "发货", "到哪", "送到", "运单",
        "签收", "配送", "派送", "寄到",
    )),
    # ---- 订单：取消 ----
    (Intent.ORDER_CANCEL, (
        "取消订单", "取消", "退了订单",
    )),
    # ---- 订单：改址 ----
    (Intent.ADDRESS_CHANGE, (
        "改地址", "改收货", "换地址", "地址改", "收货地址",
        "地址错", "地址写错", "改个地址",
    )),
    # ---- 订单：查询 ----
    (Intent.ORDER_INQUIRY, (
        "订单", "买过哪些", "买了什么", "买了啥", "我的订单",
        "订单列表", "订单明细", "当前状态", "下单", "买了", "这单",
    )),
    # ---- 售前：推荐 ----
    (Intent.PRODUCT_RECOMMENDATION, (
        "推荐", "哪款好", "值得买", "推荐一下", "推荐个",
        "哪个好", "适合", "帮我挑", "怎么选",
    )),
    # ---- 售前：商品咨询 ----
    (Intent.PRODUCT_INQUIRY, (
        "商品", "价格", "多少钱", "库存", "有货", "售罄",
        "规格", "颜色", "尺码", "内存", "参数", "优惠",
        "会员", "包邮", "保修", "质保", "赠品", "券",
        "耳机", "手机", "鞋", "牛仔裤", "码吗", "码号", "型号",
        "四件套", "米床", "灰色", "适配",
    )),
    # ---- 对话流：收尾 ----
    (Intent.CLOSURE, (
        "再见", "拜拜", "回头见", "不聊了", "先这样", "没别的了",
        "没有其他问题", "结束对话", "bye", "goodbye", "谢谢",
        "感谢", "辛苦了",
    )),
    # ---- 对话流：问候 ----
    (Intent.GREETINGS, (
        "你好", "您好", "嗨", "hello", "hi", "在吗", "哈喽",
    )),
    # ---- 对话流：确认 ----
    (Intent.CONFIRMATION, (
        "好的", "好吧", "明白了", "知道了", "了解了", "收到",
        "可以的", "没问题", "行", "嗯嗯", "对的", "是的", "确认",
    )),
]


def recognize(user_input: str) -> Intent:
    """按规则识别 17 类意图；空输入与未命中返回 OTHERS。

    纯函数、无副作用；供 API 主链路与评测共用。
    """
    text = active_user_text(user_input).lower()
    if not text:
        return Intent.OTHERS

    for intent, terms in _RULES:
        if any(term in text for term in terms):
            return intent
    # 显式订单号且没有更具体的意图词时，按订单查询处理（如"帮我查 ORD-xxx"）。
    if has_order_id(user_input):
        return Intent.ORDER_INQUIRY
    return Intent.OTHERS


def has_order_id(user_input: str) -> bool:
    """用户话语中是否出现订单号（ORD-xxx）。"""
    return _ORDER_ID_RE.search(user_input or "") is not None
