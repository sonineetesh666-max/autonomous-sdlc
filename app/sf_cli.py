"""Deterministic Salesforce operations done by the ORCHESTRATOR (not by Claude):
org auth, checking the target org is reachable, deploying repo source, and independent
verification. No scratch orgs: every project targets one existing, already-authorized org
(config/registry.yaml org_alias) and the repo is synced to it before each run.
Claude uses the Salesforce DX MCP server; this module is the pipeline's own check."""
from __future__ import annotations

import json
import os
import shutil
import subprocess
from dataclasses import asdict, dataclass, field
from pathlib import Path

_ENV = {**os.environ, "SF_AUTOUPDATE_DISABLE": "true", "SF_DISABLE_TELEMETRY": "true", "FORCE_COLOR": "0"}
# On Windows `sf` is a .cmd shim; subprocess needs the resolved path (with extension) to run it
# directly, since unlike an interactive shell it won't search PATHEXT for a bare "sf".
_SF = shutil.which("sf") or "sf"


def sf(args: list[str], cwd: Path | str | None = None, timeout: int = 1800) -> dict:
    p = subprocess.run([_SF, *args, "--json"], cwd=cwd, capture_output=True, text=True,
                       timeout=timeout, env=_ENV)
    try:
        data = json.loads(p.stdout)
    except json.JSONDecodeError:
        data = {"status": p.returncode or 1, "message": (p.stderr or p.stdout)[-4000:]}
    data.setdefault("status", p.returncode)
    return data


def _find(obj, key):
    """Depth-first search for the first value stored under `key` (sf JSON shapes vary by version)."""
    if isinstance(obj, dict):
        if key in obj:
            return obj[key]
        for v in obj.values():
            found = _find(v, key)
            if found is not None:
                return found
    elif isinstance(obj, list):
        for v in obj:
            found = _find(v, key)
            if found is not None:
                return found
    return None


# ---------- auth + org lifecycle ----------

def login_devhub_jwt(client_id: str, key_file: str, username: str, instance_url: str, alias: str) -> dict:
    return sf(["org", "login", "jwt", "--client-id", client_id, "--jwt-key-file", key_file,
               "--username", username, "--instance-url", instance_url, "--alias", alias,
               "--set-default-dev-hub"])


def check_org(alias: str) -> tuple[bool, str]:
    """Confirms the target org is authorized and reachable before we deploy anything to it."""
    data = sf(["org", "display", "--target-org", alias])
    if data.get("status") != 0:
        return False, data.get("message", f"Org '{alias}' is not authorized (run `sf org login web --alias {alias}`).")
    result = data.get("result") or {}
    if result.get("connectedStatus") not in ("Connected", None):
        return False, f"Org '{alias}' is not connected (status: {result.get('connectedStatus')})."
    return True, result.get("username", "")


def deploy_project(workspace: Path, alias: str) -> tuple[bool, list[str]]:
    data = sf(["project", "deploy", "start", "--target-org", alias, "--wait", "30", "--ignore-conflicts"],
              cwd=workspace)
    if data.get("status") == 0:
        return True, []
    failures = _find(data, "componentFailures") or []
    if isinstance(failures, dict):
        failures = [failures]
    errors = [f"{f.get('fileName') or f.get('fullName')}:{f.get('lineNumber', '?')} {f.get('problem')}"
              for f in failures]
    return False, errors or [data.get("message") or "Deploy failed (no details returned)"]


# ---------- independent verification ----------

@dataclass
class VerifyReport:
    ok: bool = False
    stage: str = "deploy"                       # deploy | tests | coverage | done
    deploy_errors: list[str] = field(default_factory=list)
    test_failures: list[str] = field(default_factory=list)
    passing: int = 0
    failing: int = 0
    coverage: dict[str, float] = field(default_factory=dict)
    below_target: dict[str, float] = field(default_factory=dict)
    test_classes: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return asdict(self)

    def as_text(self) -> str:
        lines = [f"Stage reached: {self.stage}"]
        if self.deploy_errors:
            lines += ["DEPLOY ERRORS:", *[f"- {e}" for e in self.deploy_errors[:40]]]
        if self.test_failures:
            lines += ["TEST FAILURES:", *[f"- {e}" for e in self.test_failures[:40]]]
        if self.below_target:
            lines += ["COVERAGE BELOW TARGET:", *[f"- {k}: {v:.0f}%" for k, v in self.below_target.items()]]
        lines += [f"Tests passed: {self.passing}, failed: {self.failing}", *self.notes]
        return "\n".join(lines)


def _classify_apex(workspace: Path, changed: list[str]) -> tuple[list[str], list[str]]:
    """Returns (code_units_needing_coverage, test_classes) from changed .cls/.trigger files."""
    code, tests = [], []
    for rel in changed:
        p = workspace / rel
        if not p.exists() or p.suffix not in (".cls", ".trigger"):
            continue
        if p.suffix == ".cls" and "@istest" in p.read_text(errors="ignore").lower():
            tests.append(p.stem)
        else:
            code.append(p.stem)
    return code, tests


def verify(workspace: Path, alias: str, changed_files: list[str], coverage_target: float) -> VerifyReport:
    rep = VerifyReport()

    ok, errors = deploy_project(workspace, alias)
    if not ok:
        rep.deploy_errors = errors
        return rep

    rep.stage = "tests"
    code_units, test_classes = _classify_apex(workspace, changed_files)
    rep.test_classes = test_classes
    if code_units and not test_classes:
        rep.notes.append(f"Changed Apex {code_units} but no test class was added or changed.")
        return rep
    if not code_units and not test_classes:
        rep.ok, rep.stage = True, "done"
        rep.notes.append("No Apex changes; deploy verified only.")
        return rep

    args = ["apex", "run", "test", "--target-org", alias, "--code-coverage", "--wait", "30"]
    for t in test_classes:
        args += ["--class-names", t]
    data = sf(args, cwd=workspace)
    res = data.get("result") or data.get("data") or {}

    for t in res.get("tests") or []:
        if t.get("Outcome") != "Pass":
            name = t.get("FullName") or f"{(t.get('ApexClass') or {}).get('Name')}.{t.get('MethodName')}"
            rep.test_failures.append(f"{name}: {t.get('Message')} {t.get('StackTrace') or ''}".strip())
    summary = res.get("summary") or {}
    rep.passing = int(summary.get("passing") or 0)
    rep.failing = int(summary.get("failing") or len(rep.test_failures))
    if not res:
        rep.test_failures.append(data.get("message") or "Test run returned no result")
    if rep.test_failures:
        return rep

    rep.stage = "coverage"
    cov = res.get("coverage") or {}
    items = cov.get("coverage", []) if isinstance(cov, dict) else cov
    by_name = {i.get("name"): float(i.get("coveredPercent") or 0) for i in items if i.get("name")}
    for unit in code_units:
        pct = by_name.get(unit, 0.0)
        rep.coverage[unit] = pct
        if pct < coverage_target:
            rep.below_target[unit] = pct
    if rep.below_target:
        return rep

    rep.ok, rep.stage = True, "done"
    return rep
