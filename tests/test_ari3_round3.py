"""ARI 3.0 defects found by the second blind review (2026-11-02), one test per defect."""
from __future__ import annotations

import os
import pty
import select
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import pytest
from ari import candidates, governor
from ari3_env import NOW, FakeModel, FakeObserver, build

T0 = datetime(2026, 11, 1, tzinfo=timezone.utc)
YES = lambda p, code: True  # noqa: E731  a confirmed terminal, for tests only


@pytest.fixture()
def env(tmp_path: Path) -> dict[str, Path]:
    return build(tmp_path / "env") | {"state": tmp_path / "state"}


def _cfg(env: dict[str, Path], obs: FakeObserver, model: FakeModel | None = None
         ) -> governor.Config:
    return governor.Config(state_dir=env["state"], roots=[str(env["root"])],
                           skills_roots=[str(env["skills"])], mcp_configs=[], now=NOW,
                           model=model is not None, runner=model, observe_fn=obs)


def _pending(env: dict[str, Path], target: str = "alpha"
             ) -> tuple[FakeObserver, dict[str, dict[str, Any]], dict[str, Any]]:
    obs = FakeObserver("d1")
    governor.tick(_cfg(env, obs))
    obs.digest = "d2"
    governor.tick(_cfg(env, obs))
    cands = candidates.load(env["state"])
    return obs, cands, next(c for c in cands.values() if c["target"] == target)


def _pty_answer(answer: bytes) -> tuple[bool, bytes]:
    """Run real_terminal in a child that owns a real pseudo terminal; the parent plays the
    person: it waits for the prompt and types `answer`."""
    pid, master = pty.fork()
    if pid == 0:  # child: the pty slave is its controlling terminal
        try:
            ok = candidates.real_terminal("approve X", "abc123")
        except BaseException:  # noqa: BLE001  any exception is a failure
            os._exit(3)
        os._exit(0 if ok else 1)
    seen = b""
    while b"> " not in seen:
        r, _, _ = select.select([master], [], [], 10)
        assert r, f"no prompt within 10 s, saw {seen!r}"
        seen += os.read(master, 1024)
    os.write(master, answer)
    _, status = os.waitpid(pid, 0)
    os.close(master)
    return os.WEXITSTATUS(status) == 0, seen


# D1 the terminal gate works on a real terminal ------------------------------------------
def test_real_terminal_accepts_the_shown_code_on_a_real_pty() -> None:
    ok, seen = _pty_answer(b"abc123\n")
    assert b"abc123" in seen and b"approve X" in seen
    assert ok


def test_real_terminal_refuses_a_wrong_code_on_a_real_pty() -> None:
    ok, _ = _pty_answer(b"zzz999\n")
    assert not ok


def test_real_terminal_refuses_without_a_controlling_terminal(tmp_path: Path) -> None:
    pid = os.fork()
    if pid == 0:
        os.setsid()  # a new session has no controlling terminal
        os._exit(0 if candidates.real_terminal("x", "abc123") is False else 1)
    _, status = os.waitpid(pid, 0)
    assert os.WEXITSTATUS(status) == 0


# D10 the approval binds the content the owner saw, not a hash stored beside it ------------
def _rewrite(c: dict[str, Any]) -> None:
    c["proposal"] = "something the owner never saw"
    c["content_hash"] = candidates.content_hash(c)  # the edit also rewrites the stored hash


def test_applied_refuses_content_edited_after_approval_even_with_a_rewritten_hash(
        env: dict[str, Path]) -> None:
    _, _, c = _pending(env)
    candidates.owner_approve(c, "APPROVED", T0, YES)
    _rewrite(c)
    with pytest.raises(candidates.CandidateError):
        candidates.owner_approve(c, "CANARY", T0, YES)
    assert c["state"] == "APPROVED"


def test_a_canary_edited_with_a_rewritten_hash_is_an_incident(env: dict[str, Path]) -> None:
    obs, cands, c = _pending(env)
    candidates.owner_approve(c, "APPROVED", T0, YES)
    candidates.owner_approve(c, "CANARY", T0 - timedelta(days=20), YES)
    _rewrite(c)
    candidates.save(env["state"], cands)
    obs.digest = "d3"
    out = governor.tick(_cfg(env, obs))
    assert any(c["id"] in i and "changed after approval" in i for i in out["incidents"])
    assert candidates.load(env["state"])[c["id"]]["state"] == "CANARY"


