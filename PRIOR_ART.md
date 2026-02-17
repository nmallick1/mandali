# Prior Art & Framework Comparison

This document acknowledges the prior art that influenced Mandali and explains how its approach addresses known limitations in existing multi-agent frameworks.

---

## Mandali's Approach

Mandali is an autonomous multi-agent system built around one core idea: **the people who build something should not be the same people who judge it.** Every domain in a task gets both a builder and a challenger. Quality comes from structured disagreement, not from asking one agent to both produce and evaluate its own work.

The team itself isn't fixed. Mandali reads the task, identifies which domains need expertise, and assembles a team to match. A code task gets the hand-tuned static team (Dev, Security, PM, QA, SRE). A data analysis task gets domain analysts, methodology reviewers, and a cross-domain coordinator. A mixed task gets both. The adversarial structure — builders paired with challengers — applies regardless of what the task is.

### Architecture

| Layer | Description |
|-------|-------------|
| **Team Assembly** | Static code team (5 hand-tuned personas) or dynamically generated experts, assembled per-task based on domain classification |
| **Adversarial Roles** | Doer (builder), Critic (challenger), Scope-keeper (cross-domain coherence) — every domain gets at least the first two |
| **Passive Orchestrator** | Monitors conversation, handles timing/nudges, bridges human input — does not direct work |
| **Phased Plan Structure** | `_CONTEXT.md` (global context), `_INDEX.md` (tracking), `phase-*.md` (per-phase tasks) |
| **Hybrid Conversation** | Orchestrator writes on behalf of agents (prevents race conditions), agents read directly via tools (true autonomy) |
| **File-Based State** | `conversation.txt`, `satisfaction.txt`, `DecisionsTracker.md` — all state persisted, agents can crash and resume |
| **Real Tooling** | Agents use Copilot CLI with actual dev tools, MCP servers, and user-installed skills |
| **Artifact Discovery** | LLM recursively discovers plan files from prompt/plan references (5 levels deep) |

### Workflow
```
Default (direct launch):
  --plan or --prompt → LLM extracts file paths → Recursive artifact discovery (5 levels)
  → Copy to workspace → User confirms → Launch team

Opt-in (--generate-plan):
  AI interviews human → Classifies task → Assembles team → Generates phased plan
  → User approves → Launch team

Phase 0A: Context Building (agents explore codebase and materials)
Phase 0B: Design Discussion (adversarial review — team agrees on approach before execution)
Phase 1+: Execution with iterative validation per phase
```

### Design Principles

1. **Adversarial quality** — builders and challengers, not self-review. Quality emerges from tension, not trust.
2. **Adaptive composition** — the team matches the task. Static personas for code. Generated specialists for everything else. Same behavioral contract either way.
3. **Consensus before execution** — mandatory design discussion where all agents agree on approach. The #1 cause of rework is misalignment; Phase 0B eliminates it upfront.
4. **Real tooling** — agents actually edit files, run tests, read code, query databases. No simulated actions.
5. **Passive coordination** — the orchestrator monitors and facilitates. It doesn't direct work. Agents self-organize via @mentions.
6. **File-based recovery** — all state lives in files. Agents can crash, restart, and resume from where they left off.
7. **Escalation discipline** — three automated nudges before human involvement. Reduces interruptions without allowing indefinite stalls.

---

## Prior Art Frameworks

### 1. Ralph Wiggum Loop

