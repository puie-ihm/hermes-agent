"""Tests for Slack thread_followup_mode=agent (IntelHouse fork feature).

thread_followup_mode=agent relaxes strict_mention so that non-mention
follow-ups inside threads the bot already participates in reach the agent,
which then decides reply / [[react:emoji]] / [[silent]]. A circuit-breaker
caps consecutive bot-originated follow-up turns to prevent ack loops.

These tests lock in:
  * the config-parse contract for _thread_followup_mode,
  * the _in_bot_thread participation check (incl. error-degradation),
  * the decision-sentinel regex used by base.py's send chokepoint.

Mirrors the mock/bootstrap pattern in test_slack_mention.py.
"""

import re
import sys
from unittest.mock import AsyncMock, MagicMock

import pytest

from gateway.config import Platform, PlatformConfig


# ---------------------------------------------------------------------------
# Mock slack-bolt if not installed (same as test_slack_mention.py)
# ---------------------------------------------------------------------------

def _ensure_slack_mock():
    if "slack_bolt" in sys.modules and hasattr(sys.modules["slack_bolt"], "__file__"):
        return

    slack_bolt = MagicMock()
    slack_bolt.async_app.AsyncApp = MagicMock
    slack_bolt.adapter.socket_mode.async_handler.AsyncSocketModeHandler = MagicMock

    slack_sdk = MagicMock()
    slack_sdk.web.async_client.AsyncWebClient = MagicMock

    for name, mod in [
        ("slack_bolt", slack_bolt),
        ("slack_bolt.async_app", slack_bolt.async_app),
        ("slack_bolt.adapter", slack_bolt.adapter),
        ("slack_bolt.adapter.socket_mode", slack_bolt.adapter.socket_mode),
        ("slack_bolt.adapter.socket_mode.async_handler", slack_bolt.adapter.socket_mode.async_handler),
        ("slack_sdk", slack_sdk),
        ("slack_sdk.web", slack_sdk.web),
        ("slack_sdk.web.async_client", slack_sdk.web.async_client),
    ]:
        sys.modules.setdefault(name, mod)


_ensure_slack_mock()

import plugins.platforms.slack.adapter as _slack_mod  # noqa: E402
_slack_mod.SLACK_AVAILABLE = True

from plugins.platforms.slack.adapter import SlackAdapter  # noqa: E402


# The exact regex used in gateway/platforms/base.py to parse [[react:emoji]].
# Kept in sync here so the parsing contract is covered without standing up the
# full _process_message_background path (which needs a live AsyncWebClient).
_REACT_RE = re.compile(r"\[\[react:\s*:?([a-z0-9_+\-]+):?\s*\]\]")

# The mention-parse regex used in slack.py handle_event routing.
_MENTION_RE = re.compile(r"<@([A-Z0-9]+)>")


def _route_mentions(text: str, bot_uid: str):
    """Mirror slack.py's routing booleans for @-mention handling."""
    uids = set(_MENTION_RE.findall(text))
    is_mentioned = bool(bot_uid and bot_uid in uids)
    other = bool(uids - ({bot_uid} if bot_uid else set()))
    return is_mentioned, other


def _make_adapter(extra: dict) -> SlackAdapter:
    adapter = object.__new__(SlackAdapter)
    adapter.platform = Platform.SLACK
    adapter.config = PlatformConfig(enabled=True, extra=extra)
    adapter._bot_user_id = "U_BOT_123"
    adapter._team_bot_user_ids = {}
    adapter._bot_message_ts = set()
    adapter._mentioned_threads = set()
    return adapter


