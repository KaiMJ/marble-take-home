# Goal

Build an operations agent for Lantern Yard Kitchen that helps the GM and owner know **what matters and what to do next**:
- Proactively surface and answer business questions about inventory, food cost, vendors, and anomalies.
- Memory that tracks changing facts from notes and data, so agents and the GM can read and correct it.
- A daily morning brief ranked by priority, with clear actions and verifications.

Quality signals: a useful problem for the customer, grounding in business context, guardrails, and knowing where confidence ends.

# Users & decisions

| User | Needs | Decisions they own |
| --- | --- | --- |
| Maya (GM) | Act before placing orders | Ordering (Alder cutoff: noon the day before delivery), receiving, recounts, invoice disputes, recipe cards |
| Theo (owner) | Understand changes and uncertainty without reading spreadsheets | Menu/event calls (weekend menu, Oct 10 brunch), escalation |

One brief serves both: actions for Maya, a short "what changed / what's uncertain" section for Theo.

**Now** = Monday, Oct 5, 2026, 6 a.m. Data covers Sep 14 – Oct 4.

# Key business data

- **Site** (`SITE_PROFILE.md`): hours, team, tools, counting conventions.
- **Sales** (`pos_daily.csv`, `menu.csv`): daily items sold, discounts, net sales.
- **Inventory & recipes** (`inventory_counts.csv`, `recipes.csv`): opening and closing counts only, plus per-portion recipes.
- **Vendors** (`invoice_*.csv`, `vendors.csv`, `vendor_catalog.csv`): purchases, pack sizes, delivery days (sheet last reviewed Sep 14).
- **Operations context** (`GM_OPS_LOG.md`): price changes, route changes, events, corrections, and things the CSVs don't capture.

# Scope

## 1. Stable reconciliation

Numbers must be reproducible: the same inputs give the same results, and every figure traces to row or note IDs. The agent explains numbers but never invents them.

Core check per ingredient: **opening count + purchases − closing count** vs. **recipe × POS sold**.

Key finding: most ingredients (rice, beans, oil, avocado, eggs) reconcile exactly. Every remaining gap should be **explained by a note** or **flagged as unknown**, never silently absorbed.

Anomaly classes the system must catch:

| Class | Example in packet |
| --- | --- |
| Billing error | INV-010-L01 peppers at $210/case vs. $21 (about $378 over) |
| Receipt status vs. reality | INV-099 marked received, delivery date Oct 6, and N15 says the cases weren't seen |
| Unit / pack change | Chicken moved from 40 lb to 20 lb cases (AW-CHK20, N11); INV-007 billed per lb |
| Price change | Chicken $3.20 → $3.65/lb from Sep 28 (N03, INV-013) |
| Stale reference data | `vendors.csv` says Alder delivers Mon/Thu; N09 says Mon/Wed from Sep 28 |
| Recipe gap | Weekend special (M99) has no recipe card (N04, N10), so its usage can't be reconciled |
| Unexplained usage | Remaining chicken, pepper, tortilla, and cheese variance (M99, plus unweighed waste in N06) |
| Unreliable count | Closing chicken count is at risk if 20 lb cases were counted as 40 lb (N11) |

"Food cost" means **purchases ÷ net sales**, the owner's loose definition. It is labeled as such, with distortions noted: the INV-010 error, and inventory build-up (most closing counts rose).

## 2. Memory

Memory holds **facts**, each with a source (row or note ID), the date it takes effect, and a status: `asserted`, `verified`, `superseded`, `retracted`, or `unverified`.

**Conflict rules** (who wins):
1. A newer note from the person who checked beats an older one (N14 retracts N13's egg hold).
2. A physical check beats an office entry (N15 vs. INV-099's "received" status).
3. A dated note beats a stale reference sheet (N09 overrides `vendors.csv`).
4. Future-dated facts apply only from their effective date (N03's price takes effect Sep 28).

**Nothing is deleted.** Old facts are marked superseded or retracted, so "what did we believe on Friday?" stays answerable.

**GM edits are just new notes.** There is no special edit path, which matches how Maya already works.

**Open questions live in memory too**, e.g. "M99 recipe missing" and "INV-099 cases physically arrived?". They stay on the brief until resolved.

## 3. Actions & verification

The agent recommends; humans act. Allowed action types:
- **Verify:** recount, or inspect physical stock.
- **Confirm:** call the office or a vendor rep.
- **Dispute:** an invoice line.
- **Record:** write a recipe card, log waste.
- **Prepare:** an input for order sizing, not the order itself.

Each action has an owner, a deadline, and cited evidence.

**Guardrails:** never place orders or contact vendors. Every claim cites its source. Evidence is kept separate from inference. When data is insufficient, say so.

## 4. Runway (not forecasting)

**Runway** = on-hand ÷ recent daily usage, compared against the next delivery date. It is stated with its assumptions: "at the last 7-day rate, assuming the count is correct." Unknown demand (the Oct 10 brunch, the market opening) is flagged as a range or as unknown, not modeled.

# Morning brief (Mon Oct 5), draft ranking

1. **Chicken runway before Wednesday's Alder delivery.** About 48 lb on hand, if the count is right, against roughly 25–28 lb/day. The order cutoff is Tue noon, and that order must cover the weekend and Oct 10.
2. **INV-099 receipt conflict.** Verify the cases physically before relying on that stock or paying the invoice.
3. **INV-010 pepper billing error, about $378.** Dispute it; it also inflates food cost.
4. **Missing Weekend special recipe (M99).** It blocks accurate usage and cost. Record the recipe card.
5. **For Theo:** chicken costs about 14% more per lb since Sep 28, the Oct 10 brunch has no demand data, and the vendor sheet is stale.

# Demo scenarios

1. **"Why might food cost be going up?"** Catches the INV-010 error and the chicken price change, and separates the real increase from the data error.
2. **"What should I worry about before Saturday?"** Chicken runway, INV-099, the Oct 10 brunch uncertainty (says what we don't know).
3. **Live memory update:** feed a new note ("INV-099 cases arrived Tue, 2×20 lb"). The fact moves from `unverified` to `verified`, the open question closes, and the brief changes.
4. **Contradiction:** N13's egg hold, then N14's retraction. Memory shows the superseded fact, and the brief doesn't rely on it.

# Out of scope (and why)

- **Per-user briefs / notifications:** can be built on the memory layer later (e.g. push to a specific user).
- **Inventory forecasting & vendor switching:** three weeks of data is too thin, and the GM has better context. We summarize and compute runway only.
- **Inferring the M99 recipe from variance:** confounded with unweighed waste (N06). We alert and ask instead of guessing.
- **Frontend:** a minimal terminal interface that shows metrics, takes questions, and shows memory state.

# Deliverables

- Sample morning brief and demo conversations (including incomplete or contradictory evidence)
- AI chat transcripts
- `ARCHITECTURE.md`: key decisions, memory design, production failure modes, testing
