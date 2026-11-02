"""Self contained offline HTML report and the opt in, privacy screened share summary.

The HTML file has no script, no external font, stylesheet or image, and a Content
Security Policy that forbids network loads. Every dynamic string is HTML escaped.

The share summary is written to a local file only; nothing is uploaded. It keeps per agent
type run buckets and rounded first request context, suppresses groups smaller than k,
folds custom agent names into "custom" and carries no path, prompt, tool argument, project,
session id or timestamp. It is aggregated and screened, not proven anonymous.
"""
from __future__ import annotations

import html
import json
import re
import statistics
from collections import defaultdict
from datetime import datetime
from typing import Any

from . import ARI_VERSION
from .abduction import HypothesisSet, context_gap
from .evidence import EvidenceSet, coverage_dict, percentile

SHARE_SCHEMA = "ari-share/1"
SHARE_NOTE = ("aggregated and screened local summary; not proven anonymous; "
              "publish only after you read it")
BUILTIN_TYPES = frozenset({"general-purpose", "Explore", "Plan", "fork", "statusline-setup",
                           "claude-code-guide", "lean-worker", "lean-checker", "unknown"})
BINS = [(0, 10_000), (10_000, 25_000), (25_000, 50_000), (50_000, 100_000),
        (100_000, 150_000), (150_000, 10**12)]
BIN_LABELS = ["<10k", "10-25k", "25-50k", "50-100k", "100-150k", "150k+"]
LEAN_TYPES = ("lean-worker", "lean-checker")


def e(x: Any) -> str:
    return html.escape(str(x), quote=True)


def start_contexts(ev: EvidenceSet) -> dict[str, list[int]]:
    out: dict[str, list[int]] = defaultdict(list)
    for u in ev.first_requests().values():
        out[u.agent_type].append(u.context)
    return dict(out)


def gap_hypotheses(ev: EvidenceSet, min_runs: int = 3) -> list[HypothesisSet]:
    """Open a hypothesis set for the gap between general-purpose and the leanest agent."""
    sc = start_contexts(ev)
    gp = sc.get("general-purpose", [])
    lean = {t: sc[t] for t in LEAN_TYPES if len(sc.get(t, [])) >= min_runs}
    if len(gp) < min_runs or not lean:
        return []
    t, vals = min(lean.items(), key=lambda kv: statistics.median(kv[1]))
    gp_med, lean_med = statistics.median(gp), statistics.median(vals)
    obs = (f"general-purpose median first request context {gp_med / 1000:.1f}k over {len(gp)} "
           f"runs vs {t} {lean_med / 1000:.1f}k over {len(vals)} runs")
    signals: dict[str, list[str]] = {
        # The lean agent files set omitClaudeMd and a tools allowlist without Skill or mcp__,
        # so these mechanisms differ by construction. That is configuration, not a test.
        "claude_md": [f"{t} sets omitClaudeMd: true (agent file)"],
        "skills": [f"{t} tools allowlist has no Skill tool (agent file)"],
        "mcp": [f"{t} tools allowlist has no mcp__ tools (agent file)"],
    }
    versions = defaultdict(set)
    for u in ev.first_requests().values():
        versions[u.agent_type].add(u.version)
    if versions.get("general-purpose", set()) != versions.get(t, set()):
        signals["version_model"] = ["the compared runs span different Claude Code versions"]
    hs = context_gap(obs, [f"first_requests:{'general-purpose'}", f"first_requests:{t}"], signals)
    return [hs]


def _svg_hbars(title: str, rows: list[tuple[str, float]], unit: str) -> str:
    if not rows:
        return "<p>No data.</p>"
    top = max(v for _, v in rows) or 1.0
    h = 24 * len(rows) + 10
    parts = [f'<svg class="chart" viewBox="0 0 640 {h}" role="img" aria-label="{e(title)}">'
             f"<title>{e(title)}</title>"]
    for i, (label, v) in enumerate(rows):
        y = 5 + i * 24
        w = max(1.0, 380 * v / top)
        parts.append(f'<text x="0" y="{y + 15}" class="lbl">{e(label[:28])}</text>'
                     f'<rect x="200" y="{y + 3}" width="{w:.1f}" height="16" class="bar"/>'
                     f'<text x="{205 + w:.1f}" y="{y + 15}" class="val">{e(f"{v:,.1f}{unit}")}'
                     "</text>")
    parts.append("</svg>")
    return "".join(parts)