@pytest.fixture(autouse=True)
def _clean_slack_env(monkeypatch):
    """Isolate these tests from the operator box's exported Slack config.

    The gateway host exports SLACK_ALLOWED_USERS / SLACK_DM_ALLOWED_USERS /
    SLACK_ALLOW_ALL_USERS into the process environment (systemd
    EnvironmentFile), and the scope-aware readers fall back to os.environ for
    the default profile — so without this every allowlist assertion below
    inherits the live allowlist and passes/fails for the wrong reason. Each
    test sets exactly the vars it needs.
    """
    for var in ("SLACK_ALLOWED_USERS", "SLACK_DM_ALLOWED_USERS", "SLACK_ALLOW_ALL_USERS",
                "SLACK_THREAD_FOLLOWUP_MODE", "SLACK_STRICT_MENTION"):
        monkeypatch.delenv(var, raising=False)


# ---- config parsing -------------------------------------------------------

def test_followup_mode_agent_parsed(monkeypatch):
    monkeypatch.delenv("SLACK_THREAD_FOLLOWUP_MODE", raising=False)
    a = _make_adapter({"strict_mention": True, "thread_followup_mode": "agent"})
    assert a._thread_followup_mode() == "agent"


def test_followup_mode_defaults_off(monkeypatch):
    monkeypatch.delenv("SLACK_THREAD_FOLLOWUP_MODE", raising=False)
    a = _make_adapter({"strict_mention": True})
    assert a._thread_followup_mode() == "off"


def test_followup_mode_env_fallback(monkeypatch):
    monkeypatch.delenv("SLACK_THREAD_FOLLOWUP_MODE", raising=False)
    a = _make_adapter({})
    monkeypatch.setenv("SLACK_THREAD_FOLLOWUP_MODE", "agent")
    assert a._thread_followup_mode() == "agent"


def test_followup_mode_env_overrides_config(monkeypatch):
    """Precedence follows the ONE per-profile setting reader (env → YAML).

    The fork read ``config.extra`` first; v0.23.0 routes every platform knob
    through ``gateway.platforms._shared.extra_or_secret``, whose documented
    contract is explicit env → profile YAML → default (same as
    ``SLACK_REACTIONS``, ``SLACK_REQUIRE_MENTION``). Keeping one reader for
    every knob beats a fork-only precedence for one key; in production
    ``SLACK_THREAD_FOLLOWUP_MODE`` is unset and ``slack.thread_followup_mode:
    agent`` in config.yaml wins anyway.
    """
    monkeypatch.setenv("SLACK_THREAD_FOLLOWUP_MODE", "off")
    a = _make_adapter({"thread_followup_mode": "agent"})
    assert a._thread_followup_mode() == "off"


# ---- _in_bot_thread participation check -----------------------------------

def test_in_bot_thread_false_for_non_thread_reply():
    a = _make_adapter({"thread_followup_mode": "agent"})
    assert a._in_bot_thread("123.45", "C1", "U1", is_thread_reply=False) is False


def test_in_bot_thread_true_when_bot_started_thread():
    a = _make_adapter({"thread_followup_mode": "agent"})
    a._bot_message_ts.add("123.45")
    assert a._in_bot_thread("123.45", "C1", "U1", is_thread_reply=True) is True


def test_in_bot_thread_true_when_mentioned_thread():
    a = _make_adapter({"thread_followup_mode": "agent"})
    a._mentioned_threads.add("123.45")
    assert a._in_bot_thread("123.45", "C1", "U1", is_thread_reply=True) is True


def test_in_bot_thread_consults_session_store():
    a = _make_adapter({"thread_followup_mode": "agent"})
    a._has_active_session_for_thread = MagicMock(return_value=True)
    assert a._in_bot_thread("777.77", "C1", "U1", is_thread_reply=True) is True


def test_in_bot_thread_false_for_unrelated_thread():
    a = _make_adapter({"thread_followup_mode": "agent"})
    # Not bot-started, not mentioned, no active session → must degrade to
    # False (no false-positive wake-up in an unrelated thread).
    a._has_active_session_for_thread = MagicMock(return_value=False)
    assert a._in_bot_thread("999.99", "C1", "U1", is_thread_reply=True) is False


