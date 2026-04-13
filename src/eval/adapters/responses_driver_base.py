from __future__ import annotations

from abc import abstractmethod
import json
import time
import uuid
from typing import Any
from urllib.parse import urljoin

from .runner_common import (
    DriverEvent,
    DriverRunState,
    RequestSpec,
    SimulatedUserReply,
    TargetDriver,
    build_auth_headers,
    json_headers,
    maybe_switch_provider,
    perform_sse_request,
    read_live_case_progress,
)


def build_responses_request_body(
    *,
    prompt: str,
    previous_response_id: str | None = None,
    input_items: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    body = {
        "input": input_items
        if input_items is not None
        else [
            {
                "type": "message",
                "role": "user",
                "content": [{"type": "input_text", "text": prompt}],
            }
        ],
        "stream": True,
        "tools": [],
    }
    if previous_response_id:
        body["previous_response_id"] = previous_response_id
    return body


def extract_responses_message_texts(raw_response: dict[str, Any]) -> list[str]:
    texts: list[str] = []
    for item in raw_response.get("output") or []:
        if not isinstance(item, dict) or item.get("type") != "message":
            continue
        for part in item.get("content") or []:
            if isinstance(part, dict) and part.get("type") == "output_text":
                text = str(part.get("text") or "").strip()
                if text:
                    texts.append(text)
    return texts


def safe_json_dict(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return value
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
        except json.JSONDecodeError:
            return {}
        return parsed if isinstance(parsed, dict) else {}
    return {}


def looks_like_clarification(call_fragment: dict[str, Any]) -> bool:
    arguments = safe_json_dict(call_fragment.get("arguments"))
    if arguments.get("question") and isinstance(arguments.get("options"), list):
        return True
    raw_name = str(call_fragment.get("name") or "").strip().lower()
    return "clarif" in raw_name or "missing_slot" in raw_name or raw_name.startswith("ask_")


class ResponsesDriverBase(TargetDriver):
    def __init__(self, *, target: dict[str, Any], context_vars: dict[str, Any]) -> None:
        super().__init__(target=target, context_vars=context_vars)
        self.base_url = str(target.get("base_url") or "").rstrip("/")
        self.headers = build_auth_headers(target)
        maybe_switch_provider(target=target, headers=self.headers)
        self.response_url = urljoin(f"{self.base_url}/", "v1/responses")
        self._last_sse_events: list[dict[str, Any]] = []
        self._current_state: DriverRunState | None = None

    def send_request(self, request_spec: RequestSpec) -> tuple[int, dict[str, Any]]:
        def on_event(event_payload: dict[str, Any]) -> None:
            summary = self._summarize_stream_event(event_payload)
            current = self._current_state
            if current is None:
                return
            live_case = read_live_case_progress()
            stream_events = (
                live_case.get("stream_events") if isinstance(live_case.get("stream_events"), list) else []
            )
            stream_events.append(summary)
            patch: dict[str, Any] = {
                "status": "streaming",
                "stream_events": stream_events[-80:],
                "last_stream_event_type": summary.get("type"),
            }
            if summary.get("append_text"):
                existing_text = str(live_case.get("stream_text") or "")
                patch["stream_text"] = (existing_text + str(summary.get("append_text")))[-12000:]
            self.update_live_case(state=current, patch=patch)

        status_code, payload, events = perform_sse_request(
            method=request_spec.method,
            url=request_spec.url,
            headers=request_spec.headers,
            payload=request_spec.payload,
            idle_timeout=request_spec.timeout_seconds,
            on_event=on_event,
        )
        self._last_sse_events = events
        return status_code, payload

    def _summarize_stream_event(self, event_payload: dict[str, Any]) -> dict[str, Any]:
        event_type = str(event_payload.get("type") or "").strip()
        summary: dict[str, Any] = {
            "type": event_type,
            "at": time.time(),
        }
        if event_type == "response.output_text.delta":
            delta = str(event_payload.get("delta") or "")
            summary["append_text"] = delta
            summary["preview"] = delta[-200:]
        elif event_type == "response.output_text.done":
            summary["text"] = str(event_payload.get("text") or "")
        elif event_type == "response.function_call_arguments.delta":
            summary["preview"] = str(event_payload.get("delta") or "")[-200:]
        elif event_type == "response.function_call_arguments.done":
            summary["arguments"] = str(event_payload.get("arguments") or "")
        elif event_type in {"response.output_item.added", "response.output_item.done"}:
            item = event_payload.get("item") if isinstance(event_payload.get("item"), dict) else {}
            summary["item_type"] = item.get("type")
            summary["name"] = item.get("name")
        elif event_type in {"response.completed", "response.failed", "response.in_progress", "response.created"}:
            response = event_payload.get("response") if isinstance(event_payload.get("response"), dict) else {}
            summary["response_status"] = response.get("status")
            if response.get("error") is not None:
                summary["error"] = response.get("error")
        return summary

    @abstractmethod
    def tool_specs(self) -> list[dict[str, Any]]:
        raise NotImplementedError

    def _request_headers(self, session_id: str | None) -> dict[str, str]:
        headers = dict(self.headers)
        if session_id:
            headers[str(self.target.get("session_header_name") or "X-Session-Id")] = session_id
        return json_headers(headers)

    def _build_request_spec(
        self,
        *,
        prompt: str,
        state: DriverRunState,
        input_items: list[dict[str, Any]] | None = None,
    ) -> RequestSpec:
        payload = build_responses_request_body(
            prompt=prompt,
            previous_response_id=state.previous_response_id,
            input_items=input_items,
        )
        payload["tools"] = self.tool_specs()
        return RequestSpec(
            method="POST",
            url=self.response_url,
            headers=self._request_headers(state.session_id),
            payload=payload,
        )

    def build_initial_request(self, prompt: str, state: DriverRunState) -> RequestSpec:
        state.session_id = f"sess_{uuid.uuid4().hex}"
        return self._build_request_spec(prompt=prompt, state=state)

    def extract_candidate_fragments(self, raw_response: dict[str, Any]) -> list[dict[str, Any]]:
        fragments: list[dict[str, Any]] = []
        for item in raw_response.get("output") or []:
            if not isinstance(item, dict):
                continue
            item_type = str(item.get("type") or "").strip()
            if item_type == "function_call":
                fragments.append(
                    {
                        "kind": "function_call",
                        "name": item.get("name"),
                        "call_id": item.get("call_id"),
                        "arguments": item.get("arguments"),
                        "payload": item,
                    }
                )
            elif item_type == "message":
                fragments.append(
                    {
                        "kind": "message",
                        "role": item.get("role"),
                        "content": item.get("content"),
                        "payload": item,
                    }
                )
        return fragments

    @abstractmethod
    def parse_interaction_event(
        self,
        candidate_fragments: list[dict[str, Any]],
        raw_response: dict[str, Any],
    ) -> DriverEvent:
        raise NotImplementedError

    def parse_response(self, raw_response: dict[str, Any], state: DriverRunState) -> DriverEvent:
        state.previous_response_id = str(raw_response.get("id") or "").strip() or state.previous_response_id
        if raw_response.get("error"):
            return DriverEvent(
                event_type="terminal_error",
                raw_name=str((raw_response.get("error") or {}).get("code") or "response_error"),
                raw_payload=raw_response.get("error"),
            )
        candidate_fragments = self.extract_candidate_fragments(raw_response)
        message_text = "\n\n".join(extract_responses_message_texts(raw_response)).strip()
        if message_text:
            clarification_calls = [
                fragment
                for fragment in candidate_fragments
                if fragment.get("kind") == "function_call" and looks_like_clarification(fragment)
            ]
            if not clarification_calls:
                return DriverEvent(
                    event_type="final_answer",
                    answer=message_text,
                    raw_name="message",
                    raw_payload=candidate_fragments,
                )
        return self.parse_interaction_event(candidate_fragments, raw_response)

    def build_followup_request(
        self,
        *,
        event: DriverEvent,
        user_reply: SimulatedUserReply,
        state: DriverRunState,
    ) -> RequestSpec:
        raw_payload = event.raw_payload[0] if isinstance(event.raw_payload, list) and event.raw_payload else {}
        if event.reply_mode == "function_call_output":
            input_items: list[dict[str, Any]] = [
                {
                    "type": "function_call_output",
                    "call_id": raw_payload.get("call_id"),
                    "output": user_reply.answer,
                }
            ]
        else:
            input_items = [
                {
                    "type": "message",
                    "role": "user",
                    "content": [{"type": "input_text", "text": user_reply.answer}],
                }
            ]
        return self._build_request_spec(
            prompt=str(self.context_vars.get("entry_question") or ""),
            state=state,
            input_items=input_items,
        )

    def extract_final_answer(self, raw_response: dict[str, Any], state: DriverRunState) -> str | None:
        answer = "\n\n".join(extract_responses_message_texts(raw_response)).strip()
        return answer or None

    def extract_backend_ids(self, raw_response: dict[str, Any], state: DriverRunState) -> dict[str, Any]:
        return {
            "session_id": state.session_id,
            "response_id": raw_response.get("id"),
        }

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
        return {
            "turn": state.turn_index,
            "request_input": prompt,
            "status_code": status_code,
            "response_id": raw_response.get("id"),
            "session_id": state.session_id,
            "status": raw_response.get("status"),
            "asks": [],
            "driver_event": {
                "event_type": event.event_type,
                "question": event.question,
                "options": event.options,
                "slot": event.slot,
                "answer": event.answer,
                "raw_name": event.raw_name,
            }
            if event
            else None,
            "stream_event_count": len(self._last_sse_events),
            "stream_event_types": [
                str(item.get("type") or "").strip()
                for item in self._last_sse_events
                if str(item.get("type") or "").strip()
            ],
            "final_answer": self.extract_final_answer(raw_response, state) or "",
            "raw_output": raw_response.get("output"),
            "error": raw_response.get("error"),
        }
