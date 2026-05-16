"""Persistent run state — JSON file on disk."""

from __future__ import annotations

import dataclasses
import datetime as dt
import json
import logging
import os
import pathlib
import time
import uuid
from typing import Any, Optional

import config

log = logging.getLogger(__name__)


@dataclasses.dataclass
class RunState:
    run_id: str
    topic: str
    seeds: list[str]
    time_cap_s: float
    max_iterations: int
    max_parallel: int
    notify_chat_id: Optional[str]
    parent_page_id: Optional[str] = None
    started_at: float = dataclasses.field(default_factory=time.time)

    # Mutable state ---
    phase: str = "init"
    seed_summaries: list[str] = dataclasses.field(default_factory=list)
    plan: list[str] = dataclasses.field(default_factory=list)
    iterations: list[dict] = dataclasses.field(default_factory=list)
    final_markdown: Optional[str] = None
    final_notion_url: Optional[str] = None
    total_cost_usd: float = 0.0
    error: Optional[str] = None
    done: bool = False

    def directory(self) -> pathlib.Path:
        return pathlib.Path(config.RUNS_DIR) / self.run_id

    def state_file(self) -> pathlib.Path:
        return self.directory() / "state.json"

    def log_file(self) -> pathlib.Path:
        return self.directory() / "log.txt"

    def final_md_file(self) -> pathlib.Path:
        return self.directory() / "final.md"

    def time_remaining_s(self) -> float:
        return self.started_at + self.time_cap_s - time.time()

    def save(self) -> None:
        self.directory().mkdir(parents=True, exist_ok=True)
        with open(self.state_file(), "w") as f:
            json.dump(dataclasses.asdict(self), f, indent=2)

    def log(self, line: str) -> None:
        ts = dt.datetime.now().isoformat(timespec="seconds")
        self.directory().mkdir(parents=True, exist_ok=True)
        with open(self.log_file(), "a") as f:
            f.write(f"[{ts}] {line}\n")
        log.info("[%s] %s", self.run_id, line)


def new_run(
    topic: str,
    seeds: list[str],
    time_cap_hours: float = config.DEFAULT_TIME_CAP_HOURS,
    max_iterations: int = config.DEFAULT_MAX_ITERATIONS,
    max_parallel: int = config.DEFAULT_MAX_PARALLEL,
    notify_chat_id: Optional[str] = config.DEFAULT_NOTIFY_CHAT_ID,
    parent_page_id: Optional[str] = None,
) -> RunState:
    rid = f"{dt.datetime.now().strftime('%Y%m%d-%H%M%S')}-{uuid.uuid4().hex[:6]}"
    state = RunState(
        run_id=rid,
        topic=topic,
        seeds=seeds,
        time_cap_s=time_cap_hours * 3600,
        max_iterations=max_iterations,
        max_parallel=max_parallel,
        notify_chat_id=notify_chat_id,
        parent_page_id=parent_page_id,
    )
    state.directory().mkdir(parents=True, exist_ok=True)
    state.save()
    return state


def load_run(run_id: str) -> RunState:
    path = pathlib.Path(config.RUNS_DIR) / run_id / "state.json"
    with open(path) as f:
        data = json.load(f)
    return RunState(**data)
