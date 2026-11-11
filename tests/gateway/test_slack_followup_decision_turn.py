"""thread_followup_mode=agent: an unmentioned follow-up that reaches the model is a decision turn.

The v0.23 port dropped the fork's triage instruction, so every follow-up Jev triage passed on was
answered in full. A decision turn must (1) carry the instruction, (2) be reply_expected=False so a
[[silent]] / [[react:]] answer can stand, and (3) never stream: a native Slack stream cannot be
retracted, and the sentinel is only recognised once the whole reply exists.
"""

import asyncio
from types import SimpleNamespace

import pytest

from gateway.config import PlatformConfig, StreamingConfig
from gateway.stream_consumer import StreamConsumerConfig
from gateway.turn_context import TurnContext
from gateway.run_turn_runner import TurnRunner
from plugins.platforms.slack.adapter import SlackAdapter, _FOLLOWUP_DECISION_PROMPT


@pytest.fixture(autouse=True)
def _no_jev(monkeypatch):
    monkeypatch.setenv("JEV_TRIAGE_DISABLED", "1")
    for var in ("SLACK_ALLOWED_USERS", "SLACK_ALLOW_ALL_USERS", "SLACK_THREAD_FOLLOWUP_MODE"):
        monkeypatch.delenv(var, raising=False)


def _adapter(in_bot_thread=True):
    # The default (LaHermes) profile's live gating: strict_mention + agent follow-ups.
    adapter = SlackAdapter(PlatformConfig(extra={
        "require_mention": True, "strict_mention": True, "thread_followup_mode": "agent"}))
    adapter._bot_user_id = "UBOT"
    adapter._team_bot_user_ids["T1"] = "UBOT"
    adapter._in_bot_thread = lambda *a, **k: in_bot_thread

    async def empty(*_, **__):
        return ""

    async def user_name(*_, **__):
        return "Sebastian"

    adapter._fetch_thread_context = empty
    adapter._fetch_thread_parent_text = empty
    adapter._resolve_user_name = user_name
    handled = []

    async def capture(event):
        handled.append(event)

    adapter.handle_message = capture
    return adapter, handled


def _event(text, ts="101.000", channel="C123", channel_type="channel"):
    return {"type": "message", "channel": channel, "channel_type": channel_type, "team": "T1",
            "user": "U123", "text": text, "ts": ts, "thread_ts": "100.000"}


def test_unmentioned_followup_is_a_decision_turn():
    adapter, handled = _adapter()
    asyncio.run(adapter._handle_slack_message(_event("thanks, that works")))
    assert len(handled) == 1
    assert handled[0].reply_expected is False
    assert handled[0].text.startswith(_FOLLOWUP_DECISION_PROMPT)
    assert handled[0].text.endswith("thanks, that works")


def test_strict_mention_still_drops_replies_outside_the_bots_threads():
    adapter, handled = _adapter(in_bot_thread=False)
    asyncio.run(adapter._handle_slack_message(_event("thanks, that works")))
    assert handled == []


def test_dm_thread_reply_is_a_normal_turn():
    adapter, handled = _adapter()
    asyncio.run(adapter._handle_slack_message(_event("and the second one?", channel="D123", channel_type="im")))
    assert len(handled) == 1
    assert handled[0].reply_expected is not False
    assert _FOLLOWUP_DECISION_PROMPT not in handled[0].text


def test_mentioned_followup_is_a_normal_turn():
    adapter, handled = _adapter()
    asyncio.run(adapter._handle_slack_message(_event("<@UBOT> can you check?")))
    assert len(handled) == 1
    assert handled[0].reply_expected is True
    assert _FOLLOWUP_DECISION_PROMPT not in handled[0].text


def _stream_deltas(reply_expected):
    adapter = SimpleNamespace(SUPPORTS_MESSAGE_EDITING=True)
    ctx = TurnContext(
        streaming_tts_consumer_holder=[None], user_config={}, resolve_display_setting=lambda *a: None,
        interim_assistant_messages_enabled=False, reply_expected=reply_expected,
        source=SimpleNamespace(platform=SimpleNamespace(value="slack"), chat_id="C123"),
        _run_still_current=lambda: True)
    runner = SimpleNamespace(
        config=SimpleNamespace(streaming=StreamingConfig(enabled=True)),
        _delivery_adapter_for=lambda source: adapter,
        _build_stream_consumer_config=lambda *a, **k: (StreamConsumerConfig(), None))
    consumer, delta_cb, _interim, _ = TurnRunner(runner, ctx)._setup_stream_consumer("slack")
    return bool(consumer and consumer.stream_deltas_enabled), delta_cb


@pytest.mark.parametrize("reply_expected", [None, True])
def test_ordinary_turn_streams(reply_expected):
    streams, delta_cb = _stream_deltas(reply_expected)
    assert streams and delta_cb is not None


def test_decision_turn_never_streams():
    streams, delta_cb = _stream_deltas(False)
    assert not streams and delta_cb is None
