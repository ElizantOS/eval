from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
import importlib
import json
import os
from pathlib import Path
import socket
import threading
import time
import uuid
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urljoin
from urllib.request import Request, urlopen

from ..common import EvalConfigError, default_live_case_file, ensure_directory, serialize_x_user_info


DEFAULT_TIMEOUT_SECONDS = 180
DEFAULT_MAX_TURNS = 8
DEFAULT_MAX_REQUEST_RETRIES = 2
@dataclass(slots=True)
class RequestSpec:
    method: str
    url: str
    headers: dict[str, str]
    payload: dict[str, Any] | None = None
    timeout_seconds: int | None = None


@dataclass(slots=True)
class DriverEvent:
    event_type: str
    question: str | None = None
    options: list[str] = field(default_factory=list)
    slot: str | None = None
    answer: str | None = None
    raw_name: str | None = None
    raw_payload: Any = None
    reply_mode: str = "user_message"


@dataclass(slots=True)
class SimulatedUserReply:
    answer: str
    confidence: float = 0.0
    used_profile_keys: list[str] = field(default_factory=list)
    source: str = "simulated_user"


@dataclass(slots=True)
class DriverRunState:
    session_id: str | None = None
    previous_response_id: str | None = None
    turn_index: int = 0
    transcript: list[dict[str, Any]] = field(default_factory=list)
    request_payloads: list[dict[str, Any]] = field(default_factory=list)
    response_payloads: list[dict[str, Any]] = field(default_factory=list)
    ask_count: int = 0
    simulated_user_trace: list[dict[str, Any]] = field(default_factory=list)
    unexpected_asks: list[dict[str, Any]] = field(default_factory=list)
    runner_warnings: list[str] = field(default_factory=list)
    driver_metadata: dict[str, Any] = field(default_factory=dict)
    final_answer: str = ""


def timeout_seconds() -> int:
    raw = str(os.environ.get("SMARTBOT_EVAL_REQUEST_TIMEOUT_SECONDS") or "").strip()
    if not raw:
        return DEFAULT_TIMEOUT_SECONDS
    try:
        value = int(raw)
    except ValueError:
        return DEFAULT_TIMEOUT_SECONDS
    return max(10, value)


def json_headers(headers: dict[str, str] | None = None) -> dict[str, str]:
    base = {"Content-Type": "application/json"}
    if headers:
        base.update(headers)
    return base


def perform_request(
    *,
    method: str,
    url: str,
    headers: dict[str, str],
    payload: dict[str, Any] | None = None,
    timeout: int | None = None,
) -> tuple[int, dict[str, Any]]:
    timeout = timeout or timeout_seconds()
    result_holder: dict[str, Any] = {}
    error_holder: dict[str, BaseException] = {}

    def worker() -> None:
        data = None
        if payload is not None:
            data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        request = Request(url=url, method=method.upper(), headers=headers, data=data)
        try:
            with urlopen(request, timeout=timeout) as response:
                body = response.read().decode("utf-8")
                result_holder["result"] = (int(response.status), json.loads(body) if body else {})
        except HTTPError as exc:
            raw = exc.read().decode("utf-8")
            try:
                error_payload = json.loads(raw) if raw else {}
            except json.JSONDecodeError:
                error_payload = {"raw": raw}
            result_holder["result"] = (int(exc.code), error_payload)
        except (TimeoutError, socket.timeout):
            error_holder["error"] = EvalConfigError(f"request timed out after {timeout}s: {url}")
        except URLError as exc:
            error_holder["error"] = EvalConfigError(f"request failed: {exc}")
        except BaseException as exc:  # pragma: no cover - defensive
            error_holder["error"] = exc

    thread = threading.Thread(target=worker, daemon=True)
    thread.start()
    thread.join(timeout)
    if thread.is_alive():
        raise EvalConfigError(f"request timed out after {timeout}s: {url}")
    if "result" in result_holder:
        return result_holder["result"]
    if "error" in error_holder:
        raise error_holder["error"]
    raise EvalConfigError(f"request failed without result: {url}")


def _decode_sse_payload(lines: list[str]) -> str:
    data_lines: list[str] = []
    for line in lines:
        if line.startswith("data:"):
            data_lines.append(line[5:].lstrip())
    return "\n".join(data_lines).strip()


def _live_case_path() -> Path:
    path = default_live_case_file()
    ensure_directory(path.parent)
    return path


