"""恢复并校验意图微调训练集与评测集。"""
from __future__ import annotations

import copy
import json
import re
from collections import Counter, defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
TRAIN_PATH = ROOT / "data/fine_tuning/multiturn_intent_sft/data_v3.jsonl"
SHAREGPT_PATH = ROOT / "data/fine_tuning/multiturn_intent_sft/data_v3_sharegpt.jsonl"
EVAL_PATH = ROOT / "data/evaluation/multiturn_intent_eval_v3.jsonl"
REPORT_PATH = ROOT / "data/evaluation/intent_dataset_repair_report.json"
LABELS = [
    "product_inquiry", "product_recommendation", "order_inquiry", "logistics_inquiry",
    "address_change", "order_cancel", "expedite", "after_sale_request", "refund_status",
    "issue_report", "invoice", "complaint", "confirmation", "answer_question", "closure",
    "greetings", "others",
]
BUSINESS_LABELS = {
    "product_inquiry", "product_recommendation", "order_inquiry", "logistics_inquiry",
    "address_change", "order_cancel", "expedite", "after_sale_request", "refund_status",
    "issue_report", "invoice", "complaint",
}
HARD_TRAIN = [('product_inquiry', [('user', '我想了解这款耳机'), ('assistant', '请问想了解什么？'), ('user', '它支持两台设备同时连接吗？')]), ('product_inquiry', [('user', '先不查订单'), ('assistant', '好的'), ('user', '我只想问这款扫地机器人有没有自动集尘功能')]), ('product_recommendation', [('user', '预算一千元，想买耳机'), ('assistant', '主要用途是什么？'), ('user', '通勤降噪，帮我挑一款')]), ('order_inquiry', [('user', '刚才查的是物流'), ('assistant', '好的'), ('user', '先别查物流了，查 ORD-ADMIN-002 的金额和商品明细')]), ('order_inquiry', [('user', '回到刚才吸尘器那单'), ('assistant', '请提供订单号'), ('user', '查一下 ORD-ADMIN-001 当前状态')]), ('logistics_inquiry', [('user', '这单 ORD-ADMIN-002 的物流到哪了'), ('assistant', '我帮您查询'), ('user', '明天能到吗？')]), ('logistics_inquiry', [('user', '订单金额已经查过了'), ('assistant', '好的'), ('user', '如果轨迹查不到，就给我这单的快递公司和运单号')]), ('logistics_inquiry', [('user', '我只关心包裹运输'), ('assistant', '好的'), ('user', '不要再查商品明细，帮我看 ORD-ADMIN-002 到哪了')]), ('address_change', [('user', '订单还没发货'), ('assistant', '可以尝试修改'), ('user', '请把 ORD-ADMIN-001 的收货地址改到浦东新区')]), ('order_cancel', [('user', '这笔订单还没发货'), ('assistant', '请问需要什么帮助？'), ('user', '请取消 ORD-ADMIN-001，不要退款')]), ('expedite', [('user', '订单状态是待发货'), ('assistant', '我可以帮您记录'), ('user', '请催 ORD-ADMIN-001 尽快发出')]), ('after_sale_request', [('user', '这件商品已经收到'), ('assistant', '请问需要什么帮助？'), ('user', '我想为 ORD-ADMIN-002 申请退货')]), ('refund_status', [('user', '之前已经提交退款'), ('assistant', '好的'), ('user', '现在只想查 ORD-ADMIN-002 的退款进度')]), ('issue_report', [('user', '订单已经收到了'), ('assistant', '使用中有什么问题吗？'), ('user', '耳机左耳没有声音，属于质量问题')]), ('invoice', [('user', '订单信息已核实'), ('assistant', '还需要什么帮助？'), ('user', '请给 ORD-ADMIN-001 开电子发票')]), ('complaint', [('user', '退款已经等了七天'), ('assistant', '我帮您记录'), ('user', '一直没人处理，我要投诉')]), ('confirmation', [('user', '可以为您申请退款，是否继续？'), ('assistant', ''), ('user', '确认申请')]), ('answer_question', [('user', '请提供订单号'), ('assistant', ''), ('user', 'ORD-ADMIN-002')]), ('answer_question', [('user', '请问改到哪个地址？'), ('assistant', ''), ('user', '改到杭州市西湖区')]), ('closure', [('user', '问题已经解决了'), ('assistant', '好的'), ('user', '谢谢，今天先这样')]), ('greetings', [('user', '你好，客服在吗？')]), ('others', [('user', '你是真人还是机器人？')])]
HARD_EVAL = [('product_inquiry', [('user', '请问这款手机支持无线充电吗？')]), ('product_inquiry', [('user', '先别查物流，我想问耳机的续航时间')]), ('product_recommendation', [('user', '给老人买扫地机器人，有什么推荐？')]), ('product_recommendation', [('user', '预算 800，帮我选个电饭煲')]), ('product_recommendation', [('user', '想买适合学生宿舍用的空气净化器，推荐一下')]), ('product_recommendation', [('user', '我主要跑步，哪款智能手表更合适？')]), ('product_recommendation', [('user', '帮我挑一个容量大、噪音小的冰箱')]), ('product_recommendation', [('user', '送父母用，推荐操作简单的洗衣机')]), ('order_inquiry', [('user', '不要查物流了，请查 ORD-20240208-002 的订单金额')]), ('order_inquiry', [('user', '回到刚才那笔订单，列出商品明细')]), ('logistics_inquiry', [('user', 'ORD-20240215-009 的包裹到哪了，后天能到吗？')]), ('logistics_inquiry', [('user', '如果没有轨迹，请告诉我 ORD-20240220-008 的承运商和运单号')]), ('address_change', [('user', '请把 ORD-20240212-007 的收货电话改成 13900001111')]), ('address_change', [('user', '地址填错了，改到杭州市拱墅区')]), ('order_cancel', [('user', '刚下的 ORD-20240201-003 不要了，帮我取消')]), ('order_cancel', [('user', '请撤销这笔订单 ORD-20240218-004')]), ('expedite', [('user', 'ORD-20240222-006 什么时候发？请帮我催一下')]), ('expedite', [('user', '这笔订单很急，能优先安排发货吗？')]), ('after_sale_request', [('user', '我想给 ORD-20240208-002 申请换货')]), ('after_sale_request', [('user', '收到的商品不合适，帮我申请退货')]), ('refund_status', [('user', 'ORD-20240215-009 的退款什么时候到账？')]), ('refund_status', [('user', '退款显示成功了，但银行卡还没收到')]), ('issue_report', [('user', '收到的杯子碎了，怎么处理？')]), ('issue_report', [('user', '耳机充电盒一直闪红灯，无法充电')]), ('invoice', [('user', '请给 ORD-20240201-003 开公司发票')]), ('invoice', [('user', '电子发票可以发到邮箱吗？')]), ('complaint', [('user', '客服反复推诿，我要投诉')]), ('complaint', [('user', '等了这么久还没人处理，太不负责了')]), ('confirmation', [('user', '可以为您保留这个处理方案，确认吗？'), ('assistant', ''), ('user', '好的，确认')]), ('confirmation', [('user', '要继续提交申请吗？'), ('assistant', ''), ('user', '同意')]), ('answer_question', [('user', '请提供联系电话'), ('assistant', ''), ('user', '13700002222')]), ('answer_question', [('user', '请选择黑色还是白色'), ('assistant', ''), ('user', '黑色')]), ('product_recommendation', [('user', '想买一台适合小户型的洗碗机，请帮我推荐')]), ('logistics_inquiry', [('user', '请查询 ORD-EVAL-LG-001 的运输节点和预计送达时间')]), ('address_change', [('user', '请把 ORD-EVAL-AD-001 的收货地址改成静安区南京西路')]), ('order_cancel', [('user', 'ORD-EVAL-OC-001 还没有发货，我决定不要了，请取消订单')]), ('order_cancel', [('user', '请帮我撤销 ORD-EVAL-OC-002 这笔未支付订单')]), ('expedite', [('user', '请优先处理 ORD-EVAL-EX-001，我后天就要使用')]), ('refund_status', [('user', '请跟进 ORD-EVAL-RF-001 的退款审核结果')]), ('invoice', [('user', '请把 ORD-EVAL-IV-001 的数电发票重新开具一份')]), ('invoice', [('user', 'ORD-EVAL-IV-002 可以开专用发票吗？')]), ('complaint', [('user', '关于售后一直无人回复，我要正式投诉并升级处理')]), ('closure', [('user', '感谢你的说明，我这边没有其他问题了')]), ('closure', [('user', '还有别的问题吗？'), ('assistant', ''), ('user', '没有了，谢谢')]), ('closure', [('user', '订单已经处理完成'), ('assistant', ''), ('user', '辛苦了，再见')]), ('greetings', [('user', '早上好，麻烦接待一下')]), ('greetings', [('user', '嗨，在吗？')]), ('others', [('user', '你能讲个故事吗？')]), ('others', [('user', '一加一等于几？')])]
CORRECTIONS = {
    "intent_eval_015": "order_cancel", "intent_eval_016": "others", "intent_eval_017": None,
    "intent_eval_020": "answer_question", "intent_eval_043": "product_inquiry",
    "intent_eval_044": "answer_question", "intent_eval_046": "answer_question",
    "intent_eval_060": "refund_status", "intent_eval_066": "product_inquiry",
    "intent_eval_073": "issue_report", "intent_eval_083": "confirmation",
    "intent_eval_084": "confirmation",
}
SLOT_RE = re.compile(r"(?:ORD-[A-Z0-9-]+|\d{11}|黑色|白色|杭州|上海|北京|换货|退货|退款|个人)")
SLOT_TASK_PREFIX = {
    "order_inquiry": "请查询订单 {value} 的当前状态",
    "logistics_inquiry": "请查询订单 {value} 的最新物流",
    "order_cancel": "请取消订单 {value}",
    "expedite": "请催促订单 {value} 尽快发货",
    "after_sale_request": "请为订单 {value} 提交售后申请",
    "invoice": "请为订单 {value} 开具发票",
    "refund_status": "请查询订单 {value} 的退款进度",
}
VARIANTS = {
    "order_inquiry": ["请查询 ORD-RESTORE-OI-%03d 的订单详情" % i for i in range(1, 31)],
    "address_change": ["请把 ORD-RESTORE-AD-%03d 的收货地址改到新的办公地点" % i for i in range(1, 21)],
    "after_sale_request": ["请为 ORD-RESTORE-AS-%03d 提交退货申请" % i for i in range(1, 21)],
    "refund_status": ["请查询 ORD-RESTORE-RF-%03d 的退款处理进度" % i for i in range(1, 21)],
    "complaint": ["关于第 %d 次售后延误，我要投诉并要求升级处理" % i for i in range(1, 11)],
    "closure": ["谢谢说明，我暂时没有其他问题了（第%d次咨询）" % i for i in range(1, 11)],
    "answer_question": ["ORD-RESTORE-AQ-%03d" % i for i in range(1, 21)],
    "greetings": ["你好，客服请接待我（第%d次）" % i for i in range(1, 16)],
}
EVAL_FALLBACKS = {
    "product_inquiry": ["请问产品 RESTORE-%03d 支持无线连接吗？" % i for i in range(1, 10)],
    "product_recommendation": ["预算 %d 元，帮我推荐适合家庭使用的电器" % (600+i*50) for i in range(1, 10)],
    "order_inquiry": ["请查询 ORD-RESTORE-E-OI-%03d 的订单明细" % i for i in range(1, 10)],
    "logistics_inquiry": ["请查询 ORD-RESTORE-E-LG-%03d 的物流和预计送达时间" % i for i in range(1, 10)],
    "address_change": ["请把 ORD-RESTORE-E-AD-%03d 的收货地址改到新地址" % i for i in range(1, 10)],
    "order_cancel": ["请取消尚未发货的 ORD-RESTORE-E-OC-%03d" % i for i in range(1, 10)],
    "expedite": ["请加急处理 ORD-RESTORE-E-EX-%03d 的发货" % i for i in range(1, 10)],
    "after_sale_request": ["请为 ORD-RESTORE-E-AS-%03d 申请换货" % i for i in range(1, 10)],
    "refund_status": ["请查询 ORD-RESTORE-E-RF-%03d 的退款状态" % i for i in range(1, 10)],
    "issue_report": ["收到的商品 RESTORE-%03d 有质量问题，怎么处理？" % i for i in range(1, 10)],
    "invoice": ["请为 ORD-RESTORE-E-IV-%03d 开具电子发票" % i for i in range(1, 10)],
    "complaint": ["售后拖延了 %d 天，我要投诉并要求处理" % (i+2) for i in range(1, 10)],
    "confirmation": ["确认继续执行第 %d 项处理" % i for i in range(1, 10)],
    "answer_question": ["ORD-RESTORE-E-AQ-%03d" % i for i in range(1, 10)],
    "closure": ["谢谢说明，我没有其他问题了（评测%d）" % i for i in range(1, 10)],
    "greetings": ["你好，客服在吗（评测%d）" % i for i in range(1, 10)],
    "others": ["请介绍一下你自己（评测%d）" % i for i in range(1, 10)],
}

