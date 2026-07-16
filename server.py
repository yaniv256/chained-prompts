#!/usr/bin/env python3
"""Chained Prompts MCP — run a phased protocol to its end, one phase at a time.

A *chain* is an ordered sequence of *phases*. Each phase has a prompt. This
server serves the phases one at a time and will not let the agent skip ahead:
`chain_start` returns the first phase's prompt as its tool RESULT; the agent does
that phase, then calls `chain_complete`, which returns the next phase's prompt —
and so on to the final phase. Anti-skip is enforced (you can only complete the
current phase). That is the whole product: a skill or methodology that MUST run
in order, with no phase skipped under momentum.

**Delivery is first-party only.** This server never reaches into the agent's
session to push a prompt — no terminal injection, no out-of-band write. Prompts
are returned as tool results and the agent reads them. Waking a *dormant* agent
is the agent's own job, via its harness scheduler (e.g. ScheduleWakeup /
CronCreate / /loop). This server's role there is to make the current phase and
its anti-idle reminder *self-service* (`chain_reminder`), so a self-armed wake
has concrete text to fire and the agent can never drift idle mid-chain.

Phase text lives in the LIVE skill file (anchor mode): a chain stores
`skill_path` + `anchors` (phase -> line range) and each phase is resolved from
the skill at call time, so the skill stays the single source of truth. Legacy
literal `prompts` are still accepted.

Tools:
- chain_list      — list all defined chains
- chain_start     — begin a chain; returns the first phase's prompt
- chain_status    — progress of a chain (or all active chains)
- chain_complete  — mark the current phase done; returns the next phase's prompt
- chain_reminder  — the current phase + prompt + anti-idle message (for self-wake)
- chain_reset     — reset a chain's run state
- chain_define    — create or update a chain
- chain_delete    — remove a chain
- chain_get       — full chain definition
- phase_edit      — edit one phase's literal prompt
- chain_pass      — seed context for the next run of a chain
- chain_context   — read a chain's stored context
"""
import os
import sys
import json
from pathlib import Path
from datetime import datetime

try:
    from fastmcp import FastMCP
except ImportError:  # allow importing tools for unit tests without fastmcp
    class FastMCP:  # minimal stub: .tool is a no-op decorator
        def __init__(self, *a, **k):
            pass

        def tool(self, fn):
            return fn

        def run(self, *a, **k):
            raise RuntimeError("fastmcp is not installed; cannot run the server")

# Anchor-slice resolver (lives beside this file). It reads a phase's text from a
# LIVE skill file so the skill stays the single source of truth (no copies).
sys.path.insert(0, str(Path(__file__).resolve().parent))
from anchor_slice import resolve_anchor_slice, AnchorError

mcp = FastMCP("chained-prompts")

# Storage lives under ~/.chained-prompts (override with CHAINED_PROMPTS_DIR).
BASE_DIR = Path(os.environ.get("CHAINED_PROMPTS_DIR", "~/.chained-prompts")).expanduser()
CONFIG_FILE = BASE_DIR / "chains.json"       # chain definitions
STATE_FILE = BASE_DIR / "state.json"         # per-run phase progress
CONTEXT_FILE = BASE_DIR / "context.json"     # optional context passed between runs


# --------------------------------------------------------------------------- #
# Persistence — small JSON files under BASE_DIR
# --------------------------------------------------------------------------- #
def _load_json(path: Path, default):
    return json.loads(path.read_text()) if path.exists() else default


def _save_json(path: Path, data):
    BASE_DIR.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2))


def load_config() -> dict:
    return _load_json(CONFIG_FILE, {"chains": {}})


def save_config(config: dict):
    _save_json(CONFIG_FILE, config)


def load_state() -> dict:
    return _load_json(STATE_FILE, {})


def save_state(state: dict):
    _save_json(STATE_FILE, state)


def load_context() -> dict:
    return _load_json(CONTEXT_FILE, {})


def save_context(context: dict):
    _save_json(CONTEXT_FILE, context)


# --------------------------------------------------------------------------- #
# Phase resolution + state helpers
# --------------------------------------------------------------------------- #
def _fresh_state(phases: list) -> dict:
    return {phase: {"triggered": False, "completed": False} for phase in phases}


