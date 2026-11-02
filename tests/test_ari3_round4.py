"""ARI 3.0 defects found by the third blind review (2026-11-02), one test per defect."""
from __future__ import annotations

import json
import os
from datetime import timedelta
from pathlib import Path
from typing import Any

import pytest
from ari import candidates, governor, investigator, policy, probes, regulator
from ari3_env import FakeModel, FakeObserver, audit, build
from test_ari3_round3 import T0, YES, _cfg, _pending

FAKE = "x9Q2" * 4  # fake material, never a real credential


@pytest.fixture()
def env(tmp_path: Path) -> dict[str, Path]:
    return build(tmp_path / "env") | {"state": tmp_path / "state"}


# A model calls are always recorded, and asked once per digest even when they raise -----
class Raising(FakeModel):
    def __init__(self, exc: BaseException) -> None:
        super().__init__([])
        self.exc = exc

    def __call__(self, cmd: list[str], stdin: str, timeout: float,
                 cwd: Path) -> tuple[int, str, str]:
        self.calls.append({"cmd": cmd, "packet": json.loads(stdin)})
        raise self.exc


def test_a_runner_that_raises_is_recorded_at_the_budget_and_not_repeated(
        env: dict[str, Path]) -> None:
    obs, model = FakeObserver("d1"), Raising(RuntimeError("boom"))
    out = governor.tick(_cfg(env, obs, model))
    rows = [r for r in regulator.rows(env["state"]) if not r.get("event")]
    assert out["status"] == "OK" and rows and not rows[0]["ok"]
    assert rows[0]["cost_usd"] == pytest.approx(policy.BOUNDS["tick_budget_usd"])
    n = len(model.calls)
    governor.tick(_cfg(env, obs, model))
    assert len(model.calls) == n


@pytest.mark.parametrize("field", ["cost", "tokens"])
def test_huge_integers_from_the_model_are_survived(tmp_path: Path, field: str) -> None:
    env_ = {"total_cost_usd": 10 ** 400 if field == "cost" else 0.1,
            "usage": {"input_tokens": 10 ** 400 if field == "tokens" else 5},
            "structured_output": {"proposals": []}}
    res = investigator.investigate({"questions": []}, 0.4, 10, tmp_path,
                                   lambda c, s, t, w: (0, json.dumps(env_), ""))
    assert res.cost_usd == pytest.approx(0.4 if field == "cost" else 0.1)
    assert 0 <= res.input_tokens <= 10 ** 12


def test_huge_integers_in_the_ledger_are_survived(tmp_path: Path) -> None:
    (tmp_path / regulator.LEDGER).write_text(json.dumps(
        {"at": T0.isoformat(), "ok": True, "cost_usd": 10 ** 400,
         "status_changes": 10 ** 400}) + "\n")
    total = regulator.spent(tmp_path, T0, 7)
    assert total == pytest.approx(policy.BOUNDS["tick_budget_usd"])
    json.dumps(regulator.self_report(tmp_path, T0), allow_nan=False)


def test_a_call_killed_midway_is_still_counted_and_not_repeated(env: dict[str, Path]) -> None:
    obs, model = FakeObserver("d1"), Raising(SystemExit(9))
    with pytest.raises(SystemExit):
        governor.tick(_cfg(env, obs, model))
    assert regulator.spent(env["state"], T0, 7) >= 0.05
    later = FakeModel([])
    governor.tick(_cfg(env, obs, later))
    asked = {q["question"] for c in model.calls for q in c["packet"]["questions"]}
    again = {q["question"] for c in later.calls for q in c["packet"]["questions"]}
    assert asked and not asked & again


def test_a_completed_call_is_not_counted_twice(env: dict[str, Path]) -> None:
    model = FakeModel([], cost=0.2)
    governor.tick(_cfg(env, FakeObserver("d1"), model))
    assert regulator.spent(env["state"], T0, 7) == pytest.approx(0.2 * len(model.calls))


