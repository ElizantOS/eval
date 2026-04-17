from __future__ import annotations

from dataclasses import dataclass
import ast
import json
import os
from pathlib import Path
import shutil
from typing import Any
import uuid
from urllib.parse import quote
from urllib.request import Request, urlopen

from openai import OpenAI

try:
    from eval.engine.common import (
        DEFAULT_SUMMARY_JSON_FILE,
        DEFAULT_SUMMARY_MD_FILE,
        DEFAULT_RUNS_DIR,
        EvalConfigError,
        JudgeProvider,
        ensure_directory,
        load_case,
        load_target,
        serialize_x_user_info,
        utc_timestamp_slug,
    )
except ImportError:  # pragma: no cover
    from eval.engine.common import (
        DEFAULT_SUMMARY_JSON_FILE,
        DEFAULT_SUMMARY_MD_FILE,
        DEFAULT_RUNS_DIR,
        EvalConfigError,
        JudgeProvider,
        ensure_directory,
        load_case,
        load_target,
        serialize_x_user_info,
        utc_timestamp_slug,
    )


@dataclass(slots=True)
class SummaryPaths:
    json_path: Path
    markdown_path: Path


def _generate_run_id() -> str:
    return f"run-{utc_timestamp_slug()}-{uuid.uuid4().hex[:4]}"


def _coerce_float(value: Any) -> float | None:
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        text = value.strip()
        if not text:
            return None
        try:
            return float(text)
        except ValueError:
            return None
    return None


def _normalize_error_payload(value: Any) -> Any:
    if value is None:
        return None
    if isinstance(value, (dict, list)):
        return value
    if isinstance(value, str):
        text = value.strip()
        if not text:
            return None
        try:
            return json.loads(text)
        except json.JSONDecodeError:
            pass
        try:
            parsed = ast.literal_eval(text)
        except (ValueError, SyntaxError):
            return text
        return parsed if isinstance(parsed, (dict, list)) else text
    return value


def _parse_provider_output(raw_output: Any) -> dict[str, Any]:
    if isinstance(raw_output, dict):
        return raw_output
    if isinstance(raw_output, str):
        try:
            loaded = json.loads(raw_output)
        except json.JSONDecodeError:
            return {"final_answer": str(raw_output)}
        return loaded if isinstance(loaded, dict) else {"final_answer": str(raw_output)}
    return {"final_answer": ""}


def _component_result_summary(components: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], dict[str, Any] | None]:
    hard_results: list[dict[str, Any]] = []
    llm_result: dict[str, Any] | None = None
    for component in components:
        if not isinstance(component, dict):
            continue
        assertion = component.get("assertion")
        if isinstance(assertion, dict) and assertion.get("type") == "llm-rubric":
            llm_result = {
                "pass": bool(component.get("pass")),
                "score": component.get("score"),
                "reason": component.get("reason"),
            }
            continue
        hard_results.append(
            {
                "pass": bool(component.get("pass")),
                "score": component.get("score"),
                "reason": component.get("reason"),
            }
        )
    return hard_results, llm_result


def _error_code(error: Any) -> str:
    normalized = _normalize_error_payload(error)
    if isinstance(normalized, dict):
        nested_error = normalized.get("error") if isinstance(normalized.get("error"), dict) else {}
        return str(normalized.get("code") or nested_error.get("code") or "").strip()
    return ""


def _classify_error_type(*, error: Any, events: list[str]) -> str | None:
    normalized = _normalize_error_payload(error)
    code = _error_code(normalized).lower()
    event_set = {str(event).strip().lower() for event in events if str(event).strip()}

    if code == "timeout_error":
        return "timeout_error"
    if code == "request_error":
        return "request_error"
    if code in {"upstream_error", "internal_error"}:
        return "backend_error"
    if code in {"invalid_request_error", "authentication_error", "permission_error", "rate_limit_error"}:
        return "target_error"
    if "http_error" in event_set:
        return "target_error"
    if "max_turns_exceeded" in event_set:
        return "conversation_error"
    if "driver_error" in event_set:
        return "driver_error"
    if "terminal_error" in event_set:
        return "target_error"
    if isinstance(normalized, str):
        lowered = normalized.lower()
        if "timed out" in lowered or "timeout" in lowered:
            return "timeout_error"
        if "driver" in lowered:
            return "driver_error"
    if normalized is not None:
        return "target_error"
    return None


