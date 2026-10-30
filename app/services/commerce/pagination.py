# 文件说明：实现签名快照游标和 SQL 键集分页，绑定身份与查询参数。
"""Signed snapshot and SQL keyset pagination with a shared cursor contract."""

import base64
import hmac
import json
import time
from datetime import datetime

from app.services.commerce.common import BusinessError, at, digest, dumps, ok


class PaginationMixin:
    """Requires the service's trusted user, scope and signing secret."""

    def _cursor_binding(self, tool, conditions):
        return digest(
            [
                self.user,
                tool,
                conditions,
                getattr(self.scope, "platform_code", None),
                getattr(self.scope, "store_id", None),
            ]
        )

    def _decode_cursor(self, token, binding):
        try:
            raw, signature = token.rsplit(".", 1)
            if not self.secret:
                raise ValueError()
            expected = hmac.new(self.secret.encode(), raw.encode(), "sha256").hexdigest()
            if not hmac.compare_digest(signature, expected):
                raise ValueError()
            payload = json.loads(base64.urlsafe_b64decode(raw))
            if payload["binding"] != binding or payload["expires"] < time.time():
                raise ValueError()
            return payload
        except (ValueError, TypeError, KeyError, AttributeError):
            raise BusinessError("invalid_cursor", "分页游标无效或已过期") from None

    def _encode_cursor(self, payload):
        if not self.secret:
            raise BusinessError("temporarily_unavailable", "分页签名未配置")
        payload["expires"] = int(time.time()) + 900
        raw = base64.urlsafe_b64encode(dumps(payload).encode()).decode()
        signature = hmac.new(self.secret.encode(), raw.encode(), "sha256").hexdigest()
        return raw + "." + signature

    def page(self, rows, tool, args, *, data=None, limitations=(), size=20):
        """Bind each page to the collection digest to detect changed results."""
        if len(rows) > 10000:
            raise BusinessError("insufficient_data", "结果过多，请缩小查询范围")
        conditions = {key: value for key, value in args.items() if key != "cursor"}
        binding = self._cursor_binding(tool, conditions)
        snapshot = digest(rows)
        offset = 0
        if args.get("cursor"):
            payload = self._decode_cursor(args["cursor"], binding)
            try:
                if payload["snapshot"] != snapshot:
                    raise BusinessError(
                        "state_changed", "查询结果已变化，请从第一页重新查询"
                    )
                offset = payload["offset"]
                if not isinstance(offset, int) or offset < 0 or offset >= len(rows):
                    raise ValueError()
            except (ValueError, TypeError, KeyError):
                raise BusinessError("invalid_cursor", "分页游标无效或已过期") from None
        next_token = None
        if offset + size < len(rows):
            next_token = self._encode_cursor(
                {"binding": binding, "snapshot": snapshot, "offset": offset + size}
            )
        return ok(
            {
                **(data or {}),
                "items": rows[offset : offset + size],
                "total": len(rows),
                "has_more": bool(next_token),
                "applied_conditions": conditions,
            },
            limitations=limitations,
            next_cursor=next_token,
        )

    def sql_page(
        self, tx, query, values, tool, args, *, keys, size=20, data=None, limitations=(), descending=()
    ):
        """Bounded keyset pagination; query and keys are trusted internal SQL."""
        conditions = {key: value for key, value in args.items() if key != "cursor"}
        binding = self._cursor_binding(tool, conditions)
        condition = ""
        descending = tuple(key for key in keys if key in descending)
        columns = ",".join(key + (" DESC" if key in descending else " ASC") for key in keys)
        if not descending:
            columns = ",".join(keys)
        parameters = list(values)
        if args.get("cursor"):
            payload = self._decode_cursor(args["cursor"], binding)
            try:
                if (payload["keys"] != list(keys) or len(payload["last"]) != len(keys)
                        or payload.get("descending", []) != list(descending)):
                    raise ValueError()
                last = [
                    at(value["value"])
                    if value["type"] == "datetime"
                    else value["value"]
                    for value in payload["last"]
                ]
            except (ValueError, TypeError, KeyError, AttributeError):
                raise BusinessError("invalid_cursor", "分页游标无效或已过期") from None
            if not descending:
                condition = " WHERE (" + columns + ") > (" + ",".join(["%s"] * len(keys)) + ")"
                parameters.extend(last)
            else:
                terms = []
                for index, key in enumerate(keys):
                    operator = "<" if key in descending else ">"
                    terms.append("(" + " AND ".join(
                        [previous + "=%s" for previous in keys[:index]] + [key + operator + "%s"]
                    ) + ")")
                    parameters.extend(last[:index + 1])
                condition = " WHERE " + " OR ".join(terms)
        total = tx.one(
            f"SELECT COUNT(*) AS n FROM ({query}) page_count", tuple(values)
        )["n"]
        rows = tx.all(
            f"SELECT * FROM ({query}) page_rows{condition} ORDER BY {columns} LIMIT %s",
            tuple(parameters + [size + 1]),
        )
        more = len(rows) > size
        rows = rows[:size]
        next_token = None
        if more:
            last = [
                {
                    "type": "datetime"
                    if isinstance(rows[-1][key], datetime)
                    else "value",
                    "value": rows[-1][key],
                }
                for key in keys
            ]
            payload = {"binding": binding, "keys": list(keys), "last": last}
            if descending:
                payload['descending'] = list(descending)
            next_token = self._encode_cursor(payload)
        return ok(
            {
                **(data or {}),
                "items": rows,
                "total": total,
                "has_more": more,
                "applied_conditions": conditions,
            },
            next_cursor=next_token,
            limitations=[
                *limitations,
                "实时分页：各页独立读取，数据变更后可从第一页重新查询",
            ],
        )