def read_live_case_progress() -> dict[str, Any]:
    path = _live_case_path()
    if not path.exists():
        return {}
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}
    return payload if isinstance(payload, dict) else {}


def write_live_case_progress(payload: dict[str, Any]) -> None:
    path = _live_case_path()
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def merge_live_case_progress(patch: dict[str, Any]) -> dict[str, Any]:
    current = read_live_case_progress()
    current.update(patch)
    current["updated_at"] = time.time()
    write_live_case_progress(current)
    return current


def perform_sse_request(
    *,
    method: str,
    url: str,
    headers: dict[str, str],
    payload: dict[str, Any] | None = None,
    idle_timeout: int | None = None,
    on_event: Any = None,
) -> tuple[int, dict[str, Any], list[dict[str, Any]]]:
    idle_timeout = idle_timeout or timeout_seconds()
    data = None
    if payload is not None:
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    request_headers = dict(headers)
    request_headers.setdefault("Accept", "text/event-stream")
    request = Request(url=url, method=method.upper(), headers=request_headers, data=data)

    try:
        with urlopen(request, timeout=idle_timeout) as response:
            status_code = int(response.status)
            events: list[dict[str, Any]] = []
            final_response: dict[str, Any] | None = None
            buffered_lines: list[str] = []

            def flush_buffer() -> bool:
                nonlocal buffered_lines, final_response
                payload_text = _decode_sse_payload(buffered_lines)
                buffered_lines = []
                if not payload_text:
                    return False
                if payload_text == "[DONE]":
                    return True
                try:
                    parsed = json.loads(payload_text)
                except json.JSONDecodeError as exc:
                    raise EvalConfigError(f"sse event json parse failed: {payload_text[:500]}") from exc
                if not isinstance(parsed, dict):
                    raise EvalConfigError("sse event payload must be an object")
                events.append(parsed)
                if callable(on_event):
                    on_event(parsed)
                response_payload = parsed.get("response")
                if isinstance(response_payload, dict):
                    final_response = response_payload
                return False

            while True:
                raw_line = response.readline()
                if not raw_line:
                    break
                line = raw_line.decode("utf-8")
                stripped = line.rstrip("\r\n")
                if not stripped:
                    if flush_buffer():
                        break
                    continue
                buffered_lines.append(stripped)

            if buffered_lines:
                flush_buffer()

            return status_code, final_response or {}, events
    except HTTPError as exc:
        raw = exc.read().decode("utf-8")
        try:
            error_payload = json.loads(raw) if raw else {}
        except json.JSONDecodeError:
            error_payload = {"raw": raw}
        return int(exc.code), error_payload, []
    except (TimeoutError, socket.timeout):
        raise EvalConfigError(f"sse idle timeout after {idle_timeout}s: {url}")
    except URLError as exc:
        raise EvalConfigError(f"sse request failed: {exc}")


def build_auth_headers(target: dict[str, Any]) -> dict[str, str]:
    headers = dict(target.get("default_headers") or {})
    auth_mode = str(target.get("auth_mode") or "").strip()
    auth_value = str(target.get("auth_value") or "").strip()
    if auth_mode == "x_api_key" and auth_value:
        headers["X-API-Key"] = auth_value
    elif auth_mode == "bearer" and auth_value:
        headers["Authorization"] = f"Bearer {auth_value}"
    x_user_info = serialize_x_user_info(target.get("identity"))
    if x_user_info:
        headers["x-user-info"] = x_user_info
    return headers


def maybe_switch_provider(*, target: dict[str, Any], headers: dict[str, str]) -> None:
    profile_name = str(target.get("admin_provider_profile") or "").strip()
    if not profile_name:
        return
    base_url = str(target.get("base_url") or "").rstrip("/")
    switch_url = urljoin(f"{base_url}/", "admin/provider/switch")
    request_headers = {
        key: value
        for key, value in headers.items()
        if key.lower() in {"x-api-key", "authorization"}
    }
    status_code, payload = perform_request(
        method="POST",
        url=switch_url,
        headers=json_headers(request_headers),
        payload={"profile_name": profile_name},
    )
    if status_code >= 400:
        raise EvalConfigError(
            f"failed to switch provider to {profile_name}: status={status_code} payload={payload}"
        )


