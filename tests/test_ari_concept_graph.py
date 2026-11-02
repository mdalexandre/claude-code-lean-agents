"""Gate B: every selected capability traces to a measurable problem and a verification."""
from __future__ import annotations

import json
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
GRAPH = json.loads((REPO / "docs" / "specs" / "ari-concept-graph.json").read_text())


def test_graph_is_well_formed_and_bounded() -> None:
    nodes = {n["id"]: n for n in GRAPH["nodes"]}
    assert len(nodes) == len(GRAPH["nodes"])
    assert len(nodes) <= GRAPH["budget"]["max_nodes"]
    assert max(n["level"] for n in nodes.values()) <= GRAPH["budget"]["max_level"]
    for a, rel, b in GRAPH["edges"]:
        assert a in nodes and b in nodes, (a, b)
        assert rel in GRAPH["relation_types"], rel


def test_every_capability_traces_to_problem_measure_and_test() -> None:
    nodes = {n["id"]: n for n in GRAPH["nodes"]}
    edges = GRAPH["edges"]
    for cid, node in nodes.items():
        if node["type"] != "capability":
            continue
        problems = [b for a, rel, b in edges if a == cid and rel == "addresses"]
        assert problems, cid
        assert any(a == p and rel == "measured_by" for p in problems for a, rel, _ in edges), cid
        assert (REPO / node["path"]).exists() and (REPO / node["verified_by"]).exists(), cid
        assert cid in GRAPH["ranking"]["rows"]
