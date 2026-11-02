# ARI SRD v2.0.0: Recursive holonic and hermeneutic intelligence

Status: FROZEN before implementation on 2026-11-01, branch `ari-v0.1.0`, nothing committed.
Mission: ARI 2.0. Predecessor: `ari-srd-v0.1.0.md` (immutable baseline; its R1 to R27 stay in force unless a row below names a change). Concept graph: `ari-concept-graph-v2.json`. Holon contracts: `ari-holons-v2.0.0.json`.

## 1. Baseline freeze

| Item | Value |
| --- | --- |
| HEAD | `c08376cc80641ab75affe701a321dfebc861f78b`, branch `ari-v0.1.0`, ARI files untracked, nothing committed |
| Tests at freeze | 113 passed (`python3 -m pytest -q`), ruff clean, `mypy --strict scripts/ari` clean (8 files) |
| Frozen code | byte copy of `scripts/` at freeze, used for every "before" measurement |
| File hashes | recorded in the verification record (`docs/ari/verification.md`, section 2.0 baseline) |
| Procedures | vgtp store `~/.vgtp/claude-code-lean-agents`: `ari-tests 0.1.5` promoted earlier; immutable, never edited by this release |

Documented CLI behavior that must survive: `weekly_burn.py`, `skill_mcp_audit.py`, `ari_report.py`, `ari_nightly.py` keep every flag, exit code (0 ok, 2 refused) and JSON key; new keys are additive.

## 2. Objectives, scope, exclusions

Objective: every operational conclusion is bounded by the evidence that produced it, every correction by its authority, every learned procedure by tests.

In scope: source manifest and checkpoints, format detection, duplicate classification with uncertainty bounds, interpretation layer and report section, per source nightly eligibility, lesson lifecycle with replay and quarantine, bounded abductive discovery engine and CLI, holon contracts, tests, docs.

Excluded (not authorized): commits, pushes, schedules or services, deleting or restricting any skill or MCP server, editing hooks or approval guards, publishing reports, promoting into the vgtp production registry, editing approved procedures, relaxing a gate to pass a test.

Assumptions (labelled): A1 transcript files are append only while a session runs (OBSERVED on 2.1.x, not documented by the vendor). A2 a record's timestamp is when it was written, so a run started on day D cannot add records dated before D except as copies of existing requests (JUDGMENT from A1 and the resume behavior seen in round 4). A3 the transcript format is internal and may change (vendor statement, see v0.1.0).

## 3. Requirements

