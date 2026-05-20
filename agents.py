"""Headless claude subprocess wrappers.

Each call spawns `claude -p <prompt> --output-format json`. The claude binary
inherits the user's allowed tools (WebSearch, WebFetch, Read, etc.) so we get
the full toolbox in every subagent for free.

Outputs:
    AgentResult(text, cost_usd, duration_s, num_turns, error, transient)

Transient errors (rate limits, server overload, 5xx, network) are retried
internally by `run_subagent_with_retry` with exponential backoff. Non-transient
errors (auth, malformed prompt, etc.) bail immediately.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
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
    transient: bool = False


_TRANSIENT_PATTERNS = (
    "rate_limit",
    "rate limit",
    "overloaded",
    "internal server error",
    "service unavailable",
    "bad gateway",
    "gateway timeout",
    "connection error",
    "connection reset",
    "connection refused",
    "timeout",
    "econnreset",
    "etimedout",
)

_HTTP_TRANSIENT_CODES = (" 429 ", " 500 ", " 502 ", " 503 ", " 504 ", "429:", "500:", "502:", "503:", "504:")


def _is_transient(blob: str) -> bool:
    low = blob.lower()
    if any(p in low for p in _TRANSIENT_PATTERNS):
        return True
    if any(c in blob for c in _HTTP_TRANSIENT_CODES):
        return True
    return False


def _parse_claude_output(stdout_bytes: bytes, stderr_bytes: bytes) -> Optional[dict]:
    """Try hard to recover Claude's JSON output even on failure.

    claude -p writes a single JSON object to stdout. On some failure paths it
    still emits that JSON (with is_error=true); on others stdout is empty and
    the only signal is in stderr.
    """
    s = stdout_bytes.decode("utf-8", "ignore").strip()
    if s:
        try:
            return json.loads(s)
        except json.JSONDecodeError:
            # Look for the first {...} block — some failure paths prepend logs.
            m = re.search(r"\{.*\}", s, re.DOTALL)
            if m:
                try:
                    return json.loads(m.group(0))
                except json.JSONDecodeError:
                    pass
    return None


async def run_subagent(
    prompt: str,
    timeout_s: int = 900,
    allowed_tools: Optional[list[str]] = None,
    cwd: Optional[str] = None,
) -> AgentResult:
    """Run a single headless claude call and return parsed result.

    timeout_s defaults to 15 minutes; long enough for a deep WebFetch + reasoning
    pass but bounded so a stuck subagent can't hang the whole run.

    On any failure path, the returned AgentResult.error carries the best signal
    we could recover from stdout JSON first, then stderr; `transient` is True
    when the error class is a rate-limit / overload / network / timeout that
    `run_subagent_with_retry` will back off and retry.
    """
    started = time.monotonic()
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
            return AgentResult("", 0.0, time.monotonic() - started, 0, error="timeout", transient=True)
    except Exception as e:
        return AgentResult("", 0.0, time.monotonic() - started, 0, error=f"spawn: {e}", transient=True)

    duration = time.monotonic() - started
    data = _parse_claude_output(stdout, stderr)

    # Nonzero exit: try to recover the real error from stdout JSON, fall back to stderr.
    if proc.returncode != 0:
        stderr_str = stderr.decode("utf-8", "ignore").strip()
        stdout_str = stdout.decode("utf-8", "ignore").strip()
        if data and data.get("is_error"):
            err = str(data.get("result", "unknown"))[:500]
        else:
            err = stderr_str or stdout_str[:500] or f"exit {proc.returncode} with empty output"
        # A nonzero exit with no parseable output and empty stderr/stdout is almost
        # always an environmental crash (CLI panic, OOM, transient bun/node hiccup,
        # silently-swallowed upstream rejection) rather than a permanent error like
        # auth or malformed prompt — those produce a JSON error or write to stderr.
        # Classify as transient so the retry loop can ride it out.
        silent_crash = not data and not stderr_str and not stdout_str
        return AgentResult(
            "",
            float((data or {}).get("total_cost_usd", 0.0)),
            duration,
            int((data or {}).get("num_turns", 0)),
            error=f"exit {proc.returncode}: {err}",
            transient=silent_crash or _is_transient(err) or _is_transient(stderr_str),
            session_id=(data or {}).get("session_id"),
        )

    # Exit 0 but no JSON we could parse — treat as transient (likely a half-written response).
    if not data:
        return AgentResult(
            "",
            0.0,
            duration,
            0,
            error=f"json parse failed; raw stdout={stdout[:300]!r}",
            transient=True,
        )

    # Exit 0 with is_error=true (e.g., "Not logged in", malformed prompt). Classify carefully.
    if data.get("is_error"):
        err = str(data.get("result", "unknown"))[:500]
        return AgentResult(
            "",
            float(data.get("total_cost_usd", 0.0)),
            duration,
            int(data.get("num_turns", 0)),
            error=err,
            transient=_is_transient(err),
            session_id=data.get("session_id"),
        )

    return AgentResult(
        text=str(data.get("result", "")),
        cost_usd=float(data.get("total_cost_usd", 0.0)),
        duration_s=duration,
        num_turns=int(data.get("num_turns", 0)),
        session_id=data.get("session_id"),
    )


# Exponential backoff schedule used by run_subagent_with_retry. Total wall time
# at max retries: 30 + 60 + 120 + 240 + 480 = ~15.5 min. Picked so a single
# rate-limit storm during the 5h Anthropic rolling window has time to clear.
RETRY_BACKOFF_S = (30, 60, 120, 240, 480)


async def run_subagent_with_retry(
    prompt: str,
    timeout_s: int = 900,
    max_retries: int = len(RETRY_BACKOFF_S),
    allowed_tools: Optional[list[str]] = None,
    cwd: Optional[str] = None,
) -> AgentResult:
    """Run a subagent, retrying transient failures with exponential backoff.

    Returns the last AgentResult — success on any attempt, or the final failure
    if all attempts are exhausted.
    """
    last: Optional[AgentResult] = None
    for attempt in range(max_retries + 1):
        result = await run_subagent(prompt, timeout_s=timeout_s, allowed_tools=allowed_tools, cwd=cwd)
        if not result.error:
            return result
        last = result
        if not result.transient or attempt >= max_retries:
            return result
        delay = RETRY_BACKOFF_S[min(attempt, len(RETRY_BACKOFF_S) - 1)]
        log.warning(
            "subagent transient failure (attempt %d/%d): %s — sleeping %ds",
            attempt + 1, max_retries + 1, result.error[:200], delay,
        )
        await asyncio.sleep(delay)
    assert last is not None
    return last


async def run_many(
    prompts: list[str],
    max_parallel: int = 4,
    timeout_s: int = 900,
    max_retries: int = len(RETRY_BACKOFF_S),
) -> list[AgentResult]:
    """Run a batch of subagents with a concurrency cap. Each retries transient
    failures independently before reporting back."""
    sem = asyncio.Semaphore(max_parallel)

    async def bounded(p: str) -> AgentResult:
        async with sem:
            return await run_subagent_with_retry(p, timeout_s=timeout_s, max_retries=max_retries)

    return await asyncio.gather(*(bounded(p) for p in prompts))


def count_transient_failures(results: list[AgentResult]) -> int:
    """Count how many results in a batch failed with a transient error class
    (used by the orchestrator to apply backpressure on max_parallel)."""
    return sum(1 for r in results if r.error and r.transient)
