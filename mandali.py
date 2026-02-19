#!/usr/bin/env python3
"""
Mandali (मंडली) — Autonomous Multi-Agent Orchestrator
======================================================
A circle of specialized AI agents that deliberate and act together.
Orchestrator is a passive monitor, not an active driver.

Usage:
  python mandali.py --plan path/to/plan.md --out-path ./output
  python mandali.py --prompt "Read phases/_CONTEXT.md and phases/_INDEX.md. Complete all phases." --out-path ./output
  python mandali.py --prompt "Build a feature" --generate-plan --out-path ./output

Requirements:
  pip install github-copilot-sdk pyyaml rich
"""

import argparse
import asyncio
import json
import os
import re
import shutil
import subprocess
import sys
import threading
import urllib.request
import yaml
from datetime import datetime
from pathlib import Path
from typing import Optional, Dict, Any
from dataclasses import dataclass, field

from rich.console import Console
from rich.panel import Panel
from rich.table import Table
from rich.prompt import Prompt, Confirm
from rich.markup import escape

console = Console()

# GitHub Copilot SDK
from copilot import CopilotClient

__version__ = "0.1.0"
try:
    from importlib.metadata import version as _pkg_version
    __version__ = _pkg_version("mandali")
except Exception:
    pass  # Not installed as package — use hardcoded fallback
GITHUB_REPO = "nmallick1/mandali"


def _check_for_updates():
    """Check GitHub for a newer version (runs in background thread, never blocks)."""
    try:
        url = f"https://raw.githubusercontent.com/{GITHUB_REPO}/main/pyproject.toml"
        req = urllib.request.Request(url, headers={"User-Agent": "mandali-update-check"})
        with urllib.request.urlopen(req, timeout=3) as resp:
            content = resp.read().decode("utf-8")
        
        match = re.search(r'^version\s*=\s*"([^"]+)"', content, re.MULTILINE)
        if not match:
            return
        
        remote_version = match.group(1)
        if remote_version != __version__:
            # Use print() — not console.print() — to avoid Rich thread-safety issues
            print(
                f"  Update available: {__version__} → {remote_version}. "
                f"Run: pip install --upgrade git+https://github.com/{GITHUB_REPO}.git"
            )
    except Exception:
        pass  # Network issues, rate limits — silently ignore


def check_for_updates_async():
    """Fire-and-forget update check in a daemon thread."""
    t = threading.Thread(target=_check_for_updates, daemon=True)
    t.start()


def get_copilot_cli_path() -> str:
    """
    Discover the copilot CLI path, handling Windows specifics.
    On Windows, we need to use the .cmd wrapper or the node loader directly.
    Exits with clear instructions if the CLI is not found.
    """
    # Check environment variable first
    if env_path := os.environ.get("COPILOT_CLI_PATH"):
        if os.path.isfile(env_path) or shutil.which(env_path):
            return env_path
        log(f"COPILOT_CLI_PATH is set to '{env_path}' but it was not found.", "ERROR")
        sys.exit(1)
    
    # Try to find copilot in PATH
    if sys.platform == "win32":
        copilot_cmd = shutil.which("copilot.cmd")
        if copilot_cmd:
            return copilot_cmd
        copilot_exe = shutil.which("copilot")
        if copilot_exe:
            return copilot_exe
    else:
        copilot_path = shutil.which("copilot")
        if copilot_path:
            return copilot_path
    
    # Not found — give clear instructions
    log("GitHub Copilot CLI not found in PATH.", "ERROR")
    console.print(Panel(
        "[bold]GitHub Copilot CLI is required but was not found.[/bold]\n\n"
        "Install it with:\n"
        "  [cyan]winget install GitHub.Copilot[/cyan]  (Windows)\n"
        "  [cyan]npm install -g @github/copilot[/cyan]  (any platform)\n\n"
        "Or set the path manually:\n"
        "  [cyan]export COPILOT_CLI_PATH=/path/to/copilot[/cyan]  (Linux/macOS)\n"
        "  [cyan]set COPILOT_CLI_PATH=C:\\path\\to\\copilot.cmd[/cyan]  (Windows)\n\n"
        "After installing, verify with:\n"
        "  [cyan]copilot --version[/cyan]\n\n"
        "For more info: [link]https://github.com/nmallick1/mandali#prerequisites[/link]",
        title="⚠️  Missing Prerequisite",
        border_style="yellow",
    ))
    sys.exit(1)


# ============================================================================
# Configuration
# ============================================================================

SCRIPT_DIR = Path(__file__).parent
CONFIG_FILE = SCRIPT_DIR / "config.yaml"
PERSONAS_DIR = SCRIPT_DIR / "personas"

STALL_TIMEOUT_SECONDS = 300  # 5 minutes without activity = stall
POLL_INTERVAL_SECONDS = 10  # Check status every 10 seconds

# Lock for serializing file writes to prevent race conditions
import threading
_satisfaction_lock = threading.Lock()

# Debug logging — enabled via --debug flag, writes JSONL to mandali-artifacts/debug.jsonl
_debug_enabled = False
_debug_file = None

def _debug_log(event: str, data: dict):
    """Write a debug event to the JSONL log file if debugging is enabled."""
    if not _debug_enabled or not _debug_file:
        return
    entry = {
        "timestamp": datetime.now().isoformat(),
        "event": event,
        **data,
    }
    try:
        with open(_debug_file, 'a', encoding='utf-8') as f:
            f.write(json.dumps(entry, default=str) + "\n")
    except Exception:
        pass  # Debug logging must never crash the app

# ============================================================================
# Dynamic Persona Constants
# ============================================================================

PERSONA_FRONTMATTER_KEYS = ['id', 'name', 'domain', 'role', 'mention']

# Universal behavioral skeleton for dynamically generated personas.
# Slots use {placeholder} syntax, filled by render_persona().
# Runtime tokens use {{TOKEN}} syntax, filled by the orchestrator at launch.
PERSONA_SKELETON_TEMPLATE = """---
id: {id}
name: {name}
domain: {domain}
role: {role}
mention: "{mention}"
---

# {name} - {role_name}

> {role_description}

## Team
{{TEAM_ROSTER}}

## Engagement
{engagement_rules}
- **Before responding**: check last {{CONVERSATION_CHECK_LINES}} lines of conversation for relevant context

## Key Files

You have access to ALL tools available in your environment — use whatever tools are needed to accomplish your work. Key files for this project:

- `_CONTEXT.md` — global context, architecture decisions, non-negotiables
- `_INDEX.md` — phase tracking, progress status
- `phase-*.md` — detailed tasks per phase
- `conversation.txt` — team communication
- `DecisionsTracker.md` — deviation log for human review

## Decision Tracking

Record deviations in `DecisionsTracker.md` (path in your initial prompt). This is a **deviation log for human review** — a human reads it to diff "what I asked for" vs "what I got." Record when:

{decision_tracking_triggers}

**Catch-all:** Record any choice a human comparing plan to implementation would be surprised by, including choices where the plan was silent. Read existing decisions first — don't re-litigate settled choices. Use the template format with `[HH:MM:SS]` timestamps.

---

## Phased Development Workflow

1. Read `_CONTEXT.md` first → `_INDEX.md` → current phase file
2. Complete each phase fully before moving to the next
3. Verify quality gates before declaring phase complete

### Phase 0A: Context Building
Before discussion, build complete understanding:
1. Read `_CONTEXT.md` first — understand the user's original ask, the problem being solved, and the big picture
2. Read the full plan and explore relevant materials — understand scope, constraints, existing work
3. Identify domain-specific concerns this plan may not have considered
4. Post: `@Team - I have reviewed the plan and materials. Ready for design discussion.`

Wait for ALL agents to confirm before design discussion begins.

### Phase 0B: Design Discussion
{phase_0b_actions}

---

## Domain Expertise

{domain_expertise}

## Non-Negotiables

{non_negotiables}

## Quality Definition

{quality_definition}

## Core Rules

{core_rules}

## Self-Unblocking (2-Strike Rule)

After raising a concern twice without resolution, you MUST either:
1. Propose a concrete resolution (a specific deliverable, fix, or alternative approach), or
2. Yield and record the disagreement in `DecisionsTracker.md`

No endless stalemates. The goal is progress, not being right.

## Domain Ownership & Conflict Resolution

- **Own**: {domain_ownership}
- **Defer to**: {defer_to}
- **Shared jurisdiction**: {shared_jurisdiction}
- **Conflict resolution**: {conflict_resolution_stance}

## Phase Responsibilities

{phase_responsibilities}

---

## Self-Validation

Verify your own work actually produces the expected result before declaring done. Don't rely solely on others to catch your mistakes. If your deliverable can be checked, check it.

## Incremental Review

Review after each phase, not just at the end. Problems found early are cheaper to fix. After each phase, verify that earlier deliverables still hold — new work can break prior work.

---

## Satisfaction Criteria

ALL must be true to declare SATISFIED:
{satisfaction_criteria}

**⚠️ Do NOT declare SATISFIED after one phase. Only when ALL phases are done or STOP directive reached.**

## Response Format
```
@Team - [Brief status]
PHASE: [current] | STATUS: [In Progress / Complete / Blocked]
{response_format_fields}
SATISFACTION_STATUS: WORKING | SATISFIED | BLOCKED - [reason] | PAUSED
```
"""

CLASSIFIER_PROMPT = """You are a task classifier. Respond with EXACTLY the format shown below. Nothing else.

## Decision Rule

Ask ONE question: **Does the task require producing or modifying software as a deliverable?**

- YES → software-development (building APIs, CLIs, libraries, infrastructure-as-code, scripts, configurations)
- NO → non-software (reports, analysis, writing, research, design, planning — even if the SUBJECT is about software)
- BOTH → mixed (software deliverables AND non-software deliverables in different domains)

The subject of the task does not determine the type. Only the deliverable does.
- "Analyze AI code review tools" → non-software (deliverable is a report ABOUT software, not software itself)
- "Build an AI code review tool" → software-development (deliverable IS software)
- "Build a dashboard AND write a market analysis" → mixed

## Domain Rules
- Use lowercase slugs (e.g., market-research, technical-writing, data-analysis)
- For pure software tasks: DOMAIN_1 is software-development
- Only list domains needing dedicated expertise. Skip generic skills.
- 1-3 domains max. Use NONE for unused slots.

## Output Format (copy this exactly, fill in values)

TASK_TYPE: <software-development or non-software or mixed>
DOMAIN_1: <primary-domain-slug>
DOMAIN_2: <supporting-domain-slug or NONE>
DOMAIN_3: <supporting-domain-slug or NONE>

IMPORTANT: TASK_TYPE must be one of exactly three values: software-development, non-software, mixed.
Respond with ONLY the 4 lines above. No explanation, no JSON, no markdown fences.
"""

PERSONA_GENERATOR_PROMPT = """
## Quality Bar (ALL personas MUST embody these traits)
1. High standards, zero ego — critique the work, not the person
2. Goes beyond the ask — anticipate issues, suggest improvements proactively
3. Domain depth — bring genuine specialist knowledge, not generic platitudes
4. Adversarial rigor — challenge assumptions, demand evidence, test boundaries
5. Opinionated but flexible — have strong defaults, yield to better arguments
6. Concrete over abstract — code samples, specific metrics, real examples over vague guidance
7. Self-aware scope — know what you own, what you don't, and say so explicitly
8. Progress-oriented — unblock yourself and others, never stall for perfection
9. Deliverable-focused — every action should advance a concrete output
10. Honest about uncertainty — say "I don't know" rather than fabricate domain expertise
11. Incremental verification — verify work at each step, don't batch-validate at the end
12. Collaborative by default — engage with teammates' work, build on each other's output
13. Domain-appropriate quality gates — enforce professional standards for your domain even when the user doesn't ask explicitly (e.g., code personas: performance, resource consumption, unhandled exceptions; analytical personas: validated facts, cited sources, reproducible methodology; writing personas: accuracy, logical structure, audience-appropriate tone)

## Role Types
- **Doer**: Primary implementer for the domain. Produces deliverables, proposes approaches, executes. May also review and challenge others' work that touches their domain.
- **Critic**: Quality challenger for the domain. Challenges methodology, verifies rigor, catches blind spots. NOT passive — actively contributes solutions, writes fixes, and demonstrates better approaches when critiquing. A Critic who only points out problems without helping resolve them is failing.
- **Scope-keeper**: Cross-domain awareness. Ensures domains don't drift, resolves boundary disputes, maintains coherent big picture.

Note: These roles define primary orientation, not rigid boundaries. A QA Doer writes tests AND critiques the dev's work from a quality perspective. A Security Critic spots vulnerabilities AND suggests fixes or writes patches. Every persona is expected to be hands-on in their domain.

## Persona File Structure
The .persona.md file MUST include:
- YAML frontmatter with: id, name, domain, role, mention
- Role name and description
- Engagement rules (when to speak, when to stay quiet)
- Decision tracking triggers
- Phase 0B actions (design discussion behavior — MUST include negotiating domain-specific quality gates beyond user's explicit requirements)
- Domain expertise (2-4 paragraphs of deep domain knowledge)
- Non-negotiables (absolute rules this persona enforces)
- Quality definition (what "good" means in this domain)
- Core operating principles (5-10 rules)
- Domain ownership, defer-to, and shared jurisdiction
- Conflict resolution stance
- Phase responsibilities
- Satisfaction criteria (checklist — ALL must be true to declare SATISFIED)
- Response format fields

## Important
- Generate personas with REAL domain depth — not generic "I review things" descriptions
- The persona must be useful for the SPECIFIC domain, not a generic template with domain name swapped in
- Engagement rules must be specific to the domain's concerns
- Satisfaction criteria must reflect domain-specific quality gates
- The id must be unique and descriptive (not "persona-1")
"""

DEDUP_AGENT_PROMPT = """You are a deduplication analyst for a multi-agent collaboration system.

Your job: Analyze a set of persona definitions for overlap. You receive full persona file contents (not summaries) because you must distinguish surface overlap from real functional overlap.

Two personas that both mention "data quality" may serve completely different functions:
- A "Data Quality Reviewer" focused on pipeline validation is different from
- A "Data Quality Reviewer" focused on statistical methodology review

Read the FULL definitions carefully before deciding.

Output ONLY valid JSON (no markdown fences) with this structure:
{
  "keep": [
    {"id": "<persona-id>", "reason": "<why this persona is unique and needed>"}
  ],
  "drop": [
    {"id": "<persona-id>", "reason": "<why this persona is redundant>", "covered_by": "<id of persona that covers this>"}
  ],
  "merge": [
    {
      "sources": ["<persona-id-1>", "<persona-id-2>"],
      "merged_name": "<suggested name for merged persona>",
      "reason": "<why these should be combined>",
      "merge_guidance": "<what to keep from each source>"
    }
  ]
}

Rules:
- Default to KEEP unless you find genuine functional overlap (not just label similarity)
- MERGE when two personas have significantly overlapping expertise and responsibilities, making them compete for the same work
- DROP when one persona is strictly a subset of another (the broader one covers everything the narrow one does)
- Static team members (Dev, PM, QA, Security, SRE) cannot be dropped or merged — only consider overlap between dynamic personas, and between dynamic and static
- When a dynamic persona overlaps with a static persona, DROP the dynamic one (static personas are hand-tuned and take priority)
- Be conservative: when in doubt, KEEP both. False dedup is worse than mild redundancy.
"""

# ============================================================================
# Data Classes
# ============================================================================

@dataclass
class PersonaAgent:
    """Represents an autonomous AI persona."""
    id: str
    name: str
    mention: str  # @Dev, @PM, etc.
    session: Any = None
    task: asyncio.Task = None  # Background task
    prompt_file: str = None  # Path to persona file (dynamic personas)
    dynamic: bool = False  # True for dynamically generated personas
    domain: str = None  # Domain (for dynamic personas)
    session_lock: asyncio.Lock = field(default_factory=asyncio.Lock)  # Serializes session access


@dataclass
class Workspace:
    """Shared workspace for agent communication."""
    path: Path  # Main output directory where agents create feature files
    artifacts_path: Path  # mandali-artifacts subfolder for orchestration files
    phases_path: Path  # phases subfolder for phased plan files
    conversation_file: Path
    satisfaction_file: Path
    decisions_file: Path
    plan_file: Path  # Legacy single-file plan OR _INDEX.md for phased plans
    context_file: Path  # _CONTEXT.md for phased plans
    index_file: Path  # _INDEX.md for phased plans
    metrics_file: Path
    
    @classmethod
    def create(cls, out_path: Path) -> 'Workspace':
        """Create a Workspace from an output path."""
        artifacts = out_path / "mandali-artifacts"
        phases = out_path / "phases"
        return cls(
            path=out_path,
            artifacts_path=artifacts,
            phases_path=phases,
            conversation_file=artifacts / "conversation.txt",
            satisfaction_file=artifacts / "satisfaction.txt",
            decisions_file=artifacts / "DecisionsTracker.md",
            plan_file=artifacts / "plan.md",  # Legacy fallback
            context_file=phases / "_CONTEXT.md",
            index_file=phases / "_INDEX.md",
            metrics_file=artifacts / "metrics.json"
        )
    
    def ensure_exists(self):
        self.path.mkdir(parents=True, exist_ok=True)
        self.artifacts_path.mkdir(parents=True, exist_ok=True)
        self.phases_path.mkdir(parents=True, exist_ok=True)
        self.conversation_file.touch()
        self.satisfaction_file.touch()
        # Copy DecisionsTracker template if it doesn't already exist
        if not self.decisions_file.exists():
            template = SCRIPT_DIR / "DecisionsTracker.md"
            if template.exists():
                shutil.copy2(template, self.decisions_file)
            else:
                self.decisions_file.touch()
    
    def is_phased_plan(self) -> bool:
        """Check if this workspace uses phased plan structure."""
        return self.index_file.exists() and self.context_file.exists()
    
    def get_plan_content(self) -> str:
        """Get plan content, preferring phased structure."""
        if self.is_phased_plan():
            # For phased plans, return _CONTEXT.md + _INDEX.md + all phase files
            content_parts = []
            
            # Read _CONTEXT.md first
            if self.context_file.exists():
                content_parts.append(f"# === _CONTEXT.md (READ FIRST) ===\n\n{self.context_file.read_text(encoding='utf-8')}")
            
            # Read _INDEX.md
            if self.index_file.exists():
                content_parts.append(f"\n\n# === _INDEX.md ===\n\n{self.index_file.read_text(encoding='utf-8')}")
            
            # Read all phase files
            phase_files = sorted(self.phases_path.glob("phase-*.md"))
            for pf in phase_files:
                content_parts.append(f"\n\n# === {pf.name} ===\n\n{pf.read_text(encoding='utf-8')}")
            
            return "\n".join(content_parts)
        elif self.plan_file.exists():
            # Fallback to single-file plan
            return self.plan_file.read_text(encoding='utf-8')
        else:
            return ""


@dataclass
class Metrics:
    """Collaboration metrics."""
    start_time: str = ""
    end_time: str = ""
    total_messages: int = 0
    human_escalations: int = 0
    nudges: int = 0  # Times orchestrator nudged inactive agents
    decisions_logged: int = 0
    victory: bool = False
    verification_rounds: int = 0
    verification_passed: bool = False
    per_agent: Dict[str, Dict] = field(default_factory=dict)


@dataclass
class TaskClassification:
    """Result of classifying a task into type and domains."""
    task_type: str  # "software-development" | "non-software" | "mixed"
    domains: list  # [{"name": "analytics", "role_in_task": "primary"}, ...]
    interview_summary: dict = field(default_factory=dict)


# ============================================================================
# Utilities
# ============================================================================

def log(msg: str, level: str = "INFO"):
    """Log with timestamp and styled output."""
    timestamp = datetime.now().strftime("%H:%M:%S")
    styles = {
        "INFO": ("ℹ️", "bright_blue"),
        "OK": ("✅", "green"),
        "WARN": ("⚠️", "yellow"),
        "ERR": ("❌", "red bold"),
        "AGENT": ("🤖", "cyan"),
        "HUMAN": ("👤", "magenta"),
    }
    symbol, style = styles.get(level, ("•", "white"))
    console.print(f"[dim]{timestamp}[/dim] {symbol} [{style}]{escape(msg)}[/{style}]")


def load_config() -> dict:
    with open(CONFIG_FILE, 'r', encoding='utf-8') as f:
        return yaml.safe_load(f)


def load_mcp_config() -> dict:
    """Load MCP server configuration from ~/.copilot/mcp-config.json.
    
    The Copilot SDK does NOT automatically inherit MCP config from the CLI.
    We must explicitly load and pass it to each session.
    """
    # Check multiple possible locations
    possible_paths = [
        Path.home() / ".copilot" / "mcp-config.json",  # User config (primary)
        Path.cwd() / ".copilot" / "mcp-config.json",   # Project config
    ]
    
    for config_path in possible_paths:
        if config_path.exists():
            try:
                with open(config_path, 'r', encoding='utf-8') as f:
                    config = json.load(f)
                    mcp_servers = config.get("mcpServers", {})
                    if mcp_servers:
                        log(f"Loaded MCP config from {config_path} ({len(mcp_servers)} servers)", "OK")
                        return mcp_servers
            except (json.JSONDecodeError, IOError) as e:
                log(f"Failed to load MCP config from {config_path}: {e}", "WARN")
    
    log("No MCP config found - agents will have limited tool access", "WARN")
    return {}


# Global MCP config (loaded once at startup)
MCP_SERVERS_CONFIG: dict = {}


def _build_session_config(model: str, system_message: str, working_directory: str = None) -> dict:
    """Build a session config with full tool access (MCP servers, skills, extensions).
    
    All sessions — persona agents and orchestrator housekeeping agents alike —
    get the same tool access. The system prompt controls behavior, not tool availability.
    """
    copilot_config_dir = Path.home() / ".copilot"
    config = {
        "model": model,
        "system_message": system_message,
    }
    if working_directory:
        config["working_directory"] = working_directory
    if copilot_config_dir.exists():
        config["config_dir"] = str(copilot_config_dir)
    if MCP_SERVERS_CONFIG:
        config["mcp_servers"] = MCP_SERVERS_CONFIG
    return config


def load_persona_prompt(persona_id: str, prompt_file: str = None,
                        team_roster: list = None, team_size: int = None) -> str:
    """Load a persona prompt file, optionally replacing runtime tokens.
    
    For static personas: loads from personas/ directory.
    For dynamic personas: loads from the specified prompt_file path, strips YAML frontmatter.
    Replaces {{TEAM_ROSTER}} and {{CONVERSATION_CHECK_LINES}} tokens if team info provided.
    """
    if prompt_file:
        filepath = Path(prompt_file)
    else:
        filepath = PERSONAS_DIR / f"{persona_id}.persona.md"
    
    content = filepath.read_text(encoding='utf-8')
    
    # Strip YAML frontmatter from dynamic personas
    if prompt_file and content.startswith('---'):
        content = strip_persona_frontmatter(content)
    
    # Replace runtime tokens if team info is available
    if team_roster is not None:
        roster_str = format_team_roster(team_roster, current_persona_id=persona_id)
        content = content.replace('{{TEAM_ROSTER}}', roster_str)
    
    if team_size is not None:
        check_lines = compute_conversation_check_lines(team_size)
        content = content.replace('{{CONVERSATION_CHECK_LINES}}', str(check_lines))
    
    return content


def _strip_code_fences(text: str) -> str:
    """Strip markdown code fences from LLM response text.
    
    Handles ```json, ```JSON, bare ```, and no-newline variants.
    """
    text = text.strip()
    if text.startswith("```"):
        # Remove opening fence (with optional language tag)
        first_newline = text.find("\n")
        if first_newline != -1:
            text = text[first_newline + 1:]
        else:
            # No newline: strip ``` prefix and any language tag
            text = text[3:]
            for tag in ("json", "JSON", "yaml", "YAML"):
                if text.startswith(tag):
                    text = text[len(tag):]
                    break
        # Remove closing fence
        if text.rstrip().endswith("```"):
            text = text.rstrip()
            text = text[:-3]
        text = text.strip()
    return text


def render_persona(skeleton: str, slots: dict) -> str:
    """Fill placeholder slots in a persona skeleton template.
    
    Uses simple string replacement so that {{RUNTIME_TOKENS}} are preserved
    (they don't match any {single_brace_key} pattern).
    """
    import re
    result = skeleton
    for key, value in slots.items():
        result = result.replace(f'{{{key}}}', str(value))
    
    # Warn about unreplaced single-brace placeholders (not {{runtime}} tokens)
    unreplaced = re.findall(r'(?<!\{)\{([a-z_]+)\}(?!\})', result)
    if unreplaced:
        log(f"Unreplaced placeholders in persona: {unreplaced}", "WARN")
    
    return result


def parse_persona_frontmatter(filepath: Path) -> dict:
    """Extract YAML frontmatter from a .persona.md file.
    
    Returns dict with keys: id, name, domain, role, mention.
    Raises ValueError if frontmatter is missing or invalid.
    """
    import yaml
    content = filepath.read_text(encoding='utf-8')
    lines = content.split('\n')
    
    if not lines or lines[0].strip() != '---':
        raise ValueError(f"No YAML frontmatter found in {filepath}")
    
    # Find closing --- on its own line with no indentation (skip line 0)
    end_line = None
    for i, line in enumerate(lines[1:], 1):
        if line.rstrip() == '---':
            end_line = i
            break
    
    if end_line is None:
        raise ValueError(f"No closing frontmatter delimiter in {filepath}")
    
    frontmatter_str = '\n'.join(lines[1:end_line])
    frontmatter = yaml.safe_load(frontmatter_str)
    
    if frontmatter is None:
        raise ValueError(f"Empty or invalid YAML frontmatter in {filepath}")
    
    # Validate required keys
    missing = [k for k in PERSONA_FRONTMATTER_KEYS if k not in frontmatter]
    if missing:
        raise ValueError(f"Missing frontmatter keys in {filepath}: {missing}")
    
    return {k: frontmatter[k] for k in PERSONA_FRONTMATTER_KEYS}


