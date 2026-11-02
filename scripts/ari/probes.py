"""Closed catalog of deterministic, read only probes for ARI 3.0 investigations (H2).

A hypothesis names one probe, its arguments and two decision rules written before the
probe runs (supports_if, refutes_if). The governor runs the probe and judges the value by
those rules, so neither the deterministic investigator nor the model can grade its own
evidence after seeing it. A value matching neither rule is inconclusive.

One pass over the transcript files of the window (by line timestamp) collects what every
built in probe needs: raw skill invocation counts, user prompts (term sets, with whether
they followed an assistant turn) and counts for any literal terms asked for. Reads only.
"""
from __future__ import annotations

import math
import os
import re
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Iterable

from .evidence import _UNPARSED, MAX_LINE_BYTES, loads_bounded

OPS = {">=": lambda a, b: a >= b, "<=": lambda a, b: a <= b, ">": lambda a, b: a > b,
       "<": lambda a, b: a < b, "==": lambda a, b: a == b}
STOP = frozenset("""a an and are as at be but by can do does for from has have how i if in
into is it its me my no not of on or our so that the their them then there these this to
up use used using was we what when where which who why will with you your yes all any
also just like make more need now one only out please should some than very want get
see sure ok okay let lets dont did does done""".split())
_TS = re.compile(rb'"timestamp"\s*:\s*"([^"]{10,40})"')
_SKILL = re.compile(rb'(?<!\\)"skill"\s*:\s*"([A-Za-z0-9][A-Za-z0-9:_.-]{0,127})"')
_SLASH = re.compile(rb"<command-name>/?([A-Za-z0-9][A-Za-z0-9:_.-]{0,127})</command-name>")
_ASSISTANT = re.compile(rb'(?<!\\)"type"\s*:\s*"assistant"')  # nested blocks never say assistant
_WORD = re.compile(r"[a-z][a-z0-9-]{2,}")
MAX_PROMPTS = 200_000
EVERYDAY_SHARE = 0.02


def terms(text: str) -> frozenset[str]:
    return frozenset(w for w in _WORD.findall(text.lower()) if w not in STOP)


def _ts(raw: bytes) -> datetime | None:
    m = _TS.search(raw)
    if not m:
        return None
    try:
        text = m.group(1).decode()
        return datetime.fromisoformat(text[:-1] + "+00:00" if text.endswith("Z") else text)
    except (ValueError, UnicodeDecodeError, OverflowError):
        return None


@dataclass
class Prompt:
    session: str
    ts: str
    text: str
    terms: frozenset[str]
    after_assistant: bool


@dataclass
class ProbeContext:
    files: list[Path] = field(default_factory=list)
    since: datetime | None = None
    until: datetime | None = None
    raw_skill: Counter[str] = field(default_factory=Counter)
    prompts: list[Prompt] = field(default_factory=list)
    term_counts: dict[str, int] = field(default_factory=dict)
    descriptions: dict[str, str] = field(default_factory=dict)
    skill_dirs: dict[str, Path] = field(default_factory=dict)
    reference_files: list[Path] = field(default_factory=list)
    capabilities: dict[str, dict[str, Any]] = field(default_factory=dict)
    audit_ok: bool = False  # parsed counts exist only when the audit tool produced them
    lines_read: int = 0
    _df: Counter[str] | None = None
    _pdf: tuple[int, Counter[str]] | None = None

    # construction --------------------------------------------------------------------
    @classmethod
    def build(cls, roots: Iterable[str | os.PathLike[str]], since: datetime | None,
              until: datetime | None, skills_roots: Iterable[str | os.PathLike[str]] = (),
              reference_files: Iterable[str | os.PathLike[str]] = (),
              audit: dict[str, Any] | None = None) -> ProbeContext:
        ctx = cls(since=since, until=until)
        for root in roots:
            base = Path(root).expanduser()
            for dirpath, _dirs, names in os.walk(base, followlinks=False):
                for n in names:
                    if not n.endswith(".jsonl"):
                        continue
                    p = Path(dirpath) / n
                    try:
                        if since and p.stat().st_mtime < since.timestamp():
                            continue  # not written since the window opened
                    except OSError:
                        continue
                    ctx.files.append(p)
        ctx.files.sort()
        for sroot in skills_roots:
            base = Path(sroot).expanduser()
            if not base.is_dir():
                continue
            for skill_md in sorted(base.glob("*/SKILL.md")):
                name, desc = read_skill(skill_md)
                ctx.descriptions.setdefault(name, desc)
                ctx.skill_dirs.setdefault(name, skill_md.parent)
                ctx.reference_files.append(skill_md)
        ctx.reference_files += [Path(f).expanduser() for f in reference_files]
        ctx.audit_ok = isinstance(audit, dict) and isinstance(audit.get("capabilities"), list)
        for c in (audit or {}).get("capabilities", []):
            if isinstance(c, dict) and c.get("category") == "skill":
                ctx.capabilities[str(c.get("name"))] = c
        ctx._scan(())
        return ctx

    def _in_window(self, raw: bytes) -> bool:
        t = _ts(raw)
        if t is None or t.tzinfo is None:
            return False
        return not ((self.since and t < self.since) or (self.until and t >= self.until))

    def _scan(self, extra_terms: tuple[str, ...]) -> None:
        """One pass over the window; the first pass also fills skills and prompts."""
        first = not self.lines_read
        needles = [(t, t.encode()) for t in extra_terms]
        counts = dict.fromkeys(extra_terms, 0)
        for p in self.files:
            session = p.stem
            last_type = ""
            try:
                fh = open(p, "rb")
            except OSError:
                continue
            with fh:
                for raw in fh:
                    if len(raw) > MAX_LINE_BYTES or not self._in_window(raw):
                        continue
                    if first:
                        self.lines_read += 1
                    for t, b in needles:
                        if b in raw:
                            counts[t] += 1
                    if not first:
                        continue
                    if b'"tool_use"' in raw and b'"Skill"' in raw:
                        for m in _SKILL.findall(raw):
                            self.raw_skill[m.decode()] += 1
                    if b"command-name" in raw:
                        for m in _SLASH.findall(raw):
                            self.raw_skill[m.decode()] += 1
                    if b'"assistant"' in raw and _ASSISTANT.search(raw):
                        last_type = "assistant"
                        continue
                    if b'"user"' not in raw or b"tool_result" in raw:
                        continue
                    rec = loads_bounded(raw)
                    if rec is _UNPARSED or not isinstance(rec, dict) or rec.get("type") != "user":
                        continue
                    text = _prompt_text(rec)
                    if text and len(self.prompts) < MAX_PROMPTS:
                        self.prompts.append(Prompt(session, str(rec.get("timestamp", "")),
                                                   text[:2000], terms(text),
                                                   last_type == "assistant"))
                    last_type = "user"
        self.term_counts.update(counts)

    def scan_terms(self, wanted: Iterable[str]) -> None:
        todo = tuple(sorted({t for t in wanted if t not in self.term_counts}))
        if todo:
            self._scan(todo)

    # derived -------------------------------------------------------------------------
    def distinctive(self, skill: str, k: int = 5) -> list[str]:
        df = self._df
        if df is None:
            df = Counter()
            for d in self.descriptions.values():
                df.update(t for t in terms(d) if len(t) >= 4)  # identifiers filtered below
            self._df = df
        common = self.everyday_words()
        own = sorted((t for t in terms(self.descriptions.get(skill, ""))
                      if len(t) >= 4 and not any(ch.isdigit() for ch in t)
                      and t not in common),
                     key=lambda t: (df[t], t))
        return own[:k]

    def everyday_words(self) -> frozenset[str]:
        """Words in more than EVERYDAY_SHARE of prompts (and at least 4): ordinary vocabulary
        of this person, which cannot single out one skill's demand."""
        n = len(self.prompts)
        if self._pdf is None or self._pdf[0] != n:
            pdf: Counter[str] = Counter()
            for p in self.prompts:
                pdf.update(p.terms)
            self._pdf = (n, pdf)
        limit = max(3, EVERYDAY_SHARE * n)
        return frozenset(t for t, c in self._pdf[1].items() if c > limit)


def _prompt_text(rec: dict[str, Any]) -> str:
    if (rec.get("isMeta") or rec.get("isSidechain") or rec.get("isCompactSummary")
            or rec.get("isVisibleInTranscriptOnly")):
        return ""  # written by Claude Code (summaries, meta), not typed by a person
    msg = rec.get("message")
    content = msg.get("content") if isinstance(msg, dict) else None
    if isinstance(content, list):
        content = " ".join(b.get("text", "") for b in content
                           if isinstance(b, dict) and b.get("type") == "text"
                           and isinstance(b.get("text"), str))
    if not isinstance(content, str):
        return ""
    text = content.strip()
    if not text or text.startswith(("<", "Caveat:", "[Request interrupted")):
        return ""  # command wrappers, system reminders, interruptions
    return text


def read_skill(skill_md: Path) -> tuple[str, str]:
    """(name, description) from SKILL.md frontmatter; the directory name as a fallback."""
    try:
        text = skill_md.read_text(encoding="utf-8", errors="replace")[:20000]
    except OSError:
        return skill_md.parent.name, ""
    name, desc = skill_md.parent.name, ""
    if text.startswith("---"):
        head = text[3:].split("\n---", 1)[0].splitlines()
        for i, line in enumerate(head):
            if line.startswith("name:"):
                name = line[5:].strip().strip("'\"") or name
            elif line.startswith("description:"):
                v = line[12:].strip()
                if v in ("|", ">", "|-", ">-"):
                    v = " ".join(x.strip() for x in head[i + 1:] if x.startswith((" ", "\t")))
                desc = v.strip("'\"")
    return name, desc


def jaccard(a: frozenset[str], b: frozenset[str]) -> float:
    return len(a & b) / len(a | b) if a and b else 0.0


def _has_token(text: str, term: str) -> bool:
    return re.search(r"(?<![A-Za-z0-9_-])" + re.escape(term) + r"(?![A-Za-z0-9_-])",
                     text) is not None


# the catalog ---------------------------------------------------------------------------
def _cap(ctx: ProbeContext, skill: str, key: str) -> tuple[float, dict[str, Any]]:
    if not ctx.audit_ok:  # a missing audit is missing evidence, never a count of zero
        return math.nan, {"note": "no audit this tick"}
    return float(ctx.capabilities.get(skill, {}).get(key, 0) or 0), {}


def _raw(ctx: ProbeContext, a: dict[str, Any]) -> tuple[float, dict[str, Any]]:
    return float(ctx.raw_skill.get(a["skill"], 0)), {"lines_read": ctx.lines_read}


def _signals(ctx: ProbeContext, a: dict[str, Any]) -> tuple[float, dict[str, Any]]:
    raw = ctx.raw_skill.get(a["skill"], 0)
    ind = _cap(ctx, a["skill"], "indirect_uses")[0]
    return float(raw + ind), {"raw_invocations": raw, "indirect_reads": ind}


def _raw_minus(ctx: ProbeContext, a: dict[str, Any]) -> tuple[float, dict[str, Any]]:
    raw = ctx.raw_skill.get(a["skill"], 0)
    parsed = _cap(ctx, a["skill"], "invocations")[0]
    return float(raw - parsed), {"raw": raw, "parsed": parsed}


def _overlap(ctx: ProbeContext, a: dict[str, Any]) -> tuple[float, dict[str, Any]]:
    own = terms(ctx.descriptions.get(a["skill"], ""))
    best, best_name = 0.0, ""
    for name, d in sorted(ctx.descriptions.items()):
        if name == a["skill"]:
            continue
        j = jaccard(own, terms(d))
        if j > best:
            best, best_name = j, name
    return round(best, 4), {"closest_skill": best_name}


def _eligible(ctx: ProbeContext, a: dict[str, Any]) -> tuple[float, dict[str, Any]]:
    want = ctx.distinctive(a["skill"])
    need = 3
    if len(want) < need:  # cannot tell demand from chance: undecided, never zero
        return math.nan, {"terms": want, "note": "too few distinctive words to judge demand"}
    n = sum(1 for p in ctx.prompts if len(p.terms.intersection(want)) >= need)
    return float(n), {"terms": want, "min_match": need, "prompts_scanned": len(ctx.prompts)}


def _references(ctx: ProbeContext, a: dict[str, Any]) -> tuple[float, dict[str, Any]]:
    own = ctx.skill_dirs.get(a["term"])
    n = 0
    for f in ctx.reference_files:
        if own is not None and own in f.parents:
            continue
        try:
            if _has_token(f.read_text(encoding="utf-8", errors="replace"), a["term"]):
                n += 1
        except OSError:
            continue
    return float(n), {"files_scanned": len(ctx.reference_files)}


def _prompt_terms(ctx: ProbeContext, a: dict[str, Any]) -> tuple[float, dict[str, Any]]:
    want = [t.lower() for t in a["terms"]]
    need = int(a.get("min_match", len(want)))
    n = sum(1 for p in ctx.prompts if len(p.terms.intersection(want)) >= need)
    return float(n), {"prompts_scanned": len(ctx.prompts)}


def _transcript_term(ctx: ProbeContext, a: dict[str, Any]) -> tuple[float, dict[str, Any]]:
    ctx.scan_terms([a["term"]])
    return float(ctx.term_counts.get(a["term"], 0)), {"lines_read": ctx.lines_read}


def _skill(a: dict[str, Any]) -> list[str]:
    s = a.get("skill")
    return [] if isinstance(s, str) and 0 < len(s) <= 128 else ["skill must be a name"]


def _term(a: dict[str, Any]) -> list[str]:
    t = a.get("term")
    return [] if isinstance(t, str) and 3 <= len(t) <= 80 else ["term must be 3 to 80 chars"]


def _terms_arg(a: dict[str, Any]) -> list[str]:
    ts, k = a.get("terms"), a.get("min_match", 1)
    if not (isinstance(ts, list) and 1 <= len(ts) <= 6
            and all(isinstance(t, str) and 3 <= len(t) <= 40 for t in ts)):
        return ["terms must be 1 to 6 words of 3 to 40 chars"]
    if not (isinstance(k, int) and not isinstance(k, bool) and 1 <= k <= len(ts)):
        return ["min_match must be an integer from 1 to len(terms)"]
    return []


CATALOG: dict[str, tuple[Any, Any, str]] = {
    "skill_raw_invocations": (_raw, _skill, "raw Skill tool calls and slash commands naming "
                              "the skill in the window, read without the parser"),
    "skill_parsed_invocations": (lambda c, a: _cap(c, a["skill"], "invocations"), _skill,
                                 "invocations the ARI parser counted"),
    "skill_indirect_reads": (lambda c, a: _cap(c, a["skill"], "indirect_uses"), _skill,
                             "reads of the skill's files without an invocation"),
    "skill_exposed_sessions": (lambda c, a: _cap(c, a["skill"], "exposed_sessions"), _skill,
                               "sessions whose skill listing included it"),
    "skill_use_signals": (_signals, _skill, "raw invocations plus indirect reads"),
    "skill_raw_minus_parsed": (_raw_minus, _skill, "raw invocations the parser missed"),
    "skill_description_overlap": (_overlap, _skill, "highest word overlap (Jaccard 0 to 1) "
                                  "of its description with another skill's"),
    "skill_eligible_prompts": (_eligible, _skill, "user prompts that contain at least 3 of "
                               "the skill's 5 most distinctive description words"),
    "reference_count": (_references, _term, "skill, agent and instruction files outside "
                        "the skill's own folder that mention the term as a word"),
    "prompt_term_count": (_prompt_terms, _terms_arg, "user prompts containing at least "
                          "min_match of the given words"),
    "transcript_term_count": (_transcript_term, _term, "transcript lines in the window "
                              "containing the literal term"),
}


