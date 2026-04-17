from __future__ import annotations

from dataclasses import asdict
from datetime import datetime
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
import signal
from pathlib import Path
import subprocess
import threading
import time
from typing import Any
from urllib.parse import parse_qs, quote, urlparse
from urllib.request import Request, urlopen

try:
    from eval.engine.common import (
        BASE_DIR,
        DEFAULT_ACTIVE_RUN_PROGRESS_FILE,
        DEFAULT_CASES_DIR,
        DEFAULT_LIVE_CASE_FILE,
        DEFAULT_LIVE_RUN_FILE,
        DEFAULT_RAW_EVAL_FILE,
        DEFAULT_RUNS_DIR,
        DEFAULT_SUMMARY_JSON_FILE,
        EvalConfigError,
        load_app_config,
        load_case,
        load_cases,
        load_targets,
        read_active_run_progress,
        serialize_x_user_info,
        write_active_run_progress,
    )
except ImportError:  # pragma: no cover - direct script entry path
    from eval.engine.common import (
        BASE_DIR,
        DEFAULT_ACTIVE_RUN_PROGRESS_FILE,
        DEFAULT_CASES_DIR,
        DEFAULT_LIVE_CASE_FILE,
        DEFAULT_LIVE_RUN_FILE,
        DEFAULT_RAW_EVAL_FILE,
        DEFAULT_RUNS_DIR,
        DEFAULT_SUMMARY_JSON_FILE,
        EvalConfigError,
        load_app_config,
        load_case,
        load_cases,
        load_targets,
        read_active_run_progress,
        serialize_x_user_info,
        write_active_run_progress,
    )


STATIC_DIR = Path(__file__).resolve().parents[1] / "web" / "static"


class EvalRunState:
    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.running = False
        self.started_at: float | None = None
        self.completed_at: float | None = None
        self.exit_code: int | None = None
        self.command: list[str] = []
        self.lines: list[str] = []

    def start(self, command: list[str]) -> bool:
        with self.lock:
            if self.running:
                return False
            self.running = True
            self.started_at = time.time()
            self.completed_at = None
            self.exit_code = None
            self.command = command
            self.lines = []
            return True

    def append(self, line: str) -> None:
        with self.lock:
            self.lines.append(line)
            if len(self.lines) > 2000:
                self.lines = self.lines[-2000:]

    def finish(self, exit_code: int) -> None:
        with self.lock:
            self.running = False
            self.completed_at = time.time()
            self.exit_code = exit_code

    def snapshot(self) -> dict[str, Any]:
        with self.lock:
            return {
                "running": self.running,
                "started_at": self.started_at,
                "completed_at": self.completed_at,
                "exit_code": self.exit_code,
                "command": self.command,
                "output": "".join(self.lines),
            }


RUN_STATE = EvalRunState()


def _json_load_file(path: Path, default: Any) -> Any:
    if not path.exists():
        return default
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return default


def _safe_float(value: Any) -> float | None:
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


def _duration_ms(started_at: Any, completed_at: Any) -> int | None:
    if not isinstance(started_at, str) or not isinstance(completed_at, str):
        return None
    try:
        start_dt = datetime.fromisoformat(started_at.replace("Z", "+00:00"))
        end_dt = datetime.fromisoformat(completed_at.replace("Z", "+00:00"))
    except ValueError:
        return None
    return max(0, int((end_dt - start_dt).total_seconds() * 1000))


def _case_to_payload(path: Path) -> dict[str, Any]:
    case = load_case(path)
    relative = path.relative_to(BASE_DIR)
    parent_name = path.parent.name.lower()
    return {
        "path": str(relative),
        "id": case.case_id,
        "target_id": case.target_id,
        "title": case.title,
        "enabled": case.enabled,
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
        "judge_rubric": case.judge_rubric,
        "hard_assertions": case.hard_assertions,
        "summary": case.summary,
        "body": case.body,
        "source": parent_name,
    }


