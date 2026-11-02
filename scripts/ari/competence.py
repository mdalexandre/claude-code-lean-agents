"""Missing operational competence (H3, R3): recurring human corrections.

A correction is a user prompt that follows an assistant turn and opens with a rejection
("no", "that's wrong", "I said", "you forgot", "stop", "instead"...). Corrections with a
shared vocabulary (word overlap) are grouped; a group seen in 3 or more sessions is a
pattern. Its signature (words most members share) is built from the older sessions only
and tested against the newest session, which it did not see: the holdout replay. A
pattern then passes the skill creation gate:

    an existing skill describes the work and was used    -> competence_revision
    an existing skill describes it but was never used    -> competence_routing
    no skill describes it                                -> competence_new_skill

This learns which corrections recur and where a procedure is missing. It does not learn
psychology and does not turn one correction into a rule: one session is never a pattern.
Excerpts are capped at 200 characters and dropped when they look like a secret.
"""
from __future__ import annotations

import hashlib
import re
from typing import Any

from .probes import ProbeContext, Prompt, jaccard, terms

CORRECTION = re.compile(
    r"^\s*(no\b|nope\b|wrong\b|that'?s (not|wrong)|not what|i said\b|i asked\b|"
    r"you (didn'?t|forgot|missed|ignored)|stop\b|don'?t\b|do not\b|why did you\b|"
    r"again\b|instead\b|actually\b)", re.IGNORECASE)
SECRETISH = re.compile(
    r"(sk-[A-Za-z0-9_-]{8,}|(?:sk|rk|pk)_(?:live|test)_[A-Za-z0-9]{6,}|A[SK]IA[0-9A-Z]{16}|"
    r"[A-Za-z0-9+/_-]{32,}|\b[0-9a-f]{20,}\b|[a-z][a-z0-9+.-]*://[^/\s:@]+:[^/\s@]+@|"
    r"\bbasic\s+[A-Za-z0-9+/=]{8,}|\b(?:pwd|pw|pass)\s*[:=]|password|passwd|passphrase|"
    r"token|secret|bearer|authorization|api[ _-]?key|credential|private[ _-]?key)",
    re.IGNORECASE)
MIN_SESSIONS = 3
GROUP_JACCARD = 0.35
HOLDOUT_COVER = 0.5


def corrections(prompts: list[Prompt]) -> list[Prompt]:
    return [p for p in prompts if p.after_assistant and CORRECTION.match(p.text)
            and len(p.terms) >= 2]


def group(cs: list[Prompt]) -> list[list[Prompt]]:
    groups: list[tuple[set[str], list[Prompt]]] = []
    for p in sorted(cs, key=lambda x: (x.ts, x.session)):
        for vocab, members in groups:
            if jaccard(frozenset(vocab), p.terms) >= GROUP_JACCARD:
                members.append(p)
                vocab.update(p.terms)
                break
        else:
            groups.append((set(p.terms), [p]))
    return [m for _v, m in groups]


def signature(members: list[Prompt]) -> list[str]:
    counts: dict[str, int] = {}
    for p in members:
        for t in p.terms:
            counts[t] = counts.get(t, 0) + 1
    need = max(2, (len(members) + 1) // 2)
    return sorted(t for t, n in counts.items() if n >= need)


def cover(sig: list[str], p: Prompt) -> float:
    return len(p.terms.intersection(sig)) / len(sig) if sig else 0.0


VALUE_AFTER_KEY = re.compile(
    r"(?i)\b(password|passwd|passphrase|pwd|pw|pass|token|secret|api[_ -]?key|"
    r"credentials?|authorization)\b\s*[:=]\s*(?:(?:bearer|basic)\s+)?\S+")
VALUE_AFTER_SCHEME = re.compile(r"(?i)\b(bearer|basic|token)\s+\S+")
URL_USERINFO = re.compile(r"://[^/\s@]*@|://[^\s@/:]*:[^\s@]*@")


def redact(text: str) -> str:
    """Text with every secret shaped run, and the value after a secret keyword, replaced,
    for error messages shown to people."""
    text = VALUE_AFTER_KEY.sub(lambda m: m.group(1) + "=[redacted]", text)
    text = VALUE_AFTER_SCHEME.sub(lambda m: m.group(1) + " [redacted]", text)
    text = URL_USERINFO.sub("://[redacted]@", text)
    return SECRETISH.sub("[redacted]", text)


EXCERPT_ALSO = re.compile(r"://[^/\s@]*@|://[^\s@/:]*:[^\s@]*@|\bcookie\s*:|\bsession\s*=",
                          re.IGNORECASE)


def excerpt(text: str) -> str:
    t = " ".join(text.split())[:200]
    return "" if SECRETISH.search(t) or EXCERPT_ALSO.search(t) else t


def patterns(ctx: ProbeContext) -> list[dict[str, Any]]:
    out = []
    for members in group(corrections(ctx.prompts)):
        sessions = sorted({p.session for p in members},
                          key=lambda s: max(p.ts for p in members if p.session == s))
        if len(sessions) < MIN_SESSIONS:
            continue
        holdout_s = sessions[-1]
        seen = [p for p in members if p.session != holdout_s]
        held = [p for p in members if p.session == holdout_s]
        sig = signature(seen)
        if len(sig) < 2:
            continue
        holdout_pass = any(cover(sig, p) >= HOLDOUT_COVER for p in held)
        pid = "P-" + hashlib.sha256("|".join(sig).encode()).hexdigest()[:10]
        out.append({"id": pid, "signature": sig, "sessions": len(sessions),
                    "corrections": len(members), "holdout_session_pass": holdout_pass,
                    "excerpt": excerpt(seen[0].text), "gate": gate(ctx, sig)})
    return sorted(out, key=lambda p: (-p["sessions"], p["id"]))


def gate(ctx: ProbeContext, sig: list[str]) -> dict[str, Any]:
    """Skill creation gate: routing, revision, or a new skill."""
    best, best_cover = "", 0.0
    for name, desc in sorted(ctx.descriptions.items()):
        d = terms(desc)
        c = len(d.intersection(sig)) / len(sig) if sig else 0.0
        if c > best_cover:
            best, best_cover = name, c
    if best_cover >= 0.5:
        used = ctx.raw_skill.get(best, 0) > 0
        return {"kind": "competence_revision" if used else "competence_routing",
                "skill": best, "cover": round(best_cover, 2)}
    return {"kind": "competence_new_skill", "skill": "", "cover": round(best_cover, 2)}
