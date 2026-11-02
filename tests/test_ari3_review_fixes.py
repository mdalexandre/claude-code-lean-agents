"""ARI 3.0 defects found by the blind review (2026-11-02), one test per defect."""
from __future__ import annotations

import fcntl
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import ari_operator
import pytest
from ari import brief, candidates, competence, governor, investigator, policy, probes, regulator
from ari.discovery import DiscoveryError
from ari.investigate import Investigation
from ari3_env import NOW, FakeModel, FakeObserver, audit, build

T0 = datetime(2026, 11, 1, tzinfo=timezone.utc)
YES = lambda p, code: True  # noqa: E731  a confirmed terminal, for tests only


@pytest.fixture()
def env(tmp_path: Path) -> dict[str, Path]:
    return build(tmp_path / "env") | {"state": tmp_path / "state"}


def _ctx(env: dict[str, Path]) -> probes.ProbeContext:
    return probes.ProbeContext.build([env["root"]], T0 - timedelta(days=14), T0,
                                     [env["skills"]], audit=audit())


def _cfg(env: dict[str, Path], obs: FakeObserver, model: FakeModel | None = None
         ) -> governor.Config:
    return governor.Config(state_dir=env["state"], roots=[str(env["root"])],
                           skills_roots=[str(env["skills"])], mcp_configs=[], now=NOW,
                           model=model is not None, runner=model, observe_fn=obs)


def _fail_rows(sd: Path, n: int) -> None:
    sd.mkdir(parents=True, exist_ok=True)
    for _ in range(n):
        regulator.record(sd, {"at": T0.isoformat(), "ok": False, "cost_usd": 0.0})


# D1 resume ---------------------------------------------------------------------------------
def test_resume_lets_the_model_run_again_after_three_failures(tmp_path: Path) -> None:
    _fail_rows(tmp_path, 3)
    gov: dict[str, Any] = {"mode": "NORMAL", "model_suspended": "3 consecutive failed calls"}
    assert not regulator.allow_model(tmp_path, gov, T0, 0)[0]
    regulator.resume(tmp_path, gov, T0)
    assert gov["model_suspended"] is None
    assert regulator.allow_model(tmp_path, gov, T0, 0) == (True, "allowed")
    _fail_rows(tmp_path, 3)  # the counter starts again after a resume
    assert "consecutive" in regulator.allow_model(tmp_path, gov, T0, 0)[1]


# C8a wrong-typed model replies -------------------------------------------------------------
def _hostile(pkt: dict[str, Any]) -> list[dict[str, Any]]:
    q = pkt["questions"][0]["question"]
    base = {"question": q, "type": "hypothesis", "mechanism": "m", "prediction": "p",
            "falsification": "f", "args": {"skill": "eps"},
            "supports_if": {"op": ">", "value": 0}, "refutes_if": {"op": "==", "value": 0}}
    return [base | {"probe": ["skill_raw_invocations"]},
            base | {"mechanism": "m2", "probe": "skill_raw_invocations",
                    "supports_if": {"op": ">", "value": 10 ** 400}}]


def test_a_malformed_reply_is_refused_and_its_cost_recorded(env: dict[str, Path]) -> None:
    obs, model = FakeObserver("d1"), FakeModel(_hostile, cost=0.3)
    out = governor.tick(_cfg(env, obs, model))
    assert out["status"] == "OK"
    rows = [r for r in regulator.rows(env["state"]) if not r.get("event")]  # result rows
    assert len(rows) == 1 and rows[0]["cost_usd"] == 0.3 and len(rows[0]["refused"]) == 2
    governor.tick(_cfg(env, obs, model))
    assert len(model.calls) == 1  # the same question is not asked again at this digest


# C8b per-call budget never exceeds the rolling remainder -----------------------------------
def test_the_call_budget_is_clamped_to_the_rolling_remainder(env: dict[str, Path]) -> None:
    env["state"].mkdir(parents=True)
    regulator.record(env["state"], {"at": "2026-10-31T00:00:00+00:00", "ok": True,
                                    "cost_usd": policy.BOUNDS["rolling_budget_usd"] - 0.1})
    model = FakeModel([])
    governor.tick(_cfg(env, FakeObserver("d1"), model))
    cmd = model.calls[0]["cmd"]
    assert float(cmd[cmd.index("--max-budget-usd") + 1]) == pytest.approx(0.1)


