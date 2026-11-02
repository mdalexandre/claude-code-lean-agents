# ARI SRD v0.1.0: Agent Runtime Intelligence

Status: implemented locally on branch `ari-v0.1.0`; not pushed, not released.
Mission: ARI-001. Methods: recursive conceptual expansion (graph in `ari-concept-graph.json`), open ended recursive abductive discovery (`scripts/ari/abduction.py`), validation gated productionization (`docs/ari/verification.md`).

## 1. Baseline (Gate A)

| Item | Value |
| --- | --- |
| Repository | `mdalexandre/claude-code-lean-agents`, local clone |
| Baseline commit | `c08376cc80641ab75affe701a321dfebc861f78b` (main, in sync with origin) |
| Dirty state at start | clean working tree (`git status --short` empty); only ignored caches present |
| Python | 3.12.3 locally; CI uses 3.12; standard library only at runtime |
| Test infrastructure | pytest (`tests/`), ruff `E,F,I` line length 100, `mypy --strict`, GitHub Actions `tests.yml` |

Baseline file hashes (sha256):

| File | sha256 |
| --- | --- |
| scripts/measure_subagents.py | c0cd21d28f22b792f1c992fd8054d310483ffb7906ca1364601659d02f82c64e |
| scripts/make_visuals.py | 9432a760a50b741477400f74fa8491560bb4a90dc31e491c91bf683ca1df07c1 |
| hooks/lean_dispatch_nudge.py | 6c0b9df833053e842d18fe7bd80dc9e84e434eddf13f78f741d40b74e641a992 |
| agents/lean-worker.md | 9b4b8f0f694d96b18c08b955dd74a68e03dee1a20660efdad4d68f6cdb4ca573 |
| agents/lean-checker.md | 4bf1510d44ba914d47567d78e1e97a1002d7e473a92bc1813c528a04b8b20153 |
| tests/test_lean_dispatch_nudge.py | f2426dd21006d1bc75fec7838dedb41b37b7dde2df025c17882763942e0b7732 |

Existing behavior mapped:
* `measure_subagents.py` globs `<root>/*/*/subagents/*.meta.json`, reads `agentType` (missing maps to `general-purpose`), takes the first assistant line with usage in file order, and prints median and p90 of input plus cache tokens per type. It does not deduplicate (it reads one line) and filters by file mtime.
* `lean_dispatch_nudge.py` is a PreToolUse hook for the Agent tool; ARI does not touch it.
* The four files above stay byte identical in this release (checked in verification).

## 2. Scope

In: the four capabilities (weekly burn forecast, skills and MCP audit, offline HTML with an opt in share file, nightly learning), the shared evidence engine and the abduction store.
Out (not authorized by the mission): pushing, releases, posting on X, uninstalling or disabling anything, uploading data, installing schedules, editing the user's Claude configuration.

## 3. Units and definitions

* Token categories are kept apart: `input_tokens`, `cache_creation_5m`, `cache_creation_1h`, `cache_read`, `output`.
* `context` = input + cache writes + cache reads of one request. `total` = context + output, an unweighted sum labelled as such everywhere it appears.
* Request = one `requestId` (fallback `message.id`).
* Starting context = context of the earliest request of a subagent run.
* All times UTC. Day keys can be shifted with `--tz-offset-minutes`.
* These are transcript tokens, not subscription quota units.

## 4. Requirements

