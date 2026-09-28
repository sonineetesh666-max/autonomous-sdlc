# Autonomous SDLC with Claudeforce — POC

**Salesforce Beyond CRM: Orchestrating the Autonomous SDLC with Claudeforce**

A Python orchestrator that takes a Jira ticket from *Ready for AI* to a GitHub pull request.
Claude does the reasoning, the Salesforce DX MCP server provides governed org access,
and humans approve at two gates (design and test results).

## Roles

| Piece | MCP role | What it does |
|---|---|---|
| This Python app | **Host** (contains the MCP **client**) | Webhooks, state machine, agent loop, Jira/GitHub calls, independent verification |
| Salesforce DX MCP server (`@salesforce/mcp`) | **Server** | Exposes org capabilities (query, metadata, tests) as tools. Started per run, scoped to one org |
| Claude API | Model | Decides which tool to call, writes the design and code |
| Jira | Human interface | Input tickets, questions, approvals |
| GitHub | Source of truth | Fresh clone per ticket, `ai/<KEY>` branch, PR |

## Flow

```mermaid
stateDiagram-v2
    [*] --> SETUP: human sets "Ready for AI"
    SETUP --> NEEDS_INFO: Definition of Ready fails
    SETUP --> DESIGNING: clone repo, check target org, deploy repo
    DESIGNING --> AWAITING_INPUT: Claude calls ask_human
    AWAITING_INPUT --> DESIGNING: human sets "Answers Submitted"
    DESIGNING --> DESIGN_REVIEW: Claude calls submit_design
    DESIGN_REVIEW --> DESIGNING: human sets "Design Changes Requested"
    DESIGN_REVIEW --> BUILDING: human sets "Design Approved"
    BUILDING --> TEST_REVIEW: submit_build passes independent verification
    BUILDING --> BLOCKED: self-heal limit reached
    TEST_REVIEW --> BUILDING: human sets "Build Changes Requested"
    TEST_REVIEW --> DONE: human sets "Tests Approved" -> PR created
    DONE --> [*]
```

Key principles:
- **The agent never waits.** It saves state (SQLite), posts to Jira, and exits. The next human transition resumes it.
- **Claude handles judgment; code handles plumbing.** Cloning, org checks, deploy verification and PR creation are deterministic Python.
- **Trust but verify.** When Claude calls `submit_build`, the pipeline deploys and runs tests itself. Failures go back to Claude (max `MAX_HEAL_ATTEMPTS`).
- **Least privilege.** Each run starts its own DX MCP server with `--orgs <this ticket's org>` only. No production credentials exist anywhere in the app.
- **No scratch orgs.** Every project routes to one existing, already-authorized `sf` org alias
  (`config/registry.yaml`). The repo is the source of truth and is redeployed to that org before
  every run, so the org never drifts from what's in git.

## Project layout

```
app/
  main.py            FastAPI: Jira webhook, manual trigger, status endpoint
  orchestrator.py    State machine: start, resume_design, revise_design, build, raise_pr
  agent/loop.py      Claude tool-use loop + MCP client bridge to the DX MCP server
  agent/local_tools.py  Repo tools: list/read/search/write (writes only under force-app/)
  agent/prompts.py   System prompts, ask_human / submit_design / submit_build schemas
  sf_cli.py          sf CLI: org check, deploy, independent verification
  jira_client.py     Jira REST v2: issue, comments, transitions, attachments
  git_github.py      Git workspace + GitHub PR API
  state.py           Per-ticket durable state
scripts/preflight.py Validates every credential/tool/integration before a live run
config/registry.yaml Jira project -> repo + target org alias
```

## Setup

### 1. Local prerequisites
Python 3.11+, Node.js 18+ (for `npx`), Salesforce CLI (`sf`), git.

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env        # fill it in
```

### 2. Salesforce
- Authorize the target org once (use the org's **My Domain** URL, not the Lightning domain):
  `sf org login web --alias devhub --instance-url https://<mydomain>.my.salesforce.com`
  (or configure the JWT variables in `.env` for headless servers).
