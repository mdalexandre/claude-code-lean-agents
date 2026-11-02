"""Holon contracts for ARI 2.0 (ARI2-R18): logical responsibilities, not processes.

Each holon has one accountable parent, an authority boundary, typed inputs and outputs,
preconditions and guarantees, evidence requirements, local correction, a budget, a safe
failure state, a verification and an escalation path. Authority, dependency and
coordination are kept apart: `parent` is authority, `consumes` is data dependency, and
coordination is the supervisor's order of calls. `docs/specs/ari-holons-v2.0.0.json` is
generated from HOLONS and a test checks the two agree.
"""
from __future__ import annotations

from typing import Any

VERSION = "2.0.0"
FORBIDDEN = ("commit", "push", "schedule", "uninstall_or_disable", "upload", "publish",
             "promote_to_vgtp_registry", "edit_user_config", "edit_hooks", "relax_gate")
FIELDS = ("id", "version", "name", "parent", "authority", "inputs", "outputs", "consumes",
          "preconditions", "guarantees", "evidence", "feedback", "budget", "safe_failure",
          "verification", "escalation", "modules")

HOLONS: list[dict[str, Any]] = [
    {"id": "H0", "version": VERSION, "name": "ARI Supervisor", "parent": "owner",
     "authority": ["order holon calls", "refuse bad input (exit 2)",
                   "degrade to PARTIAL with warnings"],
     "inputs": {"cli_args": "argparse.Namespace", "roots": "list[path]"},
     "outputs": {"exit_code": "int 0|1|2", "report": "json|text|html"},
     "consumes": ["H1", "H2", "H3", "H4", "H5"],
     "preconditions": ["arguments pass bounded parsing"],
     "guarantees": ["no traceback reaches the user", "coverage warnings are printed"],
     "evidence": "every output names its window and parser version",
     "feedback": "bounded argument types; broad catch returns exit 2 with the reason",
     "budget": {"memory": "12G scope for real data", "network": "none"},
     "safe_failure": "exit 2 with 'refused: <reason>', nothing written",
     "verification": "tests/test_ari2_contracts.py, CLI refusal tests",
     "escalation": "owner reads the refusal",
     "modules": ["scripts/ari/cli_common.py", "scripts/weekly_burn.py",
                 "scripts/skill_mcp_audit.py", "scripts/ari_report.py",
                 "scripts/ari_nightly.py", "scripts/ari_discover.py",
                 "scripts/ari/discovery.py"]},
    {"id": "H1", "version": VERSION, "name": "Evidence Integrity", "parent": "H0",
     "authority": ["read transcripts under configured roots", "classify sources",
                   "skip and count damaged records"],
     "inputs": {"roots": "list[path]", "window": "[since, until) UTC"},
     "outputs": {"evidence": "EvidenceSet (ari-event/1)", "manifest": "list[source row]",
                 "coverage": "Coverage counters"},
     "consumes": [],
     "preconditions": ["roots are directories; symlinks leaving a root are refused"],
     "guarantees": ["one request counted once", "every unread file is classified",
                    "ambiguous duplicates carry a band"],
     "evidence": "sampled checkpoint per source; parser version ari-parser/2",
     "feedback": "coverage warnings; unread_gaps() feeds H3 and H4",
     "budget": {"line_bytes": 67108864, "reads": "each file once plus two sampled reads"},
     "safe_failure": "PARTIAL coverage with counted gaps, never a silent zero",
     "verification": "tests/test_ari_evidence.py, tests/test_ari2_evidence.py",
     "escalation": "warnings name the class of every gap",
     "modules": ["scripts/ari/evidence.py"]},
    {"id": "H2", "version": VERSION, "name": "Resource Intelligence", "parent": "H0",
     "authority": ["compute token totals, bands and forecasts"],
     "inputs": {"evidence": "EvidenceSet", "now": "datetime UTC"},
     "outputs": {"burn": "ari-weekly-burn/1 + total_uncertainty, duplicate_classes, "
                         "assumptions"},
     "consumes": ["H1"],
     "preconditions": ["at least 3 complete history days with usage for a forecast"],
     "guarantees": ["provider quota ETA is always reported unavailable",
                    "the counted total is labelled as the upper bound when ambiguous"],
     "evidence": "period, window and assumptions in every output",
     "feedback": "backtest against persistence",
     "budget": {"history_days": "up to the read window"},
     "safe_failure": "forecast INSUFFICIENT_DATA",
     "verification": "tests/test_ari_forecast.py, tests/test_ari2_evidence.py",
     "escalation": "warning when the read window misses part of the period",
     "modules": ["scripts/ari/forecast.py"]},
    {"id": "H3", "version": VERSION, "name": "Capability Intelligence", "parent": "H0",
     "authority": ["recommend; never change a setting"],
     "inputs": {"evidence": "EvidenceSet", "skills_roots": "list[path]",
                "mcp_configs": "list[path]"},
     "outputs": {"audit": "ari-capability-audit/1 with findings"},
     "consumes": ["H1", "H4"],
     "preconditions": ["exposure records present for removal class advice"],
     "guarantees": ["removal class advice passes two gates: the recommendation validator "
                    "and the finding contract"],
     "evidence": "exposed sessions, invocations, indirect reads per capability",
     "feedback": "rejected advice keeps its reason",
     "budget": {"min_exposed_sessions": 20, "min_window_days": 14},
     "safe_failure": "INVESTIGATE or INSUFFICIENT_EVIDENCE",
     "verification": "tests/test_ari_audit.py, tests/test_ari2_interpret.py",
     "escalation": "--accept-coverage-gaps is the owner's explicit choice",
     "modules": ["scripts/ari/audit.py"]},
    {"id": "H4", "version": VERSION, "name": "Hermeneutic Intelligence", "parent": "H0",
     "authority": ["state readings and the decision the evidence justifies"],
     "inputs": {"evidence": "EvidenceSet", "capability": "dict", "burn": "dict"},
     "outputs": {"finding": "ari-finding/1", "html": "escaped offline page"},
     "consumes": ["H1", "H2"],
     "preconditions": ["provenance available"],
     "guarantees": ["at least two readings", "no removal decision under SUPPORTED gaps",
                    "no script and no external resource in the page"],
     "evidence": "provenance on every finding",
     "feedback": "validate_finding refuses a contract breach",
     "budget": {"detailed_findings_in_html": 8},
     "safe_failure": "decision INVESTIGATE",
     "verification": "tests/test_ari2_interpret.py, tests/test_ari_report.py",
     "escalation": "would_change names the evidence that reopens a finding",
     "modules": ["scripts/ari/interpret.py", "scripts/ari/report.py",
                 "scripts/ari/abduction.py"]},
    {"id": "H5", "version": VERSION, "name": "Procedural Learning", "parent": "H0",
     "authority": ["create SPECIFIED lessons", "replay", "quarantine on failed tests"],
     "inputs": {"evidence": "EvidenceSet", "state_dir": "path"},
     "outputs": {"state": "ari-learn-state/2", "summary": "run summary"},
     "consumes": ["H1"],
     "preconditions": ["state file is ari-learn-state/1 or /2"],
     "guarantees": ["idempotent reruns", "no lesson from an unstable day",
                    "PROMOTED needs an approval bound to the content hash",
                    "never writes a vgtp registry"],
     "evidence": "snapshot content hashes; replay bound to code sha256 and parser",
     "feedback": "replay quarantines a lesson whose trigger no longer reproduces",
     "budget": {"days": 14, "min_runs": 3},
     "safe_failure": "refused promotion with the failed contract named",
     "verification": "tests/test_ari_learn.py, tests/test_ari2_learn.py",
     "escalation": "APPROVAL_PENDING waits for the owner",
     "modules": ["scripts/ari/learn.py"]},
]


