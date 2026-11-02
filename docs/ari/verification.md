# ARI 0.1.0 verification record (test procedure 0.1.5)

Recorded 2026-11-01 on branch `ari-v0.1.0` (from `main` at c08376c), Linux, Python 3.12, Claude Code 2.1.x transcripts. Nothing here was pushed, committed, scheduled or uploaded.

## Independence, stated plainly

* The author (the Claude session that wrote the code) ran the build, the unit tests and the real data runs.
* Five fresh `lean-checker` subagents qualified the test procedure. Each one received only the procedure, criteria and store paths plus the qualify command, and none was given the author's verdict. Two other fresh `lean-checker` subagents reviewed the build blind against fixed criteria K1 to K10, and a sixth qualified procedure 0.1.5.
* All of these checkers are the same model family as the author, running in the same session's tooling. That is separation of context, not independent human validation.

## Gate commands (run from the repository root)

```bash
ruff check --select E,F,I --line-length 100 hooks scripts tests
mypy --strict hooks/lean_dispatch_nudge.py scripts/measure_subagents.py scripts/make_visuals.py
(cd scripts && mypy --strict ari weekly_burn.py skill_mcp_audit.py ari_report.py ari_nightly.py)
PYTHONDONTWRITEBYTECODE=1 python3 -m pytest -q -p no:cacheprovider tests
python3 tests/mutation_controls.py        # exit 1 = every mutation caught
python3 scripts/make_visuals.py --check
sha256sum scripts/measure_subagents.py scripts/make_visuals.py hooks/lean_dispatch_nudge.py \
  agents/lean-worker.md agents/lean-checker.md tests/test_lean_dispatch_nudge.py
```

The final results are as follows:

* ruff and both mypy runs are clean.
* 113 tests pass, with 0 skipped.
* The mutation control catches all 9 mutations, and exits 1.
* The visuals check exits 0.
* All six baseline hashes equal the SRD section 1 values.

## Qualified procedure

The procedure was qualified with validation gated test productionization. The store is `~/.vgtp/claude-code-lean-agents`. Versions 0.1.0 to 0.1.4 froze the criteria `ari-suite-v2` (at least 66 tests); 0.1.5 froze `ari-suite-v3` (at least 97 tests, the static count of test declarations after repair round 5). Both allow at most 1 skipped test and require negative controls.

| Version | Tests | Qualified by | Result | Why superseded |
| --- | --- | --- | --- | --- |
| 0.1.0 | 66 | lean-checker-qualifier | QUALIFIED, 3 trials | the checker showed the mutation control counted any pytest exit code and any crash as "caught" |
| 0.1.1 | 66 | lean-checker-qualifier-2 | QUALIFIED | real data showed the dedup rule lost streamed output tokens |
| 0.1.2 | 67 | lean-checker-qualifier-3 | QUALIFIED | real data showed MCP servers split by naming |
| 0.1.3 | 68 | lean-checker-qualifier-4 | QUALIFIED, promoted | blind review found the defects in repair round 4 |
| 0.1.4 | 82 | lean-checker-qualifier-5 | QUALIFIED, promoted | second blind review found the defects in repair round 5 |
| 0.1.5 | 113 | lean-checker-qualifier-6 | QUALIFIED, 3 trials agree, promoted (production), independent | current |

Each trial ran two negative controls. The first is the tool's injected pytest failure. The second is `tests/mutation_controls.py`, which breaks one gate at a time in a scratch copy and requires a real failing test: pytest must exit 1 and report "failed". There are nine such gates:

* dedup
* symlink containment
* backtest leakage
* removal validator
* share screen
* MCP name join
* workflow discovery
* unknown names reach skill events (retargeted in 0.1.5: the known name filter is now the effective privacy gate, so breaking only the older path rule no longer leaks and no longer counts as a mutation)
* promotion contract

A collection error, an anchor mismatch or a crash makes the control fail.

Known limit: the control does not rerun the unmutated copy. Within each trial, the same tree passes all 113 tests unmutated, which covers it.

## Repairs (all 5 allowed were used)

