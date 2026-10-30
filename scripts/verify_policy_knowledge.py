# 文件说明：核验知识正文、审核摘要与源码依据，并验证已发布政策检索。
"""Check the reviewed repository corpus and the configured real policy retriever."""
import hashlib
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

QUERIES = {
    'register': '怎么注册账号，注册后会自动送新人券吗',
    'password_reset': '登录密码输错五次会锁定多久，能短信找回密码吗',
    'account_security': '发现账号异常怎么办，能查看登录设备和冻结账户吗',
    'points_rules': '积分可以抵现金或兑换优惠券吗',
    'payment_methods': '可以用花呗分期或平台钱包充值付款吗',
    'payment_failed': '支付页面超时但银行已扣款应该怎么办',
    'order_status': '待收货一定已经发货了吗，已关闭和已取消有什么区别',
    'cancel_order': '已发货未签收取消订单是否扣除原订单运费',
    'modify_order': '未付款订单可以修改发票抬头和备注吗',
    'return_process': '退货退款要先拿到退货授权再寄回吗',
    'refund_timeline': '退款渠道显示成功代表银行卡已经到账了吗',
    'seven_day_return': '所有商品都能签收七天内无理由退货吗',
    'warranty': '保修维修是否免费，有统一的保修期限吗',
    'quality_issue': '商品质量问题可以直接退一赔三吗',
    'invoice': '申请开票后怎么取得发票文件，演示PDF能报销吗',
    'standard_shipping': '同城配送保证两天送达吗，全国都能配送吗',
    'shipping_tracking': '催发货受理后就代表已经发货了吗，如何查物流',
    'limited_offers': '促销满减能默认和优惠券叠加吗',
    'new_arrivals': '新品每周几上架，能订阅到货通知吗',
    'complaint': '人工投诉多久回复，会自动赠送补偿券吗',
    'membership': '成长会员和VIP月卡年卡、店铺会员是什么关系',
    'coupon_rules': '已付款订单退款后优惠券会退回吗',
    'clothing_size': '服装胸围腰围如何测量，所有品牌M码都一样吗',
    'shoe_size': '鞋码怎么按脚长和脚宽选择，中欧英美码能统一换算吗',
    'fabric_care': '衣服洗护要看洗标吗，盐水浸泡一定能固色吗',
    'price_protection': '商品降价可以申请价保补差吗，怎么确定可比价格',
    'exchange': '换货能换其他商品吗，换尺码需要检查库存和价差吗',
    'delivery_quote': '配送报价需要商品数量和已有地址吗，报价包含运费吗',
    'after_sale_status': '售后已受理还能撤回申请吗，停止聊天会撤销售后吗',
    'operation_confirmation': '聊天中回复澄清问题等于确认下单吗，确认后价格变化怎么办',
    'demo_service': '本地模拟渠道支持哪些业务，模拟退款是真实到账吗',
    'image_evidence': '售后凭证支持上传视频或动画图片吗，图片会一直保留吗',
    'customer_guide': '客服工作台有哪些页面，怎样查询订单和转人工',
}


def review_digest(path):
    # Git may check out CRLF on Windows and LF in Linux containers. Review
    # content, not transport line endings; all other text changes still differ.
    return hashlib.sha256(path.read_text(encoding='utf-8').encode('utf-8')).hexdigest()


def verify_review(root=ROOT):
    review = json.loads((root / 'docs/knowledge_review.json').read_text(encoding='utf-8'))
    if review.get('digest_format') != 'utf8-lf-sha256':
        raise ValueError('Unknown review digest format')
    knowledge = root / 'app/rag/knowledge'
    files = {p.name for p in knowledge.glob('*.md')}
    if set(review['documents']) != files:
        raise ValueError('Review inventory differs from the knowledge directory')
    for name, record in review['documents'].items():
        if review_digest(knowledge / name) != record['sha256']:
            raise ValueError('Review is stale for ' + name)
        metadata = json.loads((knowledge / name).with_suffix('.policy.json').read_text(encoding='utf-8'))
        if review_digest((knowledge / name).with_suffix('.policy.json')) != record['metadata_sha256']:
            raise ValueError('Reviewed metadata changed for ' + name)
        if (metadata['source_id'], metadata['version']) != (record['source_id'], record['version']):
            raise ValueError('Reviewed source/version differs for ' + name)
        for source, expected in record['review_basis'].items():
            if review_digest(root / source) != expected:
                raise ValueError('Review basis changed: ' + source + ' for ' + name)
    return review


def main():
    from datetime import datetime, timezone
    from app.config.settings import settings
    from app.rag.chunker import policy_chunks
    from app.rag.policy import PolicyContext, PolicyError
    from app.rag.service import _get_policy_retriever

    review = verify_review()
    chunks = policy_chunks(ROOT / settings.kb_dir)
    retriever = _get_policy_retriever()
    release = retriever.index.verify_corpus(chunks, retriever.embedder.index_identity)
    context = PolicyContext(request_at=datetime.now(timezone.utc))
    failures = []
    for doc, query in QUERIES.items():
        result = retriever.search(query, context, top_k=3)
        found = [hit['doc'] for hit in result['results']]
        if doc not in found or not retriever.current(result, context):
            failures.append(dict(expected=doc, found=found))
    try:
        retriever.search('星际航行燃料与黑洞蒸发', context)
        failures.append(dict(expected='no unrelated evidence'))
    except PolicyError as error:
        if error.code != 'policy_evidence_missing':
            raise
    print(json.dumps(dict(release=release, review_version=review['version'],
                          queries=len(QUERIES), failures=failures), ensure_ascii=False, indent=2))
    return 1 if failures else 0


if __name__ == '__main__':
    raise SystemExit(main())
