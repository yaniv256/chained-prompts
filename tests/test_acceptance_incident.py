"""U7 acceptance: drive the incident-investigation chain end-to-end against the
REAL deployed skill file, proving the 4 acceptance properties offline (the MCP
registration in U8 only exposes this same pipeline to the agent).
"""
import os
import importlib
from pathlib import Path
import pytest

SKILL = Path.home() / ".claude/skills/incident-investigation/SKILL.md"

PHASE_HEADINGS = [
    "### Phase 1: Open Investigation",
    "### Phase 2: Initial Hypotheses",
    "### Phase 3: Evidence Collection (Non-Destructive)",
    "### Phase 4: Revised Hypotheses (Post-Evidence)",
    "### Phase 5: Experimentation",
    "### Phase 6: Final Hypothesis Revision",
    "### Phase 7: Blame Assignment (Three Levels)",
    "### Phase 8: Immediate Fix",
    "### Phase 9: Anti-Pattern Search",
    "### Phase 10: Comprehensive Remediation Plan",
]
END = "## Additional Resources"


@pytest.fixture
def srv(tmp_path, monkeypatch):
    monkeypatch.setenv("CHAINED_PROMPTS_DIR", str(tmp_path / "store"))
    import server
    importlib.reload(server)
    return server


def _register(server):
    phases = [f"phase{i}" for i in range(1, 11)]
    anchors = {}
    for i, h in enumerate(PHASE_HEADINGS):
        end = PHASE_HEADINGS[i + 1] if i + 1 < len(PHASE_HEADINGS) else END
        anchors[f"phase{i+1}"] = {"start": h, "end": end}
    return server.chain_define(
        "incident-investigation", "Incident Investigation",
        phases, skill_path=str(SKILL), anchors=anchors,
    )


@pytest.mark.skipif(not SKILL.exists(), reason="incident-investigation skill not deployed")
def test_acceptance_all_four_properties(srv):
    _register(srv)

    # (a) each phase's slice matches the live skill text
    start = srv.chain_start("incident-investigation")
    assert start["prompt"].startswith("### Phase 1: Open Investigation")
    assert start["total"] == 10

    # (b) cannot skip: completing a later phase from phase 1 is rejected
    assert srv.chain_complete("incident-investigation", "phase10")["error"] == "out_of_order"

    # (d) all 10 phases must complete to finish — walk them in order
    seen = [start["prompt"]]
    for i in range(1, 10):
        r = srv.chain_complete("incident-investigation", f"phase{i}")
        assert r["auto_triggered"] == f"phase{i+1}", f"stuck at phase{i}"
        assert r["next_prompt"].startswith(PHASE_HEADINGS[i])  # phase i+1's heading
        seen.append(r["next_prompt"])
    final = srv.chain_complete("incident-investigation", "phase10")
    assert final.get("chain_complete") is True

    # phase 9 (anti-pattern search) and 10 (remediation) — the ones agents skip —
    # are genuinely served
    assert any("Anti-Pattern Search" in s for s in seen)
    assert any("Comprehensive Remediation Plan" in s for s in seen)
    # last phase runs to the section boundary, not into Additional Resources
    assert "Additional Resources" not in seen[-1]


@pytest.mark.skipif(not SKILL.exists(), reason="skill not deployed")
def test_acceptance_live_edit(srv, tmp_path, monkeypatch):
    # (c) editing the skill body reflects on next run with no manifest change.
    # Copy the skill to a temp path we can edit, register against it.
    copy = tmp_path / "skill.md"
    copy.write_text(SKILL.read_text())
    phases = [f"phase{i}" for i in range(1, 11)]
    anchors = {f"phase{i+1}": {"start": PHASE_HEADINGS[i],
                               "end": PHASE_HEADINGS[i+1] if i+1 < 10 else END}
               for i in range(10)}
    srv.chain_define("ii2", "II", phases, skill_path=str(copy), anchors=anchors)
    before = srv.chain_start("ii2")["prompt"]
    copy.write_text(copy.read_text().replace(
        "### Phase 1: Open Investigation", "### Phase 1: Open Investigation\nLIVE-EDIT-MARKER", 1))
    after = srv.chain_start("ii2")["prompt"]
    assert "LIVE-EDIT-MARKER" in after and "LIVE-EDIT-MARKER" not in before
