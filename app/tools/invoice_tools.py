"""invoice tool entry points. Business logic lives in app.services.commerce."""
from app.tools.dispatch import bind

list_invoice_requests = bind('list_invoice_requests')
get_invoice_request = bind('get_invoice_request')
prepare_invoice = bind('prepare_invoice')