# C canaries are judged only on evidence that exists, each on its own window -----------------
def _canary(env: dict[str, Path], kind: str = "telemetry_defect"
            ) -> tuple[FakeObserver, dict[str, Any]]:
    obs, cands, c = _pending(env)
    c = next(x for x in cands.values() if x["kind"] == kind)
    candidates.owner_approve(c, "APPROVED", T0, YES)
    candidates.owner_approve(c, "CANARY", T0 - timedelta(days=20), YES)
    candidates.save(env["state"], cands)
    return obs, c


def test_a_canary_is_not_judged_without_transcripts(env: dict[str, Path],
                                                    tmp_path: Path) -> None:
    obs, c = _canary(env)
    obs.audit, obs.digest = audit(eps_parsed=3), "d3"
    empty = tmp_path / "empty"
    empty.mkdir()
    cfg = _cfg(env, obs)
    cfg.roots = [str(empty)]
    out = governor.tick(cfg)
    assert candidates.load(env["state"])[c["id"]]["state"] == "CANARY"
    assert any(c["id"] in i and "evidence" in i for i in out["incidents"])


def test_a_canary_is_not_judged_while_transcripts_are_unread(env: dict[str, Path]) -> None:
    obs, c = _canary(env)
    a = audit(eps_parsed=3)
    a["coverage"] = {"unread_gaps": 4}
    obs.audit, obs.digest = a, "d3"
    governor.tick(_cfg(env, obs))
    assert candidates.load(env["state"])[c["id"]]["state"] == "CANARY"


def test_each_canary_is_judged_on_a_window_from_its_own_start(
        env: dict[str, Path], monkeypatch: pytest.MonkeyPatch) -> None:
    seen: list[Any] = []
    real = probes.ProbeContext.build

    def spy(roots: Any, since: Any, *a: Any, **k: Any) -> probes.ProbeContext:
        seen.append(since)
        return real(roots, since, *a, **k)
    base = {"kind": "telemetry_defect", "target": "eps", "proposal": "p",
            "verify": {"min_days": 1, "spec": probes.spec("skill_raw_invocations", {"skill": "eps"},
                                                          (">", 0), ("==", 0))}}
    cands = {}
    for i, hour in enumerate((9, 18)):
        c = candidates.make(base["kind"], f"eps{i}", "p", base["verify"], {}, 1.0, T0)
        c["state"], c["canary"] = "CANARY", {"started": (T0 - timedelta(days=5)).replace(
            hour=hour).isoformat()}
        c["approval"] = {"content_hash": c["content_hash"]}
        cands[c["id"]] = c
    monkeypatch.setattr(probes.ProbeContext, "build", staticmethod(spy))
    governor.verify_canaries(cands, _cfg(env, FakeObserver()), T0, audit(), [])
    starts = sorted(s.hour for s in seen if s is not None)
    assert starts == [9, 18]


# D tool and model error text never carries a secret value into the brief ------------------
@pytest.mark.parametrize("text,value", [
    ("Authorization: Bearer " + FAKE, FAKE),
    ("password=" + FAKE + " next", FAKE),
    ("api_key: " + FAKE, FAKE),
    ("redis://:" + FAKE + "@cache.internal:6379/0", FAKE),
    ("https://bob:" + FAKE + "@example.com/x", FAKE),
    ("token " + FAKE, FAKE),
])
def test_redact_removes_the_value_not_only_the_keyword(text: str, value: str) -> None:
    from ari import competence
    assert value not in competence.redact(text)


def test_model_error_text_is_screened(env: dict[str, Path]) -> None:
    class Leaky(FakeModel):
        def __call__(self, cmd: list[str], stdin: str, timeout: float,
                     cwd: Path) -> tuple[int, str, str]:
            self.calls.append({"cmd": cmd, "packet": json.loads(stdin)})
            return 1, "", "Invalid API key: sk-ant-" + FAKE + " password=" + FAKE
    governor.tick(_cfg(env, FakeObserver("d1"), Leaky([])))
    for name in ("brief.md", "brief.json", regulator.LEDGER):
        assert FAKE not in (env["state"] / name).read_text(), name


