"""Regression tests for the defects an independent blind review found (repair round 4)."""
from __future__ import annotations

import json
import os
from dataclasses import asdict
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
import skill_mcp_audit
import weekly_burn
from ari import audit, forecast, learn
from ari.evidence import (
    Coverage,
    EvidenceSet,
    UsageEvent,
    ingest,
    skill_names_from_listing,
)
from ari_fixtures import asst, usage, write_jsonl

NOW = datetime(2026, 11, 4, 12, 0, tzinfo=timezone.utc)


def test_workflow_subagents_are_read_and_unknown_files_warned(tmp_path: Path) -> None:
    wf = tmp_path / "p" / "sess" / "subagents" / "workflows" / "wf_1"
    write_jsonl(wf / "agent-w1.jsonl", [asst("w1", "2026-10-26T10:00:00Z", usage(5))])
    (wf / "agent-w1.meta.json").write_text(json.dumps({"agentType": "lean-worker"}))
    write_jsonl(wf / "journal.jsonl", [{"type": "note"}])
    # ARI2-R1 (v2.0): a journal is an index, not an unread gap; a marked file in an unknown
    # place still is
    write_jsonl(tmp_path / "p" / "odd" / "deep" / "x.jsonl",
                [asst("hidden", "2026-10-26T10:00:00Z", usage(1))])
    ev = ingest([tmp_path])
    assert [(u.request_id, u.agent_type) for u in ev.usage] == [("w1", "lean-worker")]
    assert ev.coverage.files_workflow_journal == 1
    assert ev.coverage.files_unrecognized == 1
    assert any("unrecognised locations" in w for w in ev.coverage.warnings())


def test_copy_with_only_message_id_is_the_same_request(tmp_path: Path) -> None:
    a = asst("r", "2026-10-26T10:00:00Z", usage(100))
    b = asst("r", "2026-10-26T10:00:00Z", usage(100))
    del b["requestId"]  # same message.id, no requestId
    write_jsonl(tmp_path / "p" / "s.jsonl", [a, b])
    ev = ingest([tmp_path])
    assert ev.coverage.requests == 1 and ev.usage[0].input_tokens == 100


def test_paths_never_become_skill_names(tmp_path: Path) -> None:
    skills = tmp_path / "skills"
    (skills / "real").mkdir(parents=True)
    (skills / "real" / "SKILL.md").write_text("---\nname: real\n---\n")
    (skills / "PRIVATE_NOTES.md").write_text("x")
    reads = [{"type": "tool_use", "id": f"t{i}", "name": "Read", "input": {"file_path": p}}
             for i, p in enumerate([str(skills / "PRIVATE_NOTES.md"),
                                    str(skills / "real" / "SKILL.md")])]
    write_jsonl(tmp_path / "root" / "p" / "s.jsonl", [
        {"type": "user", "timestamp": "2026-10-26T10:00:00Z",
         "message": {"role": "user",
                     "content": "<command-name>/home/me/private/path</command-name>"}},
        asst("r", "2026-10-26T10:00:01Z", usage(1), content=reads)])
    ev = ingest([tmp_path / "root"], want_tools=True, skills_roots=[skills])
    named = {t.skill for t in ev.tools if t.skill}
    assert named == {"real"}
    assert "private" not in json.dumps([asdict(t) for t in ev.tools], default=str)


def test_namespaced_skill_names_keep_their_colon() -> None:
    listing = ("- stripe:test-cards: Display cards\n- stripe:explain-error: Explain\n"
               "- hookify:writing-rules (hookify:writing-hookify-rules): Use\n- plain: x")
    assert set(skill_names_from_listing(listing)) == {
        "stripe:test-cards", "stripe:explain-error", "hookify:writing-rules", "plain"}


def test_symlinked_skill_outside_root_is_not_read(tmp_path: Path) -> None:
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "SKILL.md").write_text("---\nname: LEAKED_NAME_FROM_OUTSIDE\n---\n")
    skills = tmp_path / "skills"
    (skills / "evil").mkdir(parents=True)
    os.symlink(outside / "SKILL.md", skills / "evil" / "SKILL.md")
    (skills / "good").mkdir()
    (skills / "good" / "SKILL.md").write_text("---\nname: good\n---\n")
    assert set(audit.skill_inventory([str(skills)])) == {"good"}


def _days(values: dict[int, int]) -> EvidenceSet:
    ev = [UsageEvent(f"r{b}", 0, 1, "s", None, "main", "main", "m", "v",
                     datetime(2026, 11, 4, 6, tzinfo=timezone.utc) - timedelta(days=b),
                     n, 0, 0, 0, 0) for b, n in values.items()]
    return EvidenceSet([], ev, [], [], Coverage(requests=len(ev)), (None, None))


def test_period_days_must_be_positive() -> None:
    with pytest.raises(ValueError):
        forecast.period_window(NOW, None, 0)
    with pytest.raises(SystemExit):
        weekly_burn.main(["--root", "/nonexistent", "--period-days", "0"])


def test_idle_days_do_not_satisfy_the_history_floor() -> None:
    r = forecast.burn(_days({4: 500}), NOW)  # one day with usage, three idle days
    assert r["forecast"]["status"] == "INSUFFICIENT_DATA"


