"""Settings (.env) + project registry (config/registry.yaml)."""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

import yaml
from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent
load_dotenv(ROOT / ".env")

# Logical status name -> Jira status name. Override in registry.yaml under jira_statuses.
DEFAULT_STATUSES = {
    # set by humans (these trigger the orchestrator)
    "ready": "Ready for AI",
    "answers_submitted": "Answers Submitted",
    "design_approved": "Design Approved",
    "design_changes": "Design Changes Requested",
    "tests_approved": "Tests Approved",
    "build_changes": "Build Changes Requested",
    # set by the bot
    "analyzing": "AI Analyzing",
    "needs_info": "AI Needs Info",
    "awaiting_input": "AI Awaiting Input",
    "design_review": "AI Design Review",
    "building": "AI Building",
    "test_review": "AI Test Review",
    "pr_raised": "PR Raised",
    "blocked": "AI Blocked",
}


def _env(name: str, default: str = "") -> str:
    return os.getenv(name, default).strip()


def _env_list(name: str, default: str = "") -> list[str]:
    return [x.strip() for x in _env(name, default).split(",") if x.strip()]


@dataclass
class ProjectConfig:
    key: str
    repo: str                                   # "owner/name"
    org_alias: str                              # existing, already-authorized `sf` org alias
    base_branch: str = "main"


@dataclass
class Settings:
    anthropic_model: str
    max_tokens: int
    jira_base_url: str
    jira_email: str
    jira_api_token: str
    jira_bot_account_id: str
    jira_field_target_repo: str
    webhook_token: str
    github_token: str
    git_author_name: str
    git_author_email: str
    work_dir: Path
    state_db: Path
    mcp_toolsets: str
    mcp_extra_args: list[str]
    coverage_target: float
    max_question_rounds: int
    max_design_revisions: int
    max_heal_attempts: int
    required_ticket_sections: list[str]
    sf_devhub_alias: str
    sf_jwt_client_id: str
    sf_jwt_key_file: str
    sf_jwt_username: str
    sf_jwt_instance_url: str
    statuses: dict[str, str] = field(default_factory=dict)
    projects: dict[str, ProjectConfig] = field(default_factory=dict)


def load_settings() -> Settings:
    reg_path = ROOT / "config" / "registry.yaml"
    reg = yaml.safe_load(reg_path.read_text()) if reg_path.exists() else {}
    reg = reg or {}
    statuses = {**DEFAULT_STATUSES, **(reg.get("jira_statuses") or {})}
    projects = {k: ProjectConfig(key=k, **v) for k, v in (reg.get("projects") or {}).items()}

    work_dir = Path(_env("WORK_DIR", str(ROOT / "workspaces"))).resolve()
    work_dir.mkdir(parents=True, exist_ok=True)

    return Settings(
        anthropic_model=_env("CLAUDE_MODEL", "claude-sonnet-5"),
        max_tokens=int(_env("CLAUDE_MAX_TOKENS", "16000")),
        jira_base_url=_env("JIRA_BASE_URL").rstrip("/"),
        jira_email=_env("JIRA_EMAIL"),
        jira_api_token=_env("JIRA_API_TOKEN"),
        jira_bot_account_id=_env("JIRA_BOT_ACCOUNT_ID"),
        jira_field_target_repo=_env("JIRA_FIELD_TARGET_REPO"),
        webhook_token=_env("WEBHOOK_TOKEN", "change-me"),
        github_token=_env("GITHUB_TOKEN"),
        git_author_name=_env("GIT_AUTHOR_NAME", "AI SDLC Bot"),
        git_author_email=_env("GIT_AUTHOR_EMAIL", "ai-sdlc-bot@example.com"),
        work_dir=work_dir,
        state_db=Path(_env("STATE_DB", str(ROOT / "state.db"))).resolve(),
        mcp_toolsets=_env("MCP_TOOLSETS", "orgs,metadata,data,testing"),
        mcp_extra_args=_env("MCP_EXTRA_ARGS").split(),
        coverage_target=float(_env("COVERAGE_TARGET", "85")),
        max_question_rounds=int(_env("MAX_QUESTION_ROUNDS", "2")),
        max_design_revisions=int(_env("MAX_DESIGN_REVISIONS", "3")),
        max_heal_attempts=int(_env("MAX_HEAL_ATTEMPTS", "5")),
        required_ticket_sections=_env_list("REQUIRED_TICKET_SECTIONS", "Business Goal,Acceptance Criteria"),
        sf_devhub_alias=_env("SF_DEVHUB_ALIAS", "devhub"),
        sf_jwt_client_id=_env("SF_JWT_CLIENT_ID"),
        sf_jwt_key_file=_env("SF_JWT_KEY_FILE"),
        sf_jwt_username=_env("SF_JWT_USERNAME"),
        sf_jwt_instance_url=_env("SF_JWT_INSTANCE_URL", "https://login.salesforce.com"),
        statuses=statuses,
        projects=projects,
    )