def load(path: Path) -> list[dict]:
    return [json.loads(x) for x in path.read_text(encoding="utf-8").splitlines() if x.strip()]

def input_messages(row: dict) -> list[dict]:
    messages = copy.deepcopy(row.get("messages", []))
    if messages and messages[-1].get("role") == "assistant":
        try: payload = json.loads(messages[-1].get("content", ""))
        except (TypeError, json.JSONDecodeError): payload = None
        if isinstance(payload, dict) and set(payload) == {"intent"} and payload["intent"] in LABELS:
            messages.pop()
    return messages

def key(row: dict) -> tuple[tuple[str, str], ...]:
    return tuple((m.get("role", ""), m.get("content", "")) for m in input_messages(row))

def make_row(label: str, turns: list[tuple[str, str]]) -> dict:
    return {"messages": [{"role": r, "content": c} for r, c in turns] + [{"role": "assistant", "content": json.dumps({"intent": label}, ensure_ascii=False)}]}

def label_of(row: dict) -> str | None:
    try: return json.loads(row["messages"][-1]["content"])["intent"]
    except (KeyError, TypeError, json.JSONDecodeError): return None

def rewrite_slot_answer(label: str, row: dict, serial: int) -> dict:
    row = copy.deepcopy(row)
    if label not in SLOT_TASK_PREFIX: return row
    users = [m for m in row["messages"][:-1] if m.get("role") == "user"]
    if not users: return row
    text = users[-1]["content"].strip()
    if SLOT_RE.fullmatch(text):
        users[-1]["content"] = SLOT_TASK_PREFIX[label].format(value=text) + f"（场景{serial}）"
    return row

