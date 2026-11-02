from __future__ import annotations

import json
import os
import sys
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path

import pytest
from ari.evidence import ingest, skill_names_from_listing
from ari_fixtures import PROMPT, SECRET, asst, build, usage, write_jsonl

T0 = datetime(2026, 10, 20, tzinfo=timezone.utc)
T1 = datetime(2026, 11, 1, tzinfo=timezone.utc)


@pytest.fixture()
def tree(tmp_path: Path) -> dict[str, Path]:
    paths = build(tmp_path / "root", tmp_path / "skills", tmp_path / "outside")
    paths["root"] = tmp_path / "root"
    paths["skills"] = tmp_path / "skills"
    return paths


def _ev(tree: dict[str, Path], **kw: object):  # type: ignore[no-untyped-def]
    return ingest([tree["root"]], since=T0, until=T1, skills_roots=[tree["skills"]], **kw)


def test_streamed_copies_count_once(tree: dict[str, Path]) -> None:
    ev = _ev(tree)
    r1 = [u for u in ev.usage if u.request_id == "r1"]
    assert len(r1) == 1 and r1[0].copies == 3
    assert r1[0].cache_creation_5m == 600 and r1[0].cache_creation_1h == 400
    assert ev.coverage.duplicate_records == 3  # r1: 2 extra copies; r2: 1 conflicting copy
    # main: r1, r2, r6; subagents: s1a, s1b, s2a, s3a, s4a
    assert ev.coverage.requests == 8


def test_conflicting_copies_flagged_and_max_kept(tree: dict[str, Path]) -> None:
    ev = _ev(tree)
    r2 = next(u for u in ev.usage if u.request_id == "r2")
    assert r2.conflict and r2.output == 99  # partial first copy, final later copy
    assert r2.input_tokens == 50 and r2.cache_read == 2000
    assert ev.coverage.duplicate_conflicts == 1


def test_zeroed_resume_copy_is_order_independent(tmp_path: Path) -> None:
    real, zero = usage(2, 3639, 317196, 2585), usage(0, 0, 0, 0)
    totals = []
    for first, second in ((real, zero), (zero, real)):
        root = tmp_path / str(len(totals))
        write_jsonl(root / "p" / "a.jsonl", [asst("rq", "2026-10-12T14:11:36Z", first)])
        write_jsonl(root / "p" / "b.jsonl", [asst("rq", "2026-10-12T14:11:36Z", second)])
        ev = ingest([root])
        assert ev.coverage.requests == 1 and ev.coverage.duplicate_conflicts == 1
        totals.append(ev.usage[0].value("total"))
    assert totals[0] == totals[1] == 2 + 3639 + 317196 + 2585


def test_skipped_records_are_counted_not_guessed(tree: dict[str, Path]) -> None:
    c = _ev(tree).coverage
    assert c.missing_request_id == 1
    assert c.synthetic_skipped == 1
    assert c.invalid_timestamp == 1
    assert c.negative_or_bad_tokens == 1
    assert c.malformed_lines == 1
    assert c.partial_last_lines == 1
    assert c.cache_split_fallback == 5  # r6 plus four subagent runs without the split
    assert any("no request id" in w for w in c.warnings())


def test_missing_agent_type_is_unknown_not_general_purpose(tree: dict[str, Path]) -> None:
    ev = _ev(tree)
    types = {u.request_id: u.agent_type for u in ev.usage}
    assert types["s3a"] == "unknown" and types["s4a"] == "unknown"
    assert "general-purpose" not in types.values()
    assert ev.coverage.missing_agent_type == 2


def test_symlink_outside_root_is_refused(tree: dict[str, Path]) -> None:
    ev = _ev(tree)
    assert ev.coverage.files_outside_root == 1
    assert all(u.request_id != "x1" for u in ev.usage)


def test_root_with_dotdot_still_bounded(tree: dict[str, Path]) -> None:
    weird = tree["root"] / "proj-a" / ".."
    ev = ingest([weird], since=T0, until=T1)
    assert all(u.request_id != "x1" for u in ev.usage)


def test_out_of_order_first_request(tree: dict[str, Path]) -> None:
    first = _ev(tree).first_requests()
    a1 = next(v for (s, a), v in first.items() if a == "a1")
    assert a1.request_id == "s1a" and a1.context == 7002