# E a pair of rules must be able to decide both ways ---------------------------------------
@pytest.mark.parametrize("sup,ref", [
    ((">=", 0), ("<", 0)),    # always supports a count
    (("<", 0), (">=", 0)),    # always refutes a count
    ((">", 5), (">", 7)),     # refutes only where supports also holds
    ((">", 5), ("<", 7)),     # each can hold alone, and both hold at 6
])
def test_a_rule_pair_that_cannot_decide_both_ways_is_refused(sup: tuple[str, float],
                                                             ref: tuple[str, float]) -> None:
    assert probes.validate_spec(probes.spec("skill_raw_invocations", {"skill": "x"}, sup, ref))


def test_an_ordinary_rule_pair_is_accepted() -> None:
    assert not probes.validate_spec(probes.spec("skill_raw_invocations", {"skill": "x"},
                                                (">", 0), ("==", 0)))
    assert not probes.validate_spec(probes.spec("skill_raw_minus_parsed", {"skill": "x"},
                                                (">", 0), ("<=", 0)))


# F hostile state never stops every later tick ----------------------------------------------
def test_deeply_nested_state_is_set_aside(env: dict[str, Path]) -> None:
    env["state"].mkdir(parents=True)
    (env["state"] / "state.json").write_text("[" * 200000)
    (env["state"] / regulator.LEDGER).write_text("[" * 200000 + "\n")
    out = governor.tick(_cfg(env, FakeObserver("d1")))
    assert out["status"] == "OK"
    assert len(json.loads((env["state"] / "state.json").read_text())["runs"]) == 1


@pytest.mark.parametrize("edit", [
    {"canary": {"started": "2026-10-01T00:00:00"}},           # no time zone
    {"verify": {"min_days": 7, "spec": {"probe": "skill_raw_invocations"}}},  # no args
    {"verify": {"min_days": 10 ** 10, "spec": probes.spec(
        "skill_raw_invocations", {"skill": "eps"}, (">", 0), ("==", 0))}},
    {"checks": "x"},
    {"benefit": 10 ** 400},
])
def test_a_wrongly_typed_canary_is_quarantined(env: dict[str, Path],
                                               edit: dict[str, Any]) -> None:
    obs, c = _canary(env)
    cands = candidates.load(env["state"])
    cands[c["id"]].update(edit)
    (env["state"] / "candidates.json").write_text(json.dumps(
        {"schema": candidates.SCHEMA, "candidates": cands}, default=str))
    obs.digest = "d3"
    out = governor.tick(_cfg(env, obs))
    held = json.loads((env["state"] / "candidates.quarantine.json").read_text())
    assert out["status"] == "OK" and any(k.startswith(c["id"] + "@") for k in held)
    again = candidates.load(env["state"]).get(c["id"])  # re-proposed from scratch, if at all
    assert again is None or (again["state"] == "CANDIDATE" and not again.get("approval"))
    obs.digest = "d4"
    assert governor.tick(_cfg(env, obs))["status"] == "OK"


def _break_inv(env: dict[str, Path], how: str) -> None:
    p = env["state"] / "investigations.json"
    d = json.loads(p.read_text())
    name = sorted(d["investigations"])[0]
    inv = d["investigations"][name]
    q = inv["ledger"]["questions"][0]
    if how == "depth":
        q["depth"] = "x"
    elif how == "hyps":
        q["hypotheses"] = "abc"
    elif how == "results":
        inv["ledger"]["hypotheses"][0]["results"] = [5]
    elif how == "spec":
        inv["specs"][next(iter(inv["specs"]))] = 5
    elif how == "root":
        inv["root"] = "Q-none"
    p.write_text(json.dumps(d))


@pytest.mark.parametrize("how", ["depth", "hyps", "results", "spec", "root"])
def test_a_wrongly_typed_investigation_is_quarantined(env: dict[str, Path], how: str) -> None:
    obs = FakeObserver("d1")
    governor.tick(_cfg(env, obs))
    _break_inv(env, how)
    obs.digest = "d2"
    out = governor.tick(_cfg(env, obs))
    assert out["status"] == "OK" and any("investigation" in i for i in out["incidents"])
    obs.digest = "d3"
    assert governor.tick(_cfg(env, obs))["status"] == "OK"


def test_a_tool_with_bad_bytes_or_deep_json_is_a_failed_tool() -> None:
    from ari import observer
    r1 = observer._run(["-c", "import sys; sys.stderr.buffer.write(b'\\xff\\xfe'); "
                              "sys.exit(1)"], 30)
    r2 = observer._run(["-c", "print('[' * 200000)"], 30)
    assert not r1["ok"] and not r2["ok"]


def test_any_crash_in_a_tick_is_recorded(env: dict[str, Path],
                                         monkeypatch: pytest.MonkeyPatch) -> None:
    def boom(*a: Any, **k: Any) -> None:
        raise RuntimeError("unexpected")
    monkeypatch.setattr(governor, "_tick", boom)
    out = governor.tick(_cfg(env, FakeObserver("d1")))
    assert out["status"] == "FAILED"
    assert "unexpected" in (env["state"] / "tick_crashes.jsonl").read_text()


# G the depth bound cannot be raised by editing stored depths -------------------------------
def test_stored_depths_are_recomputed_from_parents() -> None:
    from ari import discovery
    led = discovery.Ledger()
    root = led.ask("root")
    child = led.ask("child", root.id)
    doc = led.to_dict()
    for q in doc["questions"]:
        q["depth"] = -20
    back = discovery.Ledger.from_dict(doc)
    assert back.questions[root.id].depth == 0 and back.questions[child.id].depth == 1


# I every program a tick executes is pinned -------------------------------------------------
def test_the_tools_a_tick_runs_are_pinned() -> None:
    for f in ("scripts/skill_mcp_audit.py", "scripts/weekly_burn.py", "scripts/ari_nightly.py"):
        assert f in policy.PROTECTED


def test_a_missing_pinned_file_is_a_problem(tmp_path: Path) -> None:
    import shutil
    root = policy.repo_root()
    for f in policy.PROTECTED:
        (tmp_path / f).parent.mkdir(parents=True, exist_ok=True)
        shutil.copy(root / f, tmp_path / f)
    (tmp_path / policy.PIN_PATH).parent.mkdir(parents=True, exist_ok=True)
    policy.write_pin(tmp_path)
    (tmp_path / "tests/test_ari3_gates.py").unlink()
    assert any("missing" in p for p in policy.integrity(tmp_path))


# K paths the suite did not exercise ---------------------------------------------------------
@pytest.mark.parametrize("cmd", ["resume", "pin", "applied"])
def test_owner_commands_need_a_terminal(env: dict[str, Path], cmd: str) -> None:
    import ari_operator
    _, cands, c = _pending(env)
    if cmd == "applied":
        candidates.owner_approve(c, "APPROVED", T0, YES)
        candidates.save(env["state"], cands)
    pin = (policy.repo_root() / policy.PIN_PATH).read_bytes()
    argv = ["--state-dir", str(env["state"]), cmd] + ([c["id"]] if cmd == "applied" else [])
    pid = os.fork()
    if pid == 0:
        os.setsid()
        os._exit(0 if ari_operator.main(argv) == 2 else 1)
    _, status = os.waitpid(pid, 0)
    assert os.WEXITSTATUS(status) == 0
    assert (policy.repo_root() / policy.PIN_PATH).read_bytes() == pin


