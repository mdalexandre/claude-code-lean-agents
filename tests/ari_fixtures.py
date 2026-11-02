"""Deterministic transcript fixtures for the ARI tests. Every value is fixed; no clock."""
from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

SECRET = "sk-ant-FIXTURESECRET0123456789"
PROMPT = "PRIVATE PROMPT TEXT about project nebula"


def usage(inp: int, cc: int = 0, cr: int = 0, out: int = 10, c5: int | None = None,
          c1: int | None = None) -> dict[str, Any]:
    u: dict[str, Any] = {"input_tokens": inp, "cache_creation_input_tokens": cc,
                         "cache_read_input_tokens": cr, "output_tokens": out}
    if c5 is not None or c1 is not None:
        u["cache_creation"] = {"ephemeral_5m_input_tokens": c5 or 0,
                               "ephemeral_1h_input_tokens": c1 or 0}
    return u


def asst(rid: str | None, ts: str, u: dict[str, Any] | None, model: str = "claude-sonnet-5-5",
         content: list[dict[str, Any]] | None = None, version: str = "2.1.316") -> dict[str, Any]:
    msg: dict[str, Any] = {"model": model, "content": content or [{"type": "text", "text": "ok"}]}
    if rid is not None:
        msg["id"] = "msg_" + rid
    if u is not None:
        msg["usage"] = u
    rec: dict[str, Any] = {"type": "assistant", "timestamp": ts, "message": msg,
                           "version": version}
    if rid is not None:
        rec["requestId"] = rid
    return rec


def write_jsonl(path: Path, records: list[Any], trailing_partial: str | None = None) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        for r in records:
            fh.write((r if isinstance(r, str) else json.dumps(r)) + "\n")
        if trailing_partial is not None:
            fh.write(trailing_partial)


def build(root: Path, skills_root: Path, outside: Path) -> dict[str, Path]:
    """Write the fixture tree. Returns named paths used by the tests."""
    proj = root / "proj-a"
    main = proj / "sess1.jsonl"
    read_path = str(skills_root / "epsilon" / "SKILL.md")
    tool_content = [
        {"type": "tool_use", "name": "Skill", "input": {"skill": "beta"}},
        {"type": "tool_use", "name": "mcp__srv1__a", "input": {"q": PROMPT}},
        {"type": "tool_use", "name": "Bash", "input": {"command": f"export API_KEY={SECRET}"}},
        {"type": "tool_use", "name": "Read", "input": {"file_path": read_path}},
    ]
    listing = "\n".join([
        "- alpha: first skill", "- beta: second skill", "- gamma (gam): third skill unused"])
    deferred = [f"mcp__{s}__{t}" for s in ("srv1", "srv2") for t in ("a", "b", "c")]
    records: list[Any] = [
        {"type": "user", "timestamp": "2026-10-26T09:00:00Z",
         "message": {"role": "user", "content": PROMPT}},
        {"type": "attachment", "timestamp": "2026-10-26T09:00:01Z",
         "attachment": {"type": "skill_listing", "content": listing}},
        {"type": "attachment", "timestamp": "2026-10-26T09:00:01Z",
         "attachment": {"type": "mcp_instructions_delta", "addedNames": ["srv1", "srv2"]}},
        {"type": "attachment", "timestamp": "2026-10-26T09:00:01Z",
         "attachment": {"type": "deferred_tools_delta", "addedNames": deferred}},
        {"type": "user", "timestamp": "2026-10-26T09:00:02Z",
         "message": {"role": "user",
                     "content": "<command-name>/alpha</command-name> run it"}},
        # r1: one request streamed as three lines with identical usage
        asst("r1", "2026-10-26T09:01:00Z", usage(100, 1000, 0, 5, c5=600, c1=400)),
        asst("r1", "2026-10-26T09:01:00Z", usage(100, 1000, 0, 5, c5=600, c1=400),
             content=tool_content),
        asst("r1", "2026-10-26T09:01:00Z", usage(100, 1000, 0, 5, c5=600, c1=400)),
        # r2: copies disagree on output tokens -> conflict, per field maximum kept
        asst("r2", "2026-10-26T09:02:00Z", usage(50, 0, 2000, 20)),
        asst("r2", "2026-10-26T09:02:00Z", usage(50, 0, 2000, 99)),
        asst(None, "2026-10-26T09:03:00Z", usage(7, 0, 0, 1)),  # missing id
        asst("r3", "2026-10-26T09:04:00Z", usage(9, 0, 0, 1), model="<synthetic>"),
        asst("r4", "not-a-time", usage(9, 0, 0, 1)),
        asst("r5", "2026-10-26T09:05:00Z", usage(-1, 0, 0, 1)),
        "this line is not json",
        asst("r6", "2026-10-26T23:30:00Z", usage(10, 300, 0, 1)),  # no cache split: fallback
    ]
    write_jsonl(main, records, trailing_partial='{"type":"assistant","timestamp":"2026-10-2')
    sub = proj / "sess1" / "subagents"
    # lean-worker run, lines written out of order: the earliest timestamp is the first request
    write_jsonl(sub / "agent-a1.jsonl", [
        asst("s1b", "2026-10-26T10:00:05Z", usage(2, 0, 9000, 30)),
        asst("s1a", "2026-10-26T10:00:00Z", usage(2, 7000, 0, 30)),
    ])
    (sub / "agent-a1.meta.json").write_text(json.dumps({"agentType": "lean-worker"}))
    write_jsonl(sub / "agent-a2.jsonl", [asst("s2a", "2026-10-26T11:00:00Z",
                                              usage(3, 120000, 0, 40))])
    (sub / "agent-a2.meta.json").write_text(
        json.dumps({"agentType": "<img src=x onerror=alert(1)>"}))
    write_jsonl(sub / "agent-a3.jsonl", [asst("s3a", "2026-10-26T12:00:00Z",
                                              usage(4, 50000, 0, 1))])  # no meta file
    write_jsonl(sub / "agent-a4.jsonl", [asst("s4a", "2026-10-26T12:30:00Z",
                                              usage(5, 60000, 0, 1))])
    (sub / "agent-a4.meta.json").write_text(json.dumps({"isBuiltIn": False}))
    # a transcript outside the root, reached through a symlink: must be refused
    outside.mkdir(parents=True, exist_ok=True)
    escaped = outside / "escaped.jsonl"
    write_jsonl(escaped, [asst("x1", "2026-10-26T13:00:00Z", usage(999999, 0, 0, 1))])
    os.symlink(escaped, proj / "sess2.jsonl")
    for name in ("alpha", "beta", "gamma", "delta", "epsilon"):
        d = skills_root / name
        d.mkdir(parents=True, exist_ok=True)
        (d / "SKILL.md").write_text(f"---\nname: {name}\ndescription: test\n---\nbody\n")
    return {"main": main, "sub": sub, "escaped": escaped}


def mcp_config(path: Path) -> Path:
    path.write_text(json.dumps({
        "mcpServers": {"srv1": {"command": "run-srv1", "env": {"TOKEN": SECRET}},
                       "idle": {"command": "x"}},
        "projects": {"/p": {"mcpServers": {"srv2": {"url": "https://example.invalid"}}}},
    }))
    return path
