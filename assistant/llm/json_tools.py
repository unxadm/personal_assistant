"""JSON-in-prompt tool calling for models without native function calling.

The tool catalogue is described in the system prompt and the model is asked to reply with
a single JSON object: either ``{"tool_calls": [...]}`` or ``{"final": "..."}``.
"""

from __future__ import annotations

import json
from typing import Any

INSTRUCTIONS = """\
# Tools
You can call tools. You do not have native function calling, so you MUST reply with exactly
one JSON object and nothing else, in one of these two forms:

To call one or more tools:
{{"tool_calls": [{{"name": "<tool name>", "arguments": {{...}}}}]}}

To give your final answer to the user:
{{"final": "<your reply>"}}

Available tools (JSON Schema parameters):
{catalogue}
"""


def _catalogue(tools: list[dict[str, Any]]) -> str:
    lines = []
    for tool in tools:
        fn = tool["function"]
        lines.append(
            f"- {fn['name']}: {fn.get('description', '')}\n"
            f"  parameters: {json.dumps(fn.get('parameters', {}), separators=(',', ':'))}"
        )
    return "\n".join(lines)


def to_json_mode_messages(
    messages: list[dict[str, Any]], tools: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    """Rewrite an OpenAI-style message list so it needs no tool-calling support."""
    instructions = INSTRUCTIONS.format(catalogue=_catalogue(tools))
    out: list[dict[str, Any]] = []
    has_system = False
    for msg in messages:
        role = msg["role"]
        if role == "system" and not has_system:
            has_system = True
            out.append({"role": "system", "content": f"{msg['content']}\n\n{instructions}"})
        elif role == "assistant" and msg.get("tool_calls"):
            calls = [
                {
                    "name": c["function"]["name"],
                    "arguments": _loads_lenient(c["function"].get("arguments") or "{}"),
                }
                for c in msg["tool_calls"]
            ]
            out.append({"role": "assistant", "content": json.dumps({"tool_calls": calls})})
        elif role == "tool":
            out.append(
                {
                    "role": "user",
                    "content": f"[tool result: {msg.get('name', 'tool')}]\n{msg['content']}",
                }
            )
        else:
            out.append({"role": role, "content": msg.get("content") or ""})
    if not has_system:
        out.insert(0, {"role": "system", "content": instructions})
    return out


def _loads_lenient(raw: str) -> dict[str, Any]:
    try:
        value = json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        return {}
    return value if isinstance(value, dict) else {}


def parse_json_reply(text: str) -> tuple[str, list[tuple[str, dict[str, Any]]]]:
    """Return ``(final_text, [(tool_name, arguments), ...])`` from a JSON-mode reply.

    Tolerates code fences and prose around the JSON. If no usable object is found the
    whole text is treated as the final answer.
    """
    decoder = json.JSONDecoder()
    pos = text.find("{")
    while pos != -1:
        try:
            obj, _ = decoder.raw_decode(text, pos)
        except json.JSONDecodeError:
            obj = None
        if isinstance(obj, dict):
            if isinstance(obj.get("tool_calls"), list):
                calls = [
                    (c["name"], c.get("arguments") if isinstance(c.get("arguments"), dict) else {})
                    for c in obj["tool_calls"]
                    if isinstance(c, dict) and isinstance(c.get("name"), str)
                ]
                if calls:
                    return "", calls
            if isinstance(obj.get("final"), str):
                return obj["final"], []
        pos = text.find("{", pos + 1)
    return text.strip(), []
