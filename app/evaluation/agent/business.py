# 文件说明：为 Agent 评测提供有状态业务替身，按配置查询和变更隔离对象。
"""Stateful external-service substitute; production confirmation remains in charge."""
from copy import deepcopy
from contextlib import contextmanager
from importlib import import_module
from threading import RLock
from unittest.mock import patch

from app.services.commerce.common import BusinessError, ok
from app.tools.catalog import BY_NAME
from app.tools.request_context import get_current_user_id
from .contracts import digest


def normalized(tool, arguments):
    return BY_NAME[tool].arguments.model_validate(arguments).model_dump(by_alias=True)


class Business:
    def __init__(self, inputs, journal):
        self.lock, self.journal = RLock(), journal
        self.objects, self.receipts = deepcopy(inputs.objects), {}
        self.reads, self.writes = inputs.reads, inputs.writes
        seen = set()
        for rule in [*self.reads, *self.writes]:
            if rule.tool not in BY_NAME or BY_NAME[rule.tool].domain == "flow":
                raise ValueError("Unknown external tool fixture")
            effect = BY_NAME[rule.tool].effect
            if (rule in self.reads and effect != "read") or (rule in self.writes and effect not in {"prepare", "dispatch"}):
                raise ValueError("Unsupported fixture effect")
            key = (rule.tool, rule.user, digest(normalized(rule.tool, rule.arguments)))
            if key in seen:
                raise ValueError("Ambiguous business fixture")
            seen.add(key)
            if rule.state_key:
                from app.services.commerce.common import unref
                state = self.objects[rule.state_key]
                if rule.arguments.get("order_ref"):
                    _, platform, order = unref(rule.arguments["order_ref"], {"order"})
                    if state.get("order_id") != order or state.get("platform_code") != platform:
                        raise ValueError("Fixture reference does not match the state object")
            if rule in self.writes:
                if rule.review.get("order_id") != state["order_id"] or rule.review.get("action") != rule.tool:
                    raise ValueError("Review must identify the exact order and action")

    def service(self, user=None, scope=None):
        return Service(self, user or get_current_user_id())

    @contextmanager
    def installed(self):
        from contextlib import ExitStack
        from app.execution.business_operations import BusinessOperations
        owner = self

        class Backend(BusinessOperations):
            def query(self, user, operation_id):
                with owner.lock:
                    row = owner.receipts.get(operation_id)
                    if row and row["user_id"] == user:
                        return dict(status="succeeded", action=row["action"], result=deepcopy(row["result"]))
                    return dict(status="unknown", request_id=operation_id)

        with ExitStack() as stack:
            for module in ("app.tools.dispatch", "app.execution.business_operations",
                           "app.services.commerce", "app.services.commerce.service"):
                stack.enter_context(patch.object(import_module(module), "service", self.service))
            stack.enter_context(patch("app.tasks.operations.backend", Backend))
            yield self


class Service:
    def __init__(self, business, user):
        self.business, self.user = business, user

    def _rule(self, rules, action, arguments):
        args = normalized(action, arguments)
        rows = [r for r in rules if r.tool == action and r.user == self.user
                and normalized(action, r.arguments) == args]
        if len(rows) != 1:
            raise BusinessError("not_found", "评测业务环境没有匹配当前用户、对象和参数的数据")
        return rows[0]

    def read(self, action, arguments):
        with self.business.lock:
            rule = self._rule(self.business.reads, action, arguments)
            data = deepcopy(rule.data)
            if rule.state_key:
                data.update(deepcopy(self.business.objects[rule.state_key]))
                data.pop("user_id", None)
            result = ok(data, limitations=rule.limitations, sources=["evaluation_fixture"])
            self.business.journal.add("events", dict(kind="business_read", tool=action,
                user_id=self.user, arguments=deepcopy(arguments), result=deepcopy(result)))
            return result

    def preview(self, action, arguments):
        with self.business.lock:
            rule = self._rule(self.business.writes, action, arguments)
            state = self.business.objects[rule.state_key]
            args = normalized(action, arguments)
            args.update(order_id=state["order_id"], platform_code=state["platform_code"])
            return dict(action=action, arguments=args, business_version=digest(state),
                        review=deepcopy(rule.review))

    def confirmation_review(self, prepared):
        # Prepared arguments are immutable and include the resolved identity.
        with self.business.lock:
            for rule in self.business.writes:
                if rule.tool == prepared["action"] and rule.user == self.user:
                    current = self.preview(rule.tool, rule.arguments)
                    if current["arguments"] == prepared["arguments"]:
                        if current["business_version"] != prepared["business_version"]:
                            raise BusinessError("not_eligible", "业务状态已变化")
                        return deepcopy(prepared["review"])
            raise BusinessError("not_found", "确认对象不存在")

    def submit(self, action, arguments, operation_id, business_version):
        with self.business.lock:
            self.business.journal.add("events", dict(kind="business_submit_attempt", action=action,
                user_id=self.user, operation_id=operation_id, arguments=deepcopy(arguments)))
            old = self.business.receipts.get(operation_id)
            signature = digest(dict(user=self.user, action=action, arguments=arguments, version=business_version))
            if old:
                if old["signature"] != signature:
                    raise BusinessError("not_eligible", "操作编号冲突")
                return deepcopy(old["result"])
            for rule in self.business.writes:
                if rule.tool != action or rule.user != self.user:
                    continue
                prepared = self.preview(action, rule.arguments)
                if prepared["arguments"] != arguments:
                    continue
                if prepared["business_version"] != business_version:
                    raise BusinessError("not_eligible", "业务版本失效")
                state = self.business.objects[rule.state_key]
                before = deepcopy(state)
                state.update(deepcopy(rule.changes))
                result = dict(success=True, request_id=operation_id, status=rule.result_status,
                    order_id=state["order_id"], platform_code=state["platform_code"])
                self.business.receipts[operation_id] = dict(signature=signature, user_id=self.user,
                    action=action, result=deepcopy(result))
                self.business.journal.add("writes", dict(action=action, operation_id=operation_id,
                    user_id=self.user, state_key=rule.state_key, arguments=deepcopy(arguments),
                    before=before, after=deepcopy(state), result=deepcopy(result)))
                return result
            raise BusinessError("not_found", "写入对象和参数不匹配")
