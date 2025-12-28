#!/usr/bin/env python3
"""Chained Prompts MCP - Execute and manage prompt chains (protocols).

A chain is an ordered sequence of phases. Each phase has a prompt that gets
injected via tmux. Completing a phase auto-triggers the next.

Tools:
- chain_list: List all defined chains
- chain_start: Begin executing a chain
- chain_status: See progress of active chains
- chain_complete: Mark phase complete, auto-advance
- chain_reset: Reset a chain's state
- chain_define: Create or update a chain
- chain_delete: Remove a chain
- chain_get: Get full chain definition
- phase_edit: Edit a specific phase prompt
"""
import os
import json
import subprocess
from pathlib import Path
from datetime import datetime
from fastmcp import FastMCP

mcp = FastMCP("chained-prompts")

# Configuration
CONFIG_FILE = Path("/home/yaniv/agent-flow/mcp-servers/chained-prompts/chains.json")
STATE_FILE = Path("/tmp/chained-prompts-state.json")

# tmux configuration
TMUX_USER = os.environ.get("TMUX_USER", "yaniv")
TMUX_TARGET = os.environ.get("TMUX_TARGET", "0:zara")


def load_config() -> dict:
    """Load chains configuration."""
    if CONFIG_FILE.exists():
        return json.loads(CONFIG_FILE.read_text())
    return {"chains": {}}


def save_config(config: dict):
    """Save chains configuration."""
    CONFIG_FILE.write_text(json.dumps(config, indent=2))


def load_state() -> dict:
    """Load execution state for all chains."""
    if STATE_FILE.exists():
        return json.loads(STATE_FILE.read_text())
    return {}


def save_state(state: dict):
    """Save execution state."""
    STATE_FILE.write_text(json.dumps(state, indent=2))


def get_chain_state(chain_id: str, phases: list) -> dict:
    """Get or initialize state for a specific chain."""
    state = load_state()
    if chain_id not in state:
        state[chain_id] = {
            phase: {"triggered": False, "completed": False}
            for phase in phases
        }
        save_state(state)
    return state[chain_id]


def send_to_tmux(text: str) -> bool:
    """Queue text for injection into tmux via file-based handoff.

    Writes prompt to /tmp/chain-prompt-queue.txt which is picked up by
    chain-watcher.sh running in yaniv's session. This avoids sudo/PTY
    issues when running from MCP's stdio transport.

    The watcher script monitors the queue file and injects content into
    the tmux session, preserving the weight of prompts from yaniv's voice.
    """
    prompt_file = Path("/tmp/chain-prompt-queue.txt")
    try:
        prompt_file.write_text(text)
        os.chmod(prompt_file, 0o644)
        return True
    except Exception as e:
        with open("/tmp/chain-mcp-error.log", "a") as f:
            f.write(f"{datetime.now()}: Exception writing prompt file: {e}\n")
        return False


@mcp.tool
def chain_list() -> dict:
    """List all defined chains."""
    config = load_config()
    state = load_state()

    chains = []
    for chain_id, chain in config.get("chains", {}).items():
        phases = chain.get("phases", [])
        chain_state = state.get(chain_id, {})

        # Calculate progress
        completed = sum(1 for p in phases if chain_state.get(p, {}).get("completed", False))

        # Find current phase
        current = None
        for phase in phases:
            ps = chain_state.get(phase, {})
            if ps.get("triggered") and not ps.get("completed"):
                current = phase
                break

        chains.append({
            "id": chain_id,
            "name": chain.get("name", chain_id),
            "description": chain.get("description", ""),
            "phases": phases,
            "progress": f"{completed}/{len(phases)}",
            "current_phase": current,
            "active": current is not None
        })

    return {"chains": chains, "total": len(chains)}