def _derive_case_status(
    *,
    item_success: bool,
    error: Any,
    events: list[str],
    hard_results: list[dict[str, Any]],
    judge_score: dict[str, Any],
) -> str:
    error_type = _classify_error_type(error=error, events=events)
    if error_type == "driver_error":
        return "error"
    if error is not None:
        return "failed"
    if not item_success:
        if any(not bool(result.get("pass")) for result in hard_results):
            return "failed"
        if str(judge_score.get("verdict") or "").strip().lower() == "fail":
            return "failed"
        return "error"
    return "passed"


def _build_case_snapshot(metadata: dict[str, Any], vars_map: dict[str, Any]) -> dict[str, Any]:
    case_path_raw = str(metadata.get("casePath") or "").strip()
    if case_path_raw:
        try:
            case = load_case(Path(case_path_raw))
        except Exception:
            case = None
        else:
            return {
                "id": case.case_id,
                "title": case.title,
                "enabled": case.enabled,
                "target_id": case.target_id,
                "skill_name": case.skill_name,
                "tags": case.tags,
                "entry_question": case.entry_question,
                "expected_mode": case.expected_mode,
                "conversation_script": [
                    {
                        "answer": step.answer,
                        "slot": step.slot,
                        "question_contains": step.question_contains or [],
                    }
                    for step in case.conversation_script
                ],
                "simulated_user_profile": case.simulated_user_profile,
                "judge_rubric": case.judge_rubric,
                "hard_assertions": case.hard_assertions,
                "body": case.body,
                "summary": case.summary,
                "path": case_path_raw,
            }

    target_payload = vars_map.get("target") if isinstance(vars_map.get("target"), dict) else {}
    return {
        "id": metadata.get("caseId") or vars_map.get("case_id"),
        "title": metadata.get("title") or vars_map.get("title"),
        "enabled": True,
        "target_id": metadata.get("targetId") or target_payload.get("id"),
        "skill_name": metadata.get("skillName"),
        "tags": metadata.get("tags") if isinstance(metadata.get("tags"), list) else [],
        "entry_question": vars_map.get("entry_question"),
        "expected_mode": vars_map.get("expected_mode"),
        "conversation_script": vars_map.get("conversation_script") if isinstance(vars_map.get("conversation_script"), list) else [],
        "simulated_user_profile": vars_map.get("simulated_user_profile") if isinstance(vars_map.get("simulated_user_profile"), dict) else {},
        "judge_rubric": metadata.get("judgeRubric"),
        "hard_assertions": json.loads(vars_map.get("hard_assertions_json") or "[]"),
        "body": metadata.get("bodyMarkdown") or "",
        "summary": metadata.get("bodyMarkdown") or "",
        "path": case_path_raw or None,
    }


def _extract_backend_session_ids(provider_payload: dict[str, Any]) -> list[str]:
    seen: list[str] = []
    top_level = str(provider_payload.get("session_id") or "").strip()
    if top_level:
        seen.append(top_level)
    for turn in provider_payload.get("transcript") or []:
        if not isinstance(turn, dict):
            continue
        session_id = str(turn.get("session_id") or "").strip()
        if session_id and session_id not in seen:
            seen.append(session_id)
    return seen


def _judge_with_model(
    *,
    judge_provider: JudgeProvider,
    metadata: dict[str, Any],
    provider_payload: dict[str, Any],
) -> dict[str, Any]:
    if provider_payload.get("error"):
        return {
            "score": 0,
            "verdict": "fail",
            "summary": "请求执行失败，未进入有效回答阶段。",
            "strengths": [],
            "issues": [str(provider_payload.get("error"))],
        }

    client = OpenAI(
        api_key=judge_provider.api_key,
        base_url=judge_provider.base_url,
    )
    prompt = {
        "case_id": metadata.get("caseId"),
        "title": metadata.get("title"),
        "skill_name": metadata.get("skillName"),
        "judge_rubric": metadata.get("judgeRubric"),
        "body_markdown": metadata.get("bodyMarkdown"),
        "provider_output": provider_payload,
    }
    completion = client.chat.completions.create(
        model=judge_provider.model,
        temperature=0,
        response_format={"type": "json_object"},
        messages=[
            {
                "role": "system",
                "content": (
                    "你是智能问数 agent 的评测裁判。"
                    "请根据输入返回 JSON，字段固定为 score(0-10整数), verdict(pass|warn|fail), summary, strengths, issues。"
                    "评分时重点看回答是否贴题、是否遵守澄清策略、是否有幻觉、是否能继续原任务。"
                ),
            },
            {
                "role": "user",
                "content": json.dumps(prompt, ensure_ascii=False),
            },
        ],
    )
    text = completion.choices[0].message.content or "{}"
    parsed = json.loads(text)
    return {
        "score": int(parsed.get("score", 0)),
        "verdict": str(parsed.get("verdict", "fail")),
        "summary": str(parsed.get("summary", "")).strip(),
        "strengths": [str(item) for item in parsed.get("strengths", []) if str(item).strip()],
        "issues": [str(item) for item in parsed.get("issues", []) if str(item).strip()],
    }


