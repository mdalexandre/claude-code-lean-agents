"""Observer (H1): run the ARI 2.0 tools, keep their raw output, digest the signals.

Each tool runs as its own process (the same commands a person would type), so ARI 3.0
judges ARI 2.0 through its tested interfaces. A tool that fails is recorded with its exit
code and stderr tail, and the observation continues with what the others produced. The
digest is a hash of the signals; the governor wakes the model only for evidence it has
not already offered at this digest (ARI-02).
"""
from __future__ import annotations

import hashlib
import json
import subprocess
import sys
from pathlib import Path
from typing import Any

from .competence import redact

SCRIPTS = Path(__file__).resolve().parent.parent


def _run(cmd: list[str], timeout: float) -> dict[str, Any]:
    try:
        p = subprocess.run([sys.executable, *cmd], capture_output=True, text=True,
                           encoding="utf-8", errors="replace", timeout=timeout, cwd=SCRIPTS,
                           check=False)
    except subprocess.TimeoutExpired:
        return {"ok": False, "error": f"timeout after {timeout:g} s", "data": None}
    try:
        data = json.loads(p.stdout) if p.stdout.strip() else None
    except (ValueError, RecursionError):  # a reply nested too deep is no reply
        data = None
    ok = p.returncode == 0 and data is not None
    return {"ok": ok, "exit": p.returncode, "data": data,
            "error": "" if ok else (redact(p.stderr.strip()[-600:])[-300:]
                                    or "no JSON on stdout")}


def observe(out_dir: Path, roots: list[str], days: float, now: str | None,
            skills_roots: list[str], mcp_configs: list[str], nightly_dir: Path,
            timeout: float = 1800) -> dict[str, Any]:
    out_dir.mkdir(parents=True, exist_ok=True)
    common = [a for r in roots for a in ("--root", r)] + (["--now", now] if now else [])
    audit_cmd = ["skill_mcp_audit.py", "--json", "--days", f"{days:g}", *common]
    audit_cmd += [a for s in skills_roots for a in ("--skills-root", s)]
    audit_cmd += [a for c in mcp_configs for a in ("--mcp-config", c)]
    tools = {
        "audit": audit_cmd,
        "burn": ["weekly_burn.py", "--json", "--days", f"{days:g}", *common],
        "nightly": ["ari_nightly.py", "run", "--state-dir", str(nightly_dir),
                    "--days", f"{min(days, 14):g}", *common],
    }
    results = {}
    for name, cmd in tools.items():
        r = _run(cmd, timeout)
        if r["data"] is not None:
            (out_dir / f"{name}.json").write_text(json.dumps(r["data"], indent=1,
                                                             sort_keys=True), encoding="utf-8")
        results[name] = r
    signals = _signals(results, nightly_dir)
    digest = hashlib.sha256(json.dumps(signals, sort_keys=True).encode()).hexdigest()
    return {"tools": {k: {"ok": v["ok"], "error": v["error"]} for k, v in results.items()},
            "audit": results["audit"]["data"], "signals": signals, "digest": digest}


def targets(audit: dict[str, Any] | None) -> list[dict[str, Any]]:
    """Skills listed but never seen used, ranked by what their listing costs."""
    out = []
    for c in (audit or {}).get("capabilities", []):
        if (isinstance(c, dict) and c.get("category") == "skill"
                and c.get("status") in ("EXPOSED_UNOBSERVED", "INDIRECT_ONLY")
                and int(c.get("exposed_sessions", 0) or 0) > 0):
            exposed = int(c.get("exposed_sessions", 0) or 0)
            chars = int(c.get("listing_chars", 0) or 0)
            out.append({"name": str(c["name"]), "exposed": exposed, "listing_chars": chars,
                        "cost": exposed * chars})
    return sorted(out, key=lambda t: (-t["cost"], t["name"]))


def _signals(results: dict[str, dict[str, Any]], nightly_dir: Path) -> dict[str, Any]:
    sig: dict[str, Any] = {}
    audit = results["audit"]["data"]
    if isinstance(audit, dict):
        acts: dict[str, int] = {}
        for r in audit.get("recommendations", []):
            acts[r.get("action", "?")] = acts.get(r.get("action", "?"), 0) + 1
        sig["audit_actions"] = acts
        sig["investigate_targets"] = [t["name"] for t in targets(audit)]
        sig["unread_gaps"] = audit.get("coverage", {}).get("unread_gaps")
    burn = results["burn"]["data"]
    if isinstance(burn, dict):
        fc = burn.get("forecast", {})
        sig["burn"] = {"observed": burn.get("observed", {}).get("metric_total"),
                       "forecast_status": fc.get("status"),
                       "point": fc.get("projected_period_total"),
                       "low": fc.get("envelope", {}).get("low"),
                       "high": fc.get("envelope", {}).get("high"),
                       "by_agent": {k: v.get("metric") for k, v in
                                    sorted(burn.get("by_agent_type", {}).items())}}
    try:
        st = json.loads((nightly_dir / "state.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        st = {}
    lessons: dict[str, int] = {}
    for lesson in st.get("lessons", {}).values():
        lessons[lesson.get("state", "?")] = lessons.get(lesson.get("state", "?"), 0) + 1
    sig["lessons"] = lessons
    snaps = st.get("snapshots", {})
    if snaps:
        last = snaps[sorted(snaps)[-1]]
        sig["start_context"] = {k: v.get("median") for k, v in
                                sorted(last.get("start_context", {}).items())}
    return sig
