"""验证会话历史只展示用户可见内容。"""

import json

from app.api.main import _display_messages


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
