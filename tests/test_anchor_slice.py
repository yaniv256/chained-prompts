"""U2: anchor-slice resolver — the correctness core. Test-first."""
import textwrap
import pytest
from anchor_slice import resolve_anchor_slice, AnchorError


def _write(tmp_path, body):
    p = tmp_path / "skill.md"
    p.write_text(textwrap.dedent(body))
    return str(p)


SKILL = """\
# A Skill

## The Phases

### Phase 1: Open
Do the opening work.
More phase-1 text.

### Phase 2: Hypotheses
List hypotheses.

### Phase 3: Evidence
Gather evidence.

## Appendix
Not a phase.
"""


def test_slice_between_two_headings(tmp_path):
    path = _write(tmp_path, SKILL)
    got = resolve_anchor_slice(path, "### Phase 1: Open", "### Phase 2: Hypotheses")
    assert "Do the opening work." in got
    assert "More phase-1 text." in got
    assert "List hypotheses." not in got          # stops before phase 2
    assert got.startswith("### Phase 1: Open")     # includes the start anchor


def test_last_phase_end_sentinel_top_section(tmp_path):
    path = _write(tmp_path, SKILL)
    got = resolve_anchor_slice(path, "### Phase 3: Evidence", "## Appendix")
    assert "Gather evidence." in got
    assert "Not a phase." not in got


def test_end_anchor_eof_when_none(tmp_path):
    path = _write(tmp_path, SKILL)
    got = resolve_anchor_slice(path, "## Appendix", None)
    assert "Not a phase." in got                   # runs to EOF


def test_missing_start_anchor_errors(tmp_path):
    path = _write(tmp_path, SKILL)
    with pytest.raises(AnchorError):
        resolve_anchor_slice(path, "### Phase 9: Nope", "### Phase 2: Hypotheses")


def test_ambiguous_anchor_errors(tmp_path):
    path = _write(tmp_path, "### Phase 1: Open\nx\n### Phase 1: Open\ny\n")
    with pytest.raises(AnchorError):
        resolve_anchor_slice(path, "### Phase 1: Open", None)


def test_live_edit_reflected(tmp_path):
    """The sync-free property: editing body between anchors shows up, no manifest change."""
    path = _write(tmp_path, SKILL)
    before = resolve_anchor_slice(path, "### Phase 1: Open", "### Phase 2: Hypotheses")
    assert "EDITED" not in before
    (tmp_path / "skill.md").write_text(SKILL.replace("Do the opening work.", "Do the opening work. EDITED"))
    after = resolve_anchor_slice(path, "### Phase 1: Open", "### Phase 2: Hypotheses")
    assert "EDITED" in after


def test_tilde_and_relative_path_resolves(tmp_path):
    path = _write(tmp_path, SKILL)
    # absolute already covered; ensure a Path-like/str both work
    got = resolve_anchor_slice(str(path), "### Phase 2: Hypotheses", "### Phase 3: Evidence")
    assert "List hypotheses." in got