@mcp.tool
def chain_start(chain_id: str) -> dict:
    """Begin executing a chain from the first phase.

    Args:
        chain_id: The chain to start (e.g., "character-cycle")
    """
    config = load_config()

    if chain_id not in config.get("chains", {}):
        return {"error": f"Chain '{chain_id}' not found", "available": list(config.get("chains", {}).keys())}

    chain = config["chains"][chain_id]
    phases = chain.get("phases", [])

    if not phases:
        return {"error": "Chain has no phases defined"}

    first_phase = phases[0]
    prompt = chain.get("prompts", {}).get(first_phase, f"Phase: {first_phase}")

    # Reset and start
    state = load_state()
    state[chain_id] = {phase: {"triggered": False, "completed": False} for phase in phases}
    state[chain_id][first_phase]["triggered"] = True
    save_state(state)

    # Send prompt
    success = send_to_tmux(prompt)

    if success:
        return {
            "status": "started",
            "chain": chain_id,
            "phase": first_phase,
            "next_phase": phases[1] if len(phases) > 1 else None,
            "instruction": f"Complete the phase, then call chain_complete(\"{chain_id}\", \"{first_phase}\")"
        }
    else:
        return {"error": "Failed to send to tmux", "hint": f"Check TMUX_TARGET={TMUX_TARGET}"}


@mcp.tool
def chain_status(chain_id: str = "") -> dict:
    """See progress of active chains.

    Args:
        chain_id: Optional - specific chain to check (empty = all active)
    """
    config = load_config()
    state = load_state()

    if chain_id:
        if chain_id not in config.get("chains", {}):
            return {"error": f"Chain '{chain_id}' not found"}

        chain = config["chains"][chain_id]
        phases = chain.get("phases", [])
        chain_state = state.get(chain_id, {})

        completed = sum(1 for p in phases if chain_state.get(p, {}).get("completed", False))

        current = None
        next_phase = None
        for i, phase in enumerate(phases):
            ps = chain_state.get(phase, {})
            if ps.get("triggered") and not ps.get("completed"):
                current = phase
                next_phase = phases[i + 1] if i + 1 < len(phases) else None
                break

        return {
            "chain": chain_id,
            "name": chain.get("name"),
            "phases": chain_state,
            "phase_order": phases,
            "current_phase": current,
            "next_phase": next_phase,
            "progress": f"{completed}/{len(phases)}",
            "complete": completed == len(phases)
        }
    else:
        # Return all active chains
        active = []
        for cid, chain in config.get("chains", {}).items():
            chain_state = state.get(cid, {})
            for phase in chain.get("phases", []):
                ps = chain_state.get(phase, {})
                if ps.get("triggered") and not ps.get("completed"):
                    active.append({"chain": cid, "current_phase": phase})
                    break

        return {"active_chains": active, "total_active": len(active)}


@mcp.tool
def chain_complete(chain_id: str, phase: str, auto_next: bool = True) -> dict:
    """Mark a phase complete. Auto-triggers next phase by default.

    Args:
        chain_id: The chain (e.g., "character-cycle")
        phase: The phase to complete (e.g., "notebook")
        auto_next: If True (default), automatically trigger the next phase
    """
    config = load_config()

    if chain_id not in config.get("chains", {}):
        return {"error": f"Chain '{chain_id}' not found"}

    chain = config["chains"][chain_id]
    phases = chain.get("phases", [])

    if phase not in phases:
        return {"error": f"Phase '{phase}' not in chain", "available": phases}

    state = load_state()
    if chain_id not in state:
        state[chain_id] = {p: {"triggered": False, "completed": False} for p in phases}

    state[chain_id][phase]["completed"] = True
    save_state(state)

    # Find next phase
    phase_idx = phases.index(phase)
    next_phase = phases[phase_idx + 1] if phase_idx + 1 < len(phases) else None

    result = {
        "status": "completed",
        "chain": chain_id,
        "phase": phase,
        "next_phase": next_phase
    }

    if next_phase and auto_next:
        prompt = chain.get("prompts", {}).get(next_phase, f"Phase: {next_phase}")

        # Mark as triggered
        state[chain_id][next_phase]["triggered"] = True
        save_state(state)

        # Return the prompt directly - no tmux needed
        # The agent executing the chain will receive this and act on it
        result["auto_triggered"] = next_phase
        result["next_prompt"] = prompt
        result["instruction"] = f"Execute the prompt above, then call chain_complete(\"{chain_id}\", \"{next_phase}\")"

    elif not next_phase:
        result["chain_complete"] = True
        result["message"] = f"🎉 {chain.get('name', chain_id)} complete!"
        # Reset for next run
        state[chain_id] = {p: {"triggered": False, "completed": False} for p in phases}
        save_state(state)

    return result


