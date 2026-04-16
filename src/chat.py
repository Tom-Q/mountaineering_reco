"""
Agentic chat assistant via the Anthropic API.

Implements the streaming multi-turn loop with tool use. Route analysis
(single-shot batch call) lives in src/analysis.py.
"""

import json
from collections.abc import Generator
from datetime import date
from pathlib import Path

from src.client import _get_client

_PROMPTS_DIR = Path(__file__).parent.parent / "prompts"
_CHAT_MODEL = "claude-sonnet-4-6"
_ALPINIST_CHAT_SYSTEM = (_PROMPTS_DIR / "alpinist_chat.md").read_text()

_MAX_CHAT_TURNS = 40


def _format_profile(user_params: dict) -> str:
    """Format the climber's grade profile as a system-prompt block."""
    mapping = [
        ("rock_onsight", "Rock onsight"),
        ("rock_trad",    "Rock trad"),
        ("ice_max",      "Ice"),
        ("mixed_max",    "Mixed"),
        ("alpine_max",   "Alpine"),
    ]
    lines = ["## Climber profile (baseline — user may adjust in conversation)"]
    for key, label in mapping:
        val = user_params.get(key)
        if val:
            lines.append(f"- {label}: {val}")
    return "\n".join(lines)


def chat_alpinist(
    api_messages: list[dict],
    today: date,
    user_params: dict | None = None,
) -> Generator[dict, None, None]:
    """
    Agentic chat loop with tool support. Yields typed event dicts:

        {"type": "text",       "text": str}
        {"type": "tool_start", "name": str, "input": dict}
        {"type": "tool_end",   "name": str, "error": str | None}
        {"type": "done",       "new_api_messages": list[dict]}

    api_messages must already include the latest user message as the last
    element. The caller appends new_api_messages to its own copy on "done".
    user_params: sidebar grade profile dict; injected into the system prompt.
    """
    from src.tools import ALL_TOOLS, dispatch_tool

    profile_block = _format_profile(user_params) + "\n\n" if user_params else ""
    system = f"Today's date: {today.isoformat()}\n\n{profile_block}{_ALPINIST_CHAT_SYSTEM}"
    working = list(api_messages[-_MAX_CHAT_TURNS:])
    new_messages: list[dict] = []

    while True:
        with _get_client().messages.stream(
            model=_CHAT_MODEL,
            max_tokens=4096,
            system=system,
            messages=working,
            tools=ALL_TOOLS,
        ) as stream:
            for chunk in stream.text_stream:
                yield {"type": "text", "text": chunk}
            final = stream.get_final_message()

        # Serialise content blocks for session state storage
        content_dicts = []
        for block in final.content:
            if block.type == "text":
                content_dicts.append({"type": "text", "text": block.text})
            elif block.type == "tool_use":
                content_dicts.append({
                    "type": "tool_use",
                    "id": block.id,
                    "name": block.name,
                    "input": block.input,
                })

        assistant_msg = {"role": "assistant", "content": content_dicts}
        working.append(assistant_msg)
        new_messages.append(assistant_msg)

        tool_blocks = [b for b in final.content if b.type == "tool_use"]
        if not tool_blocks:
            yield {"type": "done", "new_api_messages": new_messages}
            return

        # Dispatch each tool call and collect results
        tool_results = []
        for block in tool_blocks:
            yield {"type": "tool_start", "name": block.name, "input": block.input}
            error = None
            try:
                result = dispatch_tool(block.name, block.input)
                result_str = json.dumps(result)
            except Exception as e:
                result_str = json.dumps({"error": str(e)})
                error = str(e)
            yield {"type": "tool_end", "name": block.name, "error": error}
            tool_results.append({
                "type": "tool_result",
                "tool_use_id": block.id,
                "content": result_str,
            })

        tool_msg = {"role": "user", "content": tool_results}
        working.append(tool_msg)
        new_messages.append(tool_msg)
        yield {"type": "text", "text": "\n\n"}
