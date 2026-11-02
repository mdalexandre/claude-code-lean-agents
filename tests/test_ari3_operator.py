"""ARI 3.0 behaviour: probes, recursive investigations, competence, governor, brief, hook."""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import ari_operator
import pytest
from ari import brief, candidates, competence, governor, investigator, probes, regulator
from ari.discovery import Ledger
from ari.governor import load_invs
from ari.investigate import Investigation
from ari3_env import NOW, FakeModel, FakeObserver, build

T0 = datetime(2026, 11, 1, tzinfo=timezone.utc)
SINCE = T0 - timedelta(days=14)
NOW_TS = "2026-10-31T10:00:00Z"


@pytest.fixture()
def env(tmp_path: Path) -> dict[str, Path]:
    return build(tmp_path / "env") | {"state": tmp_path / "state"}


def _ctx(env: dict[str, Path], audit: dict[str, Any] | None = None) -> probes.ProbeContext:
    from ari3_env import audit as mk
    return probes.ProbeContext.build([env["root"]], SINCE, T0, [env["skills"]],
                                     audit=audit or mk())


def _cfg(env: dict[str, Path], obs: FakeObserver, model: FakeModel | None = None,
         now: str = NOW) -> governor.Config:
    return governor.Config(state_dir=env["state"], roots=[str(env["root"])],
                           skills_roots=[str(env["skills"])], mcp_configs=[], now=now,
                           model=model is not None, runner=model, observe_fn=obs)


# probes ------------------------------------------------------------------------------------
def test_raw_scan_counts_real_calls_not_escaped_mentions(env: dict[str, Path]) -> None:
    ctx = _ctx(env)
    assert ctx.raw_skill["eps"] == 3 and ctx.raw_skill["alpha"] == 0


def test_prompts_know_whether_they_followed_an_assistant_turn(env: dict[str, Path]) -> None:
    ctx = _ctx(env)
    firsts = [p for p in ctx.prompts if p.text.startswith("please reconcile")]
    corr = [p for p in ctx.prompts if "linter" in p.text]
    assert len(firsts) == 3 and not any(p.after_assistant for p in firsts)
    assert len(corr) == 3 and all(p.after_assistant for p in corr)


def test_lines_outside_the_window_are_ignored(env: dict[str, Path]) -> None:
    ctx = probes.ProbeContext.build([env["root"]], T0, T0 + timedelta(days=1), [env["skills"]])
    assert not ctx.prompts and not ctx.raw_skill


@pytest.mark.parametrize("value,expected", [(0, "supports"), (3, "refutes"), (1, "inconclusive")])
def test_judge_uses_rules_fixed_in_advance(value: float, expected: str) -> None:
    spec = probes.spec("skill_eligible_prompts", {"skill": "x"}, ("==", 0), (">=", 3))
    assert probes.judge(value, spec) == expected


def test_an_incoherent_rule_pair_never_decides() -> None:
    spec = probes.spec("skill_eligible_prompts", {"skill": "x"}, (">=", 0), (">=", 0))
    assert probes.judge(5, spec) == "inconclusive"


@pytest.mark.parametrize("bad", [
    {"probe": "skill_eligible_prompts", "args": {"skill": ""}},
    {"probe": "prompt_term_count", "args": {"terms": ["ab"], "min_match": 1}},
    {"probe": "prompt_term_count", "args": {"terms": ["abc"], "min_match": 2}},
    {"probe": "transcript_term_count", "args": {"term": "x" * 81}},
])
def test_probe_arguments_are_bounded(bad: dict[str, Any]) -> None:
    spec = bad | {"supports_if": {"op": ">", "value": 0}, "refutes_if": {"op": "==", "value": 0}}
    assert probes.validate_spec(spec)


def test_rules_must_be_finite_numbers() -> None:
    spec = probes.spec("skill_exposed_sessions", {"skill": "a"}, (">", 0), ("==", 0))
    spec["supports_if"]["value"] = float("nan")
    assert any("supports_if" in e for e in probes.validate_spec(spec))
    spec["supports_if"] = {"op": ">", "value": True}
    assert probes.validate_spec(spec)


def test_term_probes_count_lines_and_prompts(env: dict[str, Path]) -> None:
    ctx = _ctx(env)
    t = probes.spec("transcript_term_count", {"term": "linter"}, (">", 0), ("==", 0))
    assert probes.run(ctx, t)[0] == 3
    p = probes.spec("prompt_term_count", {"terms": ["reconcile", "ledger"], "min_match": 2},
                    (">", 0), ("==", 0))
    assert probes.run(ctx, p)[0] == 3


