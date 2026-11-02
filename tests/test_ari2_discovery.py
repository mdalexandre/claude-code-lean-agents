"""ARI 2.0 bounded abductive discovery: ARI2-R16 and R17."""
from __future__ import annotations

import json
from pathlib import Path

import ari_discover
import pytest
from ari.discovery import DiscoveryError, Ledger
from ari_fixtures import asst, usage, write_jsonl


def _ev(tmp_path: Path, text: str = "observed") -> Path:
    p = tmp_path / f"e{abs(hash(text)) % 10_000}.txt"
    p.write_text(text)
    return p


def _two(led: Ledger) -> tuple[str, str, str]:
    q = led.ask("the nightly drew no lesson")
    a = led.propose(q.id, "a global deferral rule blocks lessons", "code read", ["p"], "f")
    b = led.propose(q.id, "no day had a detectable change", "detector", ["p"], "f")
    return q.id, a.id, b.id


def test_unsupported_causal_explanation_stays_a_hypothesis(tmp_path: Path) -> None:
    led = Ledger()
    _, a, b = _two(led)
    led.record(a, "supports", _ev(tmp_path), "one supporting result")
    h = led.hypotheses[a]
    assert h.status == "HYPOTHESIS"  # its rival is still open
    assert h.uncertainties == [f"competitor {b} is not ruled out"]
    led.record(b, "inconclusive", _ev(tmp_path, "x"), "no decision")
    assert led.hypotheses[a].status == "HYPOTHESIS"


def test_supported_only_when_every_rival_is_decided(tmp_path: Path) -> None:
    led = Ledger()
    q, a, b = _two(led)
    led.record(a, "supports", _ev(tmp_path), "s")
    led.record(b, "refutes", _ev(tmp_path, "r"), "r")
    assert led.hypotheses[a].status == "SUPPORTED"
    assert led.hypotheses[b].status == "CONTRADICTED"
    assert led.questions[q].status == "CLOSED" and led.validate() == []
    assert len(led.hypotheses[a].results[0]["sha256"]) == 64


def test_forced_supported_status_is_flagged(tmp_path: Path) -> None:
    led = Ledger()
    _, a, b = _two(led)
    led.hypotheses[a].status = "SUPPORTED"  # set by hand, no evidence, rival open
    errs = led.validate()
    assert any("without bound supporting evidence" in e for e in errs)
    assert any(f"competitor {b} is open" in e for e in errs)


def test_contradictory_evidence_gives_unresolved(tmp_path: Path) -> None:
    led = Ledger()
    _, a, _ = _two(led)
    led.record(a, "supports", _ev(tmp_path), "s")
    led.record(a, "refutes", _ev(tmp_path, "r"), "r")
    h = led.hypotheses[a]
    assert h.status == "UNRESOLVED" and "conflicting" in h.unresolved_reason


def test_unresolved_needs_a_reason(tmp_path: Path) -> None:
    led = Ledger()
    _, a, _ = _two(led)
    with pytest.raises(DiscoveryError):
        led.unresolved(a, "  ")
    led.hypotheses[a].status = "UNRESOLVED"
    assert any("without a reason" in e for e in led.validate())


def test_budget_exhaustion_goes_to_the_backlog() -> None:
    led = Ledger(max_depth=1, max_nodes=3)
    q0 = led.ask("root observation")
    q1 = led.ask("child observation", q0.id)
    q2 = led.ask("grandchild observation", q1.id)
    assert q2.status == "BUDGET_EXHAUSTED"
    assert led.backlog[0]["question"] == q2.id
    with pytest.raises(DiscoveryError, match="beyond the budget"):
        led.propose(q2.id, "m", "p", ["x"], "f")
    q3 = led.ask("a sibling past the node cap", q0.id)
    assert q3.status == "BUDGET_EXHAUSTED" and len(led.backlog) == 2
    assert led.validate() == []


def test_cycle_is_refused() -> None:
    led = Ledger()
    q0 = led.ask("Files were not read")
    q1 = led.ask("the journals are missing agents", q0.id)
    with pytest.raises(DiscoveryError, match="cycle"):
        led.ask("files were NOT read!", q1.id)


def test_novelty_and_required_fields() -> None:
    led = Ledger()
    q = led.ask("o")
    led.propose(q.id, "Cache reads dominate", "p", ["x"], "f")
    other = led.ask("another observation")
    with pytest.raises(DiscoveryError, match="not novel"):
        led.propose(other.id, "cache reads, dominate.", "p", ["x"], "f")
    for bad in (("", "p", ["x"], "f"), ("m2", "", ["x"], "f"), ("m3", "p", [], "f"),
                ("m4", "p", ["x"], " ")):
        with pytest.raises(DiscoveryError, match="required"):
            led.propose(q.id, *bad)