def test_in_bot_thread_swallows_session_lookup_errors():
    a = _make_adapter({"thread_followup_mode": "agent"})
    a._has_active_session_for_thread = MagicMock(side_effect=RuntimeError("no store"))
    # Must not propagate — degrade to False.
    assert a._in_bot_thread("999.99", "C1", "U1", is_thread_reply=True) is False


# ---- decision-sentinel regex (contract shared with base.py) ----------------

@pytest.mark.parametrize("text,expected", [
    ("[[react:+1]]", "+1"),
    ("[[react:eyes]]", "eyes"),
    ("[[react:pray]]", "pray"),
    ("[[react::thumbsup:]]", "thumbsup"),   # model wrapped emoji in colons
    ("[[react: white_check_mark ]]", "white_check_mark"),  # stray spaces
])
def test_react_sentinel_parses(text, expected):
    m = _REACT_RE.search(text)
    assert m is not None
    assert m.group(1) == expected


def test_silent_sentinel_detected():
    assert "[[silent]]" in "ok then [[silent]]"


def test_plain_reply_has_no_sentinel():
    text = "Sure, the deploy finished at 10:00."
    assert "[[silent]]" not in text
    assert _REACT_RE.search(text) is None


# ---- @-mention routing edge cases (prod bug: bot answered for @Matteo) ------

BOT = "U0AH8J6H5A9"   # LaHermes bot_user_id
MATTEO = "U01QBQA0YDT"  # a human


def test_mention_bot_only_is_addressed():
    is_m, other = _route_mentions(f"<@{BOT}> what's up", BOT)
    assert is_m is True and other is False


def test_mention_other_human_only_not_addressed():
    # The exact prod case: bot must NOT be considered addressed.
    is_m, other = _route_mentions(f"<@{MATTEO}> are you available at 9pm?", BOT)
    assert is_m is False and other is True


def test_mention_bot_and_human_bot_wins():
    # Bot mentioned alongside a human → bot IS addressed, should reply.
    is_m, other = _route_mentions(f"<@{BOT}> <@{MATTEO}> can you two sync?", BOT)
    assert is_m is True and other is True


def test_mention_none_is_plain_followup():
    is_m, other = _route_mentions("anyone up for lunch?", BOT)
    assert is_m is False and other is False


def test_mention_here_channel_not_bot():
    # @here / @channel are not <@Uxxx> tokens → not a bot mention.
    is_m, other = _route_mentions("<!here> standup in 5", BOT)
    assert is_m is False and other is False


# ---- sentinel parsing is unconditional (regression: [[silent]] leak) -------
# The send chokepoint in base.py must parse decision sentinels on EVERY Slack
# turn, not only is_followup_decision turns — otherwise a model that emits
# [[silent]] in a direct-mention turn leaks the raw token into the channel as
# visible text (observed in prod 2026-05-29). This guards the gate condition.

import inspect  # noqa: E402
from gateway.platforms import base as _base_mod  # noqa: E402


def _decision_filter_source() -> str:
    return inspect.getsource(_base_mod.BasePlatformAdapter._consume_decision_sentinels)


def test_sentinel_filter_is_unconditional():
    """The shared decision-sentinel filter must parse [[silent]]/[[react]]
    unconditionally — never gated on is_followup_decision, or a token emitted
    outside a triage turn leaks into the channel as visible text (prod
    2026-05-29)."""
    body = _decision_filter_source()
    assert "is_followup_decision" not in body, (
        "decision-sentinel parsing is gated on is_followup_decision again — a "
        "[[silent]]/[[react]] emitted outside triage will leak as visible text"
    )
    assert "[[silent]]" in body
    assert "[[react" in body