# D2 every module the operator runs is pinned, and owner commands check the pin -----------
def test_every_ari_module_and_the_operator_are_pinned() -> None:
    from ari import policy
    root = policy.repo_root()
    mods = {str(p.relative_to(root)) for p in (root / "scripts" / "ari").glob("*.py")}
    assert mods | {"scripts/ari_operator.py"} <= set(policy.PROTECTED)


@pytest.mark.parametrize("cmd", ["approve", "applied", "resume"])
def test_owner_commands_refuse_when_a_protected_file_changed(
        env: dict[str, Path], monkeypatch: pytest.MonkeyPatch, cmd: str) -> None:
    import ari_operator
    from ari import policy
    _, cands, c = _pending(env)
    if cmd == "applied":
        candidates.owner_approve(c, "APPROVED", T0, YES)
        candidates.save(env["state"], cands)
    before = (env["state"] / "candidates.json").read_bytes()
    monkeypatch.setattr(policy, "integrity", lambda root=None: ["scripts/ari/x.py: changed"])
    monkeypatch.setattr(candidates, "real_terminal", YES)
    argv = ["--state-dir", str(env["state"]), cmd] + ([c["id"]] if cmd != "resume" else [])
    assert ari_operator.main(argv) == 2
    assert (env["state"] / "candidates.json").read_bytes() == before


# D4 / C3 a model verdict never settles a question the probes left open -------------------
def _three_way(tmp_path: Path) -> tuple[Any, str, list[str]]:
    from ari.investigate import Investigation
    inv = Investigation.open_skill("zeta", 30)
    q = inv.ledger.questions[inv.root]
    ev = tmp_path / "ev.json"
    ev.write_text("{}")
    a, b, c = q.hypotheses[:3]
    inv.ledger.record(a, "supports", ev, "probe")
    inv.ledger.record(b, "inconclusive", ev, "probe")
    inv.ledger.record(c, "inconclusive", ev, "probe")
    return inv, inv.root, [a, b, c]


def test_unresolved_rivals_cannot_hand_the_question_to_a_supported_one(
        tmp_path: Path) -> None:
    import json

    from ari.discovery import DiscoveryError
    inv, qid, (a, b, c) = _three_way(tmp_path)
    inv.apply({"question": qid, "type": "unresolved", "hypothesis": b,
               "reason": "no probe can decide"}, "r1")  # c is still open: nothing settles
    before = json.dumps(inv.to_dict(), sort_keys=True)
    with pytest.raises(DiscoveryError, match="settle"):
        inv.apply({"question": qid, "type": "unresolved", "hypothesis": c,
                   "reason": "no probe can decide"}, "r2")
    assert json.dumps(inv.to_dict(), sort_keys=True) == before
    assert inv.ledger.questions[qid].status == "OPEN" and inv.supported(qid) is None


def test_a_repeated_refused_proposal_adds_one_backlog_row(tmp_path: Path) -> None:
    from ari.discovery import DiscoveryError
    inv, qid, _ = _three_way(tmp_path)
    inv.ledger.budget["max_nodes"] = len(inv.ledger.questions)  # no room for a child
    h = {"mechanism": "m", "prediction": "p", "falsification": "f",
         "probe": "skill_raw_invocations", "args": {"skill": "zeta"},
         "supports_if": {"op": ">", "value": 0}, "refutes_if": {"op": "==", "value": 0}}
    prop = {"question": qid, "type": "child_question", "observation": "why",
            "hypotheses": [h, dict(h, mechanism="m2")]}
    for _ in range(3):
        with pytest.raises(DiscoveryError):
            inv.apply(prop, "r")
    assert len(inv.ledger.backlog) == 1


def test_a_saved_ledger_cannot_raise_the_recursion_bounds() -> None:
    from ari import discovery, policy
    led = discovery.Ledger()
    doc = led.to_dict() | {"budget": {"max_depth": 99, "max_per_node": 99, "max_nodes": 999}}
    back = discovery.Ledger.from_dict(doc)
    assert back.budget == {"max_depth": discovery.MAX_DEPTH,
                           "max_per_node": discovery.MAX_PER_NODE,
                           "max_nodes": discovery.MAX_NODES}
    assert (policy.BOUNDS["max_depth"], policy.BOUNDS["max_per_node"],
            policy.BOUNDS["max_nodes"]) == (discovery.MAX_DEPTH, discovery.MAX_PER_NODE,
                                            discovery.MAX_NODES)


