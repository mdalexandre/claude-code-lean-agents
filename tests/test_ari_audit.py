from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path

import pytest
from ari import audit
from ari.evidence import ingest
from ari_fixtures import SECRET, build, mcp_config

T0 = datetime(2026, 10, 20, tzinfo=timezone.utc)
T1 = datetime(2026, 11, 1, tzinfo=timezone.utc)


@pytest.fixture()
def env(tmp_path: Path) -> dict[str, Path]:
    build(tmp_path / "root", tmp_path / "skills", tmp_path / "outside")
    return {"root": tmp_path / "root", "skills": tmp_path / "skills",
            "cfg": mcp_config(tmp_path / "claude.json"), "tmp": tmp_path}


def _run(env: dict[str, Path], **kw: float | bool) -> dict[str, object]:
    ev = ingest([env["root"]], since=T0, until=T1, want_tools=True, want_exposure=True,
                skills_roots=[env["skills"]])
    return audit.audit(ev, [str(env["skills"])], [str(env["cfg"])], window_days=12, **kw)


def _caps(r: dict[str, object]) -> dict[tuple[str, str], dict[str, object]]:
    return {(c["category"], c["name"]): c for c in r["capabilities"]}  # type: ignore[index,union-attr]


def test_inventory_exposure_invocation_distinctions(env: dict[str, Path]) -> None:
    caps = _caps(_run(env))
    assert caps[("skill", "alpha")]["status"] == "INVOKED"  # slash command
    assert caps[("skill", "beta")]["status"] == "INVOKED"  # Skill tool
    assert caps[("skill", "gamma")]["status"] == "EXPOSED_UNOBSERVED"
    assert caps[("skill", "delta")]["status"] == "INSTALLED_NOT_SEEN_EXPOSED"
    assert caps[("skill", "epsilon")]["status"] == "INDIRECT_ONLY"  # SKILL.md was read
    assert caps[("mcp_server", "srv1")]["status"] == "INVOKED"
    assert caps[("mcp_server", "srv2")]["status"] == "EXPOSED_UNOBSERVED"
    assert caps[("mcp_server", "idle")]["status"] == "INSTALLED_NOT_SEEN_EXPOSED"
    assert caps[("mcp_server", "srv2")]["installed"] is True  # project block counts


def test_unsupported_removal_is_rejected(env: dict[str, Path]) -> None:
    recs = {(r["category"], r["target"]): r for r in _run(env)["recommendations"]}  # type: ignore[union-attr]
    g = recs[("skill", "gamma")]
    assert g["action"] == "INSUFFICIENT_EVIDENCE" and "sessions" in g["rejected"]
    s2 = recs[("mcp_server", "srv2")]
    assert s2["action"] == "INSUFFICIENT_EVIDENCE"
    assert recs[("skill", "delta")]["action"] == "UNKNOWN"
    assert not any(r["action"] in ("CONSIDER_REMOVAL", "RESTRICT_EXPOSURE")
                   for r in recs.values())


def test_supported_removal_carries_all_fields(env: dict[str, Path]) -> None:
    # the fixture holds an unreadable line and a partial last line on purpose
    blocked = {(x["category"], x["target"]): x for x in _run(env, min_sessions=1, min_days=1)[
        "recommendations"]}  # type: ignore[union-attr]
    assert blocked[("skill", "gamma")]["action"] == "INVESTIGATE"
    assert "unread" in blocked[("skill", "gamma")]["rejected"]
    r = _run(env, min_sessions=1, min_days=1, accept_coverage_gaps=True)
    recs = {(x["category"], x["target"]): x for x in r["recommendations"]}  # type: ignore[union-attr]
    g = recs[("skill", "gamma")]
    assert g["action"] == "CONSIDER_REMOVAL" and g["rejected"] is None
    for f in audit.REQUIRED_FIELDS:
        assert g[f]
    assert recs[("mcp_server", "srv2")]["action"] == "RESTRICT_EXPOSURE"


def test_validator_rejects_bad_recommendations() -> None:
    cap = audit.Capability("x", "skill", installed=True, exposed_sessions=50)
    bare = audit.Recommendation("x", "skill", "CONSIDER_REMOVAL", "r")
    assert "missing" in (audit.validate_recommendation(bare, cap, 30, 20, 14) or "")
    full = audit.Recommendation("x", "skill", "CONSIDER_REMOVAL", "r", "b", ["e"], "risk", "v")
    assert audit.validate_recommendation(full, cap, 30, 20, 14) is None
    cap.indirect_uses = 1
    assert "used" in (audit.validate_recommendation(full, cap, 30, 20, 14) or "")
    cap.indirect_uses = 0
    assert "window" in (audit.validate_recommendation(full, cap, 3, 20, 14) or "")
    cap.installed = False
    assert "not installed" in (audit.validate_recommendation(full, cap, 30, 20, 14) or "")
    assert audit.validate_recommendation(
        audit.Recommendation("x", "skill", "KEEP", "r"), None, 0, 20, 14) is None