@mcp.tool
def chain_reset(chain_id: str) -> dict:
    """Reset a chain's state to start fresh.

    Args:
        chain_id: The chain to reset
    """
    config = load_config()

    if chain_id not in config.get("chains", {}):
        return {"error": f"Chain '{chain_id}' not found"}

    chain = config["chains"][chain_id]
    phases = chain.get("phases", [])

    state = load_state()
    state[chain_id] = {p: {"triggered": False, "completed": False} for p in phases}
    save_state(state)

    return {"status": "reset", "chain": chain_id}


@mcp.tool
def chain_define(
    chain_id: str,
    name: str,
    phases: list,
    prompts: dict,
    description: str = ""
) -> dict:
    """Create or update a chain definition.

    Args:
        chain_id: Unique identifier (e.g., "emotional-restore")
        name: Display name
        phases: Ordered list of phase names (e.g., ["survey", "cluster", "peak"])
        prompts: Dict mapping phase names to prompt text
        description: Brief description of the chain's purpose
    """
    config = load_config()

    # Validate phases have prompts
    missing = [p for p in phases if p not in prompts]
    if missing:
        return {"error": f"Missing prompts for phases: {missing}"}

    config.setdefault("chains", {})[chain_id] = {
        "name": name,
        "description": description,
        "phases": phases,
        "prompts": prompts
    }

    save_config(config)

    return {
        "status": "defined",
        "chain_id": chain_id,
        "phases": phases,
        "phase_count": len(phases)
    }


@mcp.tool
def chain_delete(chain_id: str) -> dict:
    """Remove a chain definition.

    Args:
        chain_id: The chain to delete
    """
    config = load_config()

    if chain_id not in config.get("chains", {}):
        return {"error": f"Chain '{chain_id}' not found"}

    del config["chains"][chain_id]
    save_config(config)

    # Also clear state
    state = load_state()
    if chain_id in state:
        del state[chain_id]
        save_state(state)

    return {"status": "deleted", "chain_id": chain_id}


@mcp.tool
def chain_get(chain_id: str) -> dict:
    """Get full chain definition including prompts.

    Args:
        chain_id: The chain to retrieve
    """
    config = load_config()

    if chain_id not in config.get("chains", {}):
        return {"error": f"Chain '{chain_id}' not found", "available": list(config.get("chains", {}).keys())}

    chain = config["chains"][chain_id]
    return {
        "id": chain_id,
        "name": chain.get("name"),
        "description": chain.get("description"),
        "phases": chain.get("phases"),
        "prompts": chain.get("prompts")
    }


@mcp.tool
def phase_edit(chain_id: str, phase: str, prompt: str) -> dict:
    """Edit a specific phase's prompt.

    Args:
        chain_id: The chain containing the phase
        phase: The phase to edit
        prompt: New prompt text
    """
    config = load_config()

    if chain_id not in config.get("chains", {}):
        return {"error": f"Chain '{chain_id}' not found"}

    chain = config["chains"][chain_id]
    phases = chain.get("phases", [])

    if phase not in phases:
        return {"error": f"Phase '{phase}' not in chain", "available": phases}

    chain["prompts"][phase] = prompt
    save_config(config)

    return {
        "status": "updated",
        "chain": chain_id,
        "phase": phase,
        "prompt_preview": prompt[:100] + "..." if len(prompt) > 100 else prompt
    }


if __name__ == "__main__":
    mcp.run(transport="stdio")