# D3 a failed audit tool is missing evidence, never zeros ---------------------------------
def test_audit_probes_are_undecided_without_an_audit(env: dict[str, Path]) -> None:
    from ari import probes
    ctx = probes.ProbeContext.build([env["root"]], T0 - timedelta(days=14), T0,
                                    [env["skills"]], audit=None)
    spec = {"probe": "skill_parsed_invocations", "args": {"skill": "eps"},
            "supports_if": {"op": ">", "value": 0}, "refutes_if": {"op": "==", "value": 0}}
    value = probes.run(ctx, spec)[0]
    assert value != value and probes.judge(value, spec) == "inconclusive"


def test_a_failed_audit_tool_moves_no_candidate(env: dict[str, Path]) -> None:
    import json
    obs = FakeObserver("d1")
    governor.tick(_cfg(env, obs))
    obs.digest = "d2"
    governor.tick(_cfg(env, obs))
    cands = candidates.load(env["state"])
    c = next(c for c in cands.values() if c["kind"] == "telemetry_defect")
    candidates.owner_approve(c, "APPROVED", T0, YES)
    candidates.owner_approve(c, "CANARY", T0 - timedelta(days=20), YES)
    candidates.save(env["state"], cands)
    invs_before = (env["state"] / "investigations.json").read_text()
    states_before = {k: v["state"] for k, v in cands.items()}
    obs.audit_ok, obs.digest = False, "d3"
    out = governor.tick(_cfg(env, obs))
    after = candidates.load(env["state"])
    assert {k: v["state"] for k, v in after.items()} == states_before
    assert json.loads((env["state"] / "investigations.json").read_text()) == \
        json.loads(invs_before)
    assert out["status"] == "OK" and any("audit" in i for i in out["incidents"])
    assert "nothing yet" not in (env["state"] / "brief.md").read_text()


# D5 one bad file, line or candidate never fails every tick -------------------------------
HOSTILE_LEDGER = [
    '{"at": "2026-11-01T00:00:00", "cost_usd": "x", "ok": false}',  # naive time, text cost
    '{"at": "2026-11-01T00:00:00+00:00", "cost_usd": NaN, "ok": false}',
    '{"at": 5, "cost_usd": -3, "status_changes": "z", "input_tokens": null, "ok": false}',
    'not json',
]


def test_hostile_ledger_rows_are_counted_conservatively(tmp_path: Path) -> None:
    import json

    from ari import policy, regulator
    (tmp_path / regulator.LEDGER).write_text("\n".join(HOSTILE_LEDGER) + "\n")
    total = regulator.spent(tmp_path, T0, 7)
    assert total == total and total >= 3 * policy.BOUNDS["tick_budget_usd"]
    rep = regulator.self_report(tmp_path, T0)
    json.dumps(rep, allow_nan=False)
    assert not regulator.allow_model(tmp_path, {"mode": "NORMAL"}, T0, 0)[0]


def test_a_tick_survives_a_hostile_ledger(env: dict[str, Path]) -> None:
    from ari import regulator
    env["state"].mkdir(parents=True)
    (env["state"] / regulator.LEDGER).write_text("\n".join(HOSTILE_LEDGER) + "\n")
    out = governor.tick(_cfg(env, FakeObserver("d1")))
    assert out["status"] == "OK" and (env["state"] / "brief.json").exists()


@pytest.mark.parametrize("name", ["candidates.json", "investigations.json"])
def test_an_unreadable_store_is_kept_aside_and_reported(env: dict[str, Path],
                                                        name: str) -> None:
    obs = FakeObserver("d1")
    governor.tick(_cfg(env, obs))
    (env["state"] / name).write_text("{not json")
    obs.digest = "d2"
    out = governor.tick(_cfg(env, obs))
    assert out["status"] == "OK" and any(name in i for i in out["incidents"])
    assert any(p.name.startswith(name + ".unreadable") for p in env["state"].iterdir())
    obs.digest = "d3"
    assert governor.tick(_cfg(env, obs))["status"] == "OK"


def test_a_malformed_candidate_is_quarantined_and_the_rest_kept(env: dict[str, Path]) -> None:
    obs, cands, c = _pending(env)
    bad = dict(c, id="C-bad", verify=None, state="BOGUS")
    cands["C-bad"] = bad
    candidates.save(env["state"], cands)
    obs.digest = "d3"
    out = governor.tick(_cfg(env, obs))
    assert out["status"] == "OK" and any("C-bad" in i for i in out["incidents"])
    kept = candidates.load(env["state"])
    assert "C-bad" not in kept and c["id"] in kept
    assert "C-bad" in (env["state"] / "candidates.quarantine.json").read_text()


