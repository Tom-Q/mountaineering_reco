#!/usr/bin/env python3
"""
Safety hook: blocks or requires explicit authorization for dangerous Bash commands.

Rules enforced:
1. git commit / git push — require CLAUDE_COMMIT_AUTHORIZED=1 prefix
2. rm / rmdir / shred  — blocked outright
3. python scripts/*.py — require CLAUDE_SCRIPT_AUTHORIZED=1 prefix
   (these make external API calls and can overwrite DB data)
4. Writes to paths outside the project dirs — blocked
   Allowed roots: /home/thomas/mountaineering_reco, /home/thomas/mountaineering_scraper
"""

import json
import re
import sys

data = json.load(sys.stdin)
cmd = data.get("tool_input", {}).get("command", "") or data.get("command", "")

ALLOWED_ROOTS = (
    "/home/thomas/mountaineering_reco",
    "/home/thomas/mountaineering_scraper",
)

# ── 1. git commit / push ─────────────────────────────────────────────────────
GIT_WRITE_OPS = ["git commit", "git push"]
if any(op in cmd for op in GIT_WRITE_OPS):
    if "CLAUDE_COMMIT_AUTHORIZED=1" in cmd:
        sys.exit(0)
    print(
        "BLOCKED: git commit/push requires explicit user instruction.\n"
        "\n"
        "Did the user explicitly say to commit or push in this turn?\n"
        "- YES: retry with CLAUDE_COMMIT_AUTHORIZED=1 prepended.\n"
        "- NO:  stop and wait."
    )
    sys.exit(1)

# ── 2. Destructive file operations ───────────────────────────────────────────
DESTRUCTIVE = re.compile(r'\brm\b|\brmdir\b|\bshred\b')
if DESTRUCTIVE.search(cmd):
    print(
        "BLOCKED: rm/rmdir/shred requires explicit user confirmation.\n"
        "Stop and ask the user before deleting anything."
    )
    sys.exit(1)

# ── 3. Data collection / enrichment scripts ──────────────────────────────────
if re.search(r'\bpython[0-9.]?\s+scripts/', cmd):
    if "CLAUDE_SCRIPT_AUTHORIZED=1" in cmd:
        sys.exit(0)
    print(
        "BLOCKED: running scripts/ requires explicit user instruction.\n"
        "\n"
        "These scripts make external API calls and can overwrite DB data.\n"
        "Did the user explicitly ask you to run this script in this turn?\n"
        "- YES: retry with CLAUDE_SCRIPT_AUTHORIZED=1 prepended.\n"
        "- NO:  stop and wait."
    )
    sys.exit(1)

# ── 4. Writes to paths outside allowed project roots ─────────────────────────
# Look for shell redirections and common write-to-path patterns pointing outside.
REDIRECT = re.compile(r'[>|]\s*(/[^\s;|&]+)')
TEE = re.compile(r'\btee\s+(/[^\s;|&]+)')

suspect_paths = REDIRECT.findall(cmd) + TEE.findall(cmd)
for path in suspect_paths:
    if not any(path.startswith(root) for root in ALLOWED_ROOTS):
        print(
            f"BLOCKED: attempt to write to '{path}' which is outside the allowed "
            f"project directories.\n"
            f"Allowed roots: {', '.join(ALLOWED_ROOTS)}\n"
            f"Write output to a path inside the project instead."
        )
        sys.exit(1)

sys.exit(0)