# C9 telemetry canaries are judged with the audit -------------------------------------------
def test_a_fixed_parser_verifies_its_telemetry_candidate(env: dict[str, Path]) -> None:
    obs = FakeObserver("d1")
    governor.tick(_cfg(env, obs))
    obs.digest = "d2"
    governor.tick(_cfg(env, obs))
    cands = candidates.load(env["state"])
    c = next(c for c in cands.values() if c["kind"] == "telemetry_defect")
    candidates.owner_approve(c, "APPROVED", T0, YES)
    candidates.owner_approve(c, "CANARY", T0 - timedelta(days=20), YES)
    candidates.save(env["state"], cands)
    obs.audit = audit(eps_parsed=3)  # the parser now sees the three calls
    obs.digest = "d3"
    governor.tick(_cfg(env, obs))
    assert candidates.load(env["state"])[c["id"]]["state"] == "VERIFIED"


def test_a_tampered_canary_is_an_incident_not_a_failed_tick(env: dict[str, Path]) -> None:
    obs = FakeObserver("d1")
    governor.tick(_cfg(env, obs))
    obs.digest = "d2"
    governor.tick(_cfg(env, obs))
    cands = candidates.load(env["state"])
    c = next(c for c in cands.values() if c["target"] == "alpha")
    candidates.owner_approve(c, "APPROVED", T0, YES)
    candidates.owner_approve(c, "CANARY", T0 - timedelta(days=20), YES)
    c["proposal"] = "something else"
    candidates.save(env["state"], cands)
    obs.digest = "d3"
    out = governor.tick(_cfg(env, obs))
    assert out["status"] == "OK" and any(c["id"] in i for i in out["incidents"])


# C3 ambiguity is never resolved by a proposal ----------------------------------------------
def _gamma(env: dict[str, Path]) -> Investigation:
    inv = Investigation.open_skill("gamma", 30)
    inv.probe(_ctx(env), "d1", env["state"] / "ev")
    return inv


def test_a_child_question_is_all_or_nothing(env: dict[str, Path]) -> None:
    inv = _gamma(env)
    qid = inv.conclusion()["open"][0]
    before = json.dumps(inv.to_dict(), sort_keys=True)
    h = {"mechanism": "same idea", "prediction": "p", "falsification": "f",
         "probe": "skill_raw_invocations", "args": {"skill": "delta"},
         "supports_if": {"op": ">", "value": 0}, "refutes_if": {"op": "==", "value": 0}}
    with pytest.raises(DiscoveryError):
        inv.apply({"question": qid, "type": "child_question", "observation": "why",
                   "hypotheses": [h, dict(h)]}, "r")
    assert json.dumps(inv.to_dict(), sort_keys=True) == before


def test_an_unresolved_verdict_cannot_crown_the_rival(env: dict[str, Path]) -> None:
    inv = _gamma(env)
    qid = inv.conclusion()["open"][0]
    q = inv.ledger.questions[qid]
    supported = [h for h in q.hypotheses if any(r["outcome"] == "supports"
                                                for r in inv.ledger.hypotheses[h].results)]
    assert len(supported) >= 2
    before = json.dumps(inv.to_dict(), sort_keys=True)
    with pytest.raises(DiscoveryError, match="settle"):
        inv.apply({"question": qid, "type": "unresolved", "hypothesis": supported[0],
                   "reason": "cannot tell"}, "r")
    assert json.dumps(inv.to_dict(), sort_keys=True) == before


# C1 a corrupt state file still yields a run record and a brief -----------------------------
def test_a_corrupt_state_file_is_kept_aside_and_reported(env: dict[str, Path]) -> None:
    env["state"].mkdir(parents=True)
    (env["state"] / "state.json").write_text("{not json")
    out = governor.tick(_cfg(env, FakeObserver("d1")))
    gov = json.loads((env["state"] / "state.json").read_text())
    assert len(gov["runs"]) == 1 and gov["model_suspended"]
    assert any(p.name.startswith("state.json.unreadable") for p in env["state"].iterdir())
    assert any("unreadable" in i for i in out["incidents"])
    assert "unreadable" in (env["state"] / "brief.md").read_text()


# C15 units are taken in turn, in a fixed order, never by magnitude --------------------------
def test_each_unit_gets_a_turn_whatever_its_magnitude() -> None:
    def pend(i: int, unit: str, benefit: float) -> dict[str, Any]:
        return {"id": f"C{i}", "state": "APPROVAL_PENDING", "benefit": benefit,
                "benefit_unit": unit}
    cands = {c["id"]: c for c in [pend(1, "listing_tokens", 9000), pend(2, "listing_tokens", 8),
                                  pend(3, "matching_prompts", 50),
                                  pend(4, "correction_sessions", 3)]}
    picked = [c["id"] for c in brief.decisions(cands)]
    assert sorted(picked) == ["C1", "C3", "C4"]
    assert picked == ["C4", "C1", "C3"]  # alphabetical by unit, best within each unit