def test_reference_count_skips_the_skills_own_folder(env: dict[str, Path]) -> None:
    (env["skills"] / "beta" / "SKILL.md").write_text(
        "---\nname: beta\ndescription: uses alpha for charts\n---\n")
    ctx = _ctx(env)
    r = probes.spec("reference_count", {"term": "alpha"}, (">", 0), ("==", 0))
    assert probes.run(ctx, r)[0] == 1


def test_frontmatter_block_description_is_read(tmp_path: Path) -> None:
    f = tmp_path / "s" / "SKILL.md"
    f.parent.mkdir()
    f.write_text("---\nname: s\ndescription: >\n  folded words\n  more words\n---\n")
    assert probes.read_skill(f) == ("s", "folded words more words")


# recursive investigation -----------------------------------------------------------------
def _settle(env: dict[str, Path], name: str) -> Investigation:
    inv = Investigation.open_skill(name, 30)
    inv.probe(_ctx(env), "d1", env["state"] / "ev")
    return inv


@pytest.mark.parametrize("name,final", [("alpha", "dormant"), ("beta", "unrouted_demand"),
                                        ("eps", "telemetry"), ("zeta", "dormant")])
def test_each_skill_settles_on_its_explanation(env: dict[str, Path], name: str,
                                               final: str) -> None:
    inv = _settle(env, name)
    assert inv.conclusion()["final"] == final and inv.ledger.validate() == []


def test_a_settled_why_opens_a_child_question(env: dict[str, Path]) -> None:
    inv = _settle(env, "alpha")
    qs = sorted(inv.ledger.questions.values(), key=lambda q: q.depth)
    assert [q.depth for q in qs] == [0, 1] and qs[1].parent == qs[0].id
    assert all(len(q.hypotheses) >= 2 for q in qs)  # ARI-03: competing explanations


def test_telemetry_opens_no_child(env: dict[str, Path]) -> None:
    assert len(_settle(env, "eps").ledger.questions) == 1


def test_two_supported_rivals_stay_open(env: dict[str, Path]) -> None:
    inv = _settle(env, "gamma")  # redundant with delta and also nobody asks for it
    c = inv.conclusion()
    assert c["final"] == "unused" and len(c["open"]) == 1


def test_evidence_files_bind_every_result(env: dict[str, Path]) -> None:
    inv = _settle(env, "alpha")
    for h in inv.ledger.hypotheses.values():
        for r in h.results:
            f = env["state"] / "ev" / r["evidence"]
            body = json.loads(f.read_text())
            assert body["hypothesis"] == h.id and len(r["sha256"]) == 64


def test_investigation_round_trips(env: dict[str, Path]) -> None:
    inv = _settle(env, "gamma")
    back = Investigation.from_dict(json.loads(json.dumps(inv.to_dict())))
    assert back.to_dict() == inv.to_dict()
    assert Ledger.from_dict(inv.ledger.to_dict()).to_dict() == inv.ledger.to_dict()


def test_replay_fails_when_the_evidence_moves(env: dict[str, Path]) -> None:
    inv = _settle(env, "alpha")
    assert inv.recheck(_ctx(env))
    for i in range(3):  # demand for alpha appears
        p = env["root"] / "proj" / f"n{i}.jsonl"
        p.write_text(json.dumps({"type": "user", "timestamp": "2026-10-30T10:00:00Z",
                                 "message": {"content": "render the orbital telescope "
                                             "spectrograms please"}}) + "\n")
    assert not inv.recheck(_ctx(env))


def test_a_model_hypothesis_is_probed_by_the_governor(env: dict[str, Path]) -> None:
    inv = _settle(env, "gamma")
    qid = inv.conclusion()["open"][0]
    inv.apply({"question": qid, "type": "hypothesis",
               "mechanism": "gamma is the newer copy of delta",
               "prediction": "delta is used", "falsification": "delta is never used",
               "probe": "skill_raw_invocations", "args": {"skill": "delta"},
               "supports_if": {"op": ">", "value": 0}, "refutes_if": {"op": "==", "value": 0}},
              "run1")
    inv.probe(_ctx(env), "d2", env["state"] / "ev")
    h = next(h for h in inv.ledger.hypotheses.values() if h.provenance == "model run run1")
    assert h.results and h.results[0]["outcome"] == "refutes"  # delta has no raw calls here


