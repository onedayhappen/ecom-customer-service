# 文件说明：定义业务错误、结果契约、金额时间转换及加密序列化工具。
"""Business errors, result contracts, serialization and encryption primitives."""

import base64
import hashlib
import json
import os
from datetime import datetime, timezone
from decimal import Decimal
from cryptography.hazmat.primitives.ciphers.aead import AESGCM


class BusinessError(Exception):
    def __init__(self, code, message, **details):
        self.code, self.message, self.details = code, message, details
        super().__init__(message)


def utcnow():
    return datetime.now(timezone.utc).replace(tzinfo=None)


def at(value):
    if value is None:
        return None
    if isinstance(value, str):
        value = datetime.fromisoformat(value.replace("Z", "+00:00"))
    return (
        value.astimezone(timezone.utc).replace(tzinfo=None) if value.tzinfo else value
    )


def encode(value):
    if isinstance(value, Decimal):
        return format(value, ".2f")
    if isinstance(value, datetime):
        return value.isoformat() + "Z" if not value.tzinfo else value.isoformat()
    raise TypeError(type(value).__name__)


def dumps(value):
    return json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=encode
    )


def decoded(value, default=None):
    if value is None:
        return default
    return json.loads(value) if isinstance(value, (str, bytes)) else value


def digest(value):
    return hashlib.sha256(dumps(value).encode()).hexdigest()


def ref(kind, *parts):
    return (
        base64.urlsafe_b64encode(dumps([kind, *map(str, parts)]).encode())
        .decode()
        .rstrip("=")
    )


def unref(value, kinds):
    try:
        data = json.loads(base64.urlsafe_b64decode(value + "=" * (-len(value) % 4)))
        if (
            not isinstance(data, list)
            or data[0] not in kinds
            or not all(isinstance(x, str) and x for x in data)
        ):
            raise ValueError()
        if len(data) != (3 if data[0] == "order" else 2):
            raise ValueError()
        return data
    except Exception:
        raise BusinessError("invalid_arguments", "对象引用无效") from None


def money(value):
    if value is None:
        raise BusinessError("insufficient_data", "金额缺失，无法核实")
    return Decimal(str(value)).quantize(Decimal(".01"))


def ok(data, *, limitations=(), sources=(), next_cursor=None):
    return {
        "ok": True,
        "success": True,
        "data": data,
        "observed_at": encode(utcnow()),
        "data_as_of": None,
        "sources": list(sources),
        "completeness": "partial" if limitations else "complete",
        "limitations": list(limitations),
        "next_cursor": next_cursor,
    }


def failure(error):
    legacy = {
        "temporarily_unavailable": "transient_failure",
        "result_unknown": "transient_failure",
        "state_changed": "confirmation_invalid",
        "not_eligible": "business_rejection",
        "insufficient_data": "evidence_unavailable",
        "unsupported_capability": "evidence_unavailable",
    }
    return {
        "ok": False,
        "success": False,
        "status": "no_match" if error.code == "not_found" else "unavailable",
        "error": {"code": error.code, "message": error.message, **error.details},
        "error_code": legacy.get(error.code, "validation_error"),
    }


class Cipher:
    def __init__(self, keys, version):
        self.keys, self.version = decoded(keys, {}) or {}, version

    def key(self, version):
        try:
            key = base64.urlsafe_b64decode(self.keys[str(version)])
            if len(key) != 32:
                raise ValueError()
            return key
        except Exception:
            raise BusinessError(
                "temporarily_unavailable", "业务加密密钥未配置或版本不可用"
            ) from None

    def seal(self, value, purpose):
        nonce = os.urandom(12)
        return nonce + AESGCM(self.key(self.version)).encrypt(
            nonce, value.encode(), purpose.encode()
        )

    def open(self, value, purpose, version):
        try:
            raw = bytes(value)
            return (
                AESGCM(self.key(version))
                .decrypt(raw[:12], raw[12:], purpose.encode())
                .decode()
            )
        except BusinessError:
            raise
        except Exception:
            raise BusinessError("insufficient_data", "加密资料无法核实") from None