def call_json_model(*, system_prompt: str, user_payload: dict[str, Any], model: str | None = None) -> dict[str, Any]:
    api_key = str(os.environ.get("OPENAI_API_KEY") or "").strip()
    if not api_key:
        raise EvalConfigError("OPENAI_API_KEY missing")
    base_url = str(
        os.environ.get("OPENAI_BASE_URL")
        or os.environ.get("OPENAI_API_BASE_URL")
        or "https://api.openai.com/v1"
    ).rstrip("/")
    status_code, payload = perform_request(
        method="POST",
        url=f"{base_url}/chat/completions",
        headers=json_headers({"Authorization": f"Bearer {api_key}"}),
        payload={
            "model": model or str(os.environ.get("SMARTBOT_EVAL_JUDGE_MODEL") or "gpt-5.2"),
            "temperature": 0,
            "response_format": {"type": "json_object"},
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": json.dumps(user_payload, ensure_ascii=False)},
            ],
        },
    )
    if status_code >= 400:
        raise EvalConfigError(f"llm json call failed: status={status_code} payload={payload}")
    content = ((((payload.get("choices") or [{}])[0]).get("message") or {}).get("content") if isinstance(payload, dict) else None)
    try:
        parsed = json.loads(str(content or "{}"))
    except json.JSONDecodeError as exc:
        raise EvalConfigError(f"llm json parse failed: {content}") from exc
    if not isinstance(parsed, dict):
        raise EvalConfigError("llm json response must be an object")
    return parsed


def load_driver_class(target: dict[str, Any]) -> type["TargetDriver"]:
    configured = str(target.get("driver_class") or "").strip()
    if not configured:
        raise EvalConfigError(f"driver_class missing for target {target.get('id')}")
    driver_path = configured
    module_name, _, class_name = driver_path.rpartition(".")
    if not module_name or not class_name:
        raise EvalConfigError(f"invalid driver_class: {driver_path}")
    try:
        module = importlib.import_module(module_name)
    except ModuleNotFoundError as exc:
        raise EvalConfigError(f"driver module not found: {module_name}") from exc
    driver_cls = getattr(module, class_name, None)
    if driver_cls is None:
        raise EvalConfigError(f"driver class not found: {driver_path}")
    return driver_cls