| ID | Holon | Requirement |
| --- | --- | --- |
| ARI2-R1 | H1 | Every `.jsonl` under each configured root is classified: `main`, `subagent`, `workflow_journal`, `foreign` (a full scan finds none of the transcript markers `"usage"`, `"tool_use"`, `command-name`, `"attachment"`), or `unknown` (markers present in an unrecognised place). Only `unknown` stays an unread gap; `foreign` and journals are listed as excluded with a reason. |
| ARI2-R2 | H1 | Each workflow journal's started agent ids are checked against transcripts; a started agent without a transcript is a counted coverage gap. |
| ARI2-R3 | H1 | The source manifest records per file: class, size, mtime_ns, complete line offset, head hash (first 64 KiB), window hash (up to 4 KiB ending at the offset), parser version. Against a previous manifest each file is NEW, UNCHANGED, GROWN, TRUNCATED or REPLACED. The checkpoint is sampled; a rewrite that keeps both sampled ranges is not detected (stated limit). |
| ARI2-R4 | H1 | Claude Code `version` major.minor of each usage record is counted; records outside the verified set (`2.1`) are counted as unverified format and warned, still parsed field by field. Unknown record `type` values are counted. |
| ARI2-R5 | H1, H2 | Duplicate requests are classified IDENTICAL, STREAM_GROWTH (only output differs), ZEROED_COPY (one copy all zero), AMBIGUOUS (anything else). For AMBIGUOUS requests the counted value stays the per field maximum (upper bound) and the largest single copy is the lower bound; burn reports `total_uncertainty` {lower, upper}. Verified deduplication is never claimed for AMBIGUOUS requests. |
| ARI2-R6 | H2 | Burn JSON gains `duplicate_classes`, `total_uncertainty`, `assumptions`; text prints the band and "Provider quota ETA: unavailable". No quota is inferred from transcripts. |
| ARI2-R7 | H3 | The audit gap gate counts only true gaps (R1 unknown, R2 missing agents, unreadable, outside root, malformed, partial, oversize). |
| ARI2-R8 | H4 | Finding schema `ari-finding/1`: observed, at least two readings each with supports, weakens and a status (SUPPORTED, WEAKENED, UNRESOLVED, NOT_TESTABLE), justified decision, what would change it, provenance (window, parser version, source counts) and coverage state (COMPLETE, PARTIAL). A validator refuses a finding without these. |
| ARI2-R9 | H4 | "No observed use" readings: unused, coverage gap, indirect invocation, not exposed, substituted. A removal class decision is allowed only with zero gaps, exposure at or above the threshold, and the coverage and not exposed readings WEAKENED; otherwise INVESTIGATE. |
| ARI2-R10 | H4 | The HTML report gains a findings section ordered Observed, Readings, Decision, What would change it, with text uncertainty labels (not color alone). No script, no external resource, all text escaped. The share file stays screened and is never called anonymous. |
| ARI2-R11 | H5 | Nightly per source eligibility: stable files and the complete lines of active files are read; only days before the run day (UTC) are snapshotted; an active file no longer blocks lessons. A day a TRUNCATED or REPLACED source contributed to is unstable for that run: recomputed, no lesson drawn. |
| ARI2-R12 | H5 | State schema `ari-learn-state/2` with the manifest; a `/1` file is migrated after a byte backup. Snapshots carry parser version and content hash. A rerun on the same data adds nothing and changes no snapshot hash. |
| ARI2-R13 | H5 | Lifecycle DISCOVERED, SPECIFIED, CANDIDATE, REPLAY_TESTED, QUALIFIED, APPROVAL_PENDING, PROMOTED, with QUARANTINED terminal (evidence kept). v1 names are accepted as aliases. Nightly creates SPECIFIED at most. |
| ARI2-R14 | H5 | Contracts: CANDIDATE needs a proposed procedure, an alternative and a rollback note. REPLAY_TESTED needs a replay on stored snapshots that reproduces the trigger and stays quiet on the baseline days (counterexamples), bound to the ARI code hash and parser version; a failed replay quarantines. QUALIFIED needs that replay plus a supporting result from a distinct checker and one rejected alternative. APPROVAL_PENDING needs the binding to still match the current code. PROMOTED needs an approval file naming the lesson id and its content hash; it writes no vgtp registry. |
| ARI2-R15 | H5 | Lessons record the Claude Code versions of their evidence; replay on a version outside them reports "version not covered". |
| ARI2-R16 | H0 | Discovery schema `ari-discovery/1`: hypotheses with id, provenance, observation, mechanism, competitors, predictions, falsification test, results, uncertainties, status HYPOTHESIS, SUPPORTED, CONTRADICTED, UNRESOLVED. Bounds: depth 3, 4 hypotheses per node, 24 nodes; novelty and cycle checks; budget exhaustion recorded. SUPPORTED needs a supporting result with a sha256 bound evidence file and every competitor CONTRADICTED or left UNRESOLVED with a reason; conflicting results give UNRESOLVED. No numeric confidence. |
| ARI2-R17 | H0 | `ari_discover.py` runs the deterministic discriminating tests for anomalies A, C, D, F and G on local data and reads audit and burn JSON for B and E; it writes the discovery ledger and changes nothing else. |
| ARI2-R18 | H0 | Holon registry `scripts/ari/holons.py` mirrors `ari-holons-v2.0.0.json`; a validator checks one parent, typed inputs and outputs, a budget, a safe failure state, a verification and that no authority overlaps the forbidden actions. |

## 4. Interfaces and units

Tokens as in v0.1.0 (five categories, `context`, `total` unweighted). Bounds are tokens. Times UTC. New JSON keys: burn `duplicate_classes` {class: requests}, `total_uncertainty` {lower, upper, ambiguous_requests}, `assumptions` [text]; evidence `manifest` [source rows], coverage counters `files_foreign`, `files_workflow_journal`, `journal_agents_missing`, `unverified_format_records`, `unknown_record_types`; audit `findings` [ari-finding/1]; nightly summary `source_changes`, `unstable_days`.

## 5. Error states and recovery

Unreadable or unknown sources degrade to PARTIAL coverage with warnings, never to a silent zero. A damaged state file refuses with exit 2. An interrupted nightly run leaves a journal; the next run records the recovery and recomputes (idempotent). A failed replay quarantines the lesson with evidence. Budget exhaustion in discovery records the open question as a backlog item.

## 6. Budgets

Discovery depth 3, 4 per node, 24 nodes. Repairs: 5 cumulative. Real data runs under `systemd-run` with 8G high, 12G max. No model call, no network.

## 7. Acceptance and verification

