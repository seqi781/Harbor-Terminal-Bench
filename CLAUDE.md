# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

An AI agent built for the [Harbor terminal benchmark](https://github.com/harbor-framework/terminal-bench) platform. The agent runs inside isolated Docker environments and solves terminal tasks. It uses the MiMo API (`https://api.xiaomimimo.com/v1`, model `mimo-v2.5-pro`) which is OpenAI-compatible.

Harbor loads the agent via: `harbor run --agent-import-path main:MiMoAgent`

## Common Commands

```bash
# Run the agent on a single harbor task
harbor run --agent-import-path main:MiMoAgent --task terminal-bench/<task-name>

# Run a batch job from a config file (e.g. negative verification)
harbor run --config nop_verify.json

# View API key usage and cost tracking
python show_keys.py

# Run tests
uv run pytest tests/

# Run a single test file
uv run pytest tests/test_agent_loop.ipynb

# Install dependencies
uv sync
```

## Architecture

```
main.py               # Entry point — exports MiMoAgent for harbor
mimo_agent/
  config.py           # Constants: MODEL, MAX_TURNS, MAX_OUTPUT_CHARS
  key_pool.py         # API key rotation and cost tracking (asyncio-safe)
  __init__.py         # Re-exports MiMoAgent; try/except so import works locally without harbor
  agent.py            # (to be written) ReAct agent loop
tests/
  test.ipynb          # Notebook: streaming, tool calls, thinking mode experiments
  test_agent_loop.ipynb
show_keys.py          # CLI: prints key_pool summary table
nop_verify.json       # Harbor batch config: runs all 78 oracle-passed tasks with nop agent
FAILED_TASKS.md       # Documents tasks that fail due to upstream apt/oracle issues (not agent bugs)
```

## Key Pool (`mimo_agent/key_pool.py`)

- Reads keys from `MIMO_API_KEYS` env var (comma-separated) or `MIMO_API_KEY`
- Keys are used **sequentially**: key[0] is used until it fails, then key[1], etc.
- A key is only marked failed on actual API errors (401/402/403/balance errors) — never on cost estimate
- Budget limit `$2.7/key` is a **display warning only** — it does not disable keys
- State is persisted to `jobs/key_pool_state.json` (atomic write via `.tmp` rename)
- `acquire()` → `record(usage)` → `release()` is the happy path per task
- `mark_failed(key, reason)` is called on API errors; triggers rotation to next key

## MiMo API Notes

- Base URL: `https://api.xiaomimimo.com/v1`
- Thinking mode is **ON by default** when no `extra_body` is passed
- To control thinking: `extra_body={"thinking": {"type": "enabled"}}` or `{"type": "disabled"}`
- `reasoning_content` appears in streaming chunks alongside `content`; they are separate fields
- Pricing (USD/1M tokens): input_cached=$0.0036, input=$0.435, output=$0.87

## Harbor Benchmark Notes

- Each task runs in a fresh Docker container with a sandboxed terminal
- The agent interacts via tool calls (e.g. `run_command`)
- `nop` agent = does nothing; used for negative verification (expected reward=0)
- `oracle` agent = reference solution; used to confirm tasks are solvable
- Job output lands in `jobs/<timestamp>/` (gitignored)
- Some tasks fail due to upstream issues (pinned apt versions unavailable, oracle bugs) — see `FAILED_TASKS.md`

## Environment

API keys live in `.env` (gitignored). Load with `python-dotenv`:

```python
from dotenv import load_dotenv
load_dotenv()
```
