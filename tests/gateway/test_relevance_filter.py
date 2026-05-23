"""Tests for the cross-platform LLM-based relevance filter.

The filter gates ``BasePlatformAdapter.handle_message()`` so the bot
doesn't reply to chatter between humans when it happens to be sitting
in an "active thread" (Slack reply_to_bot_thread / Telegram group
follow-up / Discord channel-prompt chat).

We don't want these tests to hit a real LLM, so the classifier is
monkey-patched per-test.  The bypass-rule tests run without any
classifier invocation and verify that DMs / slash commands / replies
/ short text are handled by cheap checks.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from gateway.config import Platform
from gateway.platforms.base import MessageEvent, MessageType
from gateway.session import SessionSource
import gateway.relevance_filter as rf


# -----------------------------------------------------------------------------
# Helpers
# -----------------------------------------------------------------------------


def _make_event(
    *,
    text: str = "what's the deploy status?",
    chat_type: str = "channel",
    platform: Platform = Platform.SLACK,
    message_id: str = "1700000000.000100",
    reply_to_text: str | None = None,
    channel_context: str | None = None,
    internal: bool = False,
    user_name: str = "alice",
) -> MessageEvent:
    source = SessionSource(
        platform=platform,
        chat_id="C0ABCDEF",
        chat_name="general",
        chat_type=chat_type,
        user_id="U123",
        user_name=user_name,
        thread_id="1700000000.000050" if chat_type != "dm" else None,
    )
    return MessageEvent(
        text=text,
        message_type=MessageType.TEXT,
        source=source,
        raw_message={},
        message_id=message_id,
        reply_to_text=reply_to_text,
        channel_context=channel_context,
        internal=internal,
    )


@pytest.fixture(autouse=True)
def _reset_filter_state(monkeypatch):
    """Each test starts from a clean module-state slate."""
    rf._reset_for_tests()
    # Clear any HERMES_RELEVANCE_FILTER_* env vars that might leak from
    # the host environment.
    for var in (
        "HERMES_RELEVANCE_FILTER_ENABLED",
        "HERMES_RELEVANCE_FILTER_MODEL",
        "HERMES_RELEVANCE_FILTER_MIN_LEN",
        "HERMES_RELEVANCE_FILTER_BYPASS_PLATFORMS",
        "HERMES_RELEVANCE_FILTER_LOG",
    ):
        monkeypatch.delenv(var, raising=False)
    yield
    rf._reset_for_tests()


# -----------------------------------------------------------------------------
# Disabled-by-default behavior
# -----------------------------------------------------------------------------


def test_filter_disabled_by_default_passes_everything():
    """With no configuration, every message is RELEVANT (preserves status quo)."""
    cfg = rf.RelevanceFilterConfig()
    assert cfg.enabled is False

    event = _make_event(text="lol same")
    assert asyncio.run(rf.should_respond(event, config=cfg)) is True


# -----------------------------------------------------------------------------
# Bypass rules — these MUST never invoke the LLM
# -----------------------------------------------------------------------------


def test_dm_bypasses_classifier():
    cfg = rf.RelevanceFilterConfig(enabled=True)
    event = _make_event(chat_type="dm", text="hey")

    with patch.object(rf, "_classify", new=AsyncMock(side_effect=AssertionError(
        "DMs must never reach the classifier"
    ))):
        assert asyncio.run(rf.should_respond(event, config=cfg)) is True


def test_slash_command_bypasses_classifier():
    cfg = rf.RelevanceFilterConfig(enabled=True)
    event = _make_event(text="/reset", chat_type="channel")

    with patch.object(rf, "_classify", new=AsyncMock(side_effect=AssertionError(
        "Slash commands must never reach the classifier"
    ))):
        assert asyncio.run(rf.should_respond(event, config=cfg)) is True


def test_reply_to_text_does_not_bypass_classifier():
    """Thread replies must go through the LLM.

    Slack sets ``reply_to_text`` for *every* message in a thread (the
    thread parent), even when the user is addressing another human.  We
    cannot treat its presence as "directed at the bot" — the classifier
    has to decide.
    """
    cfg = rf.RelevanceFilterConfig(enabled=True)
    event = _make_event(
        text="Hey John, can you help check?",
        chat_type="channel",
        reply_to_text="do you see what I write before?",
    )
    with patch.object(rf, "_classify", new=AsyncMock(return_value=False)) as classify:
        assert asyncio.run(rf.should_respond(event, config=cfg)) is False
        classify.assert_awaited_once()


def test_internal_event_bypasses_classifier():
    cfg = rf.RelevanceFilterConfig(enabled=True)
    event = _make_event(text="background job done", internal=True)
    with patch.object(rf, "_classify", new=AsyncMock(side_effect=AssertionError(
        "Internal events must never reach the classifier"
    ))):
        assert asyncio.run(rf.should_respond(event, config=cfg)) is True


def test_bypass_platforms_skips_classifier():
    cfg = rf.RelevanceFilterConfig(
        enabled=True, bypass_platforms=["slack"]
    )
    event = _make_event(platform=Platform.SLACK, chat_type="channel")
    with patch.object(rf, "_classify", new=AsyncMock(side_effect=AssertionError(
        "Bypassed platforms must not invoke the classifier"
    ))):
        assert asyncio.run(rf.should_respond(event, config=cfg)) is True


def test_short_text_dropped_without_classifier():
    """Reactions / acknowledgements drop without LLM cost."""
    cfg = rf.RelevanceFilterConfig(enabled=True, min_text_length=3)
    event = _make_event(text="ok")
    with patch.object(rf, "_classify", new=AsyncMock(side_effect=AssertionError(
        "Short text must be dropped before the classifier"
    ))):
        assert asyncio.run(rf.should_respond(event, config=cfg)) is False


def test_empty_text_dropped_without_classifier():
    cfg = rf.RelevanceFilterConfig(enabled=True)
    event = _make_event(text="   ")
    with patch.object(rf, "_classify", new=AsyncMock(side_effect=AssertionError(
        "Empty text must be dropped before the classifier"
    ))):
        assert asyncio.run(rf.should_respond(event, config=cfg)) is False


# -----------------------------------------------------------------------------
# Classifier path — RELEVANT / IGNORE
# -----------------------------------------------------------------------------


def test_classifier_relevant_yields_true():
    cfg = rf.RelevanceFilterConfig(enabled=True)
    event = _make_event(text="@bot what's the deploy status?")
    with patch.object(rf, "_classify", new=AsyncMock(return_value=True)) as classify:
        assert asyncio.run(rf.should_respond(event, config=cfg)) is True
        classify.assert_awaited_once()


def test_classifier_ignore_yields_false():
    cfg = rf.RelevanceFilterConfig(enabled=True)
    event = _make_event(text="alice, want to grab lunch in 10?")
    with patch.object(rf, "_classify", new=AsyncMock(return_value=False)) as classify:
        assert asyncio.run(rf.should_respond(event, config=cfg)) is False
        classify.assert_awaited_once()


def test_classifier_exception_fails_open():
    """Any classifier error → respond (we never silently drop)."""
    cfg = rf.RelevanceFilterConfig(enabled=True)
    event = _make_event(text="a question that needs an answer")
    with patch.object(
        rf,
        "_classify",
        new=AsyncMock(side_effect=RuntimeError("LLM blew up")),
    ):
        assert asyncio.run(rf.should_respond(event, config=cfg)) is True


def test_classifier_timeout_fails_open():
    cfg = rf.RelevanceFilterConfig(enabled=True)
    event = _make_event(text="something needs doing")
    with patch.object(
        rf,
        "_classify",
        new=AsyncMock(side_effect=asyncio.TimeoutError()),
    ):
        assert asyncio.run(rf.should_respond(event, config=cfg)) is True


# -----------------------------------------------------------------------------
# Decision cache
# -----------------------------------------------------------------------------


def test_decision_cache_avoids_second_classifier_call():
    """Re-delivered webhooks (same message_id) must not re-spend tokens."""
    cfg = rf.RelevanceFilterConfig(enabled=True)
    event = _make_event(message_id="WEBHOOK-RETRY-1")

    classify_mock = AsyncMock(return_value=True)
    with patch.object(rf, "_classify", new=classify_mock):
        assert asyncio.run(rf.should_respond(event, config=cfg)) is True
        assert asyncio.run(rf.should_respond(event, config=cfg)) is True
        assert classify_mock.await_count == 1


# -----------------------------------------------------------------------------
# Configuration: env + dict round-trip
# -----------------------------------------------------------------------------


def test_from_env_overlays_existing_config(monkeypatch):
    monkeypatch.setenv("HERMES_RELEVANCE_FILTER_ENABLED", "true")
    monkeypatch.setenv("HERMES_RELEVANCE_FILTER_MIN_LEN", "5")
    monkeypatch.setenv(
        "HERMES_RELEVANCE_FILTER_BYPASS_PLATFORMS",
        "telegram, discord",
    )
    cfg = rf.RelevanceFilterConfig.from_env()
    assert cfg.enabled is True
    assert cfg.min_text_length == 5
    assert cfg.bypass_platforms == ["telegram", "discord"]


def test_from_dict_round_trip():
    payload = {
        "enabled": True,
        "model": "openai/gpt-4.1-mini",
        "min_text_length": 4,
        "bypass_platforms": ["matrix"],
        "log_decisions": False,
    }
    cfg = rf.RelevanceFilterConfig.from_dict(payload)
    assert cfg.enabled is True
    assert cfg.model == "openai/gpt-4.1-mini"
    assert cfg.min_text_length == 4
    assert cfg.bypass_platforms == ["matrix"]
    assert cfg.log_decisions is False
    assert cfg.to_dict() == payload


def test_from_dict_handles_csv_bypass_platforms():
    cfg = rf.RelevanceFilterConfig.from_dict({
        "enabled": True,
        "bypass_platforms": "slack, telegram",
    })
    assert cfg.bypass_platforms == ["slack", "telegram"]


def test_from_dict_with_empty_data_returns_default():
    cfg = rf.RelevanceFilterConfig.from_dict(None)
    assert cfg.enabled is False
    assert cfg.bypass_platforms == []


def test_is_bypassed_case_insensitive():
    cfg = rf.RelevanceFilterConfig(
        enabled=True, bypass_platforms=["Slack", "DISCORD"],
    )
    assert cfg.is_bypassed("slack") is True
    assert cfg.is_bypassed("SLACK") is True
    assert cfg.is_bypassed("matrix") is False


# -----------------------------------------------------------------------------
# _classify integration — fake auxiliary client wiring
# -----------------------------------------------------------------------------


class _FakeChoice:
    def __init__(self, content: str):
        self.message = SimpleNamespace(content=content)


class _FakeResponse:
    def __init__(self, content: str):
        self.choices = [_FakeChoice(content)]


class _FakeCompletions:
    def __init__(self, content: str):
        self._content = content
        self.calls: list[dict] = []

    async def create(self, **kwargs):
        self.calls.append(kwargs)
        return _FakeResponse(self._content)


class _FakeChat:
    def __init__(self, completions):
        self.completions = completions


class _FakeAsyncClient:
    def __init__(self, content: str):
        self._completions = _FakeCompletions(content)
        self.chat = _FakeChat(self._completions)


def test_classify_routes_to_auxiliary_client_and_parses_ignore():
    """_classify should call the aux client and treat 'IGNORE' as False."""
    cfg = rf.RelevanceFilterConfig(enabled=True, model="claude-haiku-4-5")
    event = _make_event(text="bob, lunch in 10?")
    client = _FakeAsyncClient("IGNORE")

    with patch(
        "agent.auxiliary_client.get_async_text_auxiliary_client",
        return_value=(client, "claude-haiku-4-5"),
    ):
        decision = asyncio.run(
            rf._classify(event, cfg, bot_name="hermes", platform_name="slack")
        )

    assert decision is False
    assert client._completions.calls, "aux client must be invoked"
    sent_model = client._completions.calls[0]["model"]
    assert sent_model == "claude-haiku-4-5"


def test_classify_treats_relevant_response_as_true():
    cfg = rf.RelevanceFilterConfig(enabled=True)
    event = _make_event(text="what's the deploy status?")
    client = _FakeAsyncClient("RELEVANT")

    with patch(
        "agent.auxiliary_client.get_async_text_auxiliary_client",
        return_value=(client, "claude-haiku-4-5"),
    ):
        decision = asyncio.run(
            rf._classify(event, cfg, bot_name="hermes", platform_name="slack")
        )
    assert decision is True


def test_classify_prompt_includes_reply_parent_when_present():
    """The thread parent must reach the LLM so it can distinguish
    bot-thread continuation from human-to-human chatter."""
    cfg = rf.RelevanceFilterConfig(enabled=True)
    event = _make_event(
        text="Hey John, can you help check?",
        chat_type="channel",
        reply_to_text="do you see what I write before?",
    )
    client = _FakeAsyncClient("IGNORE")

    with patch(
        "agent.auxiliary_client.get_async_text_auxiliary_client",
        return_value=(client, "claude-haiku-4-5"),
    ):
        asyncio.run(
            rf._classify(event, cfg, bot_name="hermes", platform_name="slack")
        )

    assert client._completions.calls, "aux client must be invoked"
    user_prompt = client._completions.calls[0]["messages"][1]["content"]
    assert "do you see what I write before?" in user_prompt
    assert "Hey John, can you help check?" in user_prompt


def test_classify_prompt_omits_reply_block_when_no_parent():
    cfg = rf.RelevanceFilterConfig(enabled=True)
    event = _make_event(text="what's the status?", reply_to_text=None)
    client = _FakeAsyncClient("RELEVANT")

    with patch(
        "agent.auxiliary_client.get_async_text_auxiliary_client",
        return_value=(client, "claude-haiku-4-5"),
    ):
        asyncio.run(
            rf._classify(event, cfg, bot_name="hermes", platform_name="slack")
        )

    user_prompt = client._completions.calls[0]["messages"][1]["content"]
    assert "parent of the thread" not in user_prompt


def test_classify_no_aux_client_fails_open():
    cfg = rf.RelevanceFilterConfig(enabled=True)
    event = _make_event()

    with patch(
        "agent.auxiliary_client.get_async_text_auxiliary_client",
        return_value=(None, None),
    ):
        decision = asyncio.run(
            rf._classify(event, cfg, bot_name="hermes", platform_name="slack")
        )
    assert decision is True


def test_classify_unexpected_response_fails_open():
    """Unparseable LLM output → fail open (RELEVANT)."""
    cfg = rf.RelevanceFilterConfig(enabled=True)
    event = _make_event()
    client = _FakeAsyncClient("um, maybe?")

    with patch(
        "agent.auxiliary_client.get_async_text_auxiliary_client",
        return_value=(client, "claude-haiku-4-5"),
    ):
        decision = asyncio.run(
            rf._classify(event, cfg, bot_name="hermes", platform_name="slack")
        )
    assert decision is True
