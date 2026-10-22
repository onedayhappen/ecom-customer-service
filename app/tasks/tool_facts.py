"""Bounded customer-facing projections of the core tool contract."""
from app.tasks.reply_facts import label

LABELS = {
    'username':'用户名','profile':'个人资料','display_name':'昵称','contacts':'联系方式',
    'contact_type':'联系方式类型','value':'内容','verified_at':'验证时间','is_default':'默认地址',
    'recipient':'收件人','phone':'电话','detail':'详细地址','province_code':'省编码','city_code':'市编码','district_code':'区编码',
    'address_id':'地址编号','platform_code':'平台','merchant_id':'商家编号','name':'名称',
    'category':'品类','description':'说明','specs':'商品属性','product':'商品','product_id':'商品编号',
    'sku_id':'规格编号','sku_code':'规格编码','product_name':'商品名称','spec_values':'规格属性',
    'list_price':'标价','currency':'币种','inventory':'库存','available_qty':'可用数量',
    'sku_summary':'可选规格','sku_count':'规格总数','recorded_minimum':'已记录最低价',
    'price_type':'价格类型','effective_from':'生效时间','effective_until':'失效时间','source':'来源',
    'window_start':'查询开始','window_end':'查询结束','total':'记录总数','items':'明细',
    'order_id':'订单号','order_item_id':'订单行编号','status':'状态','source_status':'来源状态',
    'goods_amount':'商品金额','discount_amount':'优惠金额','shipping_amount':'运费','payable_amount':'应付金额',
    'paid_amount':'已付金额','amount':'金额','quantity':'数量','unit_price':'购买单价',
    'title_snapshot':'购买名称','spec_snapshot':'购买规格','address':'收货区域',
    'created_at':'创建时间','updated_at':'更新时间','completed_at':'完成时间','closed_at':'关闭时间',
    'payment_id':'支付记录','channel':'渠道','paid_at':'支付时间','recorded_success_amounts':'已记录成功支付金额',
    'order_paid_amount':'订单已付金额','order_currency':'订单币种','payment_transactions':'支付记录',
    'shipment_id':'包裹编号','shipments':'包裹','shipment':'包裹','tracking_number':'运单号',
    'carrier_code':'承运商','shipped_at':'发出时间','delivered_at':'签收时间','event_time':'轨迹时间',
    'event_type':'轨迹类型','content':'内容','location':'位置','item':'包裹明细','quantities':'数量核对',
    'purchased':'购买数量','recorded_shipped':'已记录发出数量','without_dispatch_record':'尚无发出记录数量','remaining_fulfillment':'待履约数量',
    'eligibility':'资格判断','reasons':'原因','reason':'原因','action':'操作','policy_id':'规则编号','policy_version':'规则版本',
    'promotion_id':'活动编号','version':'版本','kind':'类型','threshold_amount':'门槛金额','discount_bps':'减免基点',
    'cap_amount':'优惠上限','starts_at':'开始时间','ends_at':'结束时间','scopes':'适用范围',
    'scope_type':'范围类型','scope_id':'范围对象','scope_complete':'范围是否完整','scope_count':'范围数量',
    'coupon_id':'券模板编号','user_coupon_id':'持券编号','expires_at':'到期时间','claimed_at':'领取时间','used_at':'使用时间',
    'is_expired':'已到期','remaining_total':'剩余发放量','remaining_user':'本人剩余可领数量',
    'template':'券模板','instance':'持券','rules_basis':'规则依据','record_type':'记录类型',
    'claim_starts_at':'领取开始','claim_ends_at':'领取结束','valid_days':'有效天数','fixed_expires_at':'固定到期时间',
    'per_user_limit':'每人限领','total_limit':'总发行上限','released_at':'释放时间',
    'eligible_amount':'适用商品金额','threshold_gap':'距离门槛','goods_after_discount':'优惠后商品金额',
    'promotions':'采用活动','coupons':'采用优惠券','after_sale_id':'售后编号','after_sale':'售后申请',
    'requested_amount':'申请金额','requested_at':'申请时间','remaining_quantity':'当前可售后数量',
    'refund_id':'退款编号','refunds':'退款记录','recorded_success_amount':'渠道已报退款成功金额',
    'invoice_id':'开票申请编号','invoice_type':'抬头类型','title':'抬头','issued_at':'记录开具时间',
    'related':'关联记录','count':'记录数量','references':'关联编号','payment_transactions':'支付记录',
    'after_sale_requests':'售后申请','invoice_requests':'开票申请',
    'handoffs':'人工交接','ticket_id':'工单编号','assignee':'客服','resolved_at':'结束时间',
}
STATES={'pending_payment':'待支付','paid':'已支付','fulfilling':'履约中','shipped':'已发货',
    'in_transit':'运输中','delivered':'已签收','completed':'已完成','cancelled':'已取消','closed':'已关闭',
    'pending_review':'待审核','approved':'已批准','rejected':'已拒绝','processing':'处理中','pending':'待处理',
    'succeeded':'记录成功','failed':'记录失败','available':'可用','reserved':'已占用','used':'已使用',
    'expired':'已过期','invalid':'已失效','unknown':'未知','eligible':'符合已核实规则',
    'ineligible':'不符合规则','active':'有效','inactive':'停用','queued':'排队中','in_progress':'处理中',
    'waiting_customer':'等待用户','personal':'个人','company':'企业','refund_only':'仅退款','return_refund':'退货退款'}

def render(name,result):
    if not result.get('success'): return []
    data=result.get('data',{})
    if name=='read_task_context': data={'handoffs':data.get('handoffs',[])}
    if not isinstance(data,dict): return []
    lines=[]; truncated=False
    def walk(value,path='',depth=0,arbitrary=False,field=None):
        nonlocal truncated
        if len(lines)>=45:
            truncated=True; return
        if depth>7: return
        if isinstance(value,dict):
            for key,item in value.items():
                if key not in LABELS and not arbitrary: continue
                title=LABELS.get(key,label(key,50))
                walk(item,(path+' / ' if path else '')+title,depth+1,key in {'specs','spec_values','spec_snapshot','recorded_minimum','recorded_success_amounts'},key)
        elif isinstance(value,list):
            if not value: lines.append(path+'：暂无记录')
            for i,item in enumerate(value): walk(item,path+f' {i+1}',depth+1,arbitrary)
        else:
            text='未知／未记录' if value is None else '是' if value is True else '否' if value is False else label(value,220)
            if field in {'status','source_status','eligibility','invoice_type','kind'}: text=STATES.get(str(value),text)
            lines.append(path+'：'+text)
    walk(data)
    if name=='claim_coupon': lines.insert(0,'已领取指定优惠券。')
    lines.extend('说明：'+label(v,300) for v in result.get('limitations',[])[:5])
    if result.get('next_cursor') or truncated: lines.append('以上为部分结果，可以继续查询明细或下一页。')
    return lines
