"""Agent 技能模块（第8期）。

基于 Anthropic Agent Skills 开放标准（agentskills.io/specification），
每个 Skill 是一个 SKILL.md 文件（YAML frontmatter + Markdown 指令），
用于教会 Agent 如何处理特定场景的标准化流程。

保留内部发现和加载能力。旧 load_skill 工具已移除，当前没有模型调用入口。
"""

from app.skills.loader import SkillManager, SkillMeta

__all__ = ["SkillManager", "SkillMeta"]