def test_every_slack_egress_runs_the_decision_filter():
    """BOTH Slack egress paths must route through _consume_decision_sentinels:
    the base.py chokepoint AND the run.py queued-follow-up resend. The resend
    path bypassing the filter is what leaked a literal [[react:eyes]] in prod
    (queued follow-up + streaming.enabled=false)."""
    chokepoint = inspect.getsource(
        _base_mod.BasePlatformAdapter._process_message_background
    )
    assert "_consume_decision_sentinels" in chokepoint, (
        "base.py chokepoint no longer routes through the shared sentinel filter"
    )
    from gateway import run as _run_mod  # noqa: PLC0415
    # Upstream split _run_agent into a thin wrapper that delegates to
    # _run_agent_inner, where the queued-follow-up resend now lives. Inspect
    # both so this guard survives that refactor (and any future re-split).
    resend = inspect.getsource(_run_mod.GatewayRunner._run_agent)
    if hasattr(_run_mod.GatewayRunner, "_run_agent_inner"):
        resend += inspect.getsource(_run_mod.GatewayRunner._run_agent_inner)
    assert "_consume_decision_sentinels" in resend, (
        "run.py queued-follow-up resend bypasses the sentinel filter — raw "
        "[[react:...]]/[[silent]] tokens will leak on queued follow-ups"
    )


# ---- bot-vs-human attribution for the circuit-breaker ----------------------
#
# Measured regression (2026-09-19, #agent-monitoring): a message posted with an
# app's USER token arrives with BOTH user (the human) and bot_id (the app), so a
# bare bot_id check counted it as bot traffic. Three such messages in a thread
# tripped the breaker and the bot went silent — no error, it just stopped
# answering.

HUMAN = "U028TNBPA22"
# Exact envelope from the incident: user-token post, human in `user`.
USER_TOKEN_POST = {"user": HUMAN, "bot_id": "B0B1T07L1NG", "app_id": "A0AH8JL4329", "text": "hi"}


def test_app_user_token_post_from_known_human_is_not_bot(monkeypatch):
    monkeypatch.setenv("SLACK_ALLOWED_USERS", f"{HUMAN},U_OTHER")
    a = _make_adapter({"thread_followup_mode": "agent"})
    assert a._is_bot_authored_followup(USER_TOKEN_POST) is False


def test_unknown_sender_with_bot_id_still_counts_as_bot(monkeypatch):
    monkeypatch.setenv("SLACK_ALLOWED_USERS", "U_SOMEONE_ELSE")
    monkeypatch.delenv("SLACK_ALLOW_ALL_USERS", raising=False)
    a = _make_adapter({"thread_followup_mode": "agent"})
    assert a._is_bot_authored_followup(USER_TOKEN_POST) is True


def test_dm_allowlist_also_marks_a_human(monkeypatch):
    monkeypatch.delenv("SLACK_ALLOWED_USERS", raising=False)
    monkeypatch.setenv("SLACK_DM_ALLOWED_USERS", HUMAN)
    a = _make_adapter({"thread_followup_mode": "agent"})
    assert a._is_bot_authored_followup(USER_TOKEN_POST) is False


def test_allow_all_users_marks_any_bot_id_sender_human(monkeypatch):
    monkeypatch.delenv("SLACK_ALLOWED_USERS", raising=False)
    monkeypatch.delenv("SLACK_DM_ALLOWED_USERS", raising=False)
    monkeypatch.setenv("SLACK_ALLOW_ALL_USERS", "true")
    a = _make_adapter({"thread_followup_mode": "agent"})
    assert a._is_bot_authored_followup(USER_TOKEN_POST) is False


def test_plain_bot_message_is_bot(monkeypatch):
    monkeypatch.setenv("SLACK_ALLOWED_USERS", HUMAN)
    a = _make_adapter({"thread_followup_mode": "agent"})
    assert a._is_bot_authored_followup({"bot_id": "B0AJB4857TJ", "text": "ok"}) is True


def test_human_message_without_bot_id_is_not_bot(monkeypatch):
    monkeypatch.delenv("SLACK_ALLOWED_USERS", raising=False)
    a = _make_adapter({"thread_followup_mode": "agent"})
    assert a._is_bot_authored_followup({"user": HUMAN, "text": "hi"}) is False


