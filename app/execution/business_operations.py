"""Trusted preflight/write adapter; business rules are rechecked inside MySQL."""
from app.data.commerce_dao import CommerceRejection
from app.execution.task_proposals import fixed_arguments, minor_units, verify_whole_order
from app.tools.commerce_models import OrderPolicy
from app.tools.mutation_context import authorized_mutation, checked_preflight
from app.tools.request_context import get_current_user_id


def refund_eligibility(snapshot):
    from app.tools.commerce_service import current_policy
    order, policy, applications, _ = snapshot
    rule = OrderPolicy.model_validate(policy["payload"]).model_dump(mode="json")
    current_policy(rule)
    if order["status"] not in rule["allowed_statuses"] or order.get("refund_status"):
        raise CommerceRejection("当前订单不符合退款申请规则，或已经存在退款申请")
    if any(r["payload"].get("status") not in {"cancelled", "rejected", "failed"} for r in applications):
        raise CommerceRejection("已有售后申请，请先查询原申请")
    minor_units(order["total"])


class BusinessOperations:
    def preview(self, record, action):
        from app.tools.commerce_service import database, invoice_eligibility
        args = fixed_arguments(record, action)
        db, user = database(), get_current_user_id()
        if not user or user != record.user_id:
            raise CommerceRejection("缺少已验证的业务用户")
        if action == "apply_refund":
            snapshot = db.refund_snapshot(user, args["order_id"])
            refund_eligibility(snapshot)
            order, policy, _, version = snapshot
            amount_kind = "order_reference"
            title = "申请整单退款"
            impact = "本次只登记申请。退款金额、优惠券、赠品和运费的处理需业务审核，尚未确认资金到账。"
        else:
            snapshot = db.preview_invoice(user, args["order_id"])
            eligible, _, version = invoice_eligibility(snapshot)
            order, policy, invoices, _ = snapshot
            active = [r for r in invoices if r["payload"]["status"] == "issued_local"]
            if not eligible:
                raise CommerceRejection("当前订单不符合开票条件")
            if action == "issue_invoice" and active:
                raise CommerceRejection("已有有效的本地演示发票，请先查询原票")
            if action == "reissue_invoice" and not any(r["record_id"] == args["invoice_id"] for r in active):
                raise CommerceRejection("原发票无效或不属于当前订单")
            args["preview_version"] = version
            amount_kind = "invoice_amount"
            title = "创建本地演示发票" if action == "issue_invoice" else "重开本地演示发票"
            impact = "仅生成本地演示记录，不开具真实税务发票。" + (
                "确认后原本地演示发票将标为已替换。" if action == "reissue_invoice" else "")
        items = verify_whole_order(record, order)
        return dict(action=action, arguments=args, business_version=version, review=dict(
            title=title, order_id=args["order_id"], items=items,
            quantity=sum(i["quantity"] for i in items), reason=args.get("reason"),
            amount_minor=minor_units(order["total"]), currency="CNY", amount_kind=amount_kind,
            fields=args.get("fields"), original_invoice_id=args.get("invoice_id"), impact=impact,
            source="mysql", is_demo=action != "apply_refund" or bool(policy.get("is_demo"))))

    def write(self, user, action, arguments, operation_id, business_version):
        from app.tools.commerce import invoke
        from app.data.order_service import request_refund
        with authorized_mutation(user, action, arguments, operation_id), checked_preflight(
                user, action, arguments, business_version):
            if action == "apply_refund":
                return request_refund(**arguments)
            return invoke(action, arguments)

    def query(self, user, operation_id):
        from app.tools.commerce_service import database
        if get_current_user_id() != user:
            raise CommerceRejection("缺少已验证的业务用户")
        return database().mutation_status(user, operation_id)