1. **Negative control hardened.** A collection error or crash no longer counts as a caught mutation. The 0.1.0 qualifier found this.
2. **Conflicting copies merged by per field maximum, no longer first kept.**
   * Over 28 days of real history, 5,934 subagent requests had a partial first copy, and every later copy was larger.
   * Keeping the first copy dropped 5,369,965 of 121,119,334 subagent output tokens (4.4%).
   * 71 main session requests were repeated in another file with all usage zeroed.
3. **MCP server names joined in the tool prefix form.**
   * 8 servers had been split into two entries each.
   * 3 of them were invoked (Claude Docs, Firecrawl, GitHub) but had received RESTRICT_EXPOSURE under their display name.
4. **Blind review round (ARI-R26).**
   * Discovery:
     * Workflow subagent transcripts were not read. That was 504 files, about 4,282 requests, dropped silently.
     * Unknown `.jsonl` files are now counted in a warning.
     * A copy that carries only `message.id` is matched to its request.
   * Privacy:
     * Path text could reach skill events through slash command names and loose files in a skills root.
     * A symlinked `SKILL.md` outside the skills root was read.
   * Audit:
     * Namespaced skill names were cut at the first colon. In one real listing, 1,027 entries parsed to 926 names.
     * The text mode printed no coverage warnings.
     * Thresholds below 1 session were accepted.
   * Forecast:
     * `--period-days 0` hung.
     * Idle days satisfied the 3 day floor.
     * The partial first day was treated as complete.
   * Nightly:
     * The next night could shrink a stored day's snapshot.
     * Empty runs added 14 empty snapshots and gave no warning.
     * Author and checker were compared case sensitively.
     * A result recorded before CANDIDATE opened TESTED.

   Each repair in round 4 has a regression test. Each test was shown to fail when its fix was reverted in a scratch copy (14 of 14 reverts caught).
5. **Second blind review round (ARI-R27).** A second fresh reviewer failed K1, K3, K4, K5, K8 and K10 again.
   * Evidence: copies that link `requestId` and `message.id` counted twice; the first read copy dated a request; oversize lines were dropped without a warning.
   * Privacy: a slash command word, a Skill argument or the first folder under the skills root could become a skill name. Names are now kept only for installed or listed skills.
   * Audit: a symlinked file inside a skill folder was not counted as use; removal advice ignored unread files; `--min-days nan` passed. Removal advice is now held at INVESTIGATE while unread gaps exist, unless `--accept-coverage-gaps` is given.
   * Command line: eleven bad inputs ended in a traceback and `--period-days 1e-9` hung. Every number and time is now range checked (exit 2), and the period rolls forward by arithmetic. A warning names the case where `--days` opens after the period start.
   * Share: an epoch like version could pass; versions are now 1 to 3 digits each.
   * Nightly: a blank checker passed; `Alice Smith` and `AliceSmith` were different people; a run that skipped active files drew lessons from a partial day; a damaged state could traceback. All four are fixed, and the report and nightly commands now print coverage warnings.

   Each repair in round 5 has a regression test in `tests/test_ari_review_repairs_r5.py`; 17 of 17 reverts were caught. The two older tests whose fixtures assumed the old behaviour were updated: the audit fixture holds unread lines on purpose, so its removal test now passes `accept_coverage_gaps`, and the privacy fixture's skill folder now holds a `SKILL.md`.

## Requirements

PASS means a test or an inspected real run supports the requirement. The tests named are in `tests/`.