PASS needs an observed test. Mandatory adversarial cases (Phase 9 list) each map to a named test in `tests/test_ari2_*.py`. Gates: full pytest, ruff, `mypy --strict scripts/ari`, extended mutation controls (each new gate broken in a scratch copy must fail a test), revert checks, real data before and after with every difference investigated, one blind review labelled as same model family context. A vgtp candidate procedure may be extracted and qualified but stays APPROVAL_PENDING.

## 8. Compatibility and rollback

Additive JSON; aliases for v1 lesson states; v1 state migrated with a `.v1.bak` copy; `measure_subagents.py` and the six v0.1.0 baseline files byte identical. Rollback: restore `scripts/` from the frozen copy and the `.v1.bak` state; nothing outside the working tree and scratch directories is written.

## 9. Revision rule

A materially different behavior or acceptance change found during implementation is added as a dated row in section 10, not by editing rows above.

## 10. Revisions

Requirement text above is unchanged since the freeze; each row clarifies or records a repair.

| Date | Requirement | Change | Why |
| --- | --- | --- | --- |
| 2026-11-01 | ARI2-R3 | Manifest rows also carry the resolved `path`, and the nightly manifest is keyed by it. A file deleted between runs is not visited and keeps its last row; `compare_checkpoint` can report MISSING, but the nightly does not need it because a stored day never shrinks. | Without the key the nightly comparison silently skipped every row (found while writing the R11 tests). |
| 2026-11-01 | ARI2-R16, R17 | "Competitor" means another hypothesis under the same question. Compatible facts (for example "unread files are not transcripts" and "journals name missing agents") are separate questions, each with its negation as the rival. | Under one question two true, compatible facts blocked each other from SUPPORTED forever (repair 1). |
| 2026-11-01 | ARI2-R17 | The anomaly F probe counts only real `<script` and `<img` tags as live. | `onerror=` also appears inside correctly escaped text, so the probe reported a false disclosure (repair 2). |
| 2026-11-01 | ARI2-R1, R2 | A journal line that parses to a list or a string is skipped. | It raised AttributeError and stopped the whole ingest (repair 3). |
| 2026-11-01 | ARI2-R4 | The known record type set lists the 23 types seen in format 2.1 transcripts on this machine (Claude Code 2.1.303 to 2.1.316). | The first set missed 13 routine types, so the unknown type warning fired on 191,851 records every run and would have hidden a real new type (repair 4, found in the real data run). |
| 2026-11-01 | ARI2-R9, R10 | CONSOLIDATE, already removal class in R9, now goes through the recommendation validator's coverage gap check and carries its own finding (duplicate, coverage gap, different targets). | It was emitted with open coverage gaps and no finding; on real data it advised consolidating two MCP servers while 7 gaps were open (repair 5, found by the blind review). |
| 2026-11-02 | ARI2-R1, R2 | JSON is parsed by a bounded loader: nesting past the stack, an integer over the conversion limit, bad encoding and a timestamp an offset pushes past year 1 or 9999 are counted as malformed, never raised. A journal agent id must match `[A-Za-z0-9_-]{1,128}` before it is used in a path. A directory link that leaves the root is counted outside the root at any depth. | One hostile record stopped the whole ingest (blind review D1), and a link below depth 2 was silently skipped (D7). |
| 2026-11-02 | ARI2-R6 | The share screen requires exactly the known fields, the fixed note and schema text and typed values, and returns a problem (never raises) for any JSON value. | The note was not scanned and values were not typed, so free text passed (D2). |
| 2026-11-02 | ARI2-R14, R15 | Approval compares a hash recomputed from the lesson's fields; replay recomputes each evidence day's hash; an unstable day is never a baseline day. | Both hashes were trusted from stored fields, and unstable days anchored comparisons (D3, D6). |
| 2026-11-02 | ARI2-R16, R17 | A hypothesis refused by the per question cap goes to the backlog; proposing a hypothesis settles its question again, so a new open rival takes SUPPORTED back to HYPOTHESIS and reopens the question. | The rival was lost and a stale SUPPORTED stood (D4). |
| 2026-11-02 | ARI2-R9, R10, R18 | A finding's decision must be in a closed vocabulary; CONSIDER_REMOVAL and RESTRICT_EXPOSURE need a not_exposed reading; the coverage gap reading and coverage state must agree with `provenance.unread_gaps`. The holon validator checks each alternative of a combined forbidden word, parent cycles and list typed authority. | Each was accepted before (D7). |
| 2026-11-02 | ARI2-R9 | Tool calls and skill or MCP listings obey the same `--days` and `--now` window as usage, since a no use reading speaks for that window; a record without a timestamp is kept. | Only usage was cut, so a fixed `--now` audit counted 432 listings and 4,818 tool calls from later sessions and changed from day to day (D8, found by the real data check after the repairs). |

