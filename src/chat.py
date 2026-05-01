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
    grade_mapping = [
        ("rock_onsight", "Rock onsight"),
        ("rock_trad",    "Rock trad"),
        ("ice_max",      "Ice"),
        ("mixed_max",    "Mixed"),
        ("alpine_max",   "Alpine"),
    ]
    lines = ["## Climber profile (baseline — user may adjust in conversation)"]
    for key, label in grade_mapping:
        val = user_params.get(key)
        if val:
            lines.append(f"- {label}: {val}")

    risk_mapping = [
        ("engagement_max", "Max engagement"),
        ("risk_max",       "Max objective risk"),
        ("exposition_max", "Max exposition"),
        ("equipment_min",  "Min equipment in place"),
    ]
    risk_lines = []
    for key, label in risk_mapping:
        val = user_params.get(key)
        if val:
            risk_lines.append(f"- {label}: {val}")
    if risk_lines:
        lines.append("")
        lines.extend(risk_lines)

    return "\n".join(lines)


def _mark_last_message_cached(messages: list[dict]) -> list[dict]:
    """Return a copy of messages with cache_control added to the last message's last content block."""
    if not messages:
        return messages
    last = messages[-1]
    content = last["content"]
    if isinstance(content, str):
        new_content = [{"type": "text", "text": content, "cache_control": {"type": "ephemeral"}}]
    elif isinstance(content, list) and content:
        new_content = [*content[:-1], {**content[-1], "cache_control": {"type": "ephemeral"}}]
    else:
        return messages
    return [*messages[:-1], {**last, "content": new_content}]


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
    system = [{"type": "text",
               "text": f"Today's date: {today.isoformat()}\n\n{profile_block}{_ALPINIST_CHAT_SYSTEM}",
               "cache_control": {"type": "ephemeral"}}]
    working = list(api_messages[-_MAX_CHAT_TURNS:])
    new_messages: list[dict] = []

    while True:
        # Mark last message as cacheable so subsequent calls hit on the full prior context
        cached_working = _mark_last_message_cached(working)
        with _get_client().messages.stream(
            model=_CHAT_MODEL,
            max_tokens=4096,
            system=system,
            messages=cached_working,
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
        parallel = len(tool_blocks) > 1
        for block in tool_blocks:
            yield {"type": "tool_start", "name": block.name, "input": block.input, "parallel": parallel}
            error = None
            try:
                result = dispatch_tool(block.name, block.input)
                # Strip side-channel keys before sending to Claude; yield as
                # a separate event so app.py can populate the image gallery.
                images = result.pop("_images", None)
                image_blobs = result.pop("_image_blobs", None)
                if images is not None or image_blobs is not None:
                    yield {
                        "type": "tool_images",
                        "images": images or [],
                        "image_blobs": image_blobs or {},
                    }
                result_str = json.dumps(result)
            except Exception as e:
                result_str = json.dumps({"error": str(e)})
                error = str(e)
            # Yield the tool result for logging; truncate large payloads.
            yield {
                "type": "tool_end",
                "name": block.name,
                "error": error,
                "result_preview": result_str[:2000] if not error else None,
            }
            tool_results.append({
                "type": "tool_result",
                "tool_use_id": block.id,
                "content": result_str,
            })

        tool_msg = {"role": "user", "content": tool_results}
        working.append(tool_msg)
        new_messages.append(tool_msg)
        yield {"type": "text", "text": "\n\n"}
