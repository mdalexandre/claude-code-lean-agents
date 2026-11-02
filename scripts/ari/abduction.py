"""Competing explanations for an observation, kept open until a test discriminates them.

An observation (for example "general-purpose starts with far more context than
lean-worker") never selects its first explanation as fact. Each HypothesisSet holds the
observation, the evidence references, a working (primary) hypothesis, at least one
alternative, supporting and conflicting evidence per explanation, a discriminating test,
an explicit confidence, a state and the next permitted action.

Recursion is bounded: a set may grow child questions only within max_depth and max_nodes.
A failed test updates or rejects its hypothesis; nothing is deleted.
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass, field
from typing import Any

SCHEMA = "ari-hypothesis-set/1"
STATES = ("OPEN", "TESTING", "SUPPORTED", "REJECTED", "INCONCLUSIVE", "BUDGET_EXHAUSTED")
OUTCOMES = ("supports", "refutes", "inconclusive")
CONFIDENCE = ("unknown", "low", "medium")  # "high" is never assigned automatically
MAX_DEPTH = 3
MAX_NODES = 24


@dataclass
class Explanation:
    id: str
    claim: str
    mechanism: str
    test: str  # discriminating test or falsifier
    supporting: list[str] = field(default_factory=list)
    conflicting: list[str] = field(default_factory=list)
    state: str = "OPEN"


@dataclass
class HypothesisSet:
    id: str
    observation: str
    evidence_refs: list[str]
    explanations: list[Explanation]
    primary: str | None
    confidence: str
    state: str
    next_action: str
    depth: int = 0
    parent: str | None = None
    results: list[dict[str, Any]] = field(default_factory=list)
    schema: str = SCHEMA

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def validate(hs: HypothesisSet) -> list[str]:
    """Return a list of contract violations (empty when the set is well formed)."""
    errs: list[str] = []
    if not hs.observation.strip():
        errs.append("observation is empty")
    if not hs.evidence_refs:
        errs.append("no evidence reference")
    if len(hs.explanations) < 2:
        errs.append("needs a primary hypothesis and at least one alternative")
    ids = [e.id for e in hs.explanations]
    if len(ids) != len(set(ids)):
        errs.append("duplicate explanation id")
    for e in hs.explanations:
        if not e.test.strip():
            errs.append(f"{e.id}: no discriminating test")
        if e.state not in STATES:
            errs.append(f"{e.id}: unknown state {e.state}")
    if hs.primary is not None and hs.primary not in ids:
        errs.append("primary is not one of the explanations")
    if hs.confidence not in CONFIDENCE:
        errs.append(f"confidence {hs.confidence} not allowed")
    if hs.state not in STATES:
        errs.append(f"unknown set state {hs.state}")
    if hs.depth > MAX_DEPTH:
        errs.append("depth above the recursion bound")
    if not hs.next_action.strip():
        errs.append("no next permitted action")
    return errs


def _sid(observation: str, refs: list[str]) -> str:
    return "H-" + hashlib.sha256(json.dumps([observation, sorted(refs)]).encode()).hexdigest()[:12]


# Catalog of explanations for context size observations. Each entry names the mechanism,
# the evidence key that would support it, and a test that can separate it from the rest.
CONTEXT_CATALOG: list[tuple[str, str, str, str]] = [
    ("claude_md", "More CLAUDE.md material reaches the agent",
     "custom and general-purpose agents load CLAUDE.md unless omitClaudeMd: true",
     "Run the same brief with omitClaudeMd true and false, all else equal; compare first "
     "request context."),
    ("skills", "The skills list is exposed to the agent",
     "an agent with the Skill tool receives the installed skill listing",
     "Run the same brief with and without Skill in the tools allowlist."),
    ("mcp", "MCP tool names and server instructions are exposed",
     "an agent with MCP tools receives deferred tool names and server instructions",
     "Disable MCP servers for one matched run (or allowlist no mcp__ tools) and compare."),
    ("agent_init", "The agent type initialises differently",
     "built in agents (Explore, Plan, fork) carry different system prompts and inheritance",
     "Compare agent types with an identical tools line and identical CLAUDE.md exposure."),
    ("version_model", "Claude Code version or model differs between the compared runs",
     "system prompts and tool definitions change between releases and models",
     "Restrict the comparison to runs with the same version and model."),
    ("environment", "Execution environment differs (project, working directory, git state)",
     "project CLAUDE.md, git status snapshot and directory contents vary by project",
     "Compare runs from the same project and session only."),
]


def context_gap(
    observation: str,
    evidence_refs: list[str],
    signals: dict[str, list[str]],
    conflicts: dict[str, list[str]] | None = None,
) -> HypothesisSet:
    """Build a set for a context size gap or growth. `signals` maps catalog ids to evidence.

    The primary is the explanation with the most supporting signals; on a tie or with no
    signal at all the primary is None and the confidence is "unknown".
    """
    conflicts = conflicts or {}
    exps = [
        Explanation(id=cid, claim=claim, mechanism=mech, test=test,
                    supporting=list(signals.get(cid, [])),
                    conflicting=list(conflicts.get(cid, [])))
        for cid, claim, mech, test in CONTEXT_CATALOG
    ]
    scored = sorted(((len(e.supporting) - len(e.conflicting), e.id) for e in exps), reverse=True)
    best, runner = scored[0], scored[1]
    if best[0] <= 0 or best[0] == runner[0]:
        primary: str | None = None
        confidence = "unknown"
        nxt = ("Run the discriminating test of the explanations with supporting evidence; "
               "no explanation is preferred yet.")
    else:
        primary = best[1]
        confidence = "low"
        nxt = f"Run the test of {primary} and of the strongest alternative ({runner[1]})."
    return HypothesisSet(
        id=_sid(observation, evidence_refs),
        observation=observation,
        evidence_refs=list(evidence_refs),
        explanations=exps,
        primary=primary,
        confidence=confidence,
        state="OPEN",
        next_action=nxt,
    )


def record_result(hs: HypothesisSet, explanation_id: str, outcome: str, evidence: str) -> None:
    """Apply one test result. A refuting result rejects that explanation, never hides it."""
    if outcome not in OUTCOMES:
        raise ValueError(f"outcome must be one of {OUTCOMES}")
    target = next((e for e in hs.explanations if e.id == explanation_id), None)
    if target is None:
        raise KeyError(explanation_id)
    hs.results.append({"explanation": explanation_id, "outcome": outcome, "evidence": evidence})
    if outcome == "supports":
        target.supporting.append(evidence)
        target.state = "SUPPORTED"
    elif outcome == "refutes":
        target.conflicting.append(evidence)
        target.state = "REJECTED"
    else:
        target.state = "INCONCLUSIVE"
    open_left = [e for e in hs.explanations if e.state in ("OPEN", "INCONCLUSIVE", "TESTING")]
    supported = [e for e in hs.explanations if e.state == "SUPPORTED"]
    if hs.primary == explanation_id and outcome == "refutes":
        hs.primary = supported[0].id if len(supported) == 1 else None
        hs.confidence = "low" if hs.primary else "unknown"
    if len(supported) == 1 and not open_left:
        hs.state = "SUPPORTED"
        hs.primary = supported[0].id
        hs.confidence = "medium"
        hs.next_action = "Replicate on a second matched workload before any promotion."
    elif not supported and not open_left:
        hs.state = "REJECTED"
        hs.primary = None
        hs.confidence = "unknown"
        hs.next_action = "Every catalogued explanation was refuted: open a new question."
    else:
        hs.state = "TESTING"
        hs.next_action = "Test the remaining open explanations: " + ", ".join(
            e.id for e in open_left) if open_left else "Resolve the competing supported results."


def child(parent: HypothesisSet, observation: str, evidence_refs: list[str],
          signals: dict[str, list[str]], node_count: int) -> HypothesisSet:
    """Open a child question under the recursion bound, or return a BUDGET_EXHAUSTED stub."""
    hs = context_gap(observation, evidence_refs, signals)
    hs.depth = parent.depth + 1
    hs.parent = parent.id
    if hs.depth > MAX_DEPTH or node_count >= MAX_NODES:
        hs.state = "BUDGET_EXHAUSTED"
        hs.next_action = "Stop: recursion bound reached; report the question as open."
        hs.depth = min(hs.depth, MAX_DEPTH)
    return hs


def from_dict(d: dict[str, Any]) -> HypothesisSet:
    exps = [Explanation(**e) for e in d["explanations"]]
    rest = {k: v for k, v in d.items() if k != "explanations"}
    return HypothesisSet(explanations=exps, **rest)
