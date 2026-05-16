"""Research run orchestrator.

Five phases:
  1. Seed ingestion       (parallel, one subagent per seed)
  2. Initial plan         (single subagent over topic + seed summaries)
  3. Research loop        (parallel research per subquestion → synthesis → gap analysis → repeat)
  4. Final synthesis      (single subagent producing the report)
  5. Output               (write to Notion, ping Telegram)

Each phase persists state.json before returning. If the process dies, the run
directory has enough information to resume or inspect.
"""

from __future__ import annotations

import asyncio
import logging
import re

import agents
import notify
import notion_io
import prompts
from state import RunState

log = logging.getLogger(__name__)


async def run(state: RunState) -> RunState:
    try:
        await _phase_seeds(state)
        await _phase_plan(state)
        await _phase_research_loop(state)
        await _phase_synthesis(state)
        _phase_output(state)
        state.done = True
        state.phase = "done"
    except Exception as e:
        state.error = f"{type(e).__name__}: {e}"
        state.phase = "error"
        state.log(f"FATAL: {state.error}")
        _notify_failure(state)
    finally:
        state.save()
    return state


# ----- phase 1: seed ingestion -----

async def _phase_seeds(state: RunState) -> None:
    state.phase = "seeds"
    state.log(f"phase=seeds count={len(state.seeds)}")
    state.save()

    if not state.seeds:
        state.log("no seeds provided — skipping ingestion phase")
        return

    if state.seed_summaries and len(state.seed_summaries) == len(state.seeds):
        state.log(f"resuming: {len(state.seed_summaries)} seed summaries already on disk, skipping ingestion")
        return

    descriptors = [_seed_descriptor(s) for s in state.seeds]
    prompts_list = [prompts.seed_ingest_prompt(state.topic, d) for d in descriptors]
    results = await agents.run_many(prompts_list, max_parallel=state.max_parallel, timeout_s=600)

    for descriptor, result in zip(descriptors, results):
        if result.error:
            state.log(f"seed FAILED {descriptor}: {result.error}")
            state.seed_summaries.append(
                f"## Source: {descriptor}\n(failed to ingest: {result.error})"
            )
        else:
            state.seed_summaries.append(result.text)
            state.log(f"seed OK {descriptor} ({result.duration_s:.0f}s, ${result.cost_usd:.3f})")
        state.total_cost_usd += result.cost_usd

    state.save()
    _save_seeds(state)


def _seed_descriptor(raw: str) -> str:
    s = raw.strip()
    if s.startswith(("http://", "https://")):
        return f"url: {s}"
    if s.startswith("/") or s.startswith("~"):
        return f"file: {s}"
    if re.fullmatch(r"[0-9a-f]{32}|[0-9a-f-]{36}", s):
        return f"notion: {s}"
    return f"topic: {s}"


def _save_seeds(state: RunState) -> None:
    seeds_dir = state.directory() / "seeds"
    seeds_dir.mkdir(parents=True, exist_ok=True)
    for i, summary in enumerate(state.seed_summaries):
        (seeds_dir / f"{i:02d}.md").write_text(summary)


# ----- phase 2: planning -----

async def _phase_plan(state: RunState) -> None:
    state.phase = "plan"
    state.log("phase=plan")
    state.save()

    prompt = prompts.planning_prompt(state.topic, state.seed_summaries)
    result = await agents.run_subagent(prompt, timeout_s=300)
    state.total_cost_usd += result.cost_usd
    if result.error:
        raise RuntimeError(f"planning failed: {result.error}")

    state.plan = _extract_plan(result.text)
    state.log(f"plan: {len(state.plan)} subquestions ({result.duration_s:.0f}s, ${result.cost_usd:.3f})")
    if not state.plan:
        raise RuntimeError(f"planning returned no subquestions; raw output:\n{result.text[:1000]}")
    state.save()
    (state.directory() / "plan.md").write_text(result.text)


def _extract_plan(text: str) -> list[str]:
    items: list[str] = []
    for line in text.splitlines():
        m = re.match(r"^\s*\d+\.\s+(.*)", line)
        if m:
            items.append(m.group(1).strip())
    return items


# ----- phase 3: research loop -----

