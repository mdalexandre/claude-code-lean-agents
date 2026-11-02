"""Regression tests for the defects the second blind review found (repair round 5)."""
from __future__ import annotations

import json
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path

import ari_nightly
import pytest
import skill_mcp_audit
import weekly_burn
from ari import audit, evidence, forecast, learn, report
from ari.evidence import Coverage, EvidenceSet, UsageEvent, ingest
from ari_fixtures import asst, usage, write_jsonl

NOW = datetime(2026, 11, 4, 12, 0, tzinfo=timezone.utc)


def test_copies_linking_both_ids_count_once(tmp_path: Path) -> None:
    only_mid = asst("x", "2026-10-26T10:00:00Z", usage(5))
    del only_mid["requestId"]
    only_mid["message"]["id"] = "M"
    both = asst("R", "2026-10-26T10:00:00Z", usage(9))
    both["message"]["id"] = "M"
    only_rid = asst("R", "2026-10-26T10:00:00Z", usage(7))
    del only_rid["message"]["id"]
    write_jsonl(tmp_path / "p" / "s.jsonl", [only_mid, both, only_rid])
    ev = ingest([tmp_path])
    assert ev.coverage.requests == 1 and ev.usage[0].input_tokens == 9


def test_earliest_copy_dates_the_request(tmp_path: Path) -> None:
    write_jsonl(tmp_path / "p" / "a.jsonl", [asst("r", "2026-10-27T00:00:01Z", usage(1))])
    write_jsonl(tmp_path / "p" / "b.jsonl", [asst("r", "2026-10-26T23:59:59Z", usage(1))])
    assert ingest([tmp_path]).usage[0].ts.date().isoformat() == "2026-10-26"