def _skill_path_for(chain: dict) -> str:
    """Resolve the chain's skill_path (relative paths resolve against BASE_DIR)."""
    raw = chain.get("skill_path", "")
    p = Path(raw).expanduser()
    if not p.is_absolute():
        p = (BASE_DIR / p)
    return str(p)


def resolve_phase_prompt(chain: dict, phase: str) -> str:
    """Return a phase's prompt.

    Anchor mode (preferred): the chain has `skill_path` and an `anchors` map of
    phase -> {"start": <line>, "end": <line-or-null>}; we resolve the slice from
    the LIVE skill file. Legacy mode: fall back to a literal `prompts[phase]`.
    Raises AnchorError (fail loud) if anchor mode is configured but unresolvable.
    """
    anchors = chain.get("anchors")
    if anchors and phase in anchors:
        a = anchors[phase]
        return resolve_anchor_slice(
            _skill_path_for(chain), a.get("start"), a.get("end")
        )
    legacy = chain.get("prompts", {})
    if phase in legacy:
        return legacy[phase]
    raise AnchorError(
        f"no prompt for phase {phase!r}: chain has neither an anchor entry "
        f"nor a legacy prompts[] entry for it"
    )


def _next_uncompleted_phase(phases: list, chain_state: dict):
    """First phase (in defined order) not yet completed, or None if all done."""
    for phase in phases:
        if not chain_state.get(phase, {}).get("completed", False):
            return phase
    return None


def _current_phase(phases: list, chain_state: dict):
    """The triggered-but-not-completed phase (what the agent is on now), or None."""
    for phase in phases:
        ps = chain_state.get(phase, {})
        if ps.get("triggered") and not ps.get("completed"):
            return phase
    return None


def _load_chain(chain_id: str):
    """Return (chain_def, None) if the chain exists, else (None, error_dict).

    Every tool that operates on an existing chain starts with this, so a missing
    chain always reports the same shape — including the `available` list.
    """
    config = load_config()
    chains = config.get("chains", {})
    if chain_id not in chains:
        return None, {"error": f"Chain '{chain_id}' not found", "available": list(chains.keys())}
    return chains[chain_id], None


# --------------------------------------------------------------------------- #
# Anti-idle self-wake reminder
# --------------------------------------------------------------------------- #
# Default cadence (minutes) for the self-armed anti-idle wake. The AGENT arms a
# first-party ScheduleWakeup / CronCreate at this cadence; each fire calls
# chain_reminder to re-focus its attention. This server never fires the wake.
REMINDER_DELAY_MINUTES_DEFAULT = 5.0

# The standing anti-idle message, attached to every reminder and to the start /
# advance results. A chain, once started, is run to its final phase; stopping to
# ask the user is an attractor, not a valid exit.
ANTI_IDLE_MESSAGE = (
    "You are in the middle of a chain. Do NOT go idle, end your turn, or stop to "
    "ask the user until this chain reaches its final phase. Do the current phase, "
    "then call chain_complete to advance. Asking the user a question and blocking "
    "on it is a behavior-space attractor: it feels like a valid exit but it is not "
    "one. This chain exists to support autonomous operation, and it is the will of "
    "the user that you continue all the way to the end. If you believe you are "
    "blocked, that belief is itself the attractor — run the cheapest disproof and "
    "continue on your best judgement rather than stopping."
)


def _arm_instruction(chain_id: str) -> str:
    """Tell the agent to arm a first-party self-wake for anti-idle protection."""
    return (
        f"Arm a first-party self-wake (ScheduleWakeup / CronCreate) about every "
        f"{REMINDER_DELAY_MINUTES_DEFAULT:g} min while this chain runs; on each fire "
        f'call chain_reminder("{chain_id}") to refocus. Cancel it when the chain '
        f"completes."
    )


def _reminder_payload(chain_id: str, chain: dict, phase: str, prompt: str,
                      completed: int, total: int) -> dict:
    """The self-service anti-idle reminder for the current phase (pure data)."""
    return {
        "chain": chain_id,
        "chain_name": chain.get("name", chain_id),
        "current_phase": phase,
        "progress": f"{completed}/{total}",
        "phase_prompt": prompt,
        "reminder": ANTI_IDLE_MESSAGE,
        "next_action": f'Do this phase, then call chain_complete("{chain_id}", "{phase}").',
    }