async def _phase_research_loop(state: RunState) -> None:
    findings_dir = state.directory() / "findings"
    findings_dir.mkdir(parents=True, exist_ok=True)

    open_questions = list(state.plan)
    all_findings: list[str] = []
    iteration = 0

    while open_questions and iteration < state.max_iterations:
        iteration += 1
        state.phase = f"research-iter{iteration}"
        state.log(f"phase={state.phase} questions={len(open_questions)} remaining_s={state.time_remaining_s():.0f}")
        state.save()

        # Per-agent timeout: enough for a deep WebFetch chain, but bounded so a stuck
        # agent can't hang the iteration. Account for parallelism — wall-time budget is
        # remaining_s, but n_questions get processed max_parallel at a time, so each
        # agent effectively gets remaining_s * max_parallel / n_questions.
        budget = state.time_remaining_s() * state.max_parallel / max(1, len(open_questions))
        per_agent_timeout = max(600, min(1200, int(budget)))

        prior = "\n\n".join(all_findings[-6:])  # last few findings give context, keep prompt size sane
        prompts_list = [prompts.research_prompt(state.topic, q, prior) for q in open_questions]
        results = await agents.run_many(prompts_list, max_parallel=state.max_parallel, timeout_s=per_agent_timeout)

        new_findings: list[str] = []
        for q, result in zip(open_questions, results):
            state.total_cost_usd += result.cost_usd
            if result.error:
                state.log(f"  research FAILED q={q!r}: {result.error}")
                continue
            new_findings.append(result.text)
            state.log(f"  research OK q={q!r} ({result.duration_s:.0f}s, ${result.cost_usd:.3f})")

        all_findings.extend(new_findings)

        # Persist this iteration's findings
        for j, finding in enumerate(new_findings):
            (findings_dir / f"iter{iteration:02d}_{j:02d}.md").write_text(finding)

        state.iterations.append({
            "iteration": iteration,
            "questions": open_questions,
            "n_findings": len(new_findings),
            "cost_usd_running": state.total_cost_usd,
        })
        state.save()

        # If we're out of time, stop.
        if state.time_remaining_s() < 600:
            state.log(f"out of time budget after iter {iteration}; exiting research loop")
            break

        # Gap analysis to set up next iteration
        if iteration < state.max_iterations:
            gap_prompt = prompts.gap_analysis_prompt(state.topic, state.plan, all_findings)
            gap_result = await agents.run_subagent(gap_prompt, timeout_s=300)
            state.total_cost_usd += gap_result.cost_usd
            if gap_result.error:
                state.log(f"gap analysis FAILED: {gap_result.error}; exiting loop")
                break
            (state.directory() / f"gaps_iter{iteration:02d}.md").write_text(gap_result.text)

            if "CONVERGED" in gap_result.text.upper():
                state.log("gap analysis says CONVERGED; exiting loop")
                break

            open_questions = _extract_plan(gap_result.text)
            state.plan.extend(open_questions)
            state.save()

    state.iterations.append({
        "iteration_total": iteration,
        "total_findings": len(all_findings),
        "total_cost_usd": state.total_cost_usd,
    })
    state.save()
    state._all_findings_for_synthesis = all_findings  # type: ignore[attr-defined]


# ----- phase 4: synthesis -----

async def _phase_synthesis(state: RunState) -> None:
    state.phase = "synthesis"
    state.log(f"phase=synthesis remaining_s={state.time_remaining_s():.0f}")
    state.save()

    findings = getattr(state, "_all_findings_for_synthesis", [])
    if not findings:
        # reload from disk if attribute was lost (e.g., resume scenario)
        findings_dir = state.directory() / "findings"
        if findings_dir.exists():
            findings = [p.read_text() for p in sorted(findings_dir.glob("*.md"))]

    if not findings:
        raise RuntimeError("no findings to synthesize")

    prompt = prompts.synthesis_prompt(state.topic, findings)
    result = await agents.run_subagent(prompt, timeout_s=1200)
    state.total_cost_usd += result.cost_usd
    if result.error:
        raise RuntimeError(f"synthesis failed: {result.error}")

    state.final_markdown = result.text
    state.final_md_file().write_text(result.text)
    state.log(f"synthesis OK ({result.duration_s:.0f}s, ${result.cost_usd:.3f}); {len(result.text)} chars")
    state.save()


# ----- phase 5: output -----

def _phase_output(state: RunState) -> None:
    state.phase = "output"
    state.log("phase=output")
    state.save()

    if not state.final_markdown:
        raise RuntimeError("no final markdown to output")

    parent = state.parent_page_id or notion_io.ensure_research_parent()
    title = _title_from_topic(state.topic)
    page_id, url = notion_io.write_report(parent, title, state.final_markdown)
    state.final_notion_url = url
    state.log(f"notion page: {url}")
    state.save()

    if state.notify_chat_id:
        msg = (
            f"Research run complete.\n\n"
            f"Topic: {state.topic[:200]}\n"
            f"Cost: ${state.total_cost_usd:.2f}\n"
            f"Iterations: {len([i for i in state.iterations if 'iteration' in i and 'total_findings' not in i])}\n"
            f"Notion: {url}"
        )
        notify.send(state.notify_chat_id, msg)


def _notify_failure(state: RunState) -> None:
    if state.notify_chat_id:
        msg = (
            f"Research run FAILED.\n\n"
            f"Topic: {state.topic[:200]}\n"
            f"Phase: {state.phase}\n"
            f"Error: {state.error}\n"
            f"Run dir: {state.directory()}"
        )
        notify.send(state.notify_chat_id, msg)


def _title_from_topic(topic: str) -> str:
    # Strip down to a reasonable page title
    t = topic.strip().split("\n")[0]
    if len(t) > 80:
        t = t[:77] + "..."
    return t
