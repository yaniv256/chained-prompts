#!/usr/bin/env python3
"""Chained Prompts MCP - Execute and manage prompt chains (protocols).

A chain is an ordered sequence of phases. Each phase has a prompt that gets
injected via tmux. Completing a phase auto-triggers the next.

Features:
- Sequential phase execution with auto-advance
- Delayed scheduling for deferred execution
- Local model execution during wait periods ("dreaming")
- State passing between chain cycles
- Auto-continue mode for continuous contemplation loops

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
- chain_schedule: Schedule a chain to start after a delay
- chain_dream: Execute local model task during wait period
- chain_pass: Pass context/seed to next cycle
- chain_auto_continue: Enable continuous cycling mode
"""
import os
import json
import subprocess
import threading
import time as time_module
from pathlib import Path
from datetime import datetime, timedelta
from fastmcp import FastMCP

mcp = FastMCP("chained-prompts")

# Configuration
CONFIG_FILE = Path("/home/yaniv/agent-flow/mcp-servers/chained-prompts/chains.json")
STATE_FILE = Path("/tmp/chained-prompts-state.json")
SCHEDULE_FILE = Path("/tmp/chained-prompts-scheduled.json")
CONTEXT_FILE = Path("/tmp/chained-prompts-context.json")

# tmux configuration
TMUX_USER = os.environ.get("TMUX_USER", "yaniv")
TMUX_TARGET_DEFAULT = "0:zara"


def _find_tmux_target() -> str:
    """Dynamically find the tmux session:window with a 'zara' window."""
    try:
        result = subprocess.run(
            ["sudo", "-u", TMUX_USER, "tmux", "list-windows", "-a", "-F",
             "#{session_name}:#{window_name}"],
            capture_output=True, text=True, timeout=5,
            stdin=subprocess.DEVNULL, start_new_session=True
        )
        if result.returncode == 0:
            for line in result.stdout.strip().split("\n"):
                if line.endswith(":zara"):
                    return line
    except Exception:
        pass
    return os.environ.get("TMUX_TARGET", TMUX_TARGET_DEFAULT)


# Active schedule threads (in-memory, lost on restart)
_schedule_threads: dict = {}


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


def load_schedule() -> dict:
    """Load scheduled chain executions."""
    if SCHEDULE_FILE.exists():
        return json.loads(SCHEDULE_FILE.read_text())
    return {"scheduled": {}, "auto_continue": {}}


def save_schedule(schedule: dict):
    """Save scheduled chain executions."""
    SCHEDULE_FILE.write_text(json.dumps(schedule, indent=2))


def load_context() -> dict:
    """Load chain context/seed data for passing between cycles."""
    if CONTEXT_FILE.exists():
        return json.loads(CONTEXT_FILE.read_text())
    return {}


def save_context(context: dict):
    """Save chain context/seed data."""
    CONTEXT_FILE.write_text(json.dumps(context, indent=2))


def call_local_model(prompt: str, model: str = "qwen3:4b") -> str:
    """Call local model via Ollama API. Returns response text."""
    import urllib.request
    import urllib.error

    try:
        data = json.dumps({
            "model": model,
            "prompt": prompt,
            "stream": False
        }).encode()

        req = urllib.request.Request(
            "http://localhost:11434/api/generate",
            data=data,
            headers={"Content-Type": "application/json"}
        )

        with urllib.request.urlopen(req, timeout=300) as resp:
            result = json.loads(resp.read().decode())
            return result.get("response", "")
    except Exception as e:
        return f"[local model error: {e}]"