def test_window_bounds_inclusive_exclusive(tree: dict[str, Path]) -> None:
    since = datetime(2026, 10, 26, 10, 0, 0, tzinfo=timezone.utc)
    until = datetime(2026, 10, 26, 11, 0, 0, tzinfo=timezone.utc)
    ev = ingest([tree["root"]], since=since, until=until)
    assert sorted(u.request_id for u in ev.usage) == ["s1a", "s1b"]


def test_mtime_prefilter_does_not_drop_recent_files(tree: dict[str, Path]) -> None:
    os.utime(tree["main"], (T0.timestamp() - 86400, T0.timestamp() - 86400))
    ev = _ev(tree)
    assert ev.coverage.files_skipped_mtime == 1
    assert all(u.session_id == "sess1" for u in ev.usage)


@pytest.mark.skipif(sys.platform == "win32" or os.geteuid() == 0, reason="needs POSIX non root")
def test_unreadable_file_reported(tree: dict[str, Path]) -> None:
    p = tree["sub"] / "agent-a2.jsonl"
    p.chmod(0)
    try:
        ev = _ev(tree)
    finally:
        p.chmod(0o644)
    assert ev.coverage.files_unreadable == 1
    assert any("could not be read" in w for w in ev.coverage.warnings())


def test_no_secret_prompt_or_path_in_events(tree: dict[str, Path]) -> None:
    ev = _ev(tree, want_tools=True, want_exposure=True)
    blob = json.dumps([asdict(x) for x in ev.usage + ev.tools + ev.exposures], default=str)
    assert SECRET not in blob and PROMPT not in blob and str(tree["skills"]) not in blob
    names = {t.tool for t in ev.tools}
    assert {"Skill", "mcp__srv1__a", "Bash", "Read", "slash_command"} <= names
    read = next(t for t in ev.tools if t.tool == "Read")
    assert read.skill == "epsilon" and read.via == "skill_file_read"


def test_skill_read_outside_skills_root_not_attributed(tmp_path: Path) -> None:
    root = tmp_path / "r"
    write_jsonl(root / "p" / "s.jsonl", [asst("q1", "2026-10-26T09:00:00Z", usage(1), content=[
        {"type": "tool_use", "name": "Read",
         "input": {"file_path": str(tmp_path / "skills" / ".." / "elsewhere" / "x.md")}}])])
    (tmp_path / "skills").mkdir()
    ev = ingest([root], want_tools=True, skills_roots=[tmp_path / "skills"])
    assert ev.tools[0].skill is None


def test_listing_parser_handles_alias_parentheses() -> None:
    names = skill_names_from_listing("- a-b: x\n- evidence-arc (evidencearc): y\nnot a line")
    assert set(names) == {"a-b", "evidence-arc"}


def test_timestamp_without_offset_read_as_utc(tmp_path: Path) -> None:
    root = tmp_path / "r"
    write_jsonl(root / "p" / "s.jsonl", [asst("q1", "2026-10-26T09:00:00", usage(1))])
    ev = ingest([root])
    assert ev.usage[0].ts.tzinfo is not None and ev.coverage.timestamp_assumed_utc == 1


def test_empty_root_is_missing_data_not_zero(tmp_path: Path) -> None:
    ev = ingest([tmp_path])
    assert ev.coverage.requests == 0
    assert any("missing data" in w for w in ev.coverage.warnings())


def test_reconciles_with_measure_subagents(tree: dict[str, Path]) -> None:
    """Typed runs: the engine's first request equals the legacy script's number."""
    import measure_subagents

    legacy = measure_subagents.collect(str(tree["root"]), days=10**6)
    ev = _ev(tree)
    new: dict[str, list[int]] = {}
    for u in ev.first_requests().values():
        new.setdefault(u.agent_type, []).append(u.context)
    assert sorted(legacy["lean-worker"]) != []
    # the legacy script reads the first line in file order, the engine the earliest time:
    # agent-a1 lines are out of order, so they differ by design (9002 vs 7002)
    assert legacy["lean-worker"] == [9002] and new["lean-worker"] == [7002]
    assert legacy["<img src=x onerror=alert(1)>"] == new["<img src=x onerror=alert(1)>"]
    # legacy maps a meta without agentType to general-purpose; the engine says unknown
    assert legacy["general-purpose"] == [60005] and new["unknown"] == [50004, 60005]
