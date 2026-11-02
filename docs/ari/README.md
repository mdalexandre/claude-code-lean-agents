# Agent Runtime Intelligence (ARI) 2.0.0

Five read only tools that measure how your Claude Code sessions and subagents actually ran, from the transcripts Claude Code already keeps on your machine. Standard library Python (tested on 3.12). Nothing is sent anywhere, nothing in your configuration is changed, and nothing is scheduled.

| Tool | What it answers |
| --- | --- |
| `scripts/weekly_burn.py` | How many transcript tokens did each agent type use this week, and where is the week heading? |
| `scripts/skill_mcp_audit.py` | Which skills and MCP servers are installed, which are exposed to sessions, which are actually used? |
| `scripts/ari_report.py` | One offline HTML page with all of it, plus an optional screened summary you could choose to share |
| `scripts/ari_nightly.py` | Opt in nightly snapshots, drift detection and candidate lessons that need tests before anyone relies on them |
| `scripts/ari_discover.py` | Bounded abductive discovery over the known ARI anomalies on your own transcripts: competing explanations, discriminating tests, evidence files bound by sha256 |

All five use one parser, `scripts/ari/evidence.py`. The existing `scripts/measure_subagents.py` is unchanged.

## Quick start

```bash
cd scripts
python3 weekly_burn.py                      # this ISO week, text
python3 weekly_burn.py --budget 5000000000  # forecast against a budget you choose
python3 skill_mcp_audit.py
python3 ari_report.py --out ~/ari-report.html
python3 ari_report.py --out ~/ari-report.html --share ~/ari-share.json
python3 ari_nightly.py run --state-dir ~/.ari-state
```

On a heavy setup (about 33 GB of transcripts, about 16,000 files) a 7 day burn run took 24.8 seconds and 117 MB of peak memory, and 28 day runs took 75 to 127 seconds and up to 1.01 GB (measured 2026-11-01, see `docs/ari/verification.md`). Use `--days` to shorten the window.

## Units: read this before quoting a number

* Every number is a transcript token count as Claude Code logged it, split into `input_tokens`, `cache_creation_5m`, `cache_creation_1h`, `cache_read` and `output`.
* `total` is the unweighted sum of those categories. Cache reads usually dominate it. It is not a cost and not a quota unit.
* Anthropic does not publish how subscription limits map to tokens, so ARI always prints `Provider quota ETA: unavailable`. `--budget` lets you forecast against your own number, and the output calls it a user defined budget.
* ARI assumes no 5 hour session cap; some plans have none.

## How the parser counts

* One API request is written as several assistant lines that share a `requestId`. It is counted once. Copies with different usage numbers are flagged as conflicts and each field keeps its largest value: later streamed copies carry the final output count of a partial earlier copy, and a resumed session can repeat a request with zeroed usage. On one 28 day real history, keeping the first copy instead would have dropped 4.4% of subagent output tokens.
* A record with no request id, no valid timestamp, invalid token fields or the `<synthetic>` model is skipped and counted, never estimated.
* A last line without a newline is a partial write and is skipped. Since 2.0 the nightly run reads files written in the last 30 minutes too (complete lines only) and reports how many were active.
* A subagent with no `agentType` in its `.meta.json` is reported as `unknown`. (The older `measure_subagents.py` reports it as `general-purpose`; it is left unchanged for compatibility.)
* Workflow subagents (`subagents/workflows/wf_*/agent-*.jsonl`) are read like other subagents. Every other `.jsonl` file under the root is classified, never silently skipped (2.0):
  * `workflow_journal`: a `journal.jsonl` beside workflow agents. It is an index, not a transcript; each agent it says was started must have its own transcript, and each missing one is counted as a coverage gap.
  * `foreign`: a full scan finds no transcript marker (usage, tool use, command, attachment), so it cannot hold a request or a tool call. Excluded, and counted.
  * `unknown`: it carries markers in a layout the parser does not know. A coverage gap, with a warning.
  * `outside_root` and `unreadable`: refused or failed, counted.
