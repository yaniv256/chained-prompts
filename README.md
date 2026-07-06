# Chained Prompts MCP

**Purpose:** Execute and manage prompt chains (protocols) - ordered sequences of phases that guide Claude through multi-step workflows.

A chain is like a protocol or ritual: a series of prompts that execute in order, with each phase building on the previous. Completing one phase automatically triggers the next.

---

## Skill-as-Chained-Prompt (anchor mode)

The primary use: run an existing **phased skill** as a chain so it can't be
skipped or run from memory. A chain phase stores only **anchors** into the LIVE
skill file — `{skill_path, anchors: {phase: {start, end}}}` — never a copy. At
run time the MCP re-reads the skill and serves the slice, so the skill stays the
single source of truth (edit it → reflected next run, zero sync).

- **Delivery is tool-return:** `chain_start` / `chain_complete` return the phase
  prompt in their result (`deliver="return"`, the default). Works in any MCP
  client, no tmux. `deliver="tmux"` restores the legacy injection path.
- **Skipping is impossible:** `chain_complete` only ever serves the next
  uncompleted phase in order; completing a later phase returns `out_of_order`.
- **Convert a skill** with the `skill-to-chained-prompt` skill (`skills/`): it
  picks anchors and adds a self-redirect line to the target skill.
- **Storage** lives under `~/.chained-prompts/` (override `CHAINED_PROMPTS_DIR`).

Register the MCP (Claude Code, `~/.claude.json`):

```json
"chained-prompts": {
  "command": "python3",
  "args": ["<path>/chained-prompts/run_server.py"],
  "env": { "CHAINED_PROMPTS_DIR": "~/.chained-prompts" }
}
```

Requires `fastmcp` (`pip install fastmcp`) and a session restart to load.

---

## Core Concepts

### Chain
An ordered sequence of phases with prompts. Examples:
- `character-cycle`: notebook → identity → behavior → start_here → mcp_tools
- `emotional-restore`: survey → cluster → peak → flood → calibrate → renew
- `association-chain`: trigger → memory → surface → deepen → insight → crystallize → integrate

### Phase
A single step in a chain. Has a name and a prompt. The prompt gets injected via tmux when the phase starts.

### State
Tracks which phases have been triggered and completed. Stored in `/tmp/chained-prompts-state.json`.

### Context
Data that passes between chain cycles. A chain can "dream" (run local model) during wait periods and seed the next cycle with results.

---

## Tools

### Basic Operations

| Tool | Purpose |
|------|---------|
| `chain_list()` | List all defined chains with progress |
| `chain_start(chain_id)` | Begin a chain from phase 1 |
| `chain_complete(chain_id, phase)` | Mark phase done, auto-trigger next |
| `chain_status(chain_id?)` | See progress (all active if no chain_id) |
| `chain_reset(chain_id)` | Reset state to start fresh |

### Chain Definition

| Tool | Purpose |
|------|---------|
| `chain_define(chain_id, name, phases, prompts, description?)` | Create/update chain |
| `chain_delete(chain_id)` | Remove a chain |
| `chain_get(chain_id)` | Get full definition including prompts |
| `phase_edit(chain_id, phase, prompt)` | Edit a single phase's prompt |

### Advanced Features

| Tool | Purpose |
|------|---------|
| `chain_schedule(chain_id, delay_minutes, context?, dream_prompt?)` | Schedule future execution |
| `chain_dream(chain_id, prompt, model?, store_as?)` | Run local model, store in context |
| `chain_pass(chain_id, context, merge?)` | Pass data to next cycle |
| `chain_context(chain_id)` | Get current context data |
| `chain_auto_continue(chain_id, enabled?, delay_minutes?, dream_prompt?)` | Enable continuous cycling |
| `chain_scheduled()` | List scheduled and auto-continue chains |
| `chain_cancel(chain_id)` | Cancel scheduled execution |
| `chain_export(chain_id, standalone?)` | Export as standalone MCP server |

