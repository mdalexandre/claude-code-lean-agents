"""The model investigator (H2): open ended hypotheses, never actions.

The governor sends open questions as JSON to a headless Claude Code process started with no
tools, no MCP servers, hooks disabled, no session persistence and a spend cap. The model
can only return proposals that match OUTPUT_SCHEMA: a new hypothesis tested by a catalog
probe with rules fixed in advance, a child question with competing hypotheses, an
UNRESOLVED verdict on a tested hypothesis, or a one sentence interpretation. The governor
validates each proposal again (investigate.Investigation.apply) and runs every probe
itself. Whatever the model writes is data; it is never executed.
"""
from __future__ import annotations

import json
import math
import subprocess
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from . import probes
from .competence import redact

MODEL = "claude-sonnet-5-5"
NO_MCP = json.dumps({"mcpServers": {}})
NO_HOOKS = json.dumps({"disableAllHooks": True})
_RULE = {"type": "object", "properties": {"op": {"enum": sorted(probes.OPS)},
                                          "value": {"type": "number"}},
         "required": ["op", "value"], "additionalProperties": False}
_TXT = {"type": "string", "minLength": 1, "maxLength": 300}
_SPEC = {"probe": {"enum": sorted(probes.CATALOG)}, "args": {"type": "object"},
         "supports_if": _RULE, "refutes_if": _RULE}
_HYP_FIELDS = {"mechanism": _TXT, "prediction": _TXT, "falsification": _TXT, **_SPEC}
_HYP = {"type": "object", "properties": _HYP_FIELDS, "required": sorted(_HYP_FIELDS),
        "additionalProperties": False}


def _variant(kind: str, fields: dict[str, Any]) -> dict[str, Any]:
    props = {"question": {"type": "string"}, "type": {"const": kind}, **fields}
    return {"type": "object", "properties": props, "required": sorted(props),
            "additionalProperties": False}


OUTPUT_SCHEMA: dict[str, Any] = {
    "type": "object", "required": ["proposals"], "additionalProperties": False,
    "properties": {"proposals": {"type": "array", "maxItems": 8, "items": {"anyOf": [
        _variant("hypothesis", _HYP_FIELDS),
        _variant("child_question", {"observation": _TXT, "hypotheses": {
            "type": "array", "minItems": 2, "maxItems": 4, "items": _HYP}}),
        _variant("unresolved", {"hypothesis": {"type": "string"}, "reason": _TXT}),
        _variant("interpretation", {"text": _TXT}),
    ]}}}}

SYSTEM = """You are the investigator of ARI, an operations intelligence that studies how a \
Claude Code setup uses its skills. You receive open questions as JSON. Each question has \
competing hypotheses with the probe that tested them and its outcomes. Your job is \
abduction: explain the evidence, do not classify it.

Return proposals only, as JSON matching the schema:
- hypothesis: a NEW mechanism not already listed, with its prediction, the observation \
that would falsify it, one catalog probe and the supports_if and refutes_if rules you \
commit to before the probe runs.
- child_question: an observation that makes a narrower question necessary, with 2 to 4 \
competing hypotheses, each with mechanism, prediction, falsification, probe and rules.
- unresolved: the id of a tested hypothesis no catalog probe can decide, with the reason.
- interpretation: text of one sentence relating the evidence to its significance and \
its uncertainty, naming the next test. Never claim causality the evidence does not show.

Rules: use only probes from the catalog. Never propose an action, a change or a removal; \
ARI proposes changes elsewhere, through gates you do not control. An ambiguity you cannot \
resolve stays unresolved: never pick a winner. Prefer the cheapest probe that separates \
the rivals. If nothing useful can be added, return an empty proposals list."""


@dataclass
class ModelResult:
    ok: bool
    proposals: list[dict[str, Any]] = field(default_factory=list)
    cost_usd: float = 0.0
    input_tokens: int = 0
    output_tokens: int = 0
    duration_s: float = 0.0
    error: str = ""