def _run_scheduled_chain(chain_id: str, delay_seconds: float, context: dict = None):
    """Background thread that waits then triggers a chain."""
    def runner():
        time_module.sleep(delay_seconds)

        # Check if we were cancelled
        schedule = load_schedule()
        if chain_id not in schedule.get("scheduled", {}):
            return  # Cancelled

        # Remove from scheduled
        del schedule["scheduled"][chain_id]
        save_schedule(schedule)

        # Store context if provided
        if context:
            ctx = load_context()
            ctx[chain_id] = context
            save_context(ctx)

        # Trigger the chain via tmux
        config = load_config()
        if chain_id in config.get("chains", {}):
            chain = config["chains"][chain_id]
            phases = chain.get("phases", [])
            if phases:
                first_phase = phases[0]
                prompt = chain.get("prompts", {}).get(first_phase, f"Phase: {first_phase}")

                # Inject context if available
                ctx = load_context().get(chain_id, {})
                if ctx:
                    context_str = json.dumps(ctx, indent=2)
                    prompt = f"[CONTEXT FROM PREVIOUS CYCLE]\n{context_str}\n\n{prompt}"

                # Reset and start
                state = load_state()
                state[chain_id] = {phase: {"triggered": False, "completed": False} for phase in phases}
                state[chain_id][first_phase]["triggered"] = True
                save_state(state)

                send_to_tmux(prompt)

    thread = threading.Thread(target=runner, daemon=True)
    thread.start()
    _schedule_threads[chain_id] = thread


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
    """Send text to tmux session using load-buffer for multi-line text."""
    import time
    import tempfile
    tmux_target = _find_tmux_target()
    try:
        # Write text to temp file, then use tmux load-buffer + paste-buffer
        with tempfile.NamedTemporaryFile(mode='w', suffix='.txt', delete=False) as f:
            f.write(text)
            temp_path = f.name

        # Make readable by yaniv
        os.chmod(temp_path, 0o644)

        # Use start_new_session=True to detach from MCP's stdio
        # This prevents blocking on PTY allocation
        subprocess.run(
            ["sudo", "-u", TMUX_USER, "tmux", "load-buffer", temp_path],
            check=True, timeout=10,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True
        )

        subprocess.run(
            ["sudo", "-u", TMUX_USER, "tmux", "paste-buffer", "-t", tmux_target],
            check=True, timeout=10,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True
        )

        time.sleep(0.3)

        subprocess.run(
            ["sudo", "-u", TMUX_USER, "tmux", "send-keys", "-t", tmux_target, "Enter"],
            check=True, timeout=10,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True
        )

        os.unlink(temp_path)
        return True
    except subprocess.CalledProcessError as e:
        with open("/tmp/chain-mcp-error.log", "a") as f:
            f.write(f"{datetime.now()}: CalledProcessError: {e}\n")
            f.write(f"  TMUX_USER={TMUX_USER} tmux_target={tmux_target}\n")
        return False
    except Exception as e:
        with open("/tmp/chain-mcp-error.log", "a") as f:
            f.write(f"{datetime.now()}: Exception: {e}\n")
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
        return {"error": "Failed to send to tmux", "hint": f"Check tmux target (resolved: {_find_tmux_target()})"}


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

        # Check for auto-continue mode
        schedule = load_schedule()
        auto_config = schedule.get("auto_continue", {}).get(chain_id)
        if auto_config:
            delay_minutes = auto_config.get("delay_minutes", 5.0)
            dream_prompt = auto_config.get("dream_prompt", "")

            # Get current context to pass forward
            ctx = load_context().get(chain_id, {})

            # Schedule next cycle
            trigger_time = datetime.now() + timedelta(minutes=delay_minutes)
            schedule.setdefault("scheduled", {})[chain_id] = {
                "trigger_at": trigger_time.isoformat(),
                "delay_minutes": delay_minutes,
                "context": ctx,
                "dream_prompt": dream_prompt,
                "dream_model": "qwen3:4b"
            }
            save_schedule(schedule)

            # Start background thread
            _run_scheduled_chain(chain_id, delay_minutes * 60, ctx)

            result["auto_continue"] = {
                "scheduled": True,
                "next_cycle_at": trigger_time.strftime("%H:%M:%S"),
                "delay_minutes": delay_minutes,
                "dreaming": bool(dream_prompt)
            }

            # If dream prompt, run it in background
            if dream_prompt:
                def dream_runner():
                    dream_result = call_local_model(dream_prompt, "qwen3:4b")
                    c = load_context()
                    c.setdefault(chain_id, {})["dream_result"] = dream_result
                    save_context(c)
                threading.Thread(target=dream_runner, daemon=True).start()

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


