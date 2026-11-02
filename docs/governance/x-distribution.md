# X distribution governance for this repository

Assessment date: 2026-11-01. Scope: how the repository is promoted on X. Nothing in this build accessed, posted through or modified X.

## Supplied observations, reconciled

| Supplied claim | Reconciliation | Status |
| --- | --- | --- |
| 91 replies on one day | The local outreach ledger (`~/.claude/skills/x-demand-reply-outreach/ledger.json`, read only) has 91 entries dated 2026-11-01 and 114 dated 2026-10-31, out of 993 entries in total | RECONCILED against the local ledger only; not against X analytics |
| 4 of 20 replies posted in one campaign | The run that asked for 20 replies ended at 4 because candidates ran out (session report, 2026-11-01) | RECONCILED against the session record; not against X |

The ledger counts what the outreach tool recorded. It is not live analytics and can miss replies posted by hand.

## Categories

| Category | What it is | How this account's replies fit |
| --- | --- | --- |
| Manual reply | A person writes and posts it | Not the main mode |
| AI assisted, manually approved | A model drafts, a person reads and approves each text, the person posts | The original procedure ("Owner approves every batch") |
| Fully automated AI reply | A model finds posts, drafts, passes automated gates and posts with no per item human approval | The mode in use since the owner's 2026-10-04 autonomy decision: candidates that pass the gates and a blind model reviewer are posted through a scripted browser without a per batch approval |
| Solicited reply | The poster asked a question or asked for tools | Some targets asked questions |
| Unsolicited reply | The poster did not ask for input | Most targets: found by keyword search on pain phrases |
| Original technical post | A post from the account about its own work | Rare so far |

## Platform rules that apply (quoted from the archived X Help Center automation rules, harvest `h_4f03938927d44c0ca1f1844fed4033e9`)

* "the deployment or operation of any AI reply bot requires prior written and explicit approval from X."
* "sending automated replies to posts based on keyword searches alone is not permitted."
* "Use non-API-based forms of automation, such as scripting the X website. The use of these techniques may result in the permanent suspension of your account."

No written approval from X is on record.

## Recommendation

1. Discontinue the fully automated mode: stop posting AI drafted replies without per item human approval, and stop scripted posting through the browser, unless and until X gives written approval. This is subject to review of the actual operating configuration (the autonomy decision recorded in the outreach skill's `decisions.md`); the decision belongs to the owner.
2. If replies continue, keep them AI assisted and manually approved and posted, few per day, only where the poster asked a question or invited tools.
3. Put the effort into original technical posts that show what the repository actually does, using only verified results from `docs/ari/verification.md`: no predicted savings, no benchmark numbers that were not measured, and always "one heavy setup" for the author's own figures.

Context (JUDGMENT): prominent accounts in this niche publicly object to AI reply bots, so the reputational risk adds to the platform risk.
