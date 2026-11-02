"""ARI 2.0 procedural learning (H5): ARI2-R11 to R15, on real transcript files."""
from __future__ import annotations

import json
import os
import shutil
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import ari_nightly
import pytest
from ari import learn
from ari.evidence import ingest
from ari_fixtures import asst, usage, write_jsonl
from test_ari_learn import NOW as MEM_NOW
from test_ari_learn import _ev as mem_ev

NOW = datetime(2026, 11, 1, 3, tzinfo=timezone.utc)


def _write_day(root: Path, back: int, size: int, mtime: float | None = None) -> list[Path]:
    day = datetime(2026, 11, 1, 9, tzinfo=timezone.utc) - timedelta(days=back)
    files = []
    for i in range(3):
        sub = root / "p" / f"s{back}" / "subagents"
        f = sub / f"agent-a{back}x{i}.jsonl"
        write_jsonl(f, [asst(f"r{back}-{i}", (day + timedelta(minutes=i)).isoformat(),
                             usage(1, size, 0, 10))])
        (sub / f"agent-a{back}x{i}.meta.json").write_text(json.dumps({"agentType":
                                                                      "lean-worker"}))
        t = mtime if mtime is not None else (day + timedelta(hours=1)).timestamp()
        os.utime(f, (t, t))
        files.append(f)
    return files


def _tree(root: Path, jump: bool = True) -> dict[int, list[Path]]:
    return {b: _write_day(root, b, 20000 if (jump and b == 1) else 7000)
            for b in range(10, 0, -1)}


def _night(root: Path, state: Path, now: datetime = NOW) -> dict[str, Any]:
    ev = ingest([root], since=now - timedelta(days=14), until=now, want_exposure=True,
                quiet_seconds=1800, now=now, skip_active=False)
    return learn.nightly(state, ev, now)


def test_active_file_beside_stable_files_does_not_block(tmp_path: Path) -> None:
    files = _tree(tmp_path / "root")
    t = (NOW - timedelta(seconds=60)).timestamp()
    os.utime(files[1][0], (t, t))  # one file of the jump day is still being written
    out = _night(tmp_path / "root", tmp_path / "st")
    assert out["active_files_read"] == 1 and out["lessons_added"] == 1


def test_rerun_is_idempotent(tmp_path: Path) -> None:
    _tree(tmp_path / "root")
    _night(tmp_path / "root", tmp_path / "st")
    before = learn.load_state(tmp_path / "st")
    out = _night(tmp_path / "root", tmp_path / "st")
    after = learn.load_state(tmp_path / "st")
    assert out["snapshots_added"] == 0 and out["lessons_added"] == 0
    assert out["source_changes"] == {"UNCHANGED": 30}
    for k in ("snapshots", "lessons", "hypotheses"):
        assert before[k] == after[k], k
    strip = {"last_change"}  # the only field a rerun may move: NEW becomes UNCHANGED
    assert {p: {f: v for f, v in r.items() if f not in strip}
            for p, r in before["manifest"].items()} == {
        p: {f: v for f, v in r.items() if f not in strip} for p, r in after["manifest"].items()}
    assert {r["last_change"] for r in after["manifest"].values()} == {"UNCHANGED"}


def test_interrupted_run_recovers_to_the_clean_result(tmp_path: Path) -> None:
    _tree(tmp_path / "root")
    _night(tmp_path / "root", tmp_path / "clean")
    (tmp_path / "crashed").mkdir()
    (tmp_path / "crashed" / "run.journal").write_text("20261101T020000Z")
    out = _night(tmp_path / "root", tmp_path / "crashed")
    assert out["recovered_from"] == "20261101T020000Z"
    a, b = learn.load_state(tmp_path / "clean"), learn.load_state(tmp_path / "crashed")
    for k in ("snapshots", "lessons", "hypotheses", "manifest"):
        assert a[k] == b[k], k
    assert not (tmp_path / "crashed" / "run.journal").exists()


def test_replaced_file_makes_its_day_unstable_until_stable(tmp_path: Path) -> None:
    root = tmp_path / "root"
    files = _tree(root, jump=False)
    assert _night(root, tmp_path / "st")["lessons_added"] == 0
    _write_day(root, 1, 20000)  # the jump day's files are rewritten in place
    out = _night(root, tmp_path / "st")
    assert out["source_changes"].get("REPLACED") == 3
    assert out["unstable_days"] == ["2026-10-31"] and out["lessons_added"] == 0
    assert learn.load_state(tmp_path / "st")["snapshots"]["2026-10-31"]["unstable"]
    out = _night(root, tmp_path / "st")  # nothing changed since: the day is stable again
    assert out["unstable_days"] == [] and out["lessons_added"] == 1
    assert "unstable" not in learn.load_state(tmp_path / "st")["snapshots"]["2026-10-31"]
    assert files