def list_case_payloads(target_id: str | None = None) -> list[dict[str, Any]]:
    cases: list[dict[str, Any]] = []
    scan_dir = DEFAULT_CASES_DIR / target_id if target_id else DEFAULT_CASES_DIR
    if not scan_dir.exists():
        return cases
    for path in sorted(scan_dir.rglob("*.md")):
        try:
            cases.append(_case_to_payload(path))
        except EvalConfigError as exc:
            cases.append(
                {
                    "path": str(path.relative_to(BASE_DIR)),
                    "id": path.stem,
                    "target_id": target_id,
                    "title": path.stem,
                    "enabled": False,
                    "skill_name": "",
                    "tags": ["invalid"],
                    "entry_question": "",
                    "expected_mode": "",
                    "conversation_script": [],
                    "judge_rubric": "",
                    "hard_assertions": [],
                    "summary": str(exc),
                    "body": "",
                    "source": path.parent.name.lower(),
                    "error": str(exc),
                }
            )
    return cases


def _run_summary_payload(summary: dict[str, Any], run_id: str, run_meta: dict[str, Any] | None = None) -> dict[str, Any]:
    run_meta = run_meta or {}
    cases = summary.get("cases") if isinstance(summary.get("cases"), list) else []
    judge_scores = [
        float((case.get("judge") or case.get("judge_score") or {}).get("score"))
        for case in cases
        if isinstance(case, dict) and _safe_float((case.get("judge") or case.get("judge_score") or {}).get("score")) is not None
    ]
    final_scores = [
        float(case.get("final_score") if case.get("final_score") is not None else case.get("final_eval_score"))
        for case in cases
        if isinstance(case, dict)
        and _safe_float(case.get("final_score") if case.get("final_score") is not None else case.get("final_eval_score")) is not None
    ]
    stats = summary.get("stats") if isinstance(summary.get("stats"), dict) else {}
    started_at = run_meta.get("started_at") or summary.get("started_at")
    completed_at = run_meta.get("completed_at") or summary.get("completed_at")
    status_counts = summary.get("status_counts") if isinstance(summary.get("status_counts"), dict) else run_meta.get("status_counts")
    if not isinstance(status_counts, dict):
        status_counts = {
            "passed": sum(1 for case in cases if isinstance(case, dict) and case.get("status") == "passed"),
            "failed": sum(1 for case in cases if isinstance(case, dict) and case.get("status") == "failed"),
            "error": sum(1 for case in cases if isinstance(case, dict) and case.get("status") == "error"),
        }
    return {
        "run_id": summary.get("run_id") or run_meta.get("run_id") or run_id,
        "promptfoo_eval_id": summary.get("promptfoo_eval_id") or summary.get("eval_id") or run_meta.get("promptfoo_eval_id"),
        "target_id": summary.get("target_id")
        or run_meta.get("target_id")
        or next(
            (
                case.get("target_id")
                for case in cases
                if isinstance(case, dict) and str(case.get("target_id") or "").strip()
            ),
            None,
        ),
        "generated_at": summary.get("generated_at"),
        "started_at": started_at,
        "completed_at": completed_at,
        "duration_ms": run_meta.get("duration_ms") or _duration_ms(started_at, completed_at),
        "status": summary.get("status") or run_meta.get("status") or "completed",
        "filters": summary.get("filters") if isinstance(summary.get("filters"), dict) else run_meta.get("filters") if isinstance(run_meta.get("filters"), dict) else {},
        "case_count": int(summary.get("case_count") or len(cases)),
        "stats": stats,
        "status_counts": status_counts,
        "judge_avg": summary.get("judge_avg") if _safe_float(summary.get("judge_avg")) is not None else round(sum(judge_scores) / len(judge_scores), 2) if judge_scores else None,
        "final_avg": summary.get("final_avg") if _safe_float(summary.get("final_avg")) is not None else round(sum(final_scores) / len(final_scores), 4) if final_scores else None,
    }


def list_eval_runs(target_id: str | None = None) -> list[dict[str, Any]]:
    if not DEFAULT_RUNS_DIR.exists():
        return []

    runs: list[dict[str, Any]] = []
    for run_dir in sorted(DEFAULT_RUNS_DIR.iterdir(), reverse=True):
        if not run_dir.is_dir():
            continue
        summary = _json_load_file(run_dir / "summary.json", None)
        if not isinstance(summary, dict) or not summary:
            continue
        run_meta = _json_load_file(run_dir / "run.json", {})
        payload = _run_summary_payload(summary, run_dir.name, run_meta)
        if target_id and payload.get("target_id") != target_id:
            continue
        runs.append(payload)
    return runs