def variants(label: str, for_eval: bool = False) -> list[dict]:
    values = EVAL_FALLBACKS.get(label, []) if for_eval else VARIANTS.get(label, [])
    return [make_row(label, [("user", text)]) for text in values]

def build_train() -> list[dict]:
    base = load(TRAIN_PATH)
    by = defaultdict(list)
    for row in base:
        label = label_of(row)
        if label in LABELS: by[label].append(row)
    hard = defaultdict(list)
    for label, turns in HARD_TRAIN: hard[label].append(make_row(label, turns))
    output, used = [], set()
    order = ["answer_question"] + [x for x in LABELS if x != "answer_question"]
    for label in order:
        candidates = hard[label] + [rewrite_slot_answer(label, r, i) for i, r in enumerate(by[label], 1)] + variants(label)
        for row in candidates:
            k = key(row)
            if k in used: continue
            output.append(row); used.add(k)
            if sum(label_of(x) == label for x in output) == 100: break
        count = sum(label_of(x) == label for x in output)
        if count != 100: raise RuntimeError(f"训练集 {label} 只有 {count} 条")
    return output

def build_eval(train: list[dict]) -> list[dict]:
    train_keys = {key(r) for r in train}
    existing = load(EVAL_PATH)
    by = defaultdict(list); seen = set()
    candidates = [(label, turns) for label, turns in HARD_EVAL]
    for row in existing:
        label = CORRECTIONS.get(row.get("id"), row.get("expected_intent"))
        if label is not None: candidates.append((label, [(m["role"], m["content"]) for m in input_messages(row)]))
    for label in LABELS:
        for expected, turns in candidates:
            if expected != label: continue
            row = {"messages": [{"role": r, "content": c} for r, c in turns], "expected_intent": label}
            k = key(row)
            if k in seen or k in train_keys: continue
            seen.add(k); by[label].append(row)
        for row in variants(label, for_eval=True):
            clean = {"messages": input_messages(row), "expected_intent": label}; k = key(clean)
            if k in seen or k in train_keys: continue
            seen.add(k); by[label].append(clean)
    output = []
    for label in LABELS:
        if len(by[label]) < 8: raise RuntimeError(f"评测集 {label} 只有 {len(by[label])} 条")
        output.extend(by[label][:8])
    for i, row in enumerate(output, 1): row["id"] = f"intent_eval_v4_{i:03d}"
    return output

