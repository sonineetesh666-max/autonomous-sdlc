"""The SDLC state machine. Each public method handles ONE human-triggered event, does its work,
saves state, and exits. Humans can take hours or days between events."""
from __future__ import annotations

import asyncio
import json
import logging
import os
import shutil
from pathlib import Path

from mcp import StdioServerParameters

from . import sf_cli
from .agent.local_tools import file_tools
from .agent.loop import FinishSignal, PauseSignal, ToolText, run_agent, tool_result
from .agent.prompts import (ASK_HUMAN_TOOL, QUESTION_LIMIT_MSG, REQUIRED_DESIGN_SECTIONS,
                            SUBMIT_BUILD_TOOL, SUBMIT_DESIGN_TOOL, build_system, design_system,
                            ticket_brief)
from .config import Settings
from .git_github import GitHubClient, GitWorkspace
from .jira_client import JiraClient
from .state import RunState, StateStore

log = logging.getLogger("sdlc.orchestrator")
DESIGN_DIR = "docs/ai-design"


class Orchestrator:
    def __init__(self, s: Settings, store: StateStore, jira: JiraClient, git: GitWorkspace, gh: GitHubClient):
        self.s, self.store, self.jira, self.git, self.gh = s, store, jira, git, gh
        self.S = s.statuses

    # ------------------------------------------------------------------ entry point
    async def handle(self, key: str, action: str) -> None:
        routes = {
            "START": self.start,
            "ANSWERS": self.resume_design,
            "DESIGN_CHANGES": self.revise_design,
            "DESIGN_APPROVED": lambda k: self.build(k, with_feedback=False),
            "BUILD_CHANGES": lambda k: self.build(k, with_feedback=True),
            "TESTS_APPROVED": self.raise_pr,
        }
        try:
            await routes[action](key)
        except Exception as e:
            log.exception("%s %s failed", key, action)
            st = self.store.load(key) or RunState(ticket_key=key)
            self._block(st, f"Unexpected error during {action}: {e}")

    # ------------------------------------------------------------------ 1-3 intake, setup, design
    async def start(self, key: str) -> None:
        existing = self.store.load(key)
        if existing and existing.stage not in ("BLOCKED", "DONE", "NEEDS_INFO"):
            log.info("Ignoring START for %s (stage %s)", key, existing.stage)
            return

        extra = [self.s.jira_field_target_repo] if self.s.jira_field_target_repo else []
        f = self.jira.get_issue(key, extra)["fields"]
        st = RunState(ticket_key=key, summary=f.get("summary", ""),
                      reporter_id=(f.get("reporter") or {}).get("accountId"))

        # Routing is deterministic configuration, never AI guesswork
        st.project_key = key.split("-")[0]
        proj = self.s.projects.get(st.project_key)
        if not proj:
            return self._block(st, f"No registry entry for Jira project '{st.project_key}' "
                                   f"(config/registry.yaml).")
        st.repo = proj.repo
        if extra and f.get(extra[0]):
            st.repo = str(f[extra[0]]).strip()
        st.base_branch, st.branch, st.org_alias = proj.base_branch, f"ai/{key}", proj.org_alias

        # Definition of Ready
        desc = (f.get("description") or "").lower()
        missing = [sec for sec in self.s.required_ticket_sections if sec.lower() not in desc]
        if missing:
            st.stage = "NEEDS_INFO"
            self._post(st, f"{self.jira.mention(st.reporter_id)}*Ticket is not ready for AI.* "
                           f"Missing section(s): {', '.join(missing)}. Update the description and move "
                           f"the ticket back to *{self.S['ready']}*.")
            self.jira.transition_to(key, self.S["needs_info"])
            return self.store.save(st)

        st.stage = "SETUP"
        self.store.save(st)
        self.jira.transition_to(key, self.S["analyzing"])

        # Plumbing = plain code (no AI): fresh clone, confirm the org, deploy repo source
        ws = await asyncio.to_thread(self.git.prepare, st.repo, st.base_branch, st.branch, key, True)
        st.workspace = str(ws)
        ok, msg = await asyncio.to_thread(sf_cli.check_org, st.org_alias)
        if not ok:
            return self._block(st, f"Target org '{st.org_alias}' is not usable: {msg}")
        ok, errors = await asyncio.to_thread(sf_cli.deploy_project, ws, st.org_alias)
        if not ok:
            return self._block(st, "Repo source did not deploy cleanly to the org (base branch broken or "
                                   "missing dependencies):\n" + "\n".join(errors[:30]))

        st.stage = "DESIGNING"
        self.store.save(st)
        messages = [{"role": "user", "content": ticket_brief(key, st.summary, f.get("description"))}]
        await self._run_design(st, messages)

    async def resume_design(self, key: str) -> None:
        st = self.store.load(key)
        if not st or st.stage != "AWAITING_INPUT":
            return log.info("Ignoring ANSWERS for %s", key)
        answers = self.jira.human_comments_since(key, st.last_bot_post_at)
        text = ("Answers from the requester:\n\n" + "\n\n---\n\n".join(answers)) if answers else \
               "The requester gave no answers. Use your default assumptions and list them under Assumptions."
        messages = st.messages + [{"role": "user", "content":
                                   st.pending_results + [tool_result(st.pending_tool_id, text)]}]
        st.messages, st.pending_tool_id, st.pending_results, st.stage = [], None, [], "DESIGNING"
        self.store.save(st)
        self.jira.transition_to(key, self.S["analyzing"])
        await self._run_design(st, messages)

    async def revise_design(self, key: str) -> None:
        st = self.store.load(key)
        if not st or st.stage != "DESIGN_REVIEW":
            return log.info("Ignoring DESIGN_CHANGES for %s", key)
        if st.design_version > self.s.max_design_revisions:
            return self._block(st, f"Design revision limit ({self.s.max_design_revisions}) reached.")
        feedback = self.jira.human_comments_since(key, st.last_bot_post_at)
        current = self._design_path(st).read_text()
        f = self.jira.get_issue(key)["fields"]
        st.stage = "DESIGNING"
        self.store.save(st)
        self.jira.transition_to(key, self.S["analyzing"])
        content = (ticket_brief(key, st.summary, f.get("description"))
                   + f"\n\nCURRENT DESIGN (v{st.design_version}):\n{current}"
                   + "\n\nREVIEWER FEEDBACK:\n" + ("\n\n".join(feedback) or "(none given)")
                   + "\n\nRevise the design to address every feedback point; keep what was not criticised. "
                     "Call submit_design with the FULL revised design.")
        await self._run_design(st, [{"role": "user", "content": content}])

    async def _run_design(self, st: RunState, messages: list) -> None:
        tools, handlers = file_tools(Path(st.workspace), writable=False)

        async def ask_human(inp):
            if st.question_rounds >= self.s.max_question_rounds:
                return ToolText(QUESTION_LIMIT_MSG)
            return PauseSignal({"questions": inp.get("questions") or []})

        async def submit_design(inp):
            md = (inp.get("design_markdown") or "").lower()
            missing = [h for h in REQUIRED_DESIGN_SECTIONS if f"## {h.lower()}" not in md]
            if missing:
                return ToolText(f"Design rejected: missing sections {missing}. Fix and resubmit.", True)
            return FinishSignal(inp)

        handlers.update(ask_human=ask_human, submit_design=submit_design)
        out = await run_agent(
            model=self.s.anthropic_model, max_tokens=self.s.max_tokens,
            system=design_system(st.ticket_key, st.workspace, st.org_alias),
            messages=messages, local_tools=tools + [ASK_HUMAN_TOOL, SUBMIT_DESIGN_TOOL],
            handlers=handlers, mcp_params=self._mcp(st), finish_tool="submit_design")

        if out.kind == "paused":
            st.question_rounds += 1
            st.messages, st.pending_tool_id, st.pending_results = out.messages, out.pending_tool_id, out.partial_results
            st.stage = "AWAITING_INPUT"
            self._post(st, self._format_questions(st, out.payload["questions"]))
            self.jira.transition_to(st.ticket_key, self.S["awaiting_input"])
            self.store.save(st)
        elif out.kind == "finished":
            await self._publish_design(st, out.payload)
        else:
            self._block(st, f"Design agent failed: {out.payload.get('reason')}")

    async def _publish_design(self, st: RunState, payload: dict) -> None:
        key, ws = st.ticket_key, Path(st.workspace)
        st.design_version += 1
        path = self._design_path(st)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(f"# Design: {key} - {st.summary}\n\n_Version {st.design_version}, AI-generated. "
                        f"Reviewed in Jira._\n\n{payload['design_markdown']}\n")
        await asyncio.to_thread(self.git.commit_and_push, ws, st.branch, f"{key}: AI design v{st.design_version}")
        self.jira.attach(key, f"{key}-design-v{st.design_version}.md", path.read_bytes())
        url = self.gh.blob_url(st.repo, st.branch, f"{DESIGN_DIR}/{key}.md")
        self._post(st, f"*Design v{st.design_version} ready for review*\n\n{payload.get('summary', '')}\n\n"
                       f"Full design: [{key}.md|{url}] (also attached)\n\n"
                       f"*Approve:* move to *{self.S['design_approved']}*.\n"
                       f"*Changes:* add comment(s), then move to *{self.S['design_changes']}*.")
        self.jira.transition_to(key, self.S["design_review"])
        st.stage = "DESIGN_REVIEW"
        self.store.save(st)

    # ------------------------------------------------------------------ 6-8 build, verify, test review
    async def build(self, key: str, with_feedback: bool) -> None:
        st = self.store.load(key)
        expected = "TEST_REVIEW" if with_feedback else "DESIGN_REVIEW"
        if not st or st.stage != expected:
            return log.info("Ignoring build event for %s (stage %s)", key, st.stage if st else None)
        feedback = self.jira.human_comments_since(key, st.last_bot_post_at) if with_feedback else []
        design = self._design_path(st).read_text()
        f = self.jira.get_issue(key)["fields"]
        st.build_rounds += 1
        st.stage = "BUILDING"
        self.store.save(st)
        self.jira.transition_to(key, self.S["building"])

        ws = Path(st.workspace)
        tools, handlers = file_tools(ws, writable=True)
        attempts = {"n": 0}

        async def submit_build(inp):
            attempts["n"] += 1
            changed = await asyncio.to_thread(self.git.changed_files, ws, st.base_branch)
            rep = await asyncio.to_thread(sf_cli.verify, ws, st.org_alias, changed, self.s.coverage_target)
            if rep.ok:
                return FinishSignal({"summary": inp.get("summary", ""), "report": rep})
            if attempts["n"] >= self.s.max_heal_attempts:
                return FinishSignal({"failed": True, "report": rep})
            return ToolText(f"VERIFICATION FAILED (attempt {attempts['n']} of {self.s.max_heal_attempts}).\n"
                            f"{rep.as_text()}\nFix the problems and call submit_build again.", True)

        handlers["submit_build"] = submit_build
        content = (ticket_brief(key, st.summary, f.get("description"))
                   + f"\n\nAPPROVED DESIGN:\n{design}")
        if feedback:
            content += ("\n\nThe code from the previous round is already in the workspace. "
                        "REVIEWER FEEDBACK to address:\n" + "\n\n".join(feedback))
        out = await run_agent(
            model=self.s.anthropic_model, max_tokens=self.s.max_tokens,
            system=build_system(key, st.workspace, st.org_alias, self.s.coverage_target),
            messages=[{"role": "user", "content": content}], local_tools=tools + [SUBMIT_BUILD_TOOL],
            handlers=handlers, mcp_params=self._mcp(st), finish_tool="submit_build")

        if out.kind != "finished":
            return self._block(st, f"Build agent failed: {out.payload.get('reason')}")
        rep = out.payload["report"]
        if out.payload.get("failed"):
            return self._block(st, f"Self-healing gave up after {attempts['n']} attempts.\n{rep.as_text()}")

        await asyncio.to_thread(self.git.commit_and_push, ws, st.branch,
                                f"{key}: AI implementation (round {st.build_rounds})")
        st.test_report = rep.to_dict()
        self.jira.attach(key, f"{key}-test-report-r{st.build_rounds}.json",
                         json.dumps(st.test_report, indent=2).encode())
        self._post(st, self._format_report(st, out.payload.get("summary", ""), attempts["n"]))
        self.jira.transition_to(key, self.S["test_review"])
        st.stage = "TEST_REVIEW"
        self.store.save(st)

    # ------------------------------------------------------------------ 9 pull request
    async def raise_pr(self, key: str) -> None:
        st = self.store.load(key)
        if not st or st.stage != "TEST_REVIEW":
            return log.info("Ignoring TESTS_APPROVED for %s", key)
        r = st.test_report
        cov = "\n".join(f"| {k} | {v:.0f}% |" for k, v in r.get("coverage", {}).items()) or "| (no Apex) | - |"
        body = (f"## {key}: {st.summary}\n\nJira: {self.jira.browse_url(key)}\n"
                f"Design: `{DESIGN_DIR}/{key}.md` (v{st.design_version}, human-approved)\n\n"
                f"### Verification (independent pipeline run)\n"
                f"- Tests: {r.get('passing', 0)} passed, {r.get('failing', 0)} failed\n\n"
                f"| Class / trigger | Coverage |\n|---|---|\n{cov}\n\n"
                f"_Generated by the Autonomous SDLC agent. Human gates passed: design review, test review._")
        st.pr_url = await asyncio.to_thread(self.gh.create_pr, st.repo, st.branch, st.base_branch,
                                            f"{key}: {st.summary}", body)
        self._post(st, f"*Pull request raised:* [{st.pr_url}|{st.pr_url}]")
        self.jira.transition_to(key, self.S["pr_raised"])
        st.stage = "DONE"
        self.store.save(st)

    # ------------------------------------------------------------------ helpers
    def _mcp(self, st: RunState) -> StdioServerParameters:
        # One DX MCP server per run, allowed to touch exactly ONE org.
        # Resolved path: on Windows npx is a .cmd shim that subprocess won't find by bare name.
        return StdioServerParameters(
            command=shutil.which("npx") or "npx",
            args=["-y", "@salesforce/mcp@latest", "--orgs", st.org_alias,
                  "--toolsets", self.s.mcp_toolsets, *self.s.mcp_extra_args],
            env=dict(os.environ), cwd=st.workspace)

    def _design_path(self, st: RunState) -> Path:
        return Path(st.workspace) / DESIGN_DIR / f"{st.ticket_key}.md"

    def _post(self, st: RunState, body: str) -> None:
        st.last_bot_post_at = self.jira.add_comment(st.ticket_key, body)

    def _block(self, st: RunState, reason: str) -> None:
        st.stage = "BLOCKED"
        self.store.save(st)
        try:
            self._post(st, f"*Blocked - needs a human*\n{{noformat}}{reason[:6000]}{{noformat}}\n"
                           f"Fix the cause, then move the ticket to *{self.S['ready']}* to restart.")
            self.jira.transition_to(st.ticket_key, self.S["blocked"])
        except Exception:
            log.exception("Could not report block for %s", st.ticket_key)

    def _format_questions(self, st: RunState, questions: list[dict]) -> str:
        lines = [f"{self.jira.mention(st.reporter_id)}*Clarification needed (round {st.question_rounds})*", ""]
        for i, q in enumerate(questions, 1):
            lines += [f"*Q{i}.* {q.get('question')}", f"_If no answer, I will assume:_ {q.get('default_assumption')}", ""]
        lines.append(f"Reply in one or more comments (e.g. 'Q1: ...'), then move the ticket to "
                     f"*{self.S['answers_submitted']}*.")
        return "\n".join(lines)

    def _format_report(self, st: RunState, summary: str, attempts: int) -> str:
        r = st.test_report
        rows = "\n".join(f"|{k}|{v:.0f}%|{self.s.coverage_target:.0f}%|" for k, v in r.get("coverage", {}).items())
        table = f"||Class / trigger||Coverage||Target||\n{rows}" if rows else "_No Apex changed._"
        compare = self.gh.compare_url(st.repo, st.base_branch, st.branch)
        return (f"*Build verified - ready for test review* (verification attempts: {attempts})\n\n"
                f"{summary}\n\n*Tests:* {r.get('passing', 0)} passed, {r.get('failing', 0)} failed\n{table}\n\n"
                f"Code diff: [compare|{compare}] | Full report attached\n\n"
                f"*Approve:* move to *{self.S['tests_approved']}* (PR will be raised).\n"
                f"*Changes:* add comment(s), then move to *{self.S['build_changes']}*.")
