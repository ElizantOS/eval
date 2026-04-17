from __future__ import annotations

import json
import os
from typing import Any

try:
    from eval.adapters.runner_common import (
        build_auth_headers as _build_headers,
        call_json_model,
        maybe_switch_provider as _maybe_switch_provider,
        perform_request as _perform_request,
        run_target_case,
    )
    from eval.engine.common import EvalConfigError
    from eval.engine.common import read_active_run_progress, write_active_run_progress
except ImportError:  # pragma: no cover - promptfoo file:// loader path
    from eval.adapters.runner_common import (
        build_auth_headers as _build_headers,
        call_json_model,
        maybe_switch_provider as _maybe_switch_provider,
        perform_request as _perform_request,
        run_target_case,
    )
    from eval.engine.common import EvalConfigError
    from eval.engine.common import read_active_run_progress, write_active_run_progress


def _active_hard_assertions(context_vars: dict[str, Any]) -> list[str]:
    if isinstance(context_vars.get("hard_assertions"), list):
        return [str(item).strip() for item in context_vars.get("hard_assertions") or [] if str(item).strip()]
    raw = str(context_vars.get("hard_assertions_json") or "").strip()
    if not raw:
        return []
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError:
        return []
    if not isinstance(parsed, list):
        return []
    return [str(item).strip() for item in parsed if str(item).strip()]


def _active_hard_assert_result(*, provider_payload: dict[str, Any], context_vars: dict[str, Any]) -> dict[str, Any]:
    hard_assertions = _active_hard_assertions(context_vars)
    final_answer = str(provider_payload.get("final_answer") or "").strip()
    ask_count = int(provider_payload.get("ask_count") or 0)
    transcript = provider_payload.get("transcript")
    error = provider_payload.get("error")
    failures: list[str] = []

    for assertion_name in hard_assertions:
        if assertion_name == "no_error" and error:
            failures.append(f"expected no error but got: {json.dumps(error, ensure_ascii=False)}")
        elif assertion_name == "non_empty_final_answer" and not final_answer:
            failures.append("final_answer is empty")
        elif assertion_name == "must_ask_clarification" and ask_count <= 0:
            failures.append("expected ask_clarification to occur at least once")
        elif assertion_name == "must_not_require_clarification" and ask_count != 0:
            failures.append("expected no ask_clarification calls")
        elif assertion_name == "transcript_present" and not transcript:
            failures.append("expected transcript to be present")

    return {
        "score": 0.0 if failures else 1.0,
        "passed": not failures,
        "details": failures,
    }


def _record_active_case_result(*, context_vars: dict[str, Any], provider_payload: dict[str, Any]) -> None:
    current = read_active_run_progress()
    if not isinstance(current, dict) or not current:
        return
    target = context_vars.get("target") if isinstance(context_vars.get("target"), dict) else {}
    current_target_id = str(current.get("target_id") or "").strip()
    target_id = str(target.get("id") or "").strip()
    case_id = str(context_vars.get("case_id") or "").strip()
    if not case_id or not current_target_id or current_target_id != target_id:
        return

    completed_cases = current.get("completed_cases") if isinstance(current.get("completed_cases"), dict) else {}
    compact = _compact_provider_payload(provider_payload)
    evaluation = _active_evaluation(provider_payload=provider_payload, context_vars=context_vars)
    completed_cases[case_id] = {
        "case_id": case_id,
        "title": context_vars.get("title"),
        "entry_question": context_vars.get("entry_question"),
        "skill_name": context_vars.get("skill_name"),
        "status": "failed" if provider_payload.get("error") else "completed",
        "final_answer": provider_payload.get("final_answer") or "",
        "ask_count": provider_payload.get("ask_count"),
        "error": provider_payload.get("error"),
        "session_id": provider_payload.get("session_id"),
        "transcript": compact.get("transcript") or [],
        "events": compact.get("events") or [],
        "runner_warnings": compact.get("runner_warnings") or [],
        "updated_at": os.environ.get("SMARTBOT_EVAL_NOW") or "",
        **evaluation,
    }
    current["completed_cases"] = completed_cases
    write_active_run_progress(current)