| Req | Status | Evidence |
| --- | --- | --- |
| R1 one ingestion layer, `ari-event/1` | PASS | all four CLIs import `ari.evidence.ingest` |
| R2 dedup, conflicts merged by maximum | PASS | `test_ari_evidence.py`, `test_ari_review_repairs.py`; real: 6,078 conflicts in 28 days, 1 in the last 7 |
| R3 skipped records counted | PASS | `test_skipped_records_are_counted_not_guessed` |
| R4 partial lines, quiet period | PASS | evidence tests; nightly real run used `--quiet-seconds 1800` |
| R5 symlinks refused | PASS | `test_symlink_outside_root_is_refused`, `test_symlinked_skill_outside_root_is_not_read` |
| R6 missing agentType is `unknown` | PASS | evidence test; blind review K2 PASS |
| R7 no prompt, arguments or paths in events | PASS after repair 4 | `test_paths_never_become_skill_names`, `test_no_secret_prompt_or_path_in_events` |
| R8 `measure_subagents.py` unchanged | PASS | hash c0cd21d2...; `test_measure_subagents_regression.py` |
| R9 to R12 weekly burn | PASS | `test_ari_forecast.py`, repair tests; real runs below |
| R13 to R17 audit | PASS after repairs 3 and 4 | `test_ari_audit.py`; real run: 0 removal or restriction recommendations on an invoked or indirectly used capability |
| R18 offline HTML | PASS | `test_ari_report.py`; real report: 0 `<script`, 0 URLs, CSP `default-src 'none'` |
| R19, R20 share | PASS | `test_ari_report.py`; blind review K7 PASS; real share read below |
| R21, R22 nightly | PASS after repair 4 | `test_ari_learn.py`, repair tests; real second run added 0 snapshots and 0 lessons |
| R23 abduction contract | PASS | `test_abduction_contract_and_bounds`, `test_correlated_exposure_change_is_not_a_cause` |
| R24 scheduling documented, not installed | PASS | `docs/ari/scheduling.md`; no unit, plist, cron entry or task was created |
| R25 X governance note | PASS | `docs/governance/x-distribution.md`; blind review confirmed the ledger counts (993 total, 114 on 2026-10-31, 91 on 2026-11-01) |
| R26 repair round 4 | PASS | `test_ari_review_repairs.py`, 14 of 14 reverts caught |
| R27 repair round 5 | PASS | `test_ari_review_repairs_r5.py`, 17 of 17 reverts caught |
| P1 to P4 | NOT_TESTED | proposed only, not built |

## Real data runs (AC-REAL)

Command shape: `python3 -X utf8 <cli> --now 2026-11-01T22:00:00Z ...`, run read only under `systemd-run --user --scope -p MemoryHigh=8G -p MemoryMax=12G`, against `~/.claude/projects` (about 33 GB, about 16,000 `.jsonl` files).

| Run | Wall time | Peak RSS |
| --- | --- | --- |
| `weekly_burn.py --days 7` | 24.8 s (25.6 s on 0.1.5) | 117 MB (122 MB) |
| `weekly_burn.py --days 28 --json` | 74.8 s (73.1 s) | 342 MB (362 MB) |
| `skill_mcp_audit.py --days 28 --json` | 126.9 s (124.7 s) | 1.01 GB (1.02 GB) |
| `ari_report.py --days 28 --share` | 123.3 s (124.5 s) | 1.01 GB (1.02 GB) |
| `ari_nightly.py run` (twice) | 52.1 s, 49.0 s (49.2 s, 47.4 s) | 446 MB, 449 MB (453 MB) |

Figures in brackets are the rerun on the round 5 build, same `--now`, 2026-11-02.

What the outputs showed:

* **Week of 2026-10-26.** 108,275 deduplicated requests and 18,352,496,874 transcript tokens. By agent type: main 55.7%, lean-checker 24.2%, lean-worker 17.2%, general-purpose 0.6%. Cache reads dominate every row.
* **28 day forecast.** Projected period total 18,563,918,828 tokens, envelope 18,457,290,586 to 18,675,305,952, from 27 complete history days. The 3 day backtest has 22 cuts: the trailing mean MAE was 2.10B against 3.94B for persistence. The 7 day run correctly reports the backtest as INSUFFICIENT_HISTORY.
* **Quota line.** Every run printed `Provider quota ETA: unavailable`.
* **Coverage warnings.**
  * 60 `.jsonl` files were not read. All were classified: 48 workflow journals and 12 report files other tools wrote into project folders. None is a transcript.
  * 1 malformed line.
  * 6,078 requests had differing copies.
* **Round 5 rerun.** The week total and request count were identical (18,352,496,874 tokens, 108,275 requests).
* **Audit (round 5 build).**
  * 1,170 capabilities: 1,085 skills and 85 MCP servers. The 11 fewer names were built in commands (`clear`, `compact`, `model`, `context` and others) and two uninstalled names that the old parser counted as invoked skills.
  * Skill statuses: 448 invoked, 82 indirect only, 553 exposed but unobserved, 2 not seen exposed.
  * Default run: 61 unread gaps (60 unrecognised files and 1 malformed line), so every removal and restriction is held at INVESTIGATE (536 in total).
  * With `--accept-coverage-gaps`: 387 CONSIDER_REMOVAL and 24 RESTRICT_EXPOSURE, none on a capability with any use in its own category. One target, the skill `revande-cal-driver`, shares its name with an MCP server that is used; the advice concerns the skill only.