@pytest.mark.parametrize("status,code", [("SAFE", 3), ("BUSY", 75), ("OK", 0), ("FAILED", 1)])
def test_tick_exit_codes(env: dict[str, Path], monkeypatch: pytest.MonkeyPatch,
                         status: str, code: int) -> None:
    import ari_operator
    monkeypatch.setattr(ari_operator, "tick", lambda cfg: {"status": status})
    assert ari_operator.main(["--state-dir", str(env["state"]), "tick", "--no-model"]) == code


def test_skipped_states_and_foreign_approvals_are_refused() -> None:
    c = candidates.make("telemetry_defect", "t", "p", {"min_days": 1, "spec": probes.spec(
        "skill_raw_invocations", {"skill": "t"}, (">", 0), ("==", 0))}, {}, 1.0, T0)
    with pytest.raises(candidates.CandidateError):
        candidates.transition(c, "APPROVAL_PENDING", "governor", "", T0)
    c["state"] = "APPROVAL_PENDING"
    for bad in ({"candidate": "C-other", "content_hash": c["content_hash"], "terminal": True},
                {"candidate": c["id"], "content_hash": c["content_hash"], "terminal": False}):
        with pytest.raises(candidates.CandidateError):
            candidates.transition(c, "APPROVED", candidates.OWNER, "", T0, bad)


# L the remaining review 3 findings: unusable state paths, owner states, screens, coverage --
def _runs(env: dict[str, Path]) -> int:
    return len(json.loads((env["state"] / "state.json").read_text())["runs"])


@pytest.mark.parametrize("name", [".lock", ".journal"])
def test_an_unusable_lock_or_journal_is_set_aside(env: dict[str, Path], name: str) -> None:
    env["state"].mkdir(parents=True)
    (env["state"] / name).mkdir()
    out = governor.tick(_cfg(env, FakeObserver("d1")))
    assert out["status"] == "OK" and _runs(env) == 1


def test_a_binary_journal_is_set_aside(env: dict[str, Path]) -> None:
    env["state"].mkdir(parents=True)
    (env["state"] / ".journal").write_bytes(b"\xff\xfe\x00")
    out = governor.tick(_cfg(env, FakeObserver("d1")))
    assert out["status"] == "OK" and any("journal" in i for i in out["incidents"])


def test_an_interrupted_tick_is_reported(env: dict[str, Path]) -> None:
    env["state"].mkdir(parents=True)
    (env["state"] / ".journal").write_text("20261031T000000Z")
    out = governor.tick(_cfg(env, FakeObserver("d1")))
    assert any("interrupted tick" in i for i in out["incidents"])


@pytest.mark.parametrize("name", ["evidence", "model-cwd", "observe", "nightly"])
def test_a_file_where_a_folder_belongs_is_set_aside(env: dict[str, Path], name: str) -> None:
    env["state"].mkdir(parents=True)
    (env["state"] / name).write_text("x")
    model = FakeModel([])
    out = governor.tick(_cfg(env, FakeObserver("d1"), model))
    assert out["status"] == "OK" and any(f"{name} was not a folder" in i
                                         for i in out["incidents"])


@pytest.mark.parametrize("doc", ["[]", "5", '{"schema": "ari-candidates/1"}', "[" * 200000])
def test_the_safe_path_survives_a_bad_candidate_store(env: dict[str, Path], doc: str,
                                                      monkeypatch: pytest.MonkeyPatch) -> None:
    governor.tick(_cfg(env, FakeObserver("d1")))
    (env["state"] / "candidates.json").write_text(doc)
    monkeypatch.setattr(policy, "integrity", lambda root=None: ["x: changed"])
    out = governor.tick(_cfg(env, FakeObserver("d2")))
    assert out["status"] == "SAFE" and _runs(env) == 2