# competence --------------------------------------------------------------------------------
def test_a_recurring_correction_is_a_pattern_with_a_holdout(env: dict[str, Path]) -> None:
    [p] = competence.patterns(_ctx(env))
    assert p["sessions"] == 3 and p["holdout_session_pass"]
    assert {"linter", "committing"} <= set(p["signature"])
    assert p["gate"]["kind"] == "competence_new_skill"


def test_the_gate_routes_to_an_existing_skill(env: dict[str, Path]) -> None:
    (env["skills"] / "lint").mkdir()
    (env["skills"] / "lint" / "SKILL.md").write_text(
        "---\nname: lint\ndescription: run the linter before committing code\n---\n")
    [p] = competence.patterns(_ctx(env))
    assert p["gate"] == {"kind": "competence_routing", "skill": "lint", "cover": p["gate"]["cover"]}


def test_one_session_is_never_a_pattern(env: dict[str, Path]) -> None:
    ctx = _ctx(env)
    ctx.prompts = [p for p in ctx.prompts if p.session == "s0"]
    assert competence.patterns(ctx) == []


def test_secret_looking_excerpts_are_dropped() -> None:
    assert competence.excerpt("no, use token sk-ant-abcdefghijkl") == ""
    assert competence.excerpt("no, the other file") == "no, the other file"


# governor ------------------------------------------------------------------------------------
def test_the_tick_creates_candidates_and_advances_them_on_new_evidence(
        env: dict[str, Path]) -> None:
    obs = FakeObserver("d1")
    governor.tick(_cfg(env, obs))
    cands = candidates.load(env["state"])
    kinds = sorted(c["kind"] for c in cands.values())
    assert kinds == ["competence_new_skill", "restrict_exposure", "restrict_exposure",
                     "routing_correction", "telemetry_defect"]
    obs.digest = "d2"
    out = governor.tick(_cfg(env, obs))
    states = {c["kind"] + ":" + c["target"]: c["state"]
              for c in candidates.load(env["state"]).values()}
    assert states["restrict_exposure:alpha"] == "APPROVAL_PENDING"
    assert out["candidate_moves"] >= 4
    text = (env["state"] / "brief.md").read_text()
    assert text.count("ari_operator.py approve") == 3  # at most three decisions


def test_coverage_gaps_block_qualification(env: dict[str, Path]) -> None:
    obs = FakeObserver("d1")
    obs.audit["coverage"]["unread_gaps"] = 2
    governor.tick(_cfg(env, obs))
    obs.digest = "d2"
    governor.tick(_cfg(env, obs))
    c = next(c for c in candidates.load(env["state"]).values() if c["target"] == "alpha")
    assert c["state"] == "REPLAYED" and "coverage gap" in c["blocked"]


def _ask_about_gamma(pkt: dict[str, Any]) -> list[dict[str, Any]]:
    q = pkt["questions"][0]
    return [{"question": q["question"], "type": "hypothesis",
             "mechanism": f"{q['target']} was replaced by a newer skill",
             "prediction": "the newer skill is used", "falsification": "it is not",
             "probe": "skill_raw_invocations", "args": {"skill": "eps"},
             "supports_if": {"op": ">", "value": 0}, "refutes_if": {"op": "==", "value": 0}},
            {"question": q["question"], "type": "interpretation",
             "text": "two explanations remain; usage of the newer skill decides"},
            {"question": "Q-unknown", "type": "interpretation", "text": "stray"}]


def test_the_model_is_asked_only_about_open_questions_and_only_once(
        env: dict[str, Path]) -> None:
    obs, model = FakeObserver("d1"), FakeModel(_ask_about_gamma)
    out = governor.tick(_cfg(env, obs, model))
    assert len(model.calls) == 1 and out["model_rounds"] == 1
    targets = {q["target"] for q in model.calls[0]["packet"]["questions"]}
    assert targets == {"gamma"}
    row = regulator.rows(env["state"])[-1]
    assert row["ok"] and row["applied"] == 2 and row["refused"] == ["question was not offered"]
    inv = load_invs(env["state"])["gamma"]
    assert any(h.provenance.startswith("model run") and h.results
               for h in inv.ledger.hypotheses.values())
    governor.tick(_cfg(env, obs, model))  # ARI-02: same evidence, no new model call
    assert len(model.calls) == 1


