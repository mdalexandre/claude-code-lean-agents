"""Bounded open ended abductive discovery (schema ari-discovery/1, ARI2-R16).

Observation -> competing hypotheses -> evidence and counterevidence -> discriminating
tests -> revised interpretation -> new questions -> validated findings or a backlog.

Status is an evidence state, never a probability:
    HYPOTHESIS    proposed, not yet decided by a test
    SUPPORTED     a supporting result with sha256 bound evidence, no refuting result, and
                  every competitor CONTRADICTED or UNRESOLVED with a stated reason
    CONTRADICTED  only refuting results
    UNRESOLVED    conflicting results, or a reason why no test can decide it

Bounds: depth 3, 4 hypotheses per question, 24 questions. A question that would exceed a
bound is recorded as BUDGET_EXHAUSTED in the backlog, never silently dropped. A question
that repeats an ancestor's observation is a cycle and is refused. A hypothesis past the
per question cap is refused and kept in the backlog. Adding a hypothesis settles its
question again, so SUPPORTED holds only while every rival, a later one included, is
decided. A hypothesis whose
normalised mechanism already exists anywhere in the ledger is refused as not novel.
"""
from __future__ import annotations

import hashlib
import json
import re
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

SCHEMA = "ari-discovery/1"
STATUSES = ("HYPOTHESIS", "SUPPORTED", "CONTRADICTED", "UNRESOLVED")
OUTCOMES = ("supports", "refutes", "inconclusive")
MAX_DEPTH = 3
MAX_PER_NODE = 4
MAX_NODES = 24


class DiscoveryError(Exception):
    pass


@dataclass
class Hypothesis:
    id: str
    question: str
    provenance: str
    mechanism: str
    predictions: list[str]
    falsification: str
    status: str = "HYPOTHESIS"
    results: list[dict[str, Any]] = field(default_factory=list)
    uncertainties: list[str] = field(default_factory=list)
    unresolved_reason: str = ""


@dataclass
class Question:
    id: str
    observation: str
    depth: int
    parent: str | None
    status: str = "OPEN"  # OPEN, CLOSED, BUDGET_EXHAUSTED
    hypotheses: list[str] = field(default_factory=list)


def _norm(text: str) -> str:
    return " ".join(re.sub(r"[^a-z0-9 ]", " ", text.lower()).split())


def _hid(text: str) -> str:
    return hashlib.sha256(_norm(text).encode()).hexdigest()[:10]


