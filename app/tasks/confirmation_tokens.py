# 文件说明：生成并校验绑定固定事项操作的确认凭据摘要。
"""Credentials bound to immutable task operations; authorization stays in TaskOperations."""
import hashlib
import hmac
import json
import secrets


def token_hash(token: str) -> str:
    return hashlib.sha256(token.encode('utf-8')).hexdigest()


def bound_token(binding: dict, secret: str) -> str:
    if len(secret) < 32:
        raise ValueError('A confirmation signing secret of at least 32 characters is required')
    canonical = json.dumps(binding, ensure_ascii=False, sort_keys=True, separators=(',', ':'))
    return hmac.new(secret.encode('utf-8'), canonical.encode('utf-8'), hashlib.sha256).hexdigest()


def token_matches(token: str, expected_hash: str) -> bool:
    return bool(token and expected_hash and secrets.compare_digest(token_hash(token), expected_hash))
