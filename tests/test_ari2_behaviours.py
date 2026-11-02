"""ARI 2.0 defect D5: behaviours the mutation harness showed no test pinned down.

Each test here has a matching control in tests/mutation_controls.py that breaks the
behaviour and must make this file fail.
"""
from __future__ import annotations

import os
from datetime import datetime, timezone
from pathlib import Path

import pytest
from ari import evidence, learn
from ari.evidence import duplicate_class, ingest
from ari_fixtures import asst, usage, write_jsonl
from test_ari2_learn import _approval as approval_file
from test_ari2_learn import _night, _to_pending, _tree, _write_day


def test_marker_scan_reads_the_whole_file(tmp_path: Path) -> None:
    """A transcript marker on a late line still makes an unknown file a gap."""
    write_jsonl(tmp_path / "p" / "s.jsonl", [asst("r", "2026-10-26T10:00:00Z", usage(1))])
    write_jsonl(tmp_path / "p" / "odd" / "x.jsonl", [{"filename": f"f{i}"} for i in range(5)]
                + [asst("h", "2026-10-26T10:00:00Z", usage(1))])
    cov = ingest([tmp_path]).coverage
    assert (cov.files_unrecognized, cov.files_foreign) == (1, 0)


@pytest.mark.parametrize("a,b", [((0, 0, 0, 0, 5), (0, 0, 0, 0, 0)),
                                 ((0, 0, 0, 0, 0), (10, 0, 0, 500, 5))])
def test_a_zeroed_copy_wins_over_growth_and_ambiguity(a: tuple[int, ...],
                                                      b: tuple[int, ...]) -> None:
    assert duplicate_class(a, b) == "ZEROED_COPY"


def test_stream_growth_needs_all_four_input_fields_equal() -> None:
    assert duplicate_class((10, 0, 0, 500, 5), (10, 0, 0, 600, 9)) == "AMBIGUOUS"


def test_a_change_inside_the_window_is_replaced(tmp_path: Path) -> None:
    """The head (first 64 KiB) is unchanged; only the window before the offset differs."""
    f = tmp_path / "s.jsonl"
    body = b"x" * (evidence.HEAD_BYTES + 2 * evidence.WINDOW_BYTES) + b"\n"
    f.write_bytes(body)
    row = {"size": len(body), "offset": len(body), **evidence.checkpoint(f, len(body))}
    f.write_bytes(body[:-10] + b"y" * 9 + b"\n")
    assert evidence.compare_checkpoint(row, f) == "REPLACED"


@pytest.mark.skipif(os.geteuid() == 0, reason="root reads a mode 000 file")
def test_an_unreadable_journal_is_counted(tmp_path: Path) -> None:
    write_jsonl(tmp_path / "p" / "s.jsonl", [asst("r", "2026-10-26T10:00:00Z", usage(1))])
    j = tmp_path / "p" / "s" / "subagents" / "workflows" / "wf_1" / "journal.jsonl"
    write_jsonl(j, [{"type": "started", "agentId": "aa"}])
    j.chmod(0)
    try:
        cov = ingest([tmp_path]).coverage
    finally:
        j.chmod(0o600)
    assert cov.files_unreadable == 1 and cov.files_workflow_journal == 0
    assert any("unreadable" in w or "could not be read" in w for w in cov.warnings())


def test_a_parser_change_after_replay_blocks_promotion(tmp_path: Path,
                                                       monkeypatch: pytest.MonkeyPatch) -> None:
    lid = _to_pending(tmp_path)
    appr = approval_file(tmp_path, lid)
    monkeypatch.setattr("ari.learn.PARSER_VERSION", evidence.PARSER_VERSION + "-next")
    with pytest.raises(learn.PromotionError, match="changed since the replay"):
        learn.promote(tmp_path, lid, "PROMOTED", "owner", approval_file=appr)


def test_the_run_day_is_never_snapshotted(tmp_path: Path) -> None:
    """The day the nightly runs on is still open, so it is partial and never stored."""
    root = tmp_path / "root"
    _tree(root)
    _write_day(root, 0, 7000)  # today, 09:00 to 09:02
    now = datetime(2026, 11, 1, 12, tzinfo=timezone.utc)
    _night(root, tmp_path / "st", now)
    snaps = learn.load_state(tmp_path / "st")["snapshots"]
    assert "2026-10-31" in snaps and "2026-11-01" not in snaps