def _svg_stacked(title: str, rows: list[tuple[str, list[float]]], keys: list[str]) -> str:
    if not rows:
        return "<p>No data.</p>"
    h = 24 * len(rows) + 34
    parts = [f'<svg class="chart" viewBox="0 0 640 {h}" role="img" aria-label="{e(title)}">'
             f"<title>{e(title)}</title>"]
    for i, (label, vals) in enumerate(rows):
        y = 5 + i * 24
        total = sum(vals) or 1.0
        x = 200.0
        parts.append(f'<text x="0" y="{y + 15}" class="lbl">{e(label[:28])}</text>')
        for j, v in enumerate(vals):
            w = 420 * v / total
            parts.append(f'<rect x="{x:.1f}" y="{y + 3}" width="{w:.1f}" height="16" '
                         f'class="s{j}"><title>{e(keys[j])}: {v / total:.1%}</title></rect>')
            x += w
    y = 10 + len(rows) * 24
    for j, k in enumerate(keys):
        parts.append(f'<rect x="{j * 125}" y="{y}" width="10" height="10" class="s{j}"/>'
                     f'<text x="{j * 125 + 14}" y="{y + 9}" class="val">{e(k)}</text>')
    parts.append("</svg>")
    return "".join(parts)


CSS = """
:root{--bg:#fbfaf7;--fg:#1d1d1b;--muted:#5d5b55;--line:#dedbd2;--bar:#3a6ea5;
--s0:#3a6ea5;--s1:#7fa7d0;--s2:#b5651d;--s3:#9bb37a;--s4:#8c5a9e;--warn:#9a4b00}
@media (prefers-color-scheme:dark){:root:not([data-theme="light"]){--bg:#17181a;--fg:#ecebe6;
--muted:#a7a59d;--line:#34352f;--bar:#7fa7d0;--s0:#7fa7d0;--s1:#3a6ea5;--s2:#e09a5a;
--s3:#9bb37a;--s4:#c49bd6;--warn:#ffb26b}}
body{background:var(--bg);color:var(--fg);font:15px/1.5 system-ui,sans-serif;margin:0 auto;
max-width:900px;padding:16px}
h1{font-size:1.5rem}h2{font-size:1.15rem;margin-top:2rem;border-bottom:1px solid var(--line)}
table{border-collapse:collapse;width:100%;font-size:.9rem;margin:.5rem 0}
th,td{border-bottom:1px solid var(--line);padding:4px 6px;text-align:left}
td.n,th.n{text-align:right;font-variant-numeric:tabular-nums}
.muted{color:var(--muted)}.warn{color:var(--warn)}
.chart{width:100%;height:auto}.lbl,.val{font-size:12px;fill:var(--fg)}.bar{fill:var(--bar)}
.s0{fill:var(--s0)}.s1{fill:var(--s1)}.s2{fill:var(--s2)}.s3{fill:var(--s3)}.s4{fill:var(--s4)}
.wrap{overflow-x:auto}code{font-size:.85rem}
p,li{max-width:70ch}h3{font-size:1rem;margin:1.2rem 0 .3rem}
.finding{border:1px solid var(--line);border-radius:6px;padding:8px 12px;margin:.8rem 0}
.badge{display:inline-block;border:1px solid currentColor;border-radius:4px;padding:0 5px;
font-size:.78rem;font-weight:600;letter-spacing:.02em}
dl.f{display:grid;grid-template-columns:max-content 1fr;gap:2px 12px;margin:.3rem 0}
dl.f dt{font-weight:600}dl.f dd{margin:0}
"""