**Source:** [ralph-wiggum.ai](https://ralph-wiggum.ai/), [beuke.org](https://beuke.org/ralph-wiggum-loop/)

**What It Is:**
Named after the Simpsons character, Ralph Wiggum is an iterative autonomous coding loop pattern. A persistent loop gives an AI agent repeated attempts at a task, feeding real feedback (test results, linter output, errors) after each attempt until success or budget exhaustion.

**Strengths:**
- Dramatically reduces human-in-the-loop bottlenecks
- Self-correcting through real feedback integration
- Context isolation prevents error compounding
- Works with any LLM-based coding agent

**Limitations:**
| Issue | Description |
|-------|-------------|
| Single-agent focus | Original pattern is one agent retrying; no built-in collaboration |
| Requires clear specs | Struggles with ambiguous or nuanced requirements |
| No domain specialization | Same agent handles security, testing, architecture |
| Iteration overhead | May generate excessive attempts for poorly specified tasks |

**How Mandali Differs:**
- Multiple specialized agents instead of one agent retrying — security issues caught by security experts, not by the same agent that wrote the code
- Critics challenge Doers in real time, catching blind spots that self-correction loops miss
- Phase 0B design discussion resolves ambiguity before execution starts
- Satisfaction-based termination instead of iteration limits

---

### 2. Gas Town

**Source:** [GitHub - steveyegge/gastown](https://github.com/steveyegge/gastown), [docs.gastownhall.ai](https://docs.gastownhall.ai/)

**What It Is:**
Created by Steve Yegge, Gas Town is a sophisticated multi-agent workspace manager designed to orchestrate 20-30+ AI coding agents. It uses a "Mayor" agent as central coordinator, spawning specialized "Polecats" (workers) organized into "Crews" working on "Rigs" (project containers). Work is tracked via atomic "Beads" (task units).

**Strengths:**
- Scales to dozens of concurrent agents
- Git-backed persistence for state and handoffs
- Strong provenance and audit trail
- Integrates well with tmux/CLI workflows
- Kubernetes-like philosophy for AI agents

**Limitations:**
| Issue | Description |
|-------|-------------|
| Resource intensive | Running 20+ Claude agents is expensive |
| Complexity overhead | Steep learning curve; overkill for smaller projects |
| Centralized Mayor | Single point of coordination can become bottleneck |
| Infrastructure requirements | Requires careful setup of git worktrees, tmux sessions |

**How Mandali Differs:**
- Team scales to the task (5 to 11 agents) instead of defaulting to 20+
- Passive orchestrator monitors — doesn't direct work like Gas Town's Mayor
- Emergent coordination via @mentions rather than top-down task assignment
- Simple file-based state instead of complex Beads/Rigs/Crews hierarchy
- Single Python script, no infrastructure setup required

---

### 3. Microsoft AutoGen

**Source:** [Microsoft Research](https://github.com/microsoft/autogen), [Microsoft Agent Framework](https://devblogs.microsoft.com/foundry/introducing-microsoft-agent-framework-the-open-source-engine-for-agentic-ai-apps/)

**What It Is:**
AutoGen is Microsoft's multi-agent conversation framework enabling AI agents to work together through structured dialogue patterns. Now in maintenance mode, succeeded by Microsoft Agent Framework.

**Strengths:**
- Research-backed multi-agent conversation patterns
- GroupChat for multi-agent dialogue
- Docker-based code execution sandboxing
- Model-agnostic design

**Limitations:**
| Issue | Description |
|-------|-------------|
| Maintenance mode | No new features; Microsoft recommends migration to Agent Framework |
| Brittle coordination | Complex handoffs and error recovery can break in deep workflows |
| Limited observability | Requires custom logging; no built-in production monitoring |
| Security gaps | Basic sandboxing; lacks enterprise governance features |
| Programmatic patterns | Conversation flows are code-defined, not emergent |

**How Mandali Differs:**
- File-based conversation — natural async dialogue, not programmatic patterns
- Agents decide when to speak based on @mentions and domain relevance
- `conversation.txt` is a complete human-readable audit log
- Dedicated security agent with design-time veto power
- Uses Copilot CLI, not tied to Azure services

---

### 4. CrewAI

**Source:** [GitHub - crewAIInc/crewAI](https://github.com/crewAIInc/crewAI)

**What It Is:**
CrewAI is an open-source framework for orchestrating role-playing AI agents. Teams of specialized agents collaborate on tasks with built-in abstractions for roles, tasks, and handoffs.

**Strengths:**
- Role-based specialization
- Clean task delegation abstractions
- Strong open-source community
- Both high-level and low-level APIs

**Limitations:**
| Issue | Description |
|-------|-------------|
| Complexity overhead | Overkill for simpler workflows; steep learning curve |
| Static memory | Agent memory doesn't persist or evolve across sessions |
| Non-determinism | LLM outputs not reliably repeatable; compliance concerns |
| Configuration challenges | Tuning agent behaviors for dynamic tasks is difficult |
| Production gaps | Requires additional tooling for enterprise integration |

**How Mandali Differs:**
- Team assembled to match the task, not pre-configured for every scenario
- Adversarial pairs per domain prevent the groupthink that single-role teams are prone to
- File-based persistence — conversation and decisions survive restarts
- Phase 0 consensus reduces non-determinism via upfront agreement
- Agents use actual Copilot CLI with real tools, not simulated actions

---

### 5. MetaGPT / ChatDev

**Source:** [MetaGPT](https://github.com/geekan/MetaGPT), [ChatDev](https://github.com/OpenBMB/ChatDev)

**What They Are:**
Software company simulations where AI agents play roles (CEO, PM, Architect, Engineer, QA). MetaGPT uses Standard Operating Procedures (SOPs); ChatDev uses synchronous chat-chains.

**Strengths:**
- Complete software development lifecycle simulation
- Role-based task decomposition
- Structured output artifacts (PRDs, designs, code)

**Limitations:**
| Issue | Description |
|-------|-------------|
| Waterfall SOP | Sequential phases; limited collaboration during execution |
| Simulated actions | Agents don't use real dev tools; generate code artifacts |
| Synchronous only | Chat-chain model; no async collaboration |
| No real testing | Tests are generated but not executed against real codebase |

**How Mandali Differs:**
- Agents discuss and iterate during execution, not just at handoff points
- Real tool execution — agents run tests, read code, make edits
- File-based model allows parallel work across agents
- Agents work on the actual repository, not simulated environments

---

## Comparison Matrix

| Feature | Ralph Wiggum | Gas Town | AutoGen | CrewAI | MetaGPT | **Mandali** |
|---------|-------------|----------|---------|--------|---------|----------|
| Multi-agent | ❌ Single | ✅ 20-30+ | ✅ GroupChat | ✅ Crews | ✅ Roles | ✅ Adaptive (5-11) |
| Adversarial roles | ❌ | ❌ | ❌ | ❌ | ❌ | ✅ Doer/Critic/Scope-keeper |
| Task-adaptive team | ❌ | ❌ | ❌ | ❌ | ❌ | ✅ Dynamic generation |
| Design consensus | ❌ | ❌ | ❌ | ❌ | ❌ | ✅ Phase 0B |
| Real tooling | ✅ | ✅ | ⚠️ Sandboxed | ❌ Simulated | ❌ Simulated | ✅ Copilot CLI + MCP + Skills |
| Async collaboration | ❌ | ✅ | ⚠️ Limited | ❌ | ❌ | ✅ File-based |
| State persistence | ❌ Fresh context | ✅ Git-backed | ❌ Memory only | ⚠️ Session | ❌ | ✅ File-based |
| Phased plans | ❌ | ❌ | ❌ | ❌ | ❌ | ✅ _CONTEXT/_INDEX/phases |
| Artifact discovery | ❌ | ❌ | ❌ | ❌ | ❌ | ✅ LLM-driven (5 levels) |
| MCP server support | ❌ | ❌ | ❌ | ❌ | ❌ | ✅ Auto-loaded |
| Git milestones | ❌ | ✅ | ❌ | ❌ | ❌ | ✅ Commit on pass |
| Security focus | ❌ | ❌ | ⚠️ Basic | ❌ | ❌ | ✅ Dedicated agent + veto |
| Verification loop | ❌ | ❌ | ❌ | ❌ | ❌ | ✅ Trust but verify |
| Human escalation | Manual | Manual | Manual | Manual | Manual | ✅ Nudge → Escalate |
| Complexity | Low | High | Medium | Medium | Medium | Low-Medium |
| Resource cost | Low | High | Medium | Medium | Medium | Low-Medium |

---

## What Sets Mandali Apart

### Team Model

**Adversarial by default.** Most multi-agent systems assign roles that cooperate: one agent writes code, another tests it. Mandali pairs each domain with both a builder and a challenger. The builder proposes and executes. The challenger reviews methodology, rigor, and assumptions — not as an afterthought quality gate, but as a continuous participant in the conversation. A Scope-keeper prevents domains from drifting when work spans multiple areas.

**Adaptive composition.** The static code team (Dev, Security, PM, QA, SRE) is the default for software tasks. For non-code work — data analysis, writing, research — Mandali generates domain specialists that carry the same behavioral depth as the hand-tuned team: engagement rules, satisfaction criteria, conflict resolution, self-unblocking protocols. Mixed tasks get both. The behavioral contract is the same; only the expertise changes.

### Collaboration Model

**Consensus before execution.** Phase 0A builds understanding (agents explore codebase, read materials). Phase 0B is a mandatory design discussion where the team — including critics — must agree on approach before work begins. This catches misalignment at the cheapest possible point.

**Self-organizing communication.** Agents coordinate through @mentions in a shared conversation file. No central controller assigns turns. Each agent has engagement rules that define when to speak and when to stay quiet, based on domain relevance — not a programmed sequence.

**Conflict resolution with teeth.** Tie-breaker authority is explicit: Security wins security disputes, PM wins scope disputes. The 2-strike rule prevents stalemates: after raising the same concern twice without resolution, an agent must propose a concrete solution or yield and record the disagreement. Progress always moves forward.

### Quality Model

**Trust but verify.** After all agents declare SATISFIED, a verification agent compares plan vs actual implementation by reading the code. DecisionsTracker entries are treated as intentional deviations, not gaps. If real gaps are found, the conversation is archived and the team is relaunched with a gap report. Up to N rounds.

**Pre-commit code review.** The Dev persona launches an independent code-review agent before every commit. Self-review rarely catches what a fresh context does.

**Deviation tracking.** `DecisionsTracker.md` records where implementation differs from the plan — not as a bug tracker, but as a human-readable diff between "what I asked for" and "what I got." A human reads it to decide what to keep.

### Tooling Model

**Real tools, real results.** Agents use Copilot CLI with actual developer tools, MCP servers, and user-installed skills. They edit files, run tests, read code, query databases, browse web pages. No simulations.

**Artifact discovery.** Default mode skips planning entirely. The LLM reads your prompt and plan files, recursively discovers all referenced artifacts (5 levels deep), copies them to workspace, and launches the team. Planning is opt-in, not the default.

**Phased plan structure.** Large plans split into `_CONTEXT.md` (global constraints), `_INDEX.md` (progress tracking), and individual `phase-*.md` files. Prevents the context loss that happens when agents work from a single large document.

**Git isolation.** When the output path is inside a git repo, Mandali creates a worktree automatically. Agents work in isolation; the original directory is never touched.

---

## Acknowledgments

This system builds on ideas from:

- **Ralph Wiggum** — The iterative feedback loop pattern
- **Gas Town** — File-based persistence and CLI integration philosophy
- **AutoGen** — Multi-agent conversation patterns and GroupChat concepts
- **CrewAI** — Role-based specialization and task delegation
- **MetaGPT/ChatDev** — Software team role modeling

Mandali gratefully acknowledges these projects and their communities for advancing the field of multi-agent AI collaboration.

---

## References

- Ralph Wiggum: https://ralph-wiggum.ai/, https://beuke.org/ralph-wiggum-loop/
- Gas Town: https://github.com/steveyegge/gastown, https://docs.gastownhall.ai/
- AutoGen: https://github.com/microsoft/autogen
- Microsoft Agent Framework: https://devblogs.microsoft.com/foundry/introducing-microsoft-agent-framework-the-open-source-engine-for-agentic-ai-apps/
- CrewAI: https://github.com/crewAIInc/crewAI
- MetaGPT: https://github.com/geekan/MetaGPT
- ChatDev: https://github.com/OpenBMB/ChatDev
