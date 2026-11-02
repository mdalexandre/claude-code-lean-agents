"""Recursive abductive investigations of ARI findings (H2, R1 and R2).

One investigation per target, each with its own discovery ledger (depth 3, 4 hypotheses
per question). A skill the audit marks INVESTIGATE (exposed, never seen used) starts with

    Q1  why is there no recorded invocation?
        unused     no direct and no indirect use at all
        indirect   used only by reading its files (another skill or agent uses it)
        telemetry  raw transcripts hold invocations the parser missed

and when Q1 settles on unused or indirect, a child question opens (R1):

    Q2  is it worth keeping exposed?
        redundant          its description overlaps another skill's
        unrouted_demand    users ask for what it does, yet it is never chosen
        dormant            distinct and not asked for

Every hypothesis carries a probe spec with rules fixed before the probe runs (probes.py).
A probe result is recorded once per evidence digest, so a rerun on the same data adds
nothing. Ambiguity is kept: two supported rivals leave the question OPEN for the model
investigator; nothing picks a winner.
"""
from __future__ import annotations

import copy
import hashlib
import json
from pathlib import Path
from typing import Any

from . import probes
from .discovery import DiscoveryError, Ledger

SCHEMA = "ari-investigation/1"
Q2_TRIGGER = ("unused", "indirect")
CONCLUSIONS = {"unused", "indirect", "telemetry", "redundant", "unrouted_demand", "dormant"}


def _q1(skill: str) -> list[tuple[str, str, str, dict[str, Any]]]:
    s = {"skill": skill}
    return [
        ("unused", f"{skill} is genuinely unused: no direct and no indirect use",
         "any raw invocation or indirect read",
         probes.spec("skill_use_signals", s, ("==", 0), (">", 0))),
        ("indirect", f"{skill} is used only indirectly, by reading its files",
         "no indirect read of its files",
         probes.spec("skill_indirect_reads", s, (">", 0), ("==", 0))),
        ("telemetry", f"{skill} is invoked, but the parser misses the invocations",
         "raw transcripts hold no more invocations than the parser counted",
         probes.spec("skill_raw_minus_parsed", s, (">", 0), ("<=", 0))),
    ]


def _q2(skill: str) -> list[tuple[str, str, str, dict[str, Any]]]:
    s = {"skill": skill}
    return [
        ("redundant", f"{skill} duplicates another skill's description",
         "description overlap with every other skill below 0.25",
         probes.spec("skill_description_overlap", s, (">=", 0.5), ("<", 0.25))),
        ("unrouted_demand", f"users ask for what {skill} does, but routing never picks it",
         "no user prompt holds its distinctive words",
         probes.spec("skill_eligible_prompts", s, (">=", 3), ("==", 0))),
        ("dormant", f"{skill} is distinct and nobody asked for it in the window",
         "three or more prompts hold its distinctive words",
         probes.spec("skill_eligible_prompts", s, ("==", 0), (">=", 3))),
    ]


