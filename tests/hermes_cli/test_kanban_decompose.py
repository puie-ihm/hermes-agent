"""Tests for the decomposer module + `hermes kanban decompose` CLI surface.

The auxiliary LLM client is mocked — no network calls. Tests exercise the
prompt plumbing, response parsing, DB writes (via the real DB helper),
and the assignee-fallback logic.
"""

from __future__ import annotations

import json as jsonlib
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc
from hermes_cli import kanban_decompose as decomp


@pytest.fixture
def kanban_home(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    kb.init_db()
    return home


def _fake_aux_response(content: str):
    resp = MagicMock()
    resp.choices = [MagicMock()]
    resp.choices[0].message.content = content
    return resp


def _mock_client_returning(content: str):
    client = MagicMock()
    client.chat.completions.create = MagicMock(return_value=_fake_aux_response(content))
    return client


def _patch_aux_client(content: str, *, model: str = "test-model"):
    # decompose_task now routes through call_llm (see #35566) — mock it at
    # the source module so task config, extra_body, and retries stay out of
    # unit-test scope.
    return patch(
        "agent.auxiliary_client.call_llm",
        return_value=_fake_aux_response(content),
    )


def _patch_list_profiles(names: list[str]):
    """Pretend the named profiles exist. The decomposer uses
    profiles_mod.list_profiles() to build the roster + valid-set, and
    profiles_mod.profile_exists() to resolve orchestrator/default."""
    from types import SimpleNamespace
    fake_profiles = [
        SimpleNamespace(
            name=n, is_default=(i == 0), description=f"desc for {n}",
            description_auto=False, model="m", provider="p", skill_count=1,
        )
        for i, n in enumerate(names)
    ]
    return [
        patch("hermes_cli.profiles.list_profiles", return_value=fake_profiles),
        patch("hermes_cli.profiles.profile_exists", side_effect=lambda x: x in names),
        patch("hermes_cli.profiles.get_active_profile_name", return_value=names[0] if names else "default"),
    ]


def test_decompose_with_fanout_creates_children(kanban_home):
    with kbc.connect() as conn:
        tid = kb.create_task(conn, title="ship a feature", triage=True)

    llm_payload = jsonlib.dumps({
        "fanout": True,
        "rationale": "test split",
        "tasks": [
            {"title": "research", "body": "look it up", "assignee": "researcher", "parents": []},
            {"title": "build", "body": "code it", "assignee": "engineer", "parents": [0]},
        ],
    })

    patches = _patch_list_profiles(["orchestrator", "researcher", "engineer"])
    for p in patches:
        p.start()
    try:
        with _patch_aux_client(llm_payload):
            outcome = decomp.decompose_task(tid, author="me")
    finally:
        for p in patches:
            p.stop()

    assert outcome.ok, outcome.reason
    assert outcome.fanout is True
    assert outcome.child_ids and len(outcome.child_ids) == 2

    with kbc.connect() as conn:
        root = kb.get_task(conn, tid)
        c0 = kb.get_task(conn, outcome.child_ids[0])
        c1 = kb.get_task(conn, outcome.child_ids[1])
    assert root.status == "todo"
    assert c0.status == "ready"
    assert c1.status == "todo"
    assert c0.assignee == "researcher"
    assert c1.assignee == "engineer"


def test_decompose_fanout_children_inherit_root_assignee_when_unrouted(kanban_home):
    """Unrouted children fall back to the ROOT task's assignee, not
    the decomposer's active profile (#114294). The active profile here is ``private``
    (an incognito profile with no credentials), so the old fallback spawned
    workers that deadlocked on capability blockers."""
    with kbc.connect() as conn:
        tid = kb.create_task(conn, title="ship it", assignee="zdr", triage=True)

    llm_payload = jsonlib.dumps({
        "fanout": True,
        "rationale": "test split",
        "tasks": [
            {"title": "research", "body": "look it up", "assignee": "made_up", "parents": []},
            {"title": "build", "body": "code it", "assignee": None, "parents": [0]},
        ],
    })

    # get_active_profile_name() is mocked to names[0] = "private" — the
    # global default chain would resolve there without kanban.default_assignee.
    patches = _patch_list_profiles(["private", "zdr"])
    for p in patches:
        p.start()
    try:
        with _patch_aux_client(llm_payload), patch(
            "hermes_cli.config.load_config_readonly",
            return_value={},
        ):
            outcome = decomp.decompose_task(tid, author="me")
    finally:
        for p in patches:
            p.stop()

    assert outcome.ok, outcome.reason
    with kbc.connect() as conn:
        root = kb.get_task(conn, tid)
        c0 = kb.get_task(conn, outcome.child_ids[0])
        c1 = kb.get_task(conn, outcome.child_ids[1])
    assert c0.assignee == "zdr"
    assert c1.assignee == "zdr"
    # Same class for the root: no ``orchestrator_profile`` must not hand the
    # orchestration card to the dispatcher's own (here: incognito) profile.
    assert root.assignee == "zdr"


def test_decompose_explicit_default_assignee_wins_over_root_assignee(kanban_home):
    """An explicitly configured ``kanban.default_assignee`` stays
    authoritative for unroutable children; the root task's assignee only
    fills in when no explicit default is set (explicit config → card
    assignee → active profile)."""
    with kbc.connect() as conn:
        tid = kb.create_task(conn, title="ship it", assignee="engineer", triage=True)

    llm_payload = jsonlib.dumps({
        "fanout": True,
        "rationale": "test split",
        "tasks": [
            {"title": "research", "body": "look it up", "assignee": "made_up", "parents": []},
            {"title": "build", "body": "code it", "assignee": None, "parents": [0]},
        ],
    })

    patches = _patch_list_profiles(["engineer", "docs", "private"])
    for p in patches:
        p.start()
    try:
        with _patch_aux_client(llm_payload), patch(
            "hermes_cli.config.load_config_readonly",
            return_value={"kanban": {"default_assignee": "docs"}},
        ):
            outcome = decomp.decompose_task(tid, author="me")
    finally:
        for p in patches:
            p.stop()

    assert outcome.ok, outcome.reason
    with kbc.connect() as conn:
        c0 = kb.get_task(conn, outcome.child_ids[0])
        c1 = kb.get_task(conn, outcome.child_ids[1])
    assert c0.assignee == "docs"
    assert c1.assignee == "docs"


def test_decompose_fanout_false_invalid_llm_assignee_uses_default(kanban_home):
    with kbc.connect() as conn:
        tid = kb.create_task(conn, title="route me safely", triage=True)

    llm_payload = jsonlib.dumps({
        "fanout": False,
        "rationale": "single unit",
        "title": "Tightened title",
        "body": "Route to fallback.",
        "assignee": "made_up",
    })

    patches = _patch_list_profiles(["orchestrator", "fallback"])
    for p in patches:
        p.start()
    try:
        with _patch_aux_client(llm_payload), patch(
            "hermes_cli.config.load_config_readonly",
            return_value={"kanban": {"default_assignee": "fallback"}},
        ):
            outcome = decomp.decompose_task(tid, author="me")
    finally:
        for p in patches:
            p.stop()

    assert outcome.ok, outcome.reason
    with kbc.connect() as conn:
        task = kb.get_task(conn, tid)
    assert task is not None
    assert task.assignee == "fallback"


def test_load_routing_falls_back_to_defaults_when_config_unreadable(kanban_home, monkeypatch):
    """decompose_task promises ok=False on expected failures; a config read that raises (missing
    profile home, HomeInitializationError) must not escape _load_routing as an exception."""
    from hermes_cli import config as config_mod

    def _boom():
        raise FileNotFoundError("profile home is gone")

    monkeypatch.setattr(config_mod, "load_config_readonly", _boom)
    routing = decomp._load_routing()
    assert routing.default_assignee == "default" and routing.auto_promote is True


def test_decompose_returns_false_when_task_not_triage(kanban_home):
    with kbc.connect() as conn:
        tid = kb.create_task(conn, title="x")  # ready, not triage

    patches = _patch_list_profiles(["orchestrator"])
    for p in patches:
        p.start()
    try:
        outcome = decomp.decompose_task(tid, author="me")
    finally:
        for p in patches:
            p.stop()
    assert outcome.ok is False




# ---------------------------------------------------------------------------
# kanban_auto_assignable — profiles that opt out of kanban auto-routing
# (fork b1fd81198a + 56fb13ec62, ported onto the v0.23.0 decomposer)
#
# A locked-down, external-facing profile must never be handed work by the
# auto-decomposer, and its own tasks must never be fanned out to internal
# workers. The opt-out lives in the profile's OWN profile.yaml so the policy
# travels with the profile instead of a central exclude-list.
# ---------------------------------------------------------------------------


def _patch_list_profiles_with_flags(specs: list[tuple[str, bool]]):
    """Like _patch_list_profiles, but each entry is (name, auto_assignable)."""
    from types import SimpleNamespace
    names = [n for n, _ in specs]
    fake_profiles = [
        SimpleNamespace(
            name=n, is_default=(i == 0), description=f"desc for {n}",
            description_auto=False, model="m", provider="p", skill_count=1,
            kanban_auto_assignable=flag,
        )
        for i, (n, flag) in enumerate(specs)
    ]
    flag_by_name = dict(specs)
    return [
        patch("hermes_cli.profiles.list_profiles", return_value=fake_profiles),
        patch("hermes_cli.profiles.profile_exists", side_effect=lambda x: x in names),
        patch("hermes_cli.profiles.get_active_profile_name",
              return_value=names[0] if names else "default"),
        # _is_auto_assignable reads profile.yaml directly rather than going
        # through list_profiles, so it needs its own stub.
        patch("hermes_cli.profiles.read_profile_meta",
              side_effect=lambda d: {
                  "description": "", "description_auto": False,
                  "kanban_auto_assignable": flag_by_name.get(Path(d).name, True),
              }),
        patch("hermes_cli.profiles.get_profile_dir", side_effect=Path),
    ]


def test_roster_excludes_profiles_that_opted_out():
    patches = _patch_list_profiles_with_flags([
        ("orchestrator", True),
        ("engineer", True),
        ("locked_down_worker", False),
    ])
    for p in patches:
        p.start()
    try:
        roster, valid = decomp._build_roster()
    finally:
        for p in patches:
            p.stop()

    roster_names = {e["name"] for e in roster}
    # Absent from the prompt roster...
    assert "locked_down_worker" not in roster_names
    # ...AND from the valid-set, or _normalize_assignee_choice() would let an
    # LLM-invented assignee through even though it was never offered.
    assert "locked_down_worker" not in valid
    assert roster_names == {"orchestrator", "engineer"}
    assert valid == {"orchestrator", "engineer"}


def test_profiles_without_the_flag_stay_assignable(kanban_home):
    """Backward compatibility: silence means routable, as before."""
    patches = _patch_list_profiles(["orchestrator", "engineer"])
    for p in patches:
        p.start()
    try:
        roster, valid = decomp._build_roster()
    finally:
        for p in patches:
            p.stop()
    assert valid == {"orchestrator", "engineer"}


def test_decompose_rewrites_assignee_that_opted_out(kanban_home):
    """End-to-end: even if the LLM names the excluded profile, it can't land."""
    with kbc.connect() as conn:
        tid = kb.create_task(conn, title="pull the numbers", triage=True)

    llm_payload = jsonlib.dumps({
        "fanout": False,
        "rationale": "single task",
        "title": "Pull the numbers",
        "body": "Read-only query.",
        "assignee": "locked_down_worker",
    })

    patches = _patch_list_profiles_with_flags([
        ("orchestrator", True),
        ("locked_down_worker", False),
    ])
    for p in patches:
        p.start()
    try:
        with _patch_aux_client(llm_payload):
            outcome = decomp.decompose_task(tid, author="me")
    finally:
        for p in patches:
            p.stop()

    assert outcome.ok, outcome.reason
    with kbc.connect() as conn:
        task = kb.get_task(conn, tid)
    assert task.assignee != "locked_down_worker"
    assert task.assignee == "orchestrator"


def test_default_assignee_falls_back_when_active_profile_opted_out():
    """The fallback must not re-open the hole _build_roster() closed.

    get_active_profile_name() is the running process's own profile. On a
    dedicated locked-down gateway that runs the auto-decomposer, the naive
    fallback would assign unroutable work straight back to itself.
    """
    patches = _patch_list_profiles_with_flags([
        ("locked_down_worker", False),
        ("orchestrator", True),
    ])
    for p in patches:
        p.start()
    try:
        resolved = decomp._resolve_profile_from_cfg({}, "default_assignee")
    finally:
        for p in patches:
            p.stop()

    assert resolved == "default"


def test_explicit_default_assignee_rejected_when_it_opted_out():
    patches = _patch_list_profiles_with_flags([
        ("orchestrator", True),
        ("locked_down_worker", False),
    ])
    for p in patches:
        p.start()
    try:
        resolved = decomp._resolve_profile_from_cfg(
            {"kanban": {"default_assignee": "locked_down_worker"}}, "default_assignee"
        )
    finally:
        for p in patches:
            p.stop()

    assert resolved != "locked_down_worker"
    assert resolved == "orchestrator"


def test_orchestrator_profile_is_not_filtered_by_the_opt_out():
    """orchestrator_profile is an explicit operator choice, not an auto-route —
    filtering it would silently move root-card ownership."""
    patches = _patch_list_profiles_with_flags([
        ("orchestrator", True),
        ("locked_down_worker", False),
    ])
    for p in patches:
        p.start()
    try:
        resolved = decomp._resolve_profile_from_cfg(
            {"kanban": {"orchestrator_profile": "locked_down_worker"}}, "orchestrator_profile"
        )
    finally:
        for p in patches:
            p.stop()

    assert resolved == "locked_down_worker"


# ---- the reverse direction: never fan out work FROM such a profile --------


def _fanout_payload():
    return jsonlib.dumps({
        "fanout": True,
        "rationale": "split it",
        "tasks": [
            {"title": "step one", "body": "a", "assignee": "orchestrator", "parents": []},
            {"title": "step two", "body": "b", "assignee": "engineer", "parents": [0]},
        ],
    })


def test_task_created_by_non_routable_profile_is_refused(kanban_home):
    with kbc.connect() as conn:
        tid = kb.create_task(
            conn, title="external buyer question", triage=True,
            created_by="locked_down_worker",
        )

    patches = _patch_list_profiles_with_flags([
        ("orchestrator", True),
        ("engineer", True),
        ("locked_down_worker", False),
    ])
    for p in patches:
        p.start()
    try:
        with _patch_aux_client(_fanout_payload()):
            outcome = decomp.decompose_task(tid, author="auto-decomposer")
    finally:
        for p in patches:
            p.stop()

    assert outcome.ok is False
    assert "outside kanban auto-routing" in outcome.reason
    assert "locked_down_worker" in outcome.reason

    # The outcome alone is not enough — assert nothing was written.
    with kbc.connect() as conn:
        task = kb.get_task(conn, tid)
        children = kb.list_tasks(conn, limit=100)
    assert task.status == "triage", "refused task must stay in triage"
    assert len(children) == 1, f"no children may be created, got {len(children)}"


def test_task_created_by_internal_profile_still_decomposes(kanban_home):
    """The gate must not block ordinary internal work."""
    with kbc.connect() as conn:
        tid = kb.create_task(
            conn, title="internal work", triage=True, created_by="orchestrator",
        )

    patches = _patch_list_profiles_with_flags([
        ("orchestrator", True),
        ("engineer", True),
        ("locked_down_worker", False),
    ])
    for p in patches:
        p.start()
    try:
        with _patch_aux_client(_fanout_payload()):
            outcome = decomp.decompose_task(tid, author="auto-decomposer")
    finally:
        for p in patches:
            p.stop()

    assert outcome.ok, outcome.reason
    assert outcome.child_ids and len(outcome.child_ids) == 2


def test_task_with_unknown_creator_still_decomposes(kanban_home):
    """created_by falls back to 'worker' when the profile is unresolvable.

    Unknown creators must stay permissive — the flag is an explicit opt-out,
    not an allowlist, and failing closed here would break ordinary dispatch.
    """
    with kbc.connect() as conn:
        tid = kb.create_task(
            conn, title="dispatcher work", triage=True, created_by="worker",
        )

    patches = _patch_list_profiles_with_flags([
        ("orchestrator", True),
        ("engineer", True),
    ])
    for p in patches:
        p.start()
    try:
        with _patch_aux_client(_fanout_payload()):
            outcome = decomp.decompose_task(tid, author="auto-decomposer")
    finally:
        for p in patches:
            p.stop()

    assert outcome.ok, outcome.reason