def test_consolidation_of_identical_tool_sets(env: dict[str, Path]) -> None:
    # ARI2-R9: CONSOLIDATE is removal class; the fixture holds unread gaps on purpose
    r = _run(env)
    cons = [x for x in r["recommendations"] if x["target"] == "srv1,srv2"]  # type: ignore[index,union-attr]
    assert cons and cons[0]["action"] == "INVESTIGATE"  # type: ignore[index]
    assert "could hold a use that tells the servers apart" in cons[0]["rejected"]  # type: ignore[index,operator]
    r = _run(env, accept_coverage_gaps=True)
    cons = [x for x in r["recommendations"] if x["target"] == "srv1,srv2"]  # type: ignore[index,union-attr]
    assert cons and cons[0]["action"] == "CONSOLIDATE"  # type: ignore[index]
    f = [x for x in r["findings"] if x["subject"] == "mcp_server:srv1,srv2"]  # type: ignore[index,union-attr]
    assert f and f[0]["decision"] == "CONSOLIDATE"  # type: ignore[index]
    assert {x["id"] for x in f[0]["readings"]} >= {"duplicate", "coverage_gap",  # type: ignore[index]
                                                  "different_targets"}


def test_mcp_config_values_never_leak(env: dict[str, Path]) -> None:
    blob = json.dumps(_run(env))
    assert SECRET not in blob and "run-srv1" not in blob and "example.invalid" not in blob


def test_audit_changes_nothing(env: dict[str, Path]) -> None:
    def digest() -> str:
        h = hashlib.sha256()
        for p in sorted(env["tmp"].rglob("*")):
            if p.is_file() and not p.is_symlink():
                h.update(str(p).encode() + p.read_bytes() + str(p.stat().st_mtime_ns).encode())
        return h.hexdigest()

    before = digest()
    _run(env, min_sessions=1, min_days=1)
    assert digest() == before


def test_display_and_tool_prefix_names_are_one_server(tmp_path: Path) -> None:
    """Exposure says "plugin:gh:gh"; tool calls say mcp__plugin_gh_gh__x. One server."""
    from ari_fixtures import write_jsonl

    root = tmp_path / "r"
    for i in range(3):
        write_jsonl(root / "p" / f"s{i}.jsonl", [
            {"type": "attachment", "timestamp": f"2026-10-2{i}T00:00:00Z",
             "attachment": {"type": "mcp_instructions_delta",
                            "addedNames": ["plugin:gh:gh", "claude.ai Docs"]}},
            {"type": "assistant", "timestamp": f"2026-10-2{i}T00:01:00Z", "requestId": f"q{i}",
             "message": {"id": f"m{i}", "model": "m", "usage": {"input_tokens": 1,
                         "output_tokens": 1},
                         "content": [{"type": "tool_use", "id": f"t{i}",
                                      "name": "mcp__plugin_gh_gh__search", "input": {}}]}}])
    cfg = tmp_path / "c.json"
    cfg.write_text(json.dumps({"mcpServers": {"claude.ai Docs": {"command": "x"}}}))
    ev = ingest([root], want_tools=True, want_exposure=True)
    r = audit.audit(ev, [], [str(cfg)], 30, min_sessions=1, min_days=1)
    caps = _caps(r)
    assert ("mcp_server", "plugin:gh:gh") not in caps
    gh = caps[("mcp_server", "plugin_gh_gh")]
    assert gh["status"] == "INVOKED" and gh["exposed_sessions"] == 3
    docs = caps[("mcp_server", "claude_ai_Docs")]
    assert docs["installed"] is True and docs["exposed_sessions"] == 3
    recs = {(x["category"], x["target"]): x["action"] for x in r["recommendations"]}
    assert recs[("mcp_server", "plugin_gh_gh")] not in ("RESTRICT_EXPOSURE", "CONSIDER_REMOVAL")


def test_drift_between_sessions(tmp_path: Path) -> None:
    from ari_fixtures import write_jsonl

    root = tmp_path / "r"
    for i, (sk, srv) in enumerate([("- a: x\n- b: y", ["s1"]), ("- a: x", ["s1", "s9"])]):
        write_jsonl(root / "p" / f"sess{i}.jsonl", [
            {"type": "attachment", "timestamp": f"2026-10-2{i}T00:00:00Z",
             "attachment": {"type": "skill_listing", "content": sk}},
            {"type": "attachment", "timestamp": f"2026-10-2{i}T00:00:00Z",
             "attachment": {"type": "mcp_instructions_delta", "addedNames": srv}}])
    ev = ingest([root], want_exposure=True)
    d = audit.audit(ev, [], [], 5)["drift"]
    assert d["changes"]["skill"] == {"added": [], "removed": ["b"]}
    assert d["changes"]["mcp_server"] == {"added": ["s9"], "removed": []}


def test_consolidate_second_gate(env: dict[str, Path], monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(audit, "validate_recommendation", lambda *a, **k: None)
    r = _run(env)
    cons = [x for x in r["recommendations"] if x["target"] == "srv1,srv2"]  # type: ignore[index,union-attr]
    assert cons[0]["action"] == "INVESTIGATE"  # type: ignore[index]
    assert cons[0]["rejected"].startswith("finding contract")  # type: ignore[index,union-attr]
