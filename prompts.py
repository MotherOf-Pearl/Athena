"""Prompt templates for each phase of a research run.

Subagents are stateless `claude -p` invocations. Every prompt must carry its
context inline. Output schemas are markdown — easier to inspect and concatenate
than JSON, and the synthesis phase consumes them directly.
"""

from __future__ import annotations

import json
import textwrap


def seed_ingest_prompt(topic: str, seed_descriptor: str) -> str:
    """Ingest a single seed source.

    seed_descriptor is one of:
      - "url: <url>" — fetch and extract
      - "file: <abs_path>" — Read and extract
      - "notion: <page_id>" — read via Notion MCP and extract
      - "topic: <name/person/company>" — treat as a directed search target
    """
    return textwrap.dedent(f"""
        You are a research assistant ingesting a seed source for a larger research project.

        Overall research topic: {topic}

        Seed source: {seed_descriptor}

        Your job:
        1. Fetch / read the source using whatever tool is appropriate (WebFetch for URLs,
           Read for files, the Notion MCP for notion: ids, WebSearch + WebFetch for topics).
        2. Extract:
           - The 3-7 most important claims, facts, or findings from this source relevant to
             the overall topic.
           - Direct quotes or specific numbers worth carrying forward.
           - References, citations, or pointers to other sources that look worth chasing.
           - Open questions the source raises but does not fully answer.

        Output format (markdown, no preamble):

        ## Source: <one-line title or URL>

        ### Key claims
        - Claim 1 with the specific detail
        - Claim 2 ...

        ### Worth carrying forward
        - Quote / number / specific finding

        ### Pointers to chase
        - <name or URL or topic> — why it matters

        ### Open questions
        - Question raised but not answered

        Be terse. If the source is irrelevant or inaccessible, say so in one line.
    """).strip()


def planning_prompt(topic: str, seed_summaries: list[str]) -> str:
    """Phase 2: produce an initial research plan as a list of subquestions."""
    joined = "\n\n---\n\n".join(seed_summaries) if seed_summaries else "(no seeds provided)"
    return textwrap.dedent(f"""
        You are planning a deep research run on the following topic.

        Topic: {topic}

        You have already ingested these seed sources. Read their extracts below
        carefully — they define the priority corpus.

        ---
        {joined}
        ---

        Produce a research plan as a numbered list of 6-10 specific subquestions.

        Good subquestions:
          - Are concrete and answerable through targeted research
          - Cover both breadth (different facets of the topic) and depth (specific
            mechanisms / numbers / comparisons)
          - Build on the seed sources — fill gaps they leave, chase pointers they
            named, validate or challenge their claims
          - Each one should be researchable in 5-15 minutes by a subagent with
            web search and web fetch

        Bad subquestions:
          - "Tell me everything about X" (unbounded)
          - "Is X good?" (no decision criteria)
          - Duplicates of what the seed sources already cover

        Output format (markdown):

        ## Research plan

        1. <subquestion>
        2. <subquestion>
        ...

        No preamble. Just the list.
    """).strip()


def research_prompt(topic: str, subquestion: str, prior_findings: str) -> str:
    """Phase 3a: deep-research a single subquestion."""
    context_block = (
        f"### Existing findings so far\n{prior_findings}\n"
        if prior_findings.strip()
        else "### Existing findings so far\n(none yet)\n"
    )
    return textwrap.dedent(f"""
        You are researching ONE subquestion within a larger investigation.

        Overall topic: {topic}

        Your subquestion: {subquestion}

        {context_block}

        Your job:
        1. Use WebSearch to find authoritative sources on this subquestion. Prefer
           primary literature (papers, official docs, github repos) over secondary
           summaries (blog posts, marketing pages).
        2. Use WebFetch to read the most relevant ones in full.
        3. Synthesize what you found into a focused answer to the subquestion.
        4. Cite every non-obvious claim with a URL in parentheses immediately
           after it. Citations must be real URLs you fetched, not invented.

        Quality bar: a thoughtful senior engineer should be able to act on your
        output without re-doing your search. Specifics, numbers, tradeoffs,
        named approaches, code/paper references — not generalities.

        Output format (markdown):

        ## Subquestion: {subquestion}

        ### Answer
        <2-4 paragraphs of substance, citing sources inline>

        ### Sources consulted
        - <URL> — <one-line description of what it gave you>
        - <URL> — ...

        ### Follow-ups this surfaced
        - <new question worth chasing if there's budget>

        If you genuinely cannot find anything substantive after a real search,
        say so in the Answer section and explain what you tried. Do not
        fabricate findings.
    """).strip()


