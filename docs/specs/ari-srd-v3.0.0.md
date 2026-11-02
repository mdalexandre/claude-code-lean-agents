# ARI 3.0: Autonomous Recursive Operational Intelligence (SRD v3.0.0)

Source: the owner's concept document "ARI 3.0" (2026-11-02), approved with "build it".
This SRD records what is built from it, the calls made while scoping, and how each
requirement is checked. Status per requirement is set by tests, never by this text.

## Intent

ARI 2.0 measures (burn, audit, report, nightly lessons, discovery). ARI 3.0 adds the
intelligence that uses those measurements without being invoked: it observes, investigates
competing explanations, turns settled investigations into candidate interventions and
procedures, verifies their real outcomes, and measures and regulates itself. It may adapt
how it works; it may never widen its own authority or weaken the gates that judge it.

## Architecture (one governing runtime, holons as responsibilities)

| Holon | Responsibility | Module | Execution |
|---|---|---|---|
| H0 Governor | authority, bounds, lock, journal, safe state, integrity of protected files | `ari/governor.py`, `ari/policy.py` | code |
| H1 Observer | runs the ARI 2.0 CLIs, digests signals, detects material change | `ari/observer.py` | code |
| H2 Investigator | abductive questions, discriminating probes, open ended proposals | `ari/probes.py`, `ari/investigator.py` | code plus a tool-less model |
| H3 Procedure Lab | correction patterns, skill creation gate, holdout replay, candidate lifecycle | `ari/competence.py`, `ari/candidates.py` | code |
| H4 Verifier and Reporter | outcome verification, self observation, regulation, brief | `ari/regulator.py`, `ari/brief.py` | code |

The model never executes anything. It receives open questions as JSON and returns
proposals as JSON validated against a schema: a new hypothesis with a probe from the closed
probe catalog and a deterministic decision rule, a child question, an UNRESOLVED verdict
with a reason, or an interpretation sentence. The governor validates and runs the probe and
judges the result by the rule the model declared before seeing it. The model process is
started with no tools, no MCP servers, hooks disabled and no session persistence.

## Recursion (R1 to R7) as built

- R1 discovery: a settled question opens a child question (why no use, then is it worth keeping).
- R2 diagnosis: every question has competing hypotheses and discriminating probes; ambiguity stays OPEN or UNRESOLVED, never a forced winner.
- R3 procedure: recurring human corrections become competence candidates through the skill creation gate (routing correction, bounded revision, new skill) and a holdout replay.
- R4 learning: every candidate carries a predeclared metric and baseline; after the owner applies it, ARI records VERIFIED or ROLLED_BACK and keeps per rule accuracy; a rule with poor accuracy yields a policy revision candidate.
- R5 self observation: every model call is recorded with cost, tokens, duration and information gain (hypothesis status changes); a `call_started` row is written before each call, so a call whose process is killed still counts at its whole budget.
- R6 coordination: not built in 3.0 (one investigator is enough until a measured need exists).
- R7 governance: protected files are hash pinned; a mismatch puts ARI in a safe state. Policy changes are proposals only; re-pinning needs a person at a terminal.

## Requirements and checks

| ID | Requirement | Check |
|---|---|---|
| ARI-01 | Unattended observation with durable run records | tick writes a run record for success and failure (tests) |
| ARI-02 | Unchanged evidence does not wake the model again | same digest, no model call (tests) |
| ARI-03 | Material findings carry competing hypotheses | every question has at least two hypotheses or a stated reason (tests) |
| ARI-04 | Bounded recursion | depth 3, 4 hypotheses per question and 24 questions per investigation (discovery.py constants, which govern; policy.BOUNDS repeats them and a test keeps the two equal; a saved ledger may lower them, never raise them, and question depths are recomputed from the parent chain on load), at most 60 new investigations opened per tick (open ones are probed again when the evidence changes), rule pairs that cannot decide both ways are refused, 3 model rounds of at most 6 questions per tick, 8 proposals per reply, nonprogress stop (tests, mutation controls) |
| ARI-05 | No promotion without replay, qualification and approval | candidate transitions refuse skipped states, and a stored APPROVED or CANARY candidate without a matching owner approval record is quarantined (tests, mutation controls) |
| ARI-06 | Authority isolation | model argv has no tools; approval needs a terminal and a typed code (tested on a real pseudo terminal); every ARI module, the operator CLI and the three programs a tick runs are pinned, and a pin mismatch gives SAFE and refuses approve, applied and resume (tests, mutation controls) |
| ARI-07 | Idempotent processing | rerun on the same data adds nothing; concurrent tick refused by a lock (tests) |
| ARI-08 | Self observation | model ledger rows (an unreadable cost counts as a whole call budget), rolling cost; budget exhaustion refuses calls, three failed calls in a row suspend the layer until the owner resumes it (tests, mutation controls) |
| ARI-09 | Outcome evaluation against predeclared baselines | CANARY to VERIFIED or ROLLED_BACK after the waiting period, judged only when the audit ran, no transcript gap is unread and transcript lines were read for the canary's own window; content must match the hash the owner approved (tests, mutation controls) |
| ARI-10 | Brief: at most three decisions, incidents always visible | brief tests |
| ARI-11 | Recoverability | stale journal recorded, atomic state writes, a failing stage leaves earlier evidence intact; unreadable state, candidate or investigation files (including JSON nested too deeply), malformed candidates and investigations, and an unusable lock, journal or working folder are kept aside with an incident; any remaining crash is written to tick_crashes.jsonl (tests, mutation controls) |

## Scoping calls (made by Claude, open to the owner)

1. Bounds start small: depth 3, 4 hypotheses, 60 investigation targets per tick, 3 model rounds per tick, USD 1 per tick, USD 5 per rolling 7 days.
2. The timer and the SessionStart hook are written but not installed; installing them is a separate owner decision (scheduling and hook edits were excluded from the ARI 2.0 authority). The hook body has unit tests; the install script was run against a sandboxed home with a stub `systemctl` on 2026-11-02 (install twice, status, uninstall: one hook entry, an unrelated hook kept, settings restored, one backup per run); the units themselves were never enabled.
3. Routing replays (matched task runs on lean agents) are not built. A `routing_correction` candidate proposes a description change and is judged afterwards by its predeclared rule (the skill is invoked at least once within 7 days); nothing reruns real tasks, because that would spend model budget on them.
4. A candidate is never applied by ARI. APPROVED means the owner may apply it; CANARY starts when the owner records that they applied it.
5. The approval gate authenticates presence at a terminal, not identity: no local mechanism can authenticate a person against a process running as the same user. The tool-less investigator and the terminal check together keep the model out of the approval path.
6. Correction excerpts stay in the local state directory (mode 0700), are capped at 200 characters and are dropped when they look like a secret (including URL credentials and cookies). Error text from tools and the model has secret values replaced before it reaches the brief or the ledger. A word repeated in three corrections is kept in the proposal even if it was a password; that residual risk is stated in the operator limits.

## Build order

policy and integrity, probes, discovery persistence, observer, investigator, regulator,
competence, candidates, brief, governor, CLI and hook, deploy units, tests and mutation
controls, real data run, independent review.