def _compact_provider_payload(provider_payload: dict[str, Any]) -> dict[str, Any]:
    transcript_summary: list[dict[str, Any]] = []
    tool_names: list[str] = []
    for turn in provider_payload.get("transcript") or []:
        if not isinstance(turn, dict):
            continue
        asks = turn.get("asks") if isinstance(turn.get("asks"), list) else []
        raw_output = turn.get("raw_output") if isinstance(turn.get("raw_output"), list) else []
        for item in raw_output:
            if isinstance(item, dict) and item.get("type") == "function_call":
                name = str(item.get("name") or "").strip()
                if name and name not in tool_names:
                    tool_names.append(name)
        transcript_summary.append(
            {
                "turn": turn.get("turn"),
                "status": turn.get("status"),
                "response_id": turn.get("response_id"),
                "session_id": turn.get("session_id"),
                "asks": asks,
                "driver_event": turn.get("driver_event"),
                "final_answer_preview": str(turn.get("final_answer") or "")[:1200],
                "error": turn.get("error"),
            }
        )
    return {
        "final_answer": provider_payload.get("final_answer"),
        "ask_count": provider_payload.get("ask_count"),
        "events": provider_payload.get("events"),
        "error": provider_payload.get("error"),
        "session_id": provider_payload.get("session_id"),
        "target_id": provider_payload.get("target_id"),
        "tool_names": tool_names,
        "unexpected_asks": provider_payload.get("unexpected_asks"),
        "unused_script_steps": provider_payload.get("unused_script_steps"),
        "runner_warnings": provider_payload.get("runner_warnings"),
        "simulated_user_trace": provider_payload.get("simulated_user_trace"),
        "transcript": transcript_summary,
    }


def _judge_output(*, provider_payload: dict[str, Any], context_vars: dict[str, Any]) -> dict[str, Any]:
    if not str(os.environ.get("OPENAI_API_KEY") or "").strip():
        return {
            "score": 0,
            "verdict": "fail",
            "summary": "OPENAI_API_KEY missing for judge request",
            "strengths": [],
            "issues": ["judge provider is not configured"],
        }
    try:
        parsed = call_json_model(
            system_prompt=(
                "你是智能问数 agent 的评测裁判。返回 JSON，字段固定为 "
                "score(0-10整数), verdict(pass|warn|fail), summary, strengths, issues。"
                "评分重点：是否贴题、是否遵守澄清策略、是否有幻觉、是否能继续原任务。"
            ),
            user_payload={
                "metadata": {
                    "case_id": context_vars.get("case_id"),
                    "title": context_vars.get("title"),
                    "judge_rubric": context_vars.get("judge_rubric"),
                },
                "provider_output": _compact_provider_payload(provider_payload),
            },
        )
    except EvalConfigError as exc:
        return {
            "score": 0,
            "verdict": "fail",
            "summary": str(exc),
            "strengths": [],
            "issues": [str(exc)],
        }
    return {
        "score": int(parsed.get("score", 0)),
        "verdict": str(parsed.get("verdict", "fail")),
        "summary": str(parsed.get("summary", "")).strip(),
        "strengths": [str(item) for item in parsed.get("strengths", []) if str(item).strip()],
        "issues": [str(item) for item in parsed.get("issues", []) if str(item).strip()],
    }


def _active_evaluation(*, provider_payload: dict[str, Any], context_vars: dict[str, Any]) -> dict[str, Any]:
    hard_assert = _active_hard_assert_result(provider_payload=provider_payload, context_vars=context_vars)
    judge = _judge_output(provider_payload=provider_payload, context_vars=context_vars)
    final_score = round((0.4 * float(hard_assert["score"])) + (0.6 * (float(judge["score"]) / 10.0)), 4)
    return {
        "hard_assert_score": round(float(hard_assert["score"]), 4),
        "hard_assert": hard_assert,
        "judge_score": judge,
        "judge": judge,
        "final_eval_score": final_score,
        "final_score": final_score,
    }


def run_interactive_case(prompt: str, context_vars: dict[str, Any]) -> dict[str, Any]:
    return run_target_case(prompt, context_vars)


def call_api(prompt: str, options: dict[str, Any], context: dict[str, Any]) -> dict[str, Any]:
    vars_map = context.get("vars") if isinstance(context, dict) else {}
    if not isinstance(vars_map, dict):
        raise EvalConfigError("promptfoo context vars missing")
    try:
        payload = run_interactive_case(prompt, vars_map)
    except EvalConfigError as exc:
        payload = {
            "final_answer": "",
            "transcript": [],
            "ask_count": 0,
            "events": ["driver_error"],
            "latency_ms": 0,
            "error": str(exc),
            "session_id": None,
            "target_id": (vars_map.get("target") or {}).get("id") if isinstance(vars_map.get("target"), dict) else None,
            "request_payload": None,
            "response_payload": None,
            "request_payloads": [],
            "response_payloads": [],
            "simulated_user_trace": [],
            "unexpected_asks": [],
            "unused_script_steps": [],
            "runner_warnings": [str(exc)],
        }
    _record_active_case_result(context_vars=vars_map, provider_payload=payload)
    return {
        "output": json.dumps(payload, ensure_ascii=False, indent=2),
        "metadata": {},
    }