def _snap(median: int, unstable: bool = False) -> dict[str, object]:
    s: dict[str, object] = {"start_context": {"lean-worker": {"median": median, "runs": 3}}}
    if unstable:
        s["unstable"] = "run-x"
    return s


def test_an_unstable_day_is_not_a_baseline() -> None:
    """D6: a day whose source files are still changing cannot anchor a comparison."""
    snaps = {"2026-10-26": _snap(7000), "2026-10-27": _snap(7000, unstable=True),
             "2026-10-28": _snap(7000), "2026-10-29": _snap(20000)}
    assert learn.detect(snaps, "2026-10-29") == []  # two stable baseline days are too few
    snaps["2026-10-25"] = _snap(7000)
    [a] = learn.detect(snaps, "2026-10-29")
    assert a["baseline_days"] == ["2026-10-25", "2026-10-26", "2026-10-28"]


@pytest.mark.parametrize("link", [("p", "odd"), ("p", "s", "deep")],
                         ids=["depth-2", "depth-3"])
def test_a_symlinked_directory_out_of_the_root_is_counted(tmp_path: Path,
                                                          link: tuple[str, ...]) -> None:
    """D7: a directory link below the transcript layout is reported, never silent."""
    outside = tmp_path / "outside"
    write_jsonl(outside / "x.jsonl", [asst("x", "2026-10-26T10:00:00Z", usage(1))])
    root = tmp_path / "root"
    write_jsonl(root / "p" / "s.jsonl", [asst("r", "2026-10-26T10:00:00Z", usage(1))])
    target = root.joinpath(*link)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.symlink_to(outside, target_is_directory=True)
    cov = ingest([root]).coverage
    assert cov.requests == 1 and cov.files_outside_root == 1
    assert cov.files_unrecognized == 0 and cov.files_foreign == 0


def test_a_directory_link_inside_the_root_is_not_a_gap(tmp_path: Path) -> None:
    root = tmp_path / "root"
    write_jsonl(root / "p" / "s.jsonl", [asst("r", "2026-10-26T10:00:00Z", usage(1))])
    (root / "p" / "loop").symlink_to(root / "p", target_is_directory=True)
    cov = ingest([root]).coverage
    assert cov.requests == 1 and cov.files_outside_root == 0


def _session(ts: str, rid: str) -> list[dict[str, object]]:
    return [{"type": "attachment", "timestamp": ts,
             "attachment": {"type": "skill_listing", "content": "- zeta: z"}},
            {"type": "assistant", "timestamp": ts, "requestId": rid,
             "message": {"id": rid, "model": "m", "usage": {"input_tokens": 1,
                                                             "output_tokens": 1},
                         "content": [{"type": "tool_use", "id": f"t{rid}", "name": "Skill",
                                      "input": {"skill": "zeta"}}]}}]


def test_tool_and_exposure_records_respect_the_window(tmp_path: Path) -> None:
    """D8 (found in the real data check): only usage was cut at --now, so tool calls and
    skill listings from after the window reached the audit and a fixed --now was not
    repeatable."""
    skills = tmp_path / "skills" / "zeta"
    skills.mkdir(parents=True)
    (skills / "SKILL.md").write_text("---\nname: zeta\n---\n")
    root = tmp_path / "root"
    write_jsonl(root / "p" / "in.jsonl", _session("2026-10-26T10:00:00Z", "a"))
    write_jsonl(root / "p" / "late.jsonl", _session("2026-11-02T08:00:00Z", "b"))
    write_jsonl(root / "p" / "early.jsonl", _session("2026-09-01T08:00:00Z", "c"))
    since = datetime(2026, 10, 4, 22, tzinfo=timezone.utc)
    until = datetime(2026, 11, 1, 22, tzinfo=timezone.utc)
    ev = ingest([root], since=since, until=until, want_tools=True, want_exposure=True,
                skills_roots=[tmp_path / "skills"])
    assert {u.request_id for u in ev.usage} == {"a"}
    assert {t.session_id for t in ev.tools} == {"in"}
    assert {e.session_id for e in ev.exposures} == {"in"}
