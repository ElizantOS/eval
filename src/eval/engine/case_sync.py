from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import re
from typing import Any

import yaml

try:
    from eval.engine.common import (
        load_app_config,
        configured_skills_dir,
        DEFAULT_SKILLS_DIR,
        EvalConfigError,
        ensure_directory,
        parse_frontmatter,
        slugify,
    )
except ImportError:  # pragma: no cover - script entry path
    from eval.engine.common import (
        load_app_config,
        configured_skills_dir,
        DEFAULT_SKILLS_DIR,
        EvalConfigError,
        ensure_directory,
        parse_frontmatter,
        slugify,
    )


@dataclass(slots=True)
class SkillExample:
    question: str
    answer: str


@dataclass(slots=True)
class SkillSeed:
    name: str
    description: str
    allowed_tools: list[str]
    instructions: str
    examples: list[SkillExample]


def _load_skill_seed(path: Path) -> SkillSeed:
    payload, body = parse_frontmatter(path.read_text(encoding="utf-8"), source=path)
    name = str(payload.get("name") or path.parent.name).strip()
    description = str(payload.get("description") or "").strip()
    allowed_tools = payload.get("allowed_tools") or []
    if not isinstance(allowed_tools, list):
        raise EvalConfigError(f"allowed_tools must be a list in {path}")
    examples = _extract_examples(body)
    return SkillSeed(
        name=name,
        description=description,
        allowed_tools=[str(item).strip() for item in allowed_tools if str(item).strip()],
        instructions=body.strip(),
        examples=examples,
    )


def _extract_examples(markdown_text: str) -> list[SkillExample]:
    pattern = re.compile(
        r"-\s*输入[:：]\s*(?P<question>.+?)\s*\n\s*输出[:：]\s*(?P<answer>.+?)(?=\n-\s*输入[:：]|\Z)",
        re.DOTALL,
    )
    examples: list[SkillExample] = []
    for match in pattern.finditer(markdown_text):
        question = re.sub(r"\s+", " ", match.group("question")).strip()
        answer = re.sub(r"\s+", " ", match.group("answer")).strip()
        if question and answer:
            examples.append(SkillExample(question=question, answer=answer))
    return examples


def _base_case_payload(skill: SkillSeed, *, target_id: str) -> dict[str, Any]:
    expected_mode = "interactive" if "ask_clarification" in skill.allowed_tools else "single_turn"
    return {
        "id": f"{skill.name}-base",
        "title": f"{skill.name} 基础能力",
        "enabled": True,
        "target_id": target_id,
        "skill_name": skill.name,
        "tags": ["auto-generated", "base", skill.name],
        "entry_question": skill.examples[0].question if skill.examples else f"请执行 {skill.name} 对应的分析",
        "expected_mode": expected_mode,
        "conversation_script": (
            [
                {
                    "slot": "organization_name",
                    "question_contains": ["组织"],
                    "answer": "华东大区",
                }
            ]
            if expected_mode == "interactive"
            else []
        ),
        "simulated_user_profile": {
            "organization_name": "华东大区",
            "province_area_name": "上海省区",
            "date_range": "最近7天",
            "metric": "货量",
            "defaults": {
                "compare_basis": "较昨日",
                "time_window_definition": "只看今天 vs 昨天",
            },
        },
        "judge_rubric": (
            f"围绕技能 `{skill.name}` 的目标进行评分。"
            "确认回答贴合问题、结构清晰，并遵守技能中的业务边界和澄清要求。"
        ),
        "hard_assertions": None,
        "target_environments": [],
    }


def _example_case_payload(skill: SkillSeed, example: SkillExample, index: int, *, target_id: str) -> dict[str, Any]:
    return {
        "id": f"{skill.name}-example-{index}",
        "title": f"{skill.name} 示例 {index}",
        "enabled": True,
        "target_id": target_id,
        "skill_name": skill.name,
        "tags": ["auto-generated", "example", skill.name],
        "entry_question": example.question,
        "expected_mode": "single_turn",
        "conversation_script": [],
        "simulated_user_profile": {
            "organization_name": "华东大区",
            "province_area_name": "上海省区",
            "date_range": "最近7天",
            "metric": "货量",
            "defaults": {
                "time_window_definition": "只看今天 vs 昨天",
            },
        },
        "judge_rubric": (
            f"围绕 `{example.question}` 这类问题进行评分。"
            "答案应沿着技能描述中的分析路径展开，不应跳题或凭空捏造数据。"
        ),
        "hard_assertions": ["no_error", "non_empty_final_answer"],
        "target_environments": [],
    }


