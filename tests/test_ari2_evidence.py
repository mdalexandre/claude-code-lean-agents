"""ARI 2.0 evidence integrity (H1) and resource intelligence (H2): ARI2-R1 to R7."""
from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

import pytest
import weekly_burn
from ari import evidence, forecast
from ari.evidence import duplicate_class, ingest
from ari_fixtures import asst, usage, write_jsonl

NOW = datetime(2026, 11, 1, 12, tzinfo=timezone.utc)


def test_every_configured_root_is_enumerated(tmp_path: Path) -> None:
    for r in ("a", "b"):
        write_jsonl(tmp_path / r / "p" / "s.jsonl",
                    [asst(f"r{r}", "2026-10-26T10:00:00Z", usage(1))])
    (tmp_path / "missing").mkdir()
    ev = ingest([tmp_path / "a", tmp_path / "b", tmp_path / "missing"])
    assert ev.coverage.roots == 3 and ev.coverage.requests == 2
    assert sorted(r["relpath"] for r in ev.manifest) == ["p/s.jsonl", "p/s.jsonl"]


def test_unread_files_are_classified_not_all_gaps(tmp_path: Path) -> None:
    root = tmp_path / "root"
    write_jsonl(root / "p" / "s.jsonl", [asst("r", "2026-10-26T10:00:00Z", usage(1))])
    write_jsonl(root / "p" / "reports" / "data.jsonl", [{"filename": "x", "verdict": "ok"}])
    wf = root / "p" / "s" / "subagents" / "workflows" / "wf_1"
    write_jsonl(wf / "journal.jsonl", [{"type": "started", "agentId": "aa"},
                                       {"type": "started", "agentId": "bb"}])
    write_jsonl(wf / "agent-aa.jsonl", [asst("w", "2026-10-26T10:00:00Z", usage(1))])
    write_jsonl(root / "p" / "odd" / "x.jsonl", [asst("h", "2026-10-26T10:00:00Z", usage(1))])
    cov = ingest([root]).coverage
    assert (cov.files_foreign, cov.files_workflow_journal, cov.files_unrecognized) == (1, 1, 1)
    assert cov.journal_agents_missing == 1  # bb started, no transcript: missing coverage
    assert cov.unread_gaps() == 2
    assert any("no transcript" in w for w in cov.warnings())


def test_malformed_lines_keep_unrelated_evidence(tmp_path: Path) -> None:
    write_jsonl(tmp_path / "p" / "s.jsonl", [
        asst("a", "2026-10-26T10:00:00Z", usage(5)), '{"usage": broken',
        "[1, 2]", asst("b", "2026-10-26T10:01:00Z", usage(7))])
    ev = ingest([tmp_path])
    assert ev.coverage.requests == 2 and ev.coverage.malformed_lines == 2


def test_unverified_format_is_counted_and_still_read(tmp_path: Path) -> None:
    write_jsonl(tmp_path / "p" / "s.jsonl", [
        asst("a", "2026-10-26T10:00:00Z", usage(5)),
        asst("b", "2026-10-26T10:01:00Z", usage(7), version="3.0.1"),
        {"type": "brand-new-kind", "timestamp": "2026-10-26T10:02:00Z",
         "message": {"usage": {}}}])
    ev = ingest([tmp_path])
    cov = ev.coverage
    assert ev.coverage.requests == 2
    assert cov.format_versions == {"2.1": 1, "3.0": 1} and cov.unverified_format_records == 1
    assert cov.unknown_record_types == 1
    assert any("not verified" in w for w in cov.warnings())


def test_repeated_requests_are_not_duplicates(tmp_path: Path) -> None:
    same = usage(100, 0, 500, 10)
    write_jsonl(tmp_path / "p" / "s.jsonl", [
        asst("r1", "2026-10-26T10:00:00Z", same), asst("r2", "2026-10-26T10:00:00Z", same),
        asst("r1", "2026-10-26T10:00:00Z", same)])
    ev = ingest([tmp_path])
    assert ev.coverage.requests == 2
    assert ev.coverage.duplicate_classes == {"IDENTICAL": 1}


def test_duplicate_classes() -> None:
    a = (10, 0, 0, 500, 5)
    assert duplicate_class(a, a) == "IDENTICAL"
    assert duplicate_class(a, (10, 0, 0, 500, 90)) == "STREAM_GROWTH"
    assert duplicate_class(a, (0, 0, 0, 0, 0)) == "ZEROED_COPY"
    assert duplicate_class(a, (10, 500, 0, 0, 5)) == "AMBIGUOUS"


def test_ambiguous_copies_give_a_band_not_a_verified_total(tmp_path: Path) -> None:
    write_jsonl(tmp_path / "p" / "s.jsonl", [
        asst("r", "2026-10-26T10:00:00Z", usage(10, 500, 0, 5)),   # cache write
        asst("r", "2026-10-26T10:00:00Z", usage(10, 0, 500, 5)),   # same tokens as a read
        asst("q", "2026-10-26T10:05:00Z", usage(10, 0, 0, 5)),
        asst("q", "2026-10-26T10:05:00Z", usage(10, 0, 0, 50))])   # stream growth
    ev = ingest([tmp_path])
    unc = ev.total_uncertainty()
    # merged r is 10 + 500 + 500 + 5 = 1015 (upper); its largest copy is 515 (lower)
    assert unc == {"lower": 515 + 60, "upper": 1015 + 60, "ambiguous_requests": 1}
    assert ev.coverage.duplicate_classes == {"AMBIGUOUS": 1, "STREAM_GROWTH": 1}
    r = forecast.burn(ev, NOW)
    assert r["total_uncertainty"]["ambiguous_requests"] == 1
    assert r["duplicate_classes"] == {"AMBIGUOUS": 1, "STREAM_GROWTH": 1}
    assert any("quota" in a for a in r["assumptions"])
    assert "between 575 and 1,075" in forecast.render_text(r)


