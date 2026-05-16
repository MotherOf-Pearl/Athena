"""CLI entry point.

Usage:
  python -m research_engine \
      --topic "How should we build NOUS ..." \
      --seed https://arxiv.org/abs/... \
      --seed /home/claude/founding_brief.pdf \
      --time-cap-hours 3 \
      --max-iterations 5

If --topic is omitted, reads {"topic": "...", "seeds": [...]} JSON from stdin.

Run-in-background pattern:
  nohup python -m research_engine --topic "..." > /tmp/run.log 2>&1 &
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import sys
import time

import config
import orchestrator
import state


def main() -> int:
    parser = argparse.ArgumentParser(description="Research engine")
    parser.add_argument("--topic", help="Topic to research (or pass JSON via stdin)")
    parser.add_argument("--seed", action="append", default=[], help="A seed source (URL, file path, notion id, or topic). Repeatable.")
    parser.add_argument("--time-cap-hours", type=float, default=config.DEFAULT_TIME_CAP_HOURS)
    parser.add_argument("--max-iterations", type=int, default=config.DEFAULT_MAX_ITERATIONS)
    parser.add_argument("--max-parallel", type=int, default=config.DEFAULT_MAX_PARALLEL)
    parser.add_argument("--parent-page-id", default=config.NOTION_RESEARCH_PARENT_ID,
                        help="Notion parent page for the report. Defaults to $NOTION_RESEARCH_PARENT_ID.")
    parser.add_argument("--chat-id", default=config.DEFAULT_NOTIFY_CHAT_ID,
                        help="Telegram chat id for the completion ping. Defaults to $TELEGRAM_CHAT_ID.")
    parser.add_argument("--no-notify", action="store_true")
    parser.add_argument("--resume", metavar="RUN_ID",
                        help="Resume a previous run by id. Loads its saved state and skips phases already complete.")
    parser.add_argument("--verbose", "-v", action="store_true")

    args = parser.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(name)s %(levelname)s %(message)s",
    )

    if args.resume:
        run_state = state.load_run(args.resume)
        # Reset the wall-clock budget so a resumed run gets a fresh window.
        run_state.started_at = time.time()
        run_state.time_cap_s = args.time_cap_hours * 3600
        run_state.error = None
        run_state.done = False
        run_state.save()
        print(f"resuming run_id={run_state.run_id}", file=sys.stderr)
        print(f"phase={run_state.phase} seed_summaries={len(run_state.seed_summaries)} cost_so_far=${run_state.total_cost_usd:.2f}", file=sys.stderr)
    else:
        topic = args.topic
        seeds = list(args.seed)
        if not topic:
            if sys.stdin.isatty():
                parser.error("Provide --topic, --resume, or JSON via stdin")
            data = json.load(sys.stdin)
            topic = data["topic"]
            seeds = data.get("seeds", []) + seeds

        if not topic.strip():
            parser.error("Empty topic")

        run_state = state.new_run(
            topic=topic,
            seeds=seeds,
            time_cap_hours=args.time_cap_hours,
            max_iterations=args.max_iterations,
            max_parallel=args.max_parallel,
            notify_chat_id=None if args.no_notify else args.chat_id,
            parent_page_id=args.parent_page_id,
        )
        print(f"run_id={run_state.run_id}", file=sys.stderr)
    print(f"dir={run_state.directory()}", file=sys.stderr)

    final = asyncio.run(orchestrator.run(run_state))

    if final.error:
        print(f"FAILED: {final.error}", file=sys.stderr)
        return 1

    print(final.final_notion_url or "(no notion url)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