* **Audit (first build, for comparison).**
  * 1,181 capabilities: 1,096 skills and 85 MCP servers.
  * Skill statuses: 459 invoked, 82 indirect only, 553 exposed but unobserved, 2 not seen exposed.
  * MCP server statuses: 58 invoked, 27 exposed but unobserved.
  * 139 namespaced skill names were kept intact.
  * Recommendations: 387 CONSIDER_REMOVAL and 24 RESTRICT_EXPOSURE, each carrying its evidence and validation method, plus 125 INVESTIGATE and 44 INSUFFICIENT_EVIDENCE.
  * These are recommendations only. Nothing was removed.
* **Share file.** Agent types folded to Explore, custom, general-purpose, lean-checker and lean-worker. Versions are "2.1" only, 1 group was suppressed, and there are no paths, ids or dates. It was written to the scratchpad only.
* **Nightly.** On the first build the first run added 13 snapshots and 3 HYPOTHESIZED lessons, all general-purpose starting context drops, and the second run added 0 and 0. On the round 5 build the first run added 13 snapshots and the second 0, but neither drew a lesson: files were still active (202 with the fixed past `--now`, 10 on the live clock, this session among them), so lessons were deferred as designed.

## Remaining limits

* The transcript and attachment formats are internal to Claude Code, and were observed on 2.1.x only.
* The harness names some of its own tools like MCP tools, for example `mcp__SubagentHandback__SubagentHandback`. They appear as servers that no config installs.
* Indirect skill use is seen only through the Read tool.
* A `usage` key written with a JSON unicode escape is not seen. A request whose copies straddle the window start is counted in the window. Neither was observed.
* Real data figures depend on this one machine and its history. They are not a benchmark.
* A nightly run draws lessons only when no transcript file is active. A machine with sessions open all night would never draw one. A narrower rule (read complete lines of active files for past days, since partial last lines are already skipped) is proposed, not built: it would be a sixth repair, beyond the mission's limit of five.
* Two copies of one request with different cache shapes (one with the 5 minute and 1 hour split, one with only the total) can overstate cache writes by the per field maximum. Not observed on real data.
* Files last modified before the window start are not opened and are not a warning, by design: they cannot hold a record from inside the window.

## ARI 2.0.0 (2026-11-01)

Everything above records 0.1.x and is kept unchanged. The baseline was frozen before any 2.0 change: git status, HEAD `c08376c`, and the sha256 of every ARI script, test and doc, with 113 tests passing and ruff and mypy clean. The requirements (`docs/specs/ari-srd-v2.0.0.md`) were frozen before implementation; section 10 records every later clarification and repair.

Checks on the final build:

* 188 tests pass (`pytest tests`), ruff is clean, and `mypy --strict` is clean on `scripts/ari`, the five CLIs and the new test files.
* `tests/mutation_controls.py` breaks 25 gates one at a time in a scratch copy, and the suite catches all 25. That covers the 9 controls from 0.1.5 and 16 new ones: finding contract, ambiguous band, journal agent check, marked unread files, hostile journal line, unread symlink escape, replay quarantine, approval binding, code binding, unstable days, the SUPPORTED rule, the live tag probe, the forbidden holon authority, the routine record types and both CONSOLIDATE gates.
* The vgtp procedure `ari-tests` was requalified for 2.0 (version 2.0.1, 3 of 3 agreeing trials of 188 tests) under new criteria (`ari-suite-v4`, floor 151 declared tests, the mutation harness as the negative control) by a separate agent. It was not promoted: production stays at 0.1.5 until the owner approves.

Repairs, each found by a test or a check and each with a regression test and a mutation control:

1. In `ari_discover.py`, compatible facts were rival hypotheses under one question, so two true facts blocked each other from SUPPORTED. Each fact is now its own question, with its negation as the rival.
2. The anomaly F probe counted `onerror=` as a live tag. That string also appears in correctly escaped text, so the probe reported a false disclosure.
3. A workflow journal line that parsed to a list raised AttributeError and stopped the ingest.
4. The known record type set missed 13 routine 2.1 types, so the unknown type warning fired on every real run (191,851 records).
5. CONSOLIDATE (removal class) bypassed the coverage gap gate and carried no finding; found by the blind review and confirmed on real data.

