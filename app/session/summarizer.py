import json
from typing import Optional

from openai import OpenAI

from app.prompts.summarizer import SUMMARY_PROMPT
from app.config.settings import settings


def summarize(
    client: OpenAI,
    model: str,
    old_messages: list[dict],
    prev_summary: Optional[str],
) -> str:
    """把老对话（可选地带上上一次 summary）压缩成新的 summary 文本。

    支持 user / assistant / tool 以及含 tool_calls 的 assistant 消息。
    """
    parts: list[str] = []
    # 上一版摘要是输入的一部分，使摘要能够跨多次压缩持续演进。
    if prev_summary:
        parts.append(f"【此前摘要】\n{prev_summary}")

    transcript_lines = []
    for msg in old_messages:
        role = msg.get("role")
        content = msg.get("content") or ""

        if role == "user":
            transcript_lines.append(f"用户：{content}")
        elif role == "assistant":
            tool_calls = msg.get("tool_calls")
            if tool_calls:
                for tc in tool_calls:
                    func = tc.get("function", {})
                    name = func.get("name", "?")
                    args = func.get("arguments", "{}")
                    transcript_lines.append(f"客服：[调用工具 {name}({args})]")
            if content:
                transcript_lines.append(f"客服：{content}")
        elif role == "tool":
            # 工具结果可能很长；摘要阶段只保留可用于后续决策的有限观察值。
            display = content if len(content) <= 200 else content[:200] + "..."
            transcript_lines.append(f"[工具结果] {display}")

    parts.append("【待压缩对话】\n" + "\n".join(transcript_lines))

    user_content = "\n\n".join(parts)

    response = client.chat.completions.create(
        model=model,
        temperature=0.3,
        messages=[
            {"role": "system", "content": SUMMARY_PROMPT},
            {"role": "user", "content": user_content},
        ],
        **settings.chat_completion_options,
    )
    return response.choices[0].message.content.strip()
