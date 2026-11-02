"""Hermeneutic layer: what was observed, what it may mean, and what that justifies.

Every material conclusion is a Finding (schema ari-finding/1, ARI2-R8): the observation,
at least two readings with the evidence that supports or weakens each, the decision the
evidence justifies, what would change it, its provenance and its coverage state. A reading
status is an evidence label, not a probability:

    SUPPORTED     evidence in this window points to it
    WEAKENED      evidence in this window points away from it
    UNRESOLVED    evidence is missing or conflicting
    NOT_TESTABLE  transcripts cannot observe it; it is shown, and it blocks nothing

validate_finding refuses a removal class decision unless coverage gaps are ruled out (or
explicitly accepted by the owner's flag) and the capability was seen exposed (ARI2-R9).
Rule based and deterministic: no model writes a reading.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any

from .evidence import PARSER_VERSION, EvidenceSet

FINDING_SCHEMA = "ari-finding/1"
READING_STATES = ("SUPPORTED", "WEAKENED", "UNRESOLVED", "NOT_TESTABLE")
COVERAGE_STATES = ("COMPLETE", "PARTIAL")
REMOVAL_CLASS = ("CONSIDER_REMOVAL", "RESTRICT_EXPOSURE", "CONSOLIDATE")
# audit actions (audit.ACTIONS, a test keeps them in step) plus the report decisions
DECISIONS = ("KEEP", "INVESTIGATE", "RESTRICT_EXPOSURE", "CONSOLIDATE",
             "COMPARE_CONFIGURATIONS", "CONSIDER_REMOVAL", "INSUFFICIENT_EVIDENCE",
             "UNKNOWN", "REPORT", "REPORT_WITH_COVERAGE", "REPORT_BAND", "REPORT_TOTAL")


@dataclass
class Reading:
    id: str
    text: str
    status: str
    supports: list[str] = field(default_factory=list)
    weakens: list[str] = field(default_factory=list)


@dataclass
class Finding:
    id: str
    subject: str
    observed: str
    readings: list[Reading]
    decision: str
    decision_reason: str
    would_change: list[str]
    provenance: dict[str, Any]
    coverage: str
    schema: str = FINDING_SCHEMA

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def provenance(ev: EvidenceSet, gaps_accepted: bool = False) -> dict[str, Any]:
    cov = ev.coverage
    return {"window": list(ev.window), "parser": PARSER_VERSION, "files_read": cov.files_read,
            "unread_gaps": cov.unread_gaps(), "gaps_accepted": gaps_accepted,
            "excluded": {k: v for k, v in cov.excluded().items() if v},
            "formats": dict(sorted(cov.format_versions.items()))}


def validate_finding(f: Finding) -> list[str]:
    """Contract violations of one finding; empty when it may be shown or acted on."""
    errs: list[str] = []
    if f.schema != FINDING_SCHEMA:
        errs.append("wrong schema")
    if not f.observed.strip():
        errs.append("nothing observed")
    if len(f.readings) < 2:
        errs.append("needs at least two readings")
    ids = [r.id for r in f.readings]
    if len(ids) != len(set(ids)):
        errs.append("duplicate reading id")
    for r in f.readings:
        if r.status not in READING_STATES:
            errs.append(f"{r.id}: unknown status {r.status}")
        if r.status == "SUPPORTED" and not r.supports:
            errs.append(f"{r.id}: SUPPORTED without supporting evidence")
        if r.status == "WEAKENED" and not r.weakens:
            errs.append(f"{r.id}: WEAKENED without weakening evidence")
    if not f.decision.strip() or not f.decision_reason.strip():
        errs.append("no decision or no reason")
    elif f.decision not in DECISIONS:
        errs.append(f"decision outside the vocabulary: {f.decision}")
    if not f.would_change:
        errs.append("nothing named that would change the conclusion")
    if not all(k in f.provenance for k in ("window", "parser", "unread_gaps")):
        errs.append("provenance incomplete")
    if f.coverage not in COVERAGE_STATES:
        errs.append(f"unknown coverage state {f.coverage}")
    by = {r.id: r for r in f.readings}
    unread = f.provenance.get("unread_gaps")
    if isinstance(unread, int) and not isinstance(unread, bool):
        if unread and f.coverage == "COMPLETE":
            errs.append(f"coverage COMPLETE with {unread} unread gaps")
        gap_r = by.get("coverage_gap")
        if unread and gap_r is not None and gap_r.status == "WEAKENED":
            errs.append(f"coverage_gap reading contradicts {unread} unread gaps")
    elif "unread_gaps" in f.provenance:
        errs.append("provenance unread_gaps is not a count")
    if f.decision in ("CONSIDER_REMOVAL", "RESTRICT_EXPOSURE") and "not_exposed" not in by:
        errs.append("removal class decision needs a not_exposed reading")
    if f.decision in REMOVAL_CLASS:
        gap = by.get("coverage_gap")
        if gap is None or gap.status == "SUPPORTED" or (
                gap.status == "UNRESOLVED" and not f.provenance.get("gaps_accepted")):
            errs.append("removal class decision while unread gaps could hold a use")
        expo = by.get("not_exposed")
        if expo is not None and expo.status != "WEAKENED":
            errs.append("removal class decision for a capability not seen exposed")
        unused = by.get("unused")
        if unused is not None and unused.status == "WEAKENED":
            errs.append("removal class decision against evidence of use")
    return errs


def no_use_finding(cap: dict[str, Any], decision: str, reason: str, ev: EvidenceSet,
                   min_sessions: int, gaps_accepted: bool) -> Finding:
    """Readings for a capability with no observed use (ARI2-R9)."""
    gaps = ev.coverage.unread_gaps()
    name, cat = cap["name"], cap["category"]
    exposed = int(cap.get("exposed_sessions", 0))
    indirect = int(cap.get("indirect_uses", 0))
    if gaps and gaps_accepted:
        gap = Reading("coverage_gap", "The observer missed the use", "UNRESOLVED",
                      supports=[f"{gaps} unread files or lines",
                                "accepted by the owner (--accept-coverage-gaps)"])
    elif gaps:
        gap = Reading("coverage_gap", "The observer missed the use", "SUPPORTED",
                      supports=[f"{gaps} unread files or lines could hold a use"])
    else:
        gap = Reading("coverage_gap", "The observer missed the use", "WEAKENED",
                      weakens=["0 unread files or lines in the window"])
    unused_ok = exposed >= min_sessions and gap.status != "SUPPORTED"
    readings = [
        Reading("unused", "Genuinely unused in this window",
                "SUPPORTED" if unused_ok else "UNRESOLVED",
                supports=[f"0 invocations across {exposed} sessions where it was exposed"],
                weakens=[] if exposed >= min_sessions else
                [f"exposed in {exposed} sessions, fewer than {min_sessions}"]),
        gap,
        Reading("indirect", "Used indirectly", "WEAKENED",
                supports=["paths other than a Read of its files (a procedure copied into "
                          "another prompt) are not observable"],
                weakens=[f"{indirect} reads of its files" if cat == "skill" else
                         "no mcp__ call from any main or subagent transcript"]),
        Reading("not_exposed", "Installed but not available to agents",
                "WEAKENED" if exposed else "SUPPORTED",
                supports=[] if exposed else ["never seen in a skill listing or MCP delta"],
                weakens=[f"exposed in {exposed} sessions"] if exposed else []),
        Reading("substituted", "Another capability did the same job", "NOT_TESTABLE"),
    ]
    change = ["any invocation or file read of it in a later window",
              "an owner statement that a recurring task needs it"]
    if gaps:
        change.append("an unread file found to hold a use of it")
    if exposed < min_sessions:
        change.append(f"exposure in at least {min_sessions} sessions")
    return Finding(
        id=f"F-nouse-{cat}-{name}", subject=f"{cat}:{name}",
        observed=f"{cat} {name}: no observed use; exposed in {exposed} sessions",
        readings=readings, decision=decision, decision_reason=reason, would_change=change,
        provenance=provenance(ev, gaps_accepted),
        coverage="PARTIAL" if gaps else "COMPLETE")


def consolidate_finding(servers: list[str], shared_tools: int, decision: str, reason: str,
                        ev: EvidenceSet, gaps_accepted: bool) -> Finding:
    """Readings for MCP servers that expose identical tool sets (ARI2-R9: removal class)."""
    gaps = ev.coverage.unread_gaps()
    if gaps and gaps_accepted:
        gap = Reading("coverage_gap", "Unread records hold use that tells the servers apart",
                      "UNRESOLVED", supports=[f"{gaps} unread files or lines",
                                              "accepted by the owner (--accept-coverage-gaps)"])
    elif gaps:
        gap = Reading("coverage_gap", "Unread records hold use that tells the servers apart",
                      "SUPPORTED", supports=[f"{gaps} unread files or lines could hold a use"])
    else:
        gap = Reading("coverage_gap", "Unread records hold use that tells the servers apart",
                      "WEAKENED", weakens=["0 unread files or lines in the window"])
    names = ",".join(servers)
    return Finding(
        id=f"F-dup-mcp_server-{names}", subject=f"mcp_server:{names}",
        observed=f"MCP servers {', '.join(servers)} expose the same {shared_tools} tool names",
        readings=[
            Reading("duplicate", "One server is a redundant copy of the other", "UNRESOLVED",
                    supports=[f"identical sets of {shared_tools} tool names"]),
            gap,
            Reading("different_targets", "The servers reach different accounts or "
                    "workspaces through the same tools", "NOT_TESTABLE"),
        ],
        decision=decision, decision_reason=reason,
        would_change=["the servers' configured targets differ",
                      "an unread file found to hold a use of one server only"],
        provenance=provenance(ev, gaps_accepted),
        coverage="PARTIAL" if gaps else "COMPLETE")


def coverage_finding(ev: EvidenceSet) -> Finding:
    cov = ev.coverage
    gaps = cov.unread_gaps()
    excl = {k: v for k, v in cov.excluded().items() if v}
    return Finding(
        id="F-coverage", subject="observer",
        observed=(f"{cov.files_read} transcript files read, {gaps} unread gaps, "
                  f"{sum(excl.values())} files excluded with a reason"),
        readings=[
            Reading("complete", "Every record that could hold usage or a tool call was read",
                    "WEAKENED" if gaps else "SUPPORTED",
                    supports=[] if gaps else ["0 unread gaps"],
                    weakens=[w for w in cov.warnings() if w] if gaps else []),
            Reading("partial", "Some usage or tool calls were not observed",
                    "SUPPORTED" if gaps else "WEAKENED",
                    supports=[f"{gaps} unread gaps"] if gaps else [],
                    weakens=[] if gaps else ["0 unread gaps"]),
            Reading("deleted_history", "Older transcripts were removed (cleanupPeriodDays)",
                    "NOT_TESTABLE"),
        ],
        decision="REPORT_WITH_COVERAGE" if gaps else "REPORT",
        decision_reason="conclusions about absence are held while gaps exist" if gaps else
        "absence of a record can be read as absence in the window",
        would_change=["reading the unread or unknown files", "a transcript format change"],
        provenance=provenance(ev), coverage="PARTIAL" if gaps else "COMPLETE")


def burn_finding(ev: EvidenceSet, burn: dict[str, Any]) -> Finding:
    unc = burn.get("total_uncertainty") or ev.total_uncertainty()
    amb = int(unc.get("ambiguous_requests", 0))
    classes = burn.get("duplicate_classes", {})
    return Finding(
        id="F-burn-total", subject="token total",
        observed=(f"{burn['observed']['metric_total']:,} tokens over "
                  f"{burn['observed']['requests']:,} requests; duplicate classes {classes}"),
        readings=[
            Reading("exact", "The counted total is exact", "WEAKENED" if amb else "SUPPORTED",
                    supports=[] if amb else ["every duplicate merged to one of its own copies"],
                    weakens=[f"{amb} requests with copies that disagree for no known reason"]
                    if amb else []),
            Reading("bounded", "The true total lies in the band",
                    "SUPPORTED", supports=[f"lower {unc['lower']:,}, upper {unc['upper']:,}"]),
            Reading("quota", "The total says how much provider quota is left", "NOT_TESTABLE"),
        ],
        decision="REPORT_BAND" if amb else "REPORT_TOTAL",
        decision_reason="the counted total is the upper bound" if amb else
        "no ambiguous duplicate in the period",
        would_change=["a documented meaning for differing copies", "a new transcript version"],
        provenance=provenance(ev),
        coverage="PARTIAL" if ev.coverage.unread_gaps() else "COMPLETE")
