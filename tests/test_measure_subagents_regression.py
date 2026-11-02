"""Regression checks for the existing measure_subagents.py command line behavior.

Written before any ARI change touched the measurement path. The script itself is left
unchanged; these tests pin its output so later work cannot drift it silently.
"""
from __future__ import annotations

import json
import re
import subprocess
import sys
from pathlib import Path

SCRIPT = Path(__file__).resolve().parent.parent / "scripts" / "measure_subagents.py"


def _run(*args: str) -> str:
    out = subprocess.run([sys.executable, str(SCRIPT), *args], capture_output=True, text=True,
                         check=True)
    return out.stdout


def _tree(root: Path, agent: str, sizes: list[int], meta: bool = True) -> None:
    sub = root / "p" / "s" / "subagents"
    sub.mkdir(parents=True, exist_ok=True)
    for i, n in enumerate(sizes):
        name = f"{agent}-{i}"
        if meta:
            (sub / f"{name}.meta.json").write_text(json.dumps({"agentType": agent}))
        (sub / f"{name}.jsonl").write_text(json.dumps(
            {"type": "assistant", "message": {"usage": {"input_tokens": n}}}) + "\n")


def test_ci_case_output_line(tmp_path: Path) -> None:
    _tree(tmp_path, "lean-worker", [1000, 2000, 3000])
    out = _run("--root", str(tmp_path))
    assert re.search(r"lean-worker +3 +2\.0k +3\.0k", out), out
    assert out.splitlines()[0].split() == ["agent", "type", "runs", "median", "start", "p90",
                                           "start"]


def test_sorted_by_median_descending_and_min_runs(tmp_path: Path) -> None:
    _tree(tmp_path, "lean-worker", [1000, 1000, 1000])
    _tree(tmp_path, "general-purpose", [90000, 100000, 110000])
    _tree(tmp_path, "Explore", [5000, 5000])
    out = _run("--root", str(tmp_path)).splitlines()
    assert out[1].startswith("general-purpose") and out[2].startswith("lean-worker")
    assert not any(line.startswith("Explore") for line in out)
    out2 = _run("--root", str(tmp_path), "--min-runs", "2")
    assert "Explore" in out2


def test_cache_fields_count_toward_start(tmp_path: Path) -> None:
    sub = tmp_path / "p" / "s" / "subagents"
    sub.mkdir(parents=True)
    for i in range(3):
        (sub / f"a{i}.meta.json").write_text(json.dumps({"agentType": "lean-checker"}))
        (sub / f"a{i}.jsonl").write_text(json.dumps({"type": "assistant", "message": {"usage": {
            "input_tokens": 1, "cache_creation_input_tokens": 4999,
            "cache_read_input_tokens": 5000}}}) + "\n")
    assert re.search(r"lean-checker +3 +10\.0k", _run("--root", str(tmp_path)))


def test_empty_root_message(tmp_path: Path) -> None:
    out = _run("--root", str(tmp_path))
    assert out.strip() == "No agent type with 3+ runs in the last 30 days."