def test_bot_message_subtype_is_bot_even_with_human_user(monkeypatch):
    monkeypatch.setenv("SLACK_ALLOWED_USERS", HUMAN)
    a = _make_adapter({"thread_followup_mode": "agent"})
    assert a._is_bot_authored_followup({"user": HUMAN, "subtype": "bot_message"}) is True


# ---- agent follow-up gate: Jev triage wiring -------------------------------
#
# These execute the gate, they do not grep for its call site. The gate is what
# the fork's inline `_agent_mode and _in_thread` branch became after upstream
# rewrote the routing: on an unmentioned reply inside a thread the bot already
# participates in, Jev decides silent / react / reply before a full LLM turn is
# spent. Fail-open is the safety property — every failure mode must return True
# (continue on the model path).

import asyncio  # noqa: E402

from agent import jev_triage  # noqa: E402

THREAD = "1700000000.000100"
TS = "1700000000.000200"


def _make_gated_adapter(extra=None):
    a = _make_adapter(extra or {"thread_followup_mode": "agent", "strict_mention": True})
    a._bot_message_ts.add(THREAD)
    a._followup_bot_turns = {}
    a._has_active_session_for_thread = MagicMock(return_value=False)

    async def _ctx(*_a, **_kw):
        return ""

    a._fetch_thread_context = _ctx
    a._add_reaction = AsyncMock()
    return a


def _gate(a, *, event=None, text="hello", is_mentioned=False, is_thread_reply=True,
          channel_id="C1"):
    return asyncio.run(a._agent_followup_gate(
        event=event if event is not None else {"user": HUMAN, "ts": TS},
        channel_id=channel_id, ts=TS, event_thread_ts=THREAD, team_id="T1",
        text=text, is_mentioned=is_mentioned, is_thread_reply=is_thread_reply,
        user_id=HUMAN, is_dm=False))


def _fake_jev(monkeypatch, *, enabled=True, action=None, confidence=0.9, calls=None):
    monkeypatch.setattr(jev_triage, "is_enabled", lambda: enabled)
    monkeypatch.setattr(jev_triage, "react_emoji", lambda: "+1")

    def _decide(message, **kwargs):
        if calls is not None:
            calls.append((message, kwargs))
        return jev_triage.TriageDecision(
            action=action, confidence=confidence, reason="test", latency_ms=1)

    monkeypatch.setattr(jev_triage, "decide_followup", _decide)


def test_gate_off_by_default_does_not_consult_jev(monkeypatch):
    calls = []
    _fake_jev(monkeypatch, action="silent", calls=calls)
    a = _make_gated_adapter({"strict_mention": True})   # followup_mode defaults to off
    assert _gate(a) is True
    assert calls == []


def test_gate_skips_a_mentioned_message(monkeypatch):
    calls = []
    _fake_jev(monkeypatch, action="silent", calls=calls)
    a = _make_gated_adapter()
    assert _gate(a, is_mentioned=True) is True
    assert calls == []


def test_gate_skips_free_response_channel(monkeypatch):
    calls = []
    _fake_jev(monkeypatch, action="silent", calls=calls)
    a = _make_gated_adapter({"thread_followup_mode": "agent",
                             "strict_mention": True,
                             "free_response_channels": ["C1"]})
    assert _gate(a) is True
    assert calls == []


def test_gate_skips_when_require_mention_is_off(monkeypatch):
    calls = []
    _fake_jev(monkeypatch, action="silent", calls=calls)
    a = _make_gated_adapter({"thread_followup_mode": "agent", "require_mention": False})
    assert _gate(a) is True
    assert calls == []


def test_gate_skips_thread_the_bot_is_not_in(monkeypatch):
    calls = []
    _fake_jev(monkeypatch, action="silent", calls=calls)
    a = _make_gated_adapter()
    a._bot_message_ts.clear()
    assert _gate(a) is True
    assert calls == []


