# Athena

An async research engine that orchestrates parallel `claude -p` subagents through a five-phase pipeline:

1. **Seed ingestion** — one subagent per seed source (URL, file path, Notion page id, or topic). Each extracts key claims, quotes, citations, and open questions.
2. **Planning** — a single subagent reads the seed extracts and produces 6–10 specific subquestions.
3. **Research loop** — subquestions are researched in parallel; a gap-analysis pass decides whether to run another iteration or converge.
4. **Synthesis** — a single subagent produces the final report from all findings.
5. **Output** — the report is written to a Notion page and (optionally) pinged to Telegram.

State is persisted to `runs/<run_id>/state.json` after every phase, so a crashed run leaves enough behind to inspect or resume.

## Requirements

- Python 3.10+
- The `claude` CLI on `$PATH` (or set `CLAUDE_BIN`), authenticated for non-interactive use.
- A Notion integration with access to the parent page you want reports written under.
- (Optional) A Telegram bot for completion pings.

## Environment variables

| Var | Required | Purpose |
|---|---|---|
| `NOTION_TOKEN` | for Notion output | Notion integration token |
| `NOTION_RESEARCH_PARENT_ID` | for Notion output | Parent page id under which reports are created |
| `TELEGRAM_BOT_TOKEN` | for notifications | Telegram bot token |
| `TELEGRAM_CHAT_ID` | for notifications | Chat id to ping when a run completes |
| `RESEARCH_ENGINE_RUNS_DIR` | no (default `./runs`) | Where run state and outputs are written |
| `CLAUDE_BIN` | no (default `/usr/bin/claude`) | Path to the `claude` CLI |

The `claude` CLI inherits its own credentials independently — make sure it's authenticated before launching a run.

## Running

```bash
python -m research_engine \
    --topic "What conditions produce great people of history?" \
    --seed "https://en.wikipedia.org/wiki/Great_man_theory" \
    --seed "Thomas Carlyle, On Heroes (1841)" \
    --time-cap-hours 3 \
    --max-iterations 5
```

Seeds may be:
- A URL — fetched with WebFetch
- An absolute file path — read directly
- A 32- or 36-char hex id — treated as a Notion page
- Anything else — treated as a topic and researched via search

Alternatively, pipe a JSON body on stdin:

```bash
echo '{"topic": "...", "seeds": ["..."]}' | python -m research_engine
```

To run in the background and detach:

```bash
nohup python -m research_engine --topic "..." > run.log 2>&1 &
```

## Output

Each run produces:
- `runs/<run_id>/state.json` — full run state (topic, seeds, plan, costs, errors)
- `runs/<run_id>/log.txt` — phase-by-phase log
- `runs/<run_id>/seeds/*.md` — per-seed extracts
- `runs/<run_id>/findings/iter*.md` — per-iteration research findings
- `runs/<run_id>/plan.md` — initial research plan
- `runs/<run_id>/gaps_iter*.md` — gap analyses between iterations
- `runs/<run_id>/final.md` — final synthesis report
- A new Notion page under `NOTION_RESEARCH_PARENT_ID`

## Architecture notes

Subagents are spawned as `claude -p <prompt> --output-format json`. They inherit the `claude` CLI's authentication and tool permissions, so anything the CLI is allowed to do (WebSearch, WebFetch, Read, etc.) is available in the subagents for free.

Per-agent timeouts are bounded by both an absolute ceiling and a dynamic budget that divides remaining wall-clock time across the work in the current iteration. A stuck subagent can't hang the whole run.