def validate(holons: list[dict[str, Any]] | None = None) -> list[str]:
    """Contract violations across the registry; empty when every holon is well formed."""
    hs = HOLONS if holons is None else holons
    errs: list[str] = []
    ids = [h.get("id") for h in hs]
    if len(ids) != len(set(ids)):
        errs.append("duplicate holon id")
    for h in hs:
        hid = h.get("id", "?")
        for f in FIELDS:
            if h.get(f) in (None, "", [], {}) and f != "consumes":
                errs.append(f"{hid}: missing {f}")
        parent = h.get("parent")
        if parent != "owner" and parent not in ids:
            errs.append(f"{hid}: parent {parent} is not a holon")
        if parent == hid:
            errs.append(f"{hid}: its own parent")
        for f in ("authority", "consumes"):  # a string would be read letter by letter
            v = h.get(f, [])
            if not (isinstance(v, list) and all(isinstance(x, str) for x in v)):
                errs.append(f"{hid}: {f} is not a list of strings")
        consumes = h.get("consumes", [])
        for c in consumes if isinstance(consumes, list) else []:
            if c not in ids or c == hid:
                errs.append(f"{hid}: consumes unknown or itself {c}")
        auth = h.get("authority", [])
        text = " ".join(map(str, auth if isinstance(auth, list) else [auth])).lower()
        for word in FORBIDDEN:  # "uninstall_or_disable" forbids each alternative
            if any(alt in text or alt.replace("_", " ") in text
                   for alt in word.split("_or_")):
                errs.append(f"{hid}: authority includes forbidden action {word}")
    parents = {h.get("id"): h.get("parent") for h in hs}
    for start in parents:
        seen, cur = set(), start
        while cur in parents and cur not in seen:
            seen.add(cur)
            cur = parents[cur]
        if cur in seen:  # walked back to a holon already on the path
            errs.append(f"{start}: parent cycle through {cur}")
            break
    roots = [h for h in hs if h.get("parent") == "owner"]
    if len(roots) != 1:
        errs.append("exactly one holon must answer to the owner")
    return errs


def document() -> dict[str, Any]:
    return {"schema": "ari-holons/1", "version": VERSION, "forbidden": list(FORBIDDEN),
            "holons": HOLONS}
