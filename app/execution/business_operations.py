# 文件说明：把持久确认记录转换为规范业务参数，衔接业务预检和提交。
"""Adapter from the durable confirmation ledger to normalized MySQL operations."""
from app.services.commerce import service
from app.services.commerce.common import BusinessError,decoded,failure
from app.services.commerce.writes import PREPARES
from app.execution.task_proposals import PreparationRejected

class BusinessOperations:
    available=True
    def __init__(self,scope=None): self.scope=scope
    def preview(self,record,action,arguments=None):
        if action not in PREPARES or arguments is None:
            raise PreparationRejected('此操作契约已移除，请重新准备',manual=True)
        try:
            prepared=service(record.user_id,self.scope).preview(action,arguments)
            for key in ('order_id','platform_code'):
                if key=='order_id' and action=='claim_coupon': continue
                if record.details.get(key) and record.details[key] != prepared['arguments'][key]:
                    raise BusinessError('not_eligible','办理对象与当前事项不一致，请先澄清')
            return prepared
        except BusinessError as e: raise PreparationRejected(e.message,manual=e.code=='insufficient_data') from None
    def review(self,user,prepared):
        return service(user,self.scope).confirmation_review(prepared)
    def write(self,user,action,arguments,operation_id,business_version):
        try: return service(user,self.scope).submit(action,arguments,operation_id,business_version)
        except BusinessError as e: return {**failure(e),'request_id':operation_id}
    def query(self,user,operation_id):
        try:
            svc=service(user,self.scope)
            with svc.store.transaction() as tx: row=svc.receipt(tx,operation_id)
            if row: return {'status':'succeeded','action':row['action'],'result':decoded(row['result'])}
        except Exception: pass
        return {'status':'unknown','request_id':operation_id}
