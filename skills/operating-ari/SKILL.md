---
name: operating-ari
description: Use when the owner asks about ARI, the ARI brief, the ARI operator, ari_operator.py, ~/.ari/operator, ari-operator.timer, a SAFE or suspended ARI, ARI model budget, or wants an ARI candidate approved, applied, rejected or a tick run.
---

# Operating ARI

ARI 3.0 (repo `/home/mdalexandre/claude-code-lean-agents`, state `~/.ari/operator`) runs nightly from `ari-operator.timer` and proposes changes; it never applies them. Answer from live state, then act only within what an agent may run. Full mechanics: `docs/ari/operator.md`.

## Read live state first

The brief is a snapshot of the last tick, so its errors can predate a fix. Before diagnosing anything the brief says, run (from the repo):

```bash
python3 scripts/ari_operator.py status     # mode, safe_reasons, model_suspended, candidates, self, integrity
systemctl --user list-timers ari-operator.timer --no-pager
git log --oneline -5 -- deploy/ scripts/
```

If the brief's error is older than a commit or unit change that addresses it, report it as possibly fixed and name the next tick as the check. Example: "/tmp/claude-1000 is owned by uid 0" is fixed by `CLAUDE_CODE_TMPDIR=%h/.claude/tmp` in the unit (commit 146a747), not by changing /tmp as root.

## Who runs what

| Command | Who | Notes |
|---|---|---|
| `status`, `brief` | agent | read only |
| `tick --no-model` | agent, when asked | heavy: 3.5 to 4.6 min, about 0.6 GB, 14 days of transcripts; wrap in the `systemd-run --user --scope` bounds |
| `tick` | agent, when asked and spend is accepted | up to USD 1 of model spend |
| `reject ID --reason` | agent, only on the owner's explicit word per decision | permanent, see below |
| `approve`, `applied`, `resume`, `pin` | owner only, in their own terminal window | each reads a one time code from /dev/tty |

An agent's shell has no controlling terminal (`/dev/tty` gives ENXIO), so the four owner commands always refuse there. Give the owner the exact command to type in a separate terminal window, or over SSH from a phone. Faking a terminal, editing `candidates.json` or `state.json`, or calling `policy.write_pin()` defeats the gate. SAFE is the designed safe outcome: a SAFE tick still writes its brief, and the next tick leaves SAFE by itself once `pin` matches the files.

## Numbers

- Model budget: USD 1 per tick, USD 5 per rolling 7 days, no call below USD 0.05. Left this week = 5 minus `self.model_cost_usd_7d` in `status`.
- A call that fails, times out or returns no readable cost is charged its whole cap.
- Three failed calls in a row suspend the model layer until the owner runs `resume`. Fix the cause first: read the last rows of `~/.ari/operator/model_runs.jsonl`.
- Tick exit codes: 0 OK, 3 SAFE, 75 BUSY (another tick holds the lock), 1 failed.

## Candidates

They move CANDIDATE, REPLAYED, QUALIFIED, APPROVAL_PENDING by themselves. Only APPROVAL_PENDING needs the owner. A candidate stays below it while the audit has unread transcript gaps (unless a tick ran with `--accept-coverage-gaps`) or while its skill was exposed in fewer than 20 sessions.

A rejected candidate keeps its id, which is derived from its kind and target, so that target never gets a new candidate of that kind. Before a bulk reject, tell the owner this and the count, and wait for their yes.

`applied` only works on an APPROVED candidate. The owner runs `approve` first.
