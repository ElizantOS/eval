from __future__ import annotations

from typing import Any
from urllib.parse import urljoin

from .runner_common import (
    DriverEvent,
    DriverRunState,
    RequestSpec,
    SimulatedUserReply,
    TargetDriver,
    build_auth_headers,
    call_json_model,
    json_headers,
    maybe_switch_provider,
)


class ChatCompletionsDriverBase(TargetDriver):
    def __init__(self, *, target: dict[str, Any], context_vars: dict[str, Any]) -> None:
        super().__init__(target=target, context_vars=context_vars)
        self.base_url = str(target.get("base_url") or "").rstrip("/")
        self.headers = build_auth_headers(target)
        maybe_switch_provider(target=target, headers=self.headers)
        self.chat_url = urljoin(f"{self.base_url}/", "v1/chat/completions")

    def tool_specs(self) -> list[dict[str, Any]]:
        return [
            {
                "type": "function",
                "function": {
                    "name": "ask_clarification",
                    "description": "向用户提澄清问题",
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "question": {"type": "string"},
                            "options": {"type": "array", "items": {"type": "string"}},
                            "slot": {"type": "string"},
                        },
                        "required": ["question", "options"],
                    },
                },
            }
        ]

    def build_initial_request(self, prompt: str, state: DriverRunState) -> RequestSpec:
        messages = [{"role": "user", "content": prompt}]
        state.driver_metadata["messages"] = messages
        payload = {"messages": messages, "tools": self.tool_specs(), "tool_choice": "auto", "stream": False}
        return RequestSpec(method="POST", url=self.chat_url, headers=json_headers(self.headers), payload=payload)

    def parse_response(self, raw_response: dict[str, Any], state: DriverRunState) -> DriverEvent:
        choices = raw_response.get("choices")
        message = (choices or [{}])[0].get("message") if isinstance(choices, list) and choices else {}
        if not isinstance(message, dict):
            return DriverEvent(event_type="terminal_error", answer="chat_completions response missing message", raw_payload=raw_response)
        tool_calls = message.get("tool_calls") if isinstance(message.get("tool_calls"), list) else []
        if not tool_calls:
            return DriverEvent(
                event_type="final_answer",
                answer=str(message.get("content") or "").strip() or None,
                raw_name="assistant_message",
                raw_payload=raw_response,
            )
        parsed = call_json_model(
            system_prompt=(
                "你是 target driver 的交互解释器。"
                "请把 chat_completions 的 tool_calls 归一成 JSON，字段固定为 "
                "event_type, question, options, slot, answer, raw_name。"
                "event_type 只能是 clarification_request, final_answer, intermediate_message, tool_call, terminal_error。"
                "如果这是向用户索要缺失信息的交互，不管原名字是什么，都输出 clarification_request。"
            ),
            user_payload={"tool_calls": tool_calls, "message": message},
        )
        state.driver_metadata["last_assistant_message"] = message
        return DriverEvent(
            event_type=str(parsed.get("event_type") or "terminal_error").strip(),
            question=str(parsed.get("question") or "").strip() or None,
            options=[str(item).strip() for item in parsed.get("options", []) if str(item).strip()] if isinstance(parsed.get("options"), list) else [],
            slot=str(parsed.get("slot") or "").strip() or None,
            answer=str(parsed.get("answer") or "").strip() or None,
            raw_name=str(parsed.get("raw_name") or "").strip() or None,
            raw_payload=tool_calls,
            reply_mode="tool_message",
        )

    def build_followup_request(
        self,
        *,
        event: DriverEvent,
        user_reply: SimulatedUserReply,
        state: DriverRunState,
    ) -> RequestSpec:
        messages = list(state.driver_metadata.get("messages") or [])
        assistant_message = state.driver_metadata.get("last_assistant_message")
        if assistant_message:
            messages.append(assistant_message)
        tool_calls = event.raw_payload if isinstance(event.raw_payload, list) else []
        tool_call_id = tool_calls[0].get("id") if tool_calls and isinstance(tool_calls[0], dict) else None
        messages.append(
            {
                "role": "tool",
                "tool_call_id": tool_call_id,
                "content": user_reply.answer,
            }
        )
        state.driver_metadata["messages"] = messages
        payload = {"messages": messages, "tools": self.tool_specs(), "tool_choice": "auto", "stream": False}
        return RequestSpec(method="POST", url=self.chat_url, headers=json_headers(self.headers), payload=payload)

    def extract_final_answer(self, raw_response: dict[str, Any], state: DriverRunState) -> str | None:
        choices = raw_response.get("choices")
        message = (choices or [{}])[0].get("message") if isinstance(choices, list) and choices else {}
        if not isinstance(message, dict):
            return None
        answer = str(message.get("content") or "").strip()
        return answer or None

    def extract_backend_ids(self, raw_response: dict[str, Any], state: DriverRunState) -> dict[str, Any]:
        return {"session_id": None, "response_id": None}

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
            "response_id": None,
            "session_id": None,
            "status": "completed" if status_code < 400 else "failed",
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
            "final_answer": self.extract_final_answer(raw_response, state) or "",
            "raw_output": raw_response,
            "error": raw_response.get("error") if isinstance(raw_response, dict) else None,
        }