def test_gate_silent_drops_the_turn(monkeypatch):
    calls = []
    _fake_jev(monkeypatch, action="silent", calls=calls)
    a = _make_gated_adapter()
    assert _gate(a, text="เดี๋ยวผมคุยกับทีมก่อนนะ") is False
    assert calls and calls[0][0] == "เดี๋ยวผมคุยกับทีมก่อนนะ"
    a._add_reaction.assert_not_called()


def test_gate_react_adds_the_emoji_and_drops_the_turn(monkeypatch):
    _fake_jev(monkeypatch, action="react")
    a = _make_gated_adapter()
    assert _gate(a, text="ขอบคุณครับ") is False
    a._add_reaction.assert_called_once_with("C1", TS, "+1", "T1")


def test_gate_react_is_dropped_but_not_reacted_when_reactions_are_off(monkeypatch):
    _fake_jev(monkeypatch, action="react")
    a = _make_gated_adapter({"thread_followup_mode": "agent", "strict_mention": True,
                             "reactions": False})
    assert _gate(a) is False
    a._add_reaction.assert_not_called()


def test_gate_no_decision_falls_through_to_the_model(monkeypatch):
    _fake_jev(monkeypatch, action=None)
    a = _make_gated_adapter()
    assert _gate(a, text="LaHermes ช่วยสรุปให้หน่อย") is True


def test_gate_is_inert_when_jev_is_disabled(monkeypatch):
    calls = []
    _fake_jev(monkeypatch, enabled=False, action="silent", calls=calls)
    a = _make_gated_adapter()
    assert _gate(a) is True
    assert calls == []


def test_gate_failure_never_silences_the_bot(monkeypatch):
    monkeypatch.setattr(jev_triage, "is_enabled", lambda: True)
    monkeypatch.setattr(jev_triage, "react_emoji", lambda: "+1")

    def _boom(*_a, **_kw):
        raise RuntimeError("typesafe down")

    monkeypatch.setattr(jev_triage, "decide_followup", _boom)
    a = _make_gated_adapter()
    assert _gate(a) is True


def test_thread_context_fetch_failure_still_decides(monkeypatch):
    calls = []
    _fake_jev(monkeypatch, action="silent", calls=calls)
    a = _make_gated_adapter()

    async def _ctx(*_a, **_kw):
        raise RuntimeError("slack api down")

    a._fetch_thread_context = _ctx
    assert _gate(a) is False
    assert calls and calls[0][1].get("thread_context") == ""


def test_bot_authored_followup_keeps_the_model_path(monkeypatch):
    calls = []
    _fake_jev(monkeypatch, action="silent", calls=calls)
    a = _make_gated_adapter()
    event = {"bot_id": "B0AJB4857TJ", "text": "ok", "ts": TS}
    assert _gate(a, event=event) is True
    assert calls == []


def test_circuit_breaker_trips_after_three_bot_turns(monkeypatch):
    calls = []
    _fake_jev(monkeypatch, action=None, calls=calls)
    a = _make_gated_adapter()
    event = {"bot_id": "B0AJB4857TJ", "text": "ok", "ts": TS}
    assert [_gate(a, event=event) for _ in range(3)] == [True, True, True]
    assert _gate(a, event=event) is False
    assert calls == []   # bot traffic never reaches Jev


def test_a_human_turn_resets_the_circuit_breaker(monkeypatch):
    _fake_jev(monkeypatch, action=None)
    a = _make_gated_adapter()
    bot_event = {"bot_id": "B0AJB4857TJ", "text": "ok", "ts": TS}
    for _ in range(3):
        _gate(a, event=bot_event)
    assert _gate(a, event={"user": HUMAN, "ts": TS}) is True   # human resets
    assert [_gate(a, event=bot_event) for _ in range(3)] == [True, True, True]