def _build_case_summary(item: dict[str, Any], judge_provider: JudgeProvider) -> dict[str, Any]:
    grading = item.get("gradingResult") if isinstance(item.get("gradingResult"), dict) else {}
    components = grading.get("componentResults") if isinstance(grading.get("componentResults"), list) else []
    hard_results, llm_result = _component_result_summary(components)
    metadata = item.get("metadata") if isinstance(item.get("metadata"), dict) else {}
    vars_map = item.get("vars") if isinstance(item.get("vars"), dict) else {}
    provider_payload = _parse_provider_output((item.get("response") or {}).get("output"))
    normalized_error = _normalize_error_payload(provider_payload.get("error"))
    provider_payload["error"] = normalized_error
    provider_evaluation = provider_payload.get("evaluation") if isinstance(provider_payload.get("evaluation"), dict) else None
    hard_assert_score = _coerce_float(metadata.get("hard_assert_score"))
    judge_score = (
        {
            "score": int(provider_evaluation.get("score", 0)),
            "verdict": str(provider_evaluation.get("verdict", "fail")),
            "summary": str(provider_evaluation.get("summary", "")).strip(),
            "strengths": [str(item) for item in provider_evaluation.get("strengths", []) if str(item).strip()],
            "issues": [str(item) for item in provider_evaluation.get("issues", []) if str(item).strip()],
        }
        if provider_evaluation is not None
        else _judge_with_model(
            judge_provider=judge_provider,
            metadata=metadata,
            provider_payload=provider_payload,
        )
    )
    if hard_assert_score is None:
        hard_assert_score = 0.0 if normalized_error else 1.0
    final_eval_score = _coerce_float(metadata.get("final_eval_score"))
    if final_eval_score is None:
        final_eval_score = round((0.4 * hard_assert_score) + (0.6 * (judge_score["score"] / 10.0)), 4)
    events = [str(event).strip() for event in (provider_payload.get("events") or []) if str(event).strip()]
    status = _derive_case_status(
        item_success=bool(item.get("success")),
        error=normalized_error,
        events=events,
        hard_results=hard_results,
        judge_score=judge_score,
    )
    error_type = _classify_error_type(error=normalized_error, events=events)
    backend_session_ids = _extract_backend_session_ids(provider_payload)
    return {
        "case_id": metadata.get("caseId") or (item.get("vars") or {}).get("case_id"),
        "target_id": metadata.get("targetId") or (item.get("vars") or {}).get("target_id"),
        "title": metadata.get("title") or (item.get("vars") or {}).get("title"),
        "environment": metadata.get("environment"),
        "skill_name": metadata.get("skillName"),
        "tags": metadata.get("tags") if isinstance(metadata.get("tags"), list) else [],
        "status": status,
        "success": bool(item.get("success")),
        "promptfoo_score": item.get("score"),
        "promptfoo_reason": grading.get("reason"),
        "hard_assertions": hard_results,
        "hard_assert_score": round(hard_assert_score, 4),
        "hard_assert": {
            "score": round(hard_assert_score, 4),
            "passed": all(bool(result.get("pass")) for result in hard_results) if hard_results else hard_assert_score >= 1.0,
            "details": hard_results,
        },
        "llm_assertion": llm_result,
        "judge_score": judge_score,
        "judge": judge_score,
        "final_eval_score": round(final_eval_score, 4),
        "final_score": round(final_eval_score, 4),
        "latency_ms": item.get("latencyMs"),
        "final_answer": provider_payload.get("final_answer", ""),
        "ask_count": provider_payload.get("ask_count"),
        "events": events,
        "transcript": provider_payload.get("transcript"),
        "simulated_user_trace": provider_payload.get("simulated_user_trace") if isinstance(provider_payload.get("simulated_user_trace"), list) else [],
        "unexpected_asks": provider_payload.get("unexpected_asks") if isinstance(provider_payload.get("unexpected_asks"), list) else [],
        "unused_script_steps": provider_payload.get("unused_script_steps") if isinstance(provider_payload.get("unused_script_steps"), list) else [],
        "runner_warnings": provider_payload.get("runner_warnings") if isinstance(provider_payload.get("runner_warnings"), list) else [],
        "request_payload": provider_payload.get("request_payload"),
        "response_payload": provider_payload.get("response_payload"),
        "request_payloads": provider_payload.get("request_payloads") if isinstance(provider_payload.get("request_payloads"), list) else [],
        "response_payloads": provider_payload.get("response_payloads") if isinstance(provider_payload.get("response_payloads"), list) else [],
        "backend_session_ids": backend_session_ids,
        "case_snapshot": _build_case_snapshot(metadata, vars_map),
        "error": normalized_error,
        "error_type": error_type,
    }