def test_per_node_cap() -> None:
    led = Ledger(max_per_node=2)
    q = led.ask("o")
    led.propose(q.id, "m1", "p", ["x"], "f")
    led.propose(q.id, "m2", "p", ["x"], "f")
    with pytest.raises(DiscoveryError, match="already has 2"):
        led.propose(q.id, "m3", "p", ["x"], "f")


def test_record_refuses_missing_evidence_and_bad_outcome(tmp_path: Path) -> None:
    led = Ledger()
    _, a, _ = _two(led)
    with pytest.raises(DiscoveryError, match="does not exist"):
        led.record(a, "supports", tmp_path / "nope.txt", "n")
    with pytest.raises(DiscoveryError, match="outcome"):
        led.record(a, "proves", _ev(tmp_path), "n")


def test_discover_cli_on_a_fixture(tmp_path: Path) -> None:
    root = tmp_path / "root"
    write_jsonl(root / "p" / "s.jsonl", [asst("r", "2026-10-30T10:00:00Z", usage(1)),
                                         asst("r", "2026-10-30T10:00:00Z", usage(1, 4))])
    wf = root / "p" / "s" / "subagents" / "workflows" / "wf_1"
    write_jsonl(wf / "journal.jsonl", [{"type": "started", "agentId": "zz"}])
    burn = tmp_path / "burn.json"
    burn.write_text(json.dumps({"by_agent_type": {"main": {"cache_read": 9, "output": 1,
                                                           "metric": 10}},
                                "total_uncertainty": {"lower": 5, "upper": 10}}))
    out = tmp_path / "out"
    rc = ari_discover.main(["--root", str(root), "--now", "2026-11-01T12:00:00Z", "--out",
                            str(out), "--burn", str(burn)])
    led = json.loads((out / "ledger.json").read_text())
    assert rc == 0 and led["schema"] == "ari-discovery/1" and led["violations"] == []
    by = {h["mechanism"]: h for h in led["hypotheses"]}
    assert by["workflow journals point at agent runs whose transcripts are missing"][
        "status"] == "SUPPORTED"
    # repair 1: compatible facts no longer block each other
    assert by["the unread files are not transcripts (indexes or project data)"][
        "status"] == "SUPPORTED"
    assert by["cache reads make up most of the counted tokens"]["status"] == "SUPPORTED"
    assert by["ambiguous duplicate copies inflate the total materially"]["status"] == \
        "SUPPORTED"
    assert by["a configuration change caused the increase"]["status"] == "UNRESOLVED"
    # repair 2: escaped hostile text is not a live tag
    assert by["transcript derived text reaches the HTML unescaped"]["status"] == \
        "CONTRADICTED"
    assert by["the share screen passes identifying or structured injection"]["status"] == \
        "CONTRADICTED"
    for h in led["hypotheses"]:  # every result is bound to an evidence file in --out
        for r in h["results"]:
            assert (out / r["evidence"]).is_file()
    assert sorted(p.name for p in tmp_path.iterdir()) == ["burn.json", "out", "root"]


def test_discover_cli_refuses_bad_input(tmp_path: Path) -> None:
    (tmp_path / "root").mkdir()
    bad = tmp_path / "a.json"
    bad.write_text("{not json")
    assert ari_discover.main(["--root", str(tmp_path / "root"), "--out", str(tmp_path / "o"),
                              "--audit", str(bad)]) == 2


def test_per_node_cap_records_the_dropped_rival_in_the_backlog() -> None:
    """D4: a rival refused by the per question cap is kept in the backlog, not lost."""
    led = Ledger(max_per_node=1)
    q = led.ask("o")
    led.propose(q.id, "m1", "p", ["x"], "f")
    with pytest.raises(DiscoveryError, match="already has 1"):
        led.propose(q.id, "m2 the rival", "p", ["x"], "f")
    assert led.backlog == [{"question": q.id, "observation": "o",
                            "mechanism": "m2 the rival",
                            "reason": "per question hypothesis cap reached"}]


def test_a_new_rival_reopens_a_settled_question(tmp_path: Path) -> None:
    """D4: SUPPORTED holds only while every rival is decided, including later ones."""
    led = Ledger()
    q, a, b = _two(led)
    led.record(a, "supports", _ev(tmp_path), "s")
    led.record(b, "refutes", _ev(tmp_path, "r"), "r")
    assert led.hypotheses[a].status == "SUPPORTED" and led.questions[q].status == "CLOSED"
    c = led.propose(q, "a third mechanism nobody tested", "review", ["p"], "f").id
    assert led.hypotheses[a].status == "HYPOTHESIS"
    assert led.hypotheses[a].uncertainties == [f"competitor {c} is not ruled out"]
    assert led.questions[q].status == "OPEN" and led.validate() == []
    led.record(c, "refutes", _ev(tmp_path, "r2"), "r2")
    assert led.hypotheses[a].status == "SUPPORTED" and led.questions[q].status == "CLOSED"
