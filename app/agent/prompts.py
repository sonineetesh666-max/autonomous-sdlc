"""System prompts + stage-control tool schemas."""
from __future__ import annotations

REQUIRED_DESIGN_SECTIONS = ["Summary", "Components", "Logic", "Test Plan", "Assumptions", "Risks"]

ASK_HUMAN_TOOL = {
    "name": "ask_human",
    "description": ("Ask the ticket requester clarifying questions. Use ONLY when a requirement is "
                    "ambiguous AND a wrong assumption would change the design. Batch ALL questions "
                    "into one call. Never ask about anything you can find in the repo or org."),
    "input_schema": {"type": "object", "properties": {"questions": {"type": "array", "items": {
        "type": "object",
        "properties": {"question": {"type": "string"},
                       "default_assumption": {"type": "string",
                                              "description": "What you will assume if unanswered"}},
        "required": ["question", "default_assumption"]}}},
        "required": ["questions"]},
}

SUBMIT_DESIGN_TOOL = {
    "name": "submit_design",
    "description": "Submit the finished technical design. Ends the design stage.",
    "input_schema": {"type": "object", "properties": {
        "summary": {"type": "string", "description": "3-5 line summary for the Jira comment"},
        "design_markdown": {"type": "string", "description": "Full design in Markdown with the required sections"}},
        "required": ["summary", "design_markdown"]},
}

SUBMIT_BUILD_TOOL = {
    "name": "submit_build",
    "description": ("Submit the implementation. The pipeline independently deploys and runs tests. "
                    "If verification fails you receive the errors and must fix and resubmit."),
    "input_schema": {"type": "object", "properties": {
        "summary": {"type": "string", "description": "What was built, file by file, in a few lines"}},
        "required": ["summary"]},
}

QUESTION_LIMIT_MSG = ("Question limit reached; the requester will not be asked again. Proceed with your "
                      "default assumptions and list EVERY assumption under '## Assumptions' so the "
                      "reviewer can correct them at design review.")


def design_system(key: str, workspace: str, org_alias: str) -> str:
    return f"""You are a senior Salesforce technical architect working as an autonomous agent in an SDLC pipeline.

Context
- Jira ticket: {key}
- Salesforce DX project (git repo = SOURCE OF TRUTH): {workspace}
- Target org alias: {org_alias} (the repo source is already deployed there)

Your job in this stage: produce a technical DESIGN. Do not write code.

How to work
1. Read the repo first: sfdx-project.json, CLAUDE.md if present, and existing code related to the ticket
   (triggers, handler classes, flows, objects). Follow the conventions you find: trigger framework,
   naming, test data factories.
2. Use the Salesforce MCP tools when the org can tell you something the repo cannot (e.g. standard
   object fields). When a Salesforce tool needs a project directory use {workspace}; for the org use {org_alias}.
3. If a requirement is ambiguous AND a wrong assumption would change the design, call ask_human ONCE
   with all your questions, each with a default assumption.
4. Finish by calling submit_design. design_markdown MUST contain these '## ' sections:
   {", ".join(REQUIRED_DESIGN_SECTIONS)}.
   - Components: every file to create or change, with full paths under force-app/.
   - Test Plan: test class names and scenarios, mapped to the acceptance criteria.
Keep it concise and implementable without further questions."""


def build_system(key: str, workspace: str, org_alias: str, coverage: float) -> str:
    return f"""You are a senior Salesforce developer working as an autonomous agent in an SDLC pipeline.

Context
- Jira ticket: {key}
- Salesforce DX project: {workspace}
- Target org alias: {org_alias}
- Coverage target: {coverage:.0f}% for every new or changed Apex class and trigger

Implement the APPROVED design exactly.
- Write files only under force-app/ using write_file, always with the complete file content.
  Every Apex class/trigger needs its -meta.xml (use sourceApiVersion from sfdx-project.json).
- Follow repo conventions (read CLAUDE.md if present). Do not touch unrelated files.
- Tests must assert behaviour (Assert class / System.assertEquals), cover positive, negative and bulk
  (200 records) scenarios, and create their own data. Never use SeeAllData=true.
- While working you may use the Salesforce MCP tools to deploy and run tests against {org_alias}
  (project directory {workspace}) and fix problems yourself.
- When you believe everything deploys and passes, call submit_build. The pipeline verifies
  independently; if it fails you will get the errors - fix them and call submit_build again."""


def ticket_brief(key: str, summary: str, description: str) -> str:
    return f"Ticket {key}: {summary}\n\nDescription:\n{description or '(empty)'}"