def test_a_candidate_with_a_nan_benefit_is_quarantined(env: dict[str, Path]) -> None:
    import json
    obs, cands, c = _pending(env)
    cands["C-nan"] = dict(c, id="C-nan", benefit=float("nan"))
    (env["state"] / "candidates.json").write_text(json.dumps(
        {"schema": candidates.SCHEMA, "candidates": cands}))  # NaN written as a bare token
    obs.digest = "d3"
    out = governor.tick(_cfg(env, obs))
    assert out["status"] == "OK" and "C-nan" not in candidates.load(env["state"])


def test_the_safe_brief_skips_a_malformed_candidate(env: dict[str, Path],
                                                    monkeypatch: pytest.MonkeyPatch) -> None:
    from ari import policy
    obs, cands, c = _pending(env)
    cands["C-bad"] = dict(c, id="C-bad", state="APPROVAL_PENDING", verify=None,
                          benefit_unit="aaa", benefit=1e9)
    candidates.save(env["state"], cands)
    monkeypatch.setattr(policy, "integrity", lambda root=None: ["x: changed"])
    obs.digest = "d3"
    out = governor.tick(_cfg(env, obs))
    assert out["status"] == "SAFE" and (env["state"] / "brief.md").exists()


# D6 every model call is recorded with a cost that never reads as free --------------------
def _runner(code: int, stdout: str) -> Any:
    return lambda cmd, stdin, timeout, cwd: (code, stdout, "")


@pytest.mark.parametrize("stdout", [
    '{"total_cost_usd": NaN, "usage": {"input_tokens": "lots"}, '
    '"structured_output": {"proposals": []}}',
    '{"total_cost_usd": -1, "usage": {"output_tokens": 1e400}, '
    '"structured_output": {"proposals": []}}',
    'not json at all',
])
def test_an_unreadable_cost_counts_as_the_whole_call_budget(tmp_path: Path,
                                                            stdout: str) -> None:
    from ari import investigator
    res = investigator.investigate({"questions": []}, 0.4, 10, tmp_path, _runner(0, stdout))
    assert res.cost_usd == pytest.approx(0.4)
    assert isinstance(res.input_tokens, int) and isinstance(res.output_tokens, int)


def test_a_timed_out_call_counts_as_the_whole_call_budget(tmp_path: Path) -> None:
    import subprocess

    from ari import investigator

    def slow(cmd: list[str], stdin: str, timeout: float, cwd: Path) -> tuple[int, str, str]:
        raise subprocess.TimeoutExpired(cmd, timeout)
    res = investigator.investigate({"questions": []}, 0.4, 10, tmp_path, slow)
    assert not res.ok and res.cost_usd == pytest.approx(0.4)


def test_a_call_that_never_started_costs_nothing(tmp_path: Path) -> None:
    from ari import investigator

    def missing(cmd: list[str], stdin: str, timeout: float, cwd: Path) -> tuple[int, str, str]:
        raise FileNotFoundError("claude")
    res = investigator.investigate({"questions": []}, 0.4, 10, tmp_path, missing)
    assert not res.ok and res.cost_usd == 0.0


def test_the_call_budget_is_floored_to_cents(env: dict[str, Path]) -> None:
    from ari import policy, regulator
    env["state"].mkdir(parents=True)
    regulator.record(env["state"], {"at": "2026-10-31T00:00:00+00:00", "ok": True,
                                    "cost_usd": policy.BOUNDS["rolling_budget_usd"] - 0.057})
    model = FakeModel([])
    governor.tick(_cfg(env, FakeObserver("d1"), model))
    cmd = model.calls[0]["cmd"]
    assert float(cmd[cmd.index("--max-budget-usd") + 1]) <= 0.057


# D7 a failed call is not repeated at the same evidence ------------------------------------
def test_a_failed_call_is_not_repeated_at_an_unchanged_digest(env: dict[str, Path]) -> None:
    obs, model = FakeObserver("d1"), FakeModel(ok=False)
    governor.tick(_cfg(env, obs, model))
    governor.tick(_cfg(env, obs, model))
    assert len(model.calls) == 1


# D8 every pending decision can be listed ---------------------------------------------------
def test_status_lists_every_pending_decision(env: dict[str, Path],
                                             capsys: pytest.CaptureFixture[str]) -> None:
    import json

    import ari_operator
    _pending(env)
    pend = sorted(k for k, v in candidates.load(env["state"]).items()
                  if v["state"] == "APPROVAL_PENDING")
    assert pend
    assert ari_operator.main(["--state-dir", str(env["state"]), "status"]) == 0
    out = json.loads(capsys.readouterr().out)
    assert sorted(d["id"] for d in out["pending"]) == pend


# D9 the secret screen covers the common shapes, and tool errors are screened ---------------
FAKE = "x9Q2" * 4  # 16 characters of fake material, never a real credential
SECRET_SHAPES = [
    "see https://bob:" + FAKE + "@example.com/repo",
    "pwd=" + FAKE, "pw: " + FAKE, "pass=" + FAKE,
    "Authorization: Basic " + "Ym9iOmh1bnRlcjI=",
    "ASIA" + "ABCDEFGHIJKLMNOP",
    "rk_" + "live_" + FAKE, "sk_" + "test_" + FAKE,
    "id " + "deadbeef" * 3,
]


@pytest.mark.parametrize("text", SECRET_SHAPES)
def test_excerpts_drop_secret_shapes(text: str) -> None:
    from ari import competence
    assert competence.excerpt(text) == ""


def test_ordinary_correction_text_survives_the_screen() -> None:
    from ari import competence
    assert competence.excerpt("no, use the other skill instead") != ""


def test_tool_errors_are_screened_before_they_reach_the_brief() -> None:
    from ari import observer
    leak = "rk_" + "live_" + FAKE
    r = observer._run(["-c", f"import sys; sys.stderr.write('failed with {leak}'); "
                             "sys.exit(1)"], 30)
    assert not r["ok"] and leak not in r["error"] and "failed with" in r["error"]


# C14 caps and paths that had no test -------------------------------------------------------
def _many(env: dict[str, Path], n: int, model: FakeModel) -> list[dict[str, Any]]:
    from ari import probes
    from ari.investigate import Investigation
    invs = {f"s{i:02d}": Investigation.open_skill(f"s{i:02d}", 30) for i in range(n)}
    env["state"].mkdir(parents=True, exist_ok=True)
    ctx = probes.ProbeContext.build([], None, None)
    gov = governor.load_gov(env["state"])
    governor.model_rounds(_cfg(env, FakeObserver(), model), gov, invs, ctx, "d1", T0,
                          env["state"] / "ev", sorted(invs))
    return model.calls


def test_a_tick_makes_at_most_three_calls_of_six_questions(env: dict[str, Path]) -> None:
    from ari import policy
    calls = _many(env, 20, FakeModel([]))
    assert len(calls) == policy.BOUNDS["max_model_rounds"] == 3
    sizes = [len(c["packet"]["questions"]) for c in calls]
    assert sizes == [policy.BOUNDS["max_model_questions"]] * 3 == [6, 6, 6]
    asked = [q["question"] for c in calls for q in c["packet"]["questions"]]
    assert len(set(asked)) == 18  # no question twice in one tick


def _budgets(calls: list[dict[str, Any]]) -> list[float]:
    return [float(c["cmd"][c["cmd"].index("--max-budget-usd") + 1]) for c in calls]


def test_each_call_budget_is_what_is_left_of_the_tick(env: dict[str, Path]) -> None:
    assert _budgets(_many(env, 20, FakeModel([], cost=0.45))) == [1.0, 0.55, 0.1]


def test_no_call_starts_below_five_cents(env: dict[str, Path]) -> None:
    assert _budgets(_many(env, 20, FakeModel([], cost=0.48))) == [1.0, 0.52]


def test_a_canary_is_not_judged_before_its_waiting_period(env: dict[str, Path]) -> None:
    obs, cands, c = _pending(env)
    candidates.owner_approve(c, "APPROVED", T0, YES)
    candidates.owner_approve(c, "CANARY", T0 - timedelta(days=c["verify"]["min_days"] - 1),
                             YES)
    candidates.save(env["state"], cands)
    obs.digest = "d3"
    governor.tick(_cfg(env, obs))
    after = candidates.load(env["state"])[c["id"]]
    assert after["state"] == "CANARY" and not after.get("checks")