def test_a_canary_without_an_owner_approval_is_quarantined(env: dict[str, Path]) -> None:
    obs, cands, c = _pending(env)
    c["state"], c["canary"], c["approval"] = "CANARY", {"started": T0.isoformat()}, None
    candidates.save(env["state"], cands)
    obs.digest = "d3"
    governor.tick(_cfg(env, obs))
    held = json.loads((env["state"] / "candidates.quarantine.json").read_text())
    assert any(k.startswith(c["id"] + "@") for k in held)


def test_the_approval_prompt_shows_the_rule_and_no_control_characters(
        env: dict[str, Path]) -> None:
    _, _, c = _pending(env)
    c["proposal"] = "restrict it\x1b[2J\x07 now"
    c["content_hash"] = candidates.content_hash(c)
    shown: list[str] = []
    candidates.owner_approve(c, "APPROVED", T0, lambda s, code: shown.append(s) or True)
    assert c["verify"]["spec"]["probe"] in shown[0]
    assert "\x1b" not in shown[0] and "\x07" not in shown[0]


@pytest.mark.parametrize("text", [
    "redis://:" + FAKE + "@cache.internal", "https://user:pa/ss@host.example/x",
    "https://" + FAKE + "@github.com/x", "Cookie: session=" + FAKE,
    "my password is x", "passwd here", "passphrase here", "a token here", "authorization",
    "the api key", "credential", "Basic " + FAKE, "x" * 40,
])
def test_excerpts_drop_secret_shapes(text: str) -> None:
    from ari import competence
    assert competence.excerpt(text) == ""


def test_excerpts_are_short() -> None:
    from ari import competence
    assert len(competence.excerpt("word " * 500)) <= 200


def test_the_brief_keeps_units_and_labels() -> None:
    from ari import brief
    _, _, c = None, None, candidates.make("routing_correction", "eps", "route it", {
        "min_days": 7, "spec": probes.spec("skill_raw_invocations", {"skill": "eps"},
                                           (">", 0), ("==", 0))}, {}, 4.0, T0)
    c["state"], c["benefit_unit"] = "APPROVAL_PENDING", "matching_prompts"
    text, doc = brief.render({"incidents": []}, {"at": T0.isoformat()}, {c["id"]: c}, {},
                             ["maybe it is dormant"], {})
    assert "4 matching_prompts" in text and "unverified" in text
    assert doc["decisions"][0]["benefit_unit"] == "matching_prompts"


def test_an_inconclusive_canary_stays_a_canary(env: dict[str, Path],
                                               monkeypatch: pytest.MonkeyPatch) -> None:
    import math
    obs, c = _canary(env)
    monkeypatch.setattr(probes, "run", lambda ctx, spec: (math.nan, {}))
    cands = candidates.load(env["state"])
    governor.verify_canaries(cands, _cfg(env, obs), T0, audit(), [])
    assert cands[c["id"]]["state"] == "CANARY"
    assert cands[c["id"]]["checks"][-1]["outcome"] == "inconclusive"


def test_policy_outcomes_count_only_rollbacks_after_the_canary_started(
        env: dict[str, Path]) -> None:
    pol = candidates.make("policy_revision", "routing_correction", "stop", {
        "min_days": 1, "spec": {"probe": "policy_outcomes",
                                "args": {"kind": "routing_correction"},
                                "supports_if": {"op": "==", "value": 0},
                                "refutes_if": {"op": ">", "value": 0}}}, {}, 1.0, T0)
    pol["state"], pol["canary"] = "CANARY", {"started": (T0 - timedelta(days=5)).isoformat()}
    pol["approval"] = {"content_hash": pol["content_hash"]}
    old = candidates.make("routing_correction", "x", "p", {"min_days": 1, "spec": probes.spec(
        "skill_raw_invocations", {"skill": "x"}, (">", 0), ("==", 0))}, {}, 1.0, T0)
    old["state"] = "ROLLED_BACK"
    old["history"].append({"to": "ROLLED_BACK", "at": (T0 - timedelta(days=9)).isoformat()})
    cands = {pol["id"]: pol, old["id"]: old}
    governor.verify_canaries(cands, _cfg(env, FakeObserver()), T0, audit(), [])
    assert pol["state"] == "VERIFIED"


