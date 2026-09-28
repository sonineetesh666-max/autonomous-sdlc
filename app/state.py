"""Durable per-ticket state. The agent never waits for humans: it saves state here and exits."""
from __future__ import annotations

import json
import sqlite3
import threading
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path


@dataclass
class RunState:
    ticket_key: str
    stage: str = "NEW"          # NEW, SETUP, DESIGNING, AWAITING_INPUT, DESIGN_REVIEW,
                                # BUILDING, TEST_REVIEW, DONE, NEEDS_INFO, BLOCKED
    summary: str = ""
    reporter_id: str | None = None
    project_key: str = ""
    repo: str = ""
    base_branch: str = "main"
    branch: str = ""
    org_alias: str = ""
    workspace: str = ""
    # paused agent conversation (only while AWAITING_INPUT)
    messages: list = field(default_factory=list)
    pending_tool_id: str | None = None
    pending_results: list = field(default_factory=list)
    # counters
    question_rounds: int = 0
    design_version: int = 0
    build_rounds: int = 0
    # bookkeeping
    last_bot_post_at: str | None = None     # Jira 'created' timestamp of our latest comment
    test_report: dict = field(default_factory=dict)
    pr_url: str | None = None


class StateStore:
    def __init__(self, path: Path):
        self._lock = threading.Lock()
        self._conn = sqlite3.connect(str(path), check_same_thread=False)
        self._conn.execute(
            "CREATE TABLE IF NOT EXISTS runs ("
            " ticket_key TEXT PRIMARY KEY, data TEXT NOT NULL, updated_at TEXT NOT NULL)"
        )
        self._conn.commit()

    def load(self, key: str) -> RunState | None:
        with self._lock:
            row = self._conn.execute("SELECT data FROM runs WHERE ticket_key = ?", (key,)).fetchone()
        return RunState(**json.loads(row[0])) if row else None

    def save(self, st: RunState) -> None:
        now = datetime.now(timezone.utc).isoformat()
        with self._lock:
            self._conn.execute(
                "INSERT INTO runs (ticket_key, data, updated_at) VALUES (?, ?, ?) "
                "ON CONFLICT(ticket_key) DO UPDATE SET data = excluded.data, updated_at = excluded.updated_at",
                (st.ticket_key, json.dumps(asdict(st)), now),
            )
            self._conn.commit()
