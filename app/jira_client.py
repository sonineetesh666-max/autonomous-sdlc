"""Minimal Jira Cloud client (REST v2 => description/comments come back as plain wiki text)."""
from __future__ import annotations

from datetime import datetime

import requests

BOT_MARKER = "[AI]"   # every bot comment starts with this, so it works even with a single shared account


def _parse_ts(ts: str) -> datetime:
    return datetime.strptime(ts, "%Y-%m-%dT%H:%M:%S.%f%z")


class JiraClient:
    def __init__(self, base_url: str, email: str, api_token: str, bot_account_id: str = ""):
        self.base = base_url.rstrip("/")
        self.auth = (email, api_token)
        self.bot_account_id = bot_account_id

    def _req(self, method: str, path: str, **kw):
        r = requests.request(method, f"{self.base}{path}", auth=self.auth, timeout=60, **kw)
        if not r.ok:
            raise RuntimeError(f"Jira {method} {path} -> {r.status_code}: {r.text[:500]}")
        return r.json() if r.content else {}

    def browse_url(self, key: str) -> str:
        return f"{self.base}/browse/{key}"

    def get_issue(self, key: str, extra_fields: list[str] | None = None) -> dict:
        fields = ["summary", "description", "status", "reporter", "project", *(extra_fields or [])]
        return self._req("GET", f"/rest/api/2/issue/{key}", params={"fields": ",".join(fields)})

    def add_comment(self, key: str, body: str) -> str:
        """Posts a comment and returns its 'created' timestamp."""
        if not body.startswith(BOT_MARKER):
            body = f"{BOT_MARKER} {body}"
        return self._req("POST", f"/rest/api/2/issue/{key}/comment", json={"body": body})["created"]

    def get_comments(self, key: str) -> list[dict]:
        data = self._req("GET", f"/rest/api/2/issue/{key}/comment",
                         params={"maxResults": 100, "orderBy": "created"})
        return data.get("comments", [])

    def human_comments_since(self, key: str, since: str | None) -> list[str]:
        since_dt = _parse_ts(since) if since else None
        out = []
        for c in self.get_comments(key):
            body = (c.get("body") or "").strip()
            author = (c.get("author") or {}).get("accountId")
            if body.startswith(BOT_MARKER):
                continue
            if self.bot_account_id and author == self.bot_account_id:
                continue
            if since_dt and _parse_ts(c["created"]) <= since_dt:
                continue
            out.append(body)
        return out

    def transition_to(self, key: str, status_name: str) -> None:
        """Finds a transition whose target status matches the name (more robust than transition names)."""
        ts = self._req("GET", f"/rest/api/2/issue/{key}/transitions")["transitions"]
        match = next((t for t in ts if t["to"]["name"].lower() == status_name.lower()), None)
        if not match:
            available = [t["to"]["name"] for t in ts]
            raise RuntimeError(f"No transition from current status of {key} to '{status_name}'. "
                               f"Available targets: {available}")
        self._req("POST", f"/rest/api/2/issue/{key}/transitions", json={"transition": {"id": match["id"]}})

    def attach(self, key: str, filename: str, content: bytes) -> None:
        r = requests.post(
            f"{self.base}/rest/api/2/issue/{key}/attachments",
            auth=self.auth, timeout=60,
            headers={"X-Atlassian-Token": "no-check"},
            files={"file": (filename, content)},
        )
        if not r.ok:
            raise RuntimeError(f"Jira attach failed {r.status_code}: {r.text[:300]}")

    @staticmethod
    def mention(account_id: str | None) -> str:
        return f"[~accountid:{account_id}] " if account_id else ""