def synthesis_prompt(topic: str, findings: list[str]) -> str:
    """Phase 4: final synthesis into a structured architecture/roadmap doc."""
    joined = "\n\n---\n\n".join(findings)
    return textwrap.dedent(f"""
        You are producing the final synthesis report for a deep research run.

        Topic: {topic}

        Below are the findings from each subagent's research pass. They are raw
        — your job is to synthesize, not to concatenate. Reconcile contradictions.
        Identify the strongest threads. Surface the load-bearing decisions.
        Preserve the citations.

        ---
        {joined}
        ---

        Produce a final report with this structure (markdown):

        # <Concise title for the report>

        ## TL;DR
        3-5 bullets capturing the most important takeaways. A reader who only
        reads this section should leave with the core picture.

        ## Recommended approach
        The synthesized recommendation. If the research suggests one path more
        than others, name it and explain why. If there are real forks where the
        right answer depends on inputs we don't yet have, name the forks
        explicitly and what would tip the decision each way.

        ## Architecture / design details
        Specific technical content: components, data flows, algorithm choices,
        library selections, tradeoffs. Headings and subheadings as needed.

        ## Roadmap
        Suggested sequencing of work. Phase 1 / Phase 2 / etc. with concrete
        first steps under each. Make it actionable.

        ## Key risks and open questions
        What this research surfaced but did not resolve. Where to focus the
        next round if there is one.

        ## Sources
        All URLs cited across the report, de-duplicated, with one-line context.

        Quality bar: a senior engineer planning a real build should be able to
        use this document as their substrate. Cite inline as (url) where a
        specific claim depends on a source. Be opinionated where the research
        supports it; flag uncertainty where it does not.
    """).strip()


def gap_analysis_prompt(topic: str, plan: list[str], findings: list[str]) -> str:
    """Phase 3c: identify remaining open questions for another iteration."""
    plan_str = "\n".join(f"{i+1}. {q}" for i, q in enumerate(plan))
    findings_str = "\n\n---\n\n".join(findings)
    return textwrap.dedent(f"""
        You are mid-way through a deep research run. Look at what's been covered
        so far and identify what's still missing.

        Topic: {topic}

        Subquestions covered so far:
        {plan_str}

        Findings from the research so far:
        ---
        {findings_str}
        ---

        Your job:
        1. Read everything above as one corpus.
        2. Identify the most important UNRESOLVED questions — gaps that would
           change the architectural recommendation if answered.
        3. Up to 5 new subquestions, ordered by importance.

        Filter rules:
          - A new subquestion is worth adding only if its answer would change
            the recommendation. Curiosity-only questions are not in scope.
          - If the findings already cover the topic well and only edge-case
            questions remain, output "CONVERGED" and stop.

        Output format (markdown):

        ## Remaining gaps

        1. <subquestion> — why it matters
        2. ...

        OR, if convergence reached:

        ## Remaining gaps

        CONVERGED — the existing findings are sufficient to write the synthesis.

        No preamble.
    """).strip()


def progress_message(phase: str, detail: str) -> str:
    """Format a progress line for the run log."""
    return json.dumps({"phase": phase, "detail": detail})
