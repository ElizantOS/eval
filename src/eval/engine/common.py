from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import fnmatch
import json
import os
from pathlib import Path
import re
import uuid
from typing import Any

import yaml

from eval.engine.common import (
    EvalConfigError,
    ensure_directory,
    serialize_x_user_info,
    smartbot_dir,
    workspace_dir,
)


BASE_DIR = workspace_dir()
SMARTBOT_DIR = smartbot_dir()

DEFAULT_APP_CONFIG_FILE = BASE_DIR / "config" / "app.yaml"
DEFAULT_TARGETS_DIR = BASE_DIR / "config" / "targets"
DEFAULT_CASES_DIR = BASE_DIR / "cases"
DEFAULT_BACKEND_ENV_FILE = SMARTBOT_DIR / "backend" / ".env"
DEFAULT_PROMPTFOO_CONFIG = BASE_DIR / "config" / "promptfooconfig.yaml"
DEFAULT_SKILLS_DIR = SMARTBOT_DIR / "backend" / "skills"
DEFAULT_GENERATED_TESTS_FILE = BASE_DIR / ".promptfoo" / "generated-tests.json"
DEFAULT_RAW_EVAL_FILE = BASE_DIR / ".promptfoo" / "latest-eval.json"
DEFAULT_SUMMARY_JSON_FILE = BASE_DIR / ".promptfoo" / "latest-summary.json"
DEFAULT_SUMMARY_MD_FILE = BASE_DIR / ".promptfoo" / "latest-summary.md"
DEFAULT_LIVE_RUN_FILE = BASE_DIR / ".promptfoo" / "live-run.json"
DEFAULT_LIVE_CASE_FILE = BASE_DIR / ".promptfoo" / "live-case.json"
DEFAULT_ACTIVE_RUN_PROGRESS_FILE = BASE_DIR / ".promptfoo" / "active-run-progress.json"
DEFAULT_RUNS_DIR = BASE_DIR / "runs"

EXPECTED_MODES = {"single_turn", "interactive"}
SUPPORTED_PROTOCOLS = {"responses", "chat_completions"}
DEFAULT_HARD_ASSERTIONS = {
    "single_turn": ["no_error", "non_empty_final_answer"],
    "interactive": ["no_error", "non_empty_final_answer"],
}


@dataclass(slots=True)
class AppConfig:
    default_target_id: str
    workspace_root: Path
    dashboard_host: str = "127.0.0.1"
    dashboard_port: int = 15600
    targets_dir: Path = DEFAULT_TARGETS_DIR
    cases_dir: Path = DEFAULT_CASES_DIR
    promptfoo_config_file: Path = DEFAULT_PROMPTFOO_CONFIG


@dataclass(slots=True)
class ConversationStep:
    answer: str
    slot: str | None = None
    question_contains: list[str] | None = None


@dataclass(slots=True)
class EvalCase:
    case_path: Path
    case_id: str
    title: str
    enabled: bool
    target_id: str
    skill_name: str
    tags: list[str]
    entry_question: str
    expected_mode: str
    conversation_script: list[ConversationStep]
    simulated_user_profile: dict[str, Any]
    judge_rubric: str
    hard_assertions: list[str]
    body: str
    summary: str


@dataclass(slots=True)
class AgentTarget:
    id: str
    name: str
    protocol: str
    driver_class: str | None
    base_url: str
    auth_mode: str
    auth_value_ref: str | None
    default_headers: dict[str, str]
    identity: dict[str, Any] | str | None
    conversation_mode: str
    session_header_name: str = "X-Session-Id"
    previous_response_supported: bool = False
    history_strategy: str = "server_managed"
    tool_call_shape: str = "responses_items"
    tool_result_shape: str = "function_call_output"
    clarification_mode: str = "tool_call"
    admin_provider_profile: str | None = None


@dataclass(slots=True)
class JudgeProvider:
    provider_id: str
    model: str
    api_key: str
    base_url: str | None


def read_json_file(path: Path, default: Any) -> Any:
    if not path.exists():
        return default
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return default


def write_json_file(path: Path, payload: Any) -> None:
    ensure_directory(path.parent)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def read_active_run_progress() -> dict[str, Any]:
    payload = read_json_file(DEFAULT_ACTIVE_RUN_PROGRESS_FILE, {})
    return payload if isinstance(payload, dict) else {}


def write_active_run_progress(payload: dict[str, Any]) -> None:
    write_json_file(DEFAULT_ACTIVE_RUN_PROGRESS_FILE, payload)


