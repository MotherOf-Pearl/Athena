"""Headless claude subprocess wrappers.

Each call spawns `claude -p <prompt> --output-format json`. The claude binary
inherits the user's allowed tools (WebSearch, WebFetch, Read, etc.) so we get
the full toolbox in every subagent for free.

Outputs:
    AgentResult(text, cost_usd, duration_s, num_turns, error)
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import subprocess
import time
from dataclasses import dataclass
from typing import Optional

import config

log = logging.getLogger(__name__)


@dataclass
class AgentResult:
    text: str
    cost_usd: float
    duration_s: float
    num_turns: int
    error: Optional[str] = None
    session_id: Optional[str] = None


async def run_subagent(
    prompt: str,
    timeout_s: int = 900,
    allowed_tools: Optional[list[str]] = None,
    cwd: Optional[str] = None,
) -> AgentResult:
    """Run a single headless claude call and return parsed result.

    timeout_s defaults to 15 minutes; long enough for a deep WebFetch + reasoning
    pass but bounded so a stuck subagent can't hang the whole run.
    """
    started = time.monotonic()
    # Pass the prompt via stdin so we never hit ARG_MAX. Some synthesis prompts
    # carry the full set of seed summaries (~150KB+) which exceeds Linux's
    # ~128KB argv limit and would crash with E2BIG.
    args = [config.CLAUDE_BIN, "-p", "--output-format", "json"]
    if allowed_tools:
        args += ["--allowedTools", ",".join(allowed_tools)]

    log.info("subagent start (prompt_len=%d)", len(prompt))
    try:
        proc = await asyncio.create_subprocess_exec(
            *args,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=cwd,
            env={**os.environ},
        )
        try:
            stdout, stderr = await asyncio.wait_for(
                proc.communicate(input=prompt.encode("utf-8")),
                timeout=timeout_s,
            )
        except asyncio.TimeoutError:
            proc.kill()
            await proc.wait()
            return AgentResult("", 0.0, time.monotonic() - started, 0, error="timeout")
    except Exception as e:
        return AgentResult("", 0.0, time.monotonic() - started, 0, error=f"spawn: {e}")

    duration = time.monotonic() - started

    if proc.returncode != 0:
        return AgentResult(
            "",
            0.0,
            duration,
            0,
            error=f"exit {proc.returncode}: {stderr.decode('utf-8', 'ignore')[:500]}",
        )

    try:
        data = json.loads(stdout.decode("utf-8"))
    except json.JSONDecodeError as e:
        return AgentResult(
            "",
            0.0,
            duration,
            0,
            error=f"json parse: {e}; raw={stdout[:300]!r}",
        )

    if data.get("is_error"):
        return AgentResult(
            "",
            data.get("total_cost_usd", 0.0),
            duration,
            data.get("num_turns", 0),
            error=str(data.get("result", "unknown"))[:500],
            session_id=data.get("session_id"),
        )

    return AgentResult(
        text=str(data.get("result", "")),
        cost_usd=float(data.get("total_cost_usd", 0.0)),
        duration_s=duration,
        num_turns=int(data.get("num_turns", 0)),
        session_id=data.get("session_id"),
    )


async def run_many(prompts: list[str], max_parallel: int = 4, timeout_s: int = 900) -> list[AgentResult]:
    """Run a batch of subagents with a concurrency cap."""
    sem = asyncio.Semaphore(max_parallel)

    async def bounded(p: str) -> AgentResult:
        async with sem:
            return await run_subagent(p, timeout_s=timeout_s)

    return await asyncio.gather(*(bounded(p) for p in prompts))
