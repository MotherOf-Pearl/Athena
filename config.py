"""Defaults for the research engine."""
import os

CLAUDE_BIN = os.environ.get("CLAUDE_BIN", "/usr/bin/claude")

DEFAULT_TIME_CAP_HOURS = 3.0
DEFAULT_MAX_ITERATIONS = 5
DEFAULT_MAX_PARALLEL = 4

# Telegram completion notifier (optional).
# Both env vars must be set to enable the post-run ping.
TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN")
DEFAULT_NOTIFY_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID")

# Where run state and outputs are written.
RUNS_DIR = os.environ.get("RESEARCH_ENGINE_RUNS_DIR", "./runs")

# Notion output target.
NOTION_TOKEN = os.environ.get("NOTION_TOKEN")
NOTION_API_BASE = "https://api.notion.com/v1"
NOTION_VERSION = "2022-06-28"
# Parent page under which reports are created. Required for Notion output.
NOTION_RESEARCH_PARENT_ID = os.environ.get("NOTION_RESEARCH_PARENT_ID")