def merge_active_run_progress(patch: dict[str, Any]) -> dict[str, Any]:
    current = read_active_run_progress()
    if not isinstance(current, dict):
        current = {}
    current.update(patch)
    current["updated_at"] = datetime.now(timezone.utc).isoformat()
    write_active_run_progress(current)
    return current


def utc_timestamp_slug() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def slugify(value: str) -> str:
    slug = re.sub(r"[^a-zA-Z0-9]+", "-", value.strip().lower()).strip("-")
    return slug or f"case-{uuid.uuid4().hex[:8]}"


def load_judge_provider() -> JudgeProvider:
    direct_api_key = str(os.getenv("OPENAI_API_KEY") or "").strip()
    if direct_api_key:
        model = str(os.getenv("SMARTBOT_EVAL_JUDGE_MODEL") or "gpt-5.2").strip()
        base_url = str(
            os.getenv("OPENAI_BASE_URL") or os.getenv("OPENAI_API_BASE_URL") or ""
        ).strip() or None
        return JudgeProvider(
            provider_id=f"openai:chat:{model}",
            model=model,
            api_key=direct_api_key,
            base_url=base_url,
        )
    raise EvalConfigError("judge provider missing: set OPENAI_API_KEY (and optional OPENAI_BASE_URL / SMARTBOT_EVAL_JUDGE_MODEL)")


def load_app_config(path: Path = DEFAULT_APP_CONFIG_FILE) -> AppConfig:
    override = os.getenv("SMARTBOT_EVAL_APP_CONFIG_FILE")
    if override:
        path = Path(override).expanduser().resolve()
    if not path.exists():
        raise EvalConfigError(f"app config not found: {path}")
    payload = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    if not isinstance(payload, dict):
        raise EvalConfigError("app.yaml must be a YAML object")
    workspace_root = path.parent.parent.resolve()
    dashboard = payload.get("dashboard") or {}
    if not isinstance(dashboard, dict):
        dashboard = {}
    workspace = payload.get("workspace") or {}
    if not isinstance(workspace, dict):
        workspace = {}
    runner = payload.get("runner") or {}
    if not isinstance(runner, dict):
        runner = {}
    default_target_id = str(payload.get("default_target_id") or "").strip()
    if not default_target_id:
        raise EvalConfigError("app.yaml missing default_target_id")

    def _resolve_path(raw: Any, default: Path) -> Path:
        text = str(raw or "").strip()
        if not text:
            return default
        candidate = Path(text).expanduser()
        if not candidate.is_absolute():
            candidate = (workspace_root / candidate).resolve()
        return candidate

    return AppConfig(
        default_target_id=default_target_id,
        workspace_root=workspace_root,
        dashboard_host=str(dashboard.get("host") or "127.0.0.1"),
        dashboard_port=int(dashboard.get("port") or 15600),
        targets_dir=_resolve_path(workspace.get("targets_dir"), DEFAULT_TARGETS_DIR),
        cases_dir=_resolve_path(workspace.get("cases_dir"), DEFAULT_CASES_DIR),
        promptfoo_config_file=_resolve_path(runner.get("promptfoo_config_file"), DEFAULT_PROMPTFOO_CONFIG),
    )


def configured_targets_dir() -> Path:
    override = os.getenv("SMARTBOT_EVAL_TARGETS_DIR")
    if override:
        return Path(override).expanduser().resolve()
    return load_app_config().targets_dir


def configured_cases_dir() -> Path:
    override = os.getenv("SMARTBOT_EVAL_CASES_DIR")
    if override:
        return Path(override).expanduser().resolve()
    return load_app_config().cases_dir


def configured_skills_dir() -> Path:
    override = os.getenv("SMARTBOT_EVAL_SKILLS_DIR")
    if override:
        return Path(override).expanduser().resolve()
    return DEFAULT_SKILLS_DIR


def configured_promptfoo_config() -> Path:
    return load_app_config().promptfoo_config_file