def test_truncated_file_is_detected_and_never_shrinks_the_day(tmp_path: Path) -> None:
    root = tmp_path / "root"
    files = _tree(root, jump=False)
    _night(root, tmp_path / "st")
    files[3][0].write_bytes(b"")
    t = (NOW - timedelta(days=2)).timestamp()
    os.utime(files[3][0], (t, t))
    out = _night(root, tmp_path / "st")
    assert out["source_changes"].get("TRUNCATED") == 1
    assert out["unstable_days"] == ["2026-10-29"]
    assert learn.load_state(tmp_path / "st")["snapshots"]["2026-10-29"]["requests"] == 3


def _to_pending(state: Path) -> str:
    learn.nightly(state, mem_ev(), MEM_NOW)
    lid = str(next(iter(learn.load_state(state)["lessons"])))
    f = state / "e.txt"
    f.write_text("ablation result")
    learn.promote(state, lid, "CANDIDATE", "me")
    learn.promote(state, lid, "REPLAY_TESTED", "me")
    learn.add_result(state, lid, "mcp", "refutes", f, "me", "checker-2")
    learn.add_result(state, lid, "skills", "supports", f, "me", "checker-2")
    learn.promote(state, lid, "QUALIFIED", "me")
    learn.promote(state, lid, "APPROVAL_PENDING", "me")
    return lid


def _approval(state: Path, lid: str, **over: object) -> Path:
    lesson = learn.load_state(state)["lessons"][lid]
    data = {"schema": learn.APPROVAL_SCHEMA, "lesson_id": lid,
            "content_hash": lesson["content_hash"], "approved_by": "owner", **over}
    p = state / "approval.json"
    p.write_text(json.dumps(data))
    return p


@pytest.mark.parametrize("over,match", [
    ({"lesson_id": "L-other"}, "another lesson"),
    ({"content_hash": "0" * 64}, "other content"),
    ({"approved_by": "  "}, "no approver"),
    ({"schema": "x"}, "is not"),
])
def test_unauthorized_promotion_is_refused(tmp_path: Path, over: dict[str, object],
                                           match: str) -> None:
    lid = _to_pending(tmp_path)
    with pytest.raises(learn.PromotionError, match="approval file"):
        learn.promote(tmp_path, lid, "PROMOTED", "me", owner_approved=True)
    with pytest.raises(learn.PromotionError, match=match):
        learn.promote(tmp_path, lid, "PROMOTED", "me", approval_file=_approval(tmp_path, lid,
                                                                               **over))
    assert learn.load_state(tmp_path)["lessons"][lid]["state"] == "APPROVAL_PENDING"


def test_code_change_after_replay_blocks_promotion(tmp_path: Path,
                                                   monkeypatch: pytest.MonkeyPatch) -> None:
    lid = _to_pending(tmp_path)
    appr = _approval(tmp_path, lid)
    monkeypatch.setattr(learn, "code_sha256", lambda: "f" * 64)
    with pytest.raises(learn.PromotionError, match="changed since the replay"):
        learn.promote(tmp_path, lid, "PROMOTED", "owner", approval_file=appr)


def test_skipping_approval_pending_is_refused(tmp_path: Path) -> None:
    lid = _to_pending(tmp_path)
    st = learn.load_state(tmp_path)
    st["lessons"][lid]["state"] = "QUALIFIED"
    learn.save_state(tmp_path, st)
    with pytest.raises(learn.PromotionError, match="next state"):
        learn.promote(tmp_path, lid, "PROMOTED", "owner", approval_file=_approval(tmp_path,
                                                                                  lid))


def test_authorized_promotion_writes_only_the_lesson_state(tmp_path: Path) -> None:
    lid = _to_pending(tmp_path)
    appr = _approval(tmp_path, lid)
    before = sorted(p.name for p in tmp_path.iterdir())
    out = learn.promote(tmp_path, lid, "PROMOTED", "owner", approval_file=appr)
    assert out["state"] == "PROMOTED"
    assert sorted(p.name for p in tmp_path.iterdir()) == before


def test_changed_evidence_quarantines_the_candidate(tmp_path: Path) -> None:
    learn.nightly(tmp_path, mem_ev(), MEM_NOW)
    lid = next(iter(learn.load_state(tmp_path)["lessons"]))
    learn.promote(tmp_path, lid, "CANDIDATE", "me")
    st = learn.load_state(tmp_path)
    day = st["lessons"][lid]["trigger"]["day"]
    st["snapshots"][day]["start_context"]["lean-worker"]["median"] = 7001  # no jump left
    learn.save_state(tmp_path, st)
    with pytest.raises(learn.PromotionError, match="QUARANTINED"):
        learn.promote(tmp_path, lid, "REPLAY_TESTED", "me")
    lesson = learn.load_state(tmp_path)["lessons"][lid]
    assert lesson["state"] == "QUARANTINED"
    assert any("did not reproduce" in p for p in lesson["replay"]["problems"])
    assert lesson["history"][-1]["reason"]  # evidence of the failure is kept
    f = tmp_path / "e.txt"
    f.write_text("x")
    with pytest.raises(learn.PromotionError):
        learn.add_result(tmp_path, lid, "skills", "supports", f, "me", "c")