def test_the_brief_counts_decisions_it_did_not_show() -> None:
    cands = {f"C{i}": {"id": f"C{i}", "state": "APPROVAL_PENDING", "benefit": i,
                       "benefit_unit": "listing_tokens", "kind": "restrict_exposure",
                       "target": f"s{i}", "proposal": "p",
                       "verify": {"min_days": 14, "spec": {"probe": "x"}}} for i in range(5)}
    text, _ = brief.render({"incidents": []}, {}, cands, {}, [], {})
    assert "2 more pending" in text


def test_approved_but_unapplied_candidates_are_shown() -> None:
    c = {"id": "C9", "state": "APPROVED", "kind": "restrict_exposure", "target": "alpha",
         "proposal": "p", "benefit": 1, "benefit_unit": "listing_tokens"}
    text, _ = brief.render({"incidents": []}, {}, {"C9": c}, {}, [], {})
    assert "C9" in text and "applied" in text


# D4 owner commands respect the tick lock ---------------------------------------------------
def test_owner_commands_wait_for_a_running_tick(env: dict[str, Path]) -> None:
    env["state"].mkdir(parents=True)
    with open(env["state"] / ".lock", "w") as held:
        fcntl.flock(held, fcntl.LOCK_EX)
        code = ari_operator.main(["--state-dir", str(env["state"]), "reject", "C-x",
                                  "--reason", "no"])
    assert code == 75


# C13 secrets in correction excerpts --------------------------------------------------------
@pytest.mark.parametrize("text", ["the api key is AKIAIOSFODNN7EXAMPLE",
                                  "the secret is hunter2hunter2", "bearer abcdef0123456789"])
def test_secret_looking_excerpts_are_dropped(text: str) -> None:
    assert competence.excerpt(text) == ""


def test_an_ordinary_correction_excerpt_is_kept() -> None:
    assert competence.excerpt("no, you forgot the linter") == "no, you forgot the linter"


# C4 per tick caps ---------------------------------------------------------------------------
def test_targets_per_tick_are_capped(env: dict[str, Path], monkeypatch: pytest.MonkeyPatch
                                     ) -> None:
    monkeypatch.setitem(policy.BOUNDS, "max_targets_per_tick", 2)
    out = governor.tick(_cfg(env, FakeObserver("d1")))
    assert out["opened"] == 2


def test_a_reply_keeps_at_most_eight_proposals(tmp_path: Path) -> None:
    props = [{"question": "Q", "type": "interpretation", "text": f"t{i}"} for i in range(12)]
    res = investigator.investigate({"questions": []}, 0.5, 10, tmp_path,
                                   FakeModel(props))
    assert len(res.proposals) == policy.BOUNDS["max_proposals"] == 8


# D5 one transcript rescan for all term probes of a pass -------------------------------------
def test_term_probes_share_one_rescan(env: dict[str, Path], monkeypatch: pytest.MonkeyPatch
                                      ) -> None:
    ctx = _ctx(env)
    scans: list[tuple[str, ...]] = []
    real = ctx._scan
    monkeypatch.setattr(ctx, "_scan", lambda terms: (scans.append(terms), real(terms))[1])
    specs = [probes.spec("transcript_term_count", {"term": w}, (">", 0), ("==", 0))
             for w in ("ledger", "linter", "orbital")]
    probes.prefetch(ctx, specs)
    for s in specs:
        probes.run(ctx, s)
    assert len(scans) == 1


# observer: the burn window follows --days ---------------------------------------------------
def test_the_burn_tool_reads_the_same_window(tmp_path: Path,
                                             monkeypatch: pytest.MonkeyPatch) -> None:
    from ari import observer
    seen: list[list[str]] = []

    def fake(cmd: list[str], timeout: float) -> dict[str, Any]:
        seen.append(cmd)
        return {"ok": False, "error": "not run", "data": None}
    monkeypatch.setattr(observer, "_run", fake)
    observer.observe(tmp_path, ["r"], 14, None, [], [], tmp_path / "n")
    burn = next(c for c in seen if any("weekly_burn" in x for x in c))
    assert burn[burn.index("--days") + 1] == "14"
