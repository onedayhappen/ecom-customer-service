"""Conservative, deterministic control parsing; ambiguous targets are asked."""
import re
from app.data_plan.taxonomy import DEFINITIONS

LABELS = {
    'after_sale_request': '售后申请', 'refund_status': '退款进度',
    'logistics_inquiry': '物流查询', 'order_inquiry': '订单查询',
    'invoice': '发票申请', 'address_change': '修改地址', 'order_cancel': '取消订单',
    'expedite': '催发货', 'complaint': '投诉', 'product_inquiry': '商品咨询',
    'product_recommendation': '商品推荐', 'issue_report': '问题反馈',
    'others': '其他咨询',
}
LABELS.update({intent.value: title for intent, (title, _) in DEFINITIONS.items()})
ORDER = re.compile(r'\bORD-[A-Za-z0-9-]+\b', re.I)
