#!/usr/bin/env python3
"""
Hook: block git commit/push unless Claude has been explicitly instructed.

How it works:
- Any Bash call containing "git commit" or "git push" is intercepted.
- If the command is prefixed with CLAUDE_COMMIT_AUTHORIZED=1, it passes.
- Otherwise it is blocked and Claude receives instructions to check whether
  it was explicitly told to commit/push, and to retry with the prefix if so.
"""

import json
import sys

data = json.load(sys.stdin)
cmd = data.get("command", "")

GIT_WRITE_OPS = ["git commit", "git push"]

if any(op in cmd for op in GIT_WRITE_OPS):
    if "CLAUDE_COMMIT_AUTHORIZED=1" in cmd:
        sys.exit(0)  # explicitly authorised — allow
    print(
        "BLOCKED: git commit/push requires explicit user instruction.\n"
        "\n"
        "Before retrying, check the conversation: did the user explicitly say\n"
        "to commit or push in this turn (e.g. 'commit', 'commit and push',\n"
        "'yes go ahead and commit')?\n"
        "\n"
        "- If YES: retry with CLAUDE_COMMIT_AUTHORIZED=1 prepended to the git\n"
        "  command, e.g.:\n"
        "    CLAUDE_COMMIT_AUTHORIZED=1 git commit -m '...'\n"
        "    CLAUDE_COMMIT_AUTHORIZED=1 git push\n"
        "\n"
        "- If NO: do not commit. Stop and wait for the user to instruct you."
    )
    sys.exit(1)

sys.exit(0)
