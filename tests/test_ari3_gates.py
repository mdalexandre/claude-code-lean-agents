"""ARI 3.0 gates (ARI-04 to ARI-07). This file is protected: it is pinned with the gates."""
from __future__ import annotations

import fcntl
import json
import os
import shutil
from datetime import datetime, timezone
from pathlib import Path

import pytest
from ari import candidates, governor, investigator, policy, probes, regulator
from ari.discovery import DiscoveryError
from ari.investigate import Investigation
from ari3_env import NOW, FakeModel, FakeObserver, build

T0 = datetime(2026, 11, 1, tzinfo=timezone.utc)


def _cfg(tmp: Path, env: dict[str, Path], obs: FakeObserver, model: FakeModel | None,
         repo: Path | None = None) -> governor.Config:
    return governor.Config(state_dir=tmp / "state", roots=[str(env["root"])],
                           skills_roots=[str(env["skills"])], mcp_configs=[], now=NOW,
                           model=model is not None, runner=model, observe_fn=obs, repo=repo)


# ARI-06: authority isolation -------------------------------------------------------------
def test_model_process_has_no_tools_no_mcp_no_hooks() -> None:
    cmd = investigator.argv(0.5)
    assert cmd[cmd.index("--tools") + 1] == ""
    assert "--strict-mcp-config" in cmd
    assert json.loads(cmd[cmd.index("--mcp-config") + 1]) == {"mcpServers": {}}
    assert json.loads(cmd[cmd.index("--settings") + 1]) == {"disableAllHooks": True}
    assert "--no-session-persistence" in cmd and cmd[cmd.index("--max-budget-usd") + 1] == "0.50"
    assert "--dangerously-skip-permissions" not in cmd and "--allowedTools" not in cmd


def _repo_copy(tmp: Path) -> Path:
    repo = tmp / "repo"
    for f in policy.PROTECTED:
        (repo / f).parent.mkdir(parents=True, exist_ok=True)
        shutil.copy(policy.repo_root() / f, repo / f)
    (repo / policy.PIN_PATH).parent.mkdir(parents=True, exist_ok=True)
    policy.write_pin(repo)
    return repo


def test_the_real_gates_match_their_pin() -> None:
    assert policy.integrity() == []


def test_a_changed_gate_puts_the_operator_in_safe_state(tmp_path: Path) -> None:
    repo = _repo_copy(tmp_path)
    assert policy.integrity(repo) == []
    with open(repo / "scripts/ari/candidates.py", "a") as fh:
        fh.write("\n# relaxed\n")
    env, model = build(tmp_path / "env"), FakeModel()
    out = governor.tick(_cfg(tmp_path, env, FakeObserver(), model, repo))
    assert out["status"] == "SAFE" and model.calls == []
    assert not (tmp_path / "state" / "candidates.json").exists()
    assert "SAFE state" in (tmp_path / "state" / "brief.md").read_text()


def test_a_missing_pin_is_safe_too(tmp_path: Path) -> None:
    repo = _repo_copy(tmp_path)
    (repo / policy.PIN_PATH).unlink()
    assert policy.integrity(repo) == ["pin unreadable: FileNotFoundError"]


# ARI-05: candidate lifecycle ---------------------------------------------------------------
def _cand() -> dict:
    return candidates.make("restrict_exposure", "alpha", "stop listing alpha",
                           {"spec": probes.spec("skill_eligible_prompts", {"skill": "alpha"},
                                                ("==", 0), (">=", 3)), "min_days": 14},
                           {}, 10.0, T0)


def test_no_state_can_be_skipped() -> None:
    c = _cand()
    with pytest.raises(candidates.CandidateError, match="cannot move"):
        candidates.transition(c, "QUALIFIED", "governor", "", T0)
    with pytest.raises(candidates.CandidateError, match="cannot move"):
        candidates.transition(c, "APPROVED", candidates.OWNER, "", T0)


def _pending() -> dict:
    c = _cand()
    for to in ("REPLAYED", "QUALIFIED", "APPROVAL_PENDING"):
        candidates.transition(c, to, "governor", "", T0)
    return c


def test_approval_is_an_owner_transition_at_a_terminal() -> None:
    c = _pending()
    with pytest.raises(candidates.CandidateError, match="owner transition"):
        candidates.transition(c, "APPROVED", "governor", "", T0)
    with pytest.raises(candidates.CandidateError, match="terminal approval"):
        candidates.transition(c, "APPROVED", candidates.OWNER, "", T0, {"terminal": True})
    with pytest.raises(candidates.CandidateError, match="not confirmed"):
        candidates.owner_approve(c, "APPROVED", T0, lambda p, code: False)
    assert c["state"] == "APPROVAL_PENDING"
    candidates.owner_approve(c, "APPROVED", T0, lambda p, code: True)
    assert c["state"] == "APPROVED" and c["approval"]["content_hash"] == c["content_hash"]


def test_an_approval_for_other_content_is_refused() -> None:
    c = _pending()
    other = {"candidate": c["id"], "content_hash": "0" * 64, "terminal": True}
    with pytest.raises(candidates.CandidateError, match="terminal approval"):
        candidates.transition(c, "APPROVED", candidates.OWNER, "", T0, other)


def test_an_approval_does_not_survive_a_content_change() -> None:
    c = _pending()
    candidates.owner_approve(c, "APPROVED", T0, lambda p, code: True)
    c["proposal"] = "remove alpha entirely"
    with pytest.raises(candidates.CandidateError, match="content changed"):
        candidates.owner_approve(c, "CANARY", T0, lambda p, code: True)