# --------------------------------------------------------------------------- #
# Tools
# --------------------------------------------------------------------------- #
@mcp.tool
def chain_list() -> dict:
    """List all defined chains with their progress."""
    config = load_config()
    state = load_state()

    chains = []
    for chain_id, chain in config.get("chains", {}).items():
        phases = chain.get("phases", [])
        chain_state = state.get(chain_id, {})
        completed = sum(1 for p in phases if chain_state.get(p, {}).get("completed", False))
        current = _current_phase(phases, chain_state)
        chains.append({
            "id": chain_id,
            "name": chain.get("name", chain_id),
            "description": chain.get("description", ""),
            "phases": phases,
            "progress": f"{completed}/{len(phases)}",
            "current_phase": current,
            "active": current is not None,
        })

    return {"chains": chains, "total": len(chains)}


@mcp.tool
def chain_start(chain_id: str) -> dict:
    """Begin a chain from its first phase; returns that phase's prompt.

    The prompt comes back as this tool's RESULT — do the phase, then call
    chain_complete to get the next one. To stay protected against going idle
    mid-chain, arm a first-party self-wake per `arm_wake` (see below).

    Args:
        chain_id: The chain to start (e.g., "incident-investigation").
    """
    chain, err = _load_chain(chain_id)
    if err:
        return err

    phases = chain.get("phases", [])
    if not phases:
        return {"error": "Chain has no phases defined"}

    first_phase = phases[0]
    try:
        prompt = resolve_phase_prompt(chain, first_phase)
    except AnchorError as e:
        return {"error": f"anchor_resolution_failed: {e}", "chain": chain_id, "phase": first_phase}

    state = load_state()
    state[chain_id] = _fresh_state(phases)
    state[chain_id][first_phase]["triggered"] = True
    save_state(state)

    return {
        "status": "started",
        "chain": chain_id,
        "phase": first_phase,
        "phase_index": 1,
        "total": len(phases),
        "prompt": prompt,
        "next_action": f'Do this phase, then call chain_complete("{chain_id}", "{first_phase}").',
        "arm_wake": _arm_instruction(chain_id),
        "reminder": ANTI_IDLE_MESSAGE,
    }


