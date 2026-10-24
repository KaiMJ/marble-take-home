# Architecture

## Shape

One tool-using loop (`agent.py`), two tools, two stores.

```
entry (ingest-log | chat | brief+snapshot)
  → system prompt rebuilt each step: rules + mode + now + schema + memory
  → LLM ⇄ tools (≤25 steps)
       python  — read-only pandas/SQL session over the records
       memory  — markdown files in a git repo
  → citation check (up to 2 corrective passes, then a visible warning)
  → answer + confirmed memory changes
```

| Choice | Why |
| --- | --- |
| SQLite + pandas sandbox, model writes its own queries | Questions are open-ended; fixed tools would cap what it can investigate. Read-only DB, subprocess, 30s timeout. |
| Fixed snapshot in chat and brief (`snapshot.py`) | Anything the operator always needs is computed by code: anomaly checks (price outliers, header vs line totals, received-but-future, off-schedule deliveries), stock coverage to the next two deliveries with order quantities, reconciliation, prices. The model ranks, explains and digs; it doesn't have to discover the basics. Not shown during log replay, which runs in the past. |
| Citation check on every answer | Only checks what code can verify: every cited ID exists. Whether the claim matches the row is left to evaluation (below), not regex heuristics. |
| Tool errors return a `Fix:` hint | Cheaper models recover in one step instead of looping. |
| litellm | Model-agnostic; swap via `LLM_MODEL`. |

## Memory

**Retained:** what the records can't tell a future session — vendor-reported changes, readings of the records (which ingredient a SKU is), corrections, open questions and hypotheses. Not retained: copies of notes or numbers a query can recompute.

**Layout:** one file, `memory.md`, with fixed sections (Vendors, Ingredients, Menu, Operations, Open, Data), loaded whole into every prompt. One file means no index to navigate and no read calls; it stays small because records and notes are never copied in. Past briefs are archived under `briefs/` and not loaded.

**Changing facts:** one fact lives in one place and is edited in place. The old value is struck through with a pointer to the new one and its source (`~~Mon/Thu [V1]~~ → Mon/Wed from 2026-09-28 [N09]`). Every write is a git commit with a reason; `view as_of=<time>` reads memory as it was. A dated vendor override keeps the previous value for earlier dates, so Thursday deliveries before Sept 28 still check as on schedule.

**Corrections change the data:** Data section lines (`- map AW-CHK20 -> Chicken… [N11]`, `- set invoice INV-099 entered_status = … [N15]`) are applied to normalized tables after every write, tagged `overridden_by`. `raw_*` tables are never touched. Deleting the line undoes it.

**Invalidated / forgotten:** data checks open a question under Open for each unlabeled SKU or recipe-less item and close it automatically when the gap is resolved. Superseded beliefs stay visible but struck. Nothing is deleted silently; nothing expires on its own (see gaps).

**Replay:** `ingest-log` runs each note at its own timestamp and hides later notes, so memory is built as it would have been live. The N13 → N14 egg-hold reversal is the conflict test.

## Left out

- Order placement or vendor contact (advice only, per brief)
- Web UI, auth, multi-site
- Automatic expiry of memory facts
- Demand forecasting beyond weekday averages (the Oct 10 brunch spike is left to the model and the GM)

## Production failures and how to test

**Likely failures**
- *Confident wrong numbers:* model writes a bad query (unit mix-up, wrong join) and cites real IDs. The citation check proves the IDs exist, not that the arithmetic is right.
- *Memory drift:* wrong correction in the Data section silently changes every later query. Mitigated by `overridden_by` and the snapshot's overrides table, not prevented.
- *Stale facts:* nothing ages out; a vendor schedule from months ago reads as current.
- *Bad source data accepted:* typos (INV-010-L01 at 10× price), office-entered "received" status (INV-099), case-size changes (AW-CHK20).
- *Prompt growth:* memory block grows with every session.
- *Model swap regressions:* behavior depends on model; litellm makes swapping easy, not safe.

**Testing usefulness and trust**
- Golden set from this packet: each planted issue (price typo, 20 lb case, route change, phantom delivery, egg-hold reversal, missing M99 recipe, walk-in loss) with the expected finding. Score recall and false alarms per model.
- Numeric checks: compare the agent's figures with hand-computed answers for 10 questions.
- Memory tests: replay the log, then assert on the Data and Open sections and that superseded facts are struck, not lost.
- In production: did Maya act on the brief items, and how often did she correct the agent? Track corrections per week as the trust metric.