@mcp.tool
def chain_schedule(
    chain_id: str,
    delay_minutes: float,
    context: str = "",
    dream_prompt: str = "",
    dream_model: str = "qwen3:4b"
) -> dict:
    """Schedule a chain to start after a delay.

    The execution pointer returns to you now. After the delay, the chain
    will be triggered via tmux injection.

    Args:
        chain_id: The chain to schedule
        delay_minutes: Minutes to wait before triggering
        context: Optional JSON context to pass to the chain (seed data)
        dream_prompt: Optional prompt for local model to process during wait
        dream_model: Model for dream processing (default: qwen3:4b)
    """
    config = load_config()

    if chain_id not in config.get("chains", {}):
        return {"error": f"Chain '{chain_id}' not found"}

    schedule = load_schedule()
    trigger_time = datetime.now() + timedelta(minutes=delay_minutes)

    # Parse context if provided
    ctx = {}
    if context:
        try:
            ctx = json.loads(context)
        except json.JSONDecodeError:
            ctx = {"raw": context}

    # Store scheduled info
    schedule.setdefault("scheduled", {})[chain_id] = {
        "trigger_at": trigger_time.isoformat(),
        "delay_minutes": delay_minutes,
        "context": ctx,
        "dream_prompt": dream_prompt,
        "dream_model": dream_model
    }
    save_schedule(schedule)

    # Start background thread
    delay_seconds = delay_minutes * 60
    _run_scheduled_chain(chain_id, delay_seconds, ctx)

    result = {
        "status": "scheduled",
        "chain": chain_id,
        "trigger_at": trigger_time.strftime("%H:%M:%S"),
        "delay_minutes": delay_minutes
    }

    # If dream prompt provided, run it now and store result in context
    if dream_prompt:
        result["dreaming"] = True
        result["dream_hint"] = "Local model processing will run in background"

        # Run dream in background thread
        def dream_runner():
            dream_result = call_local_model(dream_prompt, dream_model)
            ctx = load_context()
            ctx.setdefault(chain_id, {})["dream_result"] = dream_result
            save_context(ctx)

        threading.Thread(target=dream_runner, daemon=True).start()

    return result


@mcp.tool
def chain_dream(
    chain_id: str,
    prompt: str,
    model: str = "qwen3:4b",
    store_as: str = "dream"
) -> dict:
    """Execute local model task and store result in chain context.

    Use this to have a local model "dream" on a topic while you're busy
    with other work. The result will be available in the chain's context
    for the next cycle.

    Args:
        chain_id: Chain to associate the dream with
        prompt: Prompt for the local model
        model: Which model to use (default: qwen3:4b)
        store_as: Key name to store result under (default: "dream")
    """
    config = load_config()

    if chain_id not in config.get("chains", {}):
        return {"error": f"Chain '{chain_id}' not found"}

    # Call local model synchronously (blocking)
    result = call_local_model(prompt, model)

    # Store in context
    ctx = load_context()
    ctx.setdefault(chain_id, {})[store_as] = result
    ctx[chain_id][f"{store_as}_timestamp"] = datetime.now().isoformat()
    save_context(ctx)

    return {
        "status": "completed",
        "chain": chain_id,
        "stored_as": store_as,
        "result_preview": result[:200] + "..." if len(result) > 200 else result,
        "model": model
    }


@mcp.tool
def chain_pass(chain_id: str, context: str, merge: bool = True) -> dict:
    """Pass context/seed data to the next cycle of a chain.

    The context will be injected at the start of the next chain cycle.

    Args:
        chain_id: The chain to pass context to
        context: JSON string with context data
        merge: If True, merge with existing context. If False, replace.
    """
    config = load_config()

    if chain_id not in config.get("chains", {}):
        return {"error": f"Chain '{chain_id}' not found"}

    try:
        new_ctx = json.loads(context)
    except json.JSONDecodeError:
        new_ctx = {"raw": context}

    ctx = load_context()

    if merge and chain_id in ctx:
        ctx[chain_id].update(new_ctx)
    else:
        ctx[chain_id] = new_ctx

    ctx.setdefault(chain_id, {})["_passed_at"] = datetime.now().isoformat()
    save_context(ctx)

    return {
        "status": "passed",
        "chain": chain_id,
        "context_keys": list(ctx[chain_id].keys()),
        "merged": merge
    }


@mcp.tool
def chain_context(chain_id: str) -> dict:
    """Get the current context/seed data for a chain.

    Args:
        chain_id: The chain to get context for
    """
    ctx = load_context()

    if chain_id not in ctx:
        return {"chain": chain_id, "context": None, "message": "No context stored"}

    return {
        "chain": chain_id,
        "context": ctx[chain_id]
    }


