# Architecture

## Overview

```
data/*.csv + GM_OPS_LOG.md
        │
        ▼
Integration ── load CSVs as-is (no unified schema yet)
        │
        ▼
Function Nodes (pure Python) ── reconciliation, runway, rules, alerts
        │                         deterministic, every figure carries row/note IDs
        ▼
Memory (SQLite long-term, context.json short-term)
        │   facts · open_questions · rules   (append-only, versioned)
        ▼
Reasoning Node (LLM) ── parse notes → candidate facts, rank + narrate findings
        │                 never computes numbers; only cites them
        ▼
Intervention Node ── human approval / verification required
        │
        ▼
Terminal UI ── morning brief, Q&A, memory state
```

Trigger Node runs the pipeline daily (6 a.m. brief) or on a new note.
Every step is logged. Stack: Python + LiteLLM, no agent framework.

## Key decisions

- **Numbers come from code, words come from the LLM.** Reconciliation and runway
  are pure Python, so the same inputs always produce the same figures. The LLM
  explains and ranks them but cannot introduce a number that isn't in a tool result.
- **No schema unification yet.** The CSVs are loaded directly. A unified integration
  layer is the first thing to build for a second site or vendor.
- **Agent recommends, humans act.** No orders and no vendor contact. Any action
  that leaves the system goes through the Intervention Node for human approval.
- **Conflict rules live in Python**, not in prompts, so they are testable and
  consistent (see Memory).

## Memory

### Storage

| Layer | Store | Purpose |
| --- | --- | --- |
| Short-term | `context.json` | Working context for the current run/session: site, user, recent notes, in-flight reasoning |
| Long-term | SQLite | Durable, versioned record of facts, open questions, and rules |

Long-term memory has three tables. Each one uses the same envelope: fixed metadata
columns plus a flexible JSONB `body`.

```
id, version, status, effective_from, recorded_at, author, source_ids, supersedes, body (JSONB)
```

- **facts**: what we believe (for example: chicken price $3.65/lb from Sep 28,
  source N03 and INV-013).
- **open_questions**: what we don't know yet (M99 recipe missing, INV-099 arrival).
  They stay on the brief until a fact resolves them.
- **rules**: business rules and conventions (for example: Alder cutoff is noon the
  day before delivery, counting conventions).

The flexible `body` lets new kinds of facts in without migrations. The fixed
metadata columns keep querying, ordering, and conflict resolution uniform.
Pydantic models validate each `body` type at write time. Migration files seed
SQLite from `data/*.csv`.

### Retained vs. forgotten

- **Everything is retained.** Writes are append-only, so an edit creates a new
  version and never overwrites the old one.
- **"Forgotten" is a status**, not a delete: `asserted`, `verified`, `unverified`,
  `superseded`, `retracted`.
- **Queries return the latest approved version by default.** An `as_of` parameter
  replays past beliefs ("what did we believe on Friday?").
- **GM edits are new notes.** They go through the same path as any other source.

### Conflict rules (Python resolver)

1. A newer note from the person who checked beats an older one (N14 retracts N13).
2. A physical check beats an office entry (N15 beats INV-099's "received").
3. A dated note beats a stale reference sheet (N09 beats `vendors.csv`).
4. A future-dated fact applies only from its `effective_from` (N03).

When a new version arrives, the resolver re-runs the affected validation checks.
That can close open questions or open new ones.

### LLM writes

The Reasoning Node can propose facts parsed from notes, but each proposal must
cite a source ID. It lands as `asserted` or `unverified` and becomes `verified`
only after a rule check or human approval.

## Tools (CLI / SDK / MCP)

Tools are plain Python functions registered with the LLM. There is no tool search
yet; queries are flexible filters over the shared envelope.

- **search**: query, filter, or extract across facts, open_questions, and rules,
  with `as_of` and status filters.
- **edit**: append a new version (add or modify). Never an in-place update.
- **compute**: reconciliation and runway per ingredient.

## Agentic nodes

| Node | Role |
| --- | --- |
| Reasoning | Parse notes, verify against facts, rank and narrate findings |
| Function | Reconciliation, runway, rules, alerts, action drafting |
| Intervention | Human approval / verification gate before any action is accepted |
| Trigger | Schedule (daily brief) and events (new note → re-resolve, re-brief) |

The brief is ranked as described in the README: deadline proximity (the order
cutoff), dollar or stockout impact, and whether the item blocks other numbers.

## UI

Terminal only. It shows the brief, takes questions, and shows memory state
(versions and statuses).

## Left out (and why)

- **Unified integration schema:** not needed for a single site's CSVs.
- **Forecasting and vendor switching:** three weeks of data is too thin. We
  compute runway only.
- **Inferring the M99 recipe:** confounded with unweighed waste (N06). We ask
  instead of guessing.
- **Per-user briefs, notifications, and a frontend:** these can be built on top of
  the memory layer later.
- **Tool search:** the tool count is small enough to register them all.

## Likely production failures

- **Bad extraction:** a misread unit (20 lb vs. 40 lb case) silently corrupts
  runway. Mitigation: require a source span and keep the fact `unverified`
  until checked.
- **Hallucinated numbers or IDs:** the LLM states a figure no tool returned.
  Mitigation: an output validator checks every number and ID against tool results.
- **Garbage counts:** a wrong count still reconciles neatly. Count reliability
  has to be its own fact.
- **Ambiguous conflicts:** same-timestamp notes or an unclear author. The resolver
  falls back to opening an open_question instead of picking a winner.
- **Stale reference data with no overriding note:** nothing flags it.
  Mitigation: age-based staleness rules.
- **Date bugs:** timezone or `effective_from` off-by-one errors (Sep 28 price,
  noon cutoff).
- **Alert fatigue:** a long, noisy brief gets ignored. Cap the brief and rank hard.
- **Short-term/long-term drift:** `context.json` diverges from SQLite. SQLite is
  the source of truth; context.json is rebuilt from it on every run.
- **Scalability:** the integration layer, new capabilities needing new nodes, and
  evaluation all grow with each customer.

## Testing (outline)

- Golden tests: reconciliation and runway outputs on the data packet.
- The demo scenarios from the README as end-to-end eval cases.
- Citation coverage: every claim in the brief traces to an ID.
- As-of replay of memory.
- Usefulness: whether the GM acts on the top items, and the false-alarm rate.



## File Structure

```
backend/
  src/
    marble/              # domain: what Marble knows about restaurant ops
      reconciliation.py  # opening + purchases − closing vs recipe × sold
      runway.py
      rules/             # conflict rules, alert rules, staleness checks
      ranking.py         # brief priority
      prompts/           # system prompts, brief template
      flows/             # morning_brief, ask, ingest_note
    ai/
      runtime/           # litellm wrapper, step logging
      nodes/             # reasoning, intervention, trigger
      tools/             # registrations → marble / memory
      checks/            # output validator (numbers/IDs cited)
    memory/
      db/                # sqlite access, append-only writes, as_of queries
      schemas/           # pydantic envelope + body types
      migrations/        # seed from data/*.csv
    api/                 # thin interface used by main.py
  tests/                 # golden + scenario evals
  var/                   # context.json, logs (gitignored)
  main.py                # terminal UI
data/                    # provided packet (read-only)
```

Deterministic code in `marble/` (reconciliation, runway, rules, ranking) never
imports from `ai/`. The LLM reaches it only through registered tools.