def test_a_failing_model_is_suspended_after_repeated_failures(env: dict[str, Path]) -> None:
    obs, model = FakeObserver("d1"), FakeModel(ok=False)
    for i in range(3):
        obs.digest = f"d{i}"
        governor.tick(_cfg(env, obs, model))
    gov = governor.load_gov(env["state"])
    assert gov["model_suspended"] and len(model.calls) == 3
    obs.digest = "d9"
    governor.tick(_cfg(env, obs, model))
    assert len(model.calls) == 3
    assert "suspended" in (env["state"] / "brief.md").read_text()


def test_a_question_that_teaches_nothing_stops_being_asked(env: dict[str, Path]) -> None:
    obs, model = FakeObserver("d1"), FakeModel([])
    for i in range(4):
        obs.digest = f"d{i}"
        governor.tick(_cfg(env, obs, model))
    assert len(model.calls) == 2  # two empty rounds, then nonprogress closes the question
    inv = load_invs(env["state"])["gamma"]
    assert inv.nonprogress


def test_a_failed_tool_is_an_incident_not_a_stop(env: dict[str, Path]) -> None:
    out = governor.tick(_cfg(env, FakeObserver("d1", tools_ok=False)))
    assert out["status"] == "OK" and any("burn tool failed" in i for i in out["incidents"])
    assert "burn tool failed" in (env["state"] / "brief.md").read_text()


def test_an_interrupted_tick_is_recorded(env: dict[str, Path]) -> None:
    env["state"].mkdir()
    (env["state"] / ".journal").write_text("20261031T000000Z")
    out = governor.tick(_cfg(env, FakeObserver()))
    assert out.get("recovered_from") == "20261031T000000Z"
    assert not (env["state"] / ".journal").exists()


def test_a_crashing_stage_keeps_earlier_state(env: dict[str, Path]) -> None:
    obs = FakeObserver("d1")
    governor.tick(_cfg(env, obs))
    before = (env["state"] / "candidates.json").read_text()

    def boom(*a: Any, **k: Any) -> dict[str, Any]:
        raise RuntimeError("observer crashed")
    out = governor.tick(_cfg(env, boom))  # type: ignore[arg-type]
    assert out["status"] == "FAILED" and "observer crashed" in out["error"]
    assert (env["state"] / "candidates.json").read_text() == before
    assert len(governor.load_gov(env["state"])["runs"]) == 2


def test_an_applied_candidate_is_verified_against_its_rule(env: dict[str, Path]) -> None:
    obs = FakeObserver("d1")
    governor.tick(_cfg(env, obs))
    obs.digest = "d2"
    governor.tick(_cfg(env, obs))
    cands = candidates.load(env["state"])
    c = next(c for c in cands.values() if c["target"] == "alpha")
    candidates.owner_approve(c, "APPROVED", T0, lambda p, code: True)
    candidates.owner_approve(c, "CANARY", T0 - timedelta(days=20), lambda p, code: True)
    candidates.save(env["state"], cands)
    obs.digest = "d3"
    out = governor.tick(_cfg(env, obs))
    c = candidates.load(env["state"])[c["id"]]
    assert out["verified"] >= 1 and c["state"] == "VERIFIED"  # nobody asked for alpha since


def _demand_for_alpha(env: dict[str, Path], day: str = "2026-10-30") -> None:
    for i in range(3):
        (env["root"] / "proj" / f"n{i}.jsonl").write_text(json.dumps(
            {"type": "user", "timestamp": f"{day}T10:00:00Z",
             "message": {"content": "render the orbital telescope spectrograms"}}) + "\n")


def test_a_conclusion_that_does_not_replay_is_rejected(env: dict[str, Path]) -> None:
    obs = FakeObserver("d1")
    governor.tick(_cfg(env, obs))
    _demand_for_alpha(env)
    obs.digest = "d2"
    governor.tick(_cfg(env, obs))
    c = next(c for c in candidates.load(env["state"]).values() if c["target"] == "alpha")
    assert c["state"] == "REJECTED" and "did not reproduce" in c["history"][-1]["note"]