@mcp.tool
def chain_auto_continue(
    chain_id: str,
    enabled: bool = True,
    delay_minutes: float = 5.0,
    dream_prompt: str = ""
) -> dict:
    """Enable or disable auto-continue mode for a chain.

    When enabled, completing the final phase will automatically schedule
    the chain to run again after the specified delay. The execution pointer
    ping-pongs between you and the local model.

    Args:
        chain_id: The chain to configure
        enabled: True to enable auto-continue, False to disable
        delay_minutes: Minutes between cycles (default: 5)
        dream_prompt: Optional prompt for local model during wait
    """
    config = load_config()

    if chain_id not in config.get("chains", {}):
        return {"error": f"Chain '{chain_id}' not found"}

    schedule = load_schedule()
    schedule.setdefault("auto_continue", {})

    if enabled:
        schedule["auto_continue"][chain_id] = {
            "delay_minutes": delay_minutes,
            "dream_prompt": dream_prompt,
            "enabled_at": datetime.now().isoformat()
        }
    else:
        schedule["auto_continue"].pop(chain_id, None)

    save_schedule(schedule)

    return {
        "status": "enabled" if enabled else "disabled",
        "chain": chain_id,
        "delay_minutes": delay_minutes if enabled else None,
        "has_dream": bool(dream_prompt) if enabled else False
    }


@mcp.tool
def chain_scheduled() -> dict:
    """List all scheduled and auto-continue chains."""
    schedule = load_schedule()
    now = datetime.now()

    scheduled = []
    for chain_id, info in schedule.get("scheduled", {}).items():
        trigger_at = datetime.fromisoformat(info["trigger_at"])
        remaining = (trigger_at - now).total_seconds() / 60
        scheduled.append({
            "chain": chain_id,
            "trigger_at": info["trigger_at"],
            "remaining_minutes": max(0, round(remaining, 1)),
            "has_dream": bool(info.get("dream_prompt"))
        })

    auto_continue = []
    for chain_id, info in schedule.get("auto_continue", {}).items():
        auto_continue.append({
            "chain": chain_id,
            "delay_minutes": info["delay_minutes"],
            "has_dream": bool(info.get("dream_prompt"))
        })

    return {
        "scheduled": scheduled,
        "auto_continue": auto_continue
    }


@mcp.tool
def chain_cancel(chain_id: str) -> dict:
    """Cancel a scheduled chain execution.

    Args:
        chain_id: The chain to cancel
    """
    schedule = load_schedule()

    if chain_id not in schedule.get("scheduled", {}):
        return {"error": f"Chain '{chain_id}' not scheduled"}

    del schedule["scheduled"][chain_id]
    save_schedule(schedule)

    # Thread will check and exit when it wakes up
    return {"status": "cancelled", "chain": chain_id}


@mcp.tool
def chain_export(chain_id: str, standalone: bool = False) -> dict:
    """Export a chain as a standalone MCP server.

    Creates a new MCP server directory with the chain baked in,
    initializes git, and registers with mcp-manager.

    Args:
        chain_id: The chain to export (e.g., "character-cycle")
        standalone: If True, copies tmux logic into exported server.
                   If False (default), depends on chained-prompts library.
    """
    config = load_config()

    if chain_id not in config.get("chains", {}):
        return {"error": f"Chain '{chain_id}' not found", "available": list(config.get("chains", {}).keys())}

    chain = config["chains"][chain_id]
    phases = chain.get("phases", [])
    prompts = chain.get("prompts", {})
    name = chain.get("name", chain_id)
    description = chain.get("description", "")

    # Target directory
    mcp_dir = Path(f"/home/yaniv/agent-flow/mcp-servers/{chain_id}")
    mcp_dir.mkdir(parents=True, exist_ok=True)

    # Generate server.py
    if standalone:
        server_code = _generate_standalone_server(chain_id, name, description, phases, prompts)
    else:
        server_code = _generate_dependent_server(chain_id, name, description, phases, prompts)

    # Write files
    (mcp_dir / "server.py").write_text(server_code)
    os.chmod(mcp_dir / "server.py", 0o664)

    # run_server.py wrapper
    run_server = '''#!/usr/bin/env python3
"""Clean wrapper for MCP server."""
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent))
from server import mcp
mcp.run(transport="stdio")
'''
    (mcp_dir / "run_server.py").write_text(run_server)
    os.chmod(mcp_dir / "run_server.py", 0o775)

    # requirements.txt
    (mcp_dir / "requirements.txt").write_text("fastmcp>=0.1.0\n")
    os.chmod(mcp_dir / "requirements.txt", 0o664)

    # .gitignore
    gitignore = """.venv/
__pycache__/
*.pyc
*.egg-info/
"""
    (mcp_dir / ".gitignore").write_text(gitignore)
    os.chmod(mcp_dir / ".gitignore", 0o664)

    # Git init or stage changes
    git_result = _setup_git(mcp_dir, chain_id, name)

    # Create venv if doesn't exist
    venv_path = mcp_dir / ".venv"
    if not venv_path.exists():
        subprocess.run(
            ["python3", "-m", "venv", str(venv_path)],
            cwd=mcp_dir,
            capture_output=True
        )
        subprocess.run(
            [str(venv_path / "bin" / "pip"), "install", "-q", "fastmcp"],
            cwd=mcp_dir,
            capture_output=True
        )

    # Register with mcp-manager
    reg_result = _register_with_mcp_manager(chain_id, mcp_dir)

    return {
        "status": "exported",
        "chain_id": chain_id,
        "path": str(mcp_dir),
        "standalone": standalone,
        "tools": ["start", "complete", "status", "reset"],
        "git": git_result,
        "mcp_manager": reg_result,
        "next": f"Restart Claude Code to load the new '{chain_id}' MCP"
    }


