"""Canonical evidence engine: one parser for every ARI tool.

Claude Code writes each session to <root>/<project>/<session>.jsonl and each subagent run
to <root>/<project>/<session>/subagents/agent-<id>.jsonl with a sibling .meta.json that
names the agent type. The format is internal to Claude Code and can change between
versions, so unknown fields are ignored and every assumption is counted in Coverage.

Rules (from the session-transcript-usage-accounting method):
* Only records with type "assistant" and a message.usage object carry usage.
* One API request is written as several assistant lines that share a requestId (fallback
  message.id). It is counted once, never summed. If copies carry different usage numbers
  the request is flagged as a conflict and each field keeps its largest value: observed
  on 2026-11-01, later streamed copies carry the final output_tokens of a partial earlier
  copy, and a resumed session can repeat a request with every field zeroed, so the
  per field maximum is the complete record whichever file is read first.
* A record with neither id is skipped and counted (MISSING_REQUEST_ID); it cannot be
  deduplicated, so counting it would invent an exact total.
* cache creation is split into 5m and 1h when the cache_creation object exists; otherwise
  the whole cache_creation_input_tokens is counted as 5m and the fallback is counted.
* The model "<synthetic>" is skipped. Timestamps are normalised to UTC; one without an
  offset is read as UTC and counted.
* A missing agentType is reported as "unknown", never assumed to be general-purpose.
* A last line without a newline is treated as a partial write and skipped.
* Files whose real path leaves the configured root (symlinks) are refused and counted.

Nothing here stores prompt text, tool arguments or file paths in events: tool events keep
the tool name, the skill name and the MCP server name only.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
from collections.abc import Iterable, Iterator
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

EVENT_SCHEMA = "ari-event/1"
PARSER_VERSION = "ari-parser/2"
VERIFIED_FORMATS = frozenset({"2.1"})  # Claude Code major.minor whose schema was checked
# record types seen in format 2.1 transcripts on 2026-11-01 (Claude Code 2.1.303 to 2.1.316);
# anything else is counted so a format change shows up as a warning
KNOWN_RECORD_TYPES = frozenset({
    "user", "assistant", "attachment", "system", "summary", "progress",
    "file-history-snapshot", "file-history-delta", "queue-operation", "api-request",
    "api-request-blob", "api-request-shape", "atis-latch", "last-prompt", "mode",
    "permission-mode", "ai-title", "frame-link", "tools-used", "pr-link", "cost-state",
    "artifact-comment-monitor", "artifact-autoreact-ledger"})
TRANSCRIPT_MARKERS = (b'"usage"', b'"tool_use"', b"command-name", b'"attachment"')
DUP_CLASSES = ("IDENTICAL", "STREAM_GROWTH", "ZEROED_COPY", "AMBIGUOUS")  # rising severity
HEAD_BYTES = 64 * 1024
WINDOW_BYTES = 4 * 1024
USAGE_KEYS = ("input_tokens", "cache_creation_5m", "cache_creation_1h", "cache_read", "output")
MAX_LINE_BYTES = 64 * 1024 * 1024


@dataclass(frozen=True)
class Source:
    """One transcript file. `identity` hashes relpath, size and mtime (cheap, not content)."""

    index: int
    relpath: str
    kind: str  # "main" or "subagent"
    session_id: str
    agent_id: str | None
    agent_type: str
    agent_type_source: str  # "meta", "meta-missing", "meta-unreadable", "main"
    size: int
    mtime: float
    identity: str
    active: bool  # modified within the quiet period, may still be written


@dataclass
class UsageEvent:
    request_id: str
    source: int
    line_no: int
    session_id: str
    agent_id: str | None
    agent_type: str
    kind: str
    model: str
    version: str
    ts: datetime
    input_tokens: int
    cache_creation_5m: int
    cache_creation_1h: int
    cache_read: int
    output: int
    copies: int = 1
    conflict: bool = False
    dup_class: str | None = None  # worst DUP_CLASSES seen among the copies, None if one copy
    copy_max_total: int = 0  # largest single copy total: the lower bound when AMBIGUOUS

    @property
    def context(self) -> int:
        """Tokens the request sent: input plus cache writes plus cache reads."""
        return self.input_tokens + self.cache_creation_5m + self.cache_creation_1h + self.cache_read

    def value(self, metric: str) -> int:
        if metric == "total":
            return self.context + self.output
        if metric == "context":
            return self.context
        if metric == "cache_creation":
            return self.cache_creation_5m + self.cache_creation_1h
        if metric in USAGE_KEYS:
            return int(getattr(self, metric))
        raise ValueError(f"unknown metric {metric!r}")


@dataclass
class ToolEvent:
    source: int
    session_id: str
    agent_id: str | None
    agent_type: str
    ts: datetime | None
    tool: str  # tool name, for MCP tools "mcp__<server>__<tool>"
    skill: str | None = None  # Skill tool target, or a skill whose files were read
    mcp_server: str | None = None
    via: str = "tool_use"  # "tool_use", "skill_file_read", "slash_command"


@dataclass
class ExposureEvent:
    source: int
    session_id: str
    ts: datetime | None
    category: str  # "skill", "mcp_server", "deferred_tool", "agent_type"
    mode: str  # "snapshot" (full list) or "delta"
    added: tuple[str, ...]
    removed: tuple[str, ...] = ()
    sizes: dict[str, int] = field(default_factory=dict)  # listing chars per skill


@dataclass
class Coverage:
    roots: int = 0
    files_listed: int = 0
    files_read: int = 0
    files_skipped_mtime: int = 0
    files_unreadable: int = 0
    files_outside_root: int = 0
    files_unrecognized: int = 0
    files_active: int = 0
    lines: int = 0
    malformed_lines: int = 0
    oversize_lines: int = 0
    partial_last_lines: int = 0
    usage_records: int = 0
    requests: int = 0
    duplicate_records: int = 0
    duplicate_conflicts: int = 0
    missing_request_id: int = 0
    invalid_timestamp: int = 0
    timestamp_assumed_utc: int = 0
    synthetic_skipped: int = 0
    negative_or_bad_tokens: int = 0
    cache_split_fallback: int = 0
    outside_window: int = 0
    missing_agent_type: int = 0
    files_workflow_journal: int = 0
    files_foreign: int = 0
    journal_agents_missing: int = 0
    unverified_format_records: int = 0
    unknown_record_types: int = 0
    format_versions: dict[str, int] = field(default_factory=dict)
    duplicate_classes: dict[str, int] = field(default_factory=dict)
    first_ts: str | None = None
    last_ts: str | None = None

    def warnings(self) -> list[str]:
        out: list[str] = []
        checks = (
            (self.files_unreadable, "transcript files could not be read"),
            (self.files_outside_root, "files refused because their real path leaves the root"),
            (self.files_unrecognized, ".jsonl files in unrecognised locations carry transcript "
                                      "markers and were not read"),
            (self.journal_agents_missing, "workflow agent runs named in a journal have no "
                                          "transcript"),
            (self.unverified_format_records, "usage records come from a Claude Code version "
                                             "whose transcript format was not verified (read "
                                             "field by field)"),
            (self.unknown_record_types, "parsed records had an unknown type"),
            (self.malformed_lines, "lines were not JSON objects and were skipped"),
            (self.partial_last_lines, "last lines had no newline (partial write) and were skipped"),
            (self.oversize_lines, "lines were larger than the line limit and were skipped"),
            (self.missing_request_id, "usage records had no request id and were not counted"),
            (self.duplicate_conflicts, "requests had differing copies (per field maximum kept)"),
            (self.invalid_timestamp, "usage records had no valid timestamp and were skipped"),
            (self.negative_or_bad_tokens, "usage records had invalid token fields, skipped"),
            (self.missing_agent_type, "subagent runs had no agentType (reported as unknown)"),
            (self.files_active, "files were modified within the quiet period (still active)"),
            (self.duplicate_classes.get("AMBIGUOUS", 0),
             "requests had copies that differ for no known reason: totals carry an uncertainty "
             "band (see total_uncertainty)"),
        )
        for n, text in checks:
            if n:
                out.append(f"{n} {text}")
        if self.requests == 0:
            out.append("no countable request found: this is missing data, not zero usage")
        return out

    def unread_gaps(self) -> int:
        """Files or lines that were not read and could hide a tool call or a request.

        Files skipped because they were last modified before the window start are not gaps:
        they cannot hold a record from inside the window.
        """
        return (self.files_unreadable + self.files_outside_root + self.files_unrecognized
                + self.malformed_lines + self.partial_last_lines + self.oversize_lines
                + self.journal_agents_missing)

    def excluded(self) -> dict[str, int]:
        """Files found and deliberately not read, with the reason (never gaps)."""
        return {"workflow_journal (index of agent runs, cross checked)":
                self.files_workflow_journal,
                "foreign (full scan found no transcript marker)": self.files_foreign,
                "before the window (mtime)": self.files_skipped_mtime}


@dataclass
class EvidenceSet:
    sources: list[Source]
    usage: list[UsageEvent]
    tools: list[ToolEvent]
    exposures: list[ExposureEvent]
    coverage: Coverage
    window: tuple[str | None, str | None]
    manifest: list[dict[str, Any]] = field(default_factory=list)

    def total_uncertainty(self, events: Iterable[UsageEvent] | None = None) -> dict[str, int]:
        """Token total bounds over `events` (default all). Only AMBIGUOUS duplicates widen
        the band; every other class merges to one of its own copies."""
        lo = hi = amb = 0
        for e in (self.usage if events is None else events):
            t = e.value("total")
            hi += t
            if e.dup_class == "AMBIGUOUS":
                amb += 1
                lo += min(t, e.copy_max_total)
            else:
                lo += t
        return {"lower": lo, "upper": hi, "ambiguous_requests": amb}

    def first_requests(self) -> dict[tuple[str, str], UsageEvent]:
        """Earliest request of every subagent run, keyed by (session_id, agent_id)."""
        first: dict[tuple[str, str], UsageEvent] = {}
        for ev in self.usage:
            if ev.kind != "subagent" or ev.agent_id is None:
                continue
            key = (ev.session_id, ev.agent_id)
            cur = first.get(key)
            if cur is None or (ev.ts, ev.line_no) < (cur.ts, cur.line_no):
                first[key] = ev
        return first


_UNPARSED = object()
AGENT_ID = re.compile(r"[A-Za-z0-9_-]{1,128}")


def loads_bounded(raw: bytes | str) -> Any:
    """json.loads that turns every hostile input into _UNPARSED instead of raising: bad
    syntax or encoding, an integer over the conversion limit (ValueError) and nesting deep
    enough to exhaust the stack (RecursionError)."""
    try:
        return json.loads(raw)
    except (ValueError, UnicodeDecodeError, RecursionError):
        return _UNPARSED


def parse_ts(value: Any, cov: Coverage) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    text = value.strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        dt = datetime.fromisoformat(text)
    except ValueError:
        return None
    if dt.tzinfo is None:
        cov.timestamp_assumed_utc += 1
        dt = dt.replace(tzinfo=timezone.utc)
    try:
        return dt.astimezone(timezone.utc)
    except OverflowError:  # an offset pushes the time past year 1 or 9999
        return None


def _within(child: Path, root: Path) -> bool:
    try:
        child.relative_to(root)
    except ValueError:
        return False
    return True


def discover(
    roots: Iterable[str | os.PathLike[str]],
    cov: Coverage,
    since: datetime | None = None,
    include_main: bool = True,
    include_subagents: bool = True,
    quiet_seconds: float = 0,
    now: datetime | None = None,
    skip_active: bool = False,
    other_rows: list[dict[str, Any]] | None = None,
) -> list[tuple[Source, Path]]:
    """List transcript files under each root, refusing any whose real path leaves it.

    Every other `.jsonl` under the root is classified (ARI2-R1) and, when `other_rows` is
    given, listed there with its class; only an `unknown` file is an unread gap."""
    found: list[tuple[Source, Path]] = []
    now_ts = (now or datetime.now(timezone.utc)).timestamp()
    since_ts = since.timestamp() if since else None
    for root_arg in roots:
        cov.roots += 1
        root = Path(root_arg).expanduser().resolve()
        if not root.is_dir():
            continue
        patterns: list[tuple[str, str]] = []
        if include_main:
            patterns.append(("*/*.jsonl", "main"))
        if include_subagents:
            patterns.append(("*/*/subagents/*.jsonl", "subagent"))
            # workflow agents: <project>/<session>/subagents/workflows/wf_*/agent-*.jsonl
            patterns.append(("*/*/subagents/workflows/*/agent-*.jsonl", "subagent"))
        matched: set[Path] = set()
        for pattern, kind in patterns:
            for path in sorted(root.glob(pattern)):
                matched.add(path)
                cov.files_listed += 1
                real = path.resolve()
                if not _within(real, root) or not real.is_file():
                    cov.files_outside_root += 1
                    continue
                try:
                    st = real.stat()
                except OSError:
                    cov.files_unreadable += 1
                    continue
                if since_ts is not None and st.st_mtime < since_ts:
                    cov.files_skipped_mtime += 1
                    continue
                rel = path.relative_to(root)
                agent_id: str | None = None
                agent_type = "main"
                type_source = "main"
                if kind == "main":
                    session_id = path.stem
                else:
                    session_id = rel.parts[1]
                    stem = path.stem
                    agent_id = stem[len("agent-"):] if stem.startswith("agent-") else stem
                    agent_type, type_source = _agent_type(path, root, cov)
                active = quiet_seconds > 0 and (now_ts - st.st_mtime) < quiet_seconds
                if active:
                    cov.files_active += 1
                    if skip_active:
                        continue
                ident = hashlib.sha256(
                    f"{rel.as_posix()}|{st.st_size}|{st.st_mtime_ns}".encode()
                ).hexdigest()[:16]
                src = Source(
                    index=len(found),
                    relpath=rel.as_posix(),
                    kind=kind,
                    session_id=session_id,
                    agent_id=agent_id,
                    agent_type=agent_type,
                    agent_type_source=type_source,
                    size=st.st_size,
                    mtime=st.st_mtime,
                    identity=ident,
                    active=active,
                )
                found.append((src, real))
        if include_main and include_subagents:
            # a layout this parser does not know must show up as a warning, never as silence
            # rglob never enters a directory link, so walk without following links and
            # report each link that leaves the root unless a layout glob already read it
            others: list[Path] = []
            for dirpath, dirnames, filenames in os.walk(root, followlinks=False):
                base = Path(dirpath)
                for d in sorted(dirnames):
                    link = base / d
                    if (link.is_symlink() and not _within(link.resolve(), root)
                            and not any(link in m.parents for m in matched)):
                        cov.files_outside_root += 1  # a directory link out of the root
                        if other_rows is not None:
                            other_rows.append({"relpath": link.relative_to(root).as_posix(),
                                               "class": "outside_root"})
                others.extend(base / f for f in filenames if f.endswith(".jsonl"))
            for p in sorted(others):
                if p in matched:
                    continue
                cls = classify_other(p, root, cov)
                if other_rows is not None:
                    other_rows.append({"relpath": p.relative_to(root).as_posix(),
                                       "class": cls})
    return found


def _has_markers(path: Path) -> bool | None:
    """True when any line holds a transcript marker; None when the file cannot be read."""
    try:
        with open(path, "rb") as fh:
            return any(any(m in raw for m in TRANSCRIPT_MARKERS) for raw in fh)
    except OSError:
        return None


def classify_other(path: Path, root: Path, cov: Coverage) -> str:
    """Class of a .jsonl outside the transcript layout: workflow_journal, foreign or unknown.

    A journal (<session>/subagents/workflows/<wf>/journal.jsonl) is an index: each agent id
    it starts must have agent-<id>.jsonl beside it, and each missing one is a gap. A file
    with no transcript marker anywhere (full scan) cannot hold a request or a tool call, so
    it is excluded as foreign; a file with markers in an unknown place is a gap."""
    real = path.resolve()
    if not _within(real, root) or not real.is_file():
        cov.files_outside_root += 1
        return "outside_root"
    parts = path.relative_to(root).parts
    if (path.name == "journal.jsonl" and len(parts) == 6 and parts[2] == "subagents"
            and parts[3] == "workflows"):
        started: set[str] = set()
        try:
            with open(real, "rb") as fh:
                for raw in fh:
                    if b'"started"' not in raw:
                        continue
                    rec = loads_bounded(raw)
                    if not isinstance(rec, dict):  # a list or string line is not a record
                        continue
                    aid = rec.get("agentId")
                    if rec.get("type") != "started" or not aid:
                        continue
                    if isinstance(aid, str) and AGENT_ID.fullmatch(aid):
                        started.add(aid)
                    else:  # never used as a path: too long, a separator, a null byte
                        cov.malformed_lines += 1
        except OSError:
            cov.files_unreadable += 1
            return "unreadable"
        cov.files_workflow_journal += 1
        cov.journal_agents_missing += sum(
            1 for a in started if not (path.parent / f"agent-{a}.jsonl").is_file())
        return "workflow_journal"
    marked = _has_markers(real)
    if marked is None:
        cov.files_unreadable += 1
        return "unreadable"
    if marked:
        cov.files_unrecognized += 1
        return "unknown"
    cov.files_foreign += 1
    return "foreign"


def checkpoint(path: Path, offset: int) -> dict[str, str]:
    """Sampled checkpoint (ARI2-R3): hash of the first HEAD_BYTES and of up to WINDOW_BYTES
    ending at `offset` (the end of the last complete line read)."""
    with open(path, "rb") as fh:
        head = fh.read(min(HEAD_BYTES, offset))
        start = max(0, offset - WINDOW_BYTES)
        fh.seek(start)
        window = fh.read(offset - start)
    return {"head_sha256": hashlib.sha256(head).hexdigest(),
            "window_sha256": hashlib.sha256(window).hexdigest()}


def compare_checkpoint(prev: dict[str, Any], path: Path) -> str:
    """NEW is decided by the caller. UNCHANGED, GROWN, TRUNCATED or REPLACED against a
    previous manifest row, by size, then the sampled hashes at the previous offset."""
    try:
        size = path.stat().st_size
    except OSError:
        return "MISSING"
    off = int(prev.get("offset", 0))
    if size < off:
        return "TRUNCATED"
    try:
        now = checkpoint(path, off)
    except OSError:
        return "MISSING"
    if (now["head_sha256"] != prev.get("head_sha256")
            or now["window_sha256"] != prev.get("window_sha256")):
        return "REPLACED"
    return "UNCHANGED" if size == int(prev.get("size", -1)) else "GROWN"


def _agent_type(path: Path, root: Path, cov: Coverage) -> tuple[str, str]:
    meta = path.with_name(path.name[: -len(".jsonl")] + ".meta.json")
    if not meta.exists():
        cov.missing_agent_type += 1
        return "unknown", "meta-missing"
    if not _within(meta.resolve(), root):
        cov.files_outside_root += 1
        cov.missing_agent_type += 1
        return "unknown", "meta-unreadable"
    try:
        data = loads_bounded(meta.read_bytes())
    except OSError:
        data = _UNPARSED
    if data is _UNPARSED:
        cov.missing_agent_type += 1
        return "unknown", "meta-unreadable"
    value = data.get("agentType") if isinstance(data, dict) else None
    if not isinstance(value, str) or not value.strip():
        cov.missing_agent_type += 1
        return "unknown", "meta-missing"
    return value.strip(), "meta"


def iter_lines(path: Path, cov: Coverage, pos: list[int] | None = None
               ) -> Iterator[tuple[int, bytes]]:
    """Yield (line_no, raw line) for complete lines; a trailing partial line is skipped.
    When `pos` is given, pos[0] is kept at the byte offset after the last complete line."""
    try:
        fh = open(path, "rb")
    except OSError:
        cov.files_unreadable += 1
        return
    with fh:
        cov.files_read += 1
        n = 0
        for raw in fh:
            n += 1
            cov.lines += 1
            if not raw.endswith(b"\n"):
                cov.partial_last_lines += 1
                return
            if pos is not None:
                pos[0] += len(raw)
            if len(raw) > MAX_LINE_BYTES:
                cov.oversize_lines += 1
                continue
            yield n, raw


def _int_field(usage: dict[str, Any], key: str) -> int | None:
    value = usage.get(key, 0)
    if value is None:
        return 0
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        return None
    return value


_SKILL_LINE = re.compile(r"^- ([^\s(]+?)(?: \([^)\n]*\))?:(?: (.*))?$")
_SLASH = re.compile(r"<command-name>/?([A-Za-z0-9][A-Za-z0-9:_.-]*)</command-name>")


def skill_names_from_listing(content: str) -> dict[str, int]:
    """Parse a skill_listing attachment: '- name: description' lines -> {name: chars}."""
    names: dict[str, int] = {}
    for line in content.splitlines():
        m = _SKILL_LINE.match(line.strip())
        if m:
            names[m.group(1).strip()] = len(line)
    return names


_MCP_NAME_UNSAFE = re.compile(r"[^A-Za-z0-9_-]")


def normalize_mcp_name(name: str) -> str:
    """The tool prefix form of an MCP server name.

    Claude Code lists a server by its display name ("plugin:github:github",
    "claude.ai Claude Docs") but prefixes its tools with that name after replacing every
    character outside [A-Za-z0-9_-] with "_" ("mcp__plugin_github_github__..."). Exposure,
    config and invocation records are joined on this form, so one server is never split
    into an invoked entry and a never used entry (observed on real history 2026-11-01).
    """
    return _MCP_NAME_UNSAFE.sub("_", name)


def mcp_server_of(tool: str) -> str | None:
    if not tool.startswith("mcp__"):
        return None
    parts = tool.split("__")
    return parts[1] if len(parts) >= 3 and parts[1] else None


def ingest(
    roots: Iterable[str | os.PathLike[str]],
    since: datetime | None = None,
    until: datetime | None = None,
    include_main: bool = True,
    include_subagents: bool = True,
    want_tools: bool = False,
    want_exposure: bool = False,
    skills_roots: Iterable[str | os.PathLike[str]] = (),
    quiet_seconds: float = 0,
    now: datetime | None = None,
    skip_active: bool = False,
) -> EvidenceSet:
    """Read transcripts under the roots into deduplicated canonical events.

    `since` is inclusive and `until` exclusive (UTC). The source files are only read.
    """
    cov = Coverage()
    others: list[dict[str, Any]] = []
    files = discover(roots, cov, since, include_main, include_subagents, quiet_seconds, now,
                     skip_active, others)
    manifest: list[dict[str, Any]] = []
    skill_dirs = [Path(p).expanduser().resolve() for p in skills_roots]
    lexical_dirs = [Path(os.path.abspath(Path(p).expanduser())) for p in skills_roots]
    installed = installed_skill_dirs(skill_dirs)
    seen: dict[str, UsageEvent] = {}
    usage: list[UsageEvent] = []
    tools: list[ToolEvent] = []
    exposures: list[ExposureEvent] = []
    for src, real in files:
        pos = [0]
        for line_no, raw in iter_lines(real, cov, pos):
            has_usage = b'"usage"' in raw
            has_tool = want_tools and (b'"tool_use"' in raw or b"command-name" in raw)
            has_att = want_exposure and b'"attachment"' in raw
            if not (has_usage or has_tool or has_att):
                if raw.strip() and not raw.lstrip().startswith(b"{"):
                    cov.malformed_lines += 1  # cheap check; unmarked lines are not parsed
                continue
            rec = loads_bounded(raw)
            if rec is _UNPARSED:
                cov.malformed_lines += 1
                continue
            if not isinstance(rec, dict):
                cov.malformed_lines += 1
                continue
            rtype = rec.get("type")
            ts = parse_ts(rec.get("timestamp"), cov)
            # tool and exposure records obey the same window as usage; one without a
            # timestamp cannot be placed and is kept, as before
            in_window = ts is None or not ((since and ts < since) or (until and ts >= until))
            if has_tool and in_window:
                tools.extend(_tool_events(rec, src, ts, skill_dirs + lexical_dirs))
            if has_att and rtype == "attachment" and in_window:
                exp = _exposure_event(rec, src, ts)
                if exp is not None:
                    exposures.append(exp)
            if isinstance(rtype, str) and rtype not in KNOWN_RECORD_TYPES:
                cov.unknown_record_types += 1
            if has_usage and rtype == "assistant":
                _usage_event(rec, src, line_no, ts, since, until, cov, seen, usage)
        row: dict[str, Any] = {"path": str(real), "relpath": src.relpath, "class": src.kind,
                               "size": src.size, "mtime": src.mtime, "offset": pos[0],
                               "active": src.active, "parser": PARSER_VERSION}
        try:
            row.update(checkpoint(real, pos[0]))
        except OSError:
            row["checkpoint"] = "unreadable"
        manifest.append(row)
    if tools:
        # a name typed after a slash or passed to Skill is kept only when it names a skill
        # that is installed or was listed; anything else may be prompt text (ARI-R7)
        known = set(installed) | {n for e in exposures if e.category == "skill"
                                  for n in e.added}
        for t in tools:
            if t.skill and t.skill not in known:
                t.skill = None
    cov.requests = len(usage)
    if usage:
        cov.first_ts = min(e.ts for e in usage).isoformat()
        cov.last_ts = max(e.ts for e in usage).isoformat()
    usage.sort(key=lambda e: (e.ts, e.source, e.line_no))
    window = (since.isoformat() if since else None, until.isoformat() if until else None)
    manifest.extend(others)
    return EvidenceSet([s for s, _ in files], usage, tools, exposures, cov, window, manifest)


def _usage_event(
    rec: dict[str, Any],
    src: Source,
    line_no: int,
    ts: datetime | None,
    since: datetime | None,
    until: datetime | None,
    cov: Coverage,
    seen: dict[str, UsageEvent],
    out: list[UsageEvent],
) -> None:
    msg = rec.get("message")
    if not isinstance(msg, dict):
        return
    usage = msg.get("usage")
    if not isinstance(usage, dict):
        return
    cov.usage_records += 1
    model = msg.get("model")
    model = model if isinstance(model, str) and model else "unknown"
    if model == "<synthetic>":
        cov.synthetic_skipped += 1
        return
    req = rec.get("requestId") if isinstance(rec.get("requestId"), str) else None
    mid = msg.get("id") if isinstance(msg.get("id"), str) else None
    keys = [k for k in (("rid:" + req) if req else None, ("mid:" + mid) if mid else None) if k]
    if not keys:
        cov.missing_request_id += 1
        return
    rid = req or mid or ""
    if ts is None:
        cov.invalid_timestamp += 1
        return
    if (since and ts < since) or (until and ts >= until):
        cov.outside_window += 1
        return
    vals: dict[str, int] = {}
    for key in ("input_tokens", "cache_creation_input_tokens", "cache_read_input_tokens",
                "output_tokens"):
        v = _int_field(usage, key)
        if v is None:
            cov.negative_or_bad_tokens += 1
            return
        vals[key] = v
    split = usage.get("cache_creation")
    if isinstance(split, dict) and (
        "ephemeral_5m_input_tokens" in split or "ephemeral_1h_input_tokens" in split
    ):
        c5 = _int_field(split, "ephemeral_5m_input_tokens")
        c1 = _int_field(split, "ephemeral_1h_input_tokens")
        if c5 is None or c1 is None:
            cov.negative_or_bad_tokens += 1
            return
    else:
        c5, c1 = vals["cache_creation_input_tokens"], 0
        if c5:
            cov.cache_split_fallback += 1
    prior = next((seen[k] for k in keys if k in seen), None)
    numbers = (vals["input_tokens"], c5, c1, vals["cache_read_input_tokens"], vals["output_tokens"])
    version = rec.get("version")
    if prior is None:
        fmt = format_of(version)
        cov.format_versions[fmt] = cov.format_versions.get(fmt, 0) + 1
        if fmt not in VERIFIED_FORMATS:
            cov.unverified_format_records += 1
    if prior is not None:
        cov.duplicate_records += 1
        prior.copies += 1
        merged = (prior.input_tokens, prior.cache_creation_5m, prior.cache_creation_1h,
                  prior.cache_read, prior.output)
        cls = duplicate_class(merged, numbers)
        if prior.dup_class is None or DUP_CLASSES.index(cls) > DUP_CLASSES.index(
                prior.dup_class):
            if prior.dup_class is not None:
                cov.duplicate_classes[prior.dup_class] -= 1
            prior.dup_class = cls
            cov.duplicate_classes[cls] = cov.duplicate_classes.get(cls, 0) + 1
        prior.copy_max_total = max(prior.copy_max_total, sum(numbers))
        for k in keys:  # a copy that carries both ids links them for the copies after it
            seen.setdefault(k, prior)
        if ts < prior.ts:  # the earliest copy dates the request, whatever the file order
            prior.ts = ts
        old = (prior.input_tokens, prior.cache_creation_5m, prior.cache_creation_1h,
               prior.cache_read, prior.output)
        if old != numbers:
            if not prior.conflict:
                prior.conflict = True
                cov.duplicate_conflicts += 1
            (prior.input_tokens, prior.cache_creation_5m, prior.cache_creation_1h,
             prior.cache_read, prior.output) = (max(a, b) for a, b in zip(old, numbers))
        return
    ev = UsageEvent(
        request_id=rid,
        source=src.index,
        line_no=line_no,
        session_id=src.session_id,
        agent_id=src.agent_id,
        agent_type=src.agent_type,
        kind=src.kind,
        model=model,
        version=version if isinstance(version, str) else "unknown",
        ts=ts,
        input_tokens=numbers[0],
        cache_creation_5m=numbers[1],
        cache_creation_1h=numbers[2],
        cache_read=numbers[3],
        output=numbers[4],
        copy_max_total=sum(numbers),
    )
    for k in keys:  # a copy that carries only one of the ids still finds this request
        seen.setdefault(k, ev)
    out.append(ev)


def format_of(version: Any) -> str:
    """major.minor of a Claude Code version string, or "unknown"."""
    if isinstance(version, str):
        m = re.match(r"(\d{1,3})\.(\d{1,3})(?:\.|$)", version)
        if m:
            return f"{int(m.group(1))}.{int(m.group(2))}"
    return "unknown"


def duplicate_class(a: tuple[int, ...], b: tuple[int, ...]) -> str:
    """Class of a second copy b against the request a (ARI2-R5). Fields are input, cache
    5m, cache 1h, cache read, output."""
    if a == b:
        return "IDENTICAL"
    if not any(a) or not any(b):
        return "ZEROED_COPY"
    if a[:4] == b[:4]:
        return "STREAM_GROWTH"
    return "AMBIGUOUS"


def _tool_events(
    rec: dict[str, Any], src: Source, ts: datetime | None, skill_dirs: list[Path]
) -> list[ToolEvent]:
    out: list[ToolEvent] = []
    rtype = rec.get("type")
    msg = rec.get("message")
    if not isinstance(msg, dict):
        return out
    content = msg.get("content")
    if rtype == "user":
        text = content if isinstance(content, str) else ""
        if isinstance(content, list):
            text = " ".join(str(b.get("text", "")) for b in content
                            if isinstance(b, dict) and b.get("type") == "text")
        for m in _SLASH.finditer(text):
            out.append(ToolEvent(src.index, src.session_id, src.agent_id, src.agent_type, ts,
                                 "slash_command", skill=m.group(1), via="slash_command"))
        return out
    if rtype != "assistant" or not isinstance(content, list):
        return out
    for block in content:
        if not isinstance(block, dict) or block.get("type") != "tool_use":
            continue
        name = block.get("name")
        if not isinstance(name, str) or not name:
            continue
        args = block.get("input") if isinstance(block.get("input"), dict) else {}
        assert isinstance(args, dict)
        skill: str | None = None
        via = "tool_use"
        if name == "Skill":
            target = args.get("skill")
            skill = target if isinstance(target, str) and target else None
        elif name == "Read" and skill_dirs:
            skill = _skill_from_path(args.get("file_path"), skill_dirs)
            if skill:
                via = "skill_file_read"
        out.append(ToolEvent(src.index, src.session_id, src.agent_id, src.agent_type, ts, name,
                             skill=skill, mcp_server=mcp_server_of(name), via=via))
    return out


def installed_skill_dirs(skill_dirs: Iterable[Path]) -> set[str]:
    """Folder names under the skills roots that hold a SKILL.md inside the root."""
    out: set[str] = set()
    for base in skill_dirs:
        if not base.is_dir():
            continue
        for skill_md in base.glob("*/SKILL.md"):
            real = skill_md.resolve()
            if real.is_file() and _within(real, base):
                out.add(skill_md.parent.name)
    return out


def _skill_from_path(value: Any, skill_dirs: list[Path]) -> str | None:
    """The skill folder a Read path names, as written (a symlinked file inside a skill folder
    still names that skill). Unknown folder names are dropped later in ingest."""
    if not isinstance(value, str) or not value:
        return None
    try:
        lexical = Path(os.path.abspath(Path(value).expanduser()))
        real = lexical.resolve()
    except (OSError, RuntimeError, ValueError):
        return None
    for p in (lexical, real):
        for d in skill_dirs:
            if _within(p, d):
                rel = p.relative_to(d)
                # only a file inside a skill folder names a skill; a loose file names nothing
                return rel.parts[0] if len(rel.parts) >= 2 else None
    return None


def _exposure_event(rec: dict[str, Any], src: Source, ts: datetime | None
                    ) -> ExposureEvent | None:
    att = rec.get("attachment")
    if not isinstance(att, dict):
        return None
    kind = att.get("type")

    def names(key: str) -> tuple[str, ...]:
        val = att.get(key)
        if not isinstance(val, list):
            return ()
        return tuple(sorted({v for v in val if isinstance(v, str) and v}))

    if kind == "skill_listing" and isinstance(att.get("content"), str):
        sizes = skill_names_from_listing(str(att["content"]))
        return ExposureEvent(src.index, src.session_id, ts, "skill", "snapshot",
                             tuple(sorted(sizes)), (), sizes)
    if kind == "mcp_instructions_delta":
        def mcp_names(key: str) -> tuple[str, ...]:
            return tuple(sorted({normalize_mcp_name(n) for n in names(key)}))

        return ExposureEvent(src.index, src.session_id, ts, "mcp_server", "delta",
                             mcp_names("addedNames"), mcp_names("removedNames"))
    if kind == "deferred_tools_delta":
        return ExposureEvent(src.index, src.session_id, ts, "deferred_tool", "delta",
                             names("addedNames"), names("removedNames"))
    if kind == "agent_listing_delta":
        return ExposureEvent(src.index, src.session_id, ts, "agent_type", "delta",
                             names("addedTypes"), names("removedTypes"))
    return None


def exposed_by_session(exposures: Iterable[ExposureEvent]) -> dict[str, dict[str, set[str]]]:
    """Final exposed set per session and category, applying snapshots and deltas in order."""
    state: dict[str, dict[str, set[str]]] = {}
    ordered = sorted(exposures, key=lambda e: (e.session_id, e.ts or datetime.min.replace(
        tzinfo=timezone.utc)))
    for e in ordered:
        cats = state.setdefault(e.session_id, {})
        cur = cats.setdefault(e.category, set())
        if e.mode == "snapshot":
            cur.clear()
        cur.update(e.added)
        cur.difference_update(e.removed)
    for cats in state.values():
        tools = cats.get("deferred_tool", set())
        servers = cats.setdefault("mcp_server", set())
        servers.update(s for s in (mcp_server_of(t) for t in tools) if s)
    return state


def coverage_dict(cov: Coverage) -> dict[str, Any]:
    return asdict(cov)


def percentile(values: list[int], q: float) -> float:
    """Nearest rank percentile (q in 0..1) on a copy; empty input raises."""
    if not values:
        raise ValueError("percentile of empty list")
    s = sorted(values)
    idx = max(0, min(len(s) - 1, int(round(q * (len(s) - 1)))))
    return float(s[idx])
