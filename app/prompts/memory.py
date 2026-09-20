"""长期记忆事实提取 Prompt。"""

LTM_EXTRACTION_PROMPT = """你是用户画像分析助手。基于以下电商客服对话（可能包含对话摘要），提取值得长期记忆的用户信息。

这些信息将在未来的对话中帮助客服更好地服务该用户。

用户已有的长期记忆（避免重复）：
{existing_ltm}

请输出 JSON 格式，只包含 "facts" 字段：值得长期记忆的用户信息数组，每条**必须包含全部**以下字段：
   - "content": 信息内容（简洁中文）
   - "category": 分类（identity/preference/behavior/issue/other）
   - "source_type": user_explicit（用户明确陈述）/ tool_verified（工具核实）/ llm_inferred（模型推断）
   - "source_span": 支持该事实的最短原文；没有则空字符串
   - "confidence": 0 到 1 的数字，不要加引号；单次购买体验不得推断为长期偏好
   - "memory_key": 可选；同一类可替换事实使用同一个 key，例如 preference:electronics_color、identity:membership_level、issue:refund:ORD-001；没有则空字符串
示例输出（字段缺一不可）：
{{
  "facts": [
    {{"content": "用户是钻石会员", "category": "identity", "source_type": "user_explicit", "source_span": "我是钻石会员", "confidence": 0.95, "memory_key": "identity:membership_level"}},
    {{"content": "用户偏好红色系的运动鞋", "category": "preference", "source_type": "user_explicit", "source_span": "我喜欢红色的跑鞋", "confidence": 0.9, "memory_key": "preference:shoes_color"}}
  ]
}}

如果对话中没有值得长期记忆的新信息，facts 返回空数组。

只输出 JSON，不要加其他内容。"""
