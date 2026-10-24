# Lantern Yard Ops Agent

A CLI agent for one restaurant: answers operator questions from invoices, POS, counts, recipes and the shift log, keeps a versioned memory, and writes a ranked morning brief. Every claim cites row or note IDs.

## Setup

Requires Python 3.11 and [uv](https://docs.astral.sh/uv/).

```bash
cp backend/.env.example .env      # config reads .env from the repo root

LLM_MODEL='xai/grok-4.7' # or Any [litellm](https://docs.litellm.ai/docs/providers)
LLM_REASONING_EFFORT="high" # recommended

cd backend && uv sync
```
## Run

```bash
cd backend
uv run python main.py load --reset-memory   # build SQLite from data/ + docs/, seed memory
uv run python main.py ingest-log            # replay GM notes N01–N15 into memory, in time order
uv run python main.py brief                 # morning brief for Mon 2026-10-05 06:00
uv run python main.py chat                  # ask questions or report new facts
```

Outputs:
- `backend/data/memories/memory.md` — memory (one markdown file in a git repo; `git log` shows every change and why)
- `backend/data/runs/<timestamp>/` — transcript, python and memory logs per session

## Cost

Grok: xai/grok-4.7, high reasoning effort:

Log ingestion + one brief run + one interactive chat session:
- Input (prompt) tokens: 1,675,611
- Output (completion) tokens: 64,360
- Total: 1,739,971
- 19 runs (124 LLM Calls)
- total cost: $2.06

## Submission contents

- `ARCHITECTURE.md` — design, memory model, failure modes, evaluation
- `samples/` — morning brief and one interactive conversation
- `transcripts/` — AI coding sessions