def _resolve_target_headers(target_id: str | None) -> dict[str, str]:
    if not target_id:
        return {}
    target = load_target(target_id)
    token = str(os.environ.get(target.auth_value_ref or "") or "").strip()
    headers: dict[str, str] = {}
    if target.auth_mode == "x_api_key" and token:
        headers["X-API-Key"] = token
    elif target.auth_mode == "bearer" and token:
        headers["Authorization"] = f"Bearer {token}"
    x_user_info = serialize_x_user_info(target.identity)
    if x_user_info:
        headers["x-user-info"] = x_user_info
    return headers


def _fetch_backend_trace(*, target_id: str | None, session_id: str) -> dict[str, Any]:
    if not target_id or not session_id:
        return {}
    target = load_target(target_id)
    headers = _resolve_target_headers(target_id)
    request = Request(
        f"{target.base_url}/v1/sessions/{quote(session_id)}/response-turns?limit=50",
        headers=headers,
        method="GET",
    )
    with urlopen(request, timeout=60) as response:
        body = response.read().decode("utf-8")
        return json.loads(body) if body else {}


def _render_markdown(summary: dict[str, Any]) -> str:
    lines = [
        f"# Run Summary - {summary['run_id']}",
        "",
        f"- Run ID: `{summary['run_id']}`",
        f"- Promptfoo Eval ID: `{summary['promptfoo_eval_id']}`",
        f"- Generated at: `{summary['generated_at']}`",
        f"- Promptfoo success/failure/error: `{summary['stats']['successes']}/{summary['stats']['failures']}/{summary['stats']['errors']}`",
        "",
        "| Case | Promptfoo | Judge | Verdict |",
        "| --- | --- | --- | --- |",
    ]
    for case in summary["cases"]:
        lines.append(
            f"| `{case['case_id']}` | `{case['final_eval_score']}` | `{case['judge_score']['score']}/10` | `{case['judge_score']['verdict']}` |"
        )
    for case in summary["cases"]:
        lines.extend(
            [
                "",
                f"## {case['title']} (`{case['case_id']}`)",
                "",
                f"- Status: `{case['status']}`",
                f"- Environment: `{case['environment']}`",
                f"- Skill: `{case['skill_name']}`",
                f"- FinalEval: `{case['final_score']}`",
                f"- HardAssert: `{case['hard_assert_score']}`",
                f"- Promptfoo: `{'PASS' if case['success'] else 'FAIL'}` score=`{case['promptfoo_score']}`",
                f"- Judge: `{case['judge']['score']}/10` verdict=`{case['judge']['verdict']}`",
                f"- Judge Summary: {case['judge']['summary'] or 'N/A'}",
                f"- Ask Count: `{case['ask_count']}`",
                f"- Latency: `{case['latency_ms']}` ms",
            ]
        )
        if case["judge"]["strengths"]:
            lines.append("- Strengths: " + "；".join(case["judge"]["strengths"]))
        if case["judge"]["issues"]:
            lines.append("- Issues: " + "；".join(case["judge"]["issues"]))
        if case["error_type"]:
            lines.append(f"- Error Type: `{case['error_type']}`")
        if case["error"]:
            lines.append(f"- Error: `{json.dumps(case['error'], ensure_ascii=False)}`")
        lines.extend(["", "### Final Answer", "", case["final_answer"] or "_Empty_", ""])
    return "\n".join(lines).strip() + "\n"