def to_sharegpt(row: dict) -> dict:
    system = "你是电商客服的意图分类器。只判断最后一条 user 消息；槽位回答标为 answer_question，明确同意执行标为 confirmation。只输出 JSON。标签：" + ", ".join(LABELS)
    conversations = [{"from": "system", "value": system}]
    for m in row["messages"]:
        conversations.append({"from": "human" if m["role"] == "user" else "gpt", "value": m["content"]})
    return {"conversations": conversations}

def write(path: Path, rows: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(json.dumps(r, ensure_ascii=False) + "\n" for r in rows), encoding="utf-8")

def main() -> None:
    train = build_train(); evaluation = build_eval(train)
    write(TRAIN_PATH, train); write(SHAREGPT_PATH, [to_sharegpt(r) for r in train]); write(EVAL_PATH, evaluation)
    report = {
        "train_path": str(TRAIN_PATH), "eval_path": str(EVAL_PATH),
        "train_total": len(train), "eval_total": len(evaluation),
        "train_distribution": dict(Counter(label_of(r) for r in train)),
        "eval_distribution": dict(Counter(r["expected_intent"] for r in evaluation)),
        "train_duplicate_dialogues": len(train) - len({key(r) for r in train}),
        "eval_train_overlap": len({key(r) for r in evaluation} & {key(r) for r in train}),
        "hard_train_cases": len(HARD_TRAIN), "hard_eval_cases": len(HARD_EVAL),
    }
    REPORT_PATH.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False, indent=2))

if __name__ == "__main__": main()
