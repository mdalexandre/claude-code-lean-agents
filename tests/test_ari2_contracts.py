"""ARI 2.0 holon contracts and the versioned concept graph: ARI2-R18 and Phase 1."""
from __future__ import annotations

import copy
import json
from pathlib import Path

import pytest
from ari import holons

REPO = Path(__file__).resolve().parents[1]
SPECS = REPO / "docs" / "specs"


def test_registry_is_well_formed() -> None:
    assert holons.validate() == []
    assert [h["id"] for h in holons.HOLONS] == ["H0", "H1", "H2", "H3", "H4", "H5"]
    for h in holons.HOLONS:
        for f in holons.FIELDS:
            assert f in h, (h["id"], f)


def test_generated_document_matches_the_code() -> None:
    doc = json.loads((SPECS / "ari-holons-v2.0.0.json").read_text(encoding="utf-8"))
    assert doc == json.loads(json.dumps(holons.document()))


def test_every_named_module_and_test_exists() -> None:
    for h in holons.HOLONS:
        for m in h["modules"]:
            assert (REPO / m).is_file(), m
        for t in h["verification"].split(", "):
            path = t.split(" ")[0]
            if path.startswith("tests/"):
                assert (REPO / path).is_file(), path


@pytest.mark.parametrize("mutate,needle", [
    (lambda hs: hs[3]["authority"].append("publish the report"), "forbidden action publish"),
    (lambda hs: hs[5]["authority"].append("promote_to_vgtp_registry"), "promote_to_vgtp"),
    (lambda hs: hs[2].__setitem__("parent", "H9"), "parent H9 is not a holon"),
    (lambda hs: hs[1].__setitem__("parent", "owner"), "exactly one holon"),
    (lambda hs: hs[4].__setitem__("safe_failure", ""), "H4: missing safe_failure"),
    (lambda hs: hs[2]["consumes"].append("H2"), "consumes unknown or itself H2"),
    (lambda hs: hs[1].__setitem__("id", "H0"), "duplicate holon id"),
    # D7: the alternatives of a combined forbidden word, parent cycles, string authority
    (lambda hs: hs[3]["authority"].append("uninstall a skill"), "uninstall_or_disable"),
    (lambda hs: hs[3]["authority"].append("Disable the hook"), "uninstall_or_disable"),
    (lambda hs: (hs[1].__setitem__("parent", "H2"), hs[2].__setitem__("parent", "H1")),
     "parent cycle"),
    (lambda hs: hs[3].__setitem__("authority", "publish the report"),
     "authority is not a list of strings"),
])
def test_contract_breaches_are_detected(mutate: object, needle: str) -> None:
    hs = copy.deepcopy(holons.HOLONS)
    mutate(hs)  # type: ignore[operator]
    assert any(needle in e for e in holons.validate(hs)), holons.validate(hs)


def test_concept_graph_is_well_formed() -> None:
    g = json.loads((SPECS / "ari-concept-graph-v2.json").read_text(encoding="utf-8"))
    assert g["schema"] == "ari-concept-graph/2"
    nodes = {n["id"]: n for n in g["nodes"]}
    roots = [n for n in g["nodes"] if "." not in n["id"]]
    assert sorted(n["id"] for n in roots) == [f"D{i}" for i in range(1, 9)]
    for n in g["nodes"]:
        if "." in n["id"]:
            assert n["id"].split(".")[0] in nodes
            for k in ("definition", "status"):
                assert n.get(k), (n["id"], k)
    assert set(g["relation_types"]) == {"refines", "causes", "bounds", "detects",
                                        "conflicts_with", "implemented_by", "tested_by"}
    for src, kind, dst in g["edges"]:
        assert kind in g["relation_types"], (src, kind, dst)
        assert src in nodes, src
        assert dst in nodes or dst.startswith(("ARI2-R", "tests/")), dst
    rejected = [n for n in g["nodes"] if n.get("status") == "REJECTED"]
    assert rejected and all(n.get("reason") for n in rejected)
    items = g["backlog"]["items"]
    assert [b["rank"] for b in items] == list(range(1, len(items) + 1))
    assert all(b["node"] in nodes and b["why"] for b in items)