def _finding_html(f: dict[str, Any]) -> str:
    rows = [[r["id"], r["text"], r["status"], "; ".join(r["supports"]) or "none",
             "; ".join(r["weakens"]) or "none"] for r in f["readings"]]
    prov = f["provenance"]
    return (f'<section class="finding"><h3>{e(f["subject"])} '
            f'<span class="badge">coverage {e(f["coverage"])}</span></h3>'
            f'<dl class="f"><dt>Observed</dt><dd>{e(f["observed"])}</dd></dl>'
            + _table("Readings (status is an evidence label, not a probability)",
                     ["reading", "meaning", "status", "supported by", "weakened by"], rows,
                     set())
            + f'<dl class="f"><dt>Decision</dt><dd><span class="badge">{e(f["decision"])}'
              f'</span> {e(f["decision_reason"])}</dd>'
              f'<dt>Would change it</dt><dd>{e("; ".join(f["would_change"]))}</dd>'
              f'<dt>Provenance</dt><dd class="muted">parser {e(prov.get("parser"))}, '
              f'{e(prov.get("files_read"))} files read, {e(prov.get("unread_gaps"))} unread '
              f'gaps, window {e(prov.get("window"))}</dd></dl></section>')


def findings_section(findings: list[dict[str, Any]], detail: int = 8) -> str:
    """Observed, readings, decision, what would change it (ARI2-R10). No-use findings are
    summarised by decision with the first `detail` shown in full."""
    if not findings:
        return "<p>No finding.</p>"
    out: list[str] = []
    general = [f for f in findings if not f["id"].startswith("F-nouse-")]
    nouse = [f for f in findings if f["id"].startswith("F-nouse-")]
    out.extend(_finding_html(f) for f in general)
    if nouse:
        by: dict[str, int] = defaultdict(int)
        for f in nouse:
            by[f["decision"]] += 1
        out.append(f"<h3>Capabilities with no observed use: {len(nouse)}</h3><p>"
                   + ", ".join(f"{e(d)}: {n}" for d, n in sorted(by.items()))
                   + ". Each keeps five readings (unused, coverage gap, indirect use, not "
                     "exposed, substituted); removal advice needs the coverage and exposure "
                     "readings weakened.</p>")
        out.extend(_finding_html(f) for f in nouse[:detail])
    return "".join(out)


def _table(caption: str, head: list[str], rows: list[list[Any]], numeric: set[int]) -> str:
    th = "".join(f'<th class="{"n" if i in numeric else ""}">{e(h)}</th>'
                 for i, h in enumerate(head))
    body = "".join("<tr>" + "".join(f'<td class="{"n" if i in numeric else ""}">{e(c)}</td>'
                                     for i, c in enumerate(r)) + "</tr>" for r in rows)
    return (f'<div class="wrap"><table><caption class="muted">{e(caption)}</caption>'
            f"<thead><tr>{th}</tr></thead><tbody>{body}</tbody></table></div>")