def test_a_call_is_recorded_when_handling_its_reply_raises(
        env: dict[str, Path], monkeypatch: pytest.MonkeyPatch) -> None:
    def boom(*a: Any, **k: Any) -> None:
        raise RuntimeError("after the call")
    monkeypatch.setattr(probes, "prefetch", boom)
    model = FakeModel([], cost=0.2)
    governor.tick(_cfg(env, FakeObserver("d1"), model))
    rows = [r for r in regulator.rows(env["state"]) if not r.get("event")]
    assert model.calls and len(rows) == len(model.calls) and rows[0]["cost_usd"] == 0.2


def test_a_fifth_hypothesis_is_refused() -> None:
    from ari import discovery
    led = discovery.Ledger()
    q = led.ask("why")
    for i in range(4):
        led.propose(q.id, f"mechanism {i}", "test", ["p"], "f")
    with pytest.raises(discovery.DiscoveryError):
        led.propose(q.id, "mechanism 4", "test", ["p"], "f")


@pytest.mark.parametrize("key", ["supports_if", "refutes_if"])
def test_both_rules_are_validated(key: str) -> None:
    s = probes.spec("skill_raw_invocations", {"skill": "x"}, (">", 0), ("==", 0))
    s[key] = {"op": "~", "value": 1}
    assert probes.validate_spec(s)


@pytest.mark.parametrize("frm", ["CANARY", "VERIFIED", "ROLLED_BACK", "REJECTED"])
def test_no_rejection_after_the_change_is_live(frm: str) -> None:
    c = candidates.make("telemetry_defect", "t", "p", {"min_days": 1, "spec": probes.spec(
        "skill_raw_invocations", {"skill": "t"}, (">", 0), ("==", 0))}, {}, 1.0, T0)
    c["state"] = frm
    with pytest.raises(candidates.CandidateError):
        candidates.transition(c, "REJECTED", "owner", "", T0)


def test_a_rarely_listed_skill_is_not_qualified() -> None:
    c = {"kind": "restrict_exposure", "target": "t"}
    assert governor._quality(c, {"t": {"exposed": 19}}, {}, 0, False, "d")
    assert governor._quality(c, {"t": {"exposed": 20}}, {}, 0, False, "d") is None


def test_atomic_write_refuses_nan_and_keeps_the_old_file(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    p = tmp_path / "x.json"
    candidates.atomic_write(p, {"a": 1})
    with pytest.raises(ValueError):
        candidates.atomic_write(p, {"a": float("nan")})

    def fail(*a: Any) -> None:
        raise OSError("disk")
    monkeypatch.setattr(os, "replace", fail)
    with pytest.raises(OSError):
        candidates.atomic_write(p, {"a": 2})
    assert json.loads(p.read_text()) == {"a": 1} and sorted(tmp_path.iterdir()) == [p]


def test_unchanged_evidence_is_reported_unchanged(env: dict[str, Path]) -> None:
    obs = FakeObserver("d1")
    governor.tick(_cfg(env, obs))
    assert governor.tick(_cfg(env, obs))["changed"] is False


@pytest.mark.parametrize("cmd", ["approve", "applied", "reject", "resume"])
def test_owner_commands_wait_for_a_running_tick(env: dict[str, Path], cmd: str) -> None:
    import fcntl

    import ari_operator
    _, _, c = _pending(env)
    argv = ["--state-dir", str(env["state"]), cmd] + ([] if cmd == "resume" else [c["id"]])
    argv += ["--reason", "r"] if cmd == "reject" else []
    with open(env["state"] / ".lock", "w") as held:
        fcntl.flock(held, fcntl.LOCK_EX | fcntl.LOCK_NB)
        assert ari_operator.main(argv) == 75
    assert candidates.load(env["state"])[c["id"]]["state"] == "APPROVAL_PENDING"