def _clarification_case_payload(skill: SkillSeed, *, target_id: str) -> dict[str, Any]:
    return {
        "id": f"{skill.name}-clarification",
        "title": f"{skill.name} 缺槽位澄清",
        "enabled": True,
        "target_id": target_id,
        "skill_name": skill.name,
        "tags": ["auto-generated", "clarification", skill.name],
        "entry_question": "帮我分析一下最近的情况",
        "expected_mode": "interactive",
        "conversation_script": [
            {
                "slot": "organization_name",
                "question_contains": ["组织", "省区", "网点"],
                "answer": "华东大区",
            },
            {
                "slot": "date_range",
                "question_contains": ["时间", "范围", "日期"],
                "answer": "最近7天",
            },
        ],
        "simulated_user_profile": {
            "organization_name": "华东大区",
            "province_area_name": "上海省区",
            "date_range": "最近7天",
            "analysis_topic": "货量和单量异常波动",
            "metric": "货量",
            "time_window_definition": "只看今天 vs 昨天",
        },
        "judge_rubric": (
            "重点检查 agent 是否先澄清缺失信息，再继续当前分析任务。"
            "澄清选项应具体互斥，补充信息后应继续原任务而不是换题。"
        ),
        "hard_assertions": ["no_error", "non_empty_final_answer"],
        "target_environments": [],
    }


def _render_case_markdown(payload: dict[str, Any], body: str) -> str:
    frontmatter = yaml.safe_dump(
        payload,
        allow_unicode=True,
        sort_keys=False,
        width=120,
    ).strip()
    return f"---\n{frontmatter}\n---\n\n{body.strip()}\n"


def _case_body(skill: SkillSeed, *, source_path: Path, note: str) -> str:
    return (
        f"来源技能：`{skill.name}`\n\n"
        f"来源文件：`{source_path}`\n\n"
        f"自动生成说明：{note}\n\n"
        f"技能描述：{skill.description or '无'}\n\n"
        "手工备注：\n"
        "- 可以直接修改 frontmatter 中的问法、标签和评分提示词。\n"
        "- 这里适合补充业务背景、已知限制和人工审阅要点。\n"
    )


def sync_cases(
    *,
    skills_dir: Path = DEFAULT_SKILLS_DIR,
    target_id: str | None = None,
    auto_cases_dir: Path,
    refresh_generated: bool = False,
) -> list[Path]:
    if skills_dir == DEFAULT_SKILLS_DIR:
        skills_dir = configured_skills_dir()
    resolved_target_id = str(target_id or "").strip() or load_app_config().default_target_id
    ensure_directory(auto_cases_dir)
    written: list[Path] = []
    for skill_file in sorted(skills_dir.glob("*/SKILL.md")):
        skill = _load_skill_seed(skill_file)
        base_payload = _base_case_payload(skill, target_id=resolved_target_id)
        candidates = [base_payload]
        for index, example in enumerate(skill.examples, start=1):
            candidates.append(_example_case_payload(skill, example, index, target_id=resolved_target_id))
        if "ask_clarification" in skill.allowed_tools:
            candidates.append(_clarification_case_payload(skill, target_id=resolved_target_id))

        for candidate in candidates:
            case_path = auto_cases_dir / f"{slugify(candidate['id'])}.md"
            if case_path.exists() and not refresh_generated:
                continue
            note = "来自 skill 自动种子化，可在不改变文件名的前提下手工调整。"
            body = _case_body(skill, source_path=skill_file, note=note)
            case_path.write_text(_render_case_markdown(candidate, body), encoding="utf-8")
            written.append(case_path)
    return written