def list_targets_payload() -> list[dict[str, Any]]:
    runs = list_eval_runs()
    runs_by_target: dict[str, int] = {}
    for run in runs:
        target_id = str(run.get("target_id") or "").strip()
        if target_id:
            runs_by_target[target_id] = runs_by_target.get(target_id, 0) + 1

    payloads: list[dict[str, Any]] = []
    for target in load_targets().values():
        cases = list_case_payloads(target.id)
        payload = asdict(target)
        payload.update(
            {
                "case_count": len(cases),
                "run_count": runs_by_target.get(target.id, 0),
            }
        )
        payloads.append(payload)
    return payloads


def load_run_detail(run_id: str) -> dict[str, Any]:
    run_dir = DEFAULT_RUNS_DIR / run_id
    summary = _json_load_file(run_dir / "summary.json", {"cases": [], "stats": {}, "eval_id": run_id})
    run_meta = _json_load_file(run_dir / "run.json", {})
    payload = _run_summary_payload(summary, run_id, run_meta)
    payload["cases"] = summary.get("cases") if isinstance(summary.get("cases"), list) else []
    payload["log"] = load_run_log(run_id)
    return payload


def load_run_case_result(run_id: str, case_id: str) -> dict[str, Any]:
    run_dir = DEFAULT_RUNS_DIR / run_id
    result_path = run_dir / "results" / f"{case_id}.json"
    result = _json_load_file(result_path, None)
    if isinstance(result, dict) and result:
        return result

    summary = load_run_detail(run_id)
    case_summary = next(
        (
            case
            for case in summary.get("cases", [])
            if isinstance(case, dict) and str(case.get("case_id") or "") == case_id
        ),
        None,
    )
    return case_summary if isinstance(case_summary, dict) else {}


def load_run_backend_trace(run_id: str, session_id: str) -> dict[str, Any]:
    path = DEFAULT_RUNS_DIR / run_id / "backend" / f"{session_id}.json"
    return _json_load_file(path, {})


def load_run_log(run_id: str) -> str:
    path = DEFAULT_RUNS_DIR / run_id / "logs" / "runner.log"
    if not path.exists():
        return ""
    return path.read_text(encoding="utf-8")


def _generate_local_token_if_needed(target_id: str) -> str | None:
    if os.getenv("VERIFY_AUTH_TOKEN"):
        return None
    if target_id not in {"test", load_app_config().default_target_id}:
        return None
    script = BASE_DIR.parent / "scripts" / "generate_ibe_token.sh"
    if not script.exists():
        return None
    try:
        token = subprocess.check_output(
            [str(script), "-e", "local"],
            cwd=str(BASE_DIR.parent),
            text=True,
            stderr=subprocess.DEVNULL,
            timeout=20,
        ).strip()
    except Exception:
        return None
    return token or None


def _eval_auth_headers(target_id: str) -> dict[str, str]:
    target = load_targets().get(target_id)
    if target is None:
        return {}
    token = os.getenv(target.auth_value_ref or "") or _generate_local_token_if_needed(target.id) or ""
    headers: dict[str, str] = {}
    if target.auth_mode == "x_api_key" and token:
        headers["X-API-Key"] = token
    elif target.auth_mode == "bearer" and token:
        headers["Authorization"] = f"Bearer {token}"
    x_user_info = serialize_x_user_info(target.identity)
    if x_user_info:
        headers["x-user-info"] = x_user_info
    return headers


def _http_get_json(url: str, headers: dict[str, str]) -> Any:
    request = Request(url=url, headers=headers, method="GET")
    with urlopen(request, timeout=60) as response:
        body = response.read().decode("utf-8")
        return json.loads(body) if body else None


def backend_sessions_payload(target_id: str) -> dict[str, Any]:
    target = load_targets().get(target_id)
    if target is None:
        return {"sessions": [], "error": f"target not found: {target_id}"}
    try:
        sessions = _http_get_json(f"{target.base_url}/v1/sessions", _eval_auth_headers(target_id))
    except Exception as exc:
        return {"sessions": [], "error": str(exc)}
    return {"sessions": sessions if isinstance(sessions, list) else []}


