"""ARI 2.0 defect D1: one hostile record or file never stops ingestion (ARI2-R1)."""
from __future__ import annotations

import json
from pathlib import Path

import pytest
from ari import audit
from ari.evidence import ingest
from ari_fixtures import asst, usage, write_jsonl

GOOD = asst("good", "2026-10-26T10:00:00Z", usage(5))
DEEP = '{"usage": ' + "[" * 200_000 + "]" * 200_000 + "}"
HUGE = '{"type": "assistant", "message": {"usage": {"input_tokens": 1' + "0" * 5000 + "}}}"


def _main(root: Path, extra: list[object]) -> None:
    write_jsonl(root / "p" / "s.jsonl", [GOOD, *extra])


@pytest.mark.parametrize("line", [DEEP, HUGE], ids=["deep-nesting", "huge-int"])
def test_hostile_transcript_line_is_counted_not_fatal(tmp_path: Path, line: str) -> None:
    _main(tmp_path, [line])
    cov = ingest([tmp_path]).coverage
    assert cov.requests == 1 and cov.malformed_lines == 1


@pytest.mark.parametrize("ts", ["9999-12-31T23:59:59-05:00", "0001-01-01T00:00:00+05:00"])
def test_extreme_timestamp_is_skipped_not_fatal(tmp_path: Path, ts: str) -> None:
    _main(tmp_path, [asst("bad", ts, usage(7))])
    ev = ingest([tmp_path])
    assert ev.coverage.requests == 1 and [u.request_id for u in ev.usage] == ["good"]


@pytest.mark.parametrize("aid", ["x" * 300, "a\u0000b", "../../etc/passwd", "a/b"],
                         ids=["too-long", "null-byte", "traversal", "slash"])
def test_hostile_journal_agent_id_is_malformed_not_fatal(tmp_path: Path, aid: str) -> None:
    _main(tmp_path, [])
    wf = tmp_path / "p" / "s" / "subagents" / "workflows" / "wf_1"
    write_jsonl(wf / "journal.jsonl", [{"type": "started", "agentId": aid}])
    cov = ingest([tmp_path]).coverage
    assert cov.requests == 1 and cov.files_workflow_journal == 1
    assert cov.journal_agents_missing == 0 and cov.malformed_lines == 1


def test_deeply_nested_journal_line_is_not_fatal(tmp_path: Path) -> None:
    _main(tmp_path, [])
    wf = tmp_path / "p" / "s" / "subagents" / "workflows" / "wf_1"
    write_jsonl(wf / "journal.jsonl", ['{"type": "started", "x": ' + "[" * 200_000 + "]"
                                       * 200_000 + "}"])
    cov = ingest([tmp_path]).coverage
    assert cov.requests == 1 and cov.files_workflow_journal == 1


def test_deeply_nested_meta_json_is_unreadable_not_fatal(tmp_path: Path) -> None:
    sub = tmp_path / "p" / "s" / "subagents"
    write_jsonl(sub / "agent-a1.jsonl", [asst("r", "2026-10-26T10:00:00Z", usage(3))])
    (sub / "agent-a1.meta.json").write_text('{"agentType": ' + "[" * 200_000 + "]" * 200_000
                                            + "}")
    ev = ingest([tmp_path])
    assert ev.coverage.requests == 1 and ev.usage[0].agent_type == "unknown"


def test_deeply_nested_mcp_config_is_skipped_not_fatal(tmp_path: Path) -> None:
    good = tmp_path / "good.json"
    good.write_text(json.dumps({"mcpServers": {"srv": {}}}))
    bad = tmp_path / "bad.json"
    bad.write_text('{"mcpServers": ' + "[" * 200_000 + "]" * 200_000 + "}")
    assert set(audit.mcp_inventory([bad, good])) == {"srv"}
