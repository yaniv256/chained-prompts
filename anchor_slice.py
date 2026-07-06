"""Anchor-slice resolution — the correctness core of skill-as-chained-prompt.

A chain phase is stored as anchors into a LIVE skill file (never a copy):
`{skill_path, start_anchor, end_anchor}`. At serve time we re-read the skill and
return the text from the start-anchor line up to (not including) the end-anchor
line. This keeps the skill the single source of truth — editing the skill body
between anchors is reflected on the next resolve with no manifest change.

Anchors are verbatim, UNIQUE lines from the skill (normally `### Phase N:`
headings). If an anchor is missing or ambiguous we fail loud rather than serve a
guessed or empty slice — that's the signal to re-pick the anchor.
"""
from pathlib import Path


class AnchorError(ValueError):
    """Raised when an anchor cannot be resolved uniquely against the skill."""


def _unique_line_index(lines, anchor, skill_path, which):
    """Index of the single line equal to `anchor` (after rstrip). Fail loud otherwise."""
    matches = [i for i, ln in enumerate(lines) if ln.rstrip("\n") == anchor]
    if not matches:
        raise AnchorError(
            f"{which} anchor not found in {skill_path!r}: {anchor!r}"
        )
    if len(matches) > 1:
        raise AnchorError(
            f"{which} anchor is ambiguous in {skill_path!r} "
            f"(matched {len(matches)} lines): {anchor!r}"
        )
    return matches[0]


def resolve_anchor_slice(skill_path, start_anchor, end_anchor):
    """Return the live skill text from `start_anchor` up to `end_anchor`.

    - `start_anchor`: verbatim unique line where the slice begins (inclusive).
    - `end_anchor`: verbatim unique line where the slice ends (exclusive), or
      None to run to end-of-file.
    Raises AnchorError if either anchor is missing or matches more than one line.
    """
    path = Path(skill_path).expanduser()
    if not path.exists():
        raise AnchorError(f"skill file not found: {skill_path!r}")
    text = path.read_text()
    lines = text.splitlines(keepends=True)

    start = _unique_line_index(lines, start_anchor, skill_path, "start")
    if end_anchor is None:
        end = len(lines)
    else:
        end = _unique_line_index(lines, end_anchor, skill_path, "end")
        if end < start:
            raise AnchorError(
                f"end anchor precedes start anchor in {skill_path!r}: "
                f"{end_anchor!r} before {start_anchor!r}"
            )
    return "".join(lines[start:end]).rstrip("\n") + "\n"
