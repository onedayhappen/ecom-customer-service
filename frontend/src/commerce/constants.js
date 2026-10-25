export const orderTabs = [
  ['all', '全部'],
  ['pending_payment', '待付款'],
  ['paid', '待发货'],
  ['pending_receipt', '待收货'],
  ['after_sale', '售后中'],
]
export const orderLabels = {
  ...Object.fromEntries(orderTabs),
  received: '已收货',
  cancelled: '已取消',
  closed: '已关闭',
}
export const couponLabels = {
  available: '可使用',
  reserved: '已占用',
  used: '已使用',
  expired: '已过期',
  invalid: '已失效',
}
export const couponTabs = [
  ['all', '全部'],
  ['shop', '店铺券'],
  ['platform', '官方券'],
  ['category', '品类券'],
  ['newcomer', '新人券'],
  ['general', '通用券'],
]
export const couponTypeLabels = Object.fromEntries(couponTabs)