def render_html(ev: EvidenceSet, burn: dict[str, Any] | None, audit: dict[str, Any] | None,
                hypotheses: list[HypothesisSet], lessons: list[dict[str, Any]],
                generated: datetime, findings: list[dict[str, Any]] | None = None) -> str:
    sc = start_contexts(ev)
    agent_rows = sorted(((t, v) for t, v in sc.items() if v),
                        key=lambda tv: -statistics.median(tv[1]))
    out: list[str] = [
        "<!doctype html><html lang=\"en\"><head><meta charset=\"utf-8\">",
        "<meta name=\"viewport\" content=\"width=device-width,initial-scale=1\">",
        "<meta http-equiv=\"Content-Security-Policy\" content=\"default-src 'none'; "
        "style-src 'unsafe-inline'; img-src data:\">",
        "<title>Agent Runtime Report</title>", f"<style>{CSS}</style></head><body>",
        "<h1>Agent Runtime Report</h1>",
        f'<p class="muted">ARI {e(ARI_VERSION)}, generated {e(generated.isoformat())}. '
        f"Window {e(ev.window[0] or 'all on disk')} to {e(ev.window[1] or 'now')}. "
        f"{len(ev.sources)} transcript files, {ev.coverage.requests:,} deduplicated requests. "
        "Units are transcript tokens, not subscription quota units. Read only; offline.</p>",
    ]
    if findings is None and audit:
        findings = list(audit.get("findings", []))
    out.append("<h2>What the evidence supports</h2>")
    out.append('<p class="muted">Each finding separates what was observed from what it may '
               "mean, names the decision the evidence justifies and what would change it.</p>")
    out.append(findings_section(findings or []))
    out.append("<h2>Agent types: starting context</h2>")
    out.append('<p class="muted">First API request of each subagent run: input plus cache '
               "writes plus cache reads. Median per type.</p>")
    out.append(_svg_hbars("Median starting context per agent type (thousand tokens)",
                          [(t, statistics.median(v) / 1000) for t, v in agent_rows], "k"))
    out.append(_table("Starting context per agent type",
                      ["agent type", "runs", "median", "p90"],
                      [[t, len(v), f"{statistics.median(v) / 1000:.1f}k",
                        f"{percentile(v, 0.9) / 1000:.1f}k"] for t, v in agent_rows], {1, 2, 3}))
    out.append("<h2>Starting context distribution</h2>")
    dist = []
    for t, v in agent_rows:
        counts = [sum(1 for x in v if lo <= x < hi) for lo, hi in BINS]
        dist.append([t] + counts)
    out.append(_table("Runs per starting context band", ["agent type"] + BIN_LABELS, dist,
                      set(range(1, 7))))
    out.append("<h2>Weekly measured usage and forecast</h2>")
    if burn:
        fc = burn.get("forecast", {})
        out.append(f"<p>{e(burn['units'])}. Period {e(burn['period']['start'][:16])} to "
                   f"{e(burn['period']['end'][:16])}. Observed so far "
                   f"<strong>{burn['observed']['metric_total']:,}</strong>.</p>")
        if fc.get("status") == "OK":
            env = fc["envelope"]
            out.append(f"<p>Forecast period total {fc['projected_period_total']:,} "
                       f"(envelope {env['low']:,} to {env['high']:,}; {e(fc['model'])}; "
                       f"{e(env['method'])}). Naive persistence "
                       f"{fc['naive_persistence_total']:,}.</p>")
        else:
            out.append(f'<p class="warn">Forecast: {e(fc.get("status"))} '
                       f"({e(fc.get('reason', ''))})</p>")
        bt = burn.get("backtest", {})
        if bt.get("status") == "OK":
            out.append(f"<p>Backtest over {bt['points']} cuts: MAE trailing mean "
                       f"{bt['mae_trailing_mean']:,}, persistence {bt['mae_persistence']:,} "
                       f"(better: {e(bt['better'])}).</p>")
        elif bt:
            out.append(f"<p class=\"muted\">Backtest: {e(bt.get('status'))}.</p>")
        out.append(f"<p><strong>{e(burn['provider_quota'])}</strong></p>")
        daily = burn.get("daily", [])
        out.append(_svg_hbars("Daily measured tokens (millions)",
                              [(d["day"], d["value"] / 1e6) for d in daily], "M"))
    else:
        out.append("<p>Not computed.</p>")
    out.append("<h2>Cache breakdown per agent type</h2>")
    keys = ["input", "cache write 5m", "cache write 1h", "cache read", "output"]
    agg: dict[str, list[float]] = defaultdict(lambda: [0.0] * 5)
    for u in ev.usage:
        row = agg[u.agent_type]
        for i, tok in enumerate((u.input_tokens, u.cache_creation_5m, u.cache_creation_1h,
                                 u.cache_read, u.output)):
            row[i] += tok
    stacked = sorted(agg.items(), key=lambda kv: -sum(kv[1]))[:12]
    out.append(_svg_stacked("Share of each token category per agent type", stacked, keys))
    out.append(_table("Token categories per agent type (all requests in the window)",
                      ["agent type"] + keys,
                      [[t] + [f"{int(v):,}" for v in vals] for t, vals in stacked],
                      set(range(1, 6))))
    out.append("<h2>Skills and MCP audit</h2>")
    if audit:
        recs = [r for r in audit["recommendations"] if r["action"] != "KEEP"]
        acts: dict[str, int] = defaultdict(int)
        for r in audit["recommendations"]:
            acts[r["action"]] += 1
        out.append("<p>" + ", ".join(f"{e(a)}: {n}" for a, n in sorted(acts.items())) +
                   ". Recommendations only; nothing was changed.</p>")
        out.append(_table("Findings other than KEEP (first 60)",
                          ["action", "category", "target", "reason", "benefit", "validation",
                           "rejected because"],
                          [[r["action"], r["category"], r["target"], r["reason"],
                            r["expected_benefit"], r["validation_method"], r["rejected"] or ""]
                           for r in recs[:60]], set()))
        out.append("<h2>Configuration drift</h2>")
        d = audit["drift"]
        if d.get("status") == "OK":
            out.append(_table(f"Exposed sets, {d['from'][:10]} to {d['to'][:10]}",
                              ["category", "added", "removed"],
                              [[c, ", ".join(ch["added"][:15]) or "none",
                                ", ".join(ch["removed"][:15]) or "none"]
                               for c, ch in d["changes"].items()], set()))
            out.append('<p class="muted">An exposure change is a discovery trigger, not the '
                       "cause of any context change.</p>")
        else:
            out.append(f"<p>{e(d.get('status'))}</p>")
    else:
        out.append("<p>Not computed.</p>")
    out.append("<h2>Evidence quality and unknowns</h2>")
    cov = coverage_dict(ev.coverage)
    out.append(_table("Parser coverage", ["field", "value"],
                      [[k, v] for k, v in cov.items()], {1}))
    for w in ev.coverage.warnings():
        out.append(f'<p class="warn">{e(w)}</p>')
    out.append("<h2>Hypotheses and proposed experiments</h2>")
    if not hypotheses and not lessons:
        out.append("<p>No open hypothesis.</p>")
    for hs in hypotheses:
        out.append(f"<p><strong>{e(hs.observation)}</strong><br>State {e(hs.state)}, "
                   f"primary {e(hs.primary or 'none preferred')}, confidence "
                   f"{e(hs.confidence)}. Next: {e(hs.next_action)}</p>")
        out.append(_table("Competing explanations", ["id", "claim", "supporting",
                                                     "discriminating test", "state"],
                          [[x.id, x.claim, "; ".join(x.supporting) or "none", x.test, x.state]
                           for x in hs.explanations], set()))
    if lessons:
        out.append(_table("Nightly lessons", ["id", "state", "claim", "proposed experiment"],
                          [[ls["id"], ls["state"], ls["claim"], ls.get("proposed_experiment", "")]
                           for ls in lessons[:40]], set()))
    out.append("</body></html>")
    return "\n".join(out)


