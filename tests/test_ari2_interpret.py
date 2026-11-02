"""ARI 2.0 hermeneutic layer (H4) and its gate on capability advice (H3): ARI2-R7 to R10."""
from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

import ari_report
import pytest
from ari import audit, interpret, report
from ari.evidence import Coverage, EvidenceSet, UsageEvent, ingest
from ari_fixtures import asst, usage, write_jsonl

NOW = datetime(2026, 11, 1, 12, tzinfo=timezone.utc)
EVIL = "<script>alert(1)</script><img src=x onerror=alert(2)>\"'&"


def _ev(gaps: int = 0) -> EvidenceSet:
    return EvidenceSet([], [], [], [], Coverage(requests=1, files_unrecognized=gaps),
                       (None, None))


def _cap(exposed: int = 30) -> dict[str, object]:
    return {"name": "zeta", "category": "skill", "exposed_sessions": exposed,
            "indirect_uses": 0}


def test_no_use_finding_keeps_five_readings() -> None:
    f = interpret.no_use_finding(_cap(), "CONSIDER_REMOVAL", "r", _ev(), 20, False)
    assert [r.id for r in f.readings] == ["unused", "coverage_gap", "indirect",
                                          "not_exposed", "substituted"]
    assert interpret.validate_finding(f) == []
    assert f.coverage == "COMPLETE" and f.provenance["parser"]


@pytest.mark.parametrize("gaps,exposed,accepted,ok", [
    (3, 30, False, False),   # gaps could hold the use
    (3, 30, True, True),     # the owner accepted the gaps explicitly
    (0, 0, False, False),    # never exposed: not available, not unused
    (0, 30, False, True),
])
def test_removal_needs_coverage_and_exposure(gaps: int, exposed: int, accepted: bool,
                                             ok: bool) -> None:
    f = interpret.no_use_finding(_cap(exposed), "CONSIDER_REMOVAL", "r", _ev(gaps), 20,
                                 accepted)
    assert (interpret.validate_finding(f) == []) is ok


def test_unsupported_reading_states_are_refused() -> None:
    f = interpret.no_use_finding(_cap(), "INVESTIGATE", "r", _ev(), 20, False)
    f.readings[0].supports = []
    f.readings.append(interpret.Reading("x", "x", "WEAKENED"))
    f.provenance = {}
    errs = interpret.validate_finding(f)
    assert any("SUPPORTED without" in e for e in errs)
    assert any("WEAKENED without" in e for e in errs)
    assert "provenance incomplete" in errs


def test_contradictory_reading_blocks_removal() -> None:
    f = interpret.no_use_finding(_cap(), "CONSIDER_REMOVAL", "r", _ev(), 20, False)
    f.readings[0].status, f.readings[0].weakens = "WEAKENED", ["a use was found"]
    assert any("against evidence of use" in e for e in interpret.validate_finding(f))


def _tree(tmp_path: Path, extra_gap: bool) -> tuple[Path, Path, Path]:
    root, skills = tmp_path / "root", tmp_path / "skills"
    for n in ("zeta",):
        (skills / n).mkdir(parents=True)
        (skills / n / "SKILL.md").write_text(f"---\nname: {n}\n---\n")
    recs = []
    for i in range(25):
        recs.append({"type": "attachment", "timestamp": f"2026-10-{10 + i % 15:02d}T09:00:00Z",
                     "attachment": {"type": "skill_listing", "content": "- zeta: z"}})
    for i in range(25):
        write_jsonl(root / "p" / f"s{i}.jsonl", [recs[i], asst(
            f"r{i}", f"2026-10-{10 + i % 15:02d}T09:01:00Z", usage(1))])
    if extra_gap:
        write_jsonl(root / "p" / "odd" / "x.jsonl", [asst("h", "2026-10-20T10:00:00Z",
                                                          usage(1))])
    cfg = tmp_path / "cfg.json"
    cfg.write_text("{}")
    return root, skills, cfg


