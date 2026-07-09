# Chained Prompts MCP

Run a **phased protocol one phase at a time — with no phase skippable.**

A *chain* is an ordered sequence of *phases*, each with a prompt. The server
serves the phases one at a time: `chain_start` returns the first phase's prompt,
you do that phase, then `chain_complete` returns the next — all the way to the
end. You can only ever complete the *current* phase; jumping ahead is rejected.
That is the whole product: a skill or methodology that **must** run in order,
without a step being dropped under momentum.

The motivating case is an LLM agent running a multi-phase methodology (an
incident investigation, a review protocol, a release gate). Left to itself an
agent skips the unglamorous late phases — the remediation, the anti-pattern
sweep. Delivered as a chain, each phase arrives only after the previous one is
marked done, and the final phase is genuinely reached.

---

## Skill-as-Chained-Prompt (anchor mode)

The primary use is running an existing **phased skill** as a chain. A phase
stores only **anchors** into the LIVE skill file —
`{skill_path, anchors: {phase: {start, end}}}` — never a copy. At run time the
server re-reads the skill and serves the slice, so the skill stays the single
source of truth: edit it, and the next run reflects the edit with zero syncing.

- **Delivery is first-party (tool-return).** `chain_start` / `chain_complete`
  return the phase prompt in their result. The server **never** reaches into your
  session to push a prompt — no terminal injection, no out-of-band write.
- **Skipping is impossible.** `chain_complete` only serves the next uncompleted
  phase in order; completing a later phase returns `out_of_order`.
- **Anti-idle self-wake.** A chain, once started, is meant to run to its final
  phase. `chain_start` tells the agent to arm a first-party recurring self-wake
  (its harness's own `ScheduleWakeup` / `CronCreate`), and `chain_reminder`
  returns the current phase + its prompt + a standing "do not go idle mid-chain"
  message for that wake to fire. The server supplies the *data*; the agent arms
  and fires the wake itself.
- **Storage** lives under `~/.chained-prompts/` (override `CHAINED_PROMPTS_DIR`).

### Register the MCP

Claude Code (`~/.claude.json`):

```json
"chained-prompts": {
  "command": "python3",
  "args": ["<path>/chained-prompts/run_server.py"],
  "env": { "CHAINED_PROMPTS_DIR": "~/.chained-prompts" }
}
```

Requires `fastmcp` (`pip install fastmcp`) and a session restart to load. Seed
your store with the bundled example:

```bash
mkdir -p ~/.chained-prompts
cp chains.example.json ~/.chained-prompts/chains.json
```

The example registers the 10-phase `incident-investigation` chain in anchor mode
against `~/.claude/skills/incident-investigation/SKILL.md`.

---

## Tools

| Tool | Purpose |
|------|---------|
| `chain_list()` | List all defined chains with progress |
| `chain_start(chain_id)` | Begin a chain; returns phase 1's prompt |
| `chain_complete(chain_id, phase)` | Mark the current phase done; returns the next prompt |
| `chain_status(chain_id?)` | Progress of a chain (all active if no `chain_id`) |
| `chain_reminder(chain_id)` | Current phase + prompt + anti-idle message (for a self-armed wake) |
| `chain_reset(chain_id)` | Reset a chain's run state |
| `chain_define(chain_id, name, phases, prompts?, skill_path?, anchors?)` | Create/update a chain |
| `chain_delete(chain_id)` | Remove a chain |
| `chain_get(chain_id)` | Full chain definition |
| `phase_edit(chain_id, phase, prompt)` | Edit one phase's literal prompt (legacy mode) |
| `chain_pass(chain_id, context, merge?)` | Seed context for the next run |
| `chain_context(chain_id)` | Read a chain's stored context |

---

## Running a chain

```python
r = chain_start("incident-investigation")
# r["prompt"] is Phase 1's text. Do it, then:
# r["arm_wake"] tells you to arm a ~5-min self-wake calling chain_reminder(...).

r = chain_complete("incident-investigation", "phase1")
# r["next_prompt"] is Phase 2's text. Repeat through phase10.
# The last chain_complete returns {"chain_complete": true, ...}.
```

On each self-wake tick you armed, call `chain_reminder("incident-investigation")`
to refocus: it returns the phase you're on, that phase's prompt, and the
do-not-go-idle message. When the chain finishes, cancel the wake.

---

## Converting a skill into a chain

Use the bundled `skill-to-chained-prompt` skill (under `skills/`): it picks
anchors from a phased skill's headings and adds a self-redirect line to the skill
so future invocations run it as a chain automatically.

---

## Design notes

- **First-party delivery only.** The server holds state and returns prompts; it
  does not inject into the agent's session. Waking a dormant agent is the agent's
  first-party job (its own scheduler). Anything that writes keystrokes into an
  agent's session from an outside process is out of scope by design.
- **Fail loud on bad anchors.** If an anchor can't be resolved from the live
  skill, `chain_start` / `chain_complete` return an `anchor_resolution_failed`
  error rather than serving a wrong or empty phase.
- **Storage layout** (`~/.chained-prompts/`): `chains.json` (definitions),
  `state.json` (per-run progress), `context.json` (optional cross-run context).
