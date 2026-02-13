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


def load_persona_prompt(persona_id: str) -> str:
    persona_file = PERSONAS_DIR / f"{persona_id}.persona.md"
    with open(persona_file, 'r', encoding='utf-8') as f:
        return f.read()


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
    copilot_config_dir = Path.home() / ".copilot"
    session_config = {
        "model": model,
        "system_message": VERIFICATION_AGENT_PROMPT,
        "working_directory": str(workspace.path)
    }
    if copilot_config_dir.exists():
        session_config["config_dir"] = str(copilot_config_dir)
    if MCP_SERVERS_CONFIG:
        session_config["mcp_servers"] = MCP_SERVERS_CONFIG
    
    session = await client.create_session(session_config)
    
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
    
    session = await client.create_session({
        "model": model,
        "system_message": HANDOFF_PROMPT,
        "working_directory": str(workspace.path)
    })
    
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
    
    if content.strip():
        handoff_file = workspace.path / "HANDOFF.md"
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
    is_first: bool = False
):
    """
    Run an agent autonomously.
    Agent reads conversation, decides when to speak, appends responses.
    """
    system_prompt = load_persona_prompt(agent.id)
    
    # Create session with tools, working in the output directory
    # Include MCP servers and user's Copilot config (skills, extensions)
    copilot_config_dir = Path.home() / ".copilot"
    session_config = {
        "model": model,
        "system_message": system_prompt,
        "working_directory": str(workspace.path),  # Agent works in output directory
        "infinite_sessions": {
            "enabled": True,
            "background_compaction_threshold": 0.80,
            "buffer_exhaustion_threshold": 0.95,
        }
    }
    if copilot_config_dir.exists():
        session_config["config_dir"] = str(copilot_config_dir)
    if MCP_SERVERS_CONFIG:
        session_config["mcp_servers"] = MCP_SERVERS_CONFIG
    
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
    if "SATISFACTION_STATUS: SATISFIED" in response:
        update_satisfaction(workspace, agent_id, "SATISFIED")
    elif "SATISFACTION_STATUS: BLOCKED" in response:
        try:
            reason = response.split("SATISFACTION_STATUS: BLOCKED -")[1].split("\n")[0].strip()
            update_satisfaction(workspace, agent_id, f"BLOCKED - {reason}")
        except:
            update_satisfaction(workspace, agent_id, "BLOCKED")
    elif "SATISFACTION_STATUS: PAUSED" in response:
        update_satisfaction(workspace, agent_id, "PAUSED - Awaiting human guidance")
    elif "SATISFACTION_STATUS: WORKING" in response:
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


async def run_interview(client: CopilotClient, model: str, initial_prompt: str) -> dict:
    """Run interactive interview: generate questions upfront, walk through them, synthesize."""
    log("Starting AI Interviewer...", "AGENT")
    console.print(Panel(
        "I'll ask a few questions to understand what you want.\n"
        "The team will handle implementation details autonomously.",
        title="🎤 AI INTERVIEWER", border_style="cyan"
    ))
    
    session = await client.create_session({
        "model": model,
        "system_message": INTERVIEWER_PROMPT
    })
    
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
                                        out_path: Path) -> str:
    """Generate a TDD + PoC structured plan from interview data."""
    log("Generating phased plan...", "AGENT")
    
    # Ensure output directory and phases subfolder exist
    out_path.mkdir(parents=True, exist_ok=True)
    phases_path = out_path / "phases"
    phases_path.mkdir(parents=True, exist_ok=True)
    
    # Plan generator needs file access to create phase files
    session = await client.create_session({
        "model": model,
        "system_message": PLAN_GENERATOR_PROMPT,
        "working_directory": str(out_path)  # Work in the output directory
        # No mcp_servers - plan generator just creates files
    })
    
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
    
    prompt = f"""
Generate a PHASED implementation plan with SEPARATE FILES.

## Original Request
{initial_prompt}

## Gathered Information
{json.dumps(gathered_info, indent=2)}
{existing_context}{existing_phases}{completed}{resume_stop}

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
    
    session = await client.create_session({
        "model": model,
        "system_message": PLAN_GENERATOR_PROMPT,
        "working_directory": str(out_path)
    })
    
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
    
    session = await client.create_session({
        "model": model,
        "system_message": (
            "You extract file and folder paths from text. "
            "Return ONLY a JSON array of strings. No explanation, no markdown fencing. "
            "Example: [\"phases/_INDEX.md\", \"docs/architecture.md\", \"src/Services/\"]"
        )
    })
    
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
        session = await client.create_session({
            "model": model,
            "system_message": (
                "You analyze plan/context documents and extract file/folder paths referenced within. "
                "Return ONLY a JSON array of strings. No explanation, no markdown fencing. "
                "Look for paths in backticks, quotes, relative references, folder structures, "
                "links, and prose descriptions. Include any file or folder an implementer would need."
            )
        })
        
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
    
    # Plan reviewer just provides text feedback, no tools needed
    session = await client.create_session({
        "model": model,
        "system_message": PLAN_REVIEWER_PROMPT
        # No working_directory or mcp_servers - reviewer just provides text feedback
    })
    
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
    
    async def start(self):
        log("Starting Copilot client...", "INFO")
        self._cli_path = get_copilot_cli_path()
        log(f"Using CLI at: {self._cli_path}", "INFO")
        await self._connect_with_retry()
        log("Copilot client ready", "OK")
    
    async def _connect_with_retry(self, max_retries: int = 3):
        """Connect to Copilot CLI with retry logic for transient failures."""
        last_error = None
        for attempt in range(1, max_retries + 1):
            self.client = CopilotClient({"cli_path": self._cli_path})
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
                except:
                    pass
        self.agents.clear()
    
    async def stop(self):
        log("Stopping all agents...", "INFO")
        await self.stop_agents()
        if self.client:
            await self.client.stop()
        log("Shutdown complete", "OK")
    
    async def launch_agents(self, workspace: Workspace, plan_content: str):
        """Launch all agents as background tasks."""
        self._workspace = workspace
        self._plan_content = plan_content
        personas = self.config.get('personas', [])
        
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
                mention=f"@{persona['id'].capitalize()}"
            )
            
            # Launch as background task
            agent.task = asyncio.create_task(
                run_autonomous_agent(
                    self.client, agent, workspace, plan_content,
                    self.model, is_first=(i == 0)
                )
            )
            
            self.agents[persona['id']] = agent
            log(f"Launched {agent.mention}", "AGENT")
            
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
                        self.model, is_first=is_first
                    )
                )
                log(f"{agent.mention} relaunched", "OK")
    
    async def monitor_loop(self, workspace: Workspace, max_stall_minutes: int = 5, is_final_round: bool = True):
        """
        Interactive passive monitoring loop.
        - Shows periodic status updates
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
            
            # Check for inactivity
            last_activity = get_last_activity_time(workspace)
            idle_seconds = (datetime.now() - last_activity).total_seconds()
            
            if idle_seconds > stall_timeout:
                # Check if any agent is explicitly waiting for human
                status = read_all_satisfaction(workspace)
                waiting_for_human = any("@HUMAN" in s or "human" in s.lower() 
                                        for s in status.values())
                
                if waiting_for_human:
                    # Agents explicitly waiting for human - escalate immediately
                    log(f"Agents waiting for human input", "WARN")
                    should_continue = await self.handle_human_escalation(workspace)
                    if not should_continue:
                        return False
                    nudge_count = 0
                elif nudge_count < max_nudges:
                    # Nudge agents to continue
                    nudge_count += 1
                    self.metrics.nudges += 1
                    log(f"Nudging agents (attempt {nudge_count}/{max_nudges})", "INFO")
                    
                    append_to_conversation(workspace, "ORCHESTRATOR", f"""
@Team - No activity detected for {int(idle_seconds // 60)} minutes.

Please continue working on the plan. If you're blocked, state what you need.

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
                
                # Print status header
                timestamp = now.strftime("%H:%M:%S")
                status_table = Table(show_header=False, box=None, padding=(0, 1))
                status_table.add_row(f"[dim]{timestamp}[/dim]", "📊", status_line, f"[dim]{msgs} msgs[/dim]")
                console.print(status_table)
                
                # Print recent conversation activity
                if recent_messages:
                    for msg_line in recent_messages:
                        console.print(f"  [dim]│[/dim] {msg_line}")
            
            # Update metrics
            self.metrics.total_messages = read_conversation(workspace).count("\n[")
            
            # Check for phase completions and nudge PM if DecisionsTracker wasn't updated
            conversation_content = read_conversation(workspace)
            new_conversation = conversation_content[last_phase_check_pos:]
            if new_conversation:
                phase_completions = re.findall(
                    r'\[[\d:]+\]\s+@PM:.*?Phase\s+\d+\S*\s+[Cc]omplete',
                    new_conversation, re.DOTALL
                )
                if phase_completions:
                    last_phase_check_pos = len(conversation_content)
                    current_mtime = workspace.decisions_file.stat().st_mtime if workspace.decisions_file.exists() else 0
                    if current_mtime == decisions_mtime:
                        # DecisionsTracker hasn't been modified since last check
                        phases_str = f"{len(phase_completions)} phase(s)" if len(phase_completions) > 1 else "a phase"
                        append_to_conversation(workspace, "ORCHESTRATOR", f"""
@PM - Completion of {phases_str} detected but DecisionsTracker.md has not been updated.

Before proceeding, verify whether any deviations from the plan occurred during the completed phase(s).
If choices were made that differ from the plan or where the plan was silent, record them in:
{workspace.decisions_file}

If no deviations occurred, acknowledge this and proceed.
""")
                        log(f"Nudged PM to check DecisionsTracker ({len(phase_completions)} phase(s))", "INFO")
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
        """Handle stall by escalating to human."""
        self.metrics.human_escalations += 1
        
        status = read_all_satisfaction(workspace)
        status_lines = "\n".join([f"- @{k.capitalize()}: {v}" for k, v in status.items()])
        
        # Inject pause message
        append_to_conversation(workspace, "ORCHESTRATOR", f"""
@AllAgents - Escalating to @HUMAN for guidance.

No activity detected for several minutes. Please pause current work
and ensure things are in a consistent state.

Wait for human input before continuing.

Current status:
{status_lines}
""")
        
        log("Waiting for human input...", "HUMAN")
        
        # Show status to human
        console.print(Panel(
            f"Agents have stalled. Current status:\n{escape(status_lines)}\n\n"
            f"Conversation: {workspace.conversation_file}\n\n"
            "Options:\n"
            "  [bold]1[/bold]. Provide guidance\n"
            "  [bold]2[/bold]. View recent conversation\n"
            "  [bold]3[/bold]. Abort",
            title="⚠️  HUMAN ESCALATION", border_style="yellow"
        ))
        
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
                    log("Could not restore stash in original directory (stash preserved)", "WARN")
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

async def run_generate_plan_flow(orchestrator, prompt: str, out_path: Path) -> Optional[str]:
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
    
    # Step 2: Generate plan (creates files in out_path)
    plan_content = await generate_plan_from_interview(
        orchestrator.client, orchestrator.model, gathered_info, prompt, out_path
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
                title="📝 REVISED PLAN", border_style="bright_blue"
            ))
            if not Confirm.ask("Use revised plan?", default=True):
                return None
            plan_content = result
            break
    
    return plan_content


# ============================================================================
# Main Entry
# ============================================================================

async def async_main(args):
    global MCP_SERVERS_CONFIG
    
    config = load_config()
    
    # Load MCP server config for all sessions
    MCP_SERVERS_CONFIG = load_mcp_config()
    
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
        
        # Initialize conversation with Context Building + Round 0: Design Discussion
        append_to_conversation(workspace, "ORCHESTRATOR", f"""
@AllAgents - Welcome to Mandali!

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

1. **@PM**: Present the plan, clarify acceptance criteria, lead the discussion
2. **@Security**: Raise ALL security concerns NOW (not during implementation)
3. **@Dev**: Propose technical approach, identify risks, suggest phase adjustments
4. **@QA**: Propose test strategy for each phase
5. **@SRE**: Propose observability requirements

**Rules for Design Discussion:**
- ALL agents must participate and acknowledge the plan
- @Security must approve the security approach BEFORE implementation begins
- Team may reorder phases, add sub-phases, or adjust scope
- @PM declares design complete with: "@Team design discussion complete, begin Phase 1"

---

## Phased Plan Workflow (if using phases/ structure)

After each phase is complete:
1. @PM updates `_INDEX.md` with: ✅ Complete, commit hash
2. @PM verifies `DecisionsTracker.md` has entries for any deviations made during this phase — if choices were made that differ from the plan or where the plan was silent, they must be recorded before moving on
3. @PM announces: "@Team Phase X complete, proceeding to Phase Y"
4. If plan says "STOP after Phase X", team stops and reports to human

---

## Communication
- Use @mentions: @Dev, @PM, @Security, @QA, @SRE, @Team, @AllAgents
- End each message with SATISFACTION_STATUS

## Victory Condition
All agents SATISFIED = Implementation complete.

---

@AllAgents - Begin by reading the plan and exploring the codebase. 
Post when you're ready for design discussion.
""")
        
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
            await orchestrator.launch_agents(workspace, plan_content)
            
            # Monitor loop — always announce "proceeding to verification" when verification is enabled
            success = await orchestrator.monitor_loop(
                workspace,
                max_stall_minutes=stall_timeout,
                is_final_round=(max_retries == 0)
            )
            
            # Stop agents before verification — fresh agents get a fresh look on relaunch
            await orchestrator.stop_agents()
            
            if not success:
                break  # Human aborted or unrecoverable
            
            # Skip verification if disabled
            if max_retries == 0:
                break
            
            # Run verification
            orchestrator.metrics.verification_rounds += 1
            passed, gap_report = await run_verification(
                orchestrator.client, orchestrator.model, workspace, plan_content
            )
            
            if passed:
                orchestrator.metrics.verification_passed = True
                # Announce final victory — verification has confirmed the implementation
                await orchestrator.announce_victory(workspace, is_final=True)
                
                # Generate handoff instructions for the user
                handoff_content = await generate_handoff(
                    orchestrator.client, orchestrator.model,
                    workspace, plan_content, prompt_context or ""
                )
                if handoff_content:
                    console.print(Panel(
                        escape(handoff_content[:3000]),
                        title="📋 HANDOFF — How to Use Your Deliverable",
                        border_style="green"
                    ))
                
                await asyncio.sleep(5)
                break
            
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


def main():
    parser = argparse.ArgumentParser(
        description="Mandali — a circle of specialized AI agents that deliberate and act together",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Personas:
  dev        Senior Developer — implementation, code quality, TDD, creative problem-solving
  security   Security Architect — threat modeling, secure defaults, least privilege
  pm         Product Manager — acceptance criteria, progress tracking, user delight
  qa         Quality Assurance — testing, edge cases, user journey validation
  sre        Site Reliability Engineer — observability, debuggability, failure modes

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
    parser.add_argument('--version', action='version', version=f'mandali {__version__}')
    parser.add_argument('--describe', type=str, metavar='PERSONA',
                        help='Show detailed description of a persona (dev, security, pm, qa, sre)')
    
    args = parser.parse_args()
    
    # Check for updates (non-blocking background thread)
    check_for_updates_async()
    
    # Handle --describe
    if args.describe:
        show_persona_description(args.describe)
        sys.exit(0)
    
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