@pytest.mark.parametrize("gap,expected", [(False, "CONSIDER_REMOVAL"), (True, "INVESTIGATE")])
def test_audit_attaches_findings_and_gates_removal(tmp_path: Path, gap: bool,
                                                   expected: str) -> None:
    root, skills, cfg = _tree(tmp_path, gap)
    ev = ingest([root], want_tools=True, want_exposure=True, skills_roots=[skills])
    out = audit.audit(ev, [str(skills)], [str(cfg)], 22)
    rec = next(r for r in out["recommendations"] if r["target"] == "zeta")
    f = next(f for f in out["findings"] if f["subject"] == "skill:zeta")
    assert rec["action"] == expected == f["decision"]
    assert out["findings"][0]["id"] == "F-coverage"
    assert out["coverage"]["unread_gaps"] == (1 if gap else 0)


def test_finding_contract_is_a_second_gate(tmp_path: Path,
                                           monkeypatch: pytest.MonkeyPatch) -> None:
    root, skills, cfg = _tree(tmp_path, False)
    ev = ingest([root], want_tools=True, want_exposure=True, skills_roots=[skills])
    monkeypatch.setattr(audit, "validate_recommendation", lambda *a, **k: None)
    ev.coverage.files_unrecognized = 2  # the first gate is gone; the finding still sees gaps
    out = audit.audit(ev, [str(skills)], [str(cfg)], 22)
    rec = next(r for r in out["recommendations"] if r["target"] == "zeta")
    assert rec["action"] == "INVESTIGATE" and "finding contract" in rec["rejected"]


def test_html_escapes_every_finding_field() -> None:
    f = interpret.no_use_finding({"name": EVIL, "category": EVIL, "exposed_sessions": 30,
                                  "indirect_uses": 0}, EVIL, EVIL, _ev(), 20, False).to_dict()
    f["would_change"] = [EVIL]
    ev = EvidenceSet([], [UsageEvent("r", 0, 1, "s", "a", EVIL, "subagent", "m", "2.1.1", NOW,
                                     1, 1, 0, 0, 1)], [], [], Coverage(requests=1),
                     (None, None))
    page = report.render_html(ev, None, None, [], [], NOW, [f])
    # a handler attribute is only live inside a real tag; both tags arrive escaped
    assert "<script" not in page and "<img" not in page
    assert "&lt;script&gt;" in page and "&lt;img" in page
    assert "http" not in page.split("</style>")[1]  # no external resource


def test_report_cli_writes_findings_section(tmp_path: Path) -> None:
    root, skills, cfg = _tree(tmp_path, True)
    out = tmp_path / "r.html"
    rc = ari_report.main(["--root", str(root), "--now", NOW.isoformat(), "--out", str(out),
                          "--skills-root", str(skills), "--mcp-config", str(cfg)])
    page = out.read_text()
    assert rc == 0 and "What the evidence supports" in page and "coverage PARTIAL" in page
    assert page.count("<section class=\"finding\">") >= 3


def test_audit_json_survives_hostile_names(tmp_path: Path) -> None:
    root, skills, cfg = _tree(tmp_path, False)
    (skills / "x\"y").mkdir()
    (skills / "x\"y" / "SKILL.md").write_text("---\nname: x\"y\n---\n")
    ev = ingest([root], want_tools=True, want_exposure=True, skills_roots=[skills])
    text = json.dumps(audit.audit(ev, [str(skills)], [str(cfg)], 22))
    assert json.loads(text)["schema"] == "ari-capability-audit/1"


@pytest.mark.parametrize("bad", [
    {"agent_types": [{"agent_type": "/home/u/x", "runs_bucket": "5-9"}]},
    {"claude_code_minor_versions": ["2.1\"},{\"x"]},
    {"suppressed_groups": "someone@example.com"},
    {"note2": "extra key"},
])
def test_share_screen_refuses_hostile_content(bad: dict[str, object]) -> None:
    base = report.build_share(_ev())
    assert report.screen_share({**base, **bad})
    assert "anonymous" not in base["note"].replace("not proven anonymous", "")