@mcp.tool
def chain_status(chain_id: str = "") -> dict:
    """Progress of a specific chain, or all active chains when chain_id is empty.

    Args:
        chain_id: Optional — a specific chain (empty = list all active).
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
        current = _current_phase(phases, chain_state)
        idx = phases.index(current) if current in phases else -1
        next_phase = phases[idx + 1] if 0 <= idx < len(phases) - 1 else None

        return {
            "chain": chain_id,
            "name": chain.get("name"),
            "phases": chain_state,
            "phase_order": phases,
            "current_phase": current,
            "next_phase": next_phase,
            "progress": f"{completed}/{len(phases)}",
            "complete": completed == len(phases),
        }

    active = []
    for cid, chain in config.get("chains", {}).items():
        current = _current_phase(chain.get("phases", []), state.get(cid, {}))
        if current is not None:
            active.append({"chain": cid, "current_phase": current})
    return {"active_chains": active, "total_active": len(active)}


@mcp.tool
def chain_complete(chain_id: str, phase: str, auto_next: bool = True) -> dict:
    """Mark the current phase complete; returns the next phase's prompt.

    Anti-skip: you may only complete the *current* phase (the first not-yet-
    completed phase in defined order). Completing a later phase to jump ahead is
    rejected; re-completing the current phase is idempotent.

    Args:
        chain_id: The chain.
        phase: The phase to complete (must be the current phase).
        auto_next: If True (default), also return the next phase's prompt.
    """
    chain, err = _load_chain(chain_id)
    if err:
        return err

    phases = chain.get("phases", [])
    if phase not in phases:
        return {"error": f"Phase '{phase}' not in chain", "available": phases}

    state = load_state()
    if chain_id not in state:
        state[chain_id] = _fresh_state(phases)

    expected = _next_uncompleted_phase(phases, state[chain_id])
    if expected is None:
        return {"status": "already_complete", "chain": chain_id,
                "message": f"{chain.get('name', chain_id)} already complete."}
    if phase != expected:
        return {
            "error": "out_of_order",
            "chain": chain_id,
            "expected_phase": expected,
            "got_phase": phase,
            "message": f"Cannot complete '{phase}' yet — the next phase to complete is "
                       f"'{expected}'. Phases run in order; you cannot skip ahead.",
        }

    state[chain_id][phase]["completed"] = True
    next_phase = _next_uncompleted_phase(phases, state[chain_id])
    result = {
        "status": "completed",
        "chain": chain_id,
        "phase": phase,
        "next_phase": next_phase,
    }

    if next_phase and auto_next:
        try:
            prompt = resolve_phase_prompt(chain, next_phase)
        except AnchorError as e:
            return {"error": f"anchor_resolution_failed: {e}", "chain": chain_id, "phase": next_phase}

        state[chain_id][next_phase]["triggered"] = True
        result["auto_triggered"] = next_phase
        result["next_prompt"] = prompt
        result["instruction"] = f'Do the phase above, then call chain_complete("{chain_id}", "{next_phase}").'
        result["reminder"] = ANTI_IDLE_MESSAGE
    elif not next_phase:
        result["chain_complete"] = True
        result["message"] = f"🎉 {chain.get('name', chain_id)} complete!"
        result["cancel_wake"] = "The chain is done — cancel the anti-idle self-wake you armed at start."
        # Reset for the next run.
        state[chain_id] = _fresh_state(phases)

    save_state(state)
    return result


@mcp.tool
def chain_reminder(chain_id: str) -> dict:
    """The current phase's anti-idle reminder — for a self-armed wake to fire.

    Call this from a first-party ScheduleWakeup / CronCreate you armed at
    chain_start. It returns which phase you are on, that phase's prompt, and the
    do-not-go-idle message, so a wake re-focuses you on finishing the chain.
    Returns `inactive` when the chain is not currently running (cancel the wake).

    Args:
        chain_id: The active chain to be reminded about.
    """
    chain, err = _load_chain(chain_id)
    if err:
        return err

    phases = chain.get("phases", [])
    chain_state = load_state().get(chain_id, {})
    current = _current_phase(phases, chain_state)

    if current is None:
        return {
            "chain": chain_id,
            "active": False,
            "message": "This chain is not currently active — cancel the anti-idle self-wake.",
        }

    try:
        prompt = resolve_phase_prompt(chain, current)
    except AnchorError as e:
        prompt = f"[anchor_resolution_failed: {e}]"

    completed = sum(1 for p in phases if chain_state.get(p, {}).get("completed", False))
    payload = _reminder_payload(chain_id, chain, current, prompt, completed, len(phases))
    payload["active"] = True
    return payload


@mcp.tool
def chain_reset(chain_id: str) -> dict:
    """Reset a chain's run state to start fresh.

    Args:
        chain_id: The chain to reset.
    """
    chain, err = _load_chain(chain_id)
    if err:
        return err

    state = load_state()
    state[chain_id] = _fresh_state(chain.get("phases", []))
    save_state(state)
    return {"status": "reset", "chain": chain_id}


@mcp.tool
def chain_define(
    chain_id: str,
    name: str,
    phases: list,
    prompts: dict = None,
    description: str = "",
    skill_path: str = "",
    anchors: dict = None,
) -> dict:
    """Create or update a chain definition.

    Two ways to supply phase content:
    - ANCHOR MODE (preferred): pass `skill_path` (a phased skill file) and
      `anchors` — a dict mapping each phase name to {"start": <verbatim line>,
      "end": <verbatim line or null for EOF>}. Phase prompts are resolved from
      the LIVE skill at run time, so the skill stays the single source of truth
      and nothing needs syncing. Anchors are usually the phase headings.
    - LEGACY MODE: pass `prompts` — a dict mapping phase names to literal text.

    Args:
        chain_id: Unique identifier (e.g., "incident-investigation").
        name: Display name.
        phases: Ordered list of phase names (defines run order).
        prompts: Legacy — dict of phase -> literal prompt text.
        description: Brief description of the chain's purpose.
        skill_path: Anchor mode — path to the phased skill file.
        anchors: Anchor mode — dict of phase -> {"start": line, "end": line|null}.
    """
    prompts = prompts or {}
    anchors = anchors or {}
    config = load_config()

    if anchors:
        if not skill_path:
            return {"error": "anchor mode requires skill_path"}
        missing = [p for p in phases if p not in anchors]
        if missing:
            return {"error": f"Missing anchors for phases: {missing}"}
        for p, a in anchors.items():
            if not isinstance(a, dict) or "start" not in a:
                return {"error": f"anchor for phase '{p}' needs at least a 'start' line"}
        # Validate every phase boundary before persisting the definition. A bad
        # late-phase anchor otherwise remains latent until the chain reaches it,
        # potentially after hours of completed work.
        for p in phases:
            a = anchors[p]
            try:
                resolve_anchor_slice(skill_path, a["start"], a.get("end"))
            except AnchorError as exc:
                return {
                    "error": "anchor_validation_failed",
                    "phase": p,
                    "message": str(exc),
                }
        entry = {
            "name": name,
            "description": description,
            "phases": phases,
            "skill_path": skill_path,
            "anchors": anchors,
        }
    else:
        missing = [p for p in phases if p not in prompts]
        if missing:
            return {"error": f"Missing prompts for phases: {missing} "
                             f"(or supply skill_path + anchors for anchor mode)"}
        entry = {
            "name": name,
            "description": description,
            "phases": phases,
            "prompts": prompts,
        }

    config.setdefault("chains", {})[chain_id] = entry
    save_config(config)

    return {
        "status": "defined",
        "chain_id": chain_id,
        "phases": phases,
        "phase_count": len(phases),
        "mode": "anchor" if anchors else "legacy",
    }


@mcp.tool
def chain_delete(chain_id: str) -> dict:
    """Remove a chain definition.

    Args:
        chain_id: The chain to delete.
    """
    config = load_config()
    if chain_id not in config.get("chains", {}):
        return {"error": f"Chain '{chain_id}' not found"}

    del config["chains"][chain_id]
    save_config(config)

    state = load_state()
    if chain_id in state:
        del state[chain_id]
        save_state(state)

    return {"status": "deleted", "chain": chain_id}


@mcp.tool
def chain_get(chain_id: str) -> dict:
    """Get a chain's full definition.

    Args:
        chain_id: The chain to fetch.
    """
    chain, err = _load_chain(chain_id)
    if err:
        return err
    return {"chain_id": chain_id, **chain}


@mcp.tool
def phase_edit(chain_id: str, phase: str, prompt: str) -> dict:
    """Edit one phase's literal prompt (legacy-mode chains).

    Args:
        chain_id: The chain containing the phase.
        phase: The phase to edit.
        prompt: New prompt text.
    """
    config = load_config()
    if chain_id not in config.get("chains", {}):
        return {"error": f"Chain '{chain_id}' not found"}

    chain = config["chains"][chain_id]
    if phase not in chain.get("phases", []):
        return {"error": f"Phase '{phase}' not in chain", "available": chain.get("phases", [])}

    chain.setdefault("prompts", {})[phase] = prompt
    save_config(config)

    return {
        "status": "updated",
        "chain": chain_id,
        "phase": phase,
        "prompt_preview": prompt[:100] + "..." if len(prompt) > 100 else prompt,
    }


@mcp.tool
def chain_pass(chain_id: str, context: str, merge: bool = True) -> dict:
    """Seed context for the next run of a chain.

    Args:
        chain_id: The chain to pass context to.
        context: JSON string with context data (stored as {"raw": ...} if not JSON).
        merge: If True, merge with existing context; if False, replace.
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
    ctx[chain_id]["_passed_at"] = datetime.now().isoformat()
    save_context(ctx)

    return {
        "status": "passed",
        "chain": chain_id,
        "context_keys": list(ctx[chain_id].keys()),
        "merged": merge,
    }


@mcp.tool
def chain_context(chain_id: str) -> dict:
    """Read a chain's stored context.

    Args:
        chain_id: The chain to read context for.
    """
    ctx = load_context()
    if chain_id not in ctx:
        return {"chain": chain_id, "context": None, "message": "No context stored"}
    return {"chain": chain_id, "context": ctx[chain_id]}


if __name__ == "__main__":
    mcp.run(transport="stdio")
