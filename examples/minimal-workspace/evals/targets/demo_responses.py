from __future__ import annotations

from typing import Any

from eval.adapters.responses_driver_base import DriverEvent, ResponsesDriverBase
from eval.adapters.runner_common import call_json_model


class DemoResponsesDriver(ResponsesDriverBase):
    def tool_specs(self) -> list[dict[str, Any]]:
        return [
            {
                "type": "function",
                "name": "ask_clarification",
                "description": "Ask the user for one missing slot",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "question": {"type": "string"},
                        "options": {"type": "array", "items": {"type": "string"}},
                        "slot": {"type": "string"},
                    },
                    "required": ["question", "options"],
                },
            }
        ]

    def parse_interaction_event(
        self,
        candidate_fragments: list[dict[str, Any]],
        raw_response: dict[str, Any],
    ) -> DriverEvent:
        parsed = call_json_model(
            system_prompt=(
                "Normalize a responses API result into JSON with "
                "event_type, question, options, slot, answer, raw_name."
            ),
            user_payload={"candidate_fragments": candidate_fragments, "raw_response": raw_response},
        )
        return DriverEvent(
            event_type=str(parsed.get("event_type") or "terminal_error").strip(),
            question=str(parsed.get("question") or "").strip() or None,
            options=[str(item).strip() for item in parsed.get("options", []) if str(item).strip()]
            if isinstance(parsed.get("options"), list)
            else [],
            slot=str(parsed.get("slot") or "").strip() or None,
            answer=str(parsed.get("answer") or "").strip() or None,
            raw_name=str(parsed.get("raw_name") or "").strip() or None,
            raw_payload=candidate_fragments,
            reply_mode="function_call_output",
        )