def backend_turns_payload(session_id: str, target_id: str) -> dict[str, Any]:
    target = load_targets().get(target_id)
    if target is None:
        return {"turns": [], "error": f"target not found: {target_id}"}
    try:
        data = _http_get_json(
            f"{target.base_url}/v1/sessions/{quote(session_id)}/response-turns?limit=50",
            _eval_auth_headers(target_id),
        )
    except Exception as exc:
        return {"turns": [], "error": str(exc)}
    return data if isinstance(data, dict) else {"turns": []}


def _archive_runner_log_from_latest(output: str) -> None:
    latest = _json_load_file(DEFAULT_SUMMARY_JSON_FILE, {})
    eval_id = str(latest.get("eval_id") or "").strip()
    if not eval_id:
        return
    logs_dir = DEFAULT_RUNS_DIR / eval_id / "logs"
    logs_dir.mkdir(parents=True, exist_ok=True)
    (logs_dir / "runner.log").write_text(output, encoding="utf-8")


def _write_json_file(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def _write_live_run_state(payload: dict[str, Any]) -> None:
    _write_json_file(DEFAULT_LIVE_RUN_FILE, payload)


def _merge_live_run_state(patch: dict[str, Any]) -> None:
    current = _json_load_file(DEFAULT_LIVE_RUN_FILE, {})
    if not isinstance(current, dict):
        current = {}
    current.update(patch)
    current["updated_at"] = time.time()
    _write_live_run_state(current)


def _write_live_case_state(payload: dict[str, Any]) -> None:
    _write_json_file(DEFAULT_LIVE_CASE_FILE, payload)


def _write_live_progress(payload: dict[str, Any]) -> None:
    run_payload = {key: value for key, value in payload.items() if key != "live_case"}
    live_case_payload = payload.get("live_case") if isinstance(payload.get("live_case"), dict) else {}
    _write_live_run_state(run_payload)
    _write_live_case_state(live_case_payload)


def live_progress_payload() -> dict[str, Any]:
    run_payload = _json_load_file(DEFAULT_LIVE_RUN_FILE, {})
    case_payload = _json_load_file(DEFAULT_LIVE_CASE_FILE, {})
    if not isinstance(run_payload, dict):
        run_payload = {}
    if not isinstance(case_payload, dict):
        case_payload = {}
    return {**run_payload, "live_case": case_payload}


def _pid_is_alive(pid: int | None) -> bool:
    if not isinstance(pid, int) or pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except OSError:
        return False
    return True


def current_run_payload() -> dict[str, Any]:
    snapshot = RUN_STATE.snapshot()
    live = live_progress_payload()
    live_pid = live.get("pid")
    live_running = bool(live.get("running")) and _pid_is_alive(live_pid if isinstance(live_pid, int) else None)
    if snapshot.get("running"):
        if not snapshot.get("output") and live.get("stdout"):
            snapshot["output"] = str(live.get("stdout") or "")
        return snapshot
    if bool(live.get("running")) and not live_running:
        _merge_live_run_state({"running": False, "completed_at": live.get("completed_at") or time.time()})
        _write_live_case_state({})
        live = live_progress_payload()
    if live_running:
        return {
            "running": True,
            "started_at": live.get("started_at"),
            "completed_at": live.get("completed_at"),
            "exit_code": live.get("exit_code"),
            "command": live.get("command") if isinstance(live.get("command"), list) else [],
            "output": str(live.get("stdout") or ""),
        }
    if snapshot.get("command") or snapshot.get("completed_at") or snapshot.get("exit_code") is not None:
        return snapshot
    return {
        "running": False,
        "started_at": live.get("started_at"),
        "completed_at": live.get("completed_at"),
        "exit_code": live.get("exit_code"),
        "command": live.get("command") if isinstance(live.get("command"), list) else [],
        "output": str(live.get("stdout") or ""),
    }


def _run_scope_payload(filters: dict[str, Any]) -> dict[str, Any]:
    case_pattern = str(filters.get("case") or "").strip() or None
    tag = str(filters.get("tag") or "").strip() or None
    skill = str(filters.get("skill") or "").strip() or None
    if case_pattern:
        return {"kind": "case", "label": f"单 Case 调试 · {case_pattern}", "case": case_pattern}
    if tag:
        return {"kind": "tag", "label": f"批次 · tag={tag}", "tag": tag}
    if skill:
        return {"kind": "skill", "label": f"批次 · skill={skill}", "skill": skill}
    return {"kind": "full", "label": "批次 · 当前 Target 全量"}


def _compute_active_case_evaluation(
    *,
    case_snapshot: dict[str, Any],
    completed_case: dict[str, Any],
) -> dict[str, Any]:
    try:
        from .provider import _active_evaluation
    except ImportError:  # pragma: no cover - direct script entry path
        from provider import _active_evaluation

    return _active_evaluation(
        provider_payload={
            "final_answer": completed_case.get("final_answer") or "",
            "ask_count": completed_case.get("ask_count") or 0,
            "error": completed_case.get("error"),
            "transcript": completed_case.get("transcript") if isinstance(completed_case.get("transcript"), list) else [],
            "events": completed_case.get("events") if isinstance(completed_case.get("events"), list) else [],
        },
        context_vars={
            "case_id": case_snapshot.get("case_id"),
            "title": case_snapshot.get("title"),
            "entry_question": case_snapshot.get("entry_question"),
            "skill_name": case_snapshot.get("skill_name"),
            "judge_rubric": case_snapshot.get("judge_rubric"),
            "hard_assertions": case_snapshot.get("hard_assertions"),
        },
    )


def active_run_payload(target_id: str | None = None) -> dict[str, Any]:
    live = live_progress_payload()
    run_state = current_run_payload()
    progress_state = read_active_run_progress()
    filters = live.get("filters") if isinstance(live.get("filters"), dict) else {}
    active_target_id = str(progress_state.get("target_id") or live.get("target_id") or "").strip()
    if target_id and active_target_id and active_target_id != target_id:
        return {"running": False}
    if not run_state.get("running") or not active_target_id:
        return {"running": False}

    planned_cases = load_cases(
        DEFAULT_CASES_DIR,
        target_id=active_target_id,
        case_pattern=str(filters.get("case") or "").strip() or None,
        tag=str(filters.get("tag") or "").strip() or None,
        skill_name=str(filters.get("skill") or "").strip() or None,
    )
    case_map = {case.case_id: case for case in planned_cases}
    planned_case_entries = progress_state.get("planned_cases") if isinstance(progress_state.get("planned_cases"), list) else []
    if not planned_case_entries:
        planned_case_entries = [
            {
                "case_id": case.case_id,
                "title": case.title,
                "entry_question": case.entry_question,
                "skill_name": case.skill_name,
                "judge_rubric": case.judge_rubric,
                "hard_assertions": case.hard_assertions,
            }
            for case in planned_cases
        ]
    completed_cases = progress_state.get("completed_cases") if isinstance(progress_state.get("completed_cases"), dict) else {}
    completed_changed = False
    for case_id, completed_case in list(completed_cases.items()):
        if not isinstance(completed_case, dict):
            continue
        if completed_case.get("judge_score") is not None and completed_case.get("final_score") is not None:
            continue
        case_obj = case_map.get(case_id)
        if case_obj is None:
            continue
        evaluation = _compute_active_case_evaluation(
            case_snapshot={
                "case_id": case_obj.case_id,
                "title": case_obj.title,
                "entry_question": case_obj.entry_question,
                "skill_name": case_obj.skill_name,
                "judge_rubric": case_obj.judge_rubric,
                "hard_assertions": case_obj.hard_assertions,
            },
            completed_case=completed_case,
        )
        completed_case.update(evaluation)
        completed_cases[case_id] = completed_case
        completed_changed = True
    if completed_changed:
        progress_state["completed_cases"] = completed_cases
        write_active_run_progress(progress_state)
    planned_case_count = int(progress_state.get("planned_case_count") or live.get("planned_case_count") or len(planned_case_entries) or 0)
    live_case = live.get("live_case") if isinstance(live.get("live_case"), dict) else {}
    live_case_id = str(live_case.get("case_id") or "").strip()
    current_index = next(
        (index + 1 for index, case in enumerate(planned_case_entries) if str(case.get("case_id") or "").strip() == live_case_id),
        None,
    )
    current_case = {
        "case_id": live_case_id or None,
        "title": live_case.get("title"),
        "entry_question": live_case.get("entry_question"),
        "status": live_case.get("status"),
        "turn_index": live_case.get("turn_index"),
        "ask_count": live_case.get("ask_count"),
        "last_user_reply": live_case.get("last_user_reply"),
        "stream_text": live_case.get("stream_text"),
        "stream_events": live_case.get("stream_events")
        if isinstance(live_case.get("stream_events"), list)
        else [],
        "session_id": live_case.get("session_id"),
        "error": live_case.get("error"),
    }
    cases: list[dict[str, Any]] = []
    for index, case in enumerate(planned_case_entries, start=1):
        case_id = str(case.get("case_id") or "").strip()
        completed_case = completed_cases.get(case_id) if isinstance(completed_cases.get(case_id), dict) else {}
        merged_case = {
            "case_id": case_id,
            "title": case.get("title"),
            "entry_question": case.get("entry_question"),
            "skill_name": case.get("skill_name"),
            "index": index,
            "status": "pending",
            "final_answer": "",
            "ask_count": None,
            "error": None,
            "transcript": [],
        }
        if completed_case:
            for key, value in completed_case.items():
                if value is None and key in merged_case:
                    continue
                merged_case[key] = value
        if live_case_id and case_id == live_case_id:
            merged_case.update(current_case)
            merged_case["status"] = "running"
        cases.append(merged_case)
    return {
        "running": True,
        "synthetic_run_id": "__active__",
        "run_id": "__active__",
        "target_id": active_target_id,
        "started_at": progress_state.get("started_at") or live.get("started_at") or run_state.get("started_at"),
        "completed_at": live.get("completed_at") or run_state.get("completed_at"),
        "command": run_state.get("command") if isinstance(run_state.get("command"), list) else [],
        "stdout_tail": str(run_state.get("output") or live.get("stdout") or ""),
        "scope": progress_state.get("scope") if isinstance(progress_state.get("scope"), dict) else _run_scope_payload(filters),
        "planned_case_count": planned_case_count,
        "current_index": current_index,
        "current_case": current_case,
        "cases": cases,
        "completed_case_summaries": [case for case in cases if case.get("status") in {"completed", "failed"}],
        "concurrency": 1,
        "error": current_case.get("error"),
    }


def _run_eval_command(payload: dict[str, Any]) -> None:
    target_id = str(payload.get("target") or "").strip() or load_app_config().default_target_id
    case_pattern = str(payload.get("case") or "").strip() or None
    tag = str(payload.get("tag") or "").strip() or None
    skill = str(payload.get("skill") or "").strip() or None
    planned_cases = load_cases(
        DEFAULT_CASES_DIR,
        target_id=target_id,
        case_pattern=case_pattern,
        tag=tag,
        skill_name=skill,
    )
    command = [
        str(BASE_DIR.parent / ".venv" / "bin" / "python"),
        "-u",
        str(BASE_DIR / "eval_cli.py"),
        "run",
        "--target",
        target_id,
    ]
    for key, flag in (("case", "--case"), ("tag", "--tag"), ("skill", "--skill")):
        value = str(payload.get(key) or "").strip()
        if value:
            command.extend([flag, value])
    if payload.get("refreshGenerated"):
        command.append("--refresh-generated")

    if not RUN_STATE.start(command):
        return
    _write_live_progress(
        {
            "running": True,
            "started_at": time.time(),
            "target_id": target_id,
            "command": command,
            "planned_case_count": len(planned_cases),
            "filters": {
                "case": case_pattern,
                "tag": tag,
                "skill": skill,
            },
            "live_case": {},
            "updated_at": time.time(),
        }
    )
    write_active_run_progress(
        {
            "running": True,
            "target_id": target_id,
            "started_at": time.time(),
            "filters": {
                "case": case_pattern,
                "tag": tag,
                "skill": skill,
            },
            "scope": _run_scope_payload(
                {
                    "case": case_pattern,
                    "tag": tag,
                    "skill": skill,
                }
            ),
            "planned_case_count": len(planned_cases),
            "planned_cases": [
                {
                    "case_id": case.case_id,
                    "title": case.title,
                    "entry_question": case.entry_question,
                    "skill_name": case.skill_name,
                }
                for case in planned_cases
            ],
            "completed_cases": {},
        }
    )

    run_env = os.environ.copy()
    generated_token = _generate_local_token_if_needed(target_id)
    if generated_token:
        run_env["VERIFY_AUTH_TOKEN"] = generated_token
    run_env["PYTHONUNBUFFERED"] = "1"

    process = subprocess.Popen(
        command,
        cwd=str(BASE_DIR.parent),
        env=run_env,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        bufsize=1,
    )
    _merge_live_run_state({"pid": process.pid})
    assert process.stdout is not None
    for line in process.stdout:
        RUN_STATE.append(line)
        _merge_live_run_state({"stdout": RUN_STATE.snapshot()["output"], "running": True})
    exit_code = process.wait()
    _archive_runner_log_from_latest(RUN_STATE.snapshot()["output"])
    RUN_STATE.finish(exit_code)
    _write_live_case_state({})
    _merge_live_run_state(
        {
            "running": False,
            "completed_at": time.time(),
            "exit_code": exit_code,
            "stdout": RUN_STATE.snapshot()["output"],
        }
    )
    write_active_run_progress(
        {
            "running": False,
            "target_id": target_id,
            "started_at": read_active_run_progress().get("started_at"),
            "completed_at": time.time(),
            "exit_code": exit_code,
            "filters": read_active_run_progress().get("filters") if isinstance(read_active_run_progress().get("filters"), dict) else {},
            "scope": read_active_run_progress().get("scope") if isinstance(read_active_run_progress().get("scope"), dict) else {},
            "planned_case_count": read_active_run_progress().get("planned_case_count"),
            "planned_cases": read_active_run_progress().get("planned_cases") if isinstance(read_active_run_progress().get("planned_cases"), list) else [],
            "completed_cases": read_active_run_progress().get("completed_cases") if isinstance(read_active_run_progress().get("completed_cases"), dict) else {},
        }
    )


def _static_content(path: str) -> tuple[bytes, str] | None:
    relative = path.lstrip("/")
    if not relative or relative == "index.html":
        relative = "index.html"
    candidate = (STATIC_DIR / relative).resolve()
    try:
        candidate.relative_to(STATIC_DIR.resolve())
    except ValueError:
        return None
    if not candidate.exists() or not candidate.is_file():
        return None
    content_type = {
        ".html": "text/html; charset=utf-8",
        ".css": "text/css; charset=utf-8",
        ".js": "application/javascript; charset=utf-8",
        ".json": "application/json; charset=utf-8",
    }.get(candidate.suffix, "application/octet-stream")
    return candidate.read_bytes(), content_type


class DashboardHandler(BaseHTTPRequestHandler):
    def log_message(self, fmt: str, *args: Any) -> None:
        return

    def _send_json(self, payload: Any, status: HTTPStatus = HTTPStatus.OK) -> None:
        raw = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(int(status))
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def _send_static(self, path: str) -> None:
        payload = _static_content(path)
        if payload is None:
            self._send_json({"error": "not found"}, HTTPStatus.NOT_FOUND)
            return
        raw, content_type = payload
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def _read_json_body(self) -> dict[str, Any]:
        length = int(self.headers.get("Content-Length") or 0)
        if length <= 0:
            return {}
        raw = self.rfile.read(length).decode("utf-8")
        try:
            payload = json.loads(raw)
        except json.JSONDecodeError:
            return {}
        return payload if isinstance(payload, dict) else {}

    def do_GET(self) -> None:
        parsed = urlparse(self.path)
        if parsed.path == "/":
            self._send_static("index.html")
            return
        if parsed.path.startswith("/static/"):
            self._send_static(parsed.path.removeprefix("/static/"))
            return

        if parsed.path == "/api/targets":
            app = load_app_config()
            self._send_json(
                {
                    "default_target_id": app.default_target_id,
                    "targets": list_targets_payload(),
                }
            )
            return

        if parsed.path.startswith("/api/targets/"):
            suffix = parsed.path.removeprefix("/api/targets/").strip("/")
            parts = [part for part in suffix.split("/") if part]
            if len(parts) == 1:
                target = next((item for item in list_targets_payload() if item["id"] == parts[0]), None)
                if target is None:
                    self._send_json({"error": "target not found"}, HTTPStatus.NOT_FOUND)
                    return
                self._send_json(target)
                return
            if len(parts) == 2 and parts[1] == "cases":
                self._send_json({"cases": list_case_payloads(parts[0])})
                return
            if len(parts) == 2 and parts[1] == "runs":
                self._send_json({"runs": list_eval_runs(parts[0])})
                return

        if parsed.path == "/api/cases":
            query = parse_qs(parsed.query)
            target_id = (query.get("target") or [""])[0].strip() or None
            self._send_json({"cases": list_case_payloads(target_id)})
            return

        if parsed.path == "/api/runs":
            query = parse_qs(parsed.query)
            target_id = (query.get("target") or [""])[0].strip() or None
            self._send_json({"runs": list_eval_runs(target_id)})
            return

        if parsed.path.startswith("/api/runs/"):
            suffix = parsed.path.removeprefix("/api/runs/").strip("/")
            parts = [part for part in suffix.split("/") if part]
            if len(parts) == 1:
                self._send_json(load_run_detail(parts[0]))
                return
            if len(parts) == 2 and parts[1] == "log":
                self._send_json({"log": load_run_log(parts[0])})
                return
            if len(parts) == 3 and parts[1] == "results":
                self._send_json(load_run_case_result(parts[0], parts[2]))
                return
            if len(parts) == 3 and parts[1] == "backend":
                self._send_json(load_run_backend_trace(parts[0], parts[2]))
                return

        if parsed.path == "/api/summary":
            query = parse_qs(parsed.query)
            target_id = (query.get("target") or [""])[0].strip() or None
            runs = list_eval_runs(target_id)
            self._send_json(load_run_detail(runs[0]["run_id"]) if runs else {"cases": [], "stats": {}})
            return

        if parsed.path == "/api/latest-eval":
            self._send_json(_json_load_file(DEFAULT_RAW_EVAL_FILE, {}))
            return

        if parsed.path == "/api/run":
            self._send_json(current_run_payload())
            return
        if parsed.path == "/api/active-run":
            query = parse_qs(parsed.query)
            target_id = (query.get("target") or [""])[0].strip() or None
            self._send_json(active_run_payload(target_id))
            return
        if parsed.path == "/api/live":
            self._send_json(live_progress_payload())
            return

        if parsed.path == "/api/backend/sessions":
            query = parse_qs(parsed.query)
            target_id = (query.get("target") or query.get("env") or [load_app_config().default_target_id])[0]
            self._send_json(backend_sessions_payload(target_id))
            return

        if parsed.path.startswith("/api/backend/session/") and parsed.path.endswith("/turns"):
            query = parse_qs(parsed.query)
            target_id = (query.get("target") or query.get("env") or [load_app_config().default_target_id])[0]
            session_id = parsed.path.removeprefix("/api/backend/session/").removesuffix("/turns").strip("/")
            self._send_json(backend_turns_payload(session_id, target_id))
            return

        self._send_json({"error": "not found"}, HTTPStatus.NOT_FOUND)

    def do_POST(self) -> None:
        parsed = urlparse(self.path)
        if parsed.path == "/api/run":
            payload = self._read_json_body()
            if current_run_payload()["running"]:
                self._send_json({"ok": False, "error": "run already in progress"}, HTTPStatus.CONFLICT)
                return
            thread = threading.Thread(target=_run_eval_command, args=(payload,), daemon=True)
            thread.start()
            self._send_json({"ok": True})
            return
        self._send_json({"error": "not found"}, HTTPStatus.NOT_FOUND)


def run_dashboard(*, host: str = "127.0.0.1", port: int = 15600, open_browser: bool = True) -> None:
    server = ThreadingHTTPServer((host, port), DashboardHandler)
    url = f"http://{host}:{port}/"
    print(f"SmartBot eval dashboard running at {url}")
    if open_browser:
        try:
            subprocess.Popen(["open", url])
        except Exception:
            pass
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