class TargetDriver(ABC):
    def __init__(self, *, target: dict[str, Any], context_vars: dict[str, Any]) -> None:
        self.target = target
        self.context_vars = context_vars

    def _live_case_meta(self, *, state: DriverRunState) -> dict[str, Any]:
        return {
            "target_id": self.target.get("id"),
            "case_id": self.context_vars.get("case_id"),
            "title": self.context_vars.get("title"),
            "entry_question": self.context_vars.get("entry_question"),
            "expected_mode": self.context_vars.get("expected_mode"),
            "turn_index": state.turn_index,
            "session_id": state.session_id,
            "ask_count": state.ask_count,
        }

    def update_live_case(self, *, state: DriverRunState, patch: dict[str, Any]) -> None:
        current = read_live_case_progress()
        current.update(self._live_case_meta(state=state))
        current.update(patch)
        merge_live_case_progress(current)

    @abstractmethod
    def build_initial_request(self, prompt: str, state: DriverRunState) -> RequestSpec:
        raise NotImplementedError

    def send_request(self, request_spec: RequestSpec) -> tuple[int, dict[str, Any]]:
        return perform_request(
            method=request_spec.method,
            url=request_spec.url,
            headers=request_spec.headers,
            payload=request_spec.payload,
            timeout=request_spec.timeout_seconds,
        )

    @abstractmethod
    def parse_response(self, raw_response: dict[str, Any], state: DriverRunState) -> DriverEvent:
        raise NotImplementedError

    @abstractmethod
    def build_followup_request(
        self,
        *,
        event: DriverEvent,
        user_reply: SimulatedUserReply,
        state: DriverRunState,
    ) -> RequestSpec:
        raise NotImplementedError

    @abstractmethod
    def extract_final_answer(self, raw_response: dict[str, Any], state: DriverRunState) -> str | None:
        raise NotImplementedError

    @abstractmethod
    def extract_backend_ids(self, raw_response: dict[str, Any], state: DriverRunState) -> dict[str, Any]:
        raise NotImplementedError

    @abstractmethod
    def serialize_trace(
        self,
        *,
        prompt: str,
        request_spec: RequestSpec,
        raw_response: dict[str, Any],
        status_code: int,
        state: DriverRunState,
        event: DriverEvent | None,
    ) -> dict[str, Any]:
        raise NotImplementedError

    def _scripted_reply(
        self,
        *,
        event: DriverEvent,
        scripted_steps: list[dict[str, Any]],
    ) -> SimulatedUserReply | None:
        question = str(event.question or "").strip().lower()
        slot = str(event.slot or "").strip() or None
        for index, step in enumerate(scripted_steps):
            step_slot = str(step.get("slot") or "").strip() or None
            if slot and step_slot and slot != step_slot:
                continue
            fragments = [str(item).strip().lower() for item in (step.get("question_contains") or []) if str(item).strip()]
            if fragments and question and not any(fragment in question for fragment in fragments):
                continue
            selected = scripted_steps.pop(index)
            return SimulatedUserReply(
                answer=str(selected.get("answer") or "").strip(),
                confidence=1.0,
                used_profile_keys=["conversation_script"],
                source="conversation_script",
            )
        return None

    def _simulate_user_reply(
        self,
        *,
        event: DriverEvent,
        state: DriverRunState,
        scripted_steps: list[dict[str, Any]],
    ) -> SimulatedUserReply:
        profile = self.context_vars.get("simulated_user_profile")
        if not isinstance(profile, dict):
            profile = {}
        heuristic = self._heuristic_simulated_reply(event=event, profile=profile)
        if heuristic is not None:
            return heuristic
        payload = call_json_model(
            system_prompt=(
                "你在模拟评测中的用户，只负责回答 agent 的澄清问题。"
                "返回 JSON，字段固定为 answer, confidence, used_profile_keys。"
                "必须简短直接，不要解释，不要反问。"
                "如果有 options，优先从 options 中选择最合理的一项。"
                "如果问题是在询问时间范围、对比口径或分析范围，优先选择更窄、更稳定、更容易执行的选项。"
            ),
            user_payload={
                "entry_question": self.context_vars.get("entry_question"),
                "event": {
                    "event_type": event.event_type,
                    "question": event.question,
                    "options": event.options,
                    "slot": event.slot,
                },
                "simulated_user_profile": profile,
                "remaining_script_hints": scripted_steps,
                "transcript": state.transcript[-4:],
                "case_body": self.context_vars.get("case_body"),
            },
        )
        answer = str(payload.get("answer") or "").strip()
        if not answer and event.options:
            answer = str(event.options[0]).strip()
            state.runner_warnings.append("simulated_user_reply_fell_back_to_first_option")
        if not answer:
            raise EvalConfigError("simulated user could not produce an answer")
        confidence = float(payload.get("confidence") or 0.0)
        used_keys = [str(item) for item in payload.get("used_profile_keys", []) if str(item).strip()] if isinstance(payload.get("used_profile_keys"), list) else []
        return SimulatedUserReply(
            answer=answer,
            confidence=confidence,
            used_profile_keys=used_keys,
            source="simulated_user",
        )

    def _heuristic_simulated_reply(
        self,
        *,
        event: DriverEvent,
        profile: dict[str, Any],
    ) -> SimulatedUserReply | None:
        slot = str(event.slot or "").strip().lower()
        options = [str(item).strip() for item in event.options if str(item).strip()]
        if not options:
            return None

        direct_profile = profile.get(slot)
        if isinstance(direct_profile, str) and direct_profile.strip():
            profile_text = direct_profile.strip()
            for option in options:
                if profile_text in option or option in profile_text:
                    return SimulatedUserReply(
                        answer=option,
                        confidence=1.0,
                        used_profile_keys=[slot],
                        source="profile_match",
                    )

        if "time" in slot or "date" in slot or "window" in slot:
            preferred_fragments = [
                "只看今天 vs 昨天",
                "今天",
                "昨日",
                "近7天",
                "最近7天",
            ]
            for fragment in preferred_fragments:
                for option in options:
                    if fragment in option:
                        return SimulatedUserReply(
                            answer=option,
                            confidence=0.95,
                            used_profile_keys=["time_range_heuristic"],
                            source="heuristic",
                        )

        if "analysis" in slot and options:
            return SimulatedUserReply(
                answer=options[0],
                confidence=0.8,
                used_profile_keys=["analysis_topic_heuristic"],
                source="heuristic",
            )
        return None

    def resolve_user_reply(
        self,
        *,
        event: DriverEvent,
        state: DriverRunState,
        scripted_steps: list[dict[str, Any]],
    ) -> SimulatedUserReply:
        scripted = self._scripted_reply(event=event, scripted_steps=scripted_steps)
        if scripted is not None:
            return scripted
        state.unexpected_asks.append(
            {
                "question": event.question,
                "slot": event.slot,
                "options": event.options,
                "raw_name": event.raw_name,
            }
        )
        return self._simulate_user_reply(event=event, state=state, scripted_steps=scripted_steps)

    def run_case(self, prompt: str) -> dict[str, Any]:
        state = DriverRunState()
        setattr(self, "_current_state", state)
        scripted_steps = [
            {
                "answer": str(step.get("answer") or "").strip(),
                "slot": str(step.get("slot") or "").strip() or None,
                "question_contains": [
                    str(fragment).strip()
                    for fragment in (step.get("question_contains") or [])
                    if str(fragment).strip()
                ],
            }
            for step in (self.context_vars.get("conversation_script") or [])
            if isinstance(step, dict)
        ]
        started_at = time.perf_counter()
        final_answer = ""
        request_spec = self.build_initial_request(prompt, state)
        self.update_live_case(
            state=state,
            patch={
                "status": "running",
                "stream_text": "",
                "stream_events": [],
                "final_answer": "",
                "error": None,
            },
        )

        for turn_index in range(1, DEFAULT_MAX_TURNS + 1):
            state.turn_index = turn_index
            retry_attempt = 0
            while True:
                try:
                    status_code, raw_response = self.send_request(request_spec)
                except EvalConfigError as exc:
                    status_code, raw_response = self.request_exception_payload(exc)
                event: DriverEvent | None = None
                if status_code < 400:
                    event = self.parse_response(raw_response, state)
                if retry_attempt >= DEFAULT_MAX_REQUEST_RETRIES or not self.should_retry_request(
                    status_code=status_code,
                    raw_response=raw_response,
                    event=event,
                ):
                    break
                retry_attempt += 1
                state.runner_warnings.append(
                    f"retrying_request turn={turn_index} attempt={retry_attempt} reason={self.retry_reason(raw_response)}"
                )
            if request_spec.payload is not None:
                state.request_payloads.append(request_spec.payload)
            state.response_payloads.append(raw_response)
            turn_record = self.serialize_trace(
                prompt=prompt,
                request_spec=request_spec,
                raw_response=raw_response,
                status_code=status_code,
                state=state,
                event=event,
            )
            state.transcript.append(turn_record)

            if status_code >= 400:
                return self._result_payload(
                    state=state,
                    final_answer="",
                    started_at=started_at,
                    error=raw_response,
                    scripted_steps=scripted_steps,
                    events=["http_error"],
                )

            if event is None:
                return self._result_payload(
                    state=state,
                    final_answer="",
                    started_at=started_at,
                    error="driver did not produce an event",
                    scripted_steps=scripted_steps,
                    events=["driver_error"],
                )

            if event.event_type == "terminal_error":
                return self._result_payload(
                    state=state,
                    final_answer="",
                    started_at=started_at,
                    error=event.raw_payload if event.raw_payload is not None else (event.answer or "terminal error"),
                    scripted_steps=scripted_steps,
                    events=["terminal_error"],
                )

            if event.event_type == "clarification_request":
                state.ask_count += 1
                user_reply = self.resolve_user_reply(event=event, state=state, scripted_steps=scripted_steps)
                state.simulated_user_trace.append(
                    {
                        "turn": turn_index,
                        "question": event.question,
                        "slot": event.slot,
                        "options": event.options,
                        "answer": user_reply.answer,
                        "source": user_reply.source,
                        "confidence": user_reply.confidence,
                        "used_profile_keys": user_reply.used_profile_keys,
                    }
                )
                if isinstance(turn_record.get("asks"), list):
                    turn_record["asks"].append(
                        {
                            "question": event.question,
                            "slot": event.slot,
                            "options": event.options,
                            "answer": user_reply.answer,
                            "source": user_reply.source,
                        }
                    )
                request_spec = self.build_followup_request(event=event, user_reply=user_reply, state=state)
                self.update_live_case(
                    state=state,
                    patch={"status": "waiting_followup", "last_user_reply": user_reply.answer},
                )
                continue

            if event.event_type == "final_answer":
                final_answer = event.answer or self.extract_final_answer(raw_response, state) or ""
                break

            if event.event_type == "intermediate_message":
                state.runner_warnings.append("received_intermediate_message_without_framework_action")
                final_answer = event.answer or self.extract_final_answer(raw_response, state) or ""
                if final_answer:
                    break
                return self._result_payload(
                    state=state,
                    final_answer="",
                    started_at=started_at,
                    error="intermediate_message received without a final answer",
                    scripted_steps=scripted_steps,
                    events=["intermediate_message", "driver_error"],
                )

            if event.event_type == "tool_call":
                state.runner_warnings.append("received_non_clarification_tool_call_without_handler")
                final_answer = event.answer or self.extract_final_answer(raw_response, state) or ""
                if final_answer:
                    break
                return self._result_payload(
                    state=state,
                    final_answer="",
                    started_at=started_at,
                    error=f"unhandled tool_call event: {event.raw_name or 'unknown'}",
                    scripted_steps=scripted_steps,
                    events=["tool_call", "driver_error"],
                )

            return self._result_payload(
                state=state,
                final_answer="",
                started_at=started_at,
                error=f"unsupported driver event_type: {event.event_type}",
                scripted_steps=scripted_steps,
                events=["driver_error"],
            )

        if not final_answer:
            return self._result_payload(
                state=state,
                final_answer="",
                started_at=started_at,
                error=f"driver exceeded max turns ({DEFAULT_MAX_TURNS}) without final answer",
                scripted_steps=scripted_steps,
                events=["max_turns_exceeded"],
            )

        return self._result_payload(
            state=state,
            final_answer=final_answer,
            started_at=started_at,
            error=None,
            scripted_steps=scripted_steps,
            events=["ask_clarification", "final_answer"] if state.ask_count else ["final_answer_only"],
        )

    def _result_payload(
        self,
        *,
        state: DriverRunState,
        final_answer: str,
        started_at: float,
        error: Any,
        scripted_steps: list[dict[str, Any]],
        events: list[str],
    ) -> dict[str, Any]:
        final_status = "completed" if error is None else "failed"
        self.update_live_case(
            state=state,
            patch={
                "status": final_status,
                "final_answer": final_answer,
                "error": error,
                "events": events,
            },
        )
        setattr(self, "_current_state", None)
        return {
            "final_answer": final_answer,
            "transcript": state.transcript,
            "ask_count": state.ask_count,
            "events": events,
            "latency_ms": round((time.perf_counter() - started_at) * 1000, 2),
            "error": error,
            "session_id": state.session_id,
            "target_id": self.target.get("id"),
            "request_payload": state.request_payloads[-1] if state.request_payloads else None,
            "response_payload": state.response_payloads[-1] if state.response_payloads else None,
            "request_payloads": state.request_payloads,
            "response_payloads": state.response_payloads,
            "simulated_user_trace": state.simulated_user_trace,
            "unexpected_asks": state.unexpected_asks,
            "unused_script_steps": scripted_steps,
            "runner_warnings": state.runner_warnings,
        }

    def request_exception_payload(self, exc: EvalConfigError) -> tuple[int, dict[str, Any]]:
        message = str(exc)
        code = "timeout_error" if "timed out" in message.lower() else "request_error"
        return 599, {"error": {"code": code, "message": message}}

    def should_retry_request(
        self,
        *,
        status_code: int,
        raw_response: dict[str, Any],
        event: DriverEvent | None,
    ) -> bool:
        error_payload = raw_response.get("error") if isinstance(raw_response, dict) else None
        if isinstance(error_payload, dict):
            code = str(error_payload.get("code") or "").strip()
            if code in {"timeout_error", "request_error", "upstream_error", "internal_error"}:
                return False
        if status_code >= 500:
            return True
        if event and event.event_type == "terminal_error" and isinstance(event.raw_payload, dict):
            code = str((event.raw_payload or {}).get("code") or ((event.raw_payload or {}).get("error") or {}).get("code") or "").strip()
            if code in {"timeout_error", "request_error", "upstream_error", "internal_error"}:
                return False
        return False

    def retry_reason(self, raw_response: dict[str, Any]) -> str:
        error_payload = raw_response.get("error") if isinstance(raw_response, dict) else None
        if isinstance(error_payload, dict):
            return str(error_payload.get("code") or error_payload.get("message") or "unknown")
        return "http_failure"


def run_target_case(prompt: str, context_vars: dict[str, Any]) -> dict[str, Any]:
    target = context_vars.get("target")
    if not isinstance(target, dict):
        raise EvalConfigError("context vars missing target config")
    driver_cls = load_driver_class(target)
    driver = driver_cls(target=target, context_vars=context_vars)
    return driver.run_case(prompt)
