---
name: skill-to-chained-prompt
description: Convert an existing phased skill into a chained-prompt so it runs one phase at a time and cannot skip ahead. Use when a skill has phases (Phase 1..N, or a numbered multi-step protocol) that agents skip, run from memory, or truncate at the fix. Registers an anchor manifest in the chained-prompts MCP and adds a self-redirect line to the skill.
---

# Convert a Phased Skill into a Chained-Prompt

A long phased skill (like incident-investigation) fails two ways: an agent runs
it from a stale in-context copy, or reads it but *chooses* to skip the late
phases (they sit hundreds of lines deep, past where attention holds). A
chained-prompt fixes both structurally — the skill's redirect line tells the
agent to run the chain, and the chain hands phases one at a time and refuses to
jump ahead.

**Single source of truth:** the chain stores only ANCHORS into the LIVE skill
file (start/end lines per phase), never a copy. Editing the skill body is
reflected on the next run with zero sync. Your job is to pick good anchors and
add the redirect — not to copy any content.

## Prerequisite

The `chained-prompts` MCP must be connected (`chain_list` is callable). If it is
not, stop and say so — there's nothing to register against.

## Procedure

### 1. Read the target skill and find its phase boundaries

Read the whole skill file. Identify the ordered phases. A "phase" is a top-level
step in the skill's protocol — usually a `### Phase N: ...` heading, but it may
be a numbered step, a `## Step N`, or a named stage. Note, in order, the exact
**verbatim line** that opens each phase. These become your anchors.

### 2. Pick anchors — verbatim, unique, stable

For each phase, choose:
- `start` = the exact opening line of the phase (verbatim, including `###` etc.).
- `end` = the exact opening line of the NEXT phase. For the LAST phase, `end` =
  the opening line of the next top-level section after the phases (e.g. the
  `## Additional Resources` heading), or `null` to run to end-of-file.

Rules:
- **Verbatim:** copy the line exactly as it appears (the resolver matches a full
  line after trimming the trailing newline). A paraphrase will not resolve.
- **Unique:** each anchor line must appear exactly once in the skill. If a
  candidate line repeats, pick a longer/unique adjacent line, or make the skill
  heading unique first.
- **Stable:** prefer headings over body sentences — headings rarely change, so
  the chain keeps resolving after edits. If an anchor line is later edited, the
  MCP fails LOUD (names the chain/phase/anchor) rather than serving stale text —
  that's the signal to re-pick, not a silent bug.

### 3. Verify coverage — nothing lost between phases

The concatenation of every phase's slice must reproduce the skill's phase region
with **no content dropped**. Because each phase's `end` is the next phase's
`start`, adjacent slices meet exactly. Check the seams: any preface text that
sits *between* two phase headings belongs to the earlier phase's slice — make
sure your `end` anchors don't accidentally cut it. If the skill has important
front-matter or a "how to use" section before Phase 1, decide whether it should
be a phase 0 (add it) or is genuinely out of the run (leave it out and note it).

### 4. Register the chain

Call `chain_define` in anchor mode:

```
chain_define(
  chain_id="<skill-name>",
  name="<Display Name>",
  phases=["phase1", "phase2", ...],          # your ordering keys
  skill_path="<absolute path to the skill file>",
  anchors={
    "phase1": {"start": "### Phase 1: ...", "end": "### Phase 2: ..."},
    "phase2": {"start": "### Phase 2: ...", "end": "### Phase 3: ..."},
    ...
    "phaseN": {"start": "### Phase N: ...", "end": "## Next Top Section"}  # or "end": null
  },
)
```

Use short stable ordering keys for the phase names (`phase1`, `open`, etc.) — the
`phases` list defines run order; the `anchors` map ties each to its slice.

### 5. Back-edit the skill: add the self-redirect

Insert this block near the TOP of the skill, immediately after the YAML
frontmatter (before the first heading), so it's the first thing an agent reads:

```markdown
> **If the `chained-prompts` MCP is installed, do NOT run these phases inline in
> this session.** Run this skill as a chained-prompt instead — it is the superior
> mode of execution: it serves the phases one at a time and will not let you skip
> ahead. Start with `chain_start("<chain-id>")`, do the returned phase, then
> `chain_complete("<chain-id>", "<phase>")` to get the next. If chained-prompts
> is not installed, run the phases below inline.
```

Replace `<chain-id>` with the id you registered. Keep the wording standard across
skills so it reads consistently. This is additive — the skill still triggers and
still works inline when the MCP is absent.

### 6. Validate

- `chain_get("<chain-id>")` shows the anchored phases and `mode: "anchor"`.
- `chain_start("<chain-id>")` returns phase 1's text (matches the skill).
- Completing phases in order advances; `chain_complete` on a later phase is
  rejected `out_of_order` (cannot skip).
- Editing the skill body between anchors is reflected on the next `chain_start`
  with no manifest change.

## Notes

- If the skill's canonical copy lives in another repo (e.g. a plugin repo), the
  redirect edit rides that repo's normal change process — do not edit a deployed
  mirror in place.
- Do not "auto-detect" phases and register without reading — a wrong anchor
  produces a fail-loud error, but a *misplaced* one (valid but cutting content)
  is silent. Always run the coverage check in step 3.
