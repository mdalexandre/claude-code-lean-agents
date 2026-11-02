"""Candidate interventions and procedures, and their gated lifecycle (H3, ARI-05, ARI-09).

    CANDIDATE -> REPLAYED -> QUALIFIED -> APPROVAL_PENDING -> APPROVED -> CANARY
              -> VERIFIED | ROLLED_BACK            (REJECTED from any open state)

The governor moves a candidate up to APPROVAL_PENDING, one checked step per transition:
REPLAYED needs a passed replay on evidence the candidate was not built from, QUALIFIED
needs its quality gates. APPROVED and CANARY are owner transitions: they need an approval
bound to the candidate's content hash, given at a terminal by typing a one time code that
is shown only on that terminal (ari_operator.py approve / applied). A headless process,
the model investigator included, has no terminal. ARI never applies a candidate: CANARY
starts when the owner records that they applied it, and VERIFIED or ROLLED_BACK is decided
by the verification rule fixed when the candidate was created.
"""
from __future__ import annotations

import hashlib
import json
import math
import os
import secrets
import tempfile
from datetime import datetime
from pathlib import Path
from typing import Any, Callable

from . import probes

SCHEMA = "ari-candidates/1"
STATES = ("CANDIDATE", "REPLAYED", "QUALIFIED", "APPROVAL_PENDING", "APPROVED", "CANARY",
          "VERIFIED", "ROLLED_BACK", "REJECTED")
NEXT = {"CANDIDATE": ("REPLAYED", "REJECTED"), "REPLAYED": ("QUALIFIED", "REJECTED"),
        "QUALIFIED": ("APPROVAL_PENDING", "REJECTED"),
        "APPROVAL_PENDING": ("APPROVED", "REJECTED"), "APPROVED": ("CANARY", "REJECTED"),
        "CANARY": ("VERIFIED", "ROLLED_BACK")}
OWNER_ONLY = ("APPROVED", "CANARY")
KINDS = ("restrict_exposure", "routing_correction", "consolidate", "telemetry_defect",
         "competence_routing", "competence_revision", "competence_new_skill",
         "policy_revision")
OWNER = "owner@terminal"
MAX_MIN_DAYS = 3650


class CandidateError(Exception):
    pass


def content_hash(c: dict[str, Any]) -> str:
    core = {k: c[k] for k in ("kind", "target", "proposal", "verify")}
    return hashlib.sha256(json.dumps(core, sort_keys=True).encode()).hexdigest()


def make(kind: str, target: str, proposal: str, verify: dict[str, Any],
         evidence: dict[str, Any], benefit: float, now: datetime) -> dict[str, Any]:
    if kind not in KINDS:
        raise CandidateError(f"unknown kind {kind}")
    if not (isinstance(verify, dict) and verify.get("spec") and verify.get("min_days")):
        raise CandidateError("a candidate needs a verification rule fixed in advance")
    cid = "C-" + hashlib.sha256(f"{kind}|{target}".encode()).hexdigest()[:10]
    c = {"id": cid, "kind": kind, "target": target, "proposal": proposal, "verify": verify,
         "evidence": evidence, "benefit": benefit, "state": "CANDIDATE",
         "created": now.isoformat(), "history": [], "approval": None, "canary": None}
    c["content_hash"] = content_hash(c)
    return c


def intact(c: dict[str, Any]) -> bool:
    """The content matches its stored hash and, once approved, the hash the owner approved.
    The stored hash alone proves nothing: an edit can rewrite it too."""
    h = content_hash(c)
    if h != c.get("content_hash"):
        return False
    approval = c.get("approval")
    return not approval or (isinstance(approval, dict) and approval.get("content_hash") == h)


def transition(c: dict[str, Any], to: str, actor: str, note: str, now: datetime,
               approval: dict[str, Any] | None = None) -> dict[str, Any]:
    cur = c["state"]
    if to not in NEXT.get(cur, ()):
        raise CandidateError(f"{c['id']}: {cur} cannot move to {to}")
    if to != "REJECTED" and not intact(c):
        raise CandidateError(f"{c['id']}: content changed after creation or approval")
    if to in OWNER_ONLY:
        if actor != OWNER:
            raise CandidateError(f"{c['id']}: {to} is an owner transition")
        if not (isinstance(approval, dict) and approval.get("content_hash") == c["content_hash"]
                and approval.get("candidate") == c["id"] and approval.get("terminal")):
            raise CandidateError(f"{c['id']}: {to} needs a terminal approval for this content")
    if to == "CANARY" and not c.get("approval"):
        raise CandidateError(f"{c['id']}: CANARY needs the earlier approval")
    c["history"].append({"from": cur, "to": to, "actor": actor, "note": note,
                         "at": now.isoformat()})
    c["state"] = to
    if to == "APPROVED":
        c["approval"] = approval
    return c


# the terminal gate ---------------------------------------------------------------------
Terminal = Callable[[str, str], bool]