def _bucket(n: int) -> str:
    for lo, hi, label in ((5, 10, "5-9"), (10, 50, "10-49"), (50, 100, "50-99")):
        if lo <= n < hi:
            return label
    return "100+" if n >= 100 else "<5"


def build_share(ev: EvidenceSet, k: int = 5) -> dict[str, Any]:
    groups: dict[str, list[int]] = defaultdict(list)
    for t, vals in start_contexts(ev).items():
        groups[t if t in BUILTIN_TYPES else "custom"].extend(vals)
    rows, suppressed = [], 0
    for t, vals in sorted(groups.items()):
        if len(vals) < k:
            suppressed += 1
            continue
        rows.append({"agent_type": t, "runs_bucket": _bucket(len(vals)),
                     "median_start_k": round(statistics.median(vals) / 1000),
                     "p90_start_k": round(percentile(vals, 0.9) / 1000)})
    versions = sorted({".".join(u.version.split(".")[:2]) for u in ev.usage
                       if re.fullmatch(r"\d{1,3}\.\d{1,3}(\.\d{1,4})*", u.version)})
    return {"schema": SHARE_SCHEMA, "ari_version": ARI_VERSION, "min_group_runs": k,
            "claude_code_minor_versions": versions, "agent_types": rows,
            "suppressed_groups": suppressed,
            "note": SHARE_NOTE}


