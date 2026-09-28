"""Validates every credential, tool, and integration the pipeline depends on, without
touching Jira, GitHub, or the org (read-only checks throughout). Run after any change to
.env or config/registry.yaml, and before every live/demo run:

    python scripts/preflight.py
"""
from __future__ import annotations

import asyncio
import shutil
import subprocess
import sys
from contextlib import AsyncExitStack
from dataclasses import dataclass
from pathlib import Path

import requests

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app.config import Settings, load_settings  # noqa: E402
from app.sf_cli import check_org  # noqa: E402

MIN_PYTHON = (3, 11)
MIN_NODE = 18
HTTP_TIMEOUT = 20


@dataclass
class Check:
    name: str
    ok: bool
    detail: str = ""


def _run(cmd: list[str], timeout: int = 30) -> tuple[bool, str]:
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        out = (p.stdout or p.stderr or "").strip().splitlines()
        return p.returncode == 0, (out[0] if out else "")
    except FileNotFoundError:
        return False, "not found on PATH"
    except subprocess.TimeoutExpired:
        return False, f"timed out after {timeout}s"
    except Exception as e:
        return False, str(e)


# ---------- tools ----------

def check_python() -> Check:
    ok = sys.version_info >= MIN_PYTHON
    return Check(f"Python >= {'.'.join(map(str, MIN_PYTHON))}", ok, f"found {sys.version.split()[0]}")


def check_node() -> Check:
    ok, detail = _run(["node", "--version"])
    if ok:
        try:
            ok = int(detail.lstrip("v").split(".")[0]) >= MIN_NODE
        except ValueError:
            ok = False
    return Check(f"Node.js >= {MIN_NODE}", ok, detail or "node not found")


def check_git() -> Check:
    ok, detail = _run(["git", "--version"])
    return Check("git on PATH", ok, detail)


def check_sf() -> Check:
    ok, detail = _run([shutil.which("sf") or "sf", "--version"])
    return Check("Salesforce CLI (sf) on PATH", ok, detail)


def check_packages() -> Check:
    missing = []
    for mod in ("anthropic", "mcp", "fastapi", "uvicorn", "requests", "dotenv", "yaml"):
        try:
            __import__(mod)
        except ImportError:
            missing.append(mod)
    detail = "all present" if not missing else f"missing: {', '.join(missing)} (pip install -r requirements.txt)"
    return Check("Python packages installed", not missing, detail)


# ---------- Claude ----------

async def check_anthropic(s: Settings) -> Check:
    if not s or "ANTHROPIC_API_KEY" not in __import__("os").environ or not __import__("os").environ.get("ANTHROPIC_API_KEY"):
        return Check("Anthropic API reachable", False, "ANTHROPIC_API_KEY not set in .env")
    try:
        from anthropic import AsyncAnthropic
        client = AsyncAnthropic()
        resp = await asyncio.wait_for(
            client.messages.create(model=s.anthropic_model, max_tokens=8,
                                   messages=[{"role": "user", "content": "ping"}]),
            timeout=HTTP_TIMEOUT)
        return Check("Anthropic API reachable", True, f"model={s.anthropic_model} replied ({resp.stop_reason})")
    except Exception as e:
        return Check("Anthropic API reachable", False, str(e)[:200])


# ---------- Jira ----------

def _jira_auth(s: Settings) -> tuple[str, tuple[str, str]]:
    return s.jira_base_url, (s.jira_email, s.jira_api_token)


def check_jira_auth(s: Settings) -> Check:
    base, auth = _jira_auth(s)
    if not base or not s.jira_email or not s.jira_api_token:
        return Check("Jira auth", False, "JIRA_BASE_URL / JIRA_EMAIL / JIRA_API_TOKEN not fully set")
    try:
        r = requests.get(f"{base}/rest/api/2/myself", auth=auth, timeout=HTTP_TIMEOUT)
        if not r.ok:
            return Check("Jira auth", False, f"{r.status_code}: {r.text[:200]}")
        me = r.json()
        ok = (me.get("emailAddress") or "").lower() == s.jira_email.lower()
        detail = f"authenticated as {me.get('emailAddress')}" + ("" if ok else
                 f" (JIRA_EMAIL is '{s.jira_email}' - mismatch)")
        return Check("Jira auth", ok, detail)
    except requests.RequestException as e:
        return Check("Jira auth", False, str(e)[:200])


def check_jira_project(s: Settings, key: str) -> Check:
    base, auth = _jira_auth(s)
    try:
        r = requests.get(f"{base}/rest/api/2/project/{key}", auth=auth, timeout=HTTP_TIMEOUT)
        if not r.ok:
            return Check(f"[{key}] Jira project exists", False, f"{r.status_code}: {r.text[:200]}")
        p = r.json()
        return Check(f"[{key}] Jira project exists", True, f"{p.get('name')} (id {p.get('id')})")
    except requests.RequestException as e:
        return Check(f"[{key}] Jira project exists", False, str(e)[:200])