class Investigation:
    def __init__(self, target: str, kind: str = "skill_no_use") -> None:
        self.target, self.kind = target, kind
        self.ledger = Ledger()
        self.specs: dict[str, dict[str, Any]] = {}     # hypothesis id -> probe spec
        self.labels: dict[str, str] = {}                # hypothesis id -> label
        self.done: list[str] = []                       # "<hid>@<digest>" already probed
        self.rounds: dict[str, int] = {}                # question id -> model rounds
        self.nonprogress: list[str] = []                # questions closed to the model
        self.interpretations: list[dict[str, str]] = []
        self.root = ""

    # construction --------------------------------------------------------------------
    @classmethod
    def open_skill(cls, skill: str, exposed: int) -> Investigation:
        inv = cls(skill)
        q = inv.ledger.ask(f"skill {skill} was listed in {exposed} sessions with no "
                           "recorded invocation")
        inv.root = q.id
        inv._add(q.id, _q1(skill), "ARI 3.0 built in taxonomy")
        return inv

    def _add(self, qid: str, hyps: list[tuple[str, str, str, dict[str, Any]]],
             provenance: str) -> None:
        for label, mech, fals, spec in hyps:
            h = self.ledger.propose(qid, mech, provenance, [json.dumps(spec["supports_if"])],
                                    fals)
            self.specs[h.id], self.labels[h.id] = spec, label

    # probing -------------------------------------------------------------------------
    def statuses(self) -> dict[str, str]:
        return {h.id: h.status for h in self.ledger.hypotheses.values()} | {
            q.id: q.status for q in self.ledger.questions.values()}

    def probe(self, ctx: probes.ProbeContext, digest: str, evidence_dir: Path) -> int:
        """Run each untested probe for this digest, follow the child questions it opens,
        and return how many statuses changed."""
        before = self.statuses()
        for _level in range(int(self.ledger.budget["max_depth"]) + 2):
            if not self._probe_once(ctx, digest, evidence_dir):
                break
            self.advance()
        after = self.statuses()
        return sum(1 for k, v in after.items() if before.get(k) != v)

    def _probe_once(self, ctx: probes.ProbeContext, digest: str, evidence_dir: Path) -> int:
        ran = 0
        for hid, spec in list(self.specs.items()):
            h = self.ledger.hypotheses[hid]
            key = f"{hid}@{digest}"
            if (key in self.done or h.status == "UNRESOLVED"
                    or self.ledger.questions[h.question].status == "CLOSED"):
                continue  # a settled question is rechecked by replay, not re-recorded
            value, detail = probes.run(ctx, spec)
            outcome = probes.judge(value, spec)
            body = {"target": self.target, "hypothesis": hid, "probe": spec,
                    "value": probes.jsonable(value),
                    "detail": detail, "outcome": outcome, "evidence_digest": digest}
            raw = json.dumps(body, sort_keys=True, default=str).encode()
            f = evidence_dir / f"{hashlib.sha256(raw).hexdigest()[:20]}.json"
            evidence_dir.mkdir(parents=True, exist_ok=True)
            f.write_bytes(raw)
            self.ledger.record(hid, outcome, f, f"{spec['probe']}={value}")
            self.done.append(key)
            ran += 1
        return ran

    def advance(self) -> None:
        """R1: a settled why-question opens the worth-keeping question once."""
        if self.kind != "skill_no_use" or not self.root:
            return
        root = self.ledger.questions[self.root]
        winner = self.supported(self.root)
        if root.status == "CLOSED" and winner in Q2_TRIGGER:
            obs = f"{self.target} has no direct use ({winner}); is it worth keeping exposed"
            if not any(q.parent == self.root and q.observation == obs
                       for q in self.ledger.questions.values()):
                q = self.ledger.ask(obs, self.root)
                if q.status == "OPEN":
                    self._add(q.id, _q2(self.target), "ARI 3.0 built in taxonomy")

    def recheck(self, ctx: probes.ProbeContext) -> bool:
        """Replay: does every supported hypothesis of the chain still hold on new evidence,
        with every rival still refuted? Records nothing."""
        chain = [h for h in self.ledger.hypotheses.values() if h.status == "SUPPORTED"]
        if not chain:
            return False
        for h in chain:
            for hid in self.ledger.questions[h.question].hypotheses:
                spec = self.specs.get(hid)
                if spec is None:
                    return False
                out = probes.judge(probes.run(ctx, spec)[0], spec)
                want = "supports" if hid == h.id else "refutes"
                rival = self.ledger.hypotheses[hid]
                if hid != h.id and rival.status == "UNRESOLVED":
                    continue
                if out != want:
                    return False
        return True

    # reading -------------------------------------------------------------------------
    def supported(self, qid: str) -> str | None:
        for hid in self.ledger.questions[qid].hypotheses:
            if self.ledger.hypotheses[hid].status == "SUPPORTED":
                return self.labels.get(hid, "model")
        return None

    def conclusion(self) -> dict[str, Any]:
        """The deepest settled answer, and the open questions left."""
        chain: list[dict[str, Any]] = []
        open_q: list[str] = []
        for q in self.ledger.questions.values():
            s = self.supported(q.id)
            if s:
                chain.append({"question": q.id, "depth": q.depth, "supported": s})
            elif q.status == "OPEN":
                open_q.append(q.id)
        chain.sort(key=lambda c: c["depth"])
        return {"target": self.target, "settled": chain, "open": open_q,
                "final": chain[-1]["supported"] if chain else None,
                "unresolved": [h.id for h in self.ledger.hypotheses.values()
                               if h.status == "UNRESOLVED"]}

    def open_questions(self) -> list[str]:
        return [q.id for q in self.ledger.questions.values()
                if q.status == "OPEN" and q.id not in self.nonprogress
                and q.hypotheses]

    # persistence ---------------------------------------------------------------------
    def to_dict(self) -> dict[str, Any]:
        return {"schema": SCHEMA, "target": self.target, "kind": self.kind,
                "root": self.root, "ledger": self.ledger.to_dict(), "specs": self.specs,
                "labels": self.labels, "done": self.done, "rounds": self.rounds,
                "nonprogress": self.nonprogress, "interpretations": self.interpretations}

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> Investigation:
        if not isinstance(d, dict) or d.get("schema") != SCHEMA:
            raise DiscoveryError("not an " + SCHEMA + " document")
        why = problem(d)
        if why:
            raise DiscoveryError("investigation unusable: " + why)
        inv = cls(str(d["target"]), str(d.get("kind", "skill_no_use")))
        inv.root = str(d.get("root", ""))
        inv.ledger = Ledger.from_dict(d["ledger"])
        if inv.root not in inv.ledger.questions:
            raise DiscoveryError("investigation unusable: root question missing")
        inv.specs, inv.labels = dict(d["specs"]), dict(d["labels"])
        inv.done, inv.rounds = list(d["done"]), dict(d.get("rounds", {}))
        inv.nonprogress = list(d.get("nonprogress", []))
        inv.interpretations = list(d.get("interpretations", []))
        return inv

    # model proposals (validated here; the model never touches the ledger) ------------
    def apply(self, p: dict[str, Any], run_id: str) -> str:
        """Apply one validated proposal, all or nothing; return a short note.

        Any refusal, including a wrongly typed field the schema let through, raises
        DiscoveryError and leaves the investigation exactly as it was."""
        snapshot = copy.deepcopy(self.to_dict())  # to_dict shares lists with live state
        try:
            return self._apply(p, run_id)
        except Exception as exc:  # noqa: BLE001  model output is data; restore, then refuse
            kept = [dict(b) for b in self.ledger.backlog]  # refused rivals stay on record
            self.__dict__.update(Investigation.from_dict(snapshot).__dict__)
            self.ledger.backlog = kept
            if isinstance(exc, DiscoveryError):
                raise
            raise DiscoveryError(f"malformed proposal: {type(exc).__name__}") from exc

    def _apply(self, p: dict[str, Any], run_id: str) -> str:
        qid = p.get("question")
        if not isinstance(qid, str):
            raise DiscoveryError("question must be a string")
        if qid not in self.ledger.questions:
            raise DiscoveryError(f"unknown question {qid!r}")
        kind = p.get("type")
        prov = f"model run {run_id}"
        if kind == "hypothesis":
            self._model_hyp(qid, p, prov)
            return "hypothesis added"
        if kind == "child_question":
            obs = _text(p.get("observation"), 300)
            hyps = p.get("hypotheses")
            if not (isinstance(hyps, list) and 2 <= len(hyps) <= 4):
                raise DiscoveryError("a child question needs 2 to 4 competing hypotheses")
            for h in hyps:  # validate every rival before the question exists
                _check_hyp(h)
            q = self.ledger.ask(obs, qid)
            if q.status != "OPEN":
                raise DiscoveryError("child question is beyond the recursion budget")
            for h in hyps:
                self._model_hyp(q.id, h, prov)
            return "child question opened"
        if kind == "unresolved":
            hid = p.get("hypothesis")
            h = self.ledger.hypotheses.get(hid) if isinstance(hid, str) else None
            if h is None or h.question != qid:
                raise DiscoveryError("unresolved names no hypothesis of this question")
            if not h.results:
                raise DiscoveryError("an untested hypothesis cannot be declared unresolved")
            if (self.ledger.questions[qid].status == "OPEN"
                    and any(r["outcome"] == "supports" for r in h.results)):
                raise DiscoveryError("an unresolved verdict on a supported hypothesis would "
                                     "settle the question for its rival")
            q = self.ledger.questions[qid]
            before = (q.status, self.supported(qid))
            self.ledger.unresolved(h.id, "model: " + _text(p.get("reason"), 300))
            if (q.status, self.supported(qid)) != before:  # apply() restores the ledger
                raise DiscoveryError("an unresolved verdict here would settle the question; "
                                     "only a probe result can do that")
            return "hypothesis unresolved"
        if kind == "interpretation":
            self.interpretations.append({"question": qid, "run": run_id,
                                         "text": _text(p.get("text"), 300)})
            return "interpretation kept"
        raise DiscoveryError(f"unknown proposal type {kind!r}")

    def _model_hyp(self, qid: str, h: dict[str, Any], prov: str) -> None:
        spec = _check_hyp(h)
        hyp = self.ledger.propose(qid, _text(h.get("mechanism"), 300), prov,
                                  [_text(h.get("prediction"), 300)],
                                  _text(h.get("falsification"), 300))
        self.specs[hyp.id], self.labels[hyp.id] = spec, "model"