def test_demand_after_an_applied_restriction_rolls_it_back(env: dict[str, Path]) -> None:
    obs = FakeObserver("d1")
    governor.tick(_cfg(env, obs))
    obs.digest = "d2"
    governor.tick(_cfg(env, obs))
    cands = candidates.load(env["state"])
    c = next(c for c in cands.values() if c["target"] == "alpha")
    candidates.owner_approve(c, "APPROVED", T0, lambda p, code: True)
    candidates.owner_approve(c, "CANARY", T0 - timedelta(days=20), lambda p, code: True)
    candidates.save(env["state"], cands)
    _demand_for_alpha(env)
    obs.digest = "d3"
    governor.tick(_cfg(env, obs))
    assert candidates.load(env["state"])[c["id"]]["state"] == "ROLLED_BACK"


def test_a_pattern_whose_holdout_failed_is_not_replayed(env: dict[str, Path]) -> None:
    [p] = competence.patterns(_ctx(env))
    p["holdout_session_pass"] = False
    c = governor._competence_candidate(p, T0, "d1")
    cands = {c["id"]: c}
    governor.advance_candidates(cands, {}, _ctx(env), {}, {p["id"]: p}, 0, False, T0, "d1")
    assert c["state"] == "CANDIDATE"


def test_poor_outcomes_become_a_policy_revision_and_then_suppress(env: dict[str, Path]) -> None:
    cands: dict[str, dict[str, Any]] = {}
    for i in range(3):
        c = candidates.make("routing_correction", f"s{i}", "p",
                            {"spec": {"probe": "x"}, "min_days": 7}, {}, 1.0, T0)
        c["state"] = "ROLLED_BACK"
        cands[c["id"]] = c
    assert regulator.poor_kinds(cands) == ["routing_correction"]
    p = governor._policy_candidate("routing_correction",
                                   regulator.accuracy(cands)["routing_correction"], T0, "d")
    assert p["kind"] == "policy_revision" and "3 of 3" in p["proposal"]
    assert governor.suppressed({p["id"]: p}) == set()
    p["state"] = "APPROVED"
    assert governor.suppressed({p["id"]: p}) == {"routing_correction"}


# brief and hook --------------------------------------------------------------------------
def test_incidents_are_always_shown_and_decisions_capped() -> None:
    cands = {}
    for i in range(5):
        c = candidates.make("restrict_exposure", f"s{i}", "p",
                            {"spec": probes.spec("skill_exposed_sessions", {"skill": "a"},
                                                 (">", 0), ("==", 0)), "min_days": 7},
                            {}, float(i), T0)
        c["state"] = "APPROVAL_PENDING"
        cands[c["id"]] = c
    text, doc = brief.render({"mode": "SAFE", "safe_reasons": ["x changed"]}, {"at": NOW},
                             cands, {}, [], {})
    assert text.index("SAFE state") < text.index("Needs you")
    assert [d["target"] for d in doc["decisions"]] == ["s4", "s3", "s2"]


def test_the_hook_shows_the_brief_once_a_day(tmp_path: Path,
                                             capsys: pytest.CaptureFixture[str]) -> None:
    (tmp_path / "brief.md").write_text("# ARI brief\n")
    assert ari_operator.hook(tmp_path, T0) == 0
    first = json.loads(capsys.readouterr().out)
    assert first["systemMessage"].startswith("# ARI brief")
    ari_operator.hook(tmp_path, T0 + timedelta(hours=2))
    assert capsys.readouterr().out == ""
    ari_operator.hook(tmp_path, T0 + timedelta(days=1))
    assert capsys.readouterr().out


def test_cli_approve_without_a_terminal_is_refused(env: dict[str, Path],
                                                   monkeypatch: pytest.MonkeyPatch) -> None:
    obs = FakeObserver("d1")
    governor.tick(_cfg(env, obs))
    obs.digest = "d2"
    governor.tick(_cfg(env, obs))
    cid = next(c["id"] for c in candidates.load(env["state"]).values()
               if c["state"] == "APPROVAL_PENDING")
    monkeypatch.setattr(candidates, "real_terminal", lambda p, c: False)
    rc = ari_operator.main(["--state-dir", str(env["state"]), "approve", cid])
    assert rc == 2 and candidates.load(env["state"])[cid]["state"] == "APPROVAL_PENDING"


def test_model_reply_parsing_handles_text_and_errors(tmp_path: Path) -> None:
    def text_reply(cmd: list[str], stdin: str, t: float, cwd: Path) -> tuple[int, str, str]:
        return 0, json.dumps({"result": "```json\n{\"proposals\": []}\n```",
                              "total_cost_usd": 0.02}), ""
    r = investigator.investigate({}, 0.1, 5, tmp_path, text_reply)
    assert r.ok and r.cost_usd == 0.02
    r = investigator.investigate({}, 0.1, 5, tmp_path, lambda *a: (0, "not json", ""))
    assert not r.ok and "schema" in r.error


