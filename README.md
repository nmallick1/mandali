# Mandali

> *Mandali (मंडली, pronounced "mun-da-lee") — Sanskrit: a circle of specialists that deliberates and acts together*

Autonomous multi-agent collaborative development system using GitHub Copilot SDK

## Prerequisites

Before using Mandali, ensure the following are installed:

| Requirement | How to install |
|-------------|---------------|
| **Python 3.10+** | [python.org](https://www.python.org/downloads/) |
| **Node.js 18+** | [nodejs.org](https://nodejs.org/) (required for Copilot CLI) |
| **GitHub Copilot CLI** | `winget install GitHub.Copilot` or `npm install -g @github/copilot` |
| **GitHub Copilot license** | Active Copilot Individual, Business, or Enterprise subscription |

**Verify your setup:**
```bash
python --version      # 3.10 or higher
copilot --version     # Should print the CLI version
```

> **Tip:** If the CLI is installed but not in your PATH, set the `COPILOT_CLI_PATH` environment variable to point directly at the binary.

---

## Quick Start

### Install

```bash
# Option A: Install as a command (recommended)
pip install git+https://github.com/nmallick1/mandali.git

# Option B: Clone and run directly
git clone https://github.com/nmallick1/mandali.git
cd mandali
pip install -r requirements.txt
```

Mandali checks for updates on each launch and notifies you when a newer version is available.

### Run

```bash
# If installed via pip (Option A):
mandali --plan phases/_INDEX.md --out-path ./output

# If cloned (Option B):
python mandali.py --plan phases/_INDEX.md --out-path ./output

# Generate a NEW plan from scratch via interview
mandali --prompt "Add caching to the API layer" --generate-plan --out-path ./output

# Control verification retries (default: 5, set 0 to disable verification)
mandali --plan phases/_INDEX.md --out-path ./output --max-retries 3
```

---

## MCP Server Configuration

The orchestrator automatically loads MCP server configuration from:
1. `~/.copilot/mcp-config.json` (user config - primary)
2. `.copilot/mcp-config.json` (project config - fallback)

The orchestrator also passes the user's `~/.copilot` config directory to agent sessions, so locally installed **skills and extensions** are available to all persona agents.

### Example mcp-config.json

```json
{
  "mcpServers": {
    "aspire": {
      "type": "local",
      "command": "aspire",
      "args": ["mcp", "start"],
      "tools": ["*"]
    },
    "playwright": {
      "type": "local",
      "command": "npm",
      "args": ["exec", "--yes", "--", "@playwright/mcp@latest"],
      "tools": ["*"]
    }
  }
}
```

Agents will have access to all configured MCP servers, enabling them to:
- Query databases
- Browse web pages
- Interact with APIs
- Access specialized development tools

---

## Phased Plan Structure (Recommended for Large Projects)

For large projects, plans are split into multiple files to prevent context loss:

```
<out-path>/
├── phases/
│   ├── _CONTEXT.md           # Global context (READ FIRST)
│   │                         # Architecture, security, non-negotiables
│   ├── _INDEX.md             # Phase tracking table with status
│   │                         # Agents update this after each phase
│   ├── phase-01-foundation.md
│   ├── phase-02-core.md
│   ├── phase-03-feature.md
│   └── ...
└── mandali-artifacts/        # Orchestration files (auto-created)
    ├── conversation.txt
    ├── satisfaction.txt
    └── DecisionsTracker.md
```

### Why Phased Files?

| Single File Plan | Phased File Structure |
|------------------|----------------------|
| Context lost in long documents | Each phase is self-contained |
| Easy to miss tasks | Clear task numbering (XX.Y) |
| Hard to track progress | _INDEX.md shows status |
| Agents forget constraints | _CONTEXT.md always available |

### File Purposes

| File | Purpose |
|------|---------|
| `_CONTEXT.md` | **Read first** - Architecture, security, non-negotiables that apply to ALL phases |
| `_INDEX.md` | **Tracking** - Phase table with status, commits, dependencies |
| `phase-XX-*.md` | **Implementation** - Detailed tasks for one phase with quality gates |

---

## Usage

### Command Line Arguments

```bash
python mandali.py [OPTIONS]
```

| Argument | Required | Description |
|----------|----------|-------------|
| `--out-path <path>` | **Yes** | Output directory where Mandali creates files. If inside a git repo, a worktree is created automatically for isolation |
| `--plan <path>` | One of plan/prompt | Path to existing plan (_INDEX.md or single plan.md) |
| `--prompt <text>` | One of plan/prompt | Prompt with instructions and references to plan files |
| `--generate-plan` | No (default: off) | Opt-in: run interview + plan generation instead of direct launch |
| `--stall-timeout <minutes>` | No (default: 5) | Minutes of inactivity before human escalation |
| `--max-retries <n>` | No (default: 5) | Max verification rounds after all agents SATISFIED. Set 0 to disable |
| `--verbose` | No | Show detailed status updates |
| `--describe <persona>` | No | Show detailed description of a persona (dev, security, pm, qa, sre) |
| `--teams` | No | Enable Teams integration for notifications and remote replies |
| `--setup-teams` | No | One-time setup: provision Azure Bot + cloud relay for Teams integration |

### Examples

```bash
# Direct launch with existing plan file
python mandali.py --plan ./phases/_INDEX.md --out-path ./output

# Direct launch with prompt referencing existing files
python mandali.py --prompt "Read phases/_CONTEXT.md and phases/_INDEX.md. Complete all phases." --out-path ./output

# Generate a NEW plan from scratch (opt-in)
python mandali.py --prompt "Add Redis caching for API responses" --generate-plan --out-path ./output/redis-caching

# Longer timeout for complex tasks
python mandali.py --plan phases/_INDEX.md --out-path ./output --stall-timeout 10
```

### What is `--stall-timeout`?

The `--stall-timeout` option controls how long the orchestrator waits before escalating to a human when agents stop making progress.

| Value | Behavior |
|-------|----------|
| `5` (default) | After 5 minutes of no conversation activity, pause agents and ask human for guidance |
| `10` | More patient - good for complex implementations where agents may take longer |
| `2` | Aggressive - escalate quickly if you want tight human oversight |

**When stall is detected:**
1. Orchestrator injects: `@AllAgents - Escalating to @HUMAN for guidance`
2. All agents pause and update status to `PAUSED`
3. Human is presented with options:
   - Provide guidance (relayed to agents)
   - View recent conversation
   - Abort

---

## Modes

### Default: Direct Launch (no `--generate-plan`)

By default, the orchestrator **skips** interview, plan generation, and AI review. It assumes you already have plan files ready.

#### With `--plan`

Reads the plan file, uses LLM to discover all referenced artifacts (up to 5 levels deep), copies them to the workspace, and launches agents.

```bash
python mandali.py --plan ./phases/_INDEX.md --out-path ./output
```

#### With `--prompt`

The LLM extracts file/folder paths mentioned in your prompt, reads those files, recursively discovers any files they reference (up to 5 levels), copies everything to the workspace, and launches agents. Your prompt is passed as additional context alongside the plan.

```bash
python mandali.py --prompt "Read phases/_CONTEXT.md and phases/_INDEX.md. Start from Phase 3." --out-path ./my-project
```

**What happens:**
1. 🔍 LLM extracts paths: `phases/_CONTEXT.md`, `phases/_INDEX.md`
2. 🔍 Reads those files, discovers nested references (depth 1/5, 2/5, ...)
3. 📁 Copies all artifacts to workspace
4. ✅ Shows you what was found — you confirm or reject
5. 🚀 Launches agents with plan + prompt as context

#### Prompt Tips

Reference your plan files explicitly in the prompt:
- ✅ `"Read phases/_CONTEXT.md and phases/_INDEX.md. Complete all phases."`
- ✅ `"Follow the plan in phases/_INDEX.md. Start from Phase 3."`
- ❌ `"Continue the implementation"` (no file references — orchestrator won't find plan files)

### Opt-in: Plan Generation (`--generate-plan`)

Add `--generate-plan` to trigger the full interview and plan generation flow. Only works with `--prompt`.

1. **AI Interviewer** - Asks clarifying questions about scope, phases, existing context
2. **Plan Generation** - Creates phased file structure (_CONTEXT.md, _INDEX.md, phase-XX.md files)
3. **User Approval** - Shows plan directory, you review/edit externally, then accept or reject
4. **Plan Review** - AI validates phased structure
5. **Agent Launch** - 5 personas begin autonomous work

```bash
python mandali.py --prompt "Add rate limiting to the API" --generate-plan --out-path ./output
```

---

## Workspace Isolation (Git Worktrees)

When `--out-path` points to a directory inside a git repository, Mandali automatically creates a **git worktree** in a sibling directory. Agents work entirely in the worktree — your original directory is never touched.

```
myproject/                        ← your repo (untouched)
myproject-mandali-20260210-053400/ ← worktree (agents work here)
```

**What happens:**
1. Mandali detects `--out-path` is inside a git repo
2. Creates a branch `mandali/session-<timestamp>`
3. Creates a worktree at `<parent>/<dirname>-mandali-<timestamp>/`
4. Redirects all agent work to the worktree
5. After the run, shows instructions to merge or discard

**After the run:**
```bash
# Review what agents changed
git diff main..mandali/session-20260210-053400

# Keep the changes
cd myproject
git merge mandali/session-20260210-053400

# Or discard everything
git worktree remove ../myproject-mandali-20260210-053400
git branch -D mandali/session-20260210-053400
```

**When `--out-path` is NOT inside a git repo**, no worktree is created and agents work directly in the specified directory (same as before).

---

## Interactive Monitoring

While agents work, you see periodic status updates:

```
[14:32:15] 📊 ✅dev 🔧sec ⏳pm ⏳qa ⏳sre | 12 msgs
           └─ @Dev: Implementing Phase 1 task 1.1, writing tests first...
```

**Status icons:**
| Icon | Meaning |
|------|---------|
| ✅ | SATISFIED - Agent's criteria are met |
| 🔧 | WORKING - Agent is actively working |
| 🔴 | BLOCKED - Agent is blocked on something |
| ⏳ | Waiting - Agent is waiting for others |

**You can type a message at any time** to interject. Your message is injected as:
```
@AllAgents - Human says:
[your message]
```

All agents will see and respond to your guidance.

---

## Agent Communication

### Protocol

| Mention | Meaning |
|---------|---------|
| `@Dev` | Addressing Developer |
| `@Security` | Addressing Security Architect |
| `@PM` | Addressing Product Manager |
| `@QA` | Addressing QA Engineer |
| `@SRE` | Addressing SRE |
| `@Team` or `@AllAgents` | Addressing everyone |
| `@HUMAN` | Human arbiter (injected by orchestrator) |

### Turn-Taking Rules

1. **When to speak:**
   - You are @mentioned
   - Discussion is in your domain (security issue → @Security speaks)
   - You have concerns about current work
   - Handoff to you: "@QA please test"

2. **When to stay quiet:**
   - You're not mentioned and it's not your domain
   - Implementation is in progress (unless you spot an issue)
   - Your feedback was already addressed

### conversation.txt Format

```
[21:15:00] @PM: @Team - I've reviewed the plan. Acceptance criteria look clear.
@Dev, you can start with Phase 1. SATISFACTION_STATUS: WORKING

[21:16:30] @DEV: @PM - Acknowledged. Starting Phase 1 implementation.
Creating SkillParser.cs... @QA - Ready for testing when available.
SATISFACTION_STATUS: WORKING

[21:18:00] @SECURITY: @Dev - Quick concern on line 45. You're not validating
the input path. This could allow path traversal. Please add validation.
SATISFACTION_STATUS: BLOCKED - Path traversal vulnerability
```

---

## Agent Lifecycle

```
┌──────────────────────────────────────────────────────────────┐
│                     AGENT LIFECYCLE                          │
├──────────────────────────────────────────────────────────────┤
│                                                              │
│  1. LAUNCH                                                   │
│     Orchestrator creates session with:                       │
│     - System prompt (persona definition)                     │
│     - Plan artifacts (phased or single-file)                 │
│     - MCP servers (from ~/.copilot/mcp-config.json)          │
│     - User's Copilot config (skills, extensions)             │
│     - Model verified via SDK list_models()                   │
│                                                              │
│  2. INTRODUCTION                                             │
│     Agent introduces self, appends to conversation.txt       │
│                                                              │
│  3. WORK LOOP (autonomous)                                   │
│     While not terminated:                                    │
│       - Read conversation.txt (new entries since last read)  │
│       - Decide if should respond                             │
│       - If yes: do work, append response                     │
│       - Update satisfaction status                           │
│       - Check for @ORCHESTRATOR messages                     │
│                                                              │
│  4. PAUSE (on escalation)                                    │
│     - Finish atomic work                                     │
│     - Update status to PAUSED                                │
│     - Wait for @HUMAN message                                │
│                                                              │
│  5. TERMINATE (on victory or abort)                          │
│     - Acknowledge completion                                 │
│     - Session destroyed by orchestrator                      │
│                                                              │
└──────────────────────────────────────────────────────────────┘
```

---

## Human Escalation Flow

**Trigger:** No conversation activity for N minutes, or agent requests `@HUMAN`.

**Escalation message (injected by orchestrator):**
```
@AllAgents - Escalating to @HUMAN for guidance.
Please pause your current work. Wait for human input before continuing.

Current status:
- @Dev: WORKING
- @Security: BLOCKED - Disagreement on encryption approach
- @PM: SATISFIED
- @QA: WORKING
- @SRE: SATISFIED
```

**Agents respond by:**
1. Finishing any atomic operation
2. Updating status to `PAUSED - Awaiting human guidance`
3. Stopping until human responds

**Human response (injected by orchestrator):**
```
@AllAgents - Human guidance:
Use AES-256-GCM. This is a company standard.
@Security, update requirements. @Dev, proceed with this approach.
```

Agents acknowledge, update status to WORKING/SATISFIED, and continue.

---

## Teams Integration

Monitor agent progress and provide guidance from Microsoft Teams — no terminal required.

### Quick Setup

```bash
# 1. Provision Azure resources (Bot, Relay, MSI) — ~3 minutes
mandali --setup-teams

# 2. Upload the generated ZIP to Teams
#    Go to Teams Admin Center → Manage Apps → Upload → mandali-bot.zip

# 3. Run with Teams enabled
mandali --plan phases/_INDEX.md --out-path ./output --teams
```

### How It Works

```
Teams ──→ Azure Bot Service ──→ Cloud Relay (App Service) ──→ WebSocket ──→ Mandali
  ←── reply ←── relay ←── WebSocket ←── agents post updates
```

1. You message the Mandali bot in Teams
2. Azure Bot Service routes the message to the cloud relay
3. The relay forwards it over WebSocket to your running Mandali instance
4. Your message is injected into `conversation.txt` as `@HUMAN` guidance
5. Agent responses are posted back to your Teams thread

### What `--setup-teams` Provisions

| Resource | SKU | Cost |
|----------|-----|------|
| Azure Bot | F0 (free) | Free |
| App Service (relay) | B1 Linux | ~$13/mo |
| User-Assigned MSI | — | Free |

### Configuration

Config is saved to `~/.copilot/mandali-teams.json`:

```json
{
  "relay_url": "wss://mandali-relay-XXXXXX.azurewebsites.net/ws",
  "api_key": "auto-generated-key",
  "bot_name": "mandali-bot-XXXXXX",
  "app_name": "mandali-relay-XXXXXX",
  "resource_group": "mandali-relay-rg"
}
```

### Requirements

- Azure CLI (`az`) authenticated with an active subscription
- Permission to create Azure Bot and App Service resources

---

## Prior Art Acknowledgment

This system implements the **multi-agent collaborative development pattern**, which is an established approach in AI research. Key prior art includes:

| Framework | Year | Contribution |
|-----------|------|--------------|
| **Society of Mind** (Minsky) | 1986 | Theoretical foundation - intelligence emerges from agent interaction |
| **CAMEL** | 2023 | Debate-based agents that challenge each other |
| **ChatDev** | 2023 | Role-based software development with "communicative dehallucination" |
| **MetaGPT** | 2023 | SOP-driven pipeline with structured document handoffs |
| **AutoGen** | 2023 | Async multi-agent with SocietyOfMindAgent consensus |
| **CrewAI** | 2023 | Role/task/crew abstractions for workflow orchestration |

We do not claim to have invented multi-agent collaborative development.

---

## What This Implementation Adds

Our contribution is **practical refinements for production/unsupervised use**:

### 1. Autonomous Self-Organization
- Agents communicate via @mentions in shared conversation file
- No turn-taking orchestration - agents decide when to speak
- Natural collaboration with "When to Respond" / "When to Stay Quiet" rules

### 2. Design Discussion Phase
- @PM leads design discussion before implementation
- @Security raises all concerns at design time (not during implementation)
- Team agrees on phase structure, then executes

### 3. TDD + PoC Phased Development
- **Phase 1**: Minimal working version (happy path + tests FIRST)
- **Phase 2**: Error handling + tests
- **Phase 3**: Edge cases + tests
- **Phase 4**: Polish, security hardening, optimization

### 4. Explicit Conflict Resolution
- **Tie-breaker rules**: Security wins security disputes; PM wins other conflicts
- **2-strike rule**: After raising same concern twice, agent MUST propose solution
- **Self-unblocking**: Agents resolve blockers themselves (e.g., Security writes the test if Dev won't) rather than staying BLOCKED indefinitely
- **DecisionsTracker.md**: Deviation log — records where implementation differs from the plan, so a human can review and decide whether to keep or discard the work
- **Pre-action conversation check**: Every agent checks the last 50 lines of conversation before their key action (committing, reviewing, declaring complete)

### 5. Human Escalation
- Stall detection triggers human escalation
- Agents pause gracefully, await guidance
- Human input relayed as `@HUMAN` message

### 6. Trust but Verify — Post-Implementation Verification Loop
- After all agents declare `SATISFIED`, a **verification agent** compares plan vs actual implementation
- The verification agent reads actual code — it does not blindly trust status claims
- **DecisionsTracker.md entries are treated as intentional** — not flagged as gaps
- Verification is **pragmatic, not strict** — alternative implementations that achieve the same goal are fine
- If gaps are found: `conversation.txt` is archived (timestamped), `satisfaction.txt` is reset, and the team is **relaunched** with a gap report
- Up to `--max-retries` rounds (default 5); set to `0` to disable verification (old behavior)
- Each round gets a fresh conversation to keep agent context windows clean

---

## Personas

| Role | Focus | Tie-Breaker Authority |
|------|-------|----------------------|
| **Dev** | Implementation, TDD, code quality | - |
| **Security** | Threat modeling, secure defaults | TIER 1 (wins security disputes) |
| **PM** | Acceptance criteria, user delight | TIER 2 (wins UX/scope disputes) |
| **QA** | Testing, user journey, edge cases | - |
| **SRE** | Observability, reliability, failure modes | - |

---

## Files

```
mandali/                           # Mandali source
├── mandali.py                     # Autonomous orchestrator
├── teams_bridge.py                # Teams WebSocket client
├── config.yaml                    # Personas, model, settings
├── PRIOR_ART.md                   # Framework comparison
├── DecisionsTracker.md            # Deviation log template
├── personas/
│   ├── dev.persona.md
│   ├── security.persona.md
│   ├── pm.persona.md
│   ├── qa.persona.md
│   └── sre.persona.md
├── relay/                         # Cloud relay (deployed to Azure)
│   ├── app.py                     # FastAPI endpoints
│   ├── bot_handler.py             # Bot Framework adapter
│   ├── ws_manager.py              # WebSocket connection manager
│   ├── config.py                  # Environment config
│   ├── utils.py                   # Message sanitization
│   ├── requirements.txt           # Relay dependencies
│   └── startup.sh                 # Gunicorn startup
└── teams-app/                     # Teams app manifest template

# When --out-path is inside a git repo (worktree isolation):
myproject/                         # Your repo (UNTOUCHED)
myproject-mandali-<timestamp>/     # Worktree (agents work here)
├── {feature files created by agents}
├── phases/                        # Phased plan files (copied or generated)
│   ├── _CONTEXT.md
│   ├── _INDEX.md
│   └── phase-*.md
└── mandali-artifacts/             # Orchestration files
    ├── conversation.txt
    ├── conversation-round-*.txt
    ├── satisfaction.txt
    ├── DecisionsTracker.md
    ├── plan.md
    └── metrics.json

# When --out-path is NOT inside a git repo (no isolation):
<out-path>/                        # Agents work directly here
├── {same layout as above}
```

---

## Prior Art

This system builds on ideas from several multi-agent frameworks:

| Framework | Key Influence |
|-----------|---------------|
| **Ralph Wiggum** | Iterative feedback loop pattern |
| **Gas Town** | File-based persistence, CLI integration |
| **AutoGen** | Multi-agent conversation patterns |
| **CrewAI** | Role-based specialization |
| **MetaGPT/ChatDev** | Software team role modeling |

See **[PRIOR_ART.md](./PRIOR_ART.md)** for detailed comparison, limitations of existing frameworks, and how our approach addresses them.

---

## What's Next

### 🔮 Emulate Me Mode

What if the agents didn't just play generic roles — but played *you*?

Mandali can already orchestrate five specialists. The next step: teach them how *you* think. Your code review instincts. Your bias toward simplicity or thoroughness. The concerns you always raise. The ones you never do.

One command. Autonomous agents. Your voice!

*Coming soon: `--as-me`*

---

## License

MIT (or as specified by repository)

---

## References

- [Ralph Wiggum](https://ralph-wiggum.ai/) - Iterative agentic coding loop
- [Gas Town](https://github.com/steveyegge/gastown) - Multi-agent workspace manager
- [AutoGen](https://github.com/microsoft/autogen) - Microsoft multi-agent framework
- [CrewAI](https://github.com/crewAIInc/crewAI) - Role-playing AI agents
- [MetaGPT](https://github.com/geekan/MetaGPT) - Multi-agent meta programming
- [ChatDev](https://github.com/OpenBMB/ChatDev) - AI software company simulation
- [Society of Mind](https://en.wikipedia.org/wiki/Society_of_Mind) - Marvin Minsky