def problem(d: Any) -> str | None:
    """Why a stored investigation cannot be processed (field types), or None."""
    if not isinstance(d, dict):
        return "not an object"
    if not (isinstance(d.get("target"), str) and isinstance(d.get("root"), str)
            and isinstance(d.get("kind", ""), str) and isinstance(d.get("ledger"), dict)):
        return "target, root, kind or ledger has the wrong type"
    specs, labels = d.get("specs"), d.get("labels")
    if not (isinstance(specs, dict) and all(isinstance(k, str) and not probes.validate_spec(v)
                                            for k, v in specs.items())):
        return "a probe spec is malformed"
    if not (isinstance(labels, dict)
            and all(isinstance(k, str) and isinstance(v, str) for k, v in labels.items())):
        return "labels malformed"
    if not all(isinstance(d.get(k, []), list) and all(isinstance(x, str) for x in d.get(k, []))
               for k in ("done", "nonprogress")):
        return "done or nonprogress malformed"
    rounds = d.get("rounds", {})
    if not (isinstance(rounds, dict) and all(
            isinstance(k, str) and isinstance(v, int) and not isinstance(v, bool)
            for k, v in rounds.items())):
        return "rounds malformed"
    interps = d.get("interpretations", [])
    if not (isinstance(interps, list)
            and all(isinstance(i, dict) and isinstance(i.get("text"), str) for i in interps)):
        return "interpretations malformed"
    return None


def _text(v: Any, n: int) -> str:
    if not isinstance(v, str) or not v.strip():
        raise DiscoveryError("a required text field is empty")
    return " ".join(v.split())[:n]


def _check_hyp(h: Any) -> dict[str, Any]:
    if not isinstance(h, dict):
        raise DiscoveryError("hypothesis is not an object")
    for k in ("mechanism", "prediction", "falsification"):
        _text(h.get(k), 300)
    spec = {"probe": h.get("probe"), "args": h.get("args"),
            "supports_if": h.get("supports_if"), "refutes_if": h.get("refutes_if")}
    errs = probes.validate_spec(spec)
    if errs:
        raise DiscoveryError("probe spec refused: " + "; ".join(errs))
    return spec
