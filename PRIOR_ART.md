# Prior Art & Framework Comparison

This document acknowledges the prior art that influenced our autonomous multi-agent collaboration system and explains how our approach addresses known limitations in existing frameworks.

---

## Our Approach: Autonomous Multi-Agent Collaboration

### Architecture Summary

| Component | Description |
|-----------|-------------|
| **5 Specialized Personas** | Dev, Security, PM, QA, SRE - domain experts with veto power |
| **Passive Orchestrator** | Monitors conversation, handles timing/nudges, bridges human input |
| **LLM-Based Artifact Discovery** | Recursively discovers plan files from prompt/plan refs (5 levels deep) |
| **Phased Plan Structure** | `_CONTEXT.md`, `_INDEX.md`, `phase-*.md` files for context-preserving plans |
| **Hybrid Conversation Model** | Orchestrator writes (prevents race conditions), agents read directly via tools |
| **File-Based State** | `conversation.txt`, `satisfaction.txt`, `DecisionsTracker.md` (deviation log) |
| **Real Tool Integration** | Agents use Copilot CLI with actual dev tools + MCP servers |

### Workflow
```
Default (direct launch):
  --plan or --prompt → LLM extracts file paths → Recursive artifact discovery (5 levels)
  → Copy to workspace → User confirms → Launch agents

Opt-in (--generate-plan):
  AI interviews human → Generates phased plan → User approve → Review → Launch agents

Phase 0A: Context Building (agents explore codebase with background agents)
Phase 0B: Design Discussion (all agents agree on approach before implementation)
Phase 1+: TDD+PoC Implementation with iterative validation per phase
```

### Key Design Principles

1. **Consensus Before Code** - Mandatory design discussion phase where all personas agree
2. **Security-First** - Security agent has early veto power at design time
3. **Nudge Before Escalate** - 3 automated nudges before human involvement
4. **File-Based Recovery** - All state persisted, agents can restart/resume
5. **Real Tooling** - No simulated actions; agents actually write and test code

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

**How We Address This:**
- **Multi-agent collaboration** instead of single-agent retry loop
- **Phase 0B Design Discussion** resolves ambiguity before implementation
- **Specialized personas** (Security, QA, SRE) catch domain-specific issues
- **Satisfaction-based termination** instead of iteration limits

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

**How We Address This:**
- **5 personas** is sufficient for most software tasks without resource explosion
- **Passive orchestrator** - not a "Mayor" directing work, just monitoring/facilitating
- **Emergent coordination** via @mentions rather than top-down task assignment
- **Simple file-based state** instead of complex Beads/Rigs/Crews hierarchy
- **Lower barrier to entry** - single Python script, no infrastructure required

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

**How We Address This:**
- **File-based conversation** - natural async dialogue, not programmatic patterns
- **Agent autonomy** - agents decide when to speak based on @mentions and domain
- **Built-in observability** - `conversation.txt` is a complete audit log
- **Security persona** - dedicated agent for security review with veto power
- **No vendor lock-in** - uses Copilot CLI, not tied to Azure services

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

**How We Address This:**
- **Right-sized complexity** - 5 personas match typical software dev team roles
- **File-based persistence** - conversation and decisions survive restarts
- **Satisfaction tracking** - explicit state per agent provides auditability
- **Phase 0 consensus** - reduces non-determinism via upfront agreement
- **Real tool integration** - agents use actual Copilot CLI, not simulated actions

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

**How We Address This:**
- **Collaborative phases** - agents discuss and iterate, not waterfall handoffs
- **Real tool execution** - agents run tests, read code, make edits via Copilot CLI
- **Async communication** - file-based model allows parallel work
- **Live codebase integration** - agents work on actual repository, not simulations

---

## Comparison Matrix

| Feature | Ralph Wiggum | Gas Town | AutoGen | CrewAI | MetaGPT | **Ours** |
|---------|-------------|----------|---------|--------|---------|----------|
| Multi-agent | ❌ Single | ✅ 20-30+ | ✅ GroupChat | ✅ Crews | ✅ Roles | ✅ 5 Personas |
| Real tooling | ✅ | ✅ | ⚠️ Sandboxed | ❌ Simulated | ❌ Simulated | ✅ Copilot CLI + MCP + Skills |
| Async support | ❌ | ✅ | ⚠️ Limited | ❌ | ❌ | ✅ File-based |
| State persistence | ❌ Fresh context | ✅ Git-backed | ❌ Memory only | ⚠️ Session | ❌ | ✅ File-based |
| Phased plans | ❌ | ❌ | ❌ | ❌ | ❌ | ✅ _CONTEXT/_INDEX/phases |
| Artifact discovery | ❌ | ❌ | ❌ | ❌ | ❌ | ✅ LLM-driven (5 levels) |
| MCP server support | ❌ | ❌ | ❌ | ❌ | ❌ | ✅ Auto-loaded |
| Git milestones | ❌ | ✅ | ❌ | ❌ | ❌ | ✅ Commit on pass |
| Security focus | ❌ | ❌ | ⚠️ Basic | ❌ | ❌ | ✅ Dedicated agent |
| Design consensus | ❌ | ❌ | ❌ | ❌ | ❌ | ✅ Phase 0B |
| Human escalation | Manual | Manual | Manual | Manual | Manual | ✅ Nudge→Escalate |
| Complexity | Low | High | Medium | Medium | Medium | Low-Medium |
| Resource cost | Low | High | Medium | Medium | Medium | Low |