def test_partial_oldest_day_is_not_history() -> None:
    ev = _days({b: 1000 for b in range(1, 6)})
    full = forecast.burn(ev, NOW)
    cut = forecast.burn(ev, NOW, data_since=datetime(2026, 10, 30, 12, tzinfo=timezone.utc))
    assert full["forecast"]["history_days"] == 5
    assert cut["forecast"]["history_days"] == 4  # 2026-10-30 opened at noon: partial


def _learn_ev(start: datetime, days: int, opened: datetime) -> EvidenceSet:
    ev = [UsageEvent(f"r{d}-{i}", 0, 1, f"s{d}", f"a{d}-{i}", "lean-worker", "subagent", "m",
                     "v", start + timedelta(days=d, hours=1 + i), 1, 7000, 0, 0, 1)
          for d in range(days) for i in range(3)]
    return EvidenceSet([], ev, [], [], Coverage(requests=len(ev)), (opened.isoformat(), None))


def test_next_night_never_shrinks_a_stored_day(tmp_path: Path) -> None:
    start = datetime(2026, 10, 13, tzinfo=timezone.utc)
    first = _learn_ev(start, 13, start)
    learn.nightly(tmp_path, first, datetime(2026, 10, 26, 12, tzinfo=timezone.utc))
    assert learn.load_state(tmp_path)["snapshots"]["2026-10-13"]["requests"] == 3
    # next night: the window opens at 2026-10-13T12:00, so that day is partial
    later = _learn_ev(start, 14, datetime(2026, 10, 13, 12, tzinfo=timezone.utc))
    learn.nightly(tmp_path, later, datetime(2026, 10, 27, 12, tzinfo=timezone.utc))
    assert learn.load_state(tmp_path)["snapshots"]["2026-10-13"]["requests"] == 3


def test_deleted_files_never_shrink_a_complete_day(tmp_path: Path) -> None:
    start = datetime(2026, 10, 13, tzinfo=timezone.utc)
    learn.nightly(tmp_path, _learn_ev(start, 13, start), datetime(2026, 10, 26, 12,
                                                                     tzinfo=timezone.utc))
    thinner = _learn_ev(start, 13, start)
    thinner.usage = [u for u in thinner.usage if not u.request_id.startswith("r0-")]
    thinner.usage += _learn_ev(start, 1, start).usage[:1]  # one of three runs left on 10-13
    learn.nightly(tmp_path, thinner, datetime(2026, 10, 26, 13, tzinfo=timezone.utc))
    assert learn.load_state(tmp_path)["snapshots"]["2026-10-13"]["requests"] == 3


def test_empty_night_adds_no_snapshots_and_warns(tmp_path: Path) -> None:
    ev = EvidenceSet([], [], [], [], Coverage(), (None, None))
    out = learn.nightly(tmp_path, ev, NOW)
    assert out["snapshots_added"] == 0 and "missing data" in out["warning"]


def _lesson(tmp_path: Path) -> str:
    from test_ari_learn import NOW as LNOW
    from test_ari_learn import _ev
    learn.nightly(tmp_path, _ev(), LNOW)
    return next(iter(learn.load_state(tmp_path)["lessons"]))


def test_checker_identity_ignores_case_and_spacing(tmp_path: Path) -> None:
    lid = _lesson(tmp_path)
    f = tmp_path / "e.txt"
    f.write_text("evidence")
    learn.promote(tmp_path, lid, "CANDIDATE", "Alice")
    ids = [x["id"] for x in learn.load_state(tmp_path)["hypotheses"][
        learn.load_state(tmp_path)["lessons"][lid]["hypothesis_set"]]["explanations"]]
    other = next(x for x in ids if x != "skills")
    learn.add_result(tmp_path, lid, other, "refutes", f, "Alice", "bob")
    learn.add_result(tmp_path, lid, "skills", "supports", f, "Alice", " alice ")
    learn.promote(tmp_path, lid, "TESTED", "Alice")
    with pytest.raises(learn.PromotionError, match="checker other than"):
        learn.promote(tmp_path, lid, "VALIDATED", "Alice")


def test_result_recorded_before_candidate_does_not_open_tested(tmp_path: Path) -> None:
    lid = _lesson(tmp_path)
    f = tmp_path / "e.txt"
    f.write_text("evidence")
    learn.add_result(tmp_path, lid, "skills", "supports", f, "me", "c")  # still SPECIFIED
    learn.promote(tmp_path, lid, "CANDIDATE", "me")
    learn.promote(tmp_path, lid, "TESTED", "me")  # v2: the replay is the test of this step
    with pytest.raises(learn.PromotionError, match="recorded experiment"):
        learn.promote(tmp_path, lid, "VALIDATED", "me")


def test_audit_cli_text_prints_warnings_and_refuses_zero_thresholds(
        tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    (tmp_path / "empty").mkdir()
    base = ["--root", str(tmp_path / "empty"), "--skills-root", str(tmp_path / "none"),
            "--mcp-config", str(tmp_path / "none.json"), "--now", "2026-11-01T00:00:00Z"]
    assert skill_mcp_audit.main(base) == 0
    assert "missing data, not zero usage" in capsys.readouterr().out
    with pytest.raises(SystemExit):
        skill_mcp_audit.main(base + ["--min-sessions", "0"])