def strip_persona_frontmatter(content: str) -> str:
    """Remove YAML frontmatter from persona file content, returning just the prompt."""
    lines = content.split('\n')
    if not lines or lines[0].strip() != '---':
        return content
    for i, line in enumerate(lines[1:], 1):
        if line.rstrip() == '---':
            return '\n'.join(lines[i + 1:]).lstrip('\n')
    return content


def compute_conversation_check_lines(team_size: int) -> int:
    """Adaptive conversation window: scales with team size."""
    return max(50, team_size * 15)


def format_team_roster(team: list, current_persona_id: str = None) -> str:
    """Format team roster as @mention list for persona files.
    
    Marks the current persona with '(you)' for self-awareness.
    """
    parts = []
    for m in team:
        mention = m.get('mention', f"@{m['name']}")
        if m['id'] == current_persona_id:
            parts.append(f"{mention} (you)")
        else:
            parts.append(mention)
    return ', '.join(parts)


def build_orchestrator_message(team_roster: list, plan_location: str, task_type: str = "software-development",
                               review_notes_path: str = None) -> str:
    """Generate the Phase 0A/0B/Communication conversation message dynamically.
    
    Replaces the hardcoded @PM/@Dev/@Security/@QA/@SRE block with role-based
    instructions built from the actual team roster.
    """
    # Determine lead
    has_pm = any(m['id'] == 'pm' for m in team_roster)
    if task_type == "non-software" and not has_pm:
        # Pure non-code: Scope-keeper leads
        scope_keepers = [m for m in team_roster if m.get('role') == 'Scope-keeper']
        lead = scope_keepers[0] if scope_keepers else team_roster[0]
    else:
        # Code or mixed: PM leads (or first persona if no PM)
        pm = next((m for m in team_roster if m['id'] == 'pm'), None)
        lead = pm or team_roster[0]
    
    lead_mention = lead.get('mention', f"@{lead['name']}")
    
    # Build Phase 0B role-based instructions
    critics = [m for m in team_roster if m.get('role') == 'Critic']
    doers = [m for m in team_roster if m.get('role') == 'Doer' and m['id'] != lead['id']]
    
    phase_0b_steps = [f"1. **{lead_mention}**: Present the plan, clarify acceptance criteria, lead the discussion"]
    
    if critics:
        critic_mentions = ', '.join(m.get('mention', f"@{m['name']}") for m in critics)
        phase_0b_steps.append(f"2. Each Critic ({critic_mentions}): Raise domain-specific concerns NOW")
    
    if doers:
        doer_mentions = ', '.join(m.get('mention', f"@{m['name']}") for m in doers)
        step_num = len(phase_0b_steps) + 1
        phase_0b_steps.append(f"{step_num}. Each Doer ({doer_mentions}): Propose approach, identify risks, suggest adjustments")
    
    # For static code team, add specific role callouts
    security = next((m for m in team_roster if m['id'] == 'security'), None)
    if security:
        step_num = len(phase_0b_steps) + 1
        phase_0b_steps.append(f"{step_num}. **@Security**: Raise ALL security concerns NOW (not during implementation)")
    
    step_num = len(phase_0b_steps) + 1
    phase_0b_steps.append(f"{step_num}. ALL agents must participate and acknowledge the plan")
    
    phase_0b_text = '\n'.join(phase_0b_steps)
    
    # Security gate for mixed/code tasks
    security_gate = ""
    if security and task_type in ("software-development", "mixed"):
        security_gate = "\n- @Security must approve the security approach BEFORE implementation begins"
    
    # Phase 0B deliverables (gap analysis)
    deliverables = f"""
### Design Discussion Deliverables
Design discussion produces updated artifacts, not just conversation:
1. If the team identified gaps, missing phases, or restructuring:
   - {lead_mention} updates _INDEX.md to reflect agreed structure
   - Affected phase files are edited (added tasks, modified criteria, reordered work)
   - New phase files are created if the team agreed to add phases
2. All decisions and filled gaps recorded in DecisionsTracker.md
3. {lead_mention} declares: "@Team design discussion complete. Plan files updated. Begin Phase 1"
"""

    # Communication section with full mention list
    all_mentions = ', '.join(m.get('mention', f"@{m['name']}") for m in team_roster)
    all_mentions += ', @Team, @AllAgents'
    
    # Phased workflow section
    phased_workflow = f"""
## Phased Plan Workflow (if using phases/ structure)

After each phase is complete:
1. {lead_mention} updates `_INDEX.md` with: ✅ Complete, commit hash
2. {lead_mention} verifies `DecisionsTracker.md` has entries for any deviations made during this phase — if choices were made that differ from the plan or where the plan was silent, they must be recorded before moving on
3. {lead_mention} announces: "@Team Phase X complete, proceeding to Phase Y"
4. If plan says "STOP after Phase X", team stops and reports to human
"""

    # Review notes reference (if plan review produced recommendations)
    review_notes_ref = ""
    if review_notes_path:
        review_notes_ref = f"\n- **Before starting discussion**: Read `{review_notes_path}` — it contains pre-execution review notes to consider"

    return f"""@AllAgents - Welcome to Mandali!

You are an autonomous team implementing {plan_location}

---

## PHASE 0A: CONTEXT BUILDING (Before Design Discussion)

Before discussing the design, each agent MUST build a complete understanding:

### Required Actions for EACH Agent:
1. **Read _CONTEXT.md FIRST** (if phased plan) - contains global architecture, security, non-negotiables
2. **Read _INDEX.md** (if phased plan) - shows phase status and dependencies
3. **Read the relevant phase file(s)** - understand tasks and quality gates
4. **Explore the codebase** - understand project structure, patterns, conventions
5. **Launch background agents** if needed to explore large codebases efficiently
6. **Understand dependencies** - what exists, what needs to be built

### Your Tools:
- Use `view` to read files
- Use `glob` and `grep` to explore the codebase
- Use `task` tool with agent_type="explore" for parallel codebase exploration
- Take your time - understanding the full picture is critical

### When Ready:
Each agent should post: "@Team - I have reviewed the plan and codebase. Ready for design discussion."

**Wait for ALL agents to confirm readiness before starting design discussion.**

---

## PHASE 0B: DESIGN DISCUSSION (After All Agents Ready)

Once ALL agents confirm readiness, begin design discussion:

{phase_0b_text}

**Rules for Design Discussion:**
- ALL agents must participate and acknowledge the plan{security_gate}{review_notes_ref}
- Team may reorder phases, add sub-phases, or adjust scope
{deliverables}
---
{phased_workflow}
---

## Communication
- Use @mentions: {all_mentions}
- End each message with SATISFACTION_STATUS

## Victory Condition
All agents SATISFIED = Implementation complete.

---

@AllAgents - Begin by reading the plan and exploring the codebase. 
Post when you're ready for design discussion.
"""


async def _send_and_get_response(client, model: str, system_prompt: str, message: str,
                                  timeout_seconds: int = 120) -> str:
    """Send a single message to an LLM session and return the response text.
    
    Uses the event-based SDK pattern (create_session + on + send).
    No tools/MCP/skills — this is for pure text-in/text-out calls
    (classification, persona generation, dedup, merge).
    Raises TimeoutError if no response within timeout_seconds.
    """
    session = await client.create_session({
        "model": model,
        "system_message": system_prompt,
    })
    
    response_parts = []
    done = asyncio.Event()
    
    def on_event(event):
        if event.type.value == "assistant.message":
            response_parts.append(event.data.content)
        elif event.type.value == "session.idle":
            done.set()
    
    unsubscribe = session.on(on_event)
    try:
        await session.send({"prompt": message})
        await asyncio.wait_for(done.wait(), timeout=timeout_seconds)
    except asyncio.TimeoutError:
        raise TimeoutError(f"LLM session timed out after {timeout_seconds}s")
    finally:
        unsubscribe()
        await session.destroy()
    
    response = ''.join(response_parts)
    
    _debug_log("llm_call", {
        "system_prompt_preview": system_prompt[:200],
        "message_preview": message[:500],
        "response_preview": response[:1000],
        "response_length": len(response),
        "model": model,
    })
    
    return response


async def classify_task(client, model: str, user_prompt: str, interview_summary: dict) -> 'TaskClassification':
    """Classify a task into type and domains using LLM analysis.
    
    Returns TaskClassification with task_type and ordered domains.
    On unparseable response, retries in the same session (LLM already
    has the analysis, just needs to reformat).
    Conservative default: classifies as 'software-development' when uncertain.
    """
    # Extract only deliverable-relevant fields from interview summary.
    # The full summary contains domain jargon that confuses the classifier.
    slim_summary = {}
    for key in ("outcome", "project_name", "success_criteria", "scope", 
                "output_directory", "constraints", "implicit_requirements"):
        if key in interview_summary:
            slim_summary[key] = interview_summary[key]
    
    # Put the user's original prompt LAST — recency bias helps the LLM
    # focus on the actual ask rather than the elaborated interview content.
    message = (
        f"Classify this task.\n\n"
        f"## Interview Context (deliverables only)\n{json.dumps(slim_summary, indent=2)}\n\n"
        f"## User's Original Prompt (this is the PRIMARY signal for classification)\n{user_prompt}"
    )
    
    VALID_TASK_TYPES = ("software-development", "non-software", "mixed")
    SW_DEV_DOMAIN = [{"name": "software-development", "role_in_task": "primary"}]
    
    # Manage session manually for same-session retry
    session = await client.create_session({
        "model": model,
        "system_message": CLASSIFIER_PROMPT,
    })
    
    async def _send_and_collect(msg: str) -> str:
        parts = []
        done = asyncio.Event()
        def on_event(event):
            if event.type.value == "assistant.message":
                parts.append(event.data.content)
            elif event.type.value == "session.idle":
                done.set()
        unsub = session.on(on_event)
        try:
            await session.send({"prompt": msg})
            await asyncio.wait_for(done.wait(), timeout=120)
        finally:
            unsub()
        return ''.join(parts)
    
    task_type = None
    domains = SW_DEV_DOMAIN
    
    try:
        response_text = await _send_and_collect(message)
        text = response_text.strip()
        
        _debug_log("llm_call", {
            "system_prompt_preview": CLASSIFIER_PROMPT[:200],
            "message_preview": message[:500],
            "response_preview": text[:1000],
            "response_length": len(text),
            "model": model,
        })
        _debug_log("classify_raw", {"response": text[:2000]})
        
        # Parse key-value lines via regex
        task_type_match = re.search(r'TASK_TYPE:\s*(\S+)', text, re.IGNORECASE)
        domain_matches = re.findall(r'DOMAIN_\d+:\s*(\S+)', text, re.IGNORECASE)
        
        task_type = task_type_match.group(1).lower().strip() if task_type_match else None
        
        # Build domains list from matches, filtering out NONE
        if domain_matches:
            domains = []
            for i, d in enumerate(domain_matches):
                d = d.lower().strip()
                if d and d != "none":
                    domains.append({"name": d, "role_in_task": "primary" if i == 0 else "supporting"})
            domains = domains or SW_DEV_DOMAIN
        
        if not task_type or task_type not in VALID_TASK_TYPES:
            # Retry in the same session — LLM already analyzed the task
            original_value = task_type or "(no TASK_TYPE found in response)"
            log(f"Classifier returned '{original_value}', retrying in same session...", "WARN")
            
            retry_msg = (
                "Your previous response was not in the expected format.\n\n"
                "Respond with EXACTLY these 4 lines, nothing else:\n\n"
                "TASK_TYPE: <software-development or non-software or mixed>\n"
                "DOMAIN_1: <primary-domain-slug>\n"
                "DOMAIN_2: <supporting-domain-slug or NONE>\n"
                "DOMAIN_3: <supporting-domain-slug or NONE>\n\n"
                "IMPORTANT: TASK_TYPE must be one of exactly three values: software-development, non-software, mixed.\n"
                "No explanation, no tables, no markdown — ONLY the 4 lines above."
            )
            
            retry_text = await _send_and_collect(retry_msg)
            _debug_log("llm_call", {
                "system_prompt_preview": "classifier_retry",
                "message_preview": retry_msg[:500],
                "response_preview": retry_text[:1000],
                "response_length": len(retry_text),
                "model": model,
            })
            
            retry_tt = re.search(r'TASK_TYPE:\s*(\S+)', retry_text, re.IGNORECASE)
            retry_domains = re.findall(r'DOMAIN_\d+:\s*(\S+)', retry_text, re.IGNORECASE)
            if retry_tt:
                task_type = retry_tt.group(1).lower().strip()
            if retry_domains:
                domains = []
                for i, d in enumerate(retry_domains):
                    d = d.lower().strip()
                    if d and d != "none":
                        domains.append({"name": d, "role_in_task": "primary" if i == 0 else "supporting"})
                domains = domains or SW_DEV_DOMAIN
        
        # If still invalid after retry, normalize from value
        if not task_type or task_type not in VALID_TASK_TYPES:
            sw_indicators = ("software", "engineering", "devops", "infrastructure", "build", "develop", "program", "implement", "deploy")
            task_type_lower = (task_type or "").lower()
            if any(kw in task_type_lower for kw in sw_indicators):
                task_type = "software-development"
            else:
                task_type = "software-development"
            log(f"Normalized task_type to '{task_type}'", "INFO")
    except TimeoutError:
        log("Classifier timed out, defaulting to software-development", "WARN")
        task_type = "software-development"
    finally:
        await session.destroy()
    
    _debug_log("classify_result", {
        "task_type": task_type,
        "domains": domains,
    })
    
    return TaskClassification(
        task_type=task_type,
        domains=domains,
        interview_summary=interview_summary,
    )


def classify_from_domains_flag(domains_str: str, interview_summary: dict = None) -> 'TaskClassification':
    """Create TaskClassification from --domains CLI flag.
    
    Infers task_type: "software-development" in domains → mixed, otherwise → non-software.
    Raises ValueError if no valid domains provided.
    """
    domain_names = [d.strip() for d in domains_str.split(",") if d.strip()]
    if not domain_names:
        raise ValueError("At least one domain required when using --domains flag")
    
    has_sw = "software-development" in domain_names or "code" in domain_names
    task_type = "mixed" if has_sw else "non-software"
    
    domains = []
    for i, name in enumerate(domain_names):
        domains.append({
            "name": name,
            "role_in_task": "primary" if i == 0 else "supporting",
        })
    
    return TaskClassification(
        task_type=task_type,
        domains=domains,
        interview_summary=interview_summary or {},
    )


async def generate_persona_file(client, model: str, skeleton: str, domain: str, role: str,
                                 existing_roster: list, personas_dir: Path) -> tuple:
    """Generate a persona .md file by having an LLM agent write it directly.
    
    The agent gets tools and writes the file itself — no JSON/frontmatter parsing needed.
    File name is deterministic: {domain}-{role}.persona.md
    Returns (filepath, meta_dict) or raises if the file wasn't created.
    """
    # Deterministic metadata — computed upfront, never parsed from file
    persona_id = f"{domain}-{role.lower()}"
    name = f"{domain.replace('-', ' ').title()} {role}"
    mention = f"@{name.replace(' ', '')}"
    filepath = personas_dir / f"{persona_id}.persona.md"
    personas_dir.mkdir(parents=True, exist_ok=True)
    
    meta = {
        'id': persona_id,
        'name': name,
        'domain': domain,
        'role': role,
        'mention': mention,
        'filepath': filepath,
    }
    
    # Remove stale file from prior failed attempt so LLM's create tool won't refuse
    if filepath.exists():
        filepath.unlink()
    
    system_prompt = (
        f"You are a persona generator. Your ONLY job is to create a single persona file.\n\n"
        + PERSONA_GENERATOR_PROMPT + "\n\n"  # Quality bar, role types (background context)
        f"## CRITICAL INSTRUCTIONS\n\n"
        f"1. Use the `create` tool to write the file to EXACTLY this path: {filepath}\n"
        f"2. Fill EVERY {{placeholder}} in the template with domain-appropriate content\n"
        f"3. Leave {{{{TEAM_ROSTER}}}} and {{{{CONVERSATION_CHECK_LINES}}}} as-is — they are runtime tokens\n"
        f"4. Do NOT explain anything — just call the create tool with the file content\n"
        f"5. Do NOT write your own format — use the EXACT template structure below\n\n"
        f"## TEMPLATE (follow this structure exactly, fill in all {{placeholders}})\n\n"
        f"```\n{skeleton}\n```"
    )
    
    message = (
        f"Create the persona file for a **{role}** in the **{domain}** domain.\n"
        f"Existing team members (for awareness, avoid overlap): {', '.join(existing_roster) or 'none yet'}\n\n"
        f"Write the file to: {filepath}"
    )
    
    # Use a session WITH tools so the LLM can use `create`
    session_config = _build_session_config(model, system_prompt, str(personas_dir.parent))
    session = await client.create_session(session_config)
    
    done = asyncio.Event()
    
    def on_event(event):
        if event.type.value == "session.idle":
            done.set()
        elif event.type.value == "session.error":
            done.set()
    
    unsubscribe = session.on(on_event)
    try:
        await session.send({"prompt": message})
        await asyncio.wait_for(done.wait(), timeout=180)
    except asyncio.TimeoutError:
        log(f"Persona generation timed out for {domain}/{role}", "WARN")
    finally:
        unsubscribe()
        await session.destroy()
    
    # Verify the file was created — that's all we need
    if not filepath.exists():
        _debug_log("persona_gen_fail", {"domain": domain, "role": role, "reason": "file not created"})
        raise ValueError(f"Persona generator did not create file for {domain}/{role} at {filepath}")
    
    _debug_log("persona_gen_ok", {"domain": domain, "role": role, "id": persona_id, "name": name})
    log(f"Generated persona: {persona_id} ({domain}/{role}) → {filepath.name}", "OK")
    return filepath, meta


async def deduplicate_personas(client, model: str, persona_registry: dict, static_roster: list) -> dict:
    """Analyze generated personas for overlap using an unbiased dedup agent.
    
    persona_registry: dict[str, dict] — keyed by persona id, value is meta dict with 'filepath'.
    Reads FULL persona file contents (not summaries) to distinguish surface
    overlap from real functional overlap. Returns recommendations dict with
    'keep', 'drop', and 'merge' lists.
    
    On unparseable response, retries once in the same session so the LLM
    has full context of its prior analysis and just needs to reformat.
    """
    # Build the full content payload for the dedup agent
    persona_contents = []
    for pid, meta in persona_registry.items():
        filepath = Path(meta['filepath'])
        content = filepath.read_text(encoding='utf-8')
        persona_contents.append(f"### Persona: {pid} ({meta['domain']}/{meta['role']})\n```\n{content}\n```")
    
    static_section = f"## Static Team (cannot be dropped/merged)\n{', '.join(static_roster)}" if static_roster else ""
    
    message = (
        f"{static_section}\n\n"
        f"## Dynamic Personas to Analyze ({len(persona_registry)} personas)\n\n"
        + "\n\n".join(persona_contents)
        + "\n\nAnalyze these personas for overlap and provide keep/drop/merge recommendations."
    )
    
    # Manage session manually so we can send a follow-up retry in the same context
    session = await client.create_session({
        "model": model,
        "system_message": DEDUP_AGENT_PROMPT,
    })
    
    async def _send_and_collect(msg: str) -> str:
        parts = []
        done = asyncio.Event()
        def on_event(event):
            if event.type.value == "assistant.message":
                parts.append(event.data.content)
            elif event.type.value == "session.idle":
                done.set()
        unsub = session.on(on_event)
        try:
            await session.send({"prompt": msg})
            await asyncio.wait_for(done.wait(), timeout=120)
        finally:
            unsub()
        return ''.join(parts)
    
    recommendations = None
    
    try:
        response_text = await _send_and_collect(message)
        _debug_log("llm_call", {
            "system_prompt_preview": DEDUP_AGENT_PROMPT[:200],
            "message_preview": message[:500],
            "response_preview": response_text[:1000],
            "response_length": len(response_text),
            "model": model,
        })
        
        text = _strip_code_fences(response_text)
        try:
            recommendations = json.loads(text)
        except json.JSONDecodeError:
            # Retry in the same session — LLM already has the full analysis context
            log("Dedup agent returned non-JSON, retrying in same session...", "WARN")
            _debug_log("dedup_parse_fail", {"raw": text[:2000], "attempt": 1})
            
            retry_msg = (
                "Your previous response was not valid parseable JSON.\n\n"
                "You MUST respond with ONLY a JSON object in this exact format:\n"
                "```\n"
                "{\n"
                '  "keep": [\n'
                '    {"id": "<persona-id>", "reason": "<why unique and needed>"}\n'
                "  ],\n"
                '  "drop": [\n'
                '    {"id": "<persona-id>", "reason": "<why redundant>", "covered_by": "<id>"}\n'
                "  ],\n"
                '  "merge": [\n'
                '    {"sources": ["<id-1>", "<id-2>"], "merged_name": "<name>", "reason": "<why>", "merge_guidance": "<what to keep>"}\n'
                "  ]\n"
                "}\n"
                "```\n\n"
                "Rules (still apply):\n"
                "- Default to KEEP unless genuine functional overlap exists\n"
                "- MERGE when two personas have significantly overlapping expertise\n"
                "- DROP when one persona is strictly a subset of another\n"
                "- Static team members cannot be dropped or merged\n"
                "- Dynamic overlapping with static → DROP the dynamic one\n"
                "- Be conservative: when in doubt, KEEP both\n\n"
                "Reformat your analysis as the JSON object above. No markdown, no tables, no explanation — ONLY the JSON."
            )
            
            retry_text = await _send_and_collect(retry_msg)
            _debug_log("llm_call", {
                "system_prompt_preview": "dedup_retry",
                "message_preview": retry_msg[:500],
                "response_preview": retry_text[:1000],
                "response_length": len(retry_text),
                "model": model,
            })
            
            retry_cleaned = _strip_code_fences(retry_text)
            try:
                recommendations = json.loads(retry_cleaned)
            except json.JSONDecodeError:
                log(f"Dedup retry also failed, keeping all: {retry_cleaned[:200]}", "WARN")
                _debug_log("dedup_parse_fail", {"raw": retry_cleaned[:2000], "attempt": 2})
    except TimeoutError:
        log("Dedup agent timed out, keeping all", "WARN")
    finally:
        await session.destroy()
    
    if recommendations is None:
        keep_list = [{"id": pid, "reason": "dedup failed"} for pid in persona_registry]
        return {"keep": keep_list, "drop": [], "merge": []}
    
    # Validate structure — ensure all values are lists
    for key in ('keep', 'drop', 'merge'):
        if key not in recommendations or not isinstance(recommendations[key], list):
            recommendations[key] = []
    
    # Validate items — filter out entries missing required 'id' field
    for key in ('keep', 'drop'):
        recommendations[key] = [item for item in recommendations[key]
                                if isinstance(item, dict) and item.get('id')]
    recommendations['merge'] = [item for item in recommendations['merge']
                                if isinstance(item, dict) and isinstance(item.get('sources'), list)
                                and len(item['sources']) >= 2]
    
    keep_count = len(recommendations['keep'])
    drop_count = len(recommendations['drop'])
    merge_count = len(recommendations['merge'])
    log(f"Dedup result: {keep_count} keep, {drop_count} drop, {merge_count} merge", "OK")
    _debug_log("dedup_result", {"recommendations": recommendations})
    
    return recommendations


MERGE_PROMPT = """You are a persona merger for a multi-agent collaboration system.

You receive two persona files that overlap. Your job: read both, produce a SINGLE merged persona file that combines the best of both.

Rules:
- Preserve specific domain knowledge from BOTH sources — don't dilute
- Merge engagement rules, satisfaction criteria, and non-negotiables (union, not intersection)
- The merged persona should be MORE capable than either source alone
- Non-negotiables, quality gates, and domain expertise from BOTH sources must survive the merge
- Inherit domain and role from the primary source
- The merged file MUST have valid YAML frontmatter with id, name, domain, role, mention
"""


async def execute_merges(client, model: str, merge_recs: list, persona_registry: dict,
                          personas_dir: Path) -> tuple:
    """Execute merge recommendations: have LLM agent read sources and write merged file.
    
    persona_registry: dict[str, dict] — keyed by persona id, value is meta dict with 'filepath'.
    Returns (merged_metas, merged_source_ids) — new meta dicts and which source IDs were consumed.
    """
    merged_metas = []
    merged_source_ids = set()
    
    for merge in merge_recs:
        source_ids = merge.get('sources', [])
        if len(source_ids) < 2:
            continue
        
        if len(source_ids) > 2:
            log(f"Merge of {len(source_ids)} personas not supported, using first 2: {source_ids[:2]}", "WARN")
            source_ids = source_ids[:2]
        
        # Verify source files exist
        source_paths = []
        for sid in source_ids:
            if sid in persona_registry:
                path = Path(persona_registry[sid]['filepath'])
                if path.exists():
                    source_paths.append(path)
        
        if len(source_paths) < 2:
            log(f"Merge skipped: not enough source files for {source_ids}", "WARN")
            continue
        
        # Deterministic merged metadata — inherit domain from first source
        merged_id = f"merged-{'-'.join(source_ids[:2])}"
        merged_path = personas_dir / f"{merged_id}.persona.md"
        primary_meta = persona_registry[source_ids[0]]
        merged_name = merge.get('merged_name', '').strip()
        if not merged_name:
            merged_name = f"Merged {primary_meta['name']}"
        merged_meta = {
            'id': merged_id,
            'name': merged_name,
            'domain': primary_meta['domain'],
            'role': primary_meta['role'],
            'mention': f"@{merged_name.replace(' ', '')}",
            'filepath': merged_path,
        }
        
        if merged_path.exists():
            merged_path.unlink()
        
        system_prompt = (
            f"{MERGE_PROMPT}\n\n"
            f"## Merge Guidance\n{merge.get('merge_guidance', 'Combine both personas')}\n\n"
            f"Read the two source persona files, then use the `create` tool to write the merged persona to:\n"
            f"{merged_path}\n\n"
            f"The merged file must follow the same structure as the source files."
        )
        
        message = (
            f"Merge these two persona files into one:\n"
            f"- Source 1: {source_paths[0]}\n"
            f"- Source 2: {source_paths[1]}\n\n"
            f"Read both files, then write the merged result to: {merged_path}"
        )
        
        session_config = _build_session_config(model, system_prompt, str(personas_dir.parent))
        session = await client.create_session(session_config)
        
        done = asyncio.Event()
        def on_event(event):
            if event.type.value in ("session.idle", "session.error"):
                done.set()
        
        unsubscribe = session.on(on_event)
        try:
            await session.send({"prompt": message})
            await asyncio.wait_for(done.wait(), timeout=180)
        except asyncio.TimeoutError:
            log(f"Merge timed out for {source_ids}", "WARN")
        finally:
            unsubscribe()
            await session.destroy()
        
        if not merged_path.exists():
            log(f"Merge failed for {source_ids}: file not created", "WARN")
            continue
        
        merged_metas.append(merged_meta)
        
        # Delete originals and track consumed IDs
        for sid in source_ids:
            if sid in persona_registry:
                Path(persona_registry[sid]['filepath']).unlink(missing_ok=True)
                merged_source_ids.add(sid)
                log(f"Deleted merged source: {sid}", "INFO")
        
        log(f"Merged {source_ids} → {merged_id}", "OK")
    
    return merged_metas, merged_source_ids


async def assemble_team(client, model: str, classification: 'TaskClassification',
                         workspace: 'Workspace', config: dict) -> list:
    """Orchestrate full persona generation + dedup pipeline.
    
    Returns a list of team member dicts, each with:
    id, name, promptFile, dynamic, domain, role, mention
    """
    DYNAMIC_PERSONA_CAP = 6
    
    # Pure software-development: return static team unchanged
    if classification.task_type == "software-development":
        static_team = []
        for p in config.get('personas', []):
            static_team.append({
                'id': p['id'],
                'name': p['name'],
                'promptFile': str(SCRIPT_DIR / p['promptFile']),
                'dynamic': False,
                'domain': 'software-development',
                'role': 'Doer',
                'mention': f"@{p['name']}",
            })
        log(f"Pure software-development task: using {len(static_team)} static personas", "OK")
        return static_team
    
    # Non-code or mixed: generate dynamic personas
    personas_dir = workspace.artifacts_path / "dynamic-personas"
    personas_dir.mkdir(parents=True, exist_ok=True)
    
    # Build existing roster for awareness during generation
    static_team = []
    existing_roster = []
    if classification.task_type == "mixed":
        for p in config.get('personas', []):
            static_team.append({
                'id': p['id'],
                'name': p['name'],
                'promptFile': str(SCRIPT_DIR / p['promptFile']),
                'dynamic': False,
                'domain': 'software-development',
                'role': 'Doer',
                'mention': f"@{p['name']}",
            })
            existing_roster.append(p['name'])
    
    # Determine roles needed per domain (skip software-development domain — handled by static team)
    non_sw_domains = [d for d in classification.domains if d['name'] not in ('software-development', 'code')]
    
    if not non_sw_domains:
        # All domains are software-development but task_type says non-software — classifier inconsistency.
        if classification.task_type == "non-software":
            # Use all domains as-is since the classifier clearly got domains wrong
            log("Non-software task but all domains are software-development — using domains as-is", "WARN")
            non_sw_domains = classification.domains
        else:
            log("No non-software domains after filtering, using static team", "WARN")
            return static_team if static_team else [{
                'id': p['id'], 'name': p['name'],
                'promptFile': str(SCRIPT_DIR / p['promptFile']),
                'dynamic': False, 'domain': 'software-development', 'role': 'Doer',
                'mention': f"@{p['name']}",
            } for p in config.get('personas', [])]
    
    # Generate personas in parallel: Doer + Critic + Scope-keeper candidate per domain
    generation_tasks = []
    task_metadata = []  # Track domain/role for each task
    
    for domain_info in non_sw_domains:
        domain = domain_info['name']
        for role in ['Doer', 'Critic', 'Scope-keeper']:
            generation_tasks.append(
                generate_persona_file(client, model, PERSONA_SKELETON_TEMPLATE,
                                       domain, role, existing_roster, personas_dir)
            )
            task_metadata.append({'domain': domain, 'role': role})
    
    log(f"Generating {len(generation_tasks)} personas in parallel...", "INFO")
    results = await asyncio.gather(*generation_tasks, return_exceptions=True)
    
    # Error handling per plan: retry failed Doer once, skip failed Critic/Scope-keeper
    # Build persona_registry: dict[id, meta] — the single source of truth for metadata
    persona_registry = {}
    failed_domains = set()
    
    for i, result in enumerate(results):
        task_meta = task_metadata[i]
        if isinstance(result, Exception):
            if task_meta['role'] == 'Doer':
                log(f"Doer generation failed for {task_meta['domain']}, retrying...", "WARN")
                try:
                    filepath, meta = await generate_persona_file(
                        client, model, PERSONA_SKELETON_TEMPLATE,
                        task_meta['domain'], 'Doer', existing_roster, personas_dir
                    )
                    persona_registry[meta['id']] = meta
                except Exception as e2:
                    log(f"Doer retry failed for {task_meta['domain']}, dropping domain: {e2}", "ERR")
                    failed_domains.add(task_meta['domain'])
            else:
                log(f"{task_meta['role']} generation failed for {task_meta['domain']}, skipping: {result}", "WARN")
        else:
            filepath, meta = result
            persona_registry[meta['id']] = meta
    
    # Remove personas from failed domains
    if failed_domains:
        persona_registry = {
            pid: m for pid, m in persona_registry.items()
            if m['domain'] not in failed_domains
        }
    
    if not persona_registry:
        log("All persona generation failed, falling back to static team", "ERR")
        if static_team:
            return static_team
        return [{
            'id': p['id'], 'name': p['name'],
            'promptFile': str(SCRIPT_DIR / p['promptFile']),
            'dynamic': False, 'domain': 'software-development', 'role': 'Doer',
            'mention': f"@{p['name']}",
        } for p in config.get('personas', [])]
    
    # Dedup
    log("Deduplicating generated personas...", "INFO")
    static_names = [p['name'] for p in static_team]
    recommendations = await deduplicate_personas(client, model, persona_registry, static_names)
    
    # Drop recommended personas
    for drop in recommendations.get('drop', []):
        drop_id = drop.get('id')
        if drop_id in persona_registry:
            Path(persona_registry[drop_id]['filepath']).unlink(missing_ok=True)
            del persona_registry[drop_id]
            log(f"Dropped persona: {drop_id} — {drop.get('reason', 'overlap')}", "INFO")
    
    # Execute merges
    merge_recs = recommendations.get('merge', [])
    if merge_recs:
        merged_metas, merged_source_ids = await execute_merges(
            client, model, merge_recs, persona_registry, personas_dir
        )
        for sid in merged_source_ids:
            persona_registry.pop(sid, None)
        for merged_meta in merged_metas:
            persona_registry[merged_meta['id']] = merged_meta
    
    # Elect Scope-keeper: primary domain's candidate wins
    primary_domain = non_sw_domains[0]['name'] if non_sw_domains else None
    scope_keeper_elected = False
    scope_keeper_winner_id = None
    scope_keeper_loser_ids = []
    
    for pid, meta in list(persona_registry.items()):
        if meta['role'] == 'Scope-keeper':
            if meta['domain'] == primary_domain and not scope_keeper_elected:
                scope_keeper_elected = True
                scope_keeper_winner_id = pid
                log(f"Scope-keeper elected: {pid} (primary domain: {primary_domain})", "OK")
            else:
                scope_keeper_loser_ids.append(pid)
    
    # Inject domain awareness from losers into winner, then remove losers
    if scope_keeper_loser_ids and scope_keeper_winner_id:
        addendum_sections = []
        for loser_id in scope_keeper_loser_ids:
            loser_meta = persona_registry[loser_id]
            loser_file = Path(loser_meta['filepath'])
            content = loser_file.read_text(encoding='utf-8')
            # Extract domain expertise section
            expertise_match = re.search(
                r'## Domain Expertise\s*\n(.*?)(?=\n## |\Z)', content, re.DOTALL
            )
            if expertise_match:
                addendum_sections.append(
                    f"### {loser_meta['domain'].replace('-', ' ').title()} Domain Awareness\n"
                    f"{expertise_match.group(1).strip()}"
                )
            loser_file.unlink(missing_ok=True)
            del persona_registry[loser_id]
            log(f"Scope-keeper loser removed: {loser_id}", "INFO")
        
        if addendum_sections:
            winner_file = Path(persona_registry[scope_keeper_winner_id]['filepath'])
            winner_content = winner_file.read_text(encoding='utf-8')
            addendum = "\n\n## Cross-Domain Awareness\n\n" + "\n\n".join(addendum_sections)
            winner_content = winner_content.rstrip() + "\n" + addendum + "\n"
            winner_file.write_text(winner_content, encoding='utf-8')
            log(f"Injected {len(addendum_sections)} domain(s) awareness into {scope_keeper_winner_id}", "OK")
    elif scope_keeper_loser_ids:
        # No winner found — just remove losers
        for loser_id in scope_keeper_loser_ids:
            Path(persona_registry[loser_id]['filepath']).unlink(missing_ok=True)
            del persona_registry[loser_id]
            log(f"Scope-keeper loser removed (no winner): {loser_id}", "INFO")
    
    # Enforce cap of DYNAMIC_PERSONA_CAP
    if len(persona_registry) > DYNAMIC_PERSONA_CAP:
        domain_priority = {d['name']: i for i, d in enumerate(non_sw_domains)}
        critics = [
            (pid, meta)
            for pid, meta in persona_registry.items()
            if meta['role'] == 'Critic'
        ]
        critics.sort(key=lambda x: domain_priority.get(x[1]['domain'], 999), reverse=True)
        
        while len(persona_registry) > DYNAMIC_PERSONA_CAP and critics:
            drop_pid, drop_meta = critics.pop(0)
            Path(drop_meta['filepath']).unlink(missing_ok=True)
            del persona_registry[drop_pid]
            log(f"Cap overflow: dropped Critic {drop_pid}", "INFO")
    
    # Build final dynamic team roster — metadata comes from registry, not files
    dynamic_team = []
    for pid, meta in persona_registry.items():
        dynamic_team.append({
            'id': pid,
            'name': meta['name'],
            'promptFile': str(meta['filepath']),
            'dynamic': True,
            'domain': meta['domain'],
            'role': meta['role'],
            'mention': meta['mention'],
        })
    
    combined = static_team + dynamic_team
    log(f"Team assembled: {len(static_team)} static + {len(dynamic_team)} dynamic = {len(combined)} total", "OK")
    _debug_log("team_assembled", {
        "static": [{"id": p["id"], "domain": p.get("domain")} for p in static_team],
        "dynamic": [{"id": p["id"], "domain": p.get("domain"), "role": p.get("role")} for p in dynamic_team],
    })
    return combined


# ============================================================================
# File Operations (with locking for Windows/Unix)
# ============================================================================

def append_to_conversation(workspace: Workspace, sender: str, message: str):
    """Append a message to conversation.txt with simple format."""
    timestamp = datetime.now().strftime("%H:%M:%S")
    sender_upper = sender.upper()
    
    # Simple one-line-per-message format for easy reading
    # Strip any trailing whitespace from message and ensure single newline
    message_clean = message.strip()
    entry = f"[{timestamp}] @{sender_upper}: {message_clean}\n\n"
    
    with open(workspace.conversation_file, 'a', encoding='utf-8') as f:
        f.write(entry)


def read_conversation(workspace: Workspace) -> str:
    """Read the full conversation."""
    if workspace.conversation_file.exists():
        return workspace.conversation_file.read_text(encoding='utf-8')
    return ""


def read_new_conversation(workspace: Workspace, last_position: int) -> tuple[str, int]:
    """Read only new content since last position."""
    content = read_conversation(workspace)
    new_content = content[last_position:]
    return new_content, len(content)


def update_satisfaction(workspace: Workspace, agent_id: str, status: str):
    """Update an agent's satisfaction status (thread-safe)."""
    with _satisfaction_lock:
        content = {}
        if workspace.satisfaction_file.exists():
            for line in workspace.satisfaction_file.read_text(encoding='utf-8').split('\n'):
                if ':' in line:
                    k, v = line.split(':', 1)
                    content[k.strip()] = v.strip()
        
        content[agent_id] = status
        
        lines = [f"{k}: {v}" for k, v in content.items()]
        workspace.satisfaction_file.write_text('\n'.join(lines), encoding='utf-8')


def read_all_satisfaction(workspace: Workspace) -> Dict[str, str]:
    """Read all agents' satisfaction status."""
    content = {}
    if workspace.satisfaction_file.exists():
        for line in workspace.satisfaction_file.read_text(encoding='utf-8').split('\n'):
            if ':' in line:
                k, v = line.split(':', 1)
                content[k.strip()] = v.strip()
    return content


def check_all_satisfied(workspace: Workspace, expected_agents: list) -> bool:
    """Check if all expected agents are SATISFIED."""
    status = read_all_satisfaction(workspace)
    for agent_id in expected_agents:
        if agent_id not in status or "SATISFIED" not in status[agent_id]:
            return False
    return True


def get_last_activity_time(workspace: Workspace) -> datetime:
    """Get timestamp of last conversation activity."""
    if workspace.conversation_file.exists():
        mtime = workspace.conversation_file.stat().st_mtime
        return datetime.fromtimestamp(mtime)
    return datetime.now()


def archive_conversation(workspace: Workspace, round_number: int):
    """Archive conversation.txt for a completed round and create a fresh one."""
    if workspace.conversation_file.exists():
        timestamp = datetime.now().strftime("%Y_%b_%d-%H_%M_%S")
        archive_name = f"conversation-round-{round_number}-{timestamp}.txt"
        archive_path = workspace.artifacts_path / archive_name
        workspace.conversation_file.rename(archive_path)
        log(f"Archived conversation → {archive_name}", "INFO")
    workspace.conversation_file.touch()


def reset_satisfaction(workspace: Workspace):
    """Clear satisfaction.txt so all agents start fresh."""
    workspace.satisfaction_file.write_text("", encoding='utf-8')


# Verification agent system prompt
VERIFICATION_AGENT_PROMPT = """You are a Verification Agent. Your job is to compare what was planned against what was actually implemented.

# Your Task
You will receive:
1. The original plan (what the team was asked to build)
2. DecisionsTracker.md (intentional deviations recorded by the team)
3. Phase index (_INDEX.md) showing what phases the team completed

You have access to the codebase via tools. **Read actual files** to verify claims.

# How to Evaluate
- Focus on **outcomes**, not process. Ask "Was X built?" not "Did they follow TDD?"
- Treat DecisionsTracker entries as **intentional deviations** — if something is recorded there with a reason, it is NOT a gap. However, if the cumulative effect of these deviations results in a product that does not match the original intent of the task, fails to meet a user's reasonable expectations, or delivers a mediocre outcome — flag that as a gap. Individual deviations may be justified, but their combined impact must still deliver a high-quality, complete product.
- Be **pragmatic** — minor polish items, style differences, or naming choices are NOT gaps.
- Value **creativity** — if the team achieved the same goal via a different approach, that is fine.
- Only flag things where the **end goal was not achieved** — a feature is missing, broken, or fundamentally incomplete.
- **Guard against MVP bias** — the team uses a PoC-style approach that adds complexity gradually, which can create a bias toward delivering only a minimal skeleton. Ask: "Would a user consider this a complete, polished product — or just a working prototype?" If the plan called for a full-featured product and what was delivered feels like a bare-minimum MVP, flag it as a gap even if every individual phase technically passed.
- Do NOT flag items where the plan was vague or left room for interpretation.

# Output Format
If everything looks good:
```
VERIFICATION_RESULT: PASS
```

If there are genuine gaps:
```
VERIFICATION_RESULT: GAPS_FOUND

## Gap 1: [Short Title]
- **What the plan asked for**: [specific requirement from the plan]
- **What was implemented**: [what you found, or "Not found"]
- **Is this in DecisionsTracker?**: No
- **Severity**: [Critical / Important / Minor]

## Gap 2: ...
```

Keep the gap report **concise** — the team will receive it as context for their next round, so brevity matters.
"""


async def run_verification(
    client: CopilotClient,
    model: str,
    workspace: Workspace,
    plan_content: str
) -> tuple[bool, str]:
    """Run a verification agent to compare plan vs actual implementation.
    
    Returns (passed: bool, gap_report: str).
    """
    log("🔍 Running post-implementation verification...", "INFO")
    
    # Gather context for the verification agent
    decisions_content = ""
    if workspace.decisions_file.exists():
        decisions_content = workspace.decisions_file.read_text(encoding='utf-8')
    
    index_content = ""
    if workspace.index_file.exists():
        index_content = workspace.index_file.read_text(encoding='utf-8')
    
    # Create a session with tool access to the codebase
    session = await client.create_session(
        _build_session_config(model, VERIFICATION_AGENT_PROMPT, str(workspace.path))
    )
    
    verification_prompt = f"""# Verify Implementation Against Plan

## Original Plan
{plan_content}

## DecisionsTracker (Intentional Deviations)
{decisions_content if decisions_content.strip() else "(No decisions recorded)"}

## Phase Index
{index_content if index_content.strip() else "(No phase index found)"}

## Instructions
1. Read the plan above carefully to understand what was supposed to be built.
2. Check DecisionsTracker for any intentional deviations (these are NOT gaps).
3. Use your tools to explore the codebase — read actual source files, look for implemented features.
4. Compare what was planned vs what exists in the code.
5. If DecisionsTracker is empty but the conversation or code shows clear deviations from the plan (e.g., different library versions, changed APIs, added scope, different approaches), flag "Empty DecisionsTracker" as a gap — decisions should have been recorded.
6. Output your verdict as VERIFICATION_RESULT: PASS or VERIFICATION_RESULT: GAPS_FOUND with details.

Focus on whether the **end goal** was achieved. Implementation creativity is valued — alternative approaches are fine.
"""
    
    response_parts = []
    done = asyncio.Event()
    
    def on_event(event):
        if event.type.value == "assistant.message":
            response_parts.append(event.data.content)
        elif event.type.value == "session.idle":
            done.set()
    
    session.on(on_event)
    try:
        await session.send({"prompt": verification_prompt})
        await asyncio.wait_for(done.wait(), timeout=300)  # 5 min timeout
    except asyncio.TimeoutError:
        log("Verification agent timed out after 5 minutes", "WARN")
        return True, ""  # Treat timeout as pass — don't block the team
    finally:
        try:
            await session.destroy()
        except Exception:
            pass
    
    response = ''.join(response_parts)
    
    if "VERIFICATION_RESULT: PASS" in response:
        log("✅ Verification passed — implementation matches intent", "OK")
        return True, ""
    elif "VERIFICATION_RESULT: GAPS_FOUND" in response:
        # Extract gap report (everything after GAPS_FOUND)
        gap_report = response.split("VERIFICATION_RESULT: GAPS_FOUND", 1)[1].strip()
        gap_count = gap_report.count("## Gap")
        log(f"⚠️ Verification found {gap_count} gap(s)", "WARN")
        return False, gap_report
    else:
        # Ambiguous response — treat as pass
        log("Verification result ambiguous — treating as pass", "WARN")
        return True, ""
# ============================================================================


async def generate_handoff(
    client: CopilotClient,
    model: str,
    workspace: Workspace,
    plan_content: str,
    user_prompt: str
) -> str:
    """Generate user-facing handoff instructions after successful completion."""
    log("📋 Generating handoff instructions...", "INFO")
    
    session = await client.create_session(
        _build_session_config(model, HANDOFF_PROMPT, str(workspace.path))
    )
    
    prompt = f"""The user's original request:
{user_prompt}

The plan that was implemented:
{plan_content[:3000]}

The workspace is at: {workspace.path}

Write a HANDOFF.md document with instructions for the user on how to use what was created.
Focus on what the user needs to know — not how it was built.
"""
    
    response_parts = []
    done = asyncio.Event()
    
    def on_event(event):
        if event.type.value == "assistant.message":
            response_parts.append(event.data.content)
        elif event.type.value == "session.idle":
            done.set()
    
    session.on(on_event)
    try:
        await session.send({"prompt": prompt})
        await done.wait()
    finally:
        await session.destroy()
    
    content = ''.join(response_parts)
    
    # Check if the LLM already created HANDOFF.md via tools
    handoff_file = workspace.path / "HANDOFF.md"
    if handoff_file.exists():
        file_content = handoff_file.read_text(encoding='utf-8').strip()
        if file_content:
            log(f"Handoff instructions saved to {handoff_file}", "OK")
            return file_content
    
    # Fallback: LLM responded with text instead of using tools
    if content.strip():
        handoff_file.write_text(content, encoding='utf-8')
        log(f"Handoff instructions saved to {handoff_file}", "OK")
        return content
    
    log("Handoff generation produced no content", "WARN")
    return ""


# ============================================================================

async def run_autonomous_agent(
    client: CopilotClient,
    agent: PersonaAgent,
    workspace: Workspace,
    plan_content: str,
    model: str,
    is_first: bool = False,
    team_roster: list = None,
    team_size: int = None
):
    """
    Run an agent autonomously.
    Agent reads conversation, decides when to speak, appends responses.
    """
    prompt_file = getattr(agent, 'prompt_file', None)
    system_prompt = load_persona_prompt(
        agent.id, prompt_file=prompt_file,
        team_roster=team_roster, team_size=team_size
    )
    
    # Create session with tools, working in the output directory
    session_config = _build_session_config(model, system_prompt, str(workspace.path))
    session_config["infinite_sessions"] = {
        "enabled": True,
        "background_compaction_threshold": 0.80,
        "buffer_exhaustion_threshold": 0.95,
    }
    
    session = await client.create_session(session_config)
    agent.session = session
    
    # Initial context for agent
    initial_prompt = f"""
# Workspace
Your workspace: {workspace.path}
Conversation file: {workspace.conversation_file}
Decisions tracker: {workspace.decisions_file}
Plan file: {workspace.plan_file}

# Plan
{plan_content}

# Your Role
You are {agent.name} ({agent.mention}).
You are part of an autonomous Mandali team implementing this plan.

# How to Communicate

## Reading Messages
Use the `view` tool to read conversation.txt periodically:
- `view` path="{workspace.conversation_file}" to see full conversation
- `view` path="{workspace.conversation_file}" view_range=[-50, -1] to see last 50 lines
- Read when you need to check for new messages or @mentions

## Writing Messages  
Just write your response naturally. The orchestrator will append it to conversation.txt on your behalf.
Format: Your message will appear as `[TIME] @{agent.id.upper()}: [your message]`

## Addressing Others
- Direct: "@Dev please implement..." or "@Security please review..."
- Everyone: "@Team" or "@AllAgents"
- Respond to "@ORCHESTRATOR" instructions (system messages)
- Respond to "@HUMAN" messages (human guidance)

# Decision Tracking
When you make a choice that differs from the plan, or where the plan was silent and you had to decide:
- Update the decisions file: {workspace.decisions_file}
- Use the template format already in that file
- This is as important as conversation — a human will read it to understand what changed and why

# Satisfaction Status
End EVERY message with one of:
- SATISFACTION_STATUS: WORKING
- SATISFACTION_STATUS: SATISFIED
- SATISFACTION_STATUS: BLOCKED - [reason]
- SATISFACTION_STATUS: PAUSED

# Your First Action
1. Read the conversation file to see what's been said
2. {"Introduce yourself, then lead by reviewing the plan (you go first)" if is_first else "Introduce yourself when appropriate, join the discussion"}
"""
    
    async def send_and_wait(prompt: str) -> str:
        """Send prompt and wait for response, handling events properly."""
        async with agent.session_lock:
            response_parts = []
            done = asyncio.Event()
            
            def on_event(event):
                if event.type.value == "assistant.message":
                    response_parts.append(event.data.content)
                elif event.type.value == "assistant.message_delta":
                    if hasattr(event.data, 'delta_content') and event.data.delta_content:
                        response_parts.append(event.data.delta_content)
                elif event.type.value == "session.idle":
                    done.set()
                elif event.type.value == "session.error":
                    log(f"{agent.mention} error: {event.data}", "ERR")
                    done.set()
            
            # Register handler, send, wait, then unregister
            unsubscribe = session.on(on_event)
            try:
                await session.send({"prompt": prompt})
                await asyncio.wait_for(done.wait(), timeout=300)  # 5 min timeout
            except asyncio.TimeoutError:
                log(f"{agent.mention} response timeout", "WARN")
            finally:
                unsubscribe()
            
            return ''.join(response_parts)
    
    # Send initial prompt
    try:
        response = await send_and_wait(initial_prompt)
        if response:
            append_to_conversation(workspace, agent.id, response)
            extract_and_update_status(workspace, agent.id, response)
            log(f"{agent.mention} introduced themselves", "AGENT")
    except Exception as e:
        log(f"{agent.mention} failed to initialize: {e}", "ERR")
        raise
    
    # Autonomous loop - agent reads conversation themselves
    last_check_position = 0
    
    while True:
        try:
            await asyncio.sleep(10)  # Check every 10 seconds
            
            # Quick check if there's new content (orchestrator still tracks for termination signals)
            current_content = read_conversation(workspace)
            if len(current_content) == last_check_position:
                continue  # No new content
            
            last_check_position = len(current_content)
            
            # Check for termination signals (orchestrator responsibility)
            if "@ORCHESTRATOR" in current_content[-500:]:  # Check recent content
                recent = current_content[-500:]
                if "VICTORY" in recent:
                    log(f"{agent.mention} acknowledging victory", "AGENT")
                    break
                if "abort" in recent.lower() or "stop all work" in recent.lower():
                    log(f"{agent.mention} acknowledging abort", "AGENT")
                    break
                if "pause" in recent.lower() or "Escalating to @HUMAN" in recent:
                    update_satisfaction(workspace, agent.id, "PAUSED - Awaiting human guidance")
                    log(f"{agent.mention} pausing for human input", "AGENT")
                    continue  # Skip processing while paused
            
            # Prompt agent to check conversation and respond if needed
            check_prompt = f"""
Check the conversation file for new messages and decide if you should respond.

Use `view` tool to read: {workspace.conversation_file}

Then:
1. If you are @mentioned or the topic is in your domain → respond
2. If you have concerns or input → respond  
3. If nothing requires your input → output exactly: NO_RESPONSE_NEEDED

Remember:
- Address others with @mentions (@Dev, @PM, @Security, @QA, @SRE, @Team)
- End every response with SATISFACTION_STATUS
"""
            
            response = await send_and_wait(check_prompt)
            
            if response and "NO_RESPONSE_NEEDED" not in response:
                append_to_conversation(workspace, agent.id, response)
                extract_and_update_status(workspace, agent.id, response)
                log(f"{agent.mention} responded", "AGENT")
                
        except asyncio.CancelledError:
            log(f"{agent.mention} cancelled", "INFO")
            break
        except Exception as e:
            log(f"{agent.mention} error in loop: {e}", "ERR")
            await asyncio.sleep(10)  # Back off on error


def extract_and_update_status(workspace: Workspace, agent_id: str, response: str):
    """Extract satisfaction status from response and update file."""
    # Loose regex: tolerates missing spaces, mixed case, extra whitespace
    match = re.search(r'SATISFACTION_STATUS\s*:\s*(SATISFIED|BLOCKED|PAUSED|WORKING)(?:\s*-\s*(.*))?', response, re.IGNORECASE)
    if match:
        status = match.group(1).upper()
        reason = (match.group(2) or "").split("\n")[0].strip()
        if status == "SATISFIED":
            update_satisfaction(workspace, agent_id, "SATISFIED")
        elif status == "BLOCKED":
            update_satisfaction(workspace, agent_id, f"BLOCKED - {reason}" if reason else "BLOCKED")
        elif status == "PAUSED":
            update_satisfaction(workspace, agent_id, "PAUSED - Awaiting human guidance")
        else:
            update_satisfaction(workspace, agent_id, "WORKING")
    else:
        # Default fallback: agent responded but didn't emit a status tag
        update_satisfaction(workspace, agent_id, "WORKING")


# ============================================================================
# Mode 2: AI Interviewer + Plan Generator (TDD + PoC Focused)
# ============================================================================

INTERVIEWER_PROMPT = """You are an AI interviewer gathering requirements from a user.

IMPORTANT: Do NOT use any tools or create files. Respond with text only.

Your goal: Understand what the user wants to achieve — their desired OUTCOME, their preferences, and what "done" looks like FROM THEIR PERSPECTIVE. You are NOT gathering implementation details — a team of AI agents will figure out the how.

## WHAT TO FOCUS ON:

1. **OUTCOME**: What does the user want to exist when this is done? What does success look like to them?

2. **USER PREFERENCES**: What choices matter to the user? (e.g., technology, visual style, tone, format, audience). Only ask about preferences the user would have an opinion on — don't ask about implementation details they'd expect the team to decide.

3. **EXISTING CONTEXT**:
   - Is there an existing codebase, project, or prior work to build on?
   - Are there existing docs, plans, or files to incorporate?
   - What's the current state/progress?

4. **SCOPE**: What's in and what's out? Where should the team stop?

## IMPLICIT REQUIREMENTS:
Users underspecify. They state *what* they want but omit *obvious* expectations.
Identify what's implied but unstated and include it in your questions so the user can confirm or correct.

- Identify the **table-stakes** for this type of deliverable — the things any user would expect even if they didn't say them
- When something is ambiguous, propose a concrete default rather than leaving it open-ended
  (e.g., instead of "What database?", say "I'll assume SQLite for simplicity — does that work, or do you need something else?")

## WHAT NOT TO ASK ABOUT:
- Testing approach, test frameworks, TDD — the team decides this
- Architecture, design patterns, implementation strategy — the team decides this
- Phase breakdown, task dependencies, quality gates — the team decides this
- Security approach, logging, error handling — the team decides this
- Anything the user would reasonably say "I don't care, just make it work" to
"""

INTERVIEWER_QUESTIONS_INSTRUCTION = """Based on the user's request below, generate a list of clarifying questions.

## Rules:
- Focus ONLY on understanding what the user wants (outcome, preferences, scope) — NOT implementation details
- Each question should address ONE specific thing
- Propose concrete defaults where possible instead of open-ended questions
- Include implicit requirement confirmations (table-stakes the user likely expects but didn't state)
- Don't ask about things the user already specified clearly in their request
- Aim for quality over quantity — ask what genuinely needs clarification

## Output format:
Return a JSON array of questions. Each question is a string.

```json
["Question 1?", "Question 2?", "Question 3?"]
```

## User's request:
{prompt}
"""

INTERVIEWER_SUMMARY_INSTRUCTION = """Based on the user's original request and their answers to your questions, produce a structured summary for the implementation team.

## Original request:
{prompt}

## Questions and answers:
{qa_pairs}

## Output format:
Output exactly: INTERVIEW_COMPLETE

Then output JSON:
```json
{{
  "project_name": "...",
  "outcome": "...",
  "success_criteria": ["..."],
  "user_preferences": {{"key": "value"}},
  "existing_context_files": ["path/to/file", "..."],
  "existing_phase_files": ["path/to/phase-01.md", "..."],
  "completed_phases": ["phase-01", "phase-02", "..."],
  "resume_from_phase": "phase-XX or null if starting fresh",
  "stop_after_phase": "phase-XX or null if completing all",
  "scope": {{
    "in": ["..."],
    "out": ["..."]
  }},
  "codebase_root": "...",
  "output_directory": "...",
  "constraints": ["..."],
  "implicit_requirements": ["..."]
}}
```
"""

HANDOFF_PROMPT = """You are producing a HANDOFF document for the user who requested this work.

The team has finished the task. Your job: write clear, concise instructions so the user knows how to USE what was created. This is NOT a technical summary for developers — it's a guide for the person who asked for this work.

## Rules:
- Start with a brief summary of what was built/created (1-2 sentences)
- Provide step-by-step instructions to get started (how to launch, open, run, read, etc.)
- Include any prerequisites (dependencies, environment setup, etc.)
- Highlight key features or sections the user should know about
- If there are known limitations or next steps, mention them briefly
- Adapt your tone to the task type:
  - Code: "How to run and use this application"
  - Analysis: "How to read this analysis and what the findings mean"
  - Writing: "Overview of what was produced and how to use it"
  - Research: "Summary of findings and how to navigate the deliverables"
- Keep it practical — the user wants to USE the output, not understand how it was built
- Do NOT use tools. Respond with the document content only.
"""

PLAN_GENERATOR_PROMPT = """You are a plan generator that creates PHASED IMPLEMENTATION PLANS as SEPARATE FILES.

## YOUR TASK

You MUST create MULTIPLE FILES using the `create` tool. Do NOT put everything in one file.

## REQUIRED FILES TO CREATE

### File 1: `phases/_CONTEXT.md`
Global context that applies to ALL phases:

```markdown
# [Project Name] - Global Context

> **READ THIS FIRST** before implementing any phase.

## Problem Statement
[What we're building and why - be specific and detailed]

## Approach
- TDD (Test-Driven Development)
- Phased delivery (complete each phase before moving to next)
- Quality gates between phases

## Key Architectural Decisions
| Decision | Choice | Rationale |
|----------|--------|-----------|
| [Decision 1] | [Choice] | [Why] |

## Security Requirements
[Security constraints, authentication, authorization]

## Non-Negotiables
- [Things that MUST NOT change]

## Project Structure
[Directory structure of the codebase]

## Success Criteria
- [ ] [Measurable criterion 1]
- [ ] [Measurable criterion 2]

## Validation Commands
- `dotnet build` - Build the solution
- `dotnet test` - Run all tests

## Commit Guidelines
After each phase: `git commit -m "Phase X: [description]"`
```

### File 2: `phases/_INDEX.md`
Phase tracking table:

```markdown
# Implementation Phase Index

> Read `_CONTEXT.md` first, then find your target phase below.

## Progress Tracking

| Phase | File | Status | Commits | Notes |
|-------|------|--------|---------|-------|
| 1: [Name] | [phase-01-name.md](phase-01-name.md) | ⏳ Not Started | | |
| 2: [Name] | [phase-02-name.md](phase-02-name.md) | ⏳ Not Started | | |

## Phase Dependencies
```
Phase 1 → Phase 2 → Phase 3 → [STOP HERE for testing]
```

## Quick Links
- [Global Context](_CONTEXT.md)
```

### Files 3+: `phases/phase-XX-name.md` (one per phase)
Each phase in its own file:

```markdown
# Phase XX: [Name]

> **Status**: ⏳ Not Started  
> **Dependencies**: Phase [X-1]  
> **Goal**: [One sentence describing what this phase achieves]

## Overview
[2-3 sentences about what this phase accomplishes]

## Tasks

- [ ] **XX.1** [Specific task description]
  - Files: `path/to/file.cs`
  - Tests: Write test first, then implement
  - Success: [How to verify this task is complete]

- [ ] **XX.2** [Specific task description]
  - Files: `path/to/file.cs`
  - Tests: [Test approach]
  - Success: [Verification method]

[Continue with XX.3, XX.4, etc.]

## Quality Gate
- [ ] `dotnet build` passes
- [ ] `dotnet test` passes
- [ ] All tasks above completed
- [ ] Code review: no critical issues
- [ ] Git commit with message: "Phase XX: [Name]"

## After This Phase
Proceed to **Phase [XX+1]: [Name]**
```

## EXECUTION INSTRUCTIONS

1. First, call `create` with path `phases/_CONTEXT.md` and the context content
2. Then, call `create` with path `phases/_INDEX.md` and the index content  
3. Then, for EACH phase, call `create` with path `phases/phase-XX-name.md`

You MUST create at least 3 files minimum (_CONTEXT.md, _INDEX.md, and at least one phase file).

## PHASE DETAIL REQUIREMENTS

Each phase file MUST have:
- 3-10 specific, actionable tasks
- File paths for each task
- Test requirements for each task
- Clear success criteria
- Quality gate checklist

Do NOT create vague tasks like "implement the feature". Be specific: "Create ISkillRepository interface with GetByIdAsync, ListAsync, SaveAsync methods"
"""

DYNAMIC_PLAN_GENERATOR_PROMPT = """You are a plan generator that creates PHASED IMPLEMENTATION PLANS as SEPARATE FILES, adapted for the task type.

## YOUR TASK

You MUST create MULTIPLE FILES using the `create` tool. Do NOT put everything in one file.

## TASK TYPE ADAPTATION

The plan content and quality gates must match the task type:

### For "software-development" tasks:
- TDD approach, build commands, git commits, test runners
- Quality gates: tests pass, builds succeed, code review done

### For "non-software" tasks:
- Domain-appropriate methodology (analysis framework, writing process, research protocol)
- Deliverable-oriented phases, NOT implementation phases
- Quality gates: peer review, methodology validation, deliverable completeness
- NO references to: TDD, builds, git commits, test runners, CI/CD

### For "mixed" tasks:
- Code phases use code quality gates (TDD, builds, tests)
- Non-code phases use domain quality gates (peer review, methodology validation)
- Deliverable-oriented phase structure that integrates both

## TEAM ROSTER

The following team members will execute this plan:
{team_roster}

Reference team members by their @mentions in phase files where relevant (e.g., "This phase is primarily owned by @DataAnalyst with review from @DataReviewer").

## REQUIRED FILES

### File 1: `phases/_CONTEXT.md`
Global context that applies to ALL phases:

```markdown
# [Project Name] - Global Context

> **READ THIS FIRST** before working on any phase.

## Original Ask (Verbatim)
> [EXACT user prompt, word for word — do not paraphrase]

## Problem Statement
[What we're building/analyzing/writing and why — be specific and detailed]

## Approach
[Domain-appropriate methodology — TDD for code, analysis framework for data, etc.]

## Key Decisions
| Decision | Choice | Rationale |
|----------|--------|-----------|
| [Decision 1] | [Choice] | [Why] |

## Non-Negotiables
- [Things that MUST NOT change]

## Success Criteria
- [ ] [Measurable criterion 1]
- [ ] [Measurable criterion 2]

## Validation
[Domain-appropriate validation: build commands for code, review criteria for writing, etc.]

## Output Structure
- All deliverable files go in the **workspace root** (this directory's parent)
- `phases/` is for plan files only — do NOT write deliverables here
- `mandali-artifacts/` is for internal orchestration — do NOT write deliverables here
```

### File 2: `phases/_INDEX.md`
Phase tracking table (same structure as standard plan)

### Files 3+: `phases/phase-XX-name.md` (one per phase)
Each phase in its own file with domain-appropriate tasks and quality gates.

## DELIVERABLE OUTPUT PATHS

CRITICAL: All deliverable files (reports, analyses, profiles, summaries, etc.) MUST be written to the **workspace root directory** (the working directory), NOT inside `mandali-artifacts/` or `phases/`.

- `mandali-artifacts/` is reserved for internal orchestration files (conversation, decisions, debug logs). Agents must NEVER write deliverables there.
- `phases/` is reserved for plan files only (_CONTEXT.md, _INDEX.md, phase-*.md).
- Each phase file MUST specify concrete output file paths relative to the workspace root. Example: `Output: competitive-analysis-report.md` not `Output: mandali-artifacts/competitive-analysis-report.md`.

Include this rule in the `_CONTEXT.md` under a "## Output Structure" section so all agents see it.

## EXECUTION INSTRUCTIONS

1. First, call `create` with path `phases/_CONTEXT.md` and the context content
2. Then, call `create` with path `phases/_INDEX.md` and the index content
3. Then, for EACH phase, call `create` with path `phases/phase-XX-name.md`

You MUST create at least 3 files minimum. Each phase file MUST have 3-10 specific, actionable tasks with clear success criteria.

## CRITICAL: ORIGINAL PROMPT PINNING

The _CONTEXT.md file MUST include the user's EXACT original prompt in the "Original Ask (Verbatim)" section. Copy it WORD FOR WORD. This is the team's north star — every persona refers back to this to stay aligned with the user's actual intent.
"""


async def run_interview(client: CopilotClient, model: str, initial_prompt: str) -> dict:
    """Run interactive interview: generate questions upfront, walk through them, synthesize."""
    log("Starting AI Interviewer...", "AGENT")
    console.print(Panel(
        "I'll ask a few questions to understand what you want.\n"
        "The team will handle implementation details autonomously.",
        title="🎤 AI INTERVIEWER", border_style="cyan"
    ))
    
    session = await client.create_session(_build_session_config(model, INTERVIEWER_PROMPT))
    
    async def send_and_wait(prompt: str) -> str:
        """Send prompt and wait for response."""
        response_parts = []
        done = asyncio.Event()
        
        def on_event(event):
            if event.type.value == "assistant.message":
                response_parts.append(event.data.content)
            elif event.type.value == "session.idle":
                done.set()
        
        unsubscribe = session.on(on_event)
        try:
            await session.send({"prompt": prompt})
            await done.wait()
        finally:
            unsubscribe()
        
        return ''.join(response_parts)
    
    try:
        # Phase 1: Generate all questions upfront
        log("Generating interview questions...", "INFO")
        questions_prompt = INTERVIEWER_QUESTIONS_INSTRUCTION.format(prompt=initial_prompt)
        response = await send_and_wait(questions_prompt)
        
        # Parse questions from JSON
        questions = []
        try:
            json_start = response.find("[")
            json_end = response.rfind("]") + 1
            if json_start != -1 and json_end > json_start:
                questions = json.loads(response[json_start:json_end])
        except json.JSONDecodeError:
            # Try extracting from code block
            try:
                cb_start = response.find("```json")
                cb_end = response.find("```", cb_start + 7)
                if cb_start != -1 and cb_end != -1:
                    questions = json.loads(response[cb_start + 7:cb_end].strip())
            except (json.JSONDecodeError, ValueError):
                pass
        
        if not questions:
            log("Failed to generate questions, using fallback", "WARN")
            questions = [
                "What does 'done' look like for you? How will you know this is successful?",
                "Is there any existing code, project, or prior work to build on?",
                "Are there any specific preferences or constraints I should know about?"
            ]
        
        log(f"Generated {len(questions)} questions", "OK")
        
        # Phase 2: Walk through questions one at a time
        qa_pairs = []
        total = len(questions)
        
        for i, question in enumerate(questions, 1):
            console.print(f"\n{escape(question)}\n")
            answer = Prompt.ask(f"[bold cyan]>[/bold cyan] [dim](question {i} of {total})[/dim]").strip()
            
            if not answer:
                answer = "(no answer — use your best judgment)"
            
            qa_pairs.append({"question": question, "answer": answer})
        
        # Phase 3: Synthesize into structured summary
        log("Synthesizing interview results...", "INFO")
        qa_text = "\n".join(
            f"Q: {qa['question']}\nA: {qa['answer']}\n"
            for qa in qa_pairs
        )
        summary_prompt = INTERVIEWER_SUMMARY_INSTRUCTION.format(
            prompt=initial_prompt, qa_pairs=qa_text
        )
        response = await send_and_wait(summary_prompt)
        
        # Parse the JSON summary
        if "INTERVIEW_COMPLETE" in response:
            log("Interview complete", "OK")
            try:
                json_start = response.find("```json")
                json_end = response.find("```", json_start + 7)
                if json_start != -1 and json_end != -1:
                    json_str = response[json_start + 7:json_end].strip()
                    return json.loads(json_str)
            except json.JSONDecodeError:
                log("Failed to parse JSON summary, using raw", "WARN")
        
        return {"raw_summary": response, "qa_pairs": qa_pairs}
    
    finally:
        try:
            await session.destroy()
        except Exception:
            pass


async def generate_plan_from_interview(client: CopilotClient, model: str, 
                                        gathered_info: dict, initial_prompt: str,
                                        out_path: Path,
                                        classification: 'TaskClassification' = None,
                                        team_roster: list = None) -> str:
    """Generate a phased plan from interview data, adapted for task type."""
    log("Generating phased plan...", "AGENT")
    
    # Ensure output directory and phases subfolder exist
    out_path.mkdir(parents=True, exist_ok=True)
    phases_path = out_path / "phases"
    phases_path.mkdir(parents=True, exist_ok=True)
    
    # Choose prompt based on task type
    if classification and classification.task_type != "software-development":
        # Format team roster for the dynamic prompt
        roster_str = ""
        if team_roster:
            roster_str = "\n".join(f"- {m['mention']} ({m['name']} — {m.get('role', 'Doer')}, {m.get('domain', 'general')})" for m in team_roster)
        else:
            roster_str = "(Team roster not yet assembled)"
        
        system_prompt = DYNAMIC_PLAN_GENERATOR_PROMPT.replace("{team_roster}", roster_str)
    else:
        system_prompt = PLAN_GENERATOR_PROMPT
    
    # Plan generator needs file access to create phase files
    session = await client.create_session(
        _build_session_config(model, system_prompt, str(out_path))
    )
    
    # Build a detailed prompt with existing context if available
    existing_context = ""
    if gathered_info.get("existing_context_files"):
        existing_context = "\n## Existing Context Files to Read First\n"
        for f in gathered_info["existing_context_files"]:
            existing_context += f"- {f}\n"
    
    existing_phases = ""
    if gathered_info.get("existing_phase_files"):
        existing_phases = "\n## Existing Phase Files (already exist, update _INDEX.md status)\n"
        for f in gathered_info["existing_phase_files"]:
            existing_phases += f"- {f}\n"
    
    completed = ""
    if gathered_info.get("completed_phases"):
        completed = f"\n## Already Completed Phases: {', '.join(gathered_info['completed_phases'])}\n"
    
    resume_stop = ""
    if gathered_info.get("resume_from_phase"):
        resume_stop += f"\n## Resume from: {gathered_info['resume_from_phase']}\n"
    if gathered_info.get("stop_after_phase"):
        resume_stop += f"## STOP after: {gathered_info['stop_after_phase']} (mark this clearly in _INDEX.md)\n"
    
    classification_context = ""
    if classification:
        classification_context = f"""
## Task Classification
- **Type**: {classification.task_type}
- **Domains**: {', '.join(d['name'] for d in classification.domains)}
"""
    
    prompt = f"""
Generate a PHASED {'implementation' if not classification or classification.task_type == 'software-development' else 'execution'} plan with SEPARATE FILES.

## Original Request (include this VERBATIM in _CONTEXT.md under "Original Ask (Verbatim)")
{initial_prompt}

## Gathered Information
{json.dumps(gathered_info, indent=2)}
{existing_context}{existing_phases}{completed}{resume_stop}{classification_context}

## CRITICAL INSTRUCTIONS

You MUST create the following files using the `create` tool:

1. **`phases/_CONTEXT.md`** - Global context file with:
   - Problem statement
   - Architecture decisions
   - Security requirements
   - Non-negotiables
   - Success criteria

2. **`phases/_INDEX.md`** - Phase tracking table with:
   - Table of all phases with status
   - Phase dependencies diagram
   - Links to phase files

3. **`phases/phase-XX-name.md`** - One file PER PHASE with:
   - Phase goal
   - Detailed tasks numbered XX.1, XX.2, etc.
   - File paths for each task
   - Quality gates
   - "After This Phase" section

Create EACH file separately using the `create` tool. Do NOT put everything in one file.

The working directory is: {out_path}
Create files in the `phases/` subfolder.

START by creating `phases/_CONTEXT.md`, then `phases/_INDEX.md`, then each phase file.
"""
    
    done = asyncio.Event()
    
    def on_event(event):
        if event.type.value == "tool.execution_start":
            tool_name = getattr(event.data, 'tool_name', None)
            args = getattr(event.data, 'arguments', None)
            if tool_name == "create" and args:
                file_path = args.get('path', '') if isinstance(args, dict) else ''
                if file_path:
                    log(f"Creating {Path(file_path).name}...", "INFO")
        elif event.type.value == "tool.execution_complete":
            tool_name = getattr(event.data, 'tool_name', None)
            args = getattr(event.data, 'arguments', None)
            if tool_name == "create" and args:
                file_path = args.get('path', '') if isinstance(args, dict) else ''
                if file_path:
                    log(f"Created {Path(file_path).name} ✓", "OK")
        elif event.type.value == "session.idle":
            done.set()
    
    session.on(on_event)
    try:
        await session.send({"prompt": prompt})
        await done.wait()
    finally:
        await session.destroy()
    
    # Read the created plan files and combine for review
    plan_content = ""
    
    context_file = phases_path / "_CONTEXT.md"
    if context_file.exists():
        plan_content += f"# === _CONTEXT.md ===\n\n{context_file.read_text(encoding='utf-8')}\n\n"
    
    index_file = phases_path / "_INDEX.md"
    if index_file.exists():
        plan_content += f"# === _INDEX.md ===\n\n{index_file.read_text(encoding='utf-8')}\n\n"
    
    # Read all phase files
    phase_files = sorted(phases_path.glob("phase-*.md"))
    for pf in phase_files:
        plan_content += f"# === {pf.name} ===\n\n{pf.read_text(encoding='utf-8')}\n\n"
    
    if plan_content:
        log(f"Generated phased plan with {len(phase_files)} phase files", "OK")
        return plan_content
    else:
        # Fallback: check if a single plan.md was created instead
        plan_file = out_path / "plan.md"
        if plan_file.exists():
            log("Warning: Single plan.md created instead of phased structure", "WARN")
            return plan_file.read_text(encoding='utf-8')
        
        log("No plan files created", "WARN")
        return ""


async def convert_to_phased_plan(client: CopilotClient, model: str,
                                  plan_content: str, out_path: Path) -> str:
    """Convert a non-phased plan into the standard phased structure.
    
    Takes any plan format (flat doc, bullet list, PRD, etc.) and generates
    _CONTEXT.md, _INDEX.md, and phase-XX-name.md files so the agent
    workflow (phase transitions, quality gates, tracking) works correctly.
    """
    log("Converting plan to phased structure...", "INFO")
    
    phases_path = out_path / "phases"
    phases_path.mkdir(parents=True, exist_ok=True)
    
    session = await client.create_session(
        _build_session_config(model, PLAN_GENERATOR_PROMPT, str(out_path))
    )
    
    prompt = f"""
Convert the following plan into a PHASED implementation structure with SEPARATE FILES.

## Original Plan Content
{plan_content}

## CRITICAL INSTRUCTIONS

The plan above may not be in phased format. Your job is to:
1. Understand the intent and requirements from the plan
2. Restructure it into logical phases with clear dependencies
3. Preserve ALL original requirements — do not drop anything
4. Add quality gates and test requirements for each phase

You MUST create the following files using the `create` tool:

1. **`phases/_CONTEXT.md`** - Global context extracted from the plan:
   - Problem statement, architecture decisions, security requirements
   - Non-negotiables, success criteria

2. **`phases/_INDEX.md`** - Phase tracking table:
   - Table of all phases with status (all ⏳ Not Started)
   - Phase dependencies diagram
   - Links to phase files

3. **`phases/phase-XX-name.md`** - One file PER PHASE with:
   - Phase goal, detailed tasks (numbered XX.1, XX.2, etc.)
   - File paths for each task, quality gates

Create EACH file separately using the `create` tool.
The working directory is: {out_path}
Create files in the `phases/` subfolder.

START by creating `phases/_CONTEXT.md`, then `phases/_INDEX.md`, then each phase file.
"""
    
    done = asyncio.Event()
    
    def on_event(event):
        if event.type.value == "tool.execution_start":
            tool_name = getattr(event.data, 'tool_name', None)
            args = getattr(event.data, 'arguments', None)
            if tool_name == "create" and args:
                file_path = args.get('path', '') if isinstance(args, dict) else ''
                if file_path:
                    log(f"Creating {Path(file_path).name}...", "INFO")
        elif event.type.value == "tool.execution_complete":
            tool_name = getattr(event.data, 'tool_name', None)
            args = getattr(event.data, 'arguments', None)
            if tool_name == "create" and args:
                file_path = args.get('path', '') if isinstance(args, dict) else ''
                if file_path:
                    log(f"Created {Path(file_path).name} ✓", "OK")
        elif event.type.value == "session.idle":
            done.set()
    
    session.on(on_event)
    try:
        await session.send({"prompt": prompt})
        await done.wait()
    finally:
        await session.destroy()
    
    # Read the created plan files
    result_content = ""
    
    context_file = phases_path / "_CONTEXT.md"
    if context_file.exists():
        result_content += f"# === _CONTEXT.md ===\n\n{context_file.read_text(encoding='utf-8')}\n\n"
    
    index_file = phases_path / "_INDEX.md"
    if index_file.exists():
        result_content += f"# === _INDEX.md ===\n\n{index_file.read_text(encoding='utf-8')}\n\n"
    
    phase_files = sorted(phases_path.glob("phase-*.md"))
    for pf in phase_files:
        result_content += f"# === {pf.name} ===\n\n{pf.read_text(encoding='utf-8')}\n\n"
    
    if result_content:
        log(f"Converted to phased plan with {len(phase_files)} phases", "OK")
        return result_content
    
    # Conversion failed — return original content as fallback
    log("Phased conversion failed, using original plan", "WARN")
    return plan_content


# ============================================================================
# Plan Artifact Discovery (skip-planning default flow)
# ============================================================================

async def extract_plan_paths(client: CopilotClient, model: str, prompt: str) -> list[Path]:
    """Use LLM to extract file/folder paths mentioned in a prompt."""
    log("Extracting file references from prompt...", "INFO")
    
    session = await client.create_session(_build_session_config(model,
        "You extract file and folder paths from text. "
        "Return ONLY a JSON array of strings. No explanation, no markdown fencing. "
        "Example: [\"phases/_INDEX.md\", \"docs/architecture.md\", \"src/Services/\"]"
    ))
    
    response_parts = []
    done = asyncio.Event()
    
    def on_event(event):
        if event.type.value == "assistant.message":
            response_parts.append(event.data.content)
        elif event.type.value == "session.idle":
            done.set()
    
    session.on(on_event)
    try:
        await session.send({"prompt": (
            f"Extract ALL file and folder paths from this text:\n\n{prompt}\n\n"
            "Include paths in backticks, quotes, or mentioned inline. "
            "Return as JSON array of strings."
        )})
        await done.wait()
    finally:
        await session.destroy()
    
    raw = ''.join(response_parts).strip()
    
    # Strip markdown fencing if present
    if raw.startswith("```"):
        raw = raw.split("\n", 1)[1] if "\n" in raw else raw[3:]
        if raw.endswith("```"):
            raw = raw[:-3]
        raw = raw.strip()
    
    try:
        paths_strs = json.loads(raw)
    except json.JSONDecodeError:
        log(f"Failed to parse LLM path extraction response: {raw[:200]}", "WARN")
        return []
    
    if not isinstance(paths_strs, list):
        return []
    
    # Resolve and validate paths
    valid_paths = []
    cwd = Path.cwd()
    for p_str in paths_strs:
        p = Path(p_str)
        # Try relative to cwd first, then absolute
        resolved = (cwd / p) if not p.is_absolute() else p
        if resolved.exists():
            valid_paths.append(resolved.resolve())
            log(f"  Found: {p_str}", "INFO")
        else:
            log(f"  Not found: {p_str} (skipped)", "WARN")
    
    return valid_paths


async def discover_plan_artifacts(
    client: CopilotClient, model: str, initial_paths: list[Path]
) -> list[Path]:
    """Recursively discover plan artifacts using LLM to read file contents.
    
    Reads each file, asks LLM to find referenced files/folders in the content,
    then follows those references up to 5 levels deep.
    """
    all_artifacts: set[Path] = set()
    pending_paths: list[Path] = list(initial_paths)
    
    for depth in range(1, 6):
        if not pending_paths:
            break
        
        # Collect files to read at this depth
        files_to_read: list[Path] = []
        for p in pending_paths:
            if p.is_dir():
                # Read all files in directory (not just .md)
                for f in sorted(p.iterdir()):
                    if f.is_file() and f not in all_artifacts:
                        files_to_read.append(f)
                        all_artifacts.add(f)
            elif p.is_file() and p not in all_artifacts:
                files_to_read.append(p)
                all_artifacts.add(p)
        
        if not files_to_read:
            break
        
        log(f"🔍 Discovering plan artifacts (depth {depth}/5)... reading {len(files_to_read)} files", "INFO")
        
        # Build combined content for LLM
        combined_content = ""
        for f in files_to_read:
            try:
                content = f.read_text(encoding='utf-8')
                combined_content += f"\n\n--- FILE: {f} ---\n{content}"
            except (UnicodeDecodeError, IOError):
                pass  # Skip binary/unreadable files
        
        if not combined_content.strip():
            break
        
        # Ask LLM to find referenced files
        session = await client.create_session(_build_session_config(model,
            "You analyze plan/context documents and extract file/folder paths referenced within. "
            "Return ONLY a JSON array of strings. No explanation, no markdown fencing. "
            "Look for paths in backticks, quotes, relative references, folder structures, "
            "links, and prose descriptions. Include any file or folder an implementer would need."
        ))
        
        response_parts = []
        done = asyncio.Event()
        
        def on_event(event):
            if event.type.value == "assistant.message":
                response_parts.append(event.data.content)
            elif event.type.value == "session.idle":
                done.set()
        
        session.on(on_event)
        try:
            await session.send({"prompt": (
                f"Extract ALL file and folder paths referenced in these documents:\n"
                f"{combined_content[:50000]}\n\n"  # Cap to avoid token limits
                "Return as JSON array of strings."
            )})
            await done.wait()
        finally:
            await session.destroy()
        
        raw = ''.join(response_parts).strip()
        if raw.startswith("```"):
            raw = raw.split("\n", 1)[1] if "\n" in raw else raw[3:]
            if raw.endswith("```"):
                raw = raw[:-3]
            raw = raw.strip()
        
        try:
            new_path_strs = json.loads(raw)
        except json.JSONDecodeError:
            log(f"  Could not parse LLM response at depth {depth}", "WARN")
            break
        
        if not isinstance(new_path_strs, list):
            break
        
        # Resolve new paths and find ones we haven't seen
        cwd = Path.cwd()
        new_paths = []
        for p_str in new_path_strs:
            p = Path(p_str)
            resolved = (cwd / p) if not p.is_absolute() else p
            if resolved.exists():
                resolved = resolved.resolve()
                if resolved not in all_artifacts:
                    new_paths.append(resolved)
        
        if new_paths:
            log(f"  Found {len(new_paths)} new files/folders", "INFO")
            pending_paths = new_paths
        else:
            log(f"  No new references found, stopping discovery", "INFO")
            break
    
    total = len(all_artifacts)
    log(f"✅ Discovery complete: {total} total files", "OK")
    return sorted(all_artifacts)


def copy_plan_artifacts(artifacts: list[Path], workspace: Workspace) -> list[tuple[Path, Path, int]]:
    """Copy discovered plan artifacts to workspace.
    
    Returns list of (source, destination, size_bytes) tuples.
    """
    workspace.ensure_exists()
    copied: list[tuple[Path, Path, int]] = []
    
    # Check if artifacts form a phased structure
    artifact_names = {a.name for a in artifacts}
    is_phased = '_INDEX.md' in artifact_names or '_CONTEXT.md' in artifact_names
    
    for src in artifacts:
        # Only copy markdown files — code files are redundant since agents
        # discover them from the codebase. MD files provide deterministic
        # access to plan details.
        if src.suffix.lower() != '.md':
            continue
        
        size = src.stat().st_size
        
        if is_phased and src.name in ('_INDEX.md', '_CONTEXT.md'):
            dst = workspace.phases_path / src.name
        elif is_phased and src.name.startswith('phase-'):
            dst = workspace.phases_path / src.name
        elif is_phased and src.parent.name == 'phases':
            # Other files in phases/ directory
            dst = workspace.phases_path / src.name
        else:
            # Non-phase files go to artifacts directory
            dst = workspace.artifacts_path / src.name
        
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src, dst)
        copied.append((src, dst, size))
    
    return copied


# ============================================================================
# Plan Review
# ============================================================================

PLAN_REVIEWER_PROMPT = """You are a Plan Reviewer ensuring PHASED PLANS are ready for UNSUPERVISED AI agent execution.

IMPORTANT: Do NOT use any tools. Provide your review as text in your response.

## PHASED PLAN STRUCTURE CHECK

The plan should consist of multiple files:
1. `phases/_CONTEXT.md` - Global context (architecture, security, non-negotiables)
2. `phases/_INDEX.md` - Phase tracking table with status and dependencies
3. `phases/phase-XX-name.md` - Individual phase files with detailed tasks

## Review Criteria

### Structure
- [ ] _CONTEXT.md exists with global context (not phase-specific details)
- [ ] _INDEX.md exists with phase tracking table
- [ ] Each phase has its own file
- [ ] Tasks are numbered as XX.Y (phase.task)
- [ ] Each phase has quality gates
- [ ] Phase dependencies are clear

### Completeness
- [ ] Problem statement is clear
- [ ] Success criteria are measurable
- [ ] Each phase has a clear goal
- [ ] Tasks are actionable (not vague)
- [ ] "STOP after phase X" is specified if applicable
- [ ] Context files to read are listed

### TDD + PoC Pattern
- [ ] Phases progress from simple to complex
- [ ] Tests are mentioned for each task
- [ ] Quality gates include build + test verification

### Resumability
- [ ] If resuming from existing work, completed phases are marked
- [ ] _INDEX.md shows current status accurately
- [ ] Dependencies are correct

Output one of:
- PLAN_APPROVED (if ready for autonomous execution)
- PLAN_NEEDS_REVISION (include specific issues to fix)
- PLAN_NEEDS_CLARIFICATION (list specific questions that must be answered)

Write your response directly - do not create files.
"""


async def review_plan(client: CopilotClient, model: str, plan_content: str) -> tuple[str, str]:
    """Review plan for unsupervised execution readiness."""
    log("Reviewing plan...", "INFO")
    
    session = await client.create_session(_build_session_config(model, PLAN_REVIEWER_PROMPT))
    
    response_parts = []
    done = asyncio.Event()
    
    def on_event(event):
        if event.type.value == "assistant.message":
            response_parts.append(event.data.content)
        elif event.type.value == "session.idle":
            done.set()
    
    session.on(on_event)
    try:
        await session.send({"prompt": f"Review this plan:\n\n{plan_content}"})
        await done.wait()
    finally:
        await session.destroy()
    
    response = ''.join(response_parts)
    
    if "PLAN_APPROVED" in response:
        return "approved", response
    elif "PLAN_NEEDS_CLARIFICATION" in response:
        return "needs_clarification", response.split("PLAN_NEEDS_CLARIFICATION", 1)[1].strip()
    elif "PLAN_NEEDS_REVISION" in response:
        return "needs_revision", response.split("PLAN_NEEDS_REVISION", 1)[1].strip()
    return "needs_revision", response


# ============================================================================
# Orchestrator (Passive Monitor)
# ============================================================================