def generate_summary(
    *,
    raw_eval_path: Path,
    judge_provider: JudgeProvider,
    filters: dict[str, Any] | None = None,
) -> SummaryPaths:
    if not raw_eval_path.exists():
        raise EvalConfigError(f"raw eval file not found: {raw_eval_path}")
    raw = json.loads(raw_eval_path.read_text(encoding="utf-8"))
    results_block = raw.get("results") if isinstance(raw.get("results"), dict) else {}
    case_results = results_block.get("results") if isinstance(results_block.get("results"), list) else []
    stats = results_block.get("stats") if isinstance(results_block.get("stats"), dict) else {}
    built_cases = [_build_case_summary(item, judge_provider) for item in case_results if isinstance(item, dict)]
    summary = {
        "run_id": _generate_run_id(),
        "promptfoo_eval_id": raw.get("evalId") or f"local-{utc_timestamp_slug()}",
        "generated_at": utc_timestamp_slug(),
        "target_id": next(
            (
                case.get("target_id")
                for case in built_cases
                if isinstance(case, dict) and str(case.get("target_id") or "").strip()
            ),
            None,
        ),
        "stats": {
            "successes": stats.get("successes", 0),
            "failures": stats.get("failures", 0),
            "errors": stats.get("errors", 0),
        },
    }
    summary["cases"] = built_cases
    summary["case_count"] = len(summary["cases"])
    summary["status_counts"] = {
        "passed": sum(1 for case in summary["cases"] if case.get("status") == "passed"),
        "failed": sum(1 for case in summary["cases"] if case.get("status") == "failed"),
        "error": sum(1 for case in summary["cases"] if case.get("status") == "error"),
    }
    judge_values = [float(case["judge"]["score"]) for case in summary["cases"] if _coerce_float(case.get("judge", {}).get("score")) is not None]
    final_values = [float(case["final_score"]) for case in summary["cases"] if _coerce_float(case.get("final_score")) is not None]
    summary["judge_avg"] = round(sum(judge_values) / len(judge_values), 2) if judge_values else None
    summary["final_avg"] = round(sum(final_values) / len(final_values), 4) if final_values else None
    archive_dir = DEFAULT_RUNS_DIR / str(summary["run_id"])
    ensure_directory(archive_dir)
    results_dir = ensure_directory(archive_dir / "results")
    backend_dir = ensure_directory(archive_dir / "backend")
    shutil.copyfile(raw_eval_path, archive_dir / "eval.json")
    raw_metadata = raw.get("metadata") if isinstance(raw.get("metadata"), dict) else {}
    started_at = raw_metadata.get("evaluationCreatedAt")
    completed_at = raw_metadata.get("exportedAt")
    target_id = summary.get("target_id")
    run_status = (
        "completed_with_errors"
        if summary["status_counts"]["error"] or summary["stats"]["errors"]
        else "completed_with_failures"
        if summary["status_counts"]["failed"] or summary["stats"]["failures"]
        else "completed"
    )
    summary["started_at"] = started_at
    summary["completed_at"] = completed_at or summary["generated_at"]
    summary["filters"] = filters or {}
    summary["status"] = run_status
    ensure_directory(DEFAULT_SUMMARY_JSON_FILE.parent)
    DEFAULT_SUMMARY_JSON_FILE.write_text(
        json.dumps(summary, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    DEFAULT_SUMMARY_MD_FILE.write_text(_render_markdown(summary), encoding="utf-8")
    (archive_dir / "run.json").write_text(
        json.dumps(
            {
                "run_id": summary["run_id"],
                "promptfoo_eval_id": summary["promptfoo_eval_id"],
                "target_id": target_id,
                "started_at": started_at,
                "completed_at": completed_at or summary["generated_at"],
                "generated_at": summary["generated_at"],
                "status": run_status,
                "filters": filters or {},
                "case_count": len(summary["cases"]),
                "stats": summary["stats"],
                "status_counts": summary["status_counts"],
                "judge_avg": summary["judge_avg"],
                "final_avg": summary["final_avg"],
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )
    (archive_dir / "summary.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    (archive_dir / "summary.md").write_text(_render_markdown(summary), encoding="utf-8")
    for case in summary["cases"]:
        case_id = str(case.get("case_id") or "unknown")
        (results_dir / f"{case_id}.json").write_text(
            json.dumps(case, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        session_ids = {
            str(session_id).strip()
            for session_id in (case.get("backend_session_ids") or [])
            if str(session_id).strip()
        }
        for session_id in session_ids:
            try:
                backend_trace = _fetch_backend_trace(
                    target_id=str(case.get("target_id") or "").strip() or None,
                    session_id=session_id,
                )
            except Exception as exc:
                backend_trace = {"error": str(exc)}
            (backend_dir / f"{session_id}.json").write_text(
                json.dumps(backend_trace, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
    return SummaryPaths(
        json_path=DEFAULT_SUMMARY_JSON_FILE,
        markdown_path=DEFAULT_SUMMARY_MD_FILE,
    )