def test_oversize_lines_are_warned(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    small = asst("r", "2026-10-26T10:00:00Z", usage(1))
    monkeypatch.setattr(evidence, "MAX_LINE_BYTES", len(json.dumps(small)) + 50)
    big = asst("big", "2026-10-26T10:00:00Z", usage(1))
    big["message"]["content"] = [{"type": "text", "text": "x" * 400}]
    write_jsonl(tmp_path / "p" / "s.jsonl", [small, big])
    ev = ingest([tmp_path])
    assert ev.coverage.oversize_lines == 1
    assert any("line limit" in w for w in ev.coverage.warnings())
    assert ev.coverage.unread_gaps() == 1


def _tool_tree(tmp_path: Path) -> tuple[Path, Path]:
    skills = tmp_path / "skills"
    (skills / "foo").mkdir(parents=True)
    (skills / "foo" / "SKILL.md").write_text("---\nname: foo\n---\n")
    outside = tmp_path / "outside.md"
    outside.write_text("x")
    os.symlink(outside, skills / "foo" / "ref.md")
    (skills / "zz-secret-client" / "notes").mkdir(parents=True)
    (skills / "zz-secret-client" / "notes" / "x.md").write_text("x")
    reads = [{"type": "tool_use", "id": f"t{i}", "name": n, "input": a} for i, (n, a) in
             enumerate([("Read", {"file_path": str(skills / "foo" / "ref.md")}),
                        ("Read", {"file_path": str(skills / "zz-secret-client" / "notes" /
                                                   "x.md")}),
                        ("Skill", {"skill": "my private words"})])]
    write_jsonl(tmp_path / "root" / "p" / "s.jsonl", [
        {"type": "user", "timestamp": "2026-10-26T10:00:00Z",
         "message": {"role": "user", "content":
                     "<command-name>/mysecretword123</command-name><command-name>/foo"
                     "</command-name>"}},
        asst("r", "2026-10-26T10:00:01Z", usage(1), content=reads)])
    return tmp_path / "root", skills


def test_only_known_skill_names_reach_events(tmp_path: Path) -> None:
    root, skills = _tool_tree(tmp_path)
    ev = ingest([root], want_tools=True, skills_roots=[skills])
    named = [(t.via, t.skill) for t in ev.tools if t.skill]
    assert ("slash_command", "foo") in named
    assert ("skill_file_read", "foo") in named  # a symlinked file inside the folder counts
    text = json.dumps([t.skill for t in ev.tools])
    for leak in ("mysecretword123", "zz-secret-client", "my private words"):
        assert leak not in text


def test_coverage_gaps_block_removal_advice() -> None:
    cap = audit.Capability("x", "skill", installed=True, exposed_sessions=50)
    full = audit.Recommendation("x", "skill", "CONSIDER_REMOVAL", "r", "b", ["e"], "risk", "v")
    assert audit.validate_recommendation(full, cap, 30, 20, 14) is None
    assert "unread" in (audit.validate_recommendation(full, cap, 30, 20, 14, 3) or "")
    assert "window" in (audit.validate_recommendation(full, cap, float("nan"), 20, 14) or "")


@pytest.mark.parametrize("extra", [
    ["--days", "nan"], ["--days", "inf"], ["--days", "1e12"], ["--days", "-1"],
    ["--period-days", "1e-9"], ["--period-days", "inf"], ["--period-days", "1e12"],
    ["--tz-offset-minutes", "99999999999"], ["--period-start", "garbage"],
    ["--period-start", "9999-12-31T23:59:59"], ["--budget", "99999999999999999999999"],
    ["--now", "garbage"],
])
def test_burn_cli_refuses_bad_input(extra: list[str]) -> None:
    with pytest.raises(SystemExit) as exc:
        weekly_burn.main(["--root", "/nonexistent", *extra])
    assert exc.value.code == 2


@pytest.mark.parametrize("extra", [["--min-days", "nan"], ["--min-days", "0.5"],
                                   ["--min-sessions", "0"]])
def test_audit_cli_refuses_bad_thresholds(extra: list[str]) -> None:
    with pytest.raises(SystemExit) as exc:
        skill_mcp_audit.main(["--root", "/nonexistent", *extra])
    assert exc.value.code == 2


def test_stale_anchor_rolls_forward_by_arithmetic() -> None:
    win = forecast.period_window(NOW, datetime(2001, 1, 1, tzinfo=timezone.utc), 1 / 24)
    assert win.start <= NOW < win.end and win.end - win.start == timedelta(hours=1)


def test_window_opening_after_period_start_is_warned() -> None:
    ev = EvidenceSet([], [UsageEvent("r", 0, 1, "s", None, "main", "main", "m", "v",
                                     NOW - timedelta(hours=1), 5, 0, 0, 0, 0)],
                     [], [], Coverage(requests=1), (None, None))
    r = forecast.burn(ev, NOW, data_since=NOW - timedelta(days=1))
    assert any("misses that part" in w for w in r["warnings"])


def test_epoch_like_version_never_reaches_the_share() -> None:
    ev = [UsageEvent(f"r{i}", 0, 1, f"s{i}", f"a{i}", "lean-worker", "subagent", "m",
                     "1761830400.123", NOW, 1, 1, 0, 0, 1) for i in range(6)]
    share = report.build_share(EvidenceSet([], ev, [], [], Coverage(requests=6), (None, None)), 5)
    assert share["claude_code_minor_versions"] == []


def _lesson(tmp_path: Path) -> str:
    from test_ari_learn import NOW as LNOW
    from test_ari_learn import _ev
    learn.nightly(tmp_path, _ev(), LNOW)
    return next(iter(learn.load_state(tmp_path)["lessons"]))


@pytest.mark.parametrize("checker", ["   ", "​"])
def test_blank_checker_is_refused(tmp_path: Path, checker: str) -> None:
    lid = _lesson(tmp_path)
    f = tmp_path / "e.txt"
    f.write_text("evidence")
    with pytest.raises(learn.PromotionError, match="both be named"):
        learn.add_result(tmp_path, lid, "skills", "supports", f, "alice", checker)


@pytest.mark.parametrize("checker", ["AliceSmith", "alice​smith", "ＡＬＩＣＥ smith"])
def test_checker_identity_ignores_all_spacing(checker: str) -> None:
    assert learn._who(checker) == learn._who("Alice Smith")


def test_active_files_no_longer_defer_lessons(tmp_path: Path) -> None:
    """ARI2-R11 replaces the round 5 global deferral: an open session blocks nothing."""
    from test_ari_learn import NOW as LNOW
    from test_ari_learn import _ev
    ev = _ev()
    ev.coverage.files_active = 1
    out = learn.nightly(tmp_path, ev, LNOW)
    assert out["lessons_added"] == 1 and out["active_files_read"] == 1


def test_nightly_cli_never_tracebacks_on_a_damaged_state(
        tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    lid = _lesson(tmp_path)
    state = json.loads((tmp_path / "state.json").read_text())
    state["hypotheses"] = {k: "damaged" for k in state["hypotheses"]}  # not a dict
    (tmp_path / "state.json").write_text(json.dumps(state))
    rc = ari_nightly.main(["promote", "--state-dir", str(tmp_path), "--id", lid, "--to",
                           "CANDIDATE", "--actor", "me"])
    assert rc == 2 and "refused" in capsys.readouterr().err


def test_nightly_cli_prints_coverage_warnings(tmp_path: Path,
                                              capsys: pytest.CaptureFixture[str]) -> None:
    (tmp_path / "empty").mkdir()
    rc = ari_nightly.main(["run", "--state-dir", str(tmp_path / "st"), "--root",
                           str(tmp_path / "empty"), "--now", "2026-11-01T00:00:00Z"])
    assert rc == 0 and "warning: no countable request" in capsys.readouterr().err