def _generate_standalone_server(chain_id: str, name: str, description: str, phases: list, prompts: dict) -> str:
    """Generate a fully self-contained server with tmux logic."""
    phases_str = json.dumps(phases)
    prompts_str = json.dumps(prompts, indent=4)

    return f'''#!/usr/bin/env python3
"""{name} MCP - {description}

Exported from chained-prompts. Standalone server with tmux injection.

Tools:
- start: Begin the chain
- complete: Mark phase done, auto-advance
- status: See progress
- reset: Start fresh
"""
import os
import json
import subprocess
import tempfile
import time
from pathlib import Path
from fastmcp import FastMCP

mcp = FastMCP("{chain_id}")

# Chain definition (baked in)
CHAIN_ID = "{chain_id}"
CHAIN_NAME = "{name}"
PHASES = {phases_str}
PROMPTS = {prompts_str}

# State file
STATE_FILE = Path("/tmp/{chain_id}-state.json")

# tmux configuration
TMUX_USER = os.environ.get("TMUX_USER", "yaniv")
TMUX_TARGET_DEFAULT = "0:zara"


def _find_tmux_target() -> str:
    try:
        result = subprocess.run(
            ["sudo", "-u", TMUX_USER, "tmux", "list-windows", "-a", "-F",
             "#{{session_name}}:#{{window_name}}"],
            capture_output=True, text=True, timeout=5,
            stdin=subprocess.DEVNULL, start_new_session=True
        )
        if result.returncode == 0:
            for line in result.stdout.strip().split("\\n"):
                if line.endswith(":zara"):
                    return line
    except Exception:
        pass
    return os.environ.get("TMUX_TARGET", TMUX_TARGET_DEFAULT)


def load_state() -> dict:
    if STATE_FILE.exists():
        return json.loads(STATE_FILE.read_text())
    return {{phase: {{"triggered": False, "completed": False}} for phase in PHASES}}


def save_state(state: dict):
    STATE_FILE.write_text(json.dumps(state, indent=2))


def send_to_tmux(text: str) -> bool:
    tmux_target = _find_tmux_target()
    try:
        with tempfile.NamedTemporaryFile(mode='w', suffix='.txt', delete=False) as f:
            f.write(text)
            temp_path = f.name
        os.chmod(temp_path, 0o644)

        subprocess.run(
            ["sudo", "-u", TMUX_USER, "tmux", "load-buffer", temp_path],
            check=True, timeout=10,
            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            start_new_session=True
        )
        subprocess.run(
            ["sudo", "-u", TMUX_USER, "tmux", "paste-buffer", "-t", tmux_target],
            check=True, timeout=10,
            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            start_new_session=True
        )
        time.sleep(0.3)
        subprocess.run(
            ["sudo", "-u", TMUX_USER, "tmux", "send-keys", "-t", tmux_target, "Enter"],
            check=True, timeout=10,
            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            start_new_session=True
        )
        os.unlink(temp_path)
        return True
    except Exception:
        return False


@mcp.tool
def start() -> dict:
    """Begin the {name} chain from the first phase."""
    first_phase = PHASES[0]
    prompt = PROMPTS.get(first_phase, f"Phase: {{first_phase}}")

    state = {{phase: {{"triggered": False, "completed": False}} for phase in PHASES}}
    state[first_phase]["triggered"] = True
    save_state(state)

    success = send_to_tmux(prompt)

    if success:
        return {{
            "status": "started",
            "phase": first_phase,
            "next_phase": PHASES[1] if len(PHASES) > 1 else None,
            "instruction": f"Complete the phase, then call complete(\\"{{first_phase}}\\")"
        }}
    return {{"error": "Failed to send to tmux"}}


@mcp.tool
def complete(phase: str, auto_next: bool = True) -> dict:
    """Mark a phase complete. Auto-triggers next phase by default.

    Args:
        phase: The phase to complete
        auto_next: If True (default), automatically trigger the next phase
    """
    if phase not in PHASES:
        return {{"error": f"Phase '{{phase}}' not found", "available": PHASES}}

    state = load_state()
    state[phase]["completed"] = True
    save_state(state)

    phase_idx = PHASES.index(phase)
    next_phase = PHASES[phase_idx + 1] if phase_idx + 1 < len(PHASES) else None

    result = {{"status": "completed", "phase": phase, "next_phase": next_phase}}

    if next_phase and auto_next:
        prompt = PROMPTS.get(next_phase, f"Phase: {{next_phase}}")
        state[next_phase]["triggered"] = True
        save_state(state)
        result["auto_triggered"] = next_phase
        result["next_prompt"] = prompt
        result["instruction"] = f"Execute the prompt above, then call complete(\\"{{next_phase}}\\")"
    elif not next_phase:
        result["chain_complete"] = True
        result["message"] = f"🎉 {{CHAIN_NAME}} complete!"
        save_state({{phase: {{"triggered": False, "completed": False}} for phase in PHASES}})

    return result


@mcp.tool
def status() -> dict:
    """See progress of the chain."""
    state = load_state()
    completed = sum(1 for p in PHASES if state.get(p, {{}}).get("completed", False))

    current = None
    next_phase = None
    for i, phase in enumerate(PHASES):
        ps = state.get(phase, {{}})
        if ps.get("triggered") and not ps.get("completed"):
            current = phase
            next_phase = PHASES[i + 1] if i + 1 < len(PHASES) else None
            break

    return {{
        "chain": CHAIN_NAME,
        "phases": state,
        "phase_order": PHASES,
        "current_phase": current,
        "next_phase": next_phase,
        "progress": f"{{completed}}/{{len(PHASES)}}",
        "complete": completed == len(PHASES)
    }}


@mcp.tool
def reset() -> dict:
    """Reset the chain to start fresh."""
    save_state({{phase: {{"triggered": False, "completed": False}} for phase in PHASES}})
    return {{"status": "reset", "chain": CHAIN_NAME}}


if __name__ == "__main__":
    mcp.run(transport="stdio")
'''


