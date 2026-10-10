"""Tests for hooks/lean_dispatch_nudge.py. Synthetic payloads only."""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

HOOK = Path(__file__).resolve().parent.parent / "hooks" / "lean_dispatch_nudge.py"


def run(payload: object, **env: str) -> subprocess.CompletedProcess[str]:
    clean = {k: v for k, v in os.environ.items() if not k.startswith("LEAN_DISPATCH")}
    clean.update(env)
    raw = payload if isinstance(payload, str) else json.dumps(payload)
    return subprocess.run(
        [sys.executable, str(HOOK)],
        input=raw, capture_output=True, text=True, env=clean, timeout=10,
    )


def agent(**tool_input: str) -> dict[str, object]:
    base = {"description": "Build the parser", "prompt": "Write parse.py"}
    return {"tool_name": "Agent", "tool_input": {**base, **tool_input}}


def note(stdout: str) -> str:
    return str(json.loads(stdout)["hookSpecificOutput"]["additionalContext"])


def test_general_purpose_gets_note() -> None:
    r = run(agent(subagent_type="general-purpose"))
    assert r.returncode == 0
    assert "lean-worker" in note(r.stdout)


def test_missing_type_counts_as_general_purpose() -> None:
    r = run(agent())
    assert "lean-checker" in note(r.stdout)


def test_lean_agent_passes_silently() -> None:
    r = run(agent(subagent_type="lean-worker"))
    assert r.returncode == 0 and r.stdout == ""


def test_other_specialist_passes_silently() -> None:
    assert run(agent(subagent_type="Explore")).stdout == ""


def test_description_naming_mcp_or_skill_is_exempt() -> None:
    assert run(agent(subagent_type="general-purpose", description="Query via mcp__db")).stdout == ""
    assert run(agent(subagent_type="general-purpose", description="Run the pdf skill")).stdout == ""


def test_mcp_in_prompt_only_is_not_exempt() -> None:
    r = run(agent(subagent_type="general-purpose", prompt="use any MCP tool"))
    assert "lean-worker" in note(r.stdout)


def test_block_mode_exits_two() -> None:
    r = run(agent(subagent_type="general-purpose"), LEAN_DISPATCH_BLOCK="1")
    assert r.returncode == 2 and "lean-worker" in r.stderr


def test_switch_off() -> None:
    r = run(agent(subagent_type="general-purpose"), LEAN_DISPATCH_ADVISORY="0")
    assert r.returncode == 0 and r.stdout == ""


def test_other_tools_ignored() -> None:
    assert run({"tool_name": "Bash", "tool_input": {"command": "ls"}}).stdout == ""


def test_malformed_input_fails_open() -> None:
    r = run("{not json")
    assert r.returncode == 0 and r.stdout == ""