def test_counterexample_firing_quarantines(tmp_path: Path) -> None:
    learn.nightly(tmp_path, mem_ev(), MEM_NOW)
    lid = next(iter(learn.load_state(tmp_path)["lessons"]))
    st = learn.load_state(tmp_path)
    base = st["lessons"][lid]["trigger"]["baseline_days"]
    for d in base[-1:]:  # the last baseline day jumps too: the trigger is not specific
        st["snapshots"][d]["start_context"]["lean-worker"]["median"] = 30000
    learn.save_state(tmp_path, st)
    res = learn.replay(tmp_path, lid)
    assert not res["passed"] and any("counterexample" in p for p in res["problems"])


def test_replay_reports_versions_not_covered(tmp_path: Path) -> None:
    learn.nightly(tmp_path, mem_ev(), MEM_NOW)
    lid = next(iter(learn.load_state(tmp_path)["lessons"]))
    st = learn.load_state(tmp_path)
    st["lessons"][lid]["scope_versions"] = ["2.0.1"]
    learn.save_state(tmp_path, st)
    assert learn.replay(tmp_path, lid)["versions_not_covered"] == ["2.1.316"]


def test_v1_state_is_migrated_with_a_backup(tmp_path: Path) -> None:
    learn.nightly(tmp_path, mem_ev(), MEM_NOW)
    st = json.loads((tmp_path / "state.json").read_text())
    st["schema"] = "ari-learn-state/1"
    st.pop("manifest")
    for lesson in st["lessons"].values():
        lesson["state"] = "HYPOTHESIZED"
    raw = json.dumps(st)
    (tmp_path / "state.json").write_text(raw)
    assert {x["state"] for x in learn.load_state(tmp_path)["lessons"].values()} == {
        "SPECIFIED"}
    learn.nightly(tmp_path, mem_ev(), MEM_NOW)
    assert (tmp_path / "state.json.v1.bak").read_text() == raw
    assert learn.load_state(tmp_path)["schema"] == "ari-learn-state/2"


def test_nightly_cli_replay_and_alias(tmp_path: Path, capsys: pytest.CaptureFixture[str]
                                      ) -> None:
    learn.nightly(tmp_path, mem_ev(), MEM_NOW)
    lid = next(iter(learn.load_state(tmp_path)["lessons"]))
    assert ari_nightly.main(["replay", "--state-dir", str(tmp_path), "--id", lid]) == 0
    assert json.loads(capsys.readouterr().out)["passed"] is True
    assert ari_nightly.main(["promote", "--state-dir", str(tmp_path), "--id", lid, "--to",
                             "RETAINED", "--actor", "me", "--owner-approved"]) == 2


def test_real_tree_end_to_end_copy(tmp_path: Path) -> None:
    """The state directory is self contained: copying it gives the same next run."""
    _tree(tmp_path / "root")
    _night(tmp_path / "root", tmp_path / "a")
    shutil.copytree(tmp_path / "a", tmp_path / "b")
    x = _night(tmp_path / "root", tmp_path / "a")
    y = _night(tmp_path / "root", tmp_path / "b")
    assert x == y


def test_lesson_edited_after_approval_is_refused(tmp_path: Path) -> None:
    """D3: the approval binds the content as it is now, not the hash stored beside it."""
    lid = _to_pending(tmp_path)
    appr = _approval(tmp_path, lid)
    st = learn.load_state(tmp_path)
    st["lessons"][lid]["scope_versions"].append("9.9.9")  # the stored hash is left alone
    learn.save_state(tmp_path, st)
    with pytest.raises(learn.PromotionError, match="other content"):
        learn.promote(tmp_path, lid, "PROMOTED", "owner", approval_file=appr)
    assert learn.load_state(tmp_path)["lessons"][lid]["state"] == "APPROVAL_PENDING"


def test_replay_recomputes_snapshot_hashes(tmp_path: Path) -> None:
    """D3: a snapshot edited in place, with its stored hash kept, fails replay."""
    learn.nightly(tmp_path, mem_ev(), MEM_NOW)
    lid = next(iter(learn.load_state(tmp_path)["lessons"]))
    st = learn.load_state(tmp_path)
    day = st["lessons"][lid]["trigger"]["baseline_days"][0]
    st["snapshots"][day]["requests"] += 1  # the trigger still reproduces
    learn.save_state(tmp_path, st)
    res = learn.replay(tmp_path, lid)
    assert not res["passed"]
    assert res["problems"] == [f"snapshot {day} changed since the lesson was built"]


@pytest.mark.parametrize("bad", ["nan", "-5", "inf"])
def test_nightly_refuses_a_bad_quiet_window(tmp_path: Path, bad: str,
                                            capsys: pytest.CaptureFixture[str]) -> None:
    """D7: --quiet-seconds must be a finite, non negative number."""
    with pytest.raises(SystemExit) as exc:
        ari_nightly.main(["run", "--state-dir", str(tmp_path / "st"), "--root",
                          str(tmp_path), "--quiet-seconds", bad])
    assert exc.value.code == 2 and "quiet-seconds" in capsys.readouterr().err
    assert not (tmp_path / "st").exists()