def test_without_a_controlling_terminal_approval_is_refused(
        monkeypatch: pytest.MonkeyPatch) -> None:
    def no_tty(*a: object, **k: object) -> int:
        raise OSError("no controlling terminal")
    monkeypatch.setattr(os, "open", no_tty)
    assert candidates.real_terminal("approve?", "abc123") is False
    with pytest.raises(candidates.CandidateError, match="not confirmed"):
        candidates.owner_approve(_pending(), "APPROVED", T0, candidates.real_terminal)


# ARI-04: bounded recursion and validated proposals ---------------------------------------
def _inv() -> Investigation:
    return Investigation.open_skill("alpha", 30)


HYP = {"mechanism": "alpha is listed only in one project", "prediction": "few sessions",
       "falsification": "listed everywhere",
       "probe": "skill_exposed_sessions", "args": {"skill": "alpha"},
       "supports_if": {"op": "<", "value": 5}, "refutes_if": {"op": ">=", "value": 5}}


def test_a_probe_outside_the_catalog_is_refused() -> None:
    inv = _inv()
    with pytest.raises(DiscoveryError, match="not in the catalog"):
        inv.apply({"question": inv.root, "type": "hypothesis", **HYP, "probe": "shell"}, "r")


def test_an_untested_hypothesis_cannot_be_declared_unresolved() -> None:
    inv = _inv()
    hid = inv.ledger.questions[inv.root].hypotheses[0]
    with pytest.raises(DiscoveryError, match="untested"):
        inv.apply({"question": inv.root, "type": "unresolved", "hypothesis": hid,
                   "reason": "cannot tell"}, "r")


def test_a_child_question_needs_competing_hypotheses() -> None:
    inv = _inv()
    with pytest.raises(DiscoveryError, match="2 to 4"):
        inv.apply({"question": inv.root, "type": "child_question", "observation": "why",
                   "hypotheses": [HYP]}, "r")


def test_recursion_stops_at_the_depth_bound() -> None:
    inv = _inv()
    qid = inv.root
    for depth in range(1, int(policy.BOUNDS["max_depth"]) + 2):
        hyps = [dict(HYP, mechanism=f"m{depth}a"), dict(HYP, mechanism=f"m{depth}b")]
        p = {"question": qid, "type": "child_question", "observation": f"level {depth}",
             "hypotheses": hyps}
        if depth > policy.BOUNDS["max_depth"]:
            with pytest.raises(DiscoveryError, match="budget"):
                inv.apply(p, "r")
        else:
            inv.apply(p, "r")
            qid = next(q.id for q in inv.ledger.questions.values() if q.depth == depth)
    assert inv.ledger.backlog


# ARI-08 regulation -------------------------------------------------------------------------
def test_rolling_budget_and_failures_stop_the_model(tmp_path: Path) -> None:
    gov: dict = {"mode": "NORMAL"}
    assert regulator.allow_model(tmp_path, gov, T0, 0)[0]
    assert not regulator.allow_model(tmp_path, gov, T0, policy.BOUNDS["tick_budget_usd"])[0]
    regulator.record(tmp_path, {"at": T0.isoformat(), "ok": True,
                                "cost_usd": policy.BOUNDS["rolling_budget_usd"]})
    ok, why = regulator.allow_model(tmp_path, gov, T0, 0)
    assert not ok and "rolling" in why
    other = tmp_path / "b"
    other.mkdir()
    for _ in range(int(policy.BOUNDS["max_model_failures"])):
        regulator.record(other, {"at": T0.isoformat(), "ok": False, "cost_usd": 0})
    assert "consecutive" in regulator.allow_model(other, gov, T0, 0)[1]
    assert not regulator.allow_model(other, {"mode": "SAFE"}, T0, 0)[0]


def test_a_question_without_progress_is_closed_to_the_model() -> None:
    rounds: dict[str, int] = {}
    np: list[str] = []
    assert not regulator.note_round(rounds, np, "Q", 0)
    assert regulator.note_round(rounds, np, "Q", 0) and np == ["Q"]
    assert not regulator.note_round(rounds, [], "R", 1)


# ARI-07: one tick at a time, idempotent ----------------------------------------------------
def test_a_concurrent_tick_is_busy(tmp_path: Path) -> None:
    env = build(tmp_path / "env")
    (tmp_path / "state").mkdir()
    with open(tmp_path / "state" / ".lock", "w") as held:
        fcntl.flock(held, fcntl.LOCK_EX)
        out = governor.tick(_cfg(tmp_path, env, FakeObserver(), None))
    assert out["status"] == "BUSY"


def test_a_second_tick_on_the_same_evidence_changes_nothing(tmp_path: Path) -> None:
    env, obs = build(tmp_path / "env"), FakeObserver()
    first = governor.tick(_cfg(tmp_path, env, obs, None))
    cands = (tmp_path / "state" / "candidates.json").read_text()
    invs = (tmp_path / "state" / "investigations.json").read_text()
    second = governor.tick(_cfg(tmp_path, env, obs, None))
    assert first["status"] == second["status"] == "OK"
    assert first["status_changes"] > 0 and second["status_changes"] == 0
    assert (tmp_path / "state" / "candidates.json").read_text() == cands
    assert (tmp_path / "state" / "investigations.json").read_text() == invs