---

## What Makes Our Approach Unique

### 1. **LLM-Driven Artifact Discovery**
Default mode skips interview/planning entirely. LLM reads your prompt and plan files, recursively discovers all referenced artifacts (up to 5 levels deep), copies them to workspace, and launches agents. Planning is opt-in via `--generate-plan`, not the default path.

### 2. **Phased Plan Structure**
Plans split into `_CONTEXT.md` (global context), `_INDEX.md` (phase tracking), and individual `phase-*.md` files. Prevents context loss in large projects and enables agents to track progress per-phase.

### 3. **Hybrid Conversation Model**
Orchestrator writes on behalf of agents (solves concurrency), agents read directly via tools (true autonomy). No central controller directing work.

### 4. **Phase 0: Consensus Before Code**
- **Phase 0A**: All agents explore codebase with background agents to build understanding
- **Phase 0B**: Mandatory design discussion until all agents agree on approach

This addresses the #1 cause of rework in AI-generated code: misalignment between agents on approach.

### 5. **Security-First by Design**
Security agent participates in design discussion with veto power. Issues caught at Phase 0, not after code is written.

### 6. **MCP Server & Skills Integration**
Agents automatically receive MCP server configuration from `~/.copilot/mcp-config.json`, and the user's full Copilot config directory (`~/.copilot`) is passed to sessions so locally installed **skills and extensions** are available to all persona agents.

### 7. **Nudge Before Escalate**
Three automated nudges at stall intervals before human involvement. Reduces unnecessary interruptions while preventing indefinite stalls.

### 8. **File-Based Recovery**
All state in files: `conversation.txt`, `satisfaction.txt`, `DecisionsTracker.md` (deviation log). Agents can crash, restart, and resume.

### 9. **Git Commits at Milestones**
Agents are instructed to commit working code at major milestones:
- Only commit when build + tests pass
- Use structured commit format: `[Mandali] feat: Brief description`
- Include commit hash in response for traceability
- Satisfaction criteria includes "Working code committed to git"

This ensures recoverable progress even if agents stall or crash mid-feature.

### 10. **Right-Sized Collaboration**
5 personas match natural software team structure. Not overkill (Gas Town's 30 agents), not undersized (Ralph Wiggum's single agent).

### 11. **Real Tool Integration**
Agents use Copilot CLI with actual developer tools plus MCP servers and user skills. No simulation—they really edit files, run tests, read code, query databases, browse web pages.

### 12. **Trust but Verify — Verification Loop**
After all agents declare SATISFIED, a verification agent compares plan vs actual implementation by reading the code. DecisionsTracker entries are treated as intentional deviations. If gaps are found, the conversation is archived and agents are relaunched with a gap report — up to `--max-retries` rounds (default 5).

### 13. **Pre-Commit Code Review**
The Dev persona launches an independent code-review agent before every commit. Self-review rarely catches issues; a separate agent with fresh context is more thorough.

---

## Acknowledgments

This system builds on ideas from:

- **Ralph Wiggum** - The iterative feedback loop pattern
- **Gas Town** - File-based persistence and CLI integration philosophy
- **AutoGen** - Multi-agent conversation patterns and GroupChat concepts
- **CrewAI** - Role-based specialization and task delegation
- **MetaGPT/ChatDev** - Software team role modeling

We gratefully acknowledge these projects and their communities for advancing the field of multi-agent AI collaboration.

---

## References

- Ralph Wiggum: https://ralph-wiggum.ai/, https://beuke.org/ralph-wiggum-loop/
- Gas Town: https://github.com/steveyegge/gastown, https://docs.gastownhall.ai/
- AutoGen: https://github.com/microsoft/autogen
- Microsoft Agent Framework: https://devblogs.microsoft.com/foundry/introducing-microsoft-agent-framework-the-open-source-engine-for-agentic-ai-apps/
- CrewAI: https://github.com/crewAIInc/crewAI
- MetaGPT: https://github.com/geekan/MetaGPT
- ChatDev: https://github.com/OpenBMB/ChatDev