def load_targets(targets_dir: Path | None = None) -> dict[str, AgentTarget]:
    targets_dir = targets_dir.resolve() if isinstance(targets_dir, Path) else configured_targets_dir()
    if not targets_dir.exists():
        raise EvalConfigError(f"targets dir not found: {targets_dir}")
    targets: dict[str, AgentTarget] = {}
    for path in sorted(targets_dir.glob("*.yaml")):
        payload = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        if not isinstance(payload, dict):
            raise EvalConfigError(f"target config must be a YAML object: {path}")
        target_id = str(payload.get("id") or path.stem).strip()
        protocol = str(payload.get("protocol") or "").strip()
        base_url = str(payload.get("base_url") or "").strip().rstrip("/")
        auth_mode = str(payload.get("auth_mode") or "").strip()
        auth_value_ref = str(payload.get("auth_value_ref") or "").strip() or None
        if not target_id or protocol not in SUPPORTED_PROTOCOLS or not base_url or not auth_mode:
            raise EvalConfigError(f"invalid target config: {path}")
        default_headers = payload.get("default_headers") or {}
        if not isinstance(default_headers, dict):
            raise EvalConfigError(f"default_headers must be an object in {path}")
        targets[target_id] = AgentTarget(
            id=target_id,
            name=str(payload.get("name") or target_id).strip(),
            protocol=protocol,
            driver_class=str(payload.get("driver_class") or "").strip() or None,
            base_url=base_url,
            auth_mode=auth_mode,
            auth_value_ref=auth_value_ref,
            default_headers={str(k): str(v) for k, v in default_headers.items()},
            identity=payload.get("identity"),
            conversation_mode=str(payload.get("conversation_mode") or "server_managed").strip(),
            session_header_name=str(payload.get("session_header_name") or "X-Session-Id").strip(),
            previous_response_supported=bool(payload.get("previous_response_supported", False)),
            history_strategy=str(payload.get("history_strategy") or "server_managed").strip(),
            tool_call_shape=str(payload.get("tool_call_shape") or "responses_items").strip(),
            tool_result_shape=str(payload.get("tool_result_shape") or "function_call_output").strip(),
            clarification_mode=str(payload.get("clarification_mode") or "tool_call").strip(),
            admin_provider_profile=str(payload.get("admin_provider_profile") or "").strip() or None,
        )
    return targets


def load_target(target_id: str | None = None, *, targets_dir: Path | None = None) -> AgentTarget:
    targets = load_targets(targets_dir)
    if not targets:
        raise EvalConfigError("no targets configured")
    resolved_id = str(target_id or "").strip() or load_app_config().default_target_id
    target = targets.get(resolved_id)
    if target is None:
        raise EvalConfigError(f"target not found: {resolved_id}")
    return target


def parse_frontmatter(markdown_text: str, *, source: Path) -> tuple[dict[str, Any], str]:
    if not markdown_text.startswith("---"):
        raise EvalConfigError(f"case file missing YAML frontmatter: {source}")
    parts = markdown_text.split("\n---", 1)
    if len(parts) != 2:
        raise EvalConfigError(f"case file has unterminated frontmatter: {source}")
    frontmatter_block = parts[0][3:]
    body = parts[1]
    if body.startswith("\n"):
        body = body[1:]
    payload = yaml.safe_load(frontmatter_block) or {}
    if not isinstance(payload, dict):
        raise EvalConfigError(f"frontmatter must be a YAML object: {source}")
    return payload, body


def summarize_markdown(body: str, *, limit: int = 280) -> str:
    normalized = re.sub(r"\s+", " ", body).strip()
    if len(normalized) <= limit:
        return normalized
    return f"{normalized[: limit - 1].rstrip()}…"


def _normalize_string_list(value: Any, *, field_name: str, source: Path) -> list[str]:
    if value is None:
        return []
    if not isinstance(value, list):
        raise EvalConfigError(f"{field_name} must be a list in {source}")
    normalized: list[str] = []
    for item in value:
        text = str(item).strip()
        if text:
            normalized.append(text)
    return normalized


def _normalize_conversation_script(value: Any, *, source: Path) -> list[ConversationStep]:
    if value is None:
        return []
    if not isinstance(value, list):
        raise EvalConfigError(f"conversation_script must be a list in {source}")
    script: list[ConversationStep] = []
    for item in value:
        if not isinstance(item, dict):
            raise EvalConfigError(f"conversation_script items must be objects in {source}")
        answer = str(item.get("answer") or "").strip()
        if not answer:
            raise EvalConfigError(f"conversation_script answer is required in {source}")
        question_contains = item.get("question_contains")
        if question_contains is None:
            normalized_contains = None
        elif isinstance(question_contains, list):
            normalized_contains = [str(part).strip() for part in question_contains if str(part).strip()]
        else:
            normalized_contains = [str(question_contains).strip()]
        slot = str(item.get("slot") or "").strip() or None
        script.append(
            ConversationStep(
                answer=answer,
                slot=slot,
                question_contains=normalized_contains or None,
            )
        )
    return script