| ID | Requirement | Status |
| --- | --- | --- |
| ARI-R1 | One ingestion layer (`scripts/ari/evidence.py`) feeds every tool; event schema `ari-event/1` | SPECIFIED |
| ARI-R2 | Streamed copies of one request count once; copies with different usage are flagged as conflicts and merged by per field maximum (repair R2, 2026-11-01: first kept undercounted streamed output) | SPECIFIED |
| ARI-R3 | Records without a request id, with invalid timestamps, invalid token fields or the `<synthetic>` model are skipped and counted, never estimated | SPECIFIED |
| ARI-R4 | A last line without newline is a partial write and is skipped; files modified within a quiet period are flagged (skipped by the nightly run) | SPECIFIED |
| ARI-R5 | Files whose real path leaves a configured root are refused and counted | SPECIFIED |
| ARI-R6 | Missing `agentType` is reported as `unknown`, never assumed to be `general-purpose` | SPECIFIED |
| ARI-R7 | Events keep no prompt text, tool arguments or file paths; tool events keep tool, skill and MCP server names only | SPECIFIED |
| ARI-R8 | `measure_subagents.py` output is unchanged; regression tests pin it | SPECIFIED |
| ARI-R9 | Weekly burn reports usage by agent type, daily and weekly trends, requests, cache categories, concentration, forecast with envelope, coverage warnings; text and JSON | SPECIFIED |
| ARI-R10 | Forecast refuses with fewer than 3 complete history days | SPECIFIED |
| ARI-R11 | Backtest compares the trailing mean with persistence using only data before each cut | SPECIFIED |
| ARI-R12 | Provider quota ETA is always printed as `Provider quota ETA: unavailable`; a `--budget` is labelled user defined; no 5 hour cap is assumed | SPECIFIED |
| ARI-R13 | Audit separates installed, exposed, explicitly invoked, indirectly used (skill file read) and unobserved capabilities | SPECIFIED |
| ARI-R14 | MCP configs are read for server names only; values are never output | SPECIFIED |
| ARI-R15 | Consequential recommendations carry benefit, evidence, capability loss risk and a validation method | SPECIFIED |
| ARI-R16 | The audit changes no file or setting | SPECIFIED |
| ARI-R17 | Removal or exposure restriction needs zero explicit and zero indirect use, at least 20 exposed sessions and 14 days; otherwise INVESTIGATE or INSUFFICIENT_EVIDENCE with the rejection reason | SPECIFIED |
| ARI-R18 | HTML report is one file, no script, no external resource, CSP `default-src 'none'`, every dynamic string escaped, sections as listed in the mission | SPECIFIED |
| ARI-R19 | `--share` writes a local file only; no module imports a network library | SPECIFIED |
| ARI-R20 | Share file: versioned schema, groups under 5 runs suppressed (floor not lowerable), custom agent names folded into `custom`, no paths, ids, project names or timestamps, a structural and pattern screen that refuses to write on failure; described as not proven anonymous | SPECIFIED |
| ARI-R21 | Nightly run is opt in, deterministic, idempotent and recoverable after interruption | SPECIFIED |
| ARI-R22 | Nightly lessons stop at HYPOTHESIZED; each later state has a promotion contract; a failed test rejects and keeps the lesson | SPECIFIED |
| ARI-R23 | Every observation keeps a primary hypothesis, at least one alternative, supporting and conflicting evidence, a discriminating test, explicit confidence, state and next action; recursion bounded at depth 3 and 24 nodes | SPECIFIED |
| ARI-R24 | Scheduling examples are documented, never installed | SPECIFIED |
| ARI-R25 | A governance note separates reply categories and applies X's written approval rule for AI reply bots | SPECIFIED |
| ARI-R26 | Repair round 4 (2026-11-01, after an independent blind review): workflow subagent transcripts read and unknown `.jsonl` files warned; message.id aliasing; no path text in skill events; namespaced skill names; symlinked SKILL.md refused; positive `--period-days`; history floor counts days with usage; partial first day excluded; snapshots never shrink; checker identity case insensitive; TESTED needs a result recorded after CANDIDATE; empty runs warn | SPECIFIED |
| ARI-R27 | Repair round 5 (2026-11-01, after a second independent blind review): copies linking requestId and message.id count once; earliest copy dates a request; oversize lines warned; slash, Skill and Read names kept only for installed or listed skills; symlinked file inside a skill folder counts as indirect use; removal advice blocked by unread gaps unless `--accept-coverage-gaps`; every CLI number and time range checked (exit 2); arithmetic period roll forward, 1 hour to 366 days; warning when the read window opens after the period start; share versions limited to `\d{1,3}.\d{1,3}`; blank checker refused and identity ignores all spacing and invisible characters; no lesson from a run that skipped active files; nightly CLI never tracebacks; report and nightly print coverage warnings | SPECIFIED |
| ARI-P1 | Manual quota indicator (a percentage read from the Claude UI with source and timestamp) | PROPOSED, not implemented |
| ARI-P2 | Optional model assisted nightly analysis with explicit config, privacy boundary and spend cap | PROPOSED, not implemented |
| ARI-P3 | Publishing share files to a public dataset | PROPOSED, needs a separate owner decision |
| ARI-P4 | Rewriting `measure_subagents.py` on top of the engine | PROPOSED; deferred because its output would change (first line in file order versus earliest timestamp, `general-purpose` versus `unknown`) |