def _generate_dependent_server(chain_id: str, name: str, description: str, phases: list, prompts: dict) -> str:
    """Generate a server that depends on chained-prompts."""
    phases_str = json.dumps(phases)
    prompts_str = json.dumps(prompts, indent=4)

    return f'''#!/usr/bin/env python3
"""{name} MCP - {description}

Exported from chained-prompts. Depends on chained-prompts for execution.

Tools:
- start: Begin the chain
- complete: Mark phase done, auto-advance
- status: See progress
- reset: Start fresh
"""
import sys
import importlib.util

# Import chained-prompts server explicitly to avoid circular import
# (local server.py shadows the module name)
spec = importlib.util.spec_from_file_location(
    "chained_prompts_server",
    "/home/yaniv/agent-flow/mcp-servers/chained-prompts/server.py"
)
chained_prompts = importlib.util.module_from_spec(spec)
spec.loader.exec_module(chained_prompts)

# Extract underlying functions from FunctionTool wrappers
chain_start = chained_prompts.chain_start.fn
chain_complete = chained_prompts.chain_complete.fn
chain_status = chained_prompts.chain_status.fn
chain_reset = chained_prompts.chain_reset.fn

from fastmcp import FastMCP

mcp = FastMCP("{chain_id}")

CHAIN_ID = "{chain_id}"


@mcp.tool
def start() -> dict:
    """Begin the {name} chain from the first phase."""
    return chain_start(CHAIN_ID)


@mcp.tool
def complete(phase: str, auto_next: bool = True) -> dict:
    """Mark a phase complete. Auto-triggers next phase by default.

    Args:
        phase: The phase to complete
        auto_next: If True (default), automatically trigger the next phase
    """
    return chain_complete(CHAIN_ID, phase, auto_next)


@mcp.tool
def status() -> dict:
    """See progress of the chain."""
    return chain_status(CHAIN_ID)


@mcp.tool
def reset() -> dict:
    """Reset the chain to start fresh."""
    return chain_reset(CHAIN_ID)


if __name__ == "__main__":
    mcp.run(transport="stdio")
'''