def test_compaction_summaries_are_not_user_prompts(env: dict[str, Path]) -> None:
    ctx = _ctx(env)
    assert not any(p.text.startswith("This session is being continued") for p in ctx.prompts)
    out, _ = probes.run(ctx, probes.spec("skill_eligible_prompts", {"skill": "alpha"},
                                         ("==", 0), (">=", 3)))
    assert out == 0


def test_identifiers_are_not_distinctive_words(tmp_path: Path) -> None:
    sk = tmp_path / "sk" / "hexy"
    sk.mkdir(parents=True)
    (sk / "SKILL.md").write_text("---\nname: hexy\ndescription: Rotate cc96bee2 tokens "
                                 "for nightly ledger batches\n---\n")
    ctx = probes.ProbeContext.build([str(tmp_path / "none")], None, None,
                                    [str(tmp_path / "sk")], [], {})
    assert "cc96bee2" not in ctx.distinctive("hexy")
    assert "ledger" in ctx.distinctive("hexy")



REQUIRED_BY_TYPE = {
    "hypothesis": {"question", "type", "mechanism", "prediction", "falsification", "probe",
                   "args", "supports_if", "refutes_if"},
    "child_question": {"question", "type", "observation", "hypotheses"},
    "unresolved": {"question", "type", "hypothesis", "reason"},
    "interpretation": {"question", "type", "text"},
}


def test_the_output_schema_requires_what_the_validator_requires() -> None:
    # A live call returned a hypothesis without prediction and a child question with "text"
    # in place of "observation": the schema allowed both and the validator refused them.
    items = investigator.OUTPUT_SCHEMA["properties"]["proposals"]["items"]
    variants = {v["properties"]["type"]["const"]: v for v in items["anyOf"]}
    assert set(variants) == set(REQUIRED_BY_TYPE)
    for kind, need in REQUIRED_BY_TYPE.items():
        v = variants[kind]
        assert set(v["required"]) == need and v["additionalProperties"] is False
    hyp = variants["child_question"]["properties"]["hypotheses"]["items"]
    assert {"mechanism", "prediction", "falsification"} <= set(hyp["required"])


def test_everyday_words_are_not_evidence_of_demand(tmp_path: Path) -> None:
    # Real run: 612 prompts "matched" a skill on words like neither, honestly, establish.
    sk = tmp_path / "sk"
    for name, desc in {"levers": "Explain honestly whether neither lever can establish rank",
                       "other": "Rotate archive bundles nightly"}.items():
        (sk / name).mkdir(parents=True)
        (sk / name / "SKILL.md").write_text(f"---\nname: {name}\ndescription: {desc}\n---\n")
    ctx = probes.ProbeContext.build([str(tmp_path / "none")], None, None, [str(sk)], [], {})
    for i in range(100):
        text = "honestly neither option can establish the plan" if i < 40 else f"task {i}"
        ctx.prompts.append(probes.Prompt(f"s{i}", NOW_TS, text, probes.terms(text), False))
    out, detail = probes.run(ctx, probes.spec("skill_eligible_prompts", {"skill": "levers"},
                                              ("==", 0), (">=", 3)))
    assert out == 0 and "honestly" not in detail["terms"]


def test_too_few_distinctive_words_leave_demand_undecided(tmp_path: Path) -> None:
    # Real run: one remaining word ("portable") was taken as "at least 3 of its words".
    sk = tmp_path / "sk"
    (sk / "port").mkdir(parents=True)
    (sk / "port" / "SKILL.md").write_text("---\nname: port\ndescription: portable kits\n---\n")
    ctx = probes.ProbeContext.build([str(tmp_path / "none")], None, None, [str(sk)], [], {})
    for i in range(5):
        ctx.prompts.append(probes.Prompt(f"s{i}", NOW_TS, "a portable plan",
                                         probes.terms("a portable plan"), False))
    spec = probes.spec("skill_eligible_prompts", {"skill": "port"}, ("==", 0), (">=", 3))
    value, detail = probes.run(ctx, spec)
    assert probes.judge(value, spec) == "inconclusive" and "too few" in detail["note"]
    assert probes.jsonable(value) is None
    json.loads(json.dumps(probes.jsonable(value), allow_nan=False))