_ALLOWED_TOP = {"schema", "ari_version", "min_group_runs", "claude_code_minor_versions",
                "agent_types", "suppressed_groups", "note"}
_ALLOWED_ROW = {"agent_type", "runs_bucket", "median_start_k", "p90_start_k"}
_FORBIDDEN = [
    (re.compile(r"[/\\]"), "path separator"),
    (re.compile(r"[\w.+-]+@[\w-]+\.[\w.]+"), "email address"),
    (re.compile(r"\d{4}-\d{2}-\d{2}"), "date"),
    (re.compile(r"\b[0-9a-f]{12,}\b", re.IGNORECASE), "long hex identifier"),
    (re.compile(r"(sk-|ghp_|xox[bp]-|AKIA)", re.IGNORECASE), "secret-like token"),
]


def _count(v: Any, floor: int = 0) -> bool:
    return isinstance(v, int) and not isinstance(v, bool) and v >= floor


_BUCKETS = {"5-9", "10-49", "50-99", "100+"}


def screen_share(obj: dict[str, Any]) -> list[str]:
    """Structural whitelist, exact fixed text, typed values, then a pattern scan of the
    whole object. Returns problems; empty means it passed. Never raises on any JSON value."""
    if not isinstance(obj, dict):
        return ["share is not a JSON object"]
    problems: list[str] = []
    if set(obj) != _ALLOWED_TOP:
        problems.append(f"keys differ from the allowlist: extra "
                        f"{sorted(map(str, set(obj) - _ALLOWED_TOP))}, missing "
                        f"{sorted(_ALLOWED_TOP - set(obj))}")
    if obj.get("schema") != SHARE_SCHEMA:
        problems.append("schema is not " + SHARE_SCHEMA)
    if obj.get("note") != SHARE_NOTE:
        problems.append("note is not the fixed note text")
    if not (isinstance(obj.get("ari_version"), str)
            and re.fullmatch(r"\d{1,3}\.\d{1,3}\.\d{1,4}", obj["ari_version"])):
        problems.append("ari_version is not major.minor.patch")
    if not _count(obj.get("min_group_runs"), 5):
        problems.append("min_group_runs is not an integer of at least 5")
    if not _count(obj.get("suppressed_groups")):
        problems.append("suppressed_groups is not a non negative integer")
    versions = obj.get("claude_code_minor_versions")
    if not isinstance(versions, list):
        problems.append("claude_code_minor_versions is not a list")
    elif not all(isinstance(v, str) and re.fullmatch(r"\d{1,3}\.\d{1,3}", v)
                 for v in versions):
        problems.append("version is not a major.minor string")
    rows = obj.get("agent_types")
    if not isinstance(rows, list):
        problems.append("agent_types is not a list")
        rows = []
    for row in rows:
        if not isinstance(row, dict):
            problems.append("agent_types row is not an object")
            continue
        if set(row) != _ALLOWED_ROW:
            problems.append(f"row keys differ from the allowlist {sorted(_ALLOWED_ROW)}")
        if row.get("agent_type") not in BUILTIN_TYPES | {"custom"}:
            problems.append("agent type outside the allowlist")
        if row.get("runs_bucket") == "<5":
            problems.append("sparse group not suppressed")
        elif row.get("runs_bucket") not in _BUCKETS:
            problems.append("runs_bucket is not a bucket label")
        if not (_count(row.get("median_start_k")) and _count(row.get("p90_start_k"))):
            problems.append("start sizes are not non negative integers")
    # schema and note were compared exactly above; the schema holds a slash by design
    text = json.dumps({k: v for k, v in obj.items() if k not in ("note", "schema")})
    for pat, label in _FORBIDDEN:
        if pat.search(text):
            problems.append(f"contains a {label}")
    return problems