- The alias used here must match `org_alias` for the project in `config/registry.yaml`.
- Your repo must be an SFDX project (`sfdx-project.json` + `force-app/`).
- Optional but recommended: add a `CLAUDE.md` at the repo root with team conventions
  (trigger framework, naming, test data factory). The agent reads it.
- Check the DX MCP tools once: `npx -y @salesforce/mcp@latest --help`. The app logs the
  discovered tool names at the start of each run.

### 3. GitHub
Fine-grained personal access token for the repo with **Contents: read/write** and
**Pull requests: read/write**. (Use a GitHub App in production.)

### 4. Jira
1. **Statuses:** add these to the project workflow. For the POC, make every status reachable from any status ("allow all statuses to transition to this one").
   - Human-set: `Ready for AI`, `Answers Submitted`, `Design Approved`, `Design Changes Requested`, `Tests Approved`, `Build Changes Requested`
   - Bot-set: `AI Analyzing`, `AI Needs Info`, `AI Awaiting Input`, `AI Design Review`, `AI Building`, `AI Test Review`, `PR Raised`, `AI Blocked`
2. **API token:** https://id.atlassian.com/manage-profile/security/api-tokens
3. **Webhook:** Jira Settings → System → WebHooks
   - URL: `https://<your-ngrok-host>/webhooks/jira?token=<WEBHOOK_TOKEN>`
   - Events: *Issue → updated*; JQL filter: `project = SCRUM`
4. **Ticket template** (the Definition of Ready check looks for the first two sections):

```
h2. Business Goal
When a Case is escalated, the account owner must follow up within 24h.

h2. Acceptance Criteria
- GIVEN a Case with IsEscalated = false WHEN it changes to true THEN a Task is created for Account.OwnerId, due in 1 day
- GIVEN a Case with no Account WHEN escalated THEN no Task is created and no error is thrown

h2. Out of Scope
Email notifications.
```

### 5. Run
```bash
uvicorn app.main:app --port 8000
ngrok http 8000        # expose to Jira
```

Without webhooks (useful for rehearsing the demo):
```bash
curl -X POST "http://localhost:8000/run/SCRUM-1/START?token=change-me"
curl "http://localhost:8000/runs/SCRUM-1?token=change-me"
```

Before a live run, validate every credential and integration:
```bash
python scripts/preflight.py
```

## Demo script (≈ 8 minutes)
1. Show the ticket and move it to **Ready for AI**.
2. Show the logs: repo cloned, target org checked, DX MCP tools discovered, Claude exploring the repo.
3. Jira shows clarification questions → answer them → **Answers Submitted**.
4. Design v1 appears (comment, attachment, file on the `ai/` branch) → request one change → v2 → **Design Approved**.
5. Logs show the build and self-healing (deploy/test errors fed back to Claude).
6. Test report with coverage table and diff link → **Tests Approved** → PR link.

Record a backup video; live org deploys can be slow.

## Known limits (say these in the deck)
- One Jira project ↔ one repo per registry entry; monorepos need component-based routing.
- Flows and declarative metadata are deployed and validated, but only Apex gets coverage gating.
- The pipeline stops at the PR. Release to production stays in your existing CI/CD with human merge.
- Duplicate webhook events are dropped per ticket. Events arriving during a long run are ignored.


Why will the audience be interested in your topic?
Salesforce Beyond CRM : Autonomous SDLC using Claude
This topic has many interest areas, and the audience is not limited to Salesforce engineer, as topic says 
SDLC Automation, 
We are automating the Legacy SDLC process by leveraging CLaude API, and this can build interest in audience that they can also leverage claude api to automate things

Why would our partners/customers be interested in it?

How are you planning to structure your presentation?
How will you divide the content, questions, demos, etc. between you and your co-presenters?
How do you plan to interact and engage with the audience?
What problem or challenge does your session help solve?
What materials, demos, or technical setup do you need beforehand?
Is there anything you need from the organizing team to make the session successful?
These points will help us understand not just the topic, but also the overall session experience you are planning to create.