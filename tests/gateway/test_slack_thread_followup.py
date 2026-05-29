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
from unittest.mock import MagicMock

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

import gateway.platforms.slack as _slack_mod  # noqa: E402
_slack_mod.SLACK_AVAILABLE = True

from gateway.platforms.slack import SlackAdapter  # noqa: E402


# The exact regex used in gateway/platforms/base.py to parse [[react:emoji]].
# Kept in sync here so the parsing contract is covered without standing up the
# full _process_message_background path (which needs a live AsyncWebClient).
_REACT_RE = re.compile(r"\[\[react:\s*:?([a-z0-9_+\-]+):?\s*\]\]")


def _make_adapter(extra: dict) -> SlackAdapter:
    adapter = object.__new__(SlackAdapter)
    adapter.platform = Platform.SLACK
    adapter.config = PlatformConfig(enabled=True, extra=extra)
    adapter._bot_user_id = "U_BOT_123"
    adapter._team_bot_user_ids = {}
    adapter._bot_message_ts = set()
    adapter._mentioned_threads = set()
    return adapter


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


def test_followup_mode_config_overrides_env(monkeypatch):
    monkeypatch.setenv("SLACK_THREAD_FOLLOWUP_MODE", "off")
    a = _make_adapter({"thread_followup_mode": "agent"})
    assert a._thread_followup_mode() == "agent"


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


# ---- sentinel parsing is unconditional (regression: [[silent]] leak) -------
# The send chokepoint in base.py must parse decision sentinels on EVERY Slack
# turn, not only is_followup_decision turns — otherwise a model that emits
# [[silent]] in a direct-mention turn leaks the raw token into the channel as
# visible text (observed in prod 2026-05-29). This guards the gate condition.

import inspect  # noqa: E402
from gateway.platforms import base as _base_mod  # noqa: E402


def _send_chokepoint_source() -> str:
    src = inspect.getsource(_base_mod.BasePlatformAdapter)
    i = src.index("[[silent]]")
    # the `if response:` guard that opens the sentinel block sits just above
    return src[max(0, i - 600):i]


def test_sentinel_block_not_gated_on_followup_flag():
    """The sentinel-parsing block must NOT be gated on is_followup_decision."""
    head = _send_chokepoint_source()
    # The guard immediately preceding the [[silent]] check is a bare
    # `if response:` — not `if response and ... is_followup_decision`.
    assert "is_followup_decision" not in head, (
        "sentinel parsing is gated on is_followup_decision again — a [[silent]]/"
        "[[react]] emitted outside triage will leak as visible text"
    )
    assert "if response:" in head