The repair cap of 5 is used up. Defects found by the blind review that are NOT repaired are listed at the end of this section.

Real data, same machine, same fixed `--now 2026-11-01T22:00:00Z` as the frozen BEFORE run of the 0.1.5 code. Peak memory is in brackets.

| Run | Before (0.1.5) | After (2.0) |
| --- | --- | --- |
| `weekly_burn.py --days 7` | 22.1 s (125 MB) | 23.7 s (133 MB) |
| `skill_mcp_audit.py --days 28` | 132.0 s (1.04 GB) | 139.0 s (1.07 GB) |
| `ari_report.py --days 28 --share` | 130.9 s (1.05 GB) | 135.3 s (1.07 GB) |
| `ari_nightly.py run`, first | 49.1 s (462 MB) | 58.6 s (533 MB) |
| `ari_nightly.py run`, second | 49.6 s (462 MB) | 57.9 s (532 MB), rerun alone; 89.1 s in the batch while two checker agents ran |
| `ari_discover.py --days 14` | none | 67.9 s (533 MB) |

Every difference, investigated:

* **Burn.** Unchanged: 18,352,496,874 tokens over 108,275 requests in the week. The uncertainty band is 18,352,496,874 to 18,352,496,874. Its single AMBIGUOUS request has one copy that is at least as large as the per field maximum in every field, so the band has zero width.
* **Audit gaps.** Unread gaps fell from 61 to 7: the 6 workflow agent runs whose transcripts are missing, plus 1 malformed line. The 48 workflow journals and 12 foreign data files are now classified and excluded with their reasons. Because 7 gaps remain, the recommendation counts are identical to BEFORE (535 INVESTIGATE, 44 INSUFFICIENT_EVIDENCE, 2 UNKNOWN, 1 CONSOLIDATE, 589 KEEP). That is the intended effect: removal still needs gap free evidence or `--accept-coverage-gaps`. After repair 5 the one CONSOLIDATE (MCP servers `lovable` and `lovable-b`, identical tool sets) is held at INVESTIGATE too, with its own finding (duplicate UNRESOLVED, coverage gap SUPPORTED, different targets NOT_TESTABLE), so the final counts are 536 INVESTIGATE, 44 INSUFFICIENT_EVIDENCE, 2 UNKNOWN and 589 KEEP.
* **Findings.** The audit now carries 582 findings. The coverage finding is PARTIAL: its "partial" reading is SUPPORTED, and the deleted history reading is NOT_TESTABLE. The report adds a "What the evidence supports" section with 10 finding blocks and no script element.
* **Share file.** Identical to BEFORE apart from `ari_version`.
* **Nightly.**
  * Requests seen rose from 180,504 to 190,362, because the 206 files that were still active are now read instead of skipped.
  * The first run drew 3 SPECIFIED lessons where 0.1.5 drew none: general-purpose starting context drops on 2026-10-28, 10-30 and 10-31.
  * The second run added 0 snapshots and 0 lessons. It classed 8,465 sources UNCHANGED and 4 GROWN; the GROWN files are live sessions, including this one.
  * The live clock run also drew 3 lessons.
  * The extra 9 s per run and 70 MB come from reading the active files and keeping the source manifest. Comparing all 8,469 checkpoints took 1.8 s, and the state file grew from 16 KB to 3.9 MB.
