# 文件说明：绑定发票查询与办理准备工具，复用统一参数和业务分发。
"""Typed tool adapters; business logic lives in commerce services."""
from app.tools.dispatch import bind

get_invoices = bind('get_invoices')
prepare_invoice = bind('prepare_invoice')
prepare_invoice_reissue = bind('prepare_invoice_reissue')
get_invoice_document = bind('get_invoice_document')