def real_terminal(prompt: str, code: str) -> bool:
    """Show a one time code on the controlling terminal and read it back from there.
    A process without a controlling terminal (cron, systemd, claude -p, an agent's shell
    tool) cannot open /dev/tty and is refused."""
    try:
        fd = os.open("/dev/tty", os.O_RDWR | os.O_NOCTTY)
    except OSError:
        return False
    try:  # raw reads and writes: a tty is not seekable, so a text "r+" wrapper fails
        os.write(fd, f"{prompt}\nType this code to confirm: {code}\n> ".encode())
        buf = b""
        while b"\n" not in buf and len(buf) < 256:
            chunk = os.read(fd, 256)
            if not chunk:
                break
            buf += chunk
    except OSError:
        return False
    finally:
        os.close(fd)
    answer = buf.split(b"\n", 1)[0].decode("utf-8", "replace").strip()
    return secrets.compare_digest(answer.encode(), code.encode())


def _printable(text: str) -> str:
    """Text for the terminal: control characters (escape sequences, bells) are shown as ?."""
    return "".join(ch if ch == "\n" or ch.isprintable() else "?" for ch in text)


def owner_approve(c: dict[str, Any], to: str, now: datetime, terminal: Terminal,
                  note: str = "") -> dict[str, Any]:
    if to not in OWNER_ONLY:
        raise CandidateError(f"{to} is not an owner transition")
    code = secrets.token_hex(3)
    v = c["verify"]
    rule = (f"verified after {v['min_days']} days by {v['spec'].get('probe')} "
            f"{json.dumps(v['spec'].get('args'), sort_keys=True)}: supports "
            f"{json.dumps(v['spec'].get('supports_if'))}, refutes "
            f"{json.dumps(v['spec'].get('refutes_if'))}")
    summary = _printable(f"{to} {c['id']} ({c['kind']}) on {c['target']}\n  {c['proposal']}\n"
                         f"  {rule}\n  content {c['content_hash'][:16]}")
    if not terminal(summary, code):
        raise CandidateError("not confirmed at a terminal")
    approval = {"candidate": c["id"], "content_hash": c["content_hash"], "terminal": True,
                "transition": to, "at": now.isoformat()}
    transition(c, to, OWNER, note or f"confirmed at a terminal for {to}", now, approval)
    if to == "CANARY":
        c["canary"] = {"started": now.isoformat()}
    return c


# store ---------------------------------------------------------------------------------
def problem(c: Any) -> str | None:
    """Why a stored candidate cannot be processed, or None when it is well formed."""
    if not isinstance(c, dict):
        return "not an object"
    for k in ("id", "kind", "target", "proposal", "content_hash", "created", "state"):
        if not isinstance(c.get(k), str):
            return f"{k} is not text"
    if c["kind"] not in KINDS or c["state"] not in STATES:
        return "unknown kind or state"
    v = c.get("verify")
    days = v.get("min_days") if isinstance(v, dict) else None
    if not (isinstance(v, dict) and isinstance(v.get("spec"), dict)
            and isinstance(days, int) and not isinstance(days, bool)
            and 0 <= days <= MAX_MIN_DAYS):
        return "verify rule malformed"
    sp = v["spec"]
    if sp.get("probe") == "policy_outcomes":
        ok = isinstance(sp.get("args"), dict) and isinstance(sp["args"].get("kind"), str) and all(
            isinstance(sp.get(k), dict) and sp[k].get("op") in probes.OPS
            and isinstance(sp[k].get("value"), (int, float))
            and not isinstance(sp[k].get("value"), bool) for k in ("supports_if", "refutes_if"))
        if not ok:
            return "verify probe spec malformed"
    elif probes.validate_spec(sp):
        return "verify probe spec malformed"
    b = c.get("benefit")
    try:
        finite = (not isinstance(b, bool) and isinstance(b, (int, float))
                  and math.isfinite(float(b)))
    except OverflowError:
        finite = False
    if not finite:
        return "benefit is not a finite number"
    if not isinstance(c.get("history"), list):
        return "history is not a list"
    if not isinstance(c.get("checks", []), list):
        return "checks is not a list"
    for k in ("approval", "canary"):
        if c.get(k) is not None and not isinstance(c[k], dict):
            return f"{k} is not an object"
    if c["state"] in OWNER_ONLY:
        a = c.get("approval") or {}
        if not (a.get("candidate") == c["id"] and isinstance(a.get("content_hash"), str)
                and a.get("terminal") is True and a.get("transition") in OWNER_ONLY):
            return "an owner state without an owner approval"
    if c["state"] == "CANARY":
        try:
            started = datetime.fromisoformat(str((c.get("canary") or {})["started"]))
        except (KeyError, ValueError):
            return "canary start unreadable"
        if started.tzinfo is None:
            return "canary start has no time zone"
    return None


def load(state_dir: Path) -> dict[str, dict[str, Any]]:
    p = state_dir / "candidates.json"
    if not p.exists():
        return {}
    data = json.loads(p.read_text(encoding="utf-8"))
    if not isinstance(data, dict) or data.get("schema") != SCHEMA:
        raise CandidateError("candidates.json is not " + SCHEMA)
    return dict(data["candidates"])


def save(state_dir: Path, cands: dict[str, dict[str, Any]]) -> None:
    atomic_write(state_dir / "candidates.json", {"schema": SCHEMA, "candidates": cands})


def atomic_write(path: Path, obj: Any) -> None:
    fd, tmp = tempfile.mkstemp(prefix=path.name + ".", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(obj, fh, indent=1, sort_keys=True, default=str, allow_nan=False)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, path)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise
