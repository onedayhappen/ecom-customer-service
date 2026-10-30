# 文件说明：发现技能文件、匹配路由关键词并按需加载正文，供独立加载器使用。
"""Skill 加载器：扫描 SKILL.md 文件，提供发现（catalog）和激活（load）能力。

遵循 Anthropic Agent Skills 开放标准（agentskills.io/specification）：
- 每个 Skill 是一个目录，包含 SKILL.md 文件（YAML frontmatter + Markdown 指令）
- 启动时只加载 name + description（~100 tokens/skill），注入 system prompt
- 内部调用 load_skill 方法时，加载完整 SKILL.md body；当前没有模型工具入口
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path


@dataclass
class SkillMeta:
    """Skill 元数据（从 SKILL.md frontmatter 解析）。"""

    name: str
    description: str
    path: Path
    routes: tuple[str, ...] = ()
    triggers: tuple[str, ...] = ()
    body: str = ""
    _body_loaded: bool = field(default=False, repr=False)

    def load_body(self) -> str:
        """加载完整的 SKILL.md body（指令部分）。"""
        if not self._body_loaded:
            raw = self.path.read_text(encoding="utf-8")
            self.body = _parse_body(raw)
            self._body_loaded = True
        return self.body


def _parse_frontmatter(content: str) -> dict:
    """解析 YAML frontmatter（简单正则，避免 PyYAML 依赖）。"""
    match = re.match(r"^---\s*\n(.*?)\n---", content, re.DOTALL)
    if not match:
        return {}

    result = {}
    current_key = None      # 正在解析的字段名
    current_value_lines: list[str] = []     # 收集当前字段的值

    
    for line in match.group(1).strip().splitlines():    # match.group(1) 获取两条横线中间的所有文本
        if ":" in line and not line.startswith(" "):
            if current_key is not None:
                result[current_key] = " ".join(current_value_lines).strip()
            key, _, value = line.partition(":")
            current_key = key.strip()
            current_value_lines = [value.strip()] if value.strip() else []
        elif current_key is not None:
            current_value_lines.append(line.strip())

    if current_key is not None:
        result[current_key] = " ".join(current_value_lines).strip()

    return result


def _parse_body(content: str) -> str:
    """提取 frontmatter 之后的 Markdown body。"""
    match = re.match(r"^---\s*\n.*?\n---\s*\n?", content, re.DOTALL)
    if match:
        return content[match.end():].strip()
    return content.strip()


class SkillManager:
    """Skill 管理器：发现、注册、加载 Skills。"""

    def __init__(self, skills_dir: str = "app/skills/definitions", enabled: bool = True):
        self.skills_dir = Path(skills_dir)
        self.enabled = enabled
        self._skills: dict[str, SkillMeta] = {}

        if self.enabled:
            self._discover()

    def _discover(self) -> None:
        """扫描 skills 目录，解析所有 SKILL.md 的 frontmatter。"""
        if not self.skills_dir.exists():
            return

        for skill_dir in sorted(self.skills_dir.iterdir()):     # iterdir()：遍历技能根目录下所有子项
            if not skill_dir.is_dir():
                continue
            skill_file = skill_dir / "SKILL.md"
            if not skill_file.exists():
                continue

            content = skill_file.read_text(encoding="utf-8")
            meta = _parse_frontmatter(content)

            name = meta.get("name", "")
            description = meta.get("description", "")
            if not name or not description:
                continue

            self._skills[name] = SkillMeta(
                name=name,
                description=description,
                path=skill_file,
                routes=self._split_values(meta.get("routes", "")),
                triggers=self._split_values(meta.get("triggers", "")),
            )

    @staticmethod
    def _split_values(value: str) -> tuple[str, ...]:
        return tuple(item.strip() for item in value.split(",") if item.strip())

    @property
    def skill_count(self) -> int:
        """快速获取当前成功加载的技能总数量"""
        return len(self._skills)

    @property
    def skill_names(self) -> list[str]:
        """返回所有已加载技能的 name 清单"""
        return list(self._skills.keys())

    def get_catalog(self) -> list[dict]:
        """返回 skill catalog（name + description），用于注入 system prompt。"""
        return [
            {"name": s.name, "description": s.description}
            for s in self._skills.values()
        ]

    def select_for_request(self, user_input: str, route: str) -> SkillMeta | None:
        """基于声明式路由和关键词，为本轮请求选择一个最匹配的 Skill。"""
        if not self.enabled:
            return None

        matched: list[tuple[int, SkillMeta]] = []
        for skill in self._skills.values():
            if skill.routes and route not in skill.routes:
                continue

            # 统计触发词命中数
            trigger_count = sum(trigger in user_input for trigger in skill.triggers)
            if trigger_count:
                matched.append((trigger_count, skill))

        if not matched:
            return None
        matched.sort(key=lambda item: item[0], reverse=True)
        return matched[0][1]


    def build_selected_skill_prompt(self, user_input: str, route: str) -> str:
        """仅注入当前请求命中的完整 SOP；未命中时不增加上下文。"""
        skill = self.select_for_request(user_input, route)
        if skill is None:
            return ""
        return (
            f"\n\n## 本轮业务流程：{skill.name}\n"
            "以下 SOP 已由系统按当前用户诉求选中。请遵循其步骤；"
            "真实业务动作仍必须调用相应工具，不能只在回复中声称已完成。\n\n"
            f"{skill.load_body()}"
        )

    def build_catalog_prompt(self) -> str:
        """构建注入 system prompt 的 skill catalog 文本。"""
        if not self.enabled or not self._skills:
            return ""

        lines = [
            "\n\n## 可用技能（Skills）",
            "以下是系统可自动匹配的专业技能。复杂业务流程会由编排器按当前请求"
            "加载详细指令；简单单次查询不会加载技能。\n",
        ]

        for skill in self._skills.values():
            lines.append(f"- **{skill.name}**：{skill.description}")

        lines.append("\n### 技能使用方式")
        lines.append("1. 系统会在匹配复杂流程时直接提供完整 SOP")
        lines.append("2. 按已加载的指令流程处理用户问题，使用已有工具完成具体操作")
        lines.append("3. 简单订单状态、单个商品规格或单条政策问答，直接调用业务工具")

        return "\n".join(lines)

    def load_skill(self, skill_name: str) -> dict:
        """生成精简技能目录清单，全局固定写入 System Prompt（系统提示词），常驻上下文"""
        if not self.enabled:
            return {"success": False, "error": "技能系统未启用"}

        skill = self._skills.get(skill_name)
        if not skill:
            available = ", ".join(self._skills.keys()) or "无"
            return {
                "success": False,
                "error": f"未找到技能「{skill_name}」，可用技能：{available}",
            }

        body = skill.load_body()
        return {
            "success": True,
            "skill_name": skill.name,
            "instructions": body,
        }
