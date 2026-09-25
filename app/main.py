"""HTTP entry point: Jira webhook -> action -> orchestrator (one background thread per ticket event)."""
from __future__ import annotations

import asyncio
import logging
import threading
from collections import defaultdict
from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException, Query, Request

from . import sf_cli
from .config import load_settings
from .git_github import GitHubClient, GitWorkspace
from .jira_client import JiraClient
from .orchestrator import Orchestrator
from .state import StateStore

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s [%(threadName)s] %(message)s")
log = logging.getLogger("sdlc")

settings = load_settings()
store = StateStore(settings.state_db)
jira = JiraClient(settings.jira_base_url, settings.jira_email, settings.jira_api_token, settings.jira_bot_account_id)
orchestrator = Orchestrator(
    settings, store, jira,
    GitWorkspace(settings.work_dir, settings.github_token, settings.git_author_name, settings.git_author_email),
    GitHubClient(settings.github_token),
)

S = settings.statuses
# Only transitions INTO human-owned statuses trigger work (bot transitions never match -> no loops)
STATUS_ACTIONS = {
    S["ready"].lower(): "START",
    S["answers_submitted"].lower(): "ANSWERS",
    S["design_changes"].lower(): "DESIGN_CHANGES",
    S["design_approved"].lower(): "DESIGN_APPROVED",
    S["build_changes"].lower(): "BUILD_CHANGES",
    S["tests_approved"].lower(): "TESTS_APPROVED",
}
VALID_ACTIONS = set(STATUS_ACTIONS.values())


class Dispatcher:
    """Runs each event in its own thread + event loop. Per-ticket lock drops duplicate deliveries."""

    def __init__(self):
        self._locks: dict[str, threading.Lock] = defaultdict(threading.Lock)
        self._guard = threading.Lock()

    def submit(self, key: str, action: str) -> bool:
        with self._guard:
            lock = self._locks[key]
        if not lock.acquire(blocking=False):
            log.warning("%s busy; dropping %s", key, action)
            return False

        def work():
            try:
                asyncio.run(orchestrator.handle(key, action))
            finally:
                lock.release()

        threading.Thread(target=work, name=f"{key}-{action}", daemon=True).start()
        return True


dispatcher = Dispatcher()


@asynccontextmanager
async def lifespan(_app: FastAPI):
    if settings.sf_jwt_client_id:
        res = sf_cli.login_devhub_jwt(settings.sf_jwt_client_id, settings.sf_jwt_key_file,
                                      settings.sf_jwt_username, settings.sf_jwt_instance_url,
                                      settings.sf_devhub_alias)
        log.info("Dev Hub JWT login status=%s", res.get("status"))
    yield


app = FastAPI(title="Autonomous SDLC orchestrator", lifespan=lifespan)


def _check(token: str) -> None:
    if token != settings.webhook_token:
        raise HTTPException(status_code=403, detail="bad token")


@app.post("/webhooks/jira")
async def jira_webhook(request: Request, token: str = Query("")):
    _check(token)
    p = await request.json()
    if p.get("webhookEvent") != "jira:issue_updated":
        return {"ignored": "event type"}
    actor = (p.get("user") or {}).get("accountId")
    if settings.jira_bot_account_id and actor == settings.jira_bot_account_id:
        return {"ignored": "bot actor"}
    items = (p.get("changelog") or {}).get("items") or []
    new_status = next((i.get("toString") for i in items if i.get("field") == "status"), None)
    action = STATUS_ACTIONS.get((new_status or "").lower())
    if not action:
        return {"ignored": f"status {new_status!r}"}
    key = p["issue"]["key"]
    return {"key": key, "action": action, "accepted": dispatcher.submit(key, action)}


@app.post("/run/{key}/{action}")
async def manual_trigger(key: str, action: str, token: str = Query("")):
    """Demo helper: trigger a stage without a webhook, e.g. POST /run/SFDEV-1/START?token=..."""
    _check(token)
    action = action.upper()
    if action not in VALID_ACTIONS:
        raise HTTPException(400, f"action must be one of {sorted(VALID_ACTIONS)}")
    return {"key": key, "action": action, "accepted": dispatcher.submit(key, action)}


@app.get("/runs/{key}")
async def run_status(key: str, token: str = Query("")):
    _check(token)
    st = store.load(key)
    if not st:
        raise HTTPException(404, "unknown ticket")
    return {k: v for k, v in st.__dict__.items() if k not in ("messages", "pending_results")}


@app.get("/health")
async def health():
    return {"ok": True}
