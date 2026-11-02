from __future__ import annotations

import json
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path

import ari_nightly
import pytest
from ari import learn
from ari.abduction import MAX_DEPTH, child, context_gap, record_result, validate
from ari.evidence import Coverage, EvidenceSet, ExposureEvent, UsageEvent

NOW = datetime(2026, 11, 1, 3, 0, tzinfo=timezone.utc)


def _ev(jump_day: int | None = 1, expo_change: bool = False) -> EvidenceSet:
    """Ten days of lean-worker runs, 3 per day at 7k; from `jump_day` days back, 20k."""
    usage, expo = [], []
    for back in range(10, 0, -1):
        day = datetime(2026, 11, 1, 9, tzinfo=timezone.utc) - timedelta(days=back)
        size = 20000 if jump_day is not None and back <= jump_day else 7000
        for i in range(3):
            aid = f"a{back}-{i}"
            usage.append(UsageEvent(f"r{aid}", 0, 1, f"s{back}", aid, "lean-worker",
                                    "subagent", "m", "2.1.316", day + timedelta(minutes=i),
                                    1, size, 0, 0, 10))
        n = 40 if expo_change and back <= (jump_day or 0) else 30
        expo.append(ExposureEvent(0, f"s{back}", day, "skill", "snapshot",
                                  tuple(f"k{j}" for j in range(n))))
    return EvidenceSet([], usage, [], expo, Coverage(requests=len(usage)), (None, None))


def test_nightly_detects_and_is_idempotent(tmp_path: Path) -> None:
    ev = _ev(expo_change=True)
    first = learn.nightly(tmp_path, ev, NOW)
    assert first["lessons_added"] == 1
    second = learn.nightly(tmp_path, ev, NOW)
    assert second["lessons_added"] == 0 and second["snapshots_added"] == 0
    st = learn.load_state(tmp_path)
    assert len(st["lessons"]) == 1 and len(st["runs"]) == 2
    lesson = next(iter(st["lessons"].values()))
    assert lesson["state"] == "SPECIFIED"  # ARI2-R13: v1 HYPOTHESIZED
    for key in ("evidence_refs", "claim", "scope", "configurations", "alternatives",
                "trigger", "rollback", "scope_versions", "evidence_hashes",
                "proposed_experiment", "expected_benefit", "failure_modes",
                "verification_state", "version", "content_hash"):
        assert lesson[key] not in (None, "", [], {})
    hs = st["hypotheses"][lesson["hypothesis_set"]]
    skills = next(x for x in hs["explanations"] if x["id"] == "skills")
    assert skills["supporting"]  # the exposure change is recorded as a signal
    assert hs["state"] == "OPEN" and hs["confidence"] in ("unknown", "low")


def test_no_change_no_lesson(tmp_path: Path) -> None:
    assert learn.nightly(tmp_path, _ev(jump_day=None), NOW)["lessons_added"] == 0


def test_promotion_contract(tmp_path: Path) -> None:
    """ARI2-R13 and R14: v1 names still work as aliases; each step keeps its contract."""
    learn.nightly(tmp_path, _ev(), NOW)
    lid = next(iter(learn.load_state(tmp_path)["lessons"]))
    with pytest.raises(learn.PromotionError):
        learn.promote(tmp_path, lid, "TESTED", "me")  # skipping a state
    learn.promote(tmp_path, lid, "CANDIDATE", "me")
    out = learn.promote(tmp_path, lid, "TESTED", "me")  # runs the replay
    assert out["state"] == "REPLAY_TESTED" and out["replay"]["passed"]
    with pytest.raises(learn.PromotionError, match="recorded experiment"):
        learn.promote(tmp_path, lid, "VALIDATED", "me")
    ev_file = tmp_path / "result.txt"
    ev_file.write_text("ablation: skills off, start 7.1k; on, 20.0k")
    learn.add_result(tmp_path, lid, "skills", "supports", ev_file, "me", "me")
    with pytest.raises(learn.PromotionError, match="checker other than"):
        learn.promote(tmp_path, lid, "VALIDATED", "me")
    learn.add_result(tmp_path, lid, "mcp", "refutes", ev_file, "me", "checker-2")
    with pytest.raises(learn.PromotionError, match="supporting result"):
        learn.promote(tmp_path, lid, "VALIDATED", "me")
    learn.add_result(tmp_path, lid, "skills", "supports", ev_file, "me", "checker-2")
    learn.promote(tmp_path, lid, "VALIDATED", "me")
    learn.promote(tmp_path, lid, "APPROVAL_PENDING", "me")
    with pytest.raises(learn.PromotionError, match="approval file"):
        learn.promote(tmp_path, lid, "RETAINED", "me", owner_approved=True)
    lesson = learn.load_state(tmp_path)["lessons"][lid]
    appr = tmp_path / "approval.json"
    appr.write_text(json.dumps({"schema": learn.APPROVAL_SCHEMA, "lesson_id": lid,
                                "content_hash": lesson["content_hash"], "approved_by": "owner"}))
    out = learn.promote(tmp_path, lid, "RETAINED", "owner", approval_file=appr)
    assert out["state"] == "PROMOTED"
    assert [h["state"] for h in out["history"]][-1] == "PROMOTED"