* Each record's `version` is mapped to a transcript format (`major.minor`). Only `2.1` is verified; records in any other format are still read and counted in a warning. Unknown record types are counted too.
* Copies of one request are classed (2.0): `IDENTICAL`, `STREAM_GROWTH` (only output differs), `ZEROED_COPY` (a resumed session's zero usage copy) or `AMBIGUOUS` (copies differ in input or cache fields for no known reason). Ambiguous requests give the total a band: the lower bound counts the largest single copy, the upper bound the per field maximum ARI reports. A band is never called a verified deduplicated total.
* Copies of one request are matched by `requestId` or `message.id`; a copy that carries both links the two, so later copies with either id count once. The earliest copy dates the request.
* A transcript whose real path leaves the root you gave (for example a symlink) is refused, and a directory link that leaves the root is counted as outside the root at any depth. One hostile record (JSON nested past the stack, an integer of thousands of digits, a timestamp pushed past year 9999 by its offset, a journal agent id that is not a short plain name) is counted as malformed and skipped; it never stops the run. The skill inventory also refuses a `SKILL.md` whose real path leaves its skills root.
* Events keep tool, skill and MCP server names only: no prompt text, tool arguments or file paths. A name typed after a slash or passed to the Skill tool is kept only when it is an installed skill folder or a listed skill; anything else is dropped. A read names a skill only when the file sits inside an installed skill's folder (as written, so a symlinked file inside the folder still counts).
* Listed skill names keep plugin namespaces (`stripe:test-cards`).

Every run prints coverage warnings (unreadable files, malformed or oversize lines, conflicts, unknown agent types): the burn and audit text output, the report command and the nightly run (on stderr) all print them. An empty result is reported as missing data, not as zero usage. Files last modified before the window start are not opened: they cannot hold a record from inside the window. Usage, tool calls and skill or MCP listings all obey the same window, so a fixed `--now` gives the same audit on a later day; a record with no timestamp cannot be placed and is kept.

## Forecast method

Rate = mean of the last 7 complete days; idle days count as 0. The day the `--days` window opens in the middle of is not a complete day and is left out. Projection = observed so far + rate x remaining days. The envelope is observed so far + the 10th and 90th percentile day x the remaining days: it is deliberately wide and is not a confidence interval. A persistence baseline (yesterday x remaining days) is printed beside it, and a backtest reports which of the two had the lower mean absolute error on your own history, using only data before each cut. The forecast is refused with fewer than 3 complete days that have usage. `--period-days` must be from 1 hour to 366 days, and the burn and audit numbers and times on the command line are range checked, with a bad value refused with exit 2. `ari_nightly.py run --quiet-seconds` must be a finite number of at least 0. When `--days` opens after the period start, a warning says the observed total misses that part of the period.

## Audit method

| Status | Meaning |
| --- | --- |
| INVOKED | Skill tool call, slash command, or `mcp__<server>__` tool call seen |
| INDIRECT_ONLY | No Skill call, but a file inside the skill folder was opened with the Read tool (a `cat` through Bash is not seen) |
| EXPOSED_UNOBSERVED | Listed to sessions, no use seen |
| INSTALLED_NOT_SEEN_EXPOSED | Installed, never seen in a listing (disabled, filtered, or format not recognised) |

`CONSIDER_REMOVAL` (skills) and `RESTRICT_EXPOSURE` (MCP servers) need zero explicit and zero indirect use, at least 20 exposed sessions and at least 14 days of covered window (the command line refuses fewer than 1 session, under 1 day, or a non finite value), and no unread file or line that could hide a use (unreadable, refused, unrecognised, malformed, oversize or partial). With such gaps the advice becomes `INVESTIGATE` until you read the warnings and rerun with `--accept-coverage-gaps`. They always carry the expected benefit, the evidence, the capability loss risk and a validation method. Otherwise the recommendation is downgraded to `INVESTIGATE` or `INSUFFICIENT_EVIDENCE` and the rejection reason is shown. MCP config files are read for server names only. Server names are joined in the form Claude Code uses as the tool prefix (every character outside letters, digits, `_` and `-` becomes `_`), so `plugin:github:github` in a listing and `mcp__plugin_github_github__...` in a tool call are one server. Tools the harness itself names like MCP tools (for example `mcp__SubagentHandback__SubagentHandback`) appear as servers that no config installs. ARI never removes or disables anything.

## Share file

`--share` writes `ari-share/1` to a local file and prints it. It holds per agent type run buckets and rounded starting context (thousands of tokens), Claude Code minor versions and a count of suppressed groups. Groups under 5 runs are suppressed (the floor cannot be lowered), custom agent names become `custom`, and a screen refuses to write the file if it finds a path, an email, a date, a long hex id or a secret like token. The screen also requires exactly the known fields, the fixed `note` and `schema` text, integers where counts belong, real bucket labels and version strings, and refuses anything else, including a share file built by other code than `build_share`. It is aggregated and screened, not proven anonymous. Nothing is uploaded; publishing it is your own separate step.

## Findings (2.0)

Audit and report conclusions are findings (`ari-finding/1`): what was observed, at least two readings of it (for a capability with no observed use: unused, a coverage gap, indirect use, not exposed, substituted), each reading SUPPORTED, WEAKENED, UNRESOLVED or NOT_TESTABLE with the evidence for that state, the decision the evidence justifies, and what would change it. A removal class decision (CONSIDER_REMOVAL, RESTRICT_EXPOSURE, CONSOLIDATE) passes two gates: the recommendation validator and the finding contract, which refuses it while the coverage gap reading is SUPPORTED (or UNRESOLVED without `--accept-coverage-gaps`) or, for a single capability, while it was never seen exposed or has no exposure reading at all. A coverage gap reading or a COMPLETE coverage state that denies the unread gaps counted in its provenance is refused, and a decision outside the known vocabulary is refused. A CONSOLIDATE finding also keeps a NOT_TESTABLE reading that the servers reach different accounts. The HTML report shows the findings in a "What the evidence supports" section, escaped, with no script and no external resource.

## Nightly lessons

```
DISCOVERED -> SPECIFIED -> CANDIDATE -> REPLAY_TESTED -> QUALIFIED -> APPROVAL_PENDING -> PROMOTED
(and QUARANTINED, kept with its evidence)
```

The 0.1 names still work as aliases (`HYPOTHESIZED` is SPECIFIED, `TESTED` is REPLAY_TESTED, `VALIDATED` is QUALIFIED, `RETAINED` is PROMOTED, `REJECTED` is QUARANTINED). A 0.1 state file is migrated on first write and the original is kept as `state.json.v1.bak`.

The nightly run stores one snapshot per complete day that has usage, never shrinks a stored day (a later run that sees fewer records, for example after `cleanupPeriodDays` deleted files, keeps the fuller one), warns when it saw no usage, and stops at SPECIFIED. Eligibility is per source (2.0): every transcript gets a checkpoint (size, complete line offset, a hash of its first 64 KiB and of the 4 KiB before the offset) and each run classes it NEW, UNCHANGED, GROWN, TRUNCATED or REPLACED. A file deleted between runs is not visited (a stored day never shrinks, so it needs no class). A day touched by a truncated or replaced file is recomputed and marked unstable, and no lesson is drawn from it, nor is it used as a baseline day for another day, until a later run sees it stable. An open session no longer blocks lessons for every other day. Reruns with the same evidence change nothing, and an interrupted run recovers to the same state as a clean one.

A lesson carries its trigger, the transcript versions it was seen on, the content hashes of its evidence days and a rollback note. `promote` advances one step at a time and enforces: CANDIDATE needs a proposed experiment, an alternative and a rollback note; REPLAY_TESTED runs the replay (`ari_nightly.py replay`), which checks the evidence hashes, that the trigger still reproduces with the same sign, and that the baseline days stay quiet, and quarantines the lesson if any check fails; QUALIFIED needs a current replay (same ARI code hash and parser version), a supporting result recorded after CANDIDATE, a checker other than the author and an alternative rejected by a test; APPROVAL_PENDING needs a current replay; PROMOTED needs an approval file (`--approval`, schema `ari-lesson-approval/1`) naming the lesson id, its content hash and the approver. `--owner-approved` alone is not enough. The approval hash is compared with a hash recomputed from the lesson's fields at promotion, and replay recomputes each evidence day's hash from its content, so a lesson or a day edited after the fact is refused. The approval file does not authenticate a person: whoever can edit both the state file and the approval file controls approval. Promotion changes only the lesson's state in the ARI store: it never writes a procedure registry and never changes a setting. When every explanation is refuted the lesson becomes QUARANTINED and stays in the store. Each anomaly opens a hypothesis set (CLAUDE.md, skills list, MCP names, agent initialisation, version or model, environment) with a discriminating test per explanation; a coincident configuration change is recorded as a signal, never as the cause.

Scheduling examples (launchd, systemd, cron, Windows): `docs/ari/scheduling.md`. None is installed.

## Schemas

`ari-event/1`, `ari-weekly-burn/1`, `ari-capability-audit/1`, `ari-share/1`, `ari-hypothesis-set/1`, `ari-learn-state/2` (reads `/1`), `ari-concept-graph/2`, `ari-finding/1`, `ari-discovery/1`, `ari-lesson-approval/1`, `ari-holons/1`. Requirements and acceptance criteria: `docs/specs/ari-srd-v2.0.0.md` (the frozen 0.1.0 SRD is kept unchanged beside it). Holon contracts: `docs/specs/ari-holons-v2.0.0.json`. Concept graph and backlog: `docs/specs/ari-concept-graph-v2.json`. Verification record: `docs/ari/verification.md`.

## Limits

* The transcript and attachment formats are internal to Claude Code and were observed on version 2.1.316. A format change shows up as coverage warnings and zero exposure records.
* Claude Code deletes transcripts older than `cleanupPeriodDays` (default 30), so history ends there.
* Skill listing token costs are estimates (characters / 4).
* A record whose `usage` key is written with a JSON unicode escape is not seen by the fast line filter. A request whose copies straddle the start of the window is counted in the window. Neither was seen in the real history checked.
* A lesson replay only shows the trigger still reproduces on stored snapshots; replays report `versions_not_covered` when a transcript version appears that the lesson was not seen on. Generalisation across versions is untested.
* Ambiguous duplicate copies cannot be told apart from two different requests sharing an id, because message content is not compared.
* The model assisted nightly layer, a manual quota indicator and publishing share files are proposed, not built.
