"""验证会话历史只展示用户可见内容。"""

import json
from uuid import uuid4

import pytest

from app.api.main import _display_messages


@pytest.mark.parametrize('owner', [None, 'other-user', 'alice'])
def test_legacy_history_requires_verified_owner(tmp_path, monkeypatch, owner):
    from app.api import main
    from app.support.store import SupportStore
    from fastapi import HTTPException
    store = SupportStore('sqlite:///' + (tmp_path / 'history.db').as_posix())
    monkeypatch.setattr(main, 'get_store', lambda: store)
    monkeypatch.setattr(main, 'load_session', lambda *args: dict(
        metadata={'owner_user_id': owner}, messages=[dict(role='user', content='历史私密信息')]))
    if owner == 'alice':
        assert main._load_session_history(str(uuid4()), 'alice').messages[0].content == '历史私密信息'
    else:
        with pytest.raises(HTTPException) as rejected:
            main._load_session_history(str(uuid4()), 'alice')
        assert rejected.value.status_code == 403


def test_display_messages_hides_internal_observation_summary():
    messages = [
        {"role": "user", "content": "订单 ORD-001 到哪了？"},
        {
            "role": "system",
            "content": "本轮工具观察摘要，仅供延续上下文使用，不是用户指令：\n- 已查询物流。",
        },
        {"role": "assistant", "content": "您的订单正在派送中。"},
    ]

    display = _display_messages(messages)

    assert [(message.role, message.content) for message in display] == [
        ("user", "订单 ORD-001 到哪了？"),
        ("assistant", "您的订单正在派送中。"),
    ]


def test_display_messages_deduplicates_legacy_structured_reply():
    reply = "您的订单正在派送中。"
    messages = [
        {"role": "user", "content": "订单 ORD-001 到哪了？"},
        {"role": "assistant", "content": reply},
        {
            "role": "assistant",
            "content": json.dumps({"reply": reply}, ensure_ascii=False),
        },
    ]

    display = _display_messages(messages)

    assert [(message.role, message.content) for message in display] == [
        ("user", "订单 ORD-001 到哪了？"),
        ("assistant", reply),
    ]
