"""Notion API client + lightweight markdown-to-blocks converter.

Uses the Notion REST API directly. The integration token is read from the
NOTION_TOKEN env var. Only the page surfaces we need are covered.

Markdown subset supported in the converter (sufficient for synthesis output):
  - # / ## / ### headings
  - paragraphs (one blank line between)
  - bulleted lists (- or *)
  - numbered lists (1. 2. ...)
  - code fences (```lang)
  - blockquotes (>)
  - horizontal rules (---)
  - inline links [text](url) and **bold** / *italic* / `code`
"""

from __future__ import annotations

import json
import logging
import re
import time
import urllib.error
import urllib.request
from typing import Any, Optional

import config

log = logging.getLogger(__name__)

NOTION_BLOCK_LIMIT = 100  # API caps children per call


def _token() -> str:
    if not config.NOTION_TOKEN:
        raise RuntimeError("NOTION_TOKEN env var is not set")
    return config.NOTION_TOKEN


def _request(method: str, path: str, body: Optional[dict] = None) -> dict:
    url = f"{config.NOTION_API_BASE}{path}"
    data = json.dumps(body).encode("utf-8") if body is not None else None
    req = urllib.request.Request(url, data=data, method=method)
    req.add_header("Authorization", f"Bearer {_token()}")
    req.add_header("Notion-Version", config.NOTION_VERSION)
    req.add_header("Content-Type", "application/json")
    for attempt in range(3):
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            err_body = e.read().decode("utf-8", "ignore")
            if 500 <= e.code < 600 and attempt < 2:
                time.sleep(2 ** attempt)
                continue
            raise RuntimeError(f"Notion {method} {path} -> {e.code}: {err_body}") from e
        except urllib.error.URLError as e:
            if attempt < 2:
                time.sleep(2 ** attempt)
                continue
            raise
    raise RuntimeError("unreachable")


# ---------- page helpers ----------


def create_page(parent_page_id: str, title: str, emoji: Optional[str] = None) -> str:
    """Create a page under parent_page_id, return its ID."""
    body: dict[str, Any] = {
        "parent": {"type": "page_id", "page_id": parent_page_id},
        "properties": {"title": [{"type": "text", "text": {"content": title}}]},
    }
    if emoji:
        body["icon"] = {"type": "emoji", "emoji": emoji}
    result = _request("POST", "/pages", body)
    return result["id"]


def find_child_page(parent_page_id: str, title: str) -> Optional[str]:
    """Find a direct child page by title; None if not found."""
    cursor = None
    while True:
        path = f"/blocks/{parent_page_id}/children?page_size=100"
        if cursor:
            path += f"&start_cursor={cursor}"
        result = _request("GET", path)
        for block in result.get("results", []):
            if block.get("type") == "child_page":
                if block["child_page"]["title"] == title:
                    return block["id"]
        if not result.get("has_more"):
            return None
        cursor = result.get("next_cursor")


def ensure_research_parent() -> str:
    """Return the configured parent page ID for research reports.

    Reads NOTION_RESEARCH_PARENT_ID from the environment. If you want a
    dedicated 'Research' subpage under some other parent, use find_child_page
    / create_page directly from your caller.
    """
    if not config.NOTION_RESEARCH_PARENT_ID:
        raise RuntimeError(
            "NOTION_RESEARCH_PARENT_ID env var not set; "
            "pass --parent-page-id or set the env var to enable Notion output"
        )
    return config.NOTION_RESEARCH_PARENT_ID


def append_blocks(page_id: str, blocks: list[dict]) -> None:
    """Append blocks to a page, batching at Notion's 100-block limit."""
    for i in range(0, len(blocks), NOTION_BLOCK_LIMIT):
        chunk = blocks[i:i + NOTION_BLOCK_LIMIT]
        _request("PATCH", f"/blocks/{page_id}/children", {"children": chunk})


# ---------- markdown → Notion blocks ----------


_LINK_RE = re.compile(r"\[([^\]]+)\]\(([^)]+)\)")
_BOLD_RE = re.compile(r"\*\*([^*]+)\*\*")
_ITALIC_RE = re.compile(r"(?<!\*)\*([^*]+)\*(?!\*)")
_CODE_RE = re.compile(r"`([^`]+)`")


def _rich_text(text: str) -> list[dict]:
    """Convert a single line of markdown to a list of Notion rich_text spans.

    Handles links, bold, italic, inline code. Combinations of annotations on the
    same span are not supported (rare in research output); the rightmost match
    wins per region. Splits the line into pieces by scanning sequentially.
    """
    spans: list[dict] = []
    remaining = text

    # Sequential tokenization: find earliest match among link/bold/italic/code.
    # Anything before it is plain; the match becomes its own span.
    while remaining:
        matches: list[tuple[int, int, str, dict]] = []  # (start, end, kind, payload)
        m = _LINK_RE.search(remaining)
        if m:
            matches.append((m.start(), m.end(), "link", {"label": m.group(1), "url": m.group(2)}))
        m = _BOLD_RE.search(remaining)
        if m:
            matches.append((m.start(), m.end(), "bold", {"label": m.group(1)}))
        m = _ITALIC_RE.search(remaining)
        if m:
            matches.append((m.start(), m.end(), "italic", {"label": m.group(1)}))
        m = _CODE_RE.search(remaining)
        if m:
            matches.append((m.start(), m.end(), "code", {"label": m.group(1)}))
        if not matches:
            spans.append({"type": "text", "text": {"content": remaining}})
            break
        matches.sort()
        start, end, kind, payload = matches[0]
        if start > 0:
            spans.append({"type": "text", "text": {"content": remaining[:start]}})
        if kind == "link":
            spans.append({
                "type": "text",
                "text": {"content": payload["label"], "link": {"url": payload["url"]}},
            })
        elif kind == "bold":
            spans.append({
                "type": "text",
                "text": {"content": payload["label"]},
                "annotations": {"bold": True},
            })
        elif kind == "italic":
            spans.append({
                "type": "text",
                "text": {"content": payload["label"]},
                "annotations": {"italic": True},
            })
        elif kind == "code":
            spans.append({
                "type": "text",
                "text": {"content": payload["label"]},
                "annotations": {"code": True},
            })
        remaining = remaining[end:]

    return spans or [{"type": "text", "text": {"content": ""}}]