def check_jira_statuses(s: Settings, key: str) -> Check:
    base, auth = _jira_auth(s)
    try:
        r = requests.get(f"{base}/rest/api/2/project/{key}/statuses", auth=auth, timeout=HTTP_TIMEOUT)
        if not r.ok:
            return Check(f"[{key}] Jira workflow statuses", False, f"{r.status_code}: {r.text[:200]}")
        present = {st["name"].lower() for issue_type in r.json() for st in issue_type.get("statuses", [])}
        required = set(s.statuses.values())
        missing = sorted(n for n in required if n.lower() not in present)
        if missing:
            return Check(f"[{key}] Jira workflow statuses", False,
                        f"missing {len(missing)}/{len(required)}: {', '.join(missing)}")
        return Check(f"[{key}] Jira workflow statuses", True, f"all {len(required)} required statuses present")
    except requests.RequestException as e:
        return Check(f"[{key}] Jira workflow statuses", False, str(e)[:200])


# ---------- GitHub ----------

def check_github_auth(s: Settings) -> Check:
    if not s.github_token:
        return Check("GitHub auth", False, "GITHUB_TOKEN not set in .env")
    try:
        r = requests.get("https://api.github.com/user",
                         headers={"Authorization": f"Bearer {s.github_token}", "User-Agent": "preflight"},
                         timeout=HTTP_TIMEOUT)
        if not r.ok:
            return Check("GitHub auth", False, f"{r.status_code}: {r.text[:200]}")
        return Check("GitHub auth", True, f"authenticated as {r.json().get('login')}")
    except requests.RequestException as e:
        return Check("GitHub auth", False, str(e)[:200])


def check_github_repo(s: Settings, repo: str) -> Check:
    try:
        r = requests.get(f"https://api.github.com/repos/{repo}",
                         headers={"Authorization": f"Bearer {s.github_token}", "User-Agent": "preflight"},
                         timeout=HTTP_TIMEOUT)
        if not r.ok:
            return Check(f"[{repo}] GitHub repo write access", False, f"{r.status_code}: {r.text[:200]}")
        push = ((r.json().get("permissions") or {}).get("push"))
        return Check(f"[{repo}] GitHub repo write access", bool(push),
                    "push access confirmed" if push else "token cannot push to this repo")
    except requests.RequestException as e:
        return Check(f"[{repo}] GitHub repo write access", False, str(e)[:200])


# ---------- Salesforce org + DX MCP server ----------

def check_sf_org(alias: str) -> Check:
    ok, detail = check_org(alias)
    return Check(f"[{alias}] Salesforce org connected", ok, detail)


async def check_mcp_probe(s: Settings, alias: str) -> Check:
    try:
        from mcp import ClientSession, StdioServerParameters
        from mcp.client.stdio import stdio_client
    except ImportError:
        return Check(f"[{alias}] DX MCP server tool discovery", False, "mcp package not installed")

    params = StdioServerParameters(
        command=shutil.which("npx") or "npx",
        args=["-y", "@salesforce/mcp@latest", "--orgs", alias,
             "--toolsets", s.mcp_toolsets, *s.mcp_extra_args],
        env=dict(__import__("os").environ), cwd=str(ROOT))
    try:
        async def _probe():
            async with AsyncExitStack() as stack:
                read, write = await stack.enter_async_context(stdio_client(params))
                session = await stack.enter_async_context(ClientSession(read, write))
                await session.initialize()
                listed = await session.list_tools()
                return [t.name for t in listed.tools]

        names = await asyncio.wait_for(_probe(), timeout=180)
        return Check(f"[{alias}] DX MCP server tool discovery", bool(names),
                    f"{len(names)} tools available" if names else "server started but listed 0 tools")
    except asyncio.TimeoutError:
        return Check(f"[{alias}] DX MCP server tool discovery", False,
                    "timed out after 180s (first run downloads the package via npx - try again)")
    except Exception as e:
        return Check(f"[{alias}] DX MCP server tool discovery", False, str(e)[:200])


# ---------- report ----------

def _report(checks: list[Check]) -> int:
    width = max(len(c.name) for c in checks) + 2
    failed = 0
    for c in checks:
        mark = "PASS" if c.ok else "FAIL"
        if not c.ok:
            failed += 1
        print(f"[{mark}] {c.name.ljust(width)} {c.detail}")
    print()
    print(f"{len(checks) - failed}/{len(checks)} checks passed.")
    if failed:
        print(f"{failed} check(s) failed - fix these before running a live ticket.")
    return 1 if failed else 0


async def main() -> int:
    checks: list[Check] = [check_python(), check_node(), check_git(), check_sf(), check_packages()]

    try:
        s = load_settings()
    except Exception as e:
        checks.append(Check("Load .env / config/registry.yaml", False, str(e)[:300]))
        return _report(checks)
    checks.append(Check("Load .env / config/registry.yaml", True,
                        f"{len(s.projects)} project(s) routed: {', '.join(s.projects)}"))

    checks.append(await check_anthropic(s))
    checks.append(check_jira_auth(s))
    checks.append(check_github_auth(s))

    for key, proj in s.projects.items():
        checks.append(check_jira_project(s, key))
        checks.append(check_jira_statuses(s, key))
        checks.append(check_github_repo(s, proj.repo))
        checks.append(check_sf_org(proj.org_alias))
        checks.append(await check_mcp_probe(s, proj.org_alias))

    return _report(checks)


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
