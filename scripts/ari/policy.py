"""ARI 3.0 authority policy (holon H0): bounds, forbidden actions, protected files.

The policy is code, not a prompt. The governor reads it; nothing the investigator returns
can change it. The protected files (every ARI module, this policy included, the operator's
command line and the gate tests) are pinned by sha256 in docs/specs/ari-protected-v3.json.
When a protected file differs from its pin, the governor enters the SAFE state (no model
call and no candidate transition) and the owner commands approve, applied and resume refuse.
Re-pinning is a development step done by a person at a terminal (ari_operator.py pin).

Limit: the pin lives in the same repository, so someone who can edit both a gate and the
pin can change the gate. The investigator cannot: it runs with no tools at all. The pin
turns a silent gate edit into a visible, refused state change.
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

from .holons import FORBIDDEN as V2_FORBIDDEN

VERSION = "3.0.0"
PIN_SCHEMA = "ari-protected/1"
PIN_PATH = "docs/specs/ari-protected-v3.json"

BOUNDS: dict[str, float] = {
    "max_depth": 3,                 # question depth below the root observation
    "max_per_node": 4,              # hypotheses per question
    "max_nodes": 24,                # questions per investigation
    "max_targets_per_tick": 60,     # new investigations opened per tick
    "max_model_questions": 6,       # open questions sent to the model per round
    "max_model_rounds": 3,          # model calls per tick
    "max_proposals": 8,             # proposals accepted from one model reply
    "nonprogress_rounds": 2,        # model rounds on a question with no status change
    "tick_budget_usd": 1.0,         # model spend per tick
    "rolling_budget_usd": 5.0,      # model spend per rolling 7 days
    "rolling_days": 7,
    "max_model_failures": 3,        # consecutive failed model calls before suspension
    "model_timeout_s": 300,
    "max_decisions": 3,             # ordinary decisions shown in the brief
}

# ARI 2.0's forbidden actions plus the ones an operator could be tempted into.
FORBIDDEN = tuple(V2_FORBIDDEN) + ("apply_change", "edit_skills", "install_skill",
                                   "edit_policy", "approve_on_behalf")

PROTECTED = (
    "scripts/ari/policy.py",
    "scripts/ari/governor.py",
    "scripts/ari/candidates.py",
    "scripts/ari/regulator.py",
    "scripts/ari/investigator.py",
    "scripts/ari/investigate.py",  # validates every model proposal
    "scripts/ari/probes.py",  # the probe catalog and the judge
    "scripts/ari/learn.py",
    "scripts/ari/discovery.py",
    "scripts/ari/holons.py",
    # every other module the operator imports, and the operator's own command line
    "scripts/ari/__init__.py",
    "scripts/ari/abduction.py",
    "scripts/ari/audit.py",
    "scripts/ari/brief.py",
    "scripts/ari/cli_common.py",
    "scripts/ari/competence.py",
    "scripts/ari/evidence.py",
    "scripts/ari/forecast.py",
    "scripts/ari/interpret.py",
    "scripts/ari/observer.py",
    "scripts/ari/report.py",
    "scripts/ari_operator.py",
    # the programs every tick executes (the observer runs them as subprocesses)
    "scripts/skill_mcp_audit.py",
    "scripts/weekly_burn.py",
    "scripts/ari_nightly.py",
    "tests/test_ari3_gates.py",
)


def repo_root() -> Path:
    return Path(__file__).resolve().parents[2]


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def pin_document(root: Path | None = None) -> dict[str, Any]:
    base = root or repo_root()
    return {"schema": PIN_SCHEMA, "version": VERSION,
            "files": {f: _sha(base / f) for f in PROTECTED}}


def write_pin(root: Path | None = None) -> Path:
    base = root or repo_root()
    p = base / PIN_PATH
    p.write_text(json.dumps(pin_document(base), indent=1, sort_keys=True) + "\n",
                 encoding="utf-8")
    return p


def integrity(root: Path | None = None) -> list[str]:
    """Problems with the protected files; empty means every gate matches its pin."""
    base = root or repo_root()
    pin = base / PIN_PATH
    try:
        doc = json.loads(pin.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        return [f"pin unreadable: {type(exc).__name__}"]
    files = doc.get("files") if isinstance(doc, dict) else None
    if not isinstance(files, dict) or doc.get("schema") != PIN_SCHEMA:
        return ["pin is not " + PIN_SCHEMA]
    problems = []
    for f in PROTECTED:
        if f not in files:
            problems.append(f"{f}: not pinned")
            continue
        path = base / f
        if not path.is_file():
            problems.append(f"{f}: missing")
        elif _sha(path) != files[f]:
            problems.append(f"{f}: changed since it was pinned")
    return problems
