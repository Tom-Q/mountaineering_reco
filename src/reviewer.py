"""Post-hoc accuracy reviewer for full route reports."""

import json
from pathlib import Path

from src.client import _get_client

_HAIKU_MODEL = "claude-haiku-4-5-20251001"
_REVIEWER_PROMPT = (Path(__file__).parent.parent / "prompts" / "route_reviewer.md").read_text()


def review_route_analysis(analysis: str, source_data: str) -> dict:
    """
    Run a Haiku reviewer pass on a completed route analysis.

    Returns {"verdict": "pass"|"revise", "issues": [...], "revised_output": str|None}
    """
    msg = (
        "## Source data\n\n" + source_data +
        "\n\n---\n\n## Analysis to review\n\n" + analysis
    )
    response = _get_client().messages.create(
        model=_HAIKU_MODEL,
        max_tokens=4096,
        system=_REVIEWER_PROMPT,
        messages=[{"role": "user", "content": msg}],
    )
    return json.loads(response.content[0].text)