def test_nightly_never_promotes_beyond_hypothesized(tmp_path: Path) -> None:
    for _ in range(3):
        learn.nightly(tmp_path, _ev(), NOW)
    states = {x["state"] for x in learn.load_state(tmp_path)["lessons"].values()}
    assert states <= {"DISCOVERED", "SPECIFIED"}


def test_failed_tests_reject_and_keep_the_lesson(tmp_path: Path) -> None:
    learn.nightly(tmp_path, _ev(), NOW)
    st = learn.load_state(tmp_path)
    lid = next(iter(st["lessons"]))
    ids = [x["id"] for x in st["hypotheses"][st["lessons"][lid]["hypothesis_set"]]["explanations"]]
    f = tmp_path / "e.txt"
    f.write_text("refuted")
    for x in ids:
        out = learn.add_result(tmp_path, lid, x, "refutes", f, "me", "c")
    assert out["state"] == "QUARANTINED"  # ARI2-R13: v1 REJECTED
    assert lid in learn.load_state(tmp_path)["lessons"]
    with pytest.raises(learn.PromotionError):
        learn.promote(tmp_path, lid, "CANDIDATE", "me")


def test_interrupted_run_recovers(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    learn.nightly(tmp_path, _ev(), NOW)
    good = (tmp_path / "state.json").read_text()

    def crash(src: object, dst: object) -> None:
        raise OSError("power cut")

    monkeypatch.setattr(os, "replace", crash)
    with pytest.raises(OSError):
        learn.nightly(tmp_path, _ev(), NOW + timedelta(hours=1))
    monkeypatch.undo()
    assert (tmp_path / "state.json").read_text() == good  # old state intact
    assert (tmp_path / "run.journal").exists()
    out = learn.nightly(tmp_path, _ev(), NOW + timedelta(hours=2))
    assert out["recovered_from"] == "20261101T040000Z"
    assert len(learn.load_state(tmp_path)["lessons"]) == 1
    assert not (tmp_path / "run.journal").exists()


def test_cli_refuses_bad_promotion(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    learn.nightly(tmp_path, _ev(), NOW)
    lid = next(iter(learn.load_state(tmp_path)["lessons"]))
    rc = ari_nightly.main(["promote", "--state-dir", str(tmp_path), "--id", lid, "--to",
                           "VALIDATED", "--actor", "me"])
    assert rc == 2 and "refused" in capsys.readouterr().err
    assert ari_nightly.main(["status", "--state-dir", str(tmp_path)]) == 0
    assert json.loads(capsys.readouterr().out)["lessons"] == {"SPECIFIED": 1}  # ARI2-R13 name


def test_abduction_contract_and_bounds() -> None:
    hs = context_gap("obs", ["ref"], {})
    assert validate(hs) == [] and hs.primary is None and hs.confidence == "unknown"
    hs2 = context_gap("obs", ["ref"], {"mcp": ["a", "b"], "skills": ["c"]})
    assert hs2.primary == "mcp" and hs2.confidence == "low" and hs2.state == "OPEN"
    record_result(hs2, "mcp", "refutes", "probe-1")
    assert hs2.primary is None and any(x.state == "REJECTED" for x in hs2.explanations)
    assert "probe-1" in next(x for x in hs2.explanations if x.id == "mcp").conflicting
    bad = context_gap("obs", [], {})
    bad.explanations = bad.explanations[:1]
    assert validate(bad)
    node = hs
    for _ in range(MAX_DEPTH + 1):
        node = child(node, "deeper", ["r"], {}, node_count=1)
    assert node.state == "BUDGET_EXHAUSTED"
    assert child(hs, "x", ["r"], {}, node_count=24).state == "BUDGET_EXHAUSTED"


def test_correlated_exposure_change_is_not_a_cause(tmp_path: Path) -> None:
    """A context jump that coincides with a skill exposure change stays a hypothesis."""
    learn.nightly(tmp_path, _ev(expo_change=True), NOW)
    st = learn.load_state(tmp_path)
    hs = next(iter(st["hypotheses"].values()))
    assert hs["state"] != "SUPPORTED" and hs["confidence"] != "medium"
    assert "change" in next(iter(st["lessons"].values()))["claim"]
