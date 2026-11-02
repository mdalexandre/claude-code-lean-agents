"""Synthetic environment for the ARI 3.0 tests: skills, transcripts, an audit, a fake model.

Six skills, each built to land on one conclusion:
    alpha  dormant          distinct description, nobody asks for it
    beta   unrouted_demand  three prompts use its distinctive words
    gamma  redundant        description nearly equal to delta's (and nobody asks: ambiguous)
    delta  invoked          the overlap partner, used normally
    eps    telemetry        raw Skill calls the (synthetic) audit did not count
    zeta   indirect         its files were read, never invoked; then dormant
Three sessions repeat one correction ("you forgot the linter before committing").
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

from ari_fixtures import asst, usage, write_jsonl

NOW = "2026-11-01T00:00:00Z"
SKILLS = {
    "alpha": "Render orbital telescope spectrograms into sonar charts",
    "beta": "Reconcile quarterly ledger invoices against vendor statements",
    "gamma": "Translate legal contracts into plain summaries quickly",
    "delta": "Translate legal contracts into plain summaries",
    "eps": "Draft haiku about mountains",
    "zeta": "Compress archive bundles for cold storage",
}
CORRECTIONS = ["no, you forgot the linter before committing",
               "you forgot to run the linter before committing again",
               "wrong, run the linter before committing please"]


def user(ts: str, text: str, sid: str) -> dict[str, Any]:
    return {"type": "user", "timestamp": ts, "sessionId": sid,
            "message": {"role": "user", "content": text}}


def compact_summary(ts: str, sid: str) -> dict[str, Any]:
    rec = user(ts, "This session is being continued from a previous conversation. We discussed "
               "how to render orbital telescope spectrograms into sonar charts.", sid)
    return rec | {"isCompactSummary": True, "isVisibleInTranscriptOnly": True}


def skill_call(ts: str, rid: str, name: str) -> dict[str, Any]:
    return asst(rid, ts, usage(1), content=[{"type": "tool_use", "id": "t" + rid,
                                              "name": "Skill", "input": {"skill": name}}])


def build(tmp: Path) -> dict[str, Path]:
    skills = tmp / "skills"
    for name, desc in SKILLS.items():
        (skills / name).mkdir(parents=True)
        (skills / name / "SKILL.md").write_text(f"---\nname: {name}\ndescription: {desc}\n---\n")
    root = tmp / "root" / "proj"
    for i in range(3):
        d = f"2026-10-2{i}"
        write_jsonl(root / f"s{i}.jsonl", [
            user(f"{d}T09:00:00Z", "please reconcile the quarterly ledger for march", f"s{i}"),
            asst(f"a{i}", f"{d}T09:01:00Z", usage(1)),
            user(f"{d}T09:02:00Z", CORRECTIONS[i], f"s{i}"),
            asst(f"b{i}", f"{d}T09:03:00Z", usage(1)),
            skill_call(f"{d}T09:04:00Z", f"e{i}", "eps"),
            # an escaped mention inside a prompt is not an invocation
            user(f"{d}T09:05:00Z", 'see {"type": "tool_use", "name": "Skill", '
                 '"input": {"skill": "alpha"}}', f"s{i}"),
            # Claude Code writes its compaction summary as a user line; nobody typed it
            compact_summary(f"{d}T09:06:00Z", f"s{i}"),
        ])
    return {"skills": skills, "root": tmp / "root"}


def audit(eps_parsed: int = 0) -> dict[str, Any]:
    def cap(name: str, status: str, inv: int = 0, ind: int = 0) -> dict[str, Any]:
        return {"name": name, "category": "skill", "status": status, "invocations": inv,
                "indirect_uses": ind, "exposed_sessions": 30, "listing_chars": 400}
    return {"schema": "ari-capability-audit/1", "coverage": {"unread_gaps": 0},
            "capabilities": [cap("alpha", "EXPOSED_UNOBSERVED"),
                             cap("beta", "EXPOSED_UNOBSERVED"),
                             cap("gamma", "EXPOSED_UNOBSERVED"),
                             cap("delta", "INVOKED", inv=5),
                             cap("eps", "EXPOSED_UNOBSERVED", inv=eps_parsed),
                             cap("zeta", "INDIRECT_ONLY", ind=2)],
            "recommendations": []}


class FakeObserver:
    """Stands in for observer.observe; the digest is whatever the test sets."""

    def __init__(self, digest: str = "d1", tools_ok: bool = True) -> None:
        self.digest, self.tools_ok, self.calls = digest, tools_ok, 0
        self.audit: dict[str, Any] | None = audit()
        self.audit_ok = True

    def __call__(self, out_dir: Path, *a: Any, **k: Any) -> dict[str, Any]:
        self.calls += 1
        tools = {"audit": {"ok": self.audit_ok, "error": "" if self.audit_ok else "boom"},
                 "burn": {"ok": self.tools_ok, "error": "" if self.tools_ok else "boom"}}
        return {"tools": tools, "audit": self.audit if self.audit_ok else None,
                "signals": {"burn": {"observed": 1000, "forecast_status": "OK", "point": 2000}},
                "digest": hashlib.sha256(self.digest.encode()).hexdigest()}


class FakeModel:
    """Stands in for `claude -p`; replies with proposals built from the packet."""

    def __init__(self, reply: Any = None, ok: bool = True, cost: float = 0.01) -> None:
        self.reply, self.ok, self.cost, self.calls = reply, ok, cost, []

    def __call__(self, cmd: list[str], stdin: str, timeout: float,
                 cwd: Path) -> tuple[int, str, str]:
        pkt = json.loads(stdin)
        self.calls.append({"cmd": cmd, "packet": pkt})
        if not self.ok:
            return 1, "", "model unavailable"
        props = self.reply(pkt) if callable(self.reply) else (self.reply or [])
        env = {"type": "result", "is_error": False, "total_cost_usd": self.cost,
               "usage": {"input_tokens": 100, "output_tokens": 20},
               "structured_output": {"proposals": props}}
        return 0, json.dumps(env), ""