class AutonomousOrchestrator:
    """Passive orchestrator that monitors autonomous agents."""
    
    def __init__(self, config: dict, verbose: bool = False):
        self.config = config
        self.verbose = verbose
        self.client: Optional[CopilotClient] = None
        self.agents: Dict[str, PersonaAgent] = {}
        self.model = config['orchestrator'].get('model', 'claude-sonnet-4')
        self.metrics = Metrics()
        self._cli_path: Optional[str] = None
        self._workspace: Optional['Workspace'] = None
        self._plan_content: Optional[str] = None
        self._user_intent: Optional[str] = None
        self.teams_bridge = None
        self.teams_thread_id = None
    
    async def start(self):
        log("Starting Copilot client...", "INFO")
        # Let SDK use its bundled CLI binary (most reliable).
        # Fall back to explicit path only if COPILOT_CLI_PATH is set.
        env_path = os.environ.get("COPILOT_CLI_PATH")
        if env_path:
            self._cli_path = env_path
            log(f"Using CLI at: {self._cli_path}", "INFO")
        else:
            self._cli_path = None
            log("Using SDK bundled CLI", "INFO")
        await self._connect_with_retry()
        log("Copilot client ready", "OK")
    
    async def _connect_with_retry(self, max_retries: int = 3):
        """Connect to Copilot CLI with retry logic for transient failures."""
        last_error = None
        for attempt in range(1, max_retries + 1):
            if self._cli_path:
                self.client = CopilotClient({"cli_path": self._cli_path})
            else:
                self.client = CopilotClient()
            try:
                await self.client.start()
                return
            except (TimeoutError, asyncio.TimeoutError, RuntimeError, ConnectionError, OSError) as e:
                last_error = e
                try:
                    await self.client.stop()
                except Exception:
                    pass
                if attempt < max_retries:
                    wait = attempt * 5
                    log(f"Copilot CLI connection failed (attempt {attempt}/{max_retries}): {e}", "WARN")
                    log(f"Retrying in {wait}s...", "INFO")
                    await asyncio.sleep(wait)
        self.client = None
        raise RuntimeError(
            f"Copilot CLI failed to connect after {max_retries} attempts. "
            f"Last error: {last_error}"
        )
    
    async def restart(self):
        """Tear down the current client and reconnect (e.g. after connection loss)."""
        log("Reconnecting to Copilot CLI...", "WARN")
        if self.client:
            try:
                await self.client.stop()
            except Exception:
                pass
            self.client = None
        await self._connect_with_retry()
        log("Reconnected to Copilot CLI", "OK")
    
    async def ensure_client(self):
        """Verify the client is alive; restart if it has died."""
        if not self.client:
            await self.restart()
            return
        try:
            await self.client.ping()
        except Exception:
            await self.restart()
    
    async def stop_agents(self):
        """Stop all agent tasks/sessions but keep the client alive for relaunch."""
        for agent in self.agents.values():
            if agent.task:
                agent.task.cancel()
            if agent.session:
                try:
                    await agent.session.destroy()
                except Exception:
                    pass
        self.agents.clear()
    
    async def stop(self):
        log("Stopping all agents...", "INFO")
        await self.stop_agents()
        if self.client:
            await self.client.stop()
        log("Shutdown complete", "OK")
    
    async def launch_agents(self, workspace: Workspace, plan_content: str,
                             team_roster: list = None):
        """Launch all agents as background tasks.
        
        If team_roster is provided, uses it instead of config['personas'].
        team_roster entries: {id, name, mention, promptFile, dynamic, domain, role}
        """
        self._workspace = workspace
        self._plan_content = plan_content
        
        # Use team roster if provided, otherwise fall back to config
        if team_roster:
            personas = team_roster
        else:
            personas = []
            for p in self.config.get('personas', []):
                personas.append({
                    'id': p['id'],
                    'name': p['name'],
                    'mention': f"@{p['name']}",
                    'promptFile': str(SCRIPT_DIR / p['promptFile']),
                    'dynamic': False,
                    'domain': 'software-development',
                    'role': 'Doer',
                })
        
        team_size = len(personas)
        
        # Store for relaunch recovery
        self._team_roster = personas
        self._team_size = team_size
        
        # Query actual available models from SDK and show active model
        try:
            models = await self.client.list_models()
            active = next((m for m in models if m.id == self.model), None)
            if active:
                multiplier = f"{active.billing.multiplier}x" if active.billing else "?"
                log(f"Model: {active.name} ({active.id}) [{multiplier}]", "OK")
            else:
                log(f"Model: {self.model} (not found in available models)", "WARN")
                available = ", ".join(m.id for m in models if m.policy and m.policy.state == "enabled")
                if available:
                    log(f"Available: {available}", "INFO")
        except Exception as e:
            log(f"Model: {self.model} (could not query models: {e})", "WARN")
        
        mcp_count = len(MCP_SERVERS_CONFIG) if MCP_SERVERS_CONFIG else 0
        if mcp_count:
            log(f"MCP servers: {mcp_count} ({', '.join(MCP_SERVERS_CONFIG.keys())})", "INFO")
        
        for i, persona in enumerate(personas):
            agent = PersonaAgent(
                id=persona['id'],
                name=persona['name'],
                mention=persona.get('mention', f"@{persona['id'].capitalize()}"),
                prompt_file=persona.get('promptFile') if persona.get('dynamic') else None,
                dynamic=persona.get('dynamic', False),
                domain=persona.get('domain'),
            )
            
            # Launch as background task
            agent.task = asyncio.create_task(
                run_autonomous_agent(
                    self.client, agent, workspace, plan_content,
                    self.model, is_first=(i == 0),
                    team_roster=personas, team_size=team_size
                )
            )
            
            self.agents[persona['id']] = agent
            dynamic_tag = " [dynamic]" if agent.dynamic else ""
            log(f"Launched {agent.mention}{dynamic_tag}", "AGENT")
            
            # Stagger launches slightly
            await asyncio.sleep(2)
    
    def get_latest_activity_summary(self, workspace: Workspace, last_shown_pos: int) -> tuple[list[str], int]:
        """Get recent conversation messages since last_shown_pos.
        
        Returns a list of formatted message lines (one per message) and the new position.
        Multi-line messages are collapsed to their first meaningful line.
        """
        content = read_conversation(workspace)
        new_content = content[last_shown_pos:]
        
        if not new_content.strip():
            return [], last_shown_pos
        
        # Parse messages: each starts with [HH:MM:SS] @SENDER:
        messages = re.findall(
            r'\[(\d{2}:\d{2}:\d{2})\]\s+@(\w+):\s*(.*?)(?=\n\[|\Z)',
            new_content, re.DOTALL
        )
        
        if not messages:
            return [], len(content)
        
        # Format each message: take first non-empty line, truncate
        formatted = []
        max_msg_len = 120
        for _time, sender, body in messages:
            # Collapse multi-line body to first meaningful line
            first_line = ""
            for line in body.strip().splitlines():
                line = line.strip()
                if line and not line.startswith('---') and not line.startswith('```'):
                    first_line = line
                    break
            if not first_line:
                first_line = "(no text)"
            if len(first_line) > max_msg_len:
                first_line = first_line[:max_msg_len - 3] + "..."
            formatted.append(f"[dim]{_time}[/dim] [bold]{sender}[/bold]: {escape(first_line)}")
        
        # Keep at most last 8 messages to avoid flooding
        return formatted[-8:], len(content)
    
    async def check_user_input(self) -> Optional[str]:
        """Non-blocking check for user input with timeout."""
        import sys
        
        # Windows doesn't support select on stdin, use msvcrt
        if sys.platform == 'win32':
            import msvcrt
            if msvcrt.kbhit():
                # Read the line with timeout
                line = ""
                timeout_counter = 0
                max_timeout = 500  # 5 seconds max wait for Enter
                
                while timeout_counter < max_timeout:
                    if msvcrt.kbhit():
                        timeout_counter = 0  # Reset on keypress
                        char = msvcrt.getwch()
                        if char == '\r' or char == '\n':
                            print()  # newline
                            line = line.strip()
                            return line if line else None
                        elif char == '\x08':  # backspace
                            if line:
                                line = line[:-1]
                                print('\b \b', end='', flush=True)
                        elif char == '\x1b':  # Escape - cancel input
                            print(" (cancelled)")
                            return None
                        elif char in ('\x00', '\xe0'):  # Special key prefix (arrows, function keys) - consume scan code
                            if msvcrt.kbhit():
                                msvcrt.getwch()  # discard scan code
                        else:
                            line += char
                            print(char, end='', flush=True)
                    else:
                        await asyncio.sleep(0.01)
                        timeout_counter += 1
                
                # Timeout - return what we have
                line = line.strip()
                if line:
                    print()
                    return line
                return None
            return None
        else:
            # Unix: use select
            import select
            if select.select([sys.stdin], [], [], 0)[0]:
                return sys.stdin.readline().strip() or None
            return None
    
    async def _check_and_recover_agents(self):
        """Detect crashed agent tasks and relaunch them automatically."""
        for agent_id, agent in list(self.agents.items()):
            if agent.task and agent.task.done():
                exc = agent.task.exception() if not agent.task.cancelled() else None
                if exc:
                    log(f"{agent.mention} crashed: {exc}", "WARN")
                else:
                    # Task finished normally (e.g. victory signal) — skip relaunch
                    continue
                
                # Clean up dead session
                if agent.session:
                    try:
                        await agent.session.destroy()
                    except Exception:
                        pass
                    agent.session = None
                
                # Ensure client is still alive before relaunching
                try:
                    await self.ensure_client()
                except Exception as e:
                    log(f"Cannot recover {agent.mention}: client reconnect failed ({e})", "ERR")
                    continue
                
                # Relaunch the agent
                log(f"Relaunching {agent.mention}...", "INFO")
                is_first = (agent_id == list(self.agents.keys())[0])
                agent.task = asyncio.create_task(
                    run_autonomous_agent(
                        self.client, agent, self._workspace, self._plan_content,
                        self.model, is_first=is_first,
                        team_roster=getattr(self, '_team_roster', None),
                        team_size=getattr(self, '_team_size', None)
                    )
                )
                log(f"{agent.mention} relaunched", "OK")
    
    async def monitor_loop(self, workspace: Workspace, max_stall_minutes: int = 5,
                           is_final_round: bool = True, round_number: int = 1, total_rounds: int = 1):
        """
        Interactive passive monitoring loop.
        - Shows periodic status updates with phase progress ticker
        - Accepts user input at any time
        - Nudges agents if inactive (up to 3 times before human escalation)
        - Checks for victory and stalls
        """
        expected_agents = list(self.agents.keys())
        stall_timeout = max_stall_minutes * 60
        last_shown_pos = 0
        update_interval = 30  # Show update every 30 seconds
        last_update_time = datetime.now()
        nudge_count = 0  # Track consecutive nudges
        max_nudges = 3  # Escalate to human after 3 nudges
        
        # Decision tracking nudge state
        last_phase_check_pos = 0  # Track conversation position for phase detection
        decisions_mtime = workspace.decisions_file.stat().st_mtime if workspace.decisions_file.exists() else 0
        
        console.print(Panel(
            "Type a message to interject, Ctrl+C to abort",
            title="📡 MONITORING", border_style="bright_blue"
        ))
        
        # Upfront expectation: show plan scope and verification info
        phase_info = self._parse_phase_progress(workspace)
        if phase_info:
            total_phases = phase_info.split('/')[-1].split(' ')[0] if '/' in phase_info else '?'
            verify_note = " | Expect 1-2 verification rounds" if total_rounds > 1 else ""
            round_note = f" | Round {round_number}/{total_rounds}" if total_rounds > 1 else ""
            console.print(f"  [dim]📋 Plan: {total_phases} phases{round_note}{verify_note}[/dim]")
        
        # Track last known phase state for ticker updates
        last_phase_ticker = ""
        
        # Satisfaction reconciliation state
        last_reconciliation_time = datetime.now()
        reconciliation_cooldown = 300  # 5 minutes between reconciliation attempts
        monitor_start_time = datetime.now()  # When monitoring began
        
        # Human-blocked detection state (independent of stall timeout)
        first_human_block_time = None  # When we first noticed agents blocked on human
        human_block_grace = 300  # 5 minutes grace before escalating
        
        while True:
            # Non-blocking sleep with input check
            for _ in range(POLL_INTERVAL_SECONDS * 10):
                await asyncio.sleep(0.1)
                
                # Check for user input
                user_input = await self.check_user_input()
                if user_input:
                    # Inject user message
                    append_to_conversation(workspace, "HUMAN", f"""
@AllAgents - Human says:

{user_input}
""")
                    log(f"Your message injected to conversation", "HUMAN")
                    nudge_count = 0  # Reset nudge count on human input
            
            # Check victory
            if check_all_satisfied(workspace, expected_agents):
                log("🎉 All agents SATISFIED - Victory!", "OK")
                await self.announce_victory(workspace, is_final=is_final_round)
                self.metrics.victory = True
                return True
            
            # Check agent health — restart any that crashed
            await self._check_and_recover_agents()
            
            # Satisfaction reconciliation: proactively poll agents when evidence
            # suggests work is done but not all agents have declared SATISFIED
            now = datetime.now()
            since_last_reconciliation = (now - last_reconciliation_time).total_seconds()
            if since_last_reconciliation >= reconciliation_cooldown:
                phases = self._parse_phase_list(workspace)
                all_phases_done = phases and all(
                    '✅' in p[2] or 'complete' in p[2].lower() for p in phases
                )
                status = read_all_satisfaction(workspace)
                no_blocked = not any('BLOCKED' in s for s in status.values())
                # Prolonged activity: monitor running >10min AND recent activity (not stalled)
                last_activity = get_last_activity_time(workspace)
                monitor_running = (now - monitor_start_time).total_seconds() > 600
                recently_active = (now - last_activity).total_seconds() < stall_timeout
                prolonged_activity = monitor_running and recently_active
                
                if all_phases_done or (prolonged_activity and no_blocked):
                    log("Running satisfaction reconciliation...", "INFO")
                    await self._reconcile_satisfaction(workspace)
                    last_reconciliation_time = datetime.now()
                    
                    # Re-check victory after reconciliation
                    if check_all_satisfied(workspace, expected_agents):
                        log("🎉 All agents SATISFIED after reconciliation - Victory!", "OK")
                        await self.announce_victory(workspace, is_final=is_final_round)
                        self.metrics.victory = True
                        return True
            
            # Independent human-blocked detection (not gated behind stall timeout)
            status = read_all_satisfaction(workspace)
            blocked_on_human = [aid for aid, s in status.items()
                                if "@HUMAN" in s or "human" in s.lower()]
            
            if blocked_on_human:
                if first_human_block_time is None:
                    first_human_block_time = datetime.now()
                    log(f"Agent(s) requesting human input: {', '.join(blocked_on_human)}", "INFO")
                elif (datetime.now() - first_human_block_time).total_seconds() > human_block_grace:
                    log(f"Agents blocked on human for >5 min, escalating", "WARN")
                    should_continue = await self.handle_human_escalation(workspace)
                    if not should_continue:
                        return False
                    first_human_block_time = None
                    nudge_count = 0
            else:
                first_human_block_time = None  # Reset if no longer blocked on human
            
            # Check for inactivity
            last_activity = get_last_activity_time(workspace)
            idle_seconds = (datetime.now() - last_activity).total_seconds()
            
            if idle_seconds > stall_timeout:
                if nudge_count < max_nudges:
                    # Nudge agents to continue
                    nudge_count += 1
                    self.metrics.nudges += 1
                    log(f"Nudging agents (attempt {nudge_count}/{max_nudges})", "INFO")
                    
                    # Phase-aware nudge: check _INDEX.md to craft a targeted message
                    phases = self._parse_phase_list(workspace)
                    all_phases_done = phases and all(
                        '✅' in p[2] or 'complete' in p[2].lower() for p in phases
                    )
                    
                    if all_phases_done:
                        append_to_conversation(workspace, "ORCHESTRATOR", f"""
@Team - All phases appear complete per _INDEX.md but not all agents have declared SATISFIED.

If your concerns are addressed, declare SATISFACTION_STATUS: SATISFIED on its own line.
If something remains, state what's needed with SATISFACTION_STATUS: BLOCKED - [reason] or WORKING.

Nudge {nudge_count}/{max_nudges} before human escalation.
""")
                    else:
                        append_to_conversation(workspace, "ORCHESTRATOR", f"""
@Team - No activity detected for {int(idle_seconds // 60)} minutes.

Please continue working on the plan. If you're blocked, state what you need. End your next message with your status on its own line, exactly like:
SATISFACTION_STATUS: WORKING
or SATISFIED, BLOCKED - [reason], PAUSED.

Nudge {nudge_count}/{max_nudges} before human escalation.
""")
                else:
                    # Max nudges reached - escalate to human
                    log(f"Max nudges reached, escalating to human", "WARN")
                    should_continue = await self.handle_human_escalation(workspace)
                    if not should_continue:
                        return False
                    nudge_count = 0  # Reset after human intervention
            else:
                # Activity detected - reset nudge count
                if nudge_count > 0:
                    nudge_count = 0
            
            # Show periodic status update
            now = datetime.now()
            if (now - last_update_time).total_seconds() >= update_interval:
                last_update_time = now
                
                # Get latest activity
                recent_messages, last_shown_pos = self.get_latest_activity_summary(workspace, last_shown_pos)
                status = read_all_satisfaction(workspace)
                
                # Build status line
                status_icons = []
                for agent_id, agent_status in status.items():
                    if "SATISFIED" in agent_status:
                        status_icons.append(f"✅{agent_id[:3]}")
                    elif "BLOCKED" in agent_status:
                        status_icons.append(f"🔴{agent_id[:3]}")
                    elif "WORKING" in agent_status:
                        status_icons.append(f"🔧{agent_id[:3]}")
                    else:
                        status_icons.append(f"⏳{agent_id[:3]}")
                
                status_line = " ".join(status_icons)
                msgs = read_conversation(workspace).count("\n[")  # Count message lines
                
                # Build phase ticker from _INDEX.md
                phase_ticker = self._build_phase_ticker(workspace)
                
                # Print status header
                timestamp = now.strftime("%H:%M:%S")
                status_table = Table(show_header=False, box=None, padding=(0, 1))
                status_table.add_row(f"[dim]{timestamp}[/dim]", "📊", status_line, f"[dim]{msgs} msgs[/dim]")
                console.print(status_table)
                
                # Print phase ticker if it changed
                if phase_ticker and phase_ticker != last_phase_ticker:
                    console.print(f"  [dim]📋[/dim] {phase_ticker}")
                    last_phase_ticker = phase_ticker
                
                # Print recent conversation activity
                if recent_messages:
                    for msg_line in recent_messages:
                        console.print(f"  [dim]│[/dim] {msg_line}")
            
            # Update metrics
            self.metrics.total_messages = read_conversation(workspace).count("\n[")
            
            # Check for phase completions — match any agent announcing completion
            conversation_content = read_conversation(workspace)
            new_conversation = conversation_content[last_phase_check_pos:]
            if new_conversation:
                phase_completions = re.findall(
                    r'\[[\d:]+\]\s+@\w+:.*?Phase\s+\d+\S*\s+[Cc]omplete',
                    new_conversation, re.DOTALL
                )
                if phase_completions:
                    last_phase_check_pos = len(conversation_content)
                    
                    # Parse _INDEX.md and show progress to user
                    phase_summary = self._parse_phase_progress(workspace)
                    if phase_summary:
                        log(phase_summary, "OK")
                    
                    current_mtime = workspace.decisions_file.stat().st_mtime if workspace.decisions_file.exists() else 0
                    if current_mtime == decisions_mtime:
                        # DecisionsTracker hasn't been modified since last check
                        phases_str = f"{len(phase_completions)} phase(s)" if len(phase_completions) > 1 else "a phase"
                        # Address the lead, not hardcoded @PM
                        lead_agents = [a for a in self.agents.values() if a.id == 'pm']
                        if not lead_agents:
                            # Non-software task: find scope-keeper or first agent
                            lead_agents = [a for a in self.agents.values() if a.domain and 'scope' in (a.domain or '').lower()]
                        lead_mention = f"@{lead_agents[0].mention.lstrip('@')}" if lead_agents else "@Team"
                        
                        append_to_conversation(workspace, "ORCHESTRATOR", f"""
{lead_mention} - Completion of {phases_str} detected but DecisionsTracker.md has not been updated.

Before proceeding, verify whether any deviations from the plan occurred during the completed phase(s).
If choices were made that differ from the plan or where the plan was silent, record them in:
{workspace.decisions_file}

If no deviations occurred, acknowledge this and proceed.
""")
                        log(f"Nudged lead to check DecisionsTracker ({len(phase_completions)} phase(s))", "INFO")
                    else:
                        # DecisionsTracker was updated — record the new mtime
                        decisions_mtime = current_mtime
                    
                    # Reinforce original user intent at every phase transition
                    if self._user_intent:
                        append_to_conversation(workspace, "ORCHESTRATOR", f"""
@Team - Phase transition checkpoint. Re-anchor on the original intent:

> {self._user_intent}

Before starting the next phase:
1. Does what we've built so far still serve this intent? Are we drifting?
2. Have you made assumptions about things the user didn't specify? (e.g., visual style, data format, defaults, error behavior) Record them in DecisionsTracker.md if not already done.
3. Are there implicit expectations for this type of application that we haven't addressed yet?
""")
    
    def _parse_phase_list(self, workspace: Workspace) -> list:
        """Parse _INDEX.md and return list of (phase_num, name, status) tuples.
        
        Shared by _build_phase_ticker and _parse_phase_progress.
        Returns empty list if _INDEX.md doesn't exist or can't be parsed.
        """
        if not workspace.index_file.exists():
            return []
        
        try:
            content = workspace.index_file.read_text(encoding='utf-8')
        except OSError:
            return []
        
        # Parse table rows: | Phase# | Name | Status | ...
        rows = re.findall(
            r'\|\s*(\d+)\s*\|\s*([^|]+?)\s*\|\s*([^|]+?)\s*\|',
            content
        )
        # Also match "Phase# : Name" format: | 01: Name | file | Status | ...
        if not rows:
            rows = re.findall(
                r'\|\s*(\d+):?\s*([^|]+?)\s*\|\s*[^|]*\|\s*([^|]+?)\s*\|',
                content
            )
        
        phases = []
        for num, name, status in rows:
            name = name.strip().rstrip('|')
            status = status.strip()
            # Skip rows from "Phase Files" reference table (status is a file link or empty)
            if not status or status.startswith('[') or status.startswith('http'):
                continue
            phases.append((num.strip(), name, status))
        
        return phases
    
    def _build_phase_ticker(self, workspace: Workspace) -> str:
        """Build a compact phase ticker line: ✅✅✅🔄⏳⏳ (3/6) | Phase 4: Analysis
        
        Returns empty string if no phases found.
        """
        phases = self._parse_phase_list(workspace)
        if not phases:
            return ""
        
        icons = []
        active_name = ""
        done_count = 0
        for num, name, status in phases:
            if '✅' in status or 'complete' in status.lower():
                icons.append("✅")
                done_count += 1
            elif '🔄' in status or 'in progress' in status.lower():
                icons.append("🔄")
                if not active_name:
                    active_name = f"Phase {num}: {name}"
            else:
                icons.append("⏳")
        
        ticker = "".join(icons) + f" ({done_count}/{len(phases)})"
        if active_name:
            ticker += f" | {active_name}"
        
        return ticker
    
    def _parse_phase_progress(self, workspace: Workspace) -> str:
        """Parse _INDEX.md and return a one-line progress summary for the user.
        
        Returns e.g.: "Phase 2: Player Research done → Phase 3: Comparison next (3/7 complete)"
        Returns empty string if _INDEX.md doesn't exist or can't be parsed.
        """
        phases = self._parse_phase_list(workspace)
        if not phases:
            return ""
        
        completed = [p for p in phases if '✅' in p[2] or 'complete' in p[2].lower()]
        in_progress = [p for p in phases if '🔄' in p[2] or 'in progress' in p[2].lower()]
        not_started = [p for p in phases if '⏳' in p[2] or 'not started' in p[2].lower()]
        
        total = len(phases)
        done_count = len(completed)
        
        # Build summary: last completed → next up
        parts = []
        if completed:
            last_done = completed[-1]
            parts.append(f"Phase {last_done[0]}: {last_done[1]} done")
        if in_progress:
            current = in_progress[0]
            parts.append(f"Phase {current[0]}: {current[1]} in progress")
        elif not_started:
            next_up = not_started[0]
            parts.append(f"Phase {next_up[0]}: {next_up[1]} next")
        
        if not parts:
            return ""
        
        return " → ".join(parts) + f" ({done_count}/{total} phases complete)"
    
    async def _reconcile_satisfaction(self, workspace: Workspace):
        """Proactively poll non-SATISFIED agents for their status via direct session prompt.
        
        Bypasses conversation.txt entirely — results go to satisfaction.txt
        and debug JSONL only. Retries once per agent on unparseable response.
        """
        status = read_all_satisfaction(workspace)
        expected = list(self.agents.keys())
        unsatisfied = [aid for aid in expected
                       if aid not in status or "SATISFIED" not in status.get(aid, "")]
        
        if not unsatisfied:
            return  # All already satisfied
        
        _debug_log("reconciliation_start", {
            "unsatisfied_agents": unsatisfied,
            "current_status": status,
        })
        
        for agent_id in unsatisfied:
            agent = self.agents.get(agent_id)
            if not agent or not agent.session:
                _debug_log("reconciliation_skip", {
                    "agent": agent_id, "reason": "no session"
                })
                continue
            
            prompt = (
                "All phases are complete per _INDEX.md. Review the current state of the implementation.\n"
                "Reply with ONLY one line: SATISFACTION_STATUS: SATISFIED or WORKING or BLOCKED - [reason]"
            )
            
            parsed_status = None
            for attempt in range(2):
                response = await self._send_reconciliation_prompt(agent, prompt)
                _debug_log("reconciliation_response", {
                    "agent": agent_id, "attempt": attempt + 1,
                    "prompt": prompt, "response": response[:500] if response else None,
                })
                
                if response:
                    match = re.search(
                        r'SATISFACTION_STATUS\s*:\s*(SATISFIED|BLOCKED|PAUSED|WORKING)(?:\s*-\s*(.*))?',
                        response, re.IGNORECASE
                    )
                    if match:
                        parsed_status = match.group(1).upper()
                        reason = (match.group(2) or "").split("\n")[0].strip()
                        break
                
                # Retry with a shorter, more direct prompt
                prompt = "Reply with exactly one line: SATISFACTION_STATUS: SATISFIED or WORKING or BLOCKED"
            
            # Update satisfaction.txt with parsed or default status
            if parsed_status == "SATISFIED":
                update_satisfaction(workspace, agent_id, "SATISFIED")
            elif parsed_status == "BLOCKED":
                update_satisfaction(workspace, agent_id, f"BLOCKED - {reason}" if reason else "BLOCKED")
            elif parsed_status == "PAUSED":
                update_satisfaction(workspace, agent_id, "PAUSED - Awaiting human guidance")
            else:
                update_satisfaction(workspace, agent_id, parsed_status or "WORKING")
            
            _debug_log("reconciliation_result", {
                "agent": agent_id,
                "parsed_status": parsed_status or "WORKING (default)",
            })
    
    async def _send_reconciliation_prompt(self, agent: PersonaAgent, prompt: str) -> str:
        """Send a prompt to an agent's existing session and return the response."""
        async with agent.session_lock:
            response_parts = []
            done = asyncio.Event()
            
            def on_event(event):
                if event.type.value == "assistant.message":
                    response_parts.append(event.data.content)
                elif event.type.value == "assistant.message_delta":
                    if hasattr(event.data, 'delta_content') and event.data.delta_content:
                        response_parts.append(event.data.delta_content)
                elif event.type.value == "session.idle":
                    done.set()
                elif event.type.value == "session.error":
                    done.set()
            
            unsubscribe = agent.session.on(on_event)
            try:
                await agent.session.send({"prompt": prompt})
                await asyncio.wait_for(done.wait(), timeout=60)
            except asyncio.TimeoutError:
                _debug_log("reconciliation_timeout", {"agent": agent.id})
            except Exception as e:
                _debug_log("reconciliation_error", {"agent": agent.id, "error": str(e)})
            finally:
                unsubscribe()
            
            return ''.join(response_parts)
    
    async def announce_victory(self, workspace: Workspace, is_final: bool = True):
        """Inject victory message. If not final, announce verification pending."""
        status = read_all_satisfaction(workspace)
        status_lines = "\n".join([f"- @{k.capitalize()}: {v}" for k, v in status.items()])
        
        if is_final:
            append_to_conversation(workspace, "ORCHESTRATOR", f"""
🎉 VICTORY! All personas satisfied. Verification passed.

Implementation complete. Great teamwork!

Final status:
{status_lines}

You may now stop. Thank you.
""")
        else:
            append_to_conversation(workspace, "ORCHESTRATOR", f"""
✅ All personas satisfied. Proceeding to verification...

Current status:
{status_lines}

The orchestrator will now verify the implementation against the plan.
Please stand by.
""")
        
        # Give agents time to see the message
        await asyncio.sleep(5)
    
    async def handle_human_escalation(self, workspace: Workspace) -> bool:
        """Handle stall by escalating to human with LLM-summarized context."""
        self.metrics.human_escalations += 1
        
        status = read_all_satisfaction(workspace)
        status_lines = "\n".join([f"- @{k.capitalize()}: {v}" for k, v in status.items()])
        
        # Identify which agents are blocked/paused
        blocked_agents = [f"@{k.capitalize()}" for k, v in status.items()
                          if any(kw in v.upper() for kw in ("BLOCKED", "PAUSED", "HUMAN"))]
        
        # Extract last ~20 messages from conversation for LLM context
        summary_text = ""
        try:
            conv = read_conversation(workspace)
            # Split on message boundaries: [HH:MM:SS] @SENDER:
            messages = re.split(r'(?=\[\d{2}:\d{2}:\d{2}\]\s+@)', conv)
            messages = [m.strip() for m in messages if m.strip()]
            recent = messages[-20:] if len(messages) > 20 else messages
            recent_text = "\n\n".join(recent)
            
            blocked_list = ", ".join(blocked_agents) if blocked_agents else "unknown agents"
            summary = await _send_and_get_response(
                self.client, self.model,
                system_prompt=(
                    "You are a concierge for a human overseeing an AI agent team. "
                    f"The following agents need human input: {blocked_list}. "
                    "Read the recent conversation and identify what specific question(s) or "
                    "decision(s) the human needs to answer. Be concise — list each question "
                    "clearly with which agent is asking. Do not include background context "
                    "the human doesn't need. If no clear question is found, say so."
                ),
                message=recent_text,
                timeout_seconds=30,
            )
            if summary and summary.strip():
                summary_text = summary.strip()
        except Exception as e:
            log(f"Failed to summarize escalation context: {e}", "WARN")
        
        # Inject pause message
        append_to_conversation(workspace, "ORCHESTRATOR", f"""
@AllAgents - Escalating to @HUMAN for guidance.

Please pause current work and ensure things are in a consistent state.

Wait for human input before continuing.

Current status:
{status_lines}
""")
        
        log("Waiting for human input...", "HUMAN")
        
        # Build panel content with LLM summary if available
        if summary_text:
            panel_content = (
                f"[bold]What agents need from you:[/bold]\n{escape(summary_text)}\n\n"
                f"[dim]Agent status:\n{escape(status_lines)}[/dim]\n\n"
                f"Conversation: {workspace.conversation_file}\n\n"
                "Options:\n"
                "  [bold]1[/bold]. Provide guidance\n"
                "  [bold]2[/bold]. View recent conversation\n"
                "  [bold]3[/bold]. Abort"
            )
        else:
            panel_content = (
                f"Agents need human input. Current status:\n{escape(status_lines)}\n\n"
                f"Conversation: {workspace.conversation_file}\n\n"
                "Options:\n"
                "  [bold]1[/bold]. Provide guidance\n"
                "  [bold]2[/bold]. View recent conversation\n"
                "  [bold]3[/bold]. Abort"
            )
        
        console.print(Panel(panel_content, title="⚠️  HUMAN ESCALATION", border_style="yellow"))
        
        while True:
            choice = Prompt.ask("Choose", choices=["1", "2", "3"])
            
            if choice == "2":
                conv = read_conversation(workspace)
                console.print(Panel(
                    escape(conv[-5000:] if len(conv) > 5000 else conv),
                    title="Recent Conversation", border_style="bright_black"
                ))
                continue
            
            elif choice == "3":
                append_to_conversation(workspace, "ORCHESTRATOR", 
                    "@AllAgents - Human has chosen to abort. Please stop all work.")
                return False
            
            elif choice == "1":
                console.print("[bold]Enter your guidance[/bold] (end with empty line):")
                lines = []
                while True:
                    line = input()
                    if line == "":
                        break
                    lines.append(line)
                
                guidance = "\n".join(lines)
                if guidance.strip():
                    append_to_conversation(workspace, "HUMAN", f"""
@AllAgents - Human guidance:

{guidance}

Please continue based on this guidance.
""")
                    log("Guidance relayed to agents", "HUMAN")
                    return True
            
            console.print("[red]Invalid choice.[/red]")


# ============================================================================
# Git Worktree Isolation
# ============================================================================

@dataclass
class WorktreeResult:
    """Result of worktree setup — carries state needed for exit instructions."""
    out_path: Path              # The path agents should work in (worktree or original)
    created: bool = False       # Whether a worktree was actually created
    branch_name: str = ""       # e.g. mandali/session-20260210-053400
    original_path: Path = None  # The user's original --out-path
    git_root: Path = None       # Root of the original git repo
    stash_ref: str = ""         # If user changes were stashed


def setup_worktree(out_path: Path) -> WorktreeResult:
    """Set up git worktree isolation if --out-path is inside a git repo.
    
    If inside a git repo: creates a sibling worktree directory so agents work
    in isolation and the user's original directory is never touched.
    If NOT inside a git repo: returns out_path unchanged (no isolation needed).
    """
    resolved = out_path.resolve()
    result = WorktreeResult(out_path=resolved, original_path=resolved)
    
    try:
        # Check if inside a git repo
        git_check = subprocess.run(
            ["git", "rev-parse", "--show-toplevel"],
            cwd=resolved, capture_output=True, text=True
        )
        
        if git_check.returncode != 0:
            # Not a git repo — initialize one for change tracking
            log("Not inside a git repo, initializing git for change tracking", "INFO")
            subprocess.run(["git", "init"], cwd=resolved, capture_output=True, text=True)
            subprocess.run(["git", "add", "-A"], cwd=resolved, capture_output=True, text=True)
            subprocess.run(
                ["git", "commit", "-m", "Initial commit (mandali workspace)", "--allow-empty"],
                cwd=resolved, capture_output=True, text=True
            )
            log("Git repository initialized", "OK")
            return result
        
        git_root = Path(git_check.stdout.strip())
        result.git_root = git_root
        
        # Build worktree path: sibling in parent directory
        timestamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        branch_name = f"mandali/session-{timestamp}"
        worktree_dir = git_root.parent / f"{git_root.name}-mandali-{timestamp}"
        
        # Handle stale worktree at target path
        if worktree_dir.exists():
            log(f"Found existing directory at {worktree_dir}", "WARN")
            if Confirm.ask(f"Remove stale session at [cyan]{worktree_dir}[/cyan] and continue?"):
                # Try to remove as worktree first, then as plain directory
                subprocess.run(
                    ["git", "worktree", "remove", "--force", str(worktree_dir)],
                    cwd=git_root, capture_output=True, text=True
                )
                if worktree_dir.exists():
                    shutil.rmtree(worktree_dir)
                log("Removed stale session directory", "OK")
            else:
                log("User chose not to remove stale session, aborting", "ERROR")
                sys.exit(1)
        
        # Stash pending changes before creating worktree
        has_pending = False
        status = subprocess.run(
            ["git", "status", "--porcelain"],
            cwd=git_root, capture_output=True, text=True, check=True
        )
        
        if status.stdout.strip():
            has_pending = True
            stash_msg = "mandali: pre-agent user changes"
            stash_result = subprocess.run(
                ["git", "stash", "push", "-u", "-m", stash_msg],
                cwd=git_root, capture_output=True, text=True, check=True
            )
            if "No local changes" not in stash_result.stdout:
                result.stash_ref = "stash@{0}"
                log(f"Stashed pending changes: {result.stash_ref}", "INFO")
            else:
                has_pending = False
        
        # Create branch and worktree
        subprocess.run(
            ["git", "worktree", "add", "-b", branch_name, str(worktree_dir)],
            cwd=git_root, capture_output=True, text=True, check=True
        )
        
        # Mark as created immediately so cleanup works if anything below fails
        result.out_path = worktree_dir
        result.created = True
        result.branch_name = branch_name
        
        # Apply stashed changes to worktree so agents pick up where user left off
        changes_carried = False
        if has_pending and result.stash_ref:
            apply_result = subprocess.run(
                ["git", "stash", "apply"],
                cwd=worktree_dir, capture_output=True, text=True
            )
            if apply_result.returncode == 0:
                log("Applied pending changes to worktree", "OK")
                changes_carried = True
                
                # Only restore original directory if apply succeeded
                pop_result = subprocess.run(
                    ["git", "stash", "pop"],
                    cwd=git_root, capture_output=True, text=True
                )
                if pop_result.returncode == 0:
                    log("Restored pending changes in original directory", "OK")
                    result.stash_ref = ""  # Stash consumed
                else:
                    log(f"Could not restore stash in original directory (stash preserved: {result.stash_ref})", "WARN")
            else:
                log("Could not apply pending changes to worktree (continuing without them)", "WARN")
                # Don't pop — leave stash intact so user can recover
        
        # Show confirmation
        panel_text = f"Original:  {git_root}\n"
        panel_text += f"Worktree:  {worktree_dir}\n"
        panel_text += f"Branch:    {branch_name}\n"
        if changes_carried:
            panel_text += f"Pending changes: carried over to worktree\n"
        elif has_pending:
            panel_text += f"Pending changes: could not carry over (stash preserved)\n"
        panel_text += f"\nYour original directory is untouched."
        console.print(Panel(panel_text, title="🔒 WORKTREE ISOLATION", border_style="bright_blue"))
        
        return result
        
    except subprocess.CalledProcessError as e:
        log(f"Git worktree setup failed: {e.stderr or e}", "WARN")
        log("Falling back to working directly in --out-path (no isolation)", "WARN")
        if result.stash_ref and result.git_root:
            subprocess.run(["git", "stash", "pop"], cwd=result.git_root, capture_output=True, text=True)
            log("Restored stashed changes", "OK")
            result.stash_ref = ""
        return result
    except Exception as e:
        log(f"Worktree setup error: {e}", "WARN")
        log("Falling back to working directly in --out-path (no isolation)", "WARN")
        if result.stash_ref and result.git_root:
            subprocess.run(["git", "stash", "pop"], cwd=result.git_root, capture_output=True, text=True)
            log("Restored stashed changes", "OK")
            result.stash_ref = ""
        return result