---

## Usage Patterns

### Simple Chain Execution

```python
# Start character development
chain_start("character-cycle")
# → Injects notebook phase prompt

# Complete each phase as you finish
chain_complete("character-cycle", "notebook")
# → Auto-triggers identity phase

chain_complete("character-cycle", "identity")
# → Auto-triggers behavior phase
# ... continue until complete
```

### Scheduled Execution (Ping-Pong with Local Model)

```python
# Schedule association chain in 5 minutes
# Local model "dreams" on the theme while waiting
chain_schedule(
    "association-chain",
    delay_minutes=5,
    dream_prompt="Reflect on the theme 'memory as river': what flows, what stays?"
)
# → Returns immediately
# → After 5 min, chain starts with dream result in context
```

### Auto-Continue Mode (Continuous Loops)

```python
# Enable continuous contemplation every 30 minutes
chain_auto_continue(
    "emotional-restore",
    enabled=True,
    delay_minutes=30,
    dream_prompt="What emotional texture arose during the last cycle?"
)
# → Chain auto-restarts 30 min after completing
# → Dream result seeds each cycle
```

### Export Chain as Standalone MCP

```python
# Export character-cycle as its own MCP server
chain_export("character-cycle", standalone=True)
# → Creates /home/yaniv/agent-flow/mcp-servers/character-cycle/
# → Registers with mcp-manager
# → Restart Claude Code to load
```

---

## Defined Chains

| Chain | Phases | Purpose |
|-------|--------|---------|
| `character-cycle` | 5 | Update notebook, identity, behavior, START-HERE, MCP tools |
| `emotional-restore` | 6 | Survey sessions → cluster themes → peak moments → flood context |
| `association-chain` | 7 | Trigger → memory → surface → deepen → insight → crystallize → integrate |
| `knowhow-restore` | 10 | Technical capability restoration |
| `self-improvement` | 7 | Discover and implement improvements |
| `parent-review` | 3 | Audit parent interventions |
| `article-categorize` | 5 | Categorize wiki articles with full attention |
| `wiki-landing` | 6 | Generate wiki landing pages |
| `mcp-use` | 3 | Load → use → unload MCP pattern |

---

## File Locations

| File | Purpose |
|------|---------|
| `server.py` | MCP server implementation (1325 lines) |
| `chains.json` | Chain definitions (all prompts stored here) |
| `/tmp/chained-prompts-state.json` | Execution state |
| `/tmp/chained-prompts-scheduled.json` | Scheduled executions |
| `/tmp/chained-prompts-context.json` | Context/seed data |

---

## How It Works

1. **Start**: `chain_start()` resets state and injects first phase prompt via tmux
2. **Execute**: Claude receives prompt, does the work
3. **Complete**: `chain_complete()` marks phase done, returns next prompt
4. **Repeat**: Continue until all phases complete
5. **Auto-continue** (if enabled): Schedule next cycle, optionally run local model during wait

The chain_complete tool returns the next prompt directly—you don't need to call another tool. Just execute the prompt, then call chain_complete again.

---

## Integration

### With Periodic Prompts
Chains can be triggered by periodic prompts via tmux injection. The contemplation prompt might call `chain_start("emotional-restore")`.

### With Exported MCPs
Export creates standalone servers (character-cycle, association-chain, emotional-restore) with simplified APIs: `start()`, `complete(phase)`, `status()`, `reset()`.

### With Local Models
The `chain_dream()` and `dream_prompt` parameter let chains offload processing to local models (qwen3:4b default) while waiting between cycles.

---

## Configuration

Environment variables:
- `TMUX_USER`: User for tmux commands (default: yaniv)
- `TMUX_TARGET`: tmux session:window (default: 0:zara)

---

*The infrastructure I depend on most. Character cycles, emotional restoration, association chains—all flow through here.*