def _normalize_simulated_user_profile(value: Any, *, source: Path) -> dict[str, Any]:
    if value is None:
        return {}
    if not isinstance(value, dict):
        raise EvalConfigError(f"simulated_user_profile must be an object in {source}")
    return value


def _infer_simulated_user_profile(*, script: list[ConversationStep]) -> dict[str, Any]:
    inferred: dict[str, Any] = {}
    for step in script:
        if step.slot and step.answer:
            inferred[step.slot] = step.answer
    return inferred


def _infer_target_id_from_path(path: Path) -> str:
    parts = path.parts
    if "cases" in parts:
        index = parts.index("cases")
        if len(parts) > index + 2:
            return parts[index + 1]
    return load_app_config().default_target_id


def load_case(path: Path) -> EvalCase:
    payload, body = parse_frontmatter(path.read_text(encoding="utf-8"), source=path)
    case_id = str(payload.get("id") or "").strip()
    title = str(payload.get("title") or "").strip()
    target_id = str(payload.get("target_id") or "").strip() or _infer_target_id_from_path(path)
    skill_name = str(payload.get("skill_name") or "").strip()
    entry_question = str(payload.get("entry_question") or "").strip()
    expected_mode = str(payload.get("expected_mode") or "").strip()
    judge_rubric = str(payload.get("judge_rubric") or "").strip()
    if not case_id or not title or not target_id or not skill_name or not entry_question or not judge_rubric:
        raise EvalConfigError(f"case is missing required frontmatter fields: {path}")
    if expected_mode not in EXPECTED_MODES:
        raise EvalConfigError(
            f"expected_mode must be one of {sorted(EXPECTED_MODES)} in {path}"
        )

    hard_assertions = _normalize_string_list(
        payload.get("hard_assertions"),
        field_name="hard_assertions",
        source=path,
    ) or list(DEFAULT_HARD_ASSERTIONS[expected_mode])

    conversation_script = _normalize_conversation_script(payload.get("conversation_script"), source=path)
    simulated_user_profile = _normalize_simulated_user_profile(payload.get("simulated_user_profile"), source=path)
    if not simulated_user_profile:
        simulated_user_profile = _infer_simulated_user_profile(script=conversation_script)

    return EvalCase(
        case_path=path,
        case_id=case_id,
        title=title,
        enabled=bool(payload.get("enabled", True)),
        target_id=target_id,
        skill_name=skill_name,
        tags=_normalize_string_list(payload.get("tags"), field_name="tags", source=path),
        entry_question=entry_question,
        expected_mode=expected_mode,
        conversation_script=conversation_script,
        simulated_user_profile=simulated_user_profile,
        judge_rubric=judge_rubric,
        hard_assertions=hard_assertions,
        body=body.strip(),
        summary=summarize_markdown(body),
    )


def target_cases_dir(target_id: str, *, cases_dir: Path = DEFAULT_CASES_DIR) -> Path:
    return cases_dir / target_id


def load_cases(
    cases_dir: Path,
    *,
    target_id: str | None = None,
    case_pattern: str | None = None,
    tag: str | None = None,
    skill_name: str | None = None,
) -> list[EvalCase]:
    resolved_target_id = str(target_id or "").strip()
    scan_dir = target_cases_dir(resolved_target_id, cases_dir=cases_dir) if resolved_target_id else cases_dir
    if not scan_dir.exists():
        return []
    loaded: list[EvalCase] = []
    for path in sorted(scan_dir.rglob("*.md")):
        case = load_case(path)
        if not case.enabled:
            continue
        if resolved_target_id and case.target_id != resolved_target_id:
            continue
        if case_pattern and not fnmatch.fnmatch(case.case_id, case_pattern):
            continue
        if tag and tag not in case.tags:
            continue
        if skill_name and case.skill_name != skill_name:
            continue
        loaded.append(case)
    return loaded

def getenv_text(name: str) -> str | None:
    value = os.getenv(name)
    if value is None:
        return None
    text = value.strip()
    return text or None


def selected_filters_from_env() -> dict[str, str | None]:
    return {
        "selected_target": getenv_text("SMARTBOT_EVAL_TARGET"),
        "case_pattern": getenv_text("SMARTBOT_EVAL_CASE_PATTERN"),
        "tag": getenv_text("SMARTBOT_EVAL_TAG"),
        "skill_name": getenv_text("SMARTBOT_EVAL_SKILL"),
    }