class Ledger:
    def __init__(self, max_depth: int = MAX_DEPTH, max_per_node: int = MAX_PER_NODE,
                 max_nodes: int = MAX_NODES) -> None:
        self.questions: dict[str, Question] = {}
        self.hypotheses: dict[str, Hypothesis] = {}
        self.backlog: list[dict[str, str]] = []
        self.budget = {"max_depth": max_depth, "max_per_node": max_per_node,
                       "max_nodes": max_nodes}

    # questions -----------------------------------------------------------------------
    def ask(self, observation: str, parent: str | None = None) -> Question:
        if not observation.strip():
            raise DiscoveryError("an observation is required")
        depth = 0
        if parent is not None:
            if parent not in self.questions:
                raise DiscoveryError(f"no question {parent}")
            depth = self.questions[parent].depth + 1
            anc: str | None = parent
            while anc is not None:  # cycle: the same observation again down one branch
                if _norm(self.questions[anc].observation) == _norm(observation):
                    raise DiscoveryError(f"cycle: repeats the observation of {anc}")
                anc = self.questions[anc].parent
        qid = "Q-" + _hid(f"{parent}|{observation}")
        if qid in self.questions:
            return self.questions[qid]
        q = Question(qid, observation, depth, parent)
        if depth > self.budget["max_depth"] or len(self.questions) >= self.budget["max_nodes"]:
            q.status = "BUDGET_EXHAUSTED"
            self.backlog.append({"question": qid, "observation": observation,
                                 "reason": "recursion budget exhausted"})
        self.questions[qid] = q
        return q

    # hypotheses ----------------------------------------------------------------------
    def propose(self, qid: str, mechanism: str, provenance: str, predictions: list[str],
                falsification: str) -> Hypothesis:
        q = self.questions.get(qid)
        if q is None:
            raise DiscoveryError(f"no question {qid}")
        if q.status == "BUDGET_EXHAUSTED":
            raise DiscoveryError(f"{qid} is beyond the budget; it stays in the backlog")
        if not (mechanism.strip() and provenance.strip() and predictions
                and falsification.strip()):
            raise DiscoveryError("mechanism, provenance, predictions and a falsification test "
                                 "are all required")
        key = _norm(mechanism)
        for h in self.hypotheses.values():
            if _norm(h.mechanism) == key:
                raise DiscoveryError(f"not novel: same mechanism as {h.id}")
        if len(q.hypotheses) >= self.budget["max_per_node"]:
            entry = {"question": qid, "observation": q.observation, "mechanism": mechanism,
                     "reason": "per question hypothesis cap reached"}
            if entry not in self.backlog:  # a refused rival is kept, never silently lost
                self.backlog.append(entry)
            raise DiscoveryError(f"{qid} already has {len(q.hypotheses)} hypotheses; the "
                                 "rival is kept in the backlog")
        h = Hypothesis("H-" + _hid(mechanism), qid, provenance, mechanism, list(predictions),
                       falsification)
        self.hypotheses[h.id] = h
        q.hypotheses.append(h.id)
        self._settle(qid)  # a new open rival takes SUPPORTED back to HYPOTHESIS
        return h

    def competitors(self, hid: str) -> list[str]:
        h = self.hypotheses[hid]
        return [x for x in self.questions[h.question].hypotheses if x != hid]

    def record(self, hid: str, outcome: str, evidence_file: Path, note: str) -> Hypothesis:
        """Bind one test result by sha256 and revise the statuses of the question."""
        if outcome not in OUTCOMES:
            raise DiscoveryError(f"outcome must be one of {OUTCOMES}")
        h = self.hypotheses.get(hid)
        if h is None:
            raise DiscoveryError(f"no hypothesis {hid}")
        p = Path(evidence_file)
        if not p.is_file():
            raise DiscoveryError("evidence file does not exist")
        h.results.append({"outcome": outcome, "note": note, "evidence": p.name,
                          "sha256": hashlib.sha256(p.read_bytes()).hexdigest()})
        self._settle(h.question)
        return h

    def unresolved(self, hid: str, reason: str) -> Hypothesis:
        if not reason.strip():
            raise DiscoveryError("an unresolved hypothesis needs a reason")
        h = self.hypotheses[hid]
        h.status, h.unresolved_reason = "UNRESOLVED", reason
        self._settle(h.question)
        return h

    def _settle(self, qid: str) -> None:
        q = self.questions[qid]
        for hid in q.hypotheses:
            h = self.hypotheses[hid]
            outs = {r["outcome"] for r in h.results}
            if "supports" in outs and "refutes" in outs:
                h.status = "UNRESOLVED"
                h.unresolved_reason = "conflicting results: a test supported and a test refuted"
            elif "refutes" in outs:
                h.status = "CONTRADICTED"
            elif h.status == "UNRESOLVED" and h.unresolved_reason:
                continue
            else:
                h.status = "HYPOTHESIS"
        for hid in q.hypotheses:  # SUPPORTED only once every competitor is decided
            h = self.hypotheses[hid]
            if h.status != "HYPOTHESIS" or "supports" not in {r["outcome"] for r in h.results}:
                continue
            open_rivals = [c for c in self.competitors(hid)
                           if self.hypotheses[c].status in ("HYPOTHESIS", "SUPPORTED")]
            if open_rivals:
                h.uncertainties = [f"competitor {c} is not ruled out" for c in open_rivals]
            else:
                h.status, h.uncertainties = "SUPPORTED", []
        if q.hypotheses and all(self.hypotheses[h].status != "HYPOTHESIS"
                                for h in q.hypotheses):
            q.status = "CLOSED"
        elif q.status == "CLOSED":  # an open rival reopens the question
            q.status = "OPEN"

    # integrity -----------------------------------------------------------------------
    def validate(self) -> list[str]:
        errs: list[str] = []
        for q in self.questions.values():
            if q.depth > self.budget["max_depth"] and q.status != "BUDGET_EXHAUSTED":
                errs.append(f"{q.id}: beyond the depth bound")
            if len(q.hypotheses) > self.budget["max_per_node"]:
                errs.append(f"{q.id}: too many hypotheses")
        for h in self.hypotheses.values():
            if h.status not in STATUSES:
                errs.append(f"{h.id}: unknown status {h.status}")
            if h.status == "SUPPORTED":
                sup = [r for r in h.results if r["outcome"] == "supports"]
                if not sup or not all(len(r.get("sha256", "")) == 64 for r in sup):
                    errs.append(f"{h.id}: SUPPORTED without bound supporting evidence")
                if any(r["outcome"] == "refutes" for r in h.results):
                    errs.append(f"{h.id}: SUPPORTED despite a refuting result")
                for c in self.competitors(h.id):
                    if self.hypotheses[c].status in ("HYPOTHESIS", "SUPPORTED"):
                        errs.append(f"{h.id}: SUPPORTED while competitor {c} is open")
            if h.status == "UNRESOLVED" and not h.unresolved_reason:
                errs.append(f"{h.id}: UNRESOLVED without a reason")
        return errs

    def to_dict(self) -> dict[str, Any]:
        return {"schema": SCHEMA, "budget": self.budget,
                "questions": [asdict(q) for q in self.questions.values()],
                "hypotheses": [asdict(h) for h in self.hypotheses.values()],
                "backlog": self.backlog, "violations": self.validate()}

    def dump(self, path: Path) -> None:
        path.write_text(json.dumps(self.to_dict(), indent=1, sort_keys=True), encoding="utf-8")