def test_burn_cli_prints_band_and_quota_line(tmp_path: Path,
                                            capsys: pytest.CaptureFixture[str]) -> None:
    write_jsonl(tmp_path / "p" / "s.jsonl", [
        asst("r", "2026-10-31T10:00:00Z", usage(10, 500, 0, 5)),
        asst("r", "2026-10-31T10:00:00Z", usage(10, 0, 500, 5))])
    assert weekly_burn.main(["--root", str(tmp_path), "--now", NOW.isoformat(), "--json"]) == 0
    out = json.loads(capsys.readouterr().out)
    assert out["provider_quota"].startswith("Provider quota ETA: unavailable")
    assert out["total_uncertainty"]["upper"] > out["total_uncertainty"]["lower"]


def test_checkpoint_classes(tmp_path: Path) -> None:
    f = tmp_path / "s.jsonl"
    f.write_bytes(b'{"a": 1}\n')
    row = {"size": f.stat().st_size, "offset": 9, **evidence.checkpoint(f, 9)}
    assert evidence.compare_checkpoint(row, f) == "UNCHANGED"
    f.write_bytes(b'{"a": 1}\n{"b": 2}\n')
    assert evidence.compare_checkpoint(row, f) == "GROWN"
    f.write_bytes(b'{"x": 1}\n{"b": 2}\n')
    assert evidence.compare_checkpoint(row, f) == "REPLACED"
    f.write_bytes(b"{}\n")
    assert evidence.compare_checkpoint(row, f) == "TRUNCATED"
    f.unlink()
    assert evidence.compare_checkpoint(row, f) == "MISSING"


def test_replay_is_deterministic(tmp_path: Path) -> None:
    write_jsonl(tmp_path / "p" / "s.jsonl", [asst("r", "2026-10-30T10:00:00Z", usage(1)),
                                             asst("r", "2026-10-30T10:00:00Z", usage(1, 4))])
    a = forecast.burn(ingest([tmp_path]), NOW)
    b = forecast.burn(ingest([tmp_path]), NOW)
    assert json.dumps(a, sort_keys=True) == json.dumps(b, sort_keys=True)


def test_hostile_journal_lines_do_not_crash_ingest(tmp_path: Path) -> None:
    wf = tmp_path / "p" / "s" / "subagents" / "workflows" / "wf_1"
    write_jsonl(wf / "journal.jsonl", ['["started"]', '"started"', '{"type": "started"}',
                                       '{"type": "started", "agentId": ["x"]}',
                                       {"type": "started", "agentId": "ok"}])
    cov = ingest([tmp_path]).coverage
    assert cov.files_workflow_journal == 1 and cov.journal_agents_missing == 1


def test_unread_symlink_leaving_the_root_is_not_opened(tmp_path: Path) -> None:
    outside = tmp_path / "secret.jsonl"
    write_jsonl(outside, [asst("x", "2026-10-26T10:00:00Z", usage(1))])  # has markers
    root = tmp_path / "root"
    write_jsonl(root / "p" / "s.jsonl", [asst("r", "2026-10-26T10:00:00Z", usage(1))])
    (root / "p" / "odd").mkdir()
    (root / "p" / "odd" / "x.jsonl").symlink_to(outside)
    cov = ingest([root]).coverage
    assert cov.files_outside_root == 1
    assert cov.files_unrecognized == 0 and cov.files_foreign == 0


def test_routine_2_1_record_types_are_not_reported_unknown(tmp_path: Path) -> None:
    routine = ["api-request", "api-request-blob", "api-request-shape", "atis-latch",
               "last-prompt", "mode", "permission-mode", "ai-title", "file-history-delta",
               "frame-link", "tools-used", "pr-link", "cost-state"]
    write_jsonl(tmp_path / "p" / "s.jsonl", [asst("a", "2026-10-26T10:00:00Z", usage(5))]
                + [{"type": t, "timestamp": "2026-10-26T10:00:00Z", "message": {"usage": {}}}
                   for t in routine]  # a marker, so the fast line filter parses each line
                + [{"type": "brand-new-kind", "timestamp": "2026-10-26T10:00:00Z",
                    "message": {"usage": {}}}])
    assert ingest([tmp_path]).coverage.unknown_record_types == 1


def test_band_line_names_its_metric_when_another_is_shown(tmp_path: Path) -> None:
    """D7: the band bounds all token types, so under --metric output it says so."""
    write_jsonl(tmp_path / "p" / "s.jsonl", [
        asst("r", "2026-10-26T10:00:00Z", usage(10, 500, 0, 5)),
        asst("r", "2026-10-26T10:00:00Z", usage(10, 0, 500, 5))])
    ev = ingest([tmp_path])
    out = forecast.render_text(forecast.burn(ev, NOW, metric="output"))
    assert "Total tokens of all types (not metric=output) between 515 and 1,015" in out
    plain = forecast.render_text(forecast.burn(ev, NOW))
    assert "Total tokens between 515 and 1,015" in plain