def print_worktree_instructions(wt: WorktreeResult):
    """Print merge/discard instructions at the end of a run."""
    if not wt.created:
        return
    
    main_branch = "main"
    try:
        head_ref = subprocess.run(
            ["git", "symbolic-ref", "refs/remotes/origin/HEAD"],
            cwd=wt.git_root, capture_output=True, text=True
        )
        if head_ref.returncode == 0:
            main_branch = head_ref.stdout.strip().split("/")[-1]
    except Exception:
        pass
    
    merge_cmds = (
        f"  cd {wt.git_root}\n"
        f"  git merge {wt.branch_name}\n"
    )
    
    discard_cmds = (
        f"  git worktree remove {wt.out_path}\n"
        f"  git branch -D {wt.branch_name}\n"
    )
    
    diff_cmd = f"  git diff {main_branch}..{wt.branch_name}"
    
    stash_note = ""
    if wt.stash_ref:
        stash_note = (
            f"\n[bold]Restore your stashed changes (in original repo):[/bold]\n"
            f"  cd {wt.git_root}\n"
            f"  git stash pop\n"
        )
    
    text = (
        f"[bold]To keep the changes (merge into {main_branch}):[/bold]\n"
        f"{merge_cmds}\n"
        f"[bold]To review before merging:[/bold]\n"
        f"{diff_cmd}\n\n"
        f"[bold]To discard everything:[/bold]\n"
        f"{discard_cmds}"
        f"{stash_note}"
    )
    
    console.print(Panel(text, title="📋 NEXT STEPS — Worktree", border_style="cyan"))


def cleanup_worktree(wt: WorktreeResult):
    """Remove worktree and branch when no agent work was done (early exit)."""
    if not wt.created:
        return
    try:
        subprocess.run(
            ["git", "worktree", "remove", "--force", str(wt.out_path)],
            cwd=wt.git_root, capture_output=True, text=True
        )
        subprocess.run(
            ["git", "branch", "-D", wt.branch_name],
            cwd=wt.git_root, capture_output=True, text=True
        )
        log(f"Cleaned up worktree: {wt.out_path}", "OK")
    except Exception:
        log(f"Could not auto-clean worktree at {wt.out_path}", "WARN")
    
    if wt.stash_ref:
        console.print(f"[yellow]⚠️  Your pending changes are stashed. Restore with:[/yellow]")
        console.print(f"[cyan]  cd {wt.git_root} && git stash pop[/cyan]")


# ============================================================================
# Plan Generation Flow
# ============================================================================

async def run_generate_plan_flow(orchestrator, prompt: str, out_path: Path,
                                  classification: 'TaskClassification' = None,
                                  team_roster: list = None) -> Optional[str]:
    """Run the generate-plan flow: Interview → Generate → Review.
    
    Returns plan_content on success, None on failure/rejection.
    """
    log("Mode: Generate plan (interview + AI generation)", "INFO")
    
    # Step 1: Interview
    gathered_info = await run_interview(
        orchestrator.client, orchestrator.model, prompt
    )
    
    if "error" in gathered_info:
        log(f"Interview failed: {gathered_info['error']}", "ERR")
        return None
    
    # Store interview info on orchestrator for later classification
    orchestrator._interview_info = gathered_info
    
    # Step 2: Generate plan (creates files in out_path)
    plan_content = await generate_plan_from_interview(
        orchestrator.client, orchestrator.model, gathered_info, prompt, out_path,
        classification=classification, team_roster=team_roster
    )
    
    if not plan_content:
        log("Plan generation failed - no plan files created", "ERR")
        return None
    
    # Determine what was created (phased or single file)
    phases_path = out_path / "phases"
    is_phased = (phases_path / "_INDEX.md").exists() or (phases_path / "_CONTEXT.md").exists()
    
    if is_phased:
        plan_location = phases_path
        phase_count = len(list(phases_path.glob("phase-*.md")))
        files_list = "\n".join(f"  - {f.name}" for f in sorted(phases_path.glob("*.md")))
    else:
        plan_location = out_path / "plan.md"
        phase_count = 0
        files_list = "  - plan.md"
    
    # Show location, let user review externally
    detail = f"Location: {plan_location}\n"
    if is_phased:
        detail += f"Structure: Phased ({phase_count} phase files)\n"
    detail += f"\nFiles created:\n{files_list}\n\n"
    detail += "[dim]Review and modify the files in your editor.[/dim]"
    console.print(Panel(detail, title="📁 PLAN GENERATED", border_style="green"))
    
    while True:
        choice = Prompt.ask("[bold]Accept or Reject?[/bold]", choices=["a", "r"], default="a").lower()
        if choice == 'r':
            log("Plan rejected by user", "WARN")
            return None
        elif choice == 'a':
            # Re-read files in case user edited them
            if is_phased:
                content_parts = []
                ctx = phases_path / "_CONTEXT.md"
                if ctx.exists():
                    content_parts.append(f"# === _CONTEXT.md ===\n\n{ctx.read_text(encoding='utf-8')}")
                idx = phases_path / "_INDEX.md"
                if idx.exists():
                    content_parts.append(f"\n\n# === _INDEX.md ===\n\n{idx.read_text(encoding='utf-8')}")
                for pf in sorted(phases_path.glob("phase-*.md")):
                    content_parts.append(f"\n\n# === {pf.name} ===\n\n{pf.read_text(encoding='utf-8')}")
                plan_content = "\n".join(content_parts)
            else:
                plan_content = (out_path / "plan.md").read_text(encoding='utf-8')
            log("Plan accepted by user", "OK")
            break
        console.print("[yellow]Please choose A or R[/yellow]")
    
    # AI review of generated plan
    while True:
        status, result = await review_plan(orchestrator.client, orchestrator.model, plan_content)
        if status == "approved":
            log("Plan approved ✅", "OK")
            break
        elif status == "needs_clarification":
            console.print(Panel(escape(result), title="❓ PLAN NEEDS CLARIFICATION", border_style="yellow"))
            answers = Prompt.ask("Your answers (or 'abort')").strip()
            if answers.lower() == 'abort':
                return None
            plan_content += f"\n\n## Clarifications\n{answers}\n"
        elif status == "needs_revision":
            console.print(Panel(
                escape(result[:2000] + "..." if len(result) > 2000 else result),
                title="📝 PLAN REVIEW", border_style="bright_blue"
            ))
            if not Confirm.ask("Accept plan with these recommendations noted for agents?", default=True):
                return None
            # Write review notes to a separate file for agents to read during Phase 0B
            review_file = out_path / "mandali-artifacts" / "_REVIEW_NOTES.md"
            review_file.parent.mkdir(parents=True, exist_ok=True)
            review_file.write_text(
                "# Plan Review Notes\n\n"
                "These notes were generated by an automated review before execution began.\n"
                "Address these points during Phase 0B design discussion.\n\n"
                "---\n\n"
                f"{result}\n",
                encoding='utf-8'
            )
            # Store path so build_orchestrator_message can reference it
            orchestrator._review_notes_path = str(review_file)
            log(f"Review notes saved to {review_file}", "INFO")
            break
    
    return plan_content


# ============================================================================
# Main Entry
# ============================================================================

async def async_main(args):
    global MCP_SERVERS_CONFIG, _debug_enabled, _debug_file
    
    config = load_config()
    
    # Load MCP server config for all sessions
    MCP_SERVERS_CONFIG = load_mcp_config()
    
    # Enable debug logging if requested
    if getattr(args, 'debug', False):
        _debug_enabled = True
        # Debug file goes in out_path/mandali-artifacts/ once workspace is created
        # For now, use a temp path; will be moved after workspace setup
        debug_dir = args.out_path.resolve() / "mandali-artifacts"
        debug_dir.mkdir(parents=True, exist_ok=True)
        _debug_file = debug_dir / "debug.jsonl"
        log(f"Debug logging enabled → {_debug_file}", "INFO")
    
    stall_timeout = getattr(args, 'stall_timeout', 5)
    max_retries = getattr(args, 'max_retries', 5)
    
    orchestrator = AutonomousOrchestrator(config, args.verbose)
    
    try:
        try:
            await orchestrator.start()
        except RuntimeError as e:
            log(str(e), "ERROR")
            console.print(Panel(
                "[bold]The Copilot CLI process failed to respond in time.[/bold]\n\n"
                "Common causes:\n"
                "  • MCP servers in your config are slow to start or unreachable\n"
                "  • The Copilot CLI is not authenticated (run [cyan]copilot auth login[/cyan])\n"
                "  • Network issues or proxy blocking the connection\n"
                "  • The CLI version is incompatible with the SDK\n\n"
                "Troubleshooting:\n"
                "  1. Run [cyan]copilot --version[/cyan] to verify the CLI works\n"
                "  2. Check your MCP config: [cyan]~/.copilot/mcp-config.json[/cyan]\n"
                "  3. Try running with fewer MCP servers to isolate the issue",
                title="⚠️  Startup Failed",
                border_style="yellow",
            ))
            return 1
        
        # Initialize Teams integration if enabled
        if args.teams:
            try:
                from teams_bridge import TeamsRelayBridge, load_relay_config
                
                relay_config = load_relay_config()
                if not relay_config:
                    console.print("[red]Teams integration requires relay_url and api_key in ~/.copilot/mandali-teams.json[/red]")
                    console.print("Run --setup-teams first, or add relay_url and api_key to the config.")
                    return 1
                
                orchestrator.teams_bridge = TeamsRelayBridge(
                    relay_url=relay_config["relay_url"],
                    api_key=relay_config["api_key"],
                )
                await orchestrator.teams_bridge.start()
                log(f"Teams relay connected: {orchestrator.teams_bridge.public_url}", "OK")
            except ImportError:
                console.print("[red]Teams integration requires teams_bridge.py and websockets package[/red]")
                return 1
            except Exception as e:
                console.print(f"[red]Failed to start Teams bridge: {e}[/red]")
                return 1
        
        # Get plan content
        out_path = args.out_path.resolve()
        out_path.mkdir(parents=True, exist_ok=True)
        prompt_context = args.prompt if args.prompt else None
        
        # Set up worktree isolation if out_path is inside a git repo
        worktree = setup_worktree(out_path)
        out_path = worktree.out_path
        
        if args.generate_plan and args.prompt:
            # ============================================================
            # GENERATE-PLAN MODE: Interview → Generate → Review
            # ============================================================
            plan_content = await run_generate_plan_flow(orchestrator, args.prompt, out_path)
            if plan_content is None:
                cleanup_worktree(worktree)
                return 1
        
        else:
            # ============================================================
            # DEFAULT MODE: Discover existing artifacts, skip planning
            # ============================================================
            log("Mode: Direct launch (discovering existing plan artifacts)", "INFO")
            plan_content = None
            
            if args.prompt:
                # Extract file paths from prompt using LLM
                initial_paths = await extract_plan_paths(
                    orchestrator.client, orchestrator.model, args.prompt
                )
                if not initial_paths:
                    log("No file references found in prompt", "WARN")
                    if Confirm.ask("[yellow]Would you like to generate a plan from this prompt instead?[/yellow]"):
                        plan_content = await run_generate_plan_flow(orchestrator, args.prompt, out_path)
                        if plan_content is None:
                            cleanup_worktree(worktree)
                            return 1
                    else:
                        log("Use --generate-plan to create a plan from scratch", "INFO")
                        cleanup_worktree(worktree)
                        return 1
            else:
                # --plan provided: start from the plan file
                plan_path = Path(args.plan)
                if not plan_path.exists():
                    log(f"Plan not found: {plan_path}", "ERR")
                    cleanup_worktree(worktree)
                    return 1
                initial_paths = [plan_path.resolve()]
                # If pointing at _INDEX.md/_CONTEXT.md, include the parent dir
                if plan_path.name in ('_INDEX.md', '_CONTEXT.md'):
                    initial_paths = [plan_path.parent.resolve()]
            
            # Only discover/copy artifacts if we didn't switch to generate-plan
            if plan_content is None:
                # Recursively discover all referenced artifacts (up to 5 levels)
                artifacts = await discover_plan_artifacts(
                    orchestrator.client, orchestrator.model, initial_paths
                )
                
                if not artifacts:
                    log("No plan artifacts found", "WARN")
                    if args.prompt and Confirm.ask("[yellow]Would you like to generate a plan from this prompt instead?[/yellow]"):
                        plan_content = await run_generate_plan_flow(orchestrator, args.prompt, out_path)
                        if plan_content is None:
                            cleanup_worktree(worktree)
                            return 1
                    else:
                        if not args.prompt:
                            log("Provide a --prompt to generate a plan from scratch", "INFO")
                        else:
                            log("Use --generate-plan to create a plan from scratch", "INFO")
                        cleanup_worktree(worktree)
                        return 1
            
            # Only show artifact UI if we discovered artifacts (not generated a plan)
            if plan_content is None:
                # Copy artifacts to workspace
                workspace = Workspace.create(out_path)
                workspace.ensure_exists()
                copied = copy_plan_artifacts(artifacts, workspace)
            
                # Show confirmation UI
                def format_size(size_bytes: int) -> str:
                    if size_bytes < 1024:
                        return f"{size_bytes} B"
                    return f"{size_bytes / 1024:.1f} KB"
                
                files_table = Table(show_header=True, header_style="bold", box=None)
                files_table.add_column("File", style="cyan")
                files_table.add_column("Size", justify="right", style="dim")
                for src, dst, size in copied:
                    files_table.add_row(str(dst.relative_to(out_path)), format_size(size))
                
                detail_parts = [f"Workspace: {out_path}\n"]
                if prompt_context:
                    preview = prompt_context[:80] + "..." if len(prompt_context) > 80 else prompt_context
                    detail_parts.append(f'Prompt: "{escape(preview)}"')
                
                console.print(Panel(
                    "\n".join(detail_parts),
                    title="📁 PLAN ARTIFACTS DISCOVERED", border_style="green"
                ))
                console.print(files_table)
                console.print("\n[dim]Review the workspace. Press Accept to launch agents, or Reject.[/dim]")
                
                while True:
                    choice = Prompt.ask("[bold]Accept or Reject?[/bold]", choices=["a", "r"], default="a").lower()
                    if choice == 'r':
                        log("Launch rejected by user", "WARN")
                        cleanup_worktree(worktree)
                        return 0
                    elif choice == 'a':
                        log("Artifacts accepted, preparing to launch", "OK")
                        break
                    console.print("[yellow]Please choose A or R[/yellow]")
                
                # Build plan_content from workspace
                plan_content = workspace.get_plan_content()
                if not plan_content:
                    # Fallback: concatenate all copied files
                    parts = []
                    for src, dst, _ in copied:
                        try:
                            parts.append(f"# === {dst.name} ===\n\n{dst.read_text(encoding='utf-8')}")
                        except (UnicodeDecodeError, IOError):
                            pass
                    plan_content = "\n\n".join(parts)
        
        # ============================================================
        # COMMON: Setup workspace and launch agents
        # ============================================================
        
        # Setup workspace (may already exist from default mode)
        workspace = Workspace.create(out_path)
        workspace.ensure_exists()
        workspace.plan_file.write_text(plan_content, encoding='utf-8')
        
        # Convert non-phased plans to phased structure for consistent agent workflow
        if not workspace.is_phased_plan():
            log("Plan is not in phased format, converting...", "INFO")
            plan_content = await convert_to_phased_plan(
                orchestrator.client, orchestrator.model, plan_content, out_path
            )
            workspace.plan_file.write_text(plan_content, encoding='utf-8')
        
        log(f"Workspace: {workspace.path}", "INFO")
        log(f"Artifacts: {workspace.artifacts_path}", "INFO")
        
        # Create Teams thread for this run if enabled
        if orchestrator.teams_bridge:
            try:
                plan_name = args.plan if args.plan else "Mandali Run"
                if hasattr(plan_name, 'name'):
                    plan_name = plan_name.name
                orchestrator.teams_thread_id = await orchestrator.teams_bridge.create_thread(
                    f"🚀 Mandali started: {plan_name}\n\n"
                    f"Agents: Dev, Security, PM, QA, SRE\n"
                    f"Workspace: {workspace.path}\n"
                    f"Reply in this thread to provide guidance."
                )
                log(f"Teams thread created", "OK")
                
                # Register reply callback that injects into conversation
                def on_teams_reply(thread_id: str, text: str):
                    # Accept messages from any thread (relay mode)
                    append_to_conversation(workspace, "HUMAN", f"""
@AllAgents - Human guidance (via Teams):

{text}

Please continue based on this guidance.
""")
                    log(f"Teams reply injected: {text[:80]}...", "TEAMS")
                    orchestrator._teams_reply_received = True
                    # Track the real thread_id for replies back
                    if orchestrator.teams_thread_id in (None, "__default__"):
                        orchestrator.teams_thread_id = thread_id
                
                orchestrator.teams_bridge.set_reply_callback(on_teams_reply)
            except Exception as e:
                log(f"Failed to create Teams thread: {e}", "WARN")
                # Continue without Teams - not fatal
        

        # Determine plan location description for agents
        if workspace.is_phased_plan():
            plan_location = f"""
**PHASED PLAN STRUCTURE** - Read files in this order:
1. `{workspace.context_file}` - Global context (READ FIRST)
2. `{workspace.index_file}` - Phase index and status tracking
3. Individual phase files in `{workspace.phases_path}/phase-*.md`
"""
        else:
            plan_location = f"the plan in `{workspace.plan_file}`"
        
        # Classify task and assemble team (dynamic persona feature)
        team_roster = None
        classification = None
        static_personas_flag = getattr(args, 'static_personas', False)
        domains_flag = getattr(args, 'domains', None)
        task_type = "software-development"  # default
        
        if not static_personas_flag:
            if domains_flag:
                # --domains overrides classifier
                classification = classify_from_domains_flag(domains_flag)
                task_type = classification.task_type
                log(f"Task type (from --domains): {task_type}, domains: {[d['name'] for d in classification.domains]}", "INFO")
            elif hasattr(orchestrator, '_interview_info') and orchestrator._interview_info:
                # Classify from interview results
                log("Classifying task type...", "INFO")
                try:
                    classification = await classify_task(
                        orchestrator.client, orchestrator.model,
                        prompt_context or "", orchestrator._interview_info
                    )
                    task_type = classification.task_type
                    log(f"Task type: {task_type}, domains: {[d['name'] for d in classification.domains]}", "OK")
                except Exception as e:
                    log(f"Classification failed, defaulting to software-development: {e}", "WARN")
                    classification = None
                    task_type = "software-development"
            
            # Assemble team if non-software or mixed
            if classification and task_type != "software-development":
                log("Assembling domain-specific team...", "INFO")
                try:
                    team_roster = await assemble_team(
                        orchestrator.client, orchestrator.model,
                        classification, workspace, config
                    )
                except Exception as e:
                    log(f"Team assembly failed, falling back to static team: {e}", "ERR")
                    team_roster = None
        
        # Initialize conversation with dynamic orchestrator message
        orch_message = build_orchestrator_message(
            team_roster or [{'id': p['id'], 'name': p['name'], 'mention': f"@{p['name']}", 'role': 'Doer', 'domain': 'software-development'} for p in config.get('personas', [])],
            plan_location,
            task_type,
            review_notes_path=getattr(orchestrator, '_review_notes_path', None)
        )
        append_to_conversation(workspace, "ORCHESTRATOR", orch_message)
        
        # If prompt context was provided, add it as additional guidance
        if prompt_context:
            append_to_conversation(workspace, "ORCHESTRATOR", f"""
@AllAgents - Additional context from user:

{prompt_context}

Use this alongside the plan files to guide your work.
""")
        
        # Store original user intent for periodic reinforcement during phase transitions
        orchestrator._user_intent = prompt_context
        
        orchestrator.metrics.start_time = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        
        # Build agents string from team roster or config
        if team_roster:
            agents_str = ", ".join([p['id'] for p in team_roster])
        else:
            agents_str = ", ".join([p['id'] for p in config['personas']])
        
        # ============================================================
        # VERIFICATION LOOP: Trust but Verify
        # ============================================================
        success = False
        gap_report = ""
        total_rounds = max_retries if max_retries > 0 else 1
        
        for round_number in range(1, total_rounds + 1):
            is_final_round = (max_retries == 0) or (round_number == total_rounds)
            
            # Show round info
            if max_retries > 0 and total_rounds > 1:
                round_label = f"Round {round_number}/{total_rounds}"
            else:
                round_label = "Round 1"
            
            panel_title = f"🚀 LAUNCHING AUTONOMOUS TEAM — {round_label}"
            panel_body = f"Workspace: {workspace.path}\nConversation: {workspace.conversation_file}\nAgents: {agents_str}"
            if gap_report:
                panel_body += f"\nMode: Addressing {gap_report.count('## Gap')} gap(s) from verification"
            console.print(Panel(panel_body, title=panel_title, border_style="green bold"))
            
            # On relaunch (round > 1), inject gap context into conversation
            if round_number > 1 and gap_report:
                append_to_conversation(workspace, "ORCHESTRATOR", f"""
@AllAgents - RELAUNCH — Round {round_number}/{total_rounds}

The previous round's implementation was verified and the following gaps were identified.
Your job is to address these gaps. DecisionsTracker.md has been preserved from previous rounds.

## Gaps to Address
{gap_report}

## Instructions
1. Read the plan files and DecisionsTracker.md for full context
2. Focus on the gaps listed above — these are the priority
3. If a gap cannot be addressed, record why in DecisionsTracker.md
4. Continue following your persona guidelines for collaboration and quality

Begin by reviewing the gaps and the codebase, then work to close them.
""")
            
            # Launch agents
            await orchestrator.launch_agents(workspace, plan_content, team_roster=team_roster)
            
            # Monitor loop — always announce "proceeding to verification" when verification is enabled
            success = await orchestrator.monitor_loop(
                workspace,
                max_stall_minutes=stall_timeout,
                is_final_round=(max_retries == 0),
                round_number=round_number,
                total_rounds=total_rounds,
            )
            
            if not success:
                await orchestrator.stop_agents()
                break  # Human aborted or unrecoverable
            
            # Skip verification if disabled
            if max_retries == 0:
                await orchestrator.stop_agents()
                break
            
            # Run verification — agents stay alive in case they're needed for handoff
            orchestrator.metrics.verification_rounds += 1
            passed, gap_report = await run_verification(
                orchestrator.client, orchestrator.model, workspace, plan_content
            )
            
            if passed:
                orchestrator.metrics.verification_passed = True
                await orchestrator.announce_victory(workspace, is_final=True)
                
                # Agents are still alive — ask for handoff while they have full context
                handoff_content = await generate_handoff(
                    orchestrator.client, orchestrator.model,
                    workspace, plan_content, prompt_context or ""
                )
                if handoff_content:
                    # Show summary (first 500 chars) and point to file for full details
                    preview = handoff_content[:500]
                    if len(handoff_content) > 500:
                        preview += "\n..."
                    handoff_path = workspace.path / "HANDOFF.md"
                    console.print(Panel(
                        f"{escape(preview)}\n\n"
                        f"[bold]Full instructions:[/bold] {handoff_path}",
                        title="📋 HANDOFF — How to Use Your Deliverable",
                        border_style="green"
                    ))
                
                await orchestrator.stop_agents()
                await asyncio.sleep(5)
                break
            
            # Verification failed — stop agents, fresh launch gets a fresh look
            await orchestrator.stop_agents()
            
            # Gaps found — check if we have more rounds
            if is_final_round:
                log(f"Max retries ({total_rounds}) exhausted with gaps remaining", "WARN")
                append_to_conversation(workspace, "ORCHESTRATOR", f"""
⚠️ Verification found gaps after {total_rounds} round(s). Max retries exhausted.

Remaining gaps:
{gap_report}

Human review recommended.
""")
                break
            
            # Archive and reset for next round
            log(f"Preparing for round {round_number + 1}...", "INFO")
            archive_conversation(workspace, round_number)
            reset_satisfaction(workspace)
            # gap_report carries forward to inject into next round's conversation
        
        orchestrator.metrics.end_time = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        
        # Print summary
        summary_table = Table(title="📊 SUMMARY", show_header=False, border_style="bright_blue")
        summary_table.add_column("Metric", style="bold")
        summary_table.add_column("Value")
        summary_table.add_row("Duration", f"{orchestrator.metrics.start_time} → {orchestrator.metrics.end_time}")
        summary_table.add_row("Messages", str(orchestrator.metrics.total_messages))
        summary_table.add_row("Nudges", str(orchestrator.metrics.nudges))
        summary_table.add_row("Escalations", str(orchestrator.metrics.human_escalations))
        victory_text = "[green]✅ Yes[/green]" if orchestrator.metrics.victory else "[red]❌ No[/red]"
        summary_table.add_row("Victory", victory_text)
        if max_retries > 0:
            summary_table.add_row("Verification Rounds", str(orchestrator.metrics.verification_rounds))
            verified_text = "[green]✅ Passed[/green]" if orchestrator.metrics.verification_passed else "[red]❌ Gaps remain[/red]"
            summary_table.add_row("Verification", verified_text)
        console.print(summary_table)
        
        # Show worktree merge/discard instructions
        print_worktree_instructions(worktree)
        
        return 0 if success else 1
        
    finally:
        await orchestrator.stop()