ROW = {"agent_type": "Explore", "runs_bucket": "5-9", "median_start_k": 12, "p90_start_k": 30}


def test_share_screen_passes_a_well_formed_share() -> None:
    base = report.build_share(_ev())
    assert report.screen_share(base) == []
    assert report.screen_share({**base, "agent_types": [ROW]}) == []


@pytest.mark.parametrize("bad", [
    {"note": "see /home/u/secret for more"},             # D2: note was never scanned
    {"note": "aggregated summary, call me"},             # the note is fixed text
    {"agent_types": ["x"]},                              # a row that is not an object
    {"agent_types": "Explore"},                          # not a list
    {"agent_types": [{**ROW, "median_start_k": "ten"}]},  # value types are checked
    {"agent_types": [{**ROW, "p90_start_k": True}]},
    {"agent_types": [{**ROW, "runs_bucket": "7"}]},       # only real bucket labels
    {"agent_types": [{"agent_type": "Explore"}]},         # a row missing its fields
    {"suppressed_groups": True},
    {"suppressed_groups": -1},
    {"min_group_runs": 2},                                # below the k floor
    {"claude_code_minor_versions": [2.1]},                # a number, not a string
    {"claude_code_minor_versions": "2.1"},
    {"ari_version": "2.0.0 call me"},
], ids=lambda b: next(iter(b)) + ":" + str(next(iter(b.values())))[:20])
def test_share_screen_checks_every_field_and_type(bad: dict[str, object]) -> None:
    problems = report.screen_share({**report.build_share(_ev()), **bad})
    assert problems, bad


@pytest.mark.parametrize("obj", [[], "share", None, 5])
def test_share_screen_refuses_a_non_object(obj: object) -> None:
    assert report.screen_share(obj) == ["share is not a JSON object"]  # type: ignore[arg-type]


@pytest.mark.parametrize("gaps,accepted,ok", [(0, False, True), (2, False, False),
                                              (2, True, True)])
def test_consolidate_finding_gates_on_coverage(gaps: int, accepted: bool, ok: bool) -> None:
    f = interpret.consolidate_finding(["a", "b"], 7, "CONSOLIDATE", "r", _ev(gaps), accepted)
    assert (interpret.validate_finding(f) == []) is ok
    assert f.subject == "mcp_server:a,b" and len(f.readings) >= 2


def test_every_audit_action_is_a_known_decision() -> None:
    assert set(audit.ACTIONS) <= set(interpret.DECISIONS)


def test_decision_outside_the_vocabulary_is_refused() -> None:
    """D7: the decision vocabulary is closed."""
    f = interpret.no_use_finding(_cap(), "DELETE_IT_NOW", "r", _ev(), 20, False)
    assert any("decision outside the vocabulary" in e for e in interpret.validate_finding(f))


@pytest.mark.parametrize("decision", ["CONSIDER_REMOVAL", "RESTRICT_EXPOSURE"])
def test_removal_needs_an_exposure_reading(decision: str) -> None:
    """D7: a removal class decision on a capability needs the not_exposed reading."""
    f = interpret.no_use_finding(_cap(), decision, "r", _ev(), 20, False)
    assert interpret.validate_finding(f) == []
    f.readings = [r for r in f.readings if r.id != "not_exposed"]
    assert any("not_exposed reading" in e for e in interpret.validate_finding(f))


def test_coverage_reading_must_agree_with_provenance() -> None:
    """D7: a coverage_gap reading or coverage state that denies counted gaps is refused."""
    f = interpret.no_use_finding(_cap(), "CONSIDER_REMOVAL", "r", _ev(), 20, False)
    f.provenance["unread_gaps"] = 3  # gaps counted, yet the reading says there are none
    errs = interpret.validate_finding(f)
    assert any("coverage_gap reading contradicts" in e for e in errs)
    assert any("coverage COMPLETE with 3 unread gaps" in e for e in errs)