def markdown_to_blocks(md: str) -> list[dict]:
    """Convert markdown text into a list of Notion block dicts.

    Notion paragraphs cap at 2000 chars per rich_text span; we keep lines
    well under that. Long paragraphs get split on whitespace if needed.
    """
    blocks: list[dict] = []
    lines = md.splitlines()
    i = 0
    while i < len(lines):
        line = lines[i].rstrip()

        # blank
        if not line.strip():
            i += 1
            continue

        # code fence
        if line.startswith("```"):
            lang = line[3:].strip() or "plain text"
            content: list[str] = []
            i += 1
            while i < len(lines) and not lines[i].startswith("```"):
                content.append(lines[i])
                i += 1
            i += 1  # consume closing fence
            blocks.append({
                "type": "code",
                "code": {
                    "rich_text": [{"type": "text", "text": {"content": "\n".join(content)[:2000]}}],
                    "language": _normalize_lang(lang),
                },
            })
            continue

        # horizontal rule
        if re.fullmatch(r"-{3,}|\*{3,}|_{3,}", line.strip()):
            blocks.append({"type": "divider", "divider": {}})
            i += 1
            continue

        # headings
        if line.startswith("### "):
            blocks.append({"type": "heading_3", "heading_3": {"rich_text": _rich_text(line[4:])}})
            i += 1
            continue
        if line.startswith("## "):
            blocks.append({"type": "heading_2", "heading_2": {"rich_text": _rich_text(line[3:])}})
            i += 1
            continue
        if line.startswith("# "):
            blocks.append({"type": "heading_1", "heading_1": {"rich_text": _rich_text(line[2:])}})
            i += 1
            continue

        # blockquote (single line; consecutive lines collapsed)
        if line.startswith("> "):
            content_lines = []
            while i < len(lines) and lines[i].startswith("> "):
                content_lines.append(lines[i][2:])
                i += 1
            blocks.append({
                "type": "quote",
                "quote": {"rich_text": _rich_text(" ".join(content_lines))},
            })
            continue

        # bulleted list
        if re.match(r"^[-*]\s+", line):
            blocks.append({
                "type": "bulleted_list_item",
                "bulleted_list_item": {"rich_text": _rich_text(re.sub(r"^[-*]\s+", "", line))},
            })
            i += 1
            continue

        # numbered list
        if re.match(r"^\d+\.\s+", line):
            blocks.append({
                "type": "numbered_list_item",
                "numbered_list_item": {"rich_text": _rich_text(re.sub(r"^\d+\.\s+", "", line))},
            })
            i += 1
            continue

        # paragraph: collapse adjacent non-blank lines
        para_lines = [line]
        i += 1
        while i < len(lines) and lines[i].strip() and not _is_special_line(lines[i]):
            para_lines.append(lines[i].rstrip())
            i += 1
        para = " ".join(para_lines)
        # split very long paragraphs to stay under Notion's 2000-char rich-text cap
        for chunk in _chunk_text(para, 1800):
            blocks.append({
                "type": "paragraph",
                "paragraph": {"rich_text": _rich_text(chunk)},
            })

    return blocks


def _is_special_line(line: str) -> bool:
    line = line.rstrip()
    if not line:
        return True
    if line.startswith(("# ", "## ", "### ", "> ", "```")):
        return True
    if re.match(r"^[-*]\s+", line) or re.match(r"^\d+\.\s+", line):
        return True
    if re.fullmatch(r"-{3,}|\*{3,}|_{3,}", line.strip()):
        return True
    return False


def _chunk_text(text: str, size: int) -> list[str]:
    if len(text) <= size:
        return [text]
    out: list[str] = []
    while text:
        if len(text) <= size:
            out.append(text)
            break
        cut = text.rfind(" ", 0, size)
        if cut <= 0:
            cut = size
        out.append(text[:cut])
        text = text[cut:].lstrip()
    return out


_LANG_MAP = {
    "py": "python", "js": "javascript", "ts": "typescript", "sh": "shell",
    "bash": "shell", "md": "markdown", "yml": "yaml",
}


def _normalize_lang(lang: str) -> str:
    lang = lang.lower().strip()
    return _LANG_MAP.get(lang, lang) if lang else "plain text"


def write_report(parent_page_id: str, title: str, markdown: str, emoji: str = "🔬") -> tuple[str, str]:
    """Create a page under parent, append the markdown as blocks. Return (page_id, url)."""
    page_id = create_page(parent_page_id, title, emoji=emoji)
    blocks = markdown_to_blocks(markdown)
    log.info("writing %d blocks to page %s", len(blocks), page_id)
    append_blocks(page_id, blocks)
    url = f"https://www.notion.so/{page_id.replace('-', '')}"
    return page_id, url
