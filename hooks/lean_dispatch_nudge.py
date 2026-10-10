#!/usr/bin/env python3
"""PreToolUse hook for the Agent tool: steer general-purpose dispatches to lean agents.

When Claude is about to start a general-purpose subagent (subagent_type missing or
"general-purpose"), this hook adds a short note to Claude's context suggesting
lean-worker or lean-checker instead. A description that names an MCP tool or a skill
("mcp__...", "MCP", "skill") is left alone, because lean agents have neither.

Environment switches:
    LEAN_DISPATCH_BLOCK=1     block the dispatch instead (exit 2, reason on stderr)
    LEAN_DISPATCH_ADVISORY=0  turn the hook off

Reads the hook payload on stdin. Any parse error exits 0 so a hook bug never blocks you.
"""
from __future__ import annotations

import json
import os
import re
import sys
from typing import Any

AGENT_TOOLS = {"Agent", "Task"}
NEEDS_FULL = re.compile(r"mcp__|\bmcps?\b|\bskills?\b", re.IGNORECASE)
MESSAGE = (
    "lean-dispatch: a general-purpose subagent loads every CLAUDE.md file, the skills "
    "list and every MCP tool before it reads its brief. If this task needs only files, "
    "shell and the web, dispatch subagent_type lean-worker (build, edit, run) or "
    "lean-checker (blind QA, review, re-check) instead. Keep general-purpose only when "
    "the task needs an MCP tool or a skill, and name it in the description."
)


def finding(payload: Any) -> str | None:
    """Return the advisory text when the payload starts a general-purpose agent."""
    if not isinstance(payload, dict):
        return None
    if payload.get("tool_name") not in AGENT_TOOLS:
        return None
    tool_input = payload.get("tool_input")
    if not isinstance(tool_input, dict):
        return None
    agent_type = tool_input.get("subagent_type")
    if not isinstance(agent_type, str) or not agent_type.strip():
        agent_type = "general-purpose"
    if agent_type.strip() != "general-purpose":
        return None
    description = tool_input.get("description")
    if isinstance(description, str) and NEEDS_FULL.search(description):
        return None
    return MESSAGE


def main() -> int:
    if os.environ.get("LEAN_DISPATCH_ADVISORY") == "0":
        return 0
    try:
        payload = json.loads(sys.stdin.read() or "{}")
    except (json.JSONDecodeError, UnicodeDecodeError):
        return 0
    note = finding(payload)
    if note is None:
        return 0
    if os.environ.get("LEAN_DISPATCH_BLOCK") == "1":
        print(note, file=sys.stderr)
        return 2
    out = {"hookSpecificOutput": {"hookEventName": "PreToolUse", "additionalContext": note}}
    print(json.dumps(out))
    return 0


if __name__ == "__main__":
    sys.exit(main())