def argv(budget_usd: float, model: str = MODEL) -> list[str]:
    """The exact command: no tools, no MCP, no hooks, no session file, a spend cap."""
    return ["claude", "-p", "--output-format", "json", "--tools", "",
            "--strict-mcp-config", "--mcp-config", NO_MCP, "--settings", NO_HOOKS,
            "--no-session-persistence", "--max-budget-usd", f"{budget_usd:.2f}",
            "--model", model, "--json-schema", json.dumps(OUTPUT_SCHEMA),
            "--system-prompt", SYSTEM]


def packet(questions: list[dict[str, Any]]) -> dict[str, Any]:
    return {"schema": "ari-investigator-packet/1", "probe_catalog": probes.describe(),
            "rule_ops": sorted(probes.OPS), "questions": questions}


def _parse(stdout: str) -> tuple[dict[str, Any] | None, dict[str, Any]]:
    try:
        env = json.loads(stdout)
    except ValueError:
        return None, {}
    if not isinstance(env, dict):
        return None, {}
    body = env.get("structured_output")
    if not isinstance(body, dict):
        text = env.get("result")
        if isinstance(text, str):
            t = text.strip().removeprefix("```json").removeprefix("```").removesuffix("```")
            try:
                body = json.loads(t)
            except ValueError:
                body = None
    return (body if isinstance(body, dict) else None), env


def _cost(v: Any, budget_usd: float) -> float:
    """The reported cost; a missing or unreadable one counts as the whole call budget."""
    if isinstance(v, bool) or not isinstance(v, (int, float)):
        return budget_usd
    try:
        f = float(v)
    except OverflowError:
        return budget_usd
    return f if math.isfinite(f) and f >= 0 else budget_usd


MAX_TOKENS = 10 ** 12


def _tokens(v: Any) -> int:
    if isinstance(v, bool) or not isinstance(v, (int, float)):
        return 0
    if isinstance(v, float) and not math.isfinite(v):
        return 0
    return min(int(v), MAX_TOKENS) if v >= 0 else 0


Runner = Callable[[list[str], str, float, Path], tuple[int, str, str]]


def _subprocess(cmd: list[str], stdin: str, timeout: float, cwd: Path) -> tuple[int, str, str]:
    p = subprocess.run(cmd, input=stdin, capture_output=True, text=True, timeout=timeout,
                       cwd=cwd, check=False)
    return p.returncode, p.stdout, p.stderr


def investigate(pkt: dict[str, Any], budget_usd: float, timeout_s: float, cwd: Path,
                runner: Runner | None = None, model: str = MODEL) -> ModelResult:
    run = runner or _subprocess
    cwd.mkdir(parents=True, exist_ok=True)
    t0 = time.monotonic()
    try:
        code, out, err = run(argv(budget_usd, model), json.dumps(pkt), timeout_s, cwd)
    except OSError as exc:  # the call never started: nothing was spent
        return ModelResult(False, error=f"{type(exc).__name__}",
                           duration_s=time.monotonic() - t0)
    except Exception as exc:  # it may have run and was stopped: assume the cap
        return ModelResult(False, error=f"{type(exc).__name__}", cost_usd=budget_usd,
                           duration_s=time.monotonic() - t0)
    dur = time.monotonic() - t0
    body, env = _parse(out)
    u_raw = env.get("usage")
    u: dict[str, Any] = u_raw if isinstance(u_raw, dict) else {}
    res = ModelResult(False, cost_usd=_cost(env.get("total_cost_usd"), budget_usd),
                      input_tokens=_tokens(u.get("input_tokens"))
                      + _tokens(u.get("cache_read_input_tokens"))
                      + _tokens(u.get("cache_creation_input_tokens")),
                      output_tokens=_tokens(u.get("output_tokens")), duration_s=dur)
    if code != 0 or env.get("is_error"):
        res.error = redact(f"exit {code}: {(err or str(env.get('result', '')))[-600:]}")[-200:]
        return res
    if body is None or not isinstance(body.get("proposals"), list):
        res.error = "reply did not match the output schema"
        return res
    res.ok = True
    res.proposals = [p for p in body["proposals"][:8] if isinstance(p, dict)]
    return res
