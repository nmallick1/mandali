# Mandali

> *Mandali (मंडली, pronounced "mun-da-lee") — Sanskrit: a circle of specialists that deliberates and acts together*

Autonomous multi-agent system that assembles the right team for any task — then makes them argue about it until the work is actually good. Built on the GitHub Copilot SDK.

---

## How It Works

You describe what you want. Mandali figures out the rest.

1. **Classifies the task** — code, research, analysis, writing, or a mix
2. **Assembles a team** — hand-tuned code specialists for software, generated domain experts for everything else
3. **Agents discuss the plan** before executing — catching misalignment early, not after hours of work
4. **Agents execute autonomously** — coordinating through @mentions, using real tools (not simulations)
5. **Verifies the result** — a separate verification agent compares plan vs actual output before declaring done
6. **You can interject at any time** — but you don't have to. The team works without supervision

---

## Prerequisites

| Requirement | How to install |
|-------------|---------------|
| **Python 3.10+** | [python.org](https://www.python.org/downloads/) |
| **Node.js 18+** | [nodejs.org](https://nodejs.org/) (required for Copilot CLI) |
| **GitHub Copilot CLI** | `winget install GitHub.Copilot` or `npm install -g @github/copilot` |
| **GitHub Copilot license** | Active Copilot Individual, Business, or Enterprise subscription |

```bash
python --version      # 3.10 or higher
copilot --version     # Should print the CLI version
```

> **Tip:** If the CLI is installed but not in your PATH, set the `COPILOT_CLI_PATH` environment variable to point directly at the binary.

---

## Quick Start

```bash
# Install
pip install git+https://github.com/nmallick1/mandali.git

# Launch with an existing plan
mandali --plan phases/_INDEX.md --out-path ./output

# Or generate a plan from scratch
mandali --prompt "Analyze the competitive landscape for AI code review tools" --generate-plan --out-path ./output
```

Mandali checks for updates on each launch and notifies you when a newer version is available.

---

## CLI Reference

| Argument | Required | Description |
|----------|----------|-------------|
| `--out-path <path>` | **Yes** | Output directory. If inside a git repo, a worktree is created for isolation |
| `--plan <path>` | One of plan/prompt | Path to existing plan (`_INDEX.md` or single `plan.md`) |
| `--prompt <text>` | One of plan/prompt | Task description or prompt referencing plan files |
| `--generate-plan` | No | Run interview → plan generation → review before launching agents |
| `--stall-timeout <min>` | No (default: 5) | Minutes of inactivity before human escalation |
| `--max-retries <n>` | No (default: 5) | Verification rounds after agents complete. Set 0 to disable |
| `--verbose` | No | Show detailed status updates |
| `--quiet` | No | Suppress non-essential output. Shows interview, escalations, victory, and a status heartbeat every 5 min. Type `status` during monitoring for on-demand progress |
| `--debug` | No | Log all LLM requests/responses for diagnostics |
| `--static-personas` | No | Force the static code team, skip task classification |
| `--domains <list>` | No | Comma-separated domain list (e.g., `analytics,writing`). Overrides classifier |
| `--describe <persona>` | No | Show detailed description of a persona |
| `--teams` | No | Enable Teams integration for notifications and remote replies |
| `--setup-teams` | No | One-time setup: provision Azure Bot + cloud relay for Teams |

---

## Modes

**Direct launch** (default) — You already have plan files. Mandali discovers referenced artifacts, copies them to the workspace, and launches agents.

```bash
mandali --plan ./phases/_INDEX.md --out-path ./output
mandali --prompt "Read phases/_CONTEXT.md and phases/_INDEX.md. Start from Phase 3." --out-path ./output
```

**Plan generation** (`--generate-plan`) — Mandali interviews you, generates a phased plan, lets you review and edit it, then launches agents.

```bash
mandali --prompt "Add rate limiting to the API" --generate-plan --out-path ./output
```

---

## Personas

Mandali reads the task and assembles a team to match.

**Code tasks** get the hand-tuned code team — Dev, Security, PM, QA, SRE — specialists whose behavioral contracts have been refined through iteration. Each has tie-breaker authority in their domain (Security wins security disputes, PM wins scope disputes).

**Non-code and mixed tasks** — research, analysis, writing, or anything spanning code and other domains — get a team of generated specialists. Each domain gets adversarial coverage: a Doer to produce the work, a Critic to challenge it, and a Scope-keeper when the task crosses multiple domains.

Generated personas carry the same behavioral depth as the static team: engagement rules, conflict resolution, self-unblocking protocols. The collaboration model is the same regardless of team composition.

**Per-persona models** — Each persona can run on a different model. Set the `model` key per persona in `config.yaml` to route cost-sensitive roles to lighter models while keeping critical roles on the strongest available. Personas without a `model` key fall back to the orchestrator default.

```bash
# Force the static code team regardless of task type
mandali --prompt "..." --generate-plan --out-path ./output --static-personas

# Override domain detection
mandali --prompt "..." --generate-plan --out-path ./output --domains analytics,writing
```

---

## Quality & Oversight

**Verification before completion** — When all agents declare their work done, a separate verification agent independently reviews the output against the plan. If gaps are found, agents are relaunched with a gap report. This repeats for up to `--max-retries` rounds (default 5), ensuring the final output actually matches what was asked for.

**Human interjection** — You can type a message at any time during execution, and it's relayed to all agents as guidance. This is entirely optional — agents work autonomously and only escalate to you if they stall or explicitly need a decision. You're in control without being required to be present.

**Deviation tracking** — Agents record every departure from the plan in `DecisionsTracker.md`, so you can diff "what I asked for" vs "what I got" after the run.

---

## Workspace Layout

```
<out-path>/
├── {deliverable files}            # Agent output goes here
├── phases/                        # Plan files
│   ├── _CONTEXT.md                # Global context (read first by all agents)
│   ├── _INDEX.md                  # Phase tracking with status
│   └── phase-*.md                 # Per-phase tasks and quality gates
└── mandali-artifacts/             # Internal orchestration (auto-created)
    ├── conversation.txt           # Agent communication log
    ├── DecisionsTracker.md        # Deviation log
    ├── dynamic-personas/          # Generated persona files (non-code tasks)
    └── ...
```

When `--out-path` is inside a git repo, Mandali creates a **worktree** so agents never touch your working directory.

---

## MCP Servers & Extensions

Mandali loads MCP server configuration from `~/.copilot/mcp-config.json` (or `.copilot/mcp-config.json` in the project). All configured servers — databases, browsers, APIs, specialized tools — are available to every agent. User-installed Copilot skills and extensions are passed through automatically.

---

## Teams Integration

Monitor agent progress and provide guidance from Microsoft Teams — no terminal required.

```bash
# 1. Provision Azure resources (~3 minutes)
mandali --setup-teams

# 2. Upload the generated mandali-bot.zip to Teams Admin Center

# 3. Run with Teams enabled
mandali --plan phases/_INDEX.md --out-path ./output --teams
```

**How it works:** You message the Mandali bot in Teams → Azure Bot Service → cloud relay (App Service) → WebSocket → your running Mandali instance. Your message is injected into `conversation.txt` as `@HUMAN` guidance. Agent responses are posted back to your Teams thread.

| Resource | SKU | Cost |
|----------|-----|------|
| Azure Bot | F0 | Free |
| App Service (relay) | B1 Linux | ~$13/mo |
| User-Assigned MSI | — | Free |

Config is saved to `~/.copilot/mandali-teams.json`. Requires Azure CLI (`az`) with an active subscription.

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

See **[PRIOR_ART.md](./PRIOR_ART.md)** for detailed comparison and how Mandali's approach addresses known limitations.

---

## What's Next

### 🔮 Emulate Me Mode

What if the agents didn't just play domain roles — but played *you*?

Mandali already assembles teams that adapt to the task. The next step: teach them how *you* think. Your code review instincts. Your bias toward simplicity or thoroughness. The concerns you always raise. The ones you never do.

One command. Autonomous agents. Your voice!

*Coming soon: `--as-me`*

---

## License

MIT

---

## References

- [Ralph Wiggum](https://ralph-wiggum.ai/) - Iterative agentic coding loop
- [Gas Town](https://github.com/steveyegge/gastown) - Multi-agent workspace manager
- [AutoGen](https://github.com/microsoft/autogen) - Microsoft multi-agent framework
- [CrewAI](https://github.com/crewAIInc/crewAI) - Role-playing AI agents
- [MetaGPT](https://github.com/geekan/MetaGPT) - Multi-agent meta programming
- [ChatDev](https://github.com/OpenBMB/ChatDev) - AI software company simulation
- [Society of Mind](https://en.wikipedia.org/wiki/Society_of_Mind) - Marvin Minsky
