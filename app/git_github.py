"""Git workspace per ticket (fresh clone = repo is the source of truth) + GitHub PR API."""
from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import requests


class GitError(RuntimeError):
    pass


class GitWorkspace:
    def __init__(self, root: Path, token: str, author_name: str, author_email: str):
        self.root = root
        self.token = token
        self.author_name = author_name
        self.author_email = author_email

    def _mask(self, text: str) -> str:
        return text.replace(self.token, "***") if self.token else text

    def _git(self, args: list[str], cwd: Path | None = None, timeout: int = 600) -> str:
        p = subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True, timeout=timeout)
        if p.returncode != 0:
            raise GitError(self._mask(f"git {' '.join(args)} failed: {p.stderr or p.stdout}"))
        return p.stdout

    def prepare(self, repo: str, base: str, branch: str, key: str, fresh: bool = True) -> Path:
        path = self.root / key
        if fresh and path.exists():
            shutil.rmtree(path)
        if not path.exists():
            url = f"https://x-access-token:{self.token}@github.com/{repo}.git"
            self._git(["clone", "--branch", base, url, str(path)])
            self._git(["checkout", "-b", branch], cwd=path)
            self._git(["config", "user.name", self.author_name], cwd=path)
            self._git(["config", "user.email", self.author_email], cwd=path)
        return path

    def commit_and_push(self, path: Path, branch: str, message: str) -> bool:
        self._git(["add", "-A"], cwd=path)
        if not self._git(["status", "--porcelain"], cwd=path).strip():
            return False
        self._git(["commit", "-m", message], cwd=path)
        # ai/* branches are owned by the bot; lease protects against clobbering human pushes
        self._git(["push", "--force-with-lease", "-u", "origin", f"HEAD:{branch}"], cwd=path)
        return True

    def changed_files(self, path: Path, base: str) -> list[str]:
        diff = self._git(["diff", "--name-only", f"origin/{base}"], cwd=path).splitlines()
        untracked = self._git(["ls-files", "--others", "--exclude-standard"], cwd=path).splitlines()
        return sorted({f.strip() for f in diff + untracked if f.strip()})


class GitHubClient:
    API = "https://api.github.com"

    def __init__(self, token: str):
        self.headers = {"Authorization": f"Bearer {token}", "Accept": "application/vnd.github+json"}

    @staticmethod
    def compare_url(repo: str, base: str, branch: str) -> str:
        return f"https://github.com/{repo}/compare/{base}...{branch}"

    @staticmethod
    def blob_url(repo: str, branch: str, path: str) -> str:
        return f"https://github.com/{repo}/blob/{branch}/{path}"

    def create_pr(self, repo: str, head: str, base: str, title: str, body: str) -> str:
        r = requests.post(f"{self.API}/repos/{repo}/pulls", headers=self.headers, timeout=60,
                          json={"title": title, "head": head, "base": base, "body": body})
        if r.status_code == 422 and "already exists" in r.text:
            owner = repo.split("/")[0]
            existing = requests.get(f"{self.API}/repos/{repo}/pulls", headers=self.headers, timeout=60,
                                    params={"head": f"{owner}:{head}", "state": "open"}).json()
            if existing:
                return existing[0]["html_url"]
        if not r.ok:
            raise RuntimeError(f"GitHub PR creation failed {r.status_code}: {r.text[:500]}")
        return r.json()["html_url"]
