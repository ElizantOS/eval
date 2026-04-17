from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

try:
    from eval.engine.common import (
        load_target,
        configured_cases_dir,
        EvalConfigError,
        load_cases,
        load_judge_provider,
        selected_filters_from_env,
    )
except ImportError:  # pragma: no cover - promptfoo file:// loader path
    from eval.engine.common import (
        load_target,
        configured_cases_dir,
        EvalConfigError,
        load_cases,
        load_judge_provider,
        selected_filters_from_env,
    )


def _path_from_env(name: str, default: Path) -> Path:
    value = os.getenv(name)
    return Path(value).expanduser().resolve() if value else default.resolve()


def generate_tests(config: dict[str, Any] | None = None) -> list[dict[str, Any]]:
    config = config or {}
    cases_dir = _path_from_env("SMARTBOT_EVAL_CASES_DIR", configured_cases_dir())

    filters = selected_filters_from_env()
    selected_target = filters["selected_target"]
    target = load_target(selected_target)

    judge_provider = load_judge_provider()
    cases = load_cases(
        cases_dir,
        target_id=target.id,
        case_pattern=filters["case_pattern"],
        tag=filters["tag"],
        skill_name=filters["skill_name"],
    )
    if not cases:
        raise EvalConfigError(
            f"no cases matched filters for target={target.id!r}"
        )

    tests: list[dict[str, Any]] = []
    for case in cases:
        tests.append(
            {
                "description": case.summary,
                "vars": {
                    "case_id": case.case_id,
                    "title": case.title,
                    "entry_question": case.entry_question,
                    "expected_mode": case.expected_mode,
                    "case_body": case.body,
                    "judge_rubric": case.judge_rubric,
                    "conversation_script": [
                        {
                            "answer": step.answer,
                            "slot": step.slot,
                            "question_contains": step.question_contains or [],
                        }
                        for step in case.conversation_script
                    ],
                    "simulated_user_profile": case.simulated_user_profile,
                    "hard_assertions_json": json.dumps(case.hard_assertions, ensure_ascii=False),
                    "target": {
                        "id": target.id,
                        "name": target.name,
                        "protocol": target.protocol,
                        "driver_class": target.driver_class,
                        "base_url": target.base_url,
                        "auth_mode": target.auth_mode,
                        "auth_value": os.getenv(target.auth_value_ref or "", ""),
                        "identity": target.identity,
                        "default_headers": target.default_headers,
                        "conversation_mode": target.conversation_mode,
                        "session_header_name": target.session_header_name,
                        "previous_response_supported": target.previous_response_supported,
                        "history_strategy": target.history_strategy,
                        "tool_call_shape": target.tool_call_shape,
                        "tool_result_shape": target.tool_result_shape,
                        "clarification_mode": target.clarification_mode,
                        "admin_provider_profile": target.admin_provider_profile,
                    },
                },
                "metadata": {
                    "caseId": case.case_id,
                    "targetId": case.target_id,
                    "title": case.title,
                    "skillName": case.skill_name,
                    "tags": case.tags,
                    "casePath": str(case.case_path),
                    "bodyMarkdown": case.body,
                    "judgeRubric": case.judge_rubric,
                },
                "assert": [
                    {
                        "type": "javascript",
                        "value": f"file://{(Path(__file__).resolve().parent / 'assertions.js')}",
                    }
                ],
            }
        )
    return tests
