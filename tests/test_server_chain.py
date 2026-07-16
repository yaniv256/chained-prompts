"""U3/U4/U5: anchor schema, tool-return delivery, and anti-skip — behavior tests.

Server module is imported with a fastmcp stub (see server.py), so @mcp.tool
functions are plain callables here. Storage is redirected to a tmp dir per test.
"""
import importlib
import textwrap
import pytest


SKILL = """\
# Incident Skill

### Phase 1: Open
Open the investigation.

### Phase 2: Hypotheses
List hypotheses.

### Phase 3: Fix
Apply the fix.
"""


@pytest.fixture
def srv(tmp_path, monkeypatch):
    monkeypatch.setenv("CHAINED_PROMPTS_DIR", str(tmp_path / "store"))
    import server
    importlib.reload(server)  # re-evaluate BASE_DIR from the env
    skill = tmp_path / "skill.md"
    skill.write_text(SKILL)
    server._skill = str(skill)
    return server


def _define_anchor_chain(server):
    return server.chain_define(
        chain_id="inc",
        name="Incident",
        phases=["p1", "p2", "p3"],
        skill_path=server._skill,
        anchors={
            "p1": {"start": "### Phase 1: Open", "end": "### Phase 2: Hypotheses"},
            "p2": {"start": "### Phase 2: Hypotheses", "end": "### Phase 3: Fix"},
            "p3": {"start": "### Phase 3: Fix", "end": None},
        },
    )


# --- U3: schema ---
def test_define_anchor_chain(srv):
    r = _define_anchor_chain(srv)
    assert r["status"] == "defined" and r["mode"] == "anchor"
    got = srv.chain_get("inc")
    assert got["skill_path"] == srv._skill and "anchors" in got


def test_define_legacy_still_works(srv):
    r = srv.chain_define("leg", "Legacy", ["a"], prompts={"a": "do a"})
    assert r["mode"] == "legacy"


def test_anchor_mode_requires_skill_path(srv):
    r = srv.chain_define("bad", "Bad", ["p1"], anchors={"p1": {"start": "x"}})
    assert "error" in r


# --- U4: tool-return delivery ---
def test_start_returns_phase1_prompt(srv):
    _define_anchor_chain(srv)
    r = srv.chain_start("inc")
    # Delivery is always first-party tool-return: the prompt comes back in-result.
    assert "prompt" in r
    assert "Open the investigation." in r["prompt"]
    assert "List hypotheses." not in r["prompt"]
    assert r["phase_index"] == 1 and r["total"] == 3


def test_complete_returns_next_prompt(srv):
    _define_anchor_chain(srv)
    srv.chain_start("inc")
    r = srv.chain_complete("inc", "p1")
    assert r["auto_triggered"] == "p2"
    assert "List hypotheses." in r["next_prompt"]


def test_live_edit_reflected_through_server(srv, tmp_path):
    _define_anchor_chain(srv)
    (tmp_path / "skill.md").write_text(SKILL.replace("Open the investigation.", "Open it. EDITED"))
    r = srv.chain_start("inc")
    assert "EDITED" in r["prompt"]  # no manifest change needed


# --- U5: anti-skip ---
def test_cannot_skip_to_later_phase(srv):
    _define_anchor_chain(srv)
    srv.chain_start("inc")
    r = srv.chain_complete("inc", "p3")  # try to jump to the last phase
    assert r["error"] == "out_of_order"
    assert r["expected_phase"] == "p1"


def test_sequential_completion_advances(srv):
    _define_anchor_chain(srv)
    srv.chain_start("inc")
    assert srv.chain_complete("inc", "p1")["auto_triggered"] == "p2"
    assert srv.chain_complete("inc", "p2")["auto_triggered"] == "p3"
    final = srv.chain_complete("inc", "p3")
    assert final.get("chain_complete") is True


def test_recomplete_current_is_idempotent(srv):
    _define_anchor_chain(srv)
    srv.chain_start("inc")
    srv.chain_complete("inc", "p1")
    # p2 is now current; completing p1 again is out_of_order (already done)
    r = srv.chain_complete("inc", "p1")
    assert r["error"] == "out_of_order" and r["expected_phase"] == "p2"


def test_fail_loud_on_bad_anchor(srv):
    r = srv.chain_define(
        "inc", "Incident", ["p1"],
        skill_path=srv._skill,
        anchors={"p1": {"start": "### Phase 9: Nope", "end": None}},
    )
    assert r["error"] == "anchor_validation_failed"
    assert r["phase"] == "p1"
    assert "start anchor not found" in r["message"]
    assert "inc" not in srv.load_config().get("chains", {})


def test_define_rejects_multiline_anchor_before_persisting(srv):
    r = srv.chain_define(
        "inc", "Incident", ["p1"],
        skill_path=srv._skill,
        anchors={
            "p1": {
                "start": "### Phase 1: Open",
                "end": "---\n\n### Phase 2: Hypotheses",
            }
        },
    )
    assert r["error"] == "anchor_validation_failed"
    assert r["phase"] == "p1"
    assert "end anchor not found" in r["message"]
    assert "inc" not in srv.load_config().get("chains", {})


# --- anti-idle self-wake reminder ---
def test_start_carries_arm_wake_and_reminder(srv):
    _define_anchor_chain(srv)
    r = srv.chain_start("inc")
    # start must tell the agent to arm a first-party self-wake and carry the message
    assert "arm_wake" in r and "ScheduleWakeup" in r["arm_wake"]
    assert "reminder" in r and "behavior-space attractor" in r["reminder"]


def test_reminder_returns_current_phase_and_message(srv):
    _define_anchor_chain(srv)
    srv.chain_start("inc")
    srv.chain_complete("inc", "p1")  # now on p2
    rem = srv.chain_reminder("inc")
    assert rem["active"] is True
    assert rem["current_phase"] == "p2"
    assert "List hypotheses." in rem["phase_prompt"]
    assert "will of the user" in rem["reminder"]
    assert rem["progress"] == "1/3"


def test_reminder_inactive_when_not_running(srv):
    _define_anchor_chain(srv)
    rem = srv.chain_reminder("inc")  # never started
    assert rem["active"] is False
    assert "cancel" in rem["message"].lower()


def test_complete_final_phase_asks_to_cancel_wake(srv):
    _define_anchor_chain(srv)
    srv.chain_start("inc")
    srv.chain_complete("inc", "p1")
    srv.chain_complete("inc", "p2")
    final = srv.chain_complete("inc", "p3")
    assert final.get("chain_complete") is True
    assert "cancel_wake" in final