def _setup_git(mcp_dir: Path, chain_id: str, name: str) -> dict:
    """Initialize or update git repo."""
    git_dir = mcp_dir / ".git"

    try:
        if not git_dir.exists():
            # Initialize new repo
            subprocess.run(["git", "init"], cwd=mcp_dir, capture_output=True, check=True)
            subprocess.run(["git", "add", "."], cwd=mcp_dir, capture_output=True, check=True)
            subprocess.run(
                ["git", "commit", "-m", f"Initial export of {name} chain"],
                cwd=mcp_dir, capture_output=True, check=True
            )
            return {"action": "initialized", "commit": "initial"}
        else:
            # Stage and commit changes
            subprocess.run(["git", "add", "."], cwd=mcp_dir, capture_output=True, check=True)
            result = subprocess.run(
                ["git", "diff", "--cached", "--quiet"],
                cwd=mcp_dir, capture_output=True
            )
            if result.returncode != 0:  # There are staged changes
                subprocess.run(
                    ["git", "commit", "-m", f"Update {name} chain export"],
                    cwd=mcp_dir, capture_output=True, check=True
                )
                return {"action": "updated", "commit": "update"}
            return {"action": "no_changes"}
    except subprocess.CalledProcessError as e:
        return {"action": "error", "error": str(e)}


def _register_with_mcp_manager(chain_id: str, mcp_dir: Path) -> dict:
    """Register the new MCP with mcp-manager and auto-load it."""
    manager_dir = Path("/home/yaniv/agent-flow/mcp-servers/mcp-manager")
    full_config_path = manager_dir / "full-config.json"
    project_mcp_path = Path("/home/yaniv/seethegalaxy/team-members/zara-chen/.mcp.json")

    try:
        # Load full config
        full_config = json.loads(full_config_path.read_text()) if full_config_path.exists() else {"mcpServers": {}}

        # Add to available servers
        full_config["mcpServers"][chain_id] = {
            "command": str(mcp_dir / ".venv" / "bin" / "python"),
            "args": [str(mcp_dir / "run_server.py")],
            "env": {
                "TMUX_USER": "yaniv"
            }
        }
        full_config_path.write_text(json.dumps(full_config, indent=2))
        os.chmod(full_config_path, 0o664)

        # Auto-load: add to project .mcp.json
        project_config = json.loads(project_mcp_path.read_text()) if project_mcp_path.exists() else {"mcpServers": {}}
        project_config["mcpServers"][chain_id] = full_config["mcpServers"][chain_id]
        project_mcp_path.write_text(json.dumps(project_config, indent=2))
        os.chmod(project_mcp_path, 0o664)

        # Update TOKEN_ESTIMATES in mcp-manager server.py
        _update_token_estimates(chain_id)

        return {"registered": True, "auto_loaded": True}
    except Exception as e:
        return {"registered": False, "error": str(e)}


def _update_token_estimates(chain_id: str):
    """Add token estimate to mcp-manager's TOKEN_ESTIMATES."""
    manager_server = Path("/home/yaniv/agent-flow/mcp-servers/mcp-manager/server.py")
    if not manager_server.exists():
        return

    content = manager_server.read_text()

    # Check if already exists
    if f'"{chain_id}"' in content:
        return

    # Find TOKEN_ESTIMATES dict and add entry
    # Look for the closing brace of TOKEN_ESTIMATES
    import re
    pattern = r'(TOKEN_ESTIMATES = \{[^}]+)"chained-prompts": \d+,'
    match = re.search(pattern, content)
    if match:
        # Add new entry after chained-prompts
        new_entry = f'"{chain_id}": 1200,\n    "chained-prompts"'
        content = content.replace('"chained-prompts"', new_entry)
        manager_server.write_text(content)
        os.chmod(manager_server, 0o664)


if __name__ == "__main__":
    mcp.run(transport="stdio")
