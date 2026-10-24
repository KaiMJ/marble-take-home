# Lantern Yard Ops Agent

A CLI agent for one restaurant: answers operator questions from invoices, POS, counts, recipes and the shift log, keeps a versioned memory, and writes a ranked morning brief. Every claim cites row or note IDs.

## Setup

Requires Python 3.11 and [uv](https://docs.astral.sh/uv/).

```bash
cp backend/.env.example .env      # config reads .env from the repo root
# set ANTHROPIC_API_KEY (or another litellm provider key + LLM_MODEL)
cd backend && uv sync
```

| Variable | Default | Notes |
| --- | --- | --- |
| `LLM_MODEL` | `xai/grok-4.3-latest` | Any [litellm](https://docs.litellm.ai/docs/providers) model string; `.env.example` uses `anthropic/claude-opus-5-5` |
| `LLM_REASONING_EFFORT` | `low` | `high` recommended; at `low` the model stops investigating after a few queries |
| `TRACE` | `1` | Live tool calls and reasoning on stderr; `0` to silence |

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

<!-- TODO: fill from a measured run -->
On `anthropic/claude-opus-5-5` at high effort, one `brief` run took 8 LLM calls, 139k input / 6.8k output tokens, $0.21. `ingest-log` cost: TODO (15 short turns). Each turn prints its usage.

## Submission contents

- `ARCHITECTURE.md` — design, memory model, failure modes, evaluation
- `samples/` — morning brief and representative conversations <!-- TODO -->
- `transcripts/` — AI coding sessions