## 5. Interfaces

```
python3 scripts/weekly_burn.py   [--root DIR]... [--days 28] [--now ISO] [--metric total|context|input_tokens|cache_creation|cache_read|output] [--period-start ISO] [--period-days 7] [--budget N] [--tz-offset-minutes M] [--json]
python3 scripts/skill_mcp_audit.py [--root DIR]... [--days 28] [--skills-root DIR]... [--mcp-config FILE]... [--min-sessions 20] [--min-days 14] [--json]
python3 scripts/ari_report.py --out FILE.html [--share FILE.json] [--share-min-runs 5] [--state-dir DIR] [--root DIR]... [--days 28]
python3 scripts/ari_nightly.py run --state-dir DIR [--days 14] [--quiet-seconds 1800]
python3 scripts/ari_nightly.py status|promote|record-result --state-dir DIR ...
```

Schemas: `ari-event/1`, `ari-weekly-burn/1`, `ari-capability-audit/1`, `ari-share/1`, `ari-hypothesis-set/1`, `ari-learn-state/1`, `ari-concept-graph/1`.

## 6. Assumptions (each labelled)

* The transcript layout and attachment record types are internal to Claude Code (OBSERVED on version 2.1.316 on this machine); a format change shows up as coverage warnings, not as silent zeros.
* Streamed copies of one request carry the same input and cache numbers (OBSERVED in a sample: 0 of 6 duplicate groups disagreed). The engine still detects disagreement.
* Transcripts older than `cleanupPeriodDays` (default 30) are deleted by Claude Code, so history is bounded by what is on disk.
* Skill listing token cost is estimated as characters / 4 (JUDGMENT, labelled as an estimate in output).

## 7. Failure modes and handling

| Failure | Handling |
| --- | --- |
| Empty or swept transcript root | `no countable request found: this is missing data, not zero usage` |
| Live file being written | partial last line skipped; nightly skips files inside the quiet period |
| Unreadable file | counted in `files_unreadable`, warning printed |
| Symlink escaping the root | refused, counted |
| Format drift | malformed line counters and zero exposure records; audit says UNKNOWN |
| Too little history | forecast INSUFFICIENT_DATA, backtest INSUFFICIENT_HISTORY |
| Share screen failure | nothing written, exit 1 |
| Crash during nightly save | atomic replace keeps the old state; journal triggers recovery next run |

## 8. Acceptance criteria

| ID | Criterion | Check |
| --- | --- | --- |
| AC-A | Baseline recorded, four baseline files byte identical after the build | `sha256sum` against section 1 |
| AC-B | Every capability node traces to a problem, a measurement and a test file | `tests/test_ari_concept_graph.py` |
| AC-D | Dedup, conflicts, skips, partial lines, symlinks, unknown agent type, window bounds, timezone handling; legacy CLI output pinned | `tests/test_ari_evidence.py`, `tests/test_measure_subagents_regression.py` |
| AC-E | Known rate forecast reproducible; insufficient data refused; units, quota and budget labels; no leakage in backtest; naive baseline compared | `tests/test_ari_forecast.py` |
| AC-F | Inventory and usage distinctions on fixtures; unsupported removal rejected; config values never output; no file changed | `tests/test_ari_audit.py` |
| AC-G | HTML offline and escaped; share screened, suppressed, local; no network import; network calls refused at runtime | `tests/test_ari_report.py` |
| AC-H | Idempotent nightly; promotion contracts; rejection keeps lessons; interrupted run recovers | `tests/test_ari_learn.py` |
| AC-I | Lint, strict types and the full test suite pass through a qualified procedure with negative controls | `docs/ari/verification.md` |
| AC-REAL | Each CLI runs on the real local transcripts read only and its output is inspected | `docs/ari/verification.md` |
