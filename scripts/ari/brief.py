"""The morning brief (H4, ARI-10): what changed, what ARI did, what needs the owner.

At most BOUNDS["max_decisions"] ordinary decisions, ranked by estimated benefit within each
benefit unit. Units are never compared: they take turns in a fixed (alphabetical) order, the
best of each unit first, and decisions left out are counted. Critical
incidents (SAFE state, a suspended model layer, a failed tool) are always shown and never
count against that limit. Numbers carry their labels: estimates say so, and nothing here
claims a change caused an outcome.
"""
from __future__ import annotations

from typing import Any

from .policy import BOUNDS


def decisions(cands: dict[str, dict[str, Any]]) -> list[dict[str, Any]]:
    """Benefits in different units are never compared: units take turns in alphabetical
    order, each giving its best remaining candidate."""
    by_unit: dict[str, list[dict[str, Any]]] = {}
    for c in cands.values():
        if c["state"] == "APPROVAL_PENDING":
            by_unit.setdefault(str(c.get("benefit_unit", "")), []).append(c)
    queues = [sorted(v, key=lambda c: (-float(c.get("benefit", 0)), c["id"]))
              for _u, v in sorted(by_unit.items())]
    out: list[dict[str, Any]] = []
    while len(out) < int(BOUNDS["max_decisions"]) and any(queues):
        for q in queues:
            if q and len(out) < int(BOUNDS["max_decisions"]):
                out.append(q.pop(0))
    return out


def render(gov: dict[str, Any], tick: dict[str, Any], cands: dict[str, dict[str, Any]],
           findings: dict[str, int], interpretations: list[str],
           self_report: dict[str, Any]) -> tuple[str, dict[str, Any]]:
    incidents = list(gov.get("incidents", []))
    if gov.get("mode") == "SAFE":
        incidents.insert(0, "SAFE state: " + "; ".join(gov.get("safe_reasons", [])))
    if gov.get("model_suspended"):
        incidents.append("model layer suspended: " + str(gov["model_suspended"]))
    dec = decisions(cands)
    states: dict[str, int] = {}
    for c in cands.values():
        states[c["state"]] = states.get(c["state"], 0) + 1
    lines = [f"# ARI brief {tick.get('at', '')[:16]}", ""]
    if incidents:
        lines += ["**Incidents**", *[f"- {i}" for i in incidents], ""]
    lines += ["**What changed**",
              f"- evidence digest {'changed' if tick.get('changed') else 'unchanged'}; "
              f"{tick.get('targets_total', 0)} skills listed but never seen used"]
    if tick.get("burn_line"):
        lines.append(f"- {tick['burn_line']}")
    lines += ["", "**What ARI did**",
              f"- opened {tick.get('opened', 0)} investigations, ran probes on "
              f"{tick.get('probed', 0)}, {tick.get('status_changes', 0)} evidence state changes",
              f"- model rounds {tick.get('model_rounds', 0)} "
              f"({tick.get('model_cost_usd', 0):.2f} USD); "
              f"{tick.get('model_note', 'no model call')}",
              "- settled so far: " + (", ".join(f"{k} {v}" for k, v in sorted(findings.items()))
                                      or "nothing yet"),
              "- candidates: " + (", ".join(f"{k} {v}" for k, v in sorted(states.items()))
                                  or "none")]
    for t in interpretations[-3:]:
        lines.append(f"- model reading (unverified text): {t[:300]}")
    lines += ["", "**Needs you**"]
    waiting = sorted(c["id"] for c in cands.values() if c["state"] == "APPROVED")
    pending = sum(1 for c in cands.values() if c["state"] == "APPROVAL_PENDING")
    if not dec and not waiting:
        lines.append("- nothing")
    if waiting:
        lines.append(f"- approved, waiting for you to apply: {', '.join(waiting)} (then "
                     "`python3 scripts/ari_operator.py applied <id>`)")
    for c in dec:
        v = c["verify"]
        lines.append(f"- {c['id']} {c['kind']} on {c['target']}: {c['proposal']} "
                     f"(estimated benefit {c['benefit']:g} {c.get('benefit_unit', '')}; "
                     f"verified after "
                     f"{v['min_days']} days by {v['spec']['probe']}). Approve: "
                     f"`python3 scripts/ari_operator.py approve {c['id']}`")
    if pending > len(dec):
        lines.append(f"- {pending - len(dec)} more pending decisions: "
                     "`python3 scripts/ari_operator.py status`")
    lines += ["", f"_ARI itself, last 7 days: {self_report.get('model_runs_7d', 0)} model "
                  f"runs, {self_report.get('model_cost_usd_7d', 0):.2f} USD, "
                  f"{self_report.get('status_changes_7d', 0)} evidence state changes._"]
    doc = {"schema": "ari-brief/1", "at": tick.get("at"), "incidents": incidents,
           "decisions": [{"id": c["id"], "kind": c["kind"], "target": c["target"],
                          "proposal": c["proposal"], "benefit": c["benefit"],
                          "benefit_unit": c.get("benefit_unit", "")} for c in dec],
           "findings": findings, "candidate_states": states, "tick": tick,
           "self": self_report}
    return "\n".join(lines) + "\n", doc