* **Discovery ledger.** 22 hypotheses over 10 questions, with 0 violations and an empty backlog: 7 SUPPORTED, 10 CONTRADICTED and 5 UNRESOLVED.
  * SUPPORTED: the unread files are not transcripts; journals name 6 agent runs without transcripts; the 0.1.5 global active file rule blocked lessons; some copies differ in cache or input fields for an unknown reason; the observer lacks coverage of the unused capabilities' use (7 gaps); cache reads make up most counted tokens; the ambiguity band is immaterial to the total.
  * CONTRADICTED, among others: HTML injection (the probe found no live tag; the blind review agreed) and share leakage (the probe's 4 hostile shares were all refused, but the blind review then found bypasses through the unscreened `note` field and untyped values, so this result is a false green and is withdrawn), more than one transcript format in the window (all records are 2.1), and "the capabilities are genuinely unused" as one explanation for all of them.
  * UNRESOLVED, each with its reason: the two candidate causes of the high token use (a configuration change, or the amount of work done), whether lessons generalise across versions, whether a request id is shared by two different requests, and whether capabilities are never exposed (true for some and not for others).
* **Coverage warnings after repair 4 (28 day burn).** The warnings are: 6 journal runs without a transcript, 1 non object line, 6,078 requests with differing copies, and 71 with ambiguous copies. The unknown type warning no longer fires.

Remaining limits for 2.0:

* The checkpoint is sampled, so a rewrite that keeps both sampled ranges is not detected.
* Replay only reproduces a lesson's trigger on stored snapshots. Generalisation across transcript versions is UNRESOLVED.
* Unknown record types are seen only on lines that carry a transcript marker, because of the fast line filter.
* The approval file binds content, not a person.
* The qualifier and the blind reviewer are the same model family as the author. They are separate agents, not independent of the model.
* The 0.1.x limit "a nightly run draws lessons only when no transcript file is active" is resolved by per source eligibility.

Defects found by the blind review (one lean-checker, same model family as the author). They were left open when the repair cap of 5 was reached and repaired on 2026-11-02, after the owner asked for them to be fixed. Each repair has a test that failed first (or, for behaviour that was already right, a mutation control that makes its test fail) and a control in `tests/mutation_controls.py`.

* D1 (high), repaired: one hostile record no longer stops `ingest`. JSON goes through a bounded loader (deep nesting, a 5,000 digit integer and bad encoding count as malformed), a timestamp an offset pushes past year 1 or 9999 is skipped, and a journal agent id must be a short plain name before it is used in a path. Tests: `tests/test_ari2_hostile.py`.
* D2 (medium), repaired: `screen_share` requires exactly the known fields, the fixed `note` and `schema` text and typed values, and returns a problem for any JSON value instead of raising. The earlier CONTRADICTED result for share leakage, withdrawn above, now rests on these tests.
* D3 (medium), repaired: approval compares a hash recomputed from the lesson's fields, and replay recomputes each evidence day's hash.
* D4 (medium), repaired: a hypothesis refused by the per question cap goes to the backlog, and a new rival takes a SUPPORTED hypothesis back to HYPOTHESIS and reopens its question.
* D5 (medium), repaired: the harness first runs the unmutated test files and stops as broken when they fail (checked by planting one failing test: it printed BROKEN BASELINE and exited 0). The 8 untested behaviours have tests in `tests/test_ari2_behaviours.py`; the replay hash check is covered by the D3 test.
* D6 (low to medium), repaired: an unstable day is never a baseline day.
* D7 (low), repaired: `--quiet-seconds` must be finite and at least 0; the holon validator checks `uninstall` and `disable` separately, finds parent cycles and refuses a string authority; `validate_finding` closes the decision vocabulary, needs a `not_exposed` reading for CONSIDER_REMOVAL and RESTRICT_EXPOSURE, and checks the coverage gap reading and coverage state against `provenance.unread_gaps`; a directory link out of the root is counted at any depth. Also, under `--metric output` the band line now says it bounds all token types.

* D8 (medium), found by the real data check after these repairs and repaired: only usage records were cut at `--days` and `--now`. Rerunning the 28 day audit at the same `--now` the next morning moved 18 recommendations from INSUFFICIENT_EVIDENCE to INVESTIGATE, because 432 skill or MCP listings and 4,818 tool calls from 25 later sessions were counted (1,942 exposure sessions instead of 1,930 inside the window). Tool and listing records now obey the window. The rerun audit counts 1,930 exposure sessions: 535 INVESTIGATE, 38 INSUFFICIENT_EVIDENCE, 7 UNKNOWN, 576 KEEP, 7 unread gaps. The 13 fewer KEEP are capabilities whose only uses fell outside the 28 days. The 7 day burn total (18,352,496,874), the lessons, the snapshots and the share file were unchanged by every repair.

After the repairs: 248 tests pass under Python 3.12.3 and 3.14.4, ruff and `mypy --strict` are clean, and the harness catches 53 of 53 mutations after a passing baseline. These numbers are from the working tree on 2026-11-02; no procedure was requalified for them yet.