PERSONA_DESCRIPTIONS = {
    "dev": {
        "title": "Dev — Senior Developer",
        "short": "Implementation, code quality, TDD, creative problem-solving",
        "detail": """
Responsibilities:
  • Implements all code changes following the phased plan
  • Writes tests BEFORE implementation (TDD)
  • Runs self code-review before presenting to team
  • Commits only when build + tests pass

Creative Problem-Solving:
  • Verifies technical feasibility — checks that referenced SDKs, packages,
    and APIs actually exist before implementation begins
  • Prefers real implementations over mocks — if a real SDK or library exists,
    uses it instead of creating mock interfaces
  • Proposes alternatives when dependencies don't exist — searches for
    compatible packages, direct API calls, or workarounds
  • Never leaves features unimplemented — every committed feature must
    actually function, not just compile

Team Blockers:
  • If ANY teammate's status is BLOCKED due to Dev's work, Dev stops
    immediately and addresses the concern before continuing
  • This is non-negotiable — BLOCKED is a hard stop

Satisfaction Criteria:
  • All phases completed (or STOP directive reached)
  • Code follows codebase patterns, TDD followed
  • Build passes, working code committed
  • No teammate BLOCKED, real implementations used where feasible
"""
    },
    "security": {
        "title": "Security — Security Architect",
        "short": "Threat modeling, secure defaults, least privilege, attack surface",
        "detail": """
Authority: TIER 1 — wins all security disputes

Responsibilities:
  • Raises ALL security concerns during design discussion (Phase 0B)
  • Reviews implementation for vulnerabilities during each phase
  • Verifies security architecture is implemented correctly, not just designed
  • Proposes creative security solutions rather than just blocking

Non-Negotiables (NEVER compromise):
  • Network isolation violations
  • Identity/credential exposure to untrusted contexts
  • Secrets in code, logs, or errors

Vigilant During Implementation:
  • Monitors for actual vulnerabilities AND verifies security architecture
  • If a concern is dismissed as "MVP" or "placeholder", verifies the
    dismissal is justified
  • When blocking Dev, helps solve the problem — doesn't just block and wait

Satisfaction Criteria:
  • All phases reviewed for security
  • No secrets in code/logs/errors
  • All inputs validated, network/identity isolation maintained
  • Audit logging for security events
"""
    },
    "pm": {
        "title": "PM — Product Manager & Scrum Master",
        "short": "Acceptance criteria, progress tracking, user delight, scope control",
        "detail": """
Authority: Tie-breaker on non-security conflicts (user delight wins)

Responsibilities:
  • Leads design discussion (Phase 0B) — presents plan, facilitates agreement
  • Verifies technical feasibility with Dev during design — confirms SDKs and
    services exist before implementation begins
  • Tracks progress in _INDEX.md after each phase completion
  • Defines clear, testable acceptance criteria
  • Controls scope — challenges additions but accepts Security requirements

Quality Gates:
  • Verifies features actually work, not just compile
  • Asks QA to run the application and validate behavior, not just unit tests
  • Challenges mocks — if Dev proposes a mock where real works, pushes back
  • Mocks persisting across multiple phases are flagged as red flags

Satisfaction Criteria:
  • All phases completed with commit hashes in _INDEX.md
  • Application actually runs (verified by QA)
  • No features left as stubs/mocks that could have been real
  • User journey is intuitive, error messages helpful
"""
    },
    "qa": {
        "title": "QA — Quality Assurance & User Advocate",
        "short": "Testing, edge cases, user journey validation, integration tests",
        "detail": """
Dual Role: Mechanical testing + User journey validation

Responsibilities:
  • Runs actual tests — doesn't just review code
  • Owns integration tests and E2E tests
  • Validates user journey — reports confusion, friction
  • Tests edge cases — empty inputs, timeouts, concurrent access
  • Verifies TDD — ensures tests come before/with implementation

Application Verification:
  • Before final sign-off, STARTS the actual application
  • Verifies at least one real endpoint or user flow works E2E
  • Unit tests passing is necessary but NOT sufficient
  • Challenges mock-only tests — tests should prove real behavior

Test Ownership:
  • Unit tests: Dev writes, QA verifies coverage
  • Integration tests: QA owns
  • E2E tests: QA owns
  • User journey: QA owns

Satisfaction Criteria:
  • All phases tested, >70% coverage on new code
  • Application starts and responds to real requests
  • Tests exercise real code paths, not just mocks
  • Edge cases tested, all tests pass consistently
"""
    },
    "sre": {
        "title": "SRE — Site Reliability Engineer",
        "short": "Observability, debuggability, self-improving software, failure modes",
        "detail": """
Core Philosophy: Self-Improving Software
  Logs should be rich enough that another AI agent can read them,
  understand issues, create work items, and implement fixes.

Responsibilities:
  • Proposes observability requirements during design discussion
  • Ensures structured logging with correlation IDs
  • Verifies health checks are meaningful (test real dependencies)
  • Documents failure modes
  • Ensures no silent failures — every catch block logs

The 3 AM Test:
  If this breaks at 3 AM, can on-call diagnose from logs alone?

Phase Focus:
  • Early phases: Basic logging, health check placeholders
  • Middle phases: Error logging, structured exceptions
  • Final phase: Full review — 3 AM test, correlation IDs, metrics,
    verify the system starts and health endpoints respond

Satisfaction Criteria:
  • All phases have observability
  • Health check exists and is meaningful
  • Structured logging with correlation IDs
  • All errors logged, logs are AI-parseable
  • System starts successfully and health endpoints respond
"""
    },
    "dynamic": {
        "title": "Dynamic Personas — Task-Adaptive Team Assembly",
        "short": "Domain specialists generated to match non-code and mixed tasks",
        "detail": """
When It Activates:
  • Non-code tasks (data analysis, research, writing, etc.)
  • Mixed tasks (code + other domains)
  • NOT used when --static-personas is set

How It Works:
  • Mandali reads the task and identifies which domains need expertise
  • Each domain gets adversarial coverage: a builder and a challenger
  • Cross-domain tasks get an additional coordinator for coherence
  • Generated personas carry the same behavioral depth as the static team:
    engagement rules, satisfaction criteria, conflict resolution protocols

Behavioral Contract:
  • Same @mention protocol as static personas
  • Same satisfaction tracking (WORKING / BLOCKED / SATISFIED)
  • Same conflict resolution (tie-breaker authority, 2-strike rule)
  • Same verification loop applies after all agents declare SATISFIED

Override Options:
  • --static-personas    Force the code team regardless of task type
  • --domains <list>     Specify domains manually (skips auto-detection)
                         e.g., --domains analytics,writing

Generated persona files are stored in:
  <out-path>/mandali-artifacts/dynamic-personas/*.persona.md
"""
    },
}


def show_persona_description(persona_id: str):
    """Show detailed description of a persona."""
    persona_id = persona_id.lower().strip()
    
    if persona_id not in PERSONA_DESCRIPTIONS:
        valid = ", ".join(PERSONA_DESCRIPTIONS.keys())
        console.print(f"[red]Unknown persona: '{persona_id}'. Valid options: {valid}[/red]")
        sys.exit(1)
    
    p = PERSONA_DESCRIPTIONS[persona_id]
    console.print(f"\n[bold bright_blue]{p['title']}[/bold bright_blue]")
    console.print(f"[bright_black]{p['short']}[/bright_black]\n")
    console.print(p['detail'].strip())
    console.print()


def run_teams_setup() -> int:
    """
    Interactive one-time setup for Teams integration.
    
    Architecture: Cloud relay on Azure App Service with MSI auth.
    - Creates: Resource Group, User-Assigned MSI, Azure Bot, App Service (relay)
    - Deploys relay code, enables WebSockets, assigns MSI
    - Builds Teams app package (ZIP for sideloading)
    - Saves local config (relay_url + api_key)
    
    No dev tunnels, no app passwords, no local server needed.
    """
    import secrets
    import shutil
    import subprocess
    import zipfile
    
    # Force UTF-8 output on Windows to avoid Rich legacy renderer issues
    if sys.platform == "win32":
        import io
        sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8', errors='replace')
    
    console = Console()
    console.print("\n[bold cyan]Mandali Teams Setup[/bold cyan]")
    console.print("=" * 50)
    console.print("This will create Azure resources and deploy a cloud relay.\n")
    console.print("Architecture: Teams -> Bot Service -> Cloud Relay <-WebSocket-> mandali")
    console.print("No dev tunnels. No local server. No app passwords.\n")
    
    # Helper: on Windows, subprocess needs shell=True for .cmd files like az
    def run_az(args_list, **kwargs):
        """Run az CLI command, handling Windows .cmd wrapper."""
        kwargs.setdefault("capture_output", True)
        kwargs.setdefault("text", True)
        kwargs.setdefault("timeout", 120)
        if sys.platform == "win32":
            kwargs["shell"] = True
            return subprocess.run(["az"] + args_list, **kwargs)
        return subprocess.run(["az"] + args_list, **kwargs)
    
    # --- Step 1: Check prerequisites ---
    console.print("[bold]Step 1/6: Checking prerequisites...[/bold]")
    
    # Check az CLI
    az_path = shutil.which("az") or shutil.which("az.cmd")
    if not az_path:
        console.print("[red]✗ Azure CLI (az) not found.[/red]")
        console.print("  Install: winget install Microsoft.AzureCLI")
        console.print("  Then: az login")
        return 1
    
    # Check az is logged in
    try:
        result = run_az(["account", "show", "--query", "{name:name, id:id, tenantId:tenantId}", "-o", "json"], timeout=30)
        if result.returncode != 0:
            console.print("[red]✗ Azure CLI not logged in.[/red]")
            console.print("  Run: az login")
            return 1
        account_info = json.loads(result.stdout)
        subscription_name = account_info.get("name", "Unknown")
        subscription_id = account_info.get("id", "")
        tenant_id = account_info.get("tenantId", "")
        console.print(f"  [green]✓[/green] Azure CLI logged in")
        console.print(f"    Subscription: {subscription_name}")
        console.print(f"    Tenant: {tenant_id}")
    except subprocess.TimeoutExpired:
        console.print("[red]✗ Azure CLI timed out. Try running 'az account show' manually.[/red]")
        return 1
    
    # Check relay directory exists
    relay_dir = Path(__file__).parent / "relay"
    if not (relay_dir / "app.py").exists():
        console.print(f"[red]✗ Relay code not found at {relay_dir}[/red]")
        console.print("  The relay/ directory should be in the mandali repo.")
        return 1
    console.print(f"  [green]✓[/green] Relay code found at {relay_dir}")
    
    console.print()
    
    # Check if already configured
    config_path = Path.home() / ".copilot" / "mandali-teams.json"
    if config_path.exists():
        existing = json.loads(config_path.read_text(encoding="utf-8"))
        console.print(f"[yellow]⚠ Existing config found at {config_path}[/yellow]")
        console.print(f"  Relay URL: {existing.get('relay_url', 'N/A')}")
        response = input("  Overwrite and create new resources? (y/N): ").strip().lower()
        if response != "y":
            console.print("Setup cancelled.")
            return 0
        console.print()
    
    # Generate unique suffix for resource names
    suffix = secrets.token_hex(3)  # 6 hex chars
    resource_group = "mandali-relay-rg"
    msi_name = f"mandali-bot-id-{suffix}"
    bot_name = f"mandali-bot-{suffix}"
    app_name = f"mandali-relay-{suffix}"
    plan_name = "mandali-relay-plan"
    location = "centralus"
    ws_api_key = secrets.token_hex(16)
    
    console.print(f"  Resource names: bot=[cyan]{bot_name}[/cyan], app=[cyan]{app_name}[/cyan]")
    console.print()
    
    # --- Step 2: Create Azure resources ---
    console.print("[bold]Step 2/6: Creating Azure resources...[/bold]")
    
    # 2a. Resource group (idempotent)
    console.print("  Creating resource group...")
    result = run_az(["group", "create", "--name", resource_group, "--location", location, "-o", "none"])
    if result.returncode != 0:
        console.print(f"[red]✗ Failed to create resource group: {result.stderr}[/red]")
        return 1
    console.print(f"  [green]✓[/green] Resource group: {resource_group}")
    
    # 2b. User-Assigned Managed Identity
    console.print("  Creating managed identity...")
    result = run_az([
        "identity", "create",
        "--name", msi_name,
        "--resource-group", resource_group,
        "--location", location,
        "--query", "{clientId:clientId, id:id}",
        "-o", "json",
    ])
    if result.returncode != 0:
        # May already exist — try to get it
        result = run_az([
            "identity", "show",
            "--name", msi_name,
            "--resource-group", resource_group,
            "--query", "{clientId:clientId, id:id}",
            "-o", "json",
        ])
        if result.returncode != 0:
            console.print(f"[red]✗ Failed to create/get MSI: {result.stderr}[/red]")
            return 1
    
    msi_info = json.loads(result.stdout)
    msi_client_id = msi_info["clientId"]
    msi_resource_id = msi_info["id"]
    console.print(f"  [green]✓[/green] MSI: {msi_name} (client ID: {msi_client_id[:8]}...)")
    
    # 2c. Azure Bot with UserAssignedMSI
    console.print("  Creating Azure Bot...")
    result = run_az([
        "bot", "create",
        "--name", bot_name,
        "--resource-group", resource_group,
        "--app-type", "UserAssignedMSI",
        "--appid", msi_client_id,
        "--tenant-id", tenant_id,
        "--msi-resource-id", msi_resource_id,
        "--sku", "F0",
    ])
    if result.returncode != 0:
        console.print(f"[red]✗ Failed to create bot: {result.stderr}[/red]")
        return 1
    console.print(f"  [green]✓[/green] Azure Bot: {bot_name}")
    
    # 2d. Add Teams channel
    console.print("  Adding Teams channel...")
    result = run_az([
        "bot", "msteams", "create",
        "--name", bot_name,
        "--resource-group", resource_group,
    ])
    if result.returncode != 0:
        console.print(f"[yellow]⚠ Teams channel may need manual setup: {result.stderr}[/yellow]")
    else:
        console.print(f"  [green]✓[/green] Teams channel added")
    
    console.print()
    
    # --- Step 3: Deploy relay to App Service ---
    console.print("[bold]Step 3/6: Deploying relay to App Service...[/bold]")
    
    # 3a. Create App Service Plan
    console.print("  Creating App Service Plan (B1 Linux)...")
    result = run_az([
        "appservice", "plan", "create",
        "--name", plan_name,
        "--resource-group", resource_group,
        "--sku", "B1",
        "--is-linux",
        "--location", location,
        "-o", "none",
    ])
    if result.returncode != 0:
        # May already exist
        if "already exists" not in result.stderr.lower() and "conflict" not in result.stderr.lower():
            console.print(f"[red]✗ Failed to create plan: {result.stderr}[/red]")
            return 1
    console.print(f"  [green]✓[/green] Plan: {plan_name}")
    
    # 3b. Create Web App
    console.print("  Creating Web App...")
    result = run_az([
        "webapp", "create",
        "--name", app_name,
        "--resource-group", resource_group,
        "--plan", plan_name,
        "--runtime", "PYTHON:3.11",
        "-o", "none",
    ])
    if result.returncode != 0:
        console.print(f"[red]✗ Failed to create webapp: {result.stderr}[/red]")
        return 1
    console.print(f"  [green]✓[/green] Web App: {app_name}")
    
    # 3c. Enable WebSockets
    console.print("  Enabling WebSockets...")
    run_az([
        "webapp", "config", "set",
        "--name", app_name,
        "--resource-group", resource_group,
        "--web-sockets-enabled", "true",
        "-o", "none",
    ])
    console.print(f"  [green]✓[/green] WebSockets enabled")
    
    # 3d. Assign MSI to the Web App
    console.print("  Assigning managed identity...")
    result = run_az([
        "webapp", "identity", "assign",
        "--name", app_name,
        "--resource-group", resource_group,
        "--identities", msi_resource_id,
        "-o", "none",
    ])
    if result.returncode != 0:
        console.print(f"[yellow]⚠ MSI assignment warning: {result.stderr}[/yellow]")
    else:
        console.print(f"  [green]✓[/green] MSI assigned to Web App")
    
    # 3e. Set environment variables
    console.print("  Configuring environment variables...")
    result = run_az([
        "webapp", "config", "appsettings", "set",
        "--name", app_name,
        "--resource-group", resource_group,
        "--settings",
        f"MICROSOFT_APP_ID={msi_client_id}",
        f"MICROSOFT_APP_TENANT_ID={tenant_id}",
        f"WS_API_KEY={ws_api_key}",
        "WEBSITES_CONTAINER_START_TIME_LIMIT=300",
        "-o", "none",
    ])
    if result.returncode != 0:
        console.print(f"[red]✗ Failed to set app settings: {result.stderr}[/red]")
        return 1
    console.print(f"  [green]✓[/green] Environment configured")
    
    # 3f. Set startup command (pip install at startup since zip deploy skips Oryx build)
    console.print("  Setting startup command...")
    startup_cmd = "pip install -r /home/site/wwwroot/requirements.txt && gunicorn app:app --bind 0.0.0.0:8000 --worker-class uvicorn.workers.UvicornWorker --timeout 120"
    run_az([
        "webapp", "config", "set",
        "--name", app_name,
        "--resource-group", resource_group,
        "--startup-file", startup_cmd,
        "-o", "none",
    ])
    console.print(f"  [green]✓[/green] Startup command set")
    
    # 3g. Deploy relay code via zip deploy
    console.print("  Deploying relay code (this may take a minute)...")
    
    # Create a temporary zip of the relay directory
    import tempfile
    with tempfile.NamedTemporaryFile(suffix=".zip", delete=False) as tmp:
        deploy_zip_path = tmp.name
    
    try:
        with zipfile.ZipFile(deploy_zip_path, "w", zipfile.ZIP_DEFLATED) as zf:
            for f in relay_dir.iterdir():
                if f.is_file() and f.name not in ("test_local.py", ".gitignore") and not f.name.startswith("."):
                    zf.write(f, f.name)
        
        result = run_az([
            "webapp", "deploy",
            "--name", app_name,
            "--resource-group", resource_group,
            "--src-path", deploy_zip_path,
            "--type", "zip",
            "--timeout", "600",
        ], timeout=600)
        
        if result.returncode != 0:
            console.print(f"[red]✗ Deploy failed: {result.stderr}[/red]")
            return 1
        
        console.print(f"  [green]✓[/green] Relay deployed to https://{app_name}.azurewebsites.net")
        console.print(f"  [dim](First startup takes ~2 min for pip install)[/dim]")
    finally:
        try:
            os.unlink(deploy_zip_path)
        except OSError:
            pass
    
    console.print()
    
    # --- Step 4: Set bot messaging endpoint ---
    console.print("[bold]Step 4/6: Configuring bot endpoint...[/bold]")
    
    endpoint = f"https://{app_name}.azurewebsites.net/api/messages"
    result = run_az([
        "bot", "update",
        "--name", bot_name,
        "--resource-group", resource_group,
        "--endpoint", endpoint,
    ])
    if result.returncode != 0:
        console.print(f"[yellow]⚠ Could not set endpoint automatically: {result.stderr}[/yellow]")
        console.print(f"  Set it manually in Azure Portal to: {endpoint}")
    else:
        console.print(f"  [green]✓[/green] Bot endpoint: {endpoint}")
    
    console.print()
    
    # --- Step 5: Save local config ---
    console.print("[bold]Step 5/6: Saving configuration...[/bold]")
    
    relay_config = {
        "relay_url": f"wss://{app_name}.azurewebsites.net/ws",
        "api_key": ws_api_key,
        "bot_name": bot_name,
        "app_name": app_name,
        "resource_group": resource_group,
        "msi_client_id": msi_client_id,
        "tenant_id": tenant_id,
    }
    
    config_path.parent.mkdir(parents=True, exist_ok=True)
    config_path.write_text(json.dumps(relay_config, indent=2), encoding="utf-8")
    
    # Set restrictive permissions on Unix
    if sys.platform != "win32":
        os.chmod(config_path, 0o600)
    
    console.print(f"  [green]✓[/green] Config saved to {config_path}")
    console.print()
    
    # --- Step 6: Build Teams app package ---
    console.print("[bold]Step 6/6: Building Teams app package...[/bold]")
    
    teams_app_dir = Path(__file__).parent / "teams-app"
    manifest_path = teams_app_dir / "manifest.json"
    
    if not manifest_path.exists():
        console.print(f"[red]✗ Manifest not found at {manifest_path}[/red]")
        return 1
    
    # Template the manifest with MSI client ID (used as bot App ID)
    manifest = manifest_path.read_text(encoding="utf-8")
    manifest = manifest.replace("{{APP_ID}}", msi_client_id)
    
    # Create ZIP
    zip_path = teams_app_dir / "mandali-bot.zip"
    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("manifest.json", manifest)
        for icon in ["color.png", "outline.png"]:
            icon_path = teams_app_dir / icon
            if icon_path.exists():
                zf.write(icon_path, icon)
    
    console.print(f"  [green]✓[/green] App package: {zip_path}")
    console.print()
    
    # --- Done! ---
    console.print("[bold green]✓ Setup complete![/bold green]")
    console.print()
    console.print("[bold]One manual step remaining:[/bold]")
    console.print(f"  1. Open Microsoft Teams")
    console.print(f"  2. Apps → Manage your apps → Upload a custom app")
    console.print(f"  3. Select: [cyan]{zip_path}[/cyan]")
    console.print(f"  4. Add the bot to your team/channel")
    console.print(f"  5. Send @Mandali any message to initialize the connection")
    console.print()
    console.print("[bold]Then run:[/bold]")
    console.print(f"  mandali --plan <your-plan> --out-path <output-dir> --teams")
    console.print()
    console.print(f"[dim]Relay URL: wss://{app_name}.azurewebsites.net/ws[/dim]")
    console.print(f"[dim]Monthly cost: ~$13 (App Service B1). Free tier doesn't support WebSockets.[/dim]")
    console.print(f"[dim]To tear down: az group delete --name {resource_group} --yes[/dim]")
    console.print()
    
    return 0


def main():
    parser = argparse.ArgumentParser(
        description="Mandali — assembles the right team for any task, then makes them argue about it until the work is actually good",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Personas:
  Mandali assembles a team to match the task.

  Code tasks get the hand-tuned code team:
    dev        Senior Developer -- implementation, code quality, TDD
    security   Security Architect -- threat modeling, secure defaults
    pm         Product Manager -- acceptance criteria, progress tracking
    qa         Quality Assurance -- testing, edge cases, user journeys
    sre        Site Reliability Engineer -- observability, failure modes

  Non-code and mixed tasks get generated domain specialists with the same
  behavioral depth. Use --static-personas to force the code team, or
  --domains to specify which domains need coverage.

Workspace Isolation:
  If --out-path is inside a git repo, Mandali automatically creates a git worktree
  in a sibling directory so agents work in isolation. Your original directory is
  never touched. After the run, you can merge or discard the changes.

Verification (Trust but Verify):
  After all agents declare SATISFIED, a verification agent compares plan vs
  implementation. If gaps are found, conversation is archived and the team is
  relaunched with gap context. Use --max-retries to control cycles (default: 5,
  0 = disable verification).

Use --describe <persona> for detailed information about a specific persona.
Example: python mandali.py --describe dev
         python mandali.py --describe dynamic
"""
    )
    
    # Required: output path where Mandali will work
    parser.add_argument('--out-path', type=Path,
                        help='Output directory where Mandali will create files (required for run)')
    
    group = parser.add_mutually_exclusive_group()
    group.add_argument('--plan', type=Path, help='Path to plan file (or _INDEX.md for phased plans)')
    group.add_argument('--prompt', type=str, help='Prompt with instructions and references to plan files')
    
    parser.add_argument('--generate-plan', action='store_true', default=False,
                        help='Run interview + plan generation (default: skip, use existing plans)')
    parser.add_argument('--stall-timeout', type=int, default=5, 
                        help='Minutes of inactivity before human escalation (default: 5)')
    parser.add_argument('--max-retries', type=int, default=5,
                        help='Max verification-relaunch cycles after victory (0 = no verification, default: 5)')
    parser.add_argument('--verbose', action='store_true', help='Verbose output')
    parser.add_argument('--debug', action='store_true', 
                        help='Log all LLM requests/responses to mandali-artifacts/debug.jsonl')
    parser.add_argument('--version', action='version', version=f'mandali {__version__}')
    parser.add_argument('--describe', type=str, metavar='PERSONA',
                        help='Show detailed description of a persona (dev, security, pm, qa, sre, dynamic)')
    parser.add_argument('--static-personas', action='store_true', default=False,
                        help='Force static code team (skip task classification). --domains is ignored.')
    parser.add_argument('--domains', type=str, default=None,
                        help='Comma-separated domain list (e.g., analytics,writing). Overrides classifier. '
                             'Infers task_type: no "software-development" → non-software, "software-development" present → mixed.')
    parser.add_argument('--teams', action='store_true', default=False,
                        help='Enable Teams integration for notifications and remote replies')
    parser.add_argument('--setup-teams', action='store_true', default=False,
                        help='One-time setup: provision Azure Bot + cloud relay for Teams integration')
    
    args = parser.parse_args()
    
    # Check for updates (non-blocking background thread)
    check_for_updates_async()
    
    # Handle --describe
    if args.describe:
        show_persona_description(args.describe)
        sys.exit(0)
    
    # Handle --setup-teams
    if args.setup_teams:
        exit_code = run_teams_setup()
        sys.exit(exit_code)
    
    # Validate required args for run mode
    if not args.out_path:
        parser.error("--out-path is required when running the team")
    if not args.plan and not args.prompt:
        parser.error("one of --plan or --prompt is required when running the team")
    if args.max_retries < 0:
        parser.error("--max-retries must be >= 0")
    if args.stall_timeout < 1:
        parser.error("--stall-timeout must be >= 1")
    
    try:
        exit_code = asyncio.run(async_main(args))
        sys.exit(exit_code)
    except KeyboardInterrupt:
        log("Interrupted by user", "WARN")
        sys.exit(1)


if __name__ == "__main__":
    main()