def test_the_owner_commands_work_end_to_end(env: dict[str, Path],
                                            monkeypatch: pytest.MonkeyPatch) -> None:
    import ari_operator
    _, cands, c = _pending(env)
    monkeypatch.setattr(candidates, "real_terminal", YES)
    sd = ["--state-dir", str(env["state"])]
    assert ari_operator.main(sd + ["approve", c["id"]]) == 0
    assert candidates.load(env["state"])[c["id"]]["state"] == "APPROVED"
    assert ari_operator.main(sd + ["applied", c["id"]]) == 0
    assert candidates.load(env["state"])[c["id"]]["state"] == "CANARY"
    other = next(k for k, v in cands.items() if v["state"] == "APPROVAL_PENDING"
                 and k != c["id"])
    assert ari_operator.main(sd + ["reject", other, "--reason", "not now"]) == 0
    assert candidates.load(env["state"])[other]["state"] == "REJECTED"
    assert ari_operator.main(sd + ["approve", "C-none"]) == 2


def test_owner_commands_refuse_without_a_terminal(env: dict[str, Path]) -> None:
    import ari_operator
    _, _, c = _pending(env)
    pid = os.fork()
    if pid == 0:
        os.setsid()  # no controlling terminal, as under cron, systemd or an agent shell
        code = ari_operator.main(["--state-dir", str(env["state"]), "approve", c["id"]])
        os._exit(0 if code == 2 else 1)
    _, status = os.waitpid(pid, 0)
    assert os.WEXITSTATUS(status) == 0
    assert candidates.load(env["state"])[c["id"]]["state"] == "APPROVAL_PENDING"


def test_a_probe_supported_hypothesis_cannot_be_declared_unresolved(tmp_path: Path) -> None:
    from ari.discovery import DiscoveryError
    inv, qid, (a, _b, _c) = _three_way(tmp_path)  # b and c stay open: nothing would settle
    with pytest.raises(DiscoveryError, match="supported"):
        inv.apply({"question": qid, "type": "unresolved", "hypothesis": a,
                   "reason": "cannot tell"}, "r")
    assert inv.ledger.hypotheses[a].status == "HYPOTHESIS"


def test_spend_older_than_the_rolling_window_does_not_count(tmp_path: Path) -> None:
    from ari import policy, regulator
    old = (T0 - timedelta(days=policy.BOUNDS["rolling_days"] + 1)).isoformat()
    regulator.record(tmp_path, {"at": old, "ok": True,
                                "cost_usd": policy.BOUNDS["rolling_budget_usd"]})
    assert regulator.spent(tmp_path, T0, policy.BOUNDS["rolling_days"]) == 0.0
    assert regulator.allow_model(tmp_path, {"mode": "NORMAL"}, T0, 0)[0]


# C1 every failure path still writes a run record ------------------------------------------
def _runs(env: dict[str, Path]) -> int:
    import json
    return len(json.loads((env["state"] / "state.json").read_text())["runs"])


def test_a_ledger_that_cannot_be_read_refuses_the_model_but_records_the_tick(
        env: dict[str, Path]) -> None:
    from ari import regulator
    env["state"].mkdir(parents=True)
    (env["state"] / regulator.LEDGER).mkdir()
    model = FakeModel([])
    out = governor.tick(_cfg(env, FakeObserver("d1"), model))
    assert _runs(env) == 1 and not model.calls
    assert out["status"] == "OK" and "ledger unreadable" in out["model_note"]
    assert any("ledger unreadable" in i for i in out["incidents"])


def test_a_wrongly_typed_run_history_is_reset_with_an_incident(env: dict[str, Path]) -> None:
    import json
    governor.tick(_cfg(env, FakeObserver("d1")))
    gov = json.loads((env["state"] / "state.json").read_text())
    gov["runs"], gov["offered"] = "oops", 7
    (env["state"] / "state.json").write_text(json.dumps(gov))
    out = governor.tick(_cfg(env, FakeObserver("d2")))
    assert out["status"] == "OK" and _runs(env) == 1
    assert any("run history" in i for i in out["incidents"])


def test_an_unwritable_brief_still_records_the_run(env: dict[str, Path]) -> None:
    env["state"].mkdir(parents=True)
    (env["state"] / "brief.md").mkdir()
    out = governor.tick(_cfg(env, FakeObserver("d1")))
    assert _runs(env) == 1 and any("brief" in i for i in out["incidents"])