def validate_spec(spec: Any) -> list[str]:
    """A probe spec: {probe, args, supports_if, refutes_if}; rules are {op, value}."""
    if not isinstance(spec, dict):
        return ["probe spec is not an object"]
    name = spec.get("probe")
    if name not in CATALOG:
        return [f"probe {name!r} is not in the catalog"]
    args = spec.get("args")
    if not isinstance(args, dict):
        return ["args is not an object"]
    errs = list(CATALOG[name][1](args))
    for key in ("supports_if", "refutes_if"):
        r = spec.get(key)
        if not (isinstance(r, dict) and r.get("op") in OPS
                and isinstance(r.get("value"), (int, float))
                and not isinstance(r.get("value"), bool)
                and math.isfinite(float(r["value"]))):
            errs.append(f"{key} must be {{op in {sorted(OPS)}, value: finite number}}")
    if not errs:
        errs += _decides(str(name), spec["supports_if"], spec["refutes_if"])
    return errs


SIGNED = {"skill_raw_minus_parsed"}  # the only probe that can be negative
FRACTION = {"skill_description_overlap"}  # a share from 0 to 1; every other probe counts


def _domain_points(name: str, values: list[float]) -> list[float]:
    """Values the probe can return, near every threshold and at the domain edges."""
    if name in FRACTION:
        pts = {0.0, 1.0}
        for v in values:
            pts |= {v - 1e-6, v, v + 1e-6}
        return sorted(p for p in pts if 0.0 <= p <= 1.0)
    pts = {0.0, 1.0, 1e9} | ({-1e9, -1.0} if name in SIGNED else set())
    for v in values:
        pts |= {math.floor(v) - 1, math.floor(v), math.ceil(v), math.ceil(v) + 1}
    return sorted(float(p) for p in pts if name in SIGNED or p >= 0)


def _decides(name: str, sup: dict[str, Any], ref: dict[str, Any]) -> list[str]:
    """A rule pair must be able to support, able to refute, and never do both at once:
    otherwise the probe was decided before it ran."""
    s_only = r_only = False
    for x in _domain_points(name, [float(sup["value"]), float(ref["value"])]):
        s, r = OPS[sup["op"]](x, sup["value"]), OPS[ref["op"]](x, ref["value"])
        if s and r:
            return [f"supports_if and refutes_if both hold at {x:g}"]
        s_only, r_only = s_only or s, r_only or r
    if not s_only:
        return ["supports_if can never hold for this probe"]
    if not r_only:
        return ["refutes_if can never hold for this probe"]
    return []


def prefetch(ctx: ProbeContext, specs: Iterable[dict[str, Any]]) -> None:
    """One transcript rescan for every word the transcript_term_count probes will need."""
    want = [str(s["args"]["term"]) for s in specs
            if s.get("probe") == "transcript_term_count" and isinstance(s.get("args"), dict)
            and isinstance(s["args"].get("term"), str)]
    if want:
        ctx.scan_terms(want)


def run(ctx: ProbeContext, spec: dict[str, Any]) -> tuple[float, dict[str, Any]]:
    value, detail = CATALOG[spec["probe"]][0](ctx, spec["args"])
    return float(value), dict(detail)


def jsonable(value: float) -> float | None:
    """A probe value for a strict JSON record: an undecidable (NaN) value becomes null."""
    return None if math.isnan(value) else value


def judge(value: float, spec: dict[str, Any]) -> str:
    if math.isnan(value):
        return "inconclusive"  # the probe could not measure: never decides
    sup, ref = spec["supports_if"], spec["refutes_if"]
    s = OPS[sup["op"]](value, sup["value"])
    r = OPS[ref["op"]](value, ref["value"])
    if s and not r:
        return "supports"
    if r and not s:
        return "refutes"
    return "inconclusive"  # neither rule, or both (an incoherent pair never decides)


def describe() -> dict[str, str]:
    return {k: v[2] for k, v in CATALOG.items()}


def spec(probe: str, args: dict[str, Any], sup: tuple[str, float],
         ref: tuple[str, float]) -> dict[str, Any]:
    return {"probe": probe, "args": args, "supports_if": {"op": sup[0], "value": sup[1]},
            "refutes_if": {"op": ref[0], "value": ref[1]}}


def group_by_session(prompts: list[Prompt]) -> dict[str, list[Prompt]]:
    out: dict[str, list[Prompt]] = defaultdict(list)
    for p in prompts:
        out[p.session].append(p)
    return out
