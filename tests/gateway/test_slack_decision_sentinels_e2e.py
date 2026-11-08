"""End-to-end coverage for the agent-decision sentinel path.

``slack.thread_followup_mode=agent`` lets the agent answer a non-mention
follow-up with a control token instead of prose:

    [[silent]]        → send nothing
    [[react:emoji]]   → send nothing, add one emoji reaction to the trigger

Two prod regressions motivate these tests, both of which the source-inspection
guards in test_slack_thread_followup.py cannot catch:

  1. A literal ``[[silent]]`` and a literal ``[[react:eyes]]`` were posted into
     the channel as visible text (2026-05-29, and again via the queued-follow-up
     resend while streaming was disabled). The parser must run on the real
     egress path and must actually return None — not merely be referenced.
  2. The token must fire the SLACK reaction, through the adapter's own
     ``_apply_decision_reaction`` → ``_add_reaction``, not just disappear.

So these tests execute the real methods instead of grepping their source:
  * the shared filter on a real ``SlackAdapter`` (token grammar + side effect),
  * the chokepoint in ``BasePlatformAdapter._process_message_background``
    (nothing reaches ``send``),
  * Slack's reaction call (the emoji that would hit ``reactions.add``).
"""

import sys
from unittest.mock import AsyncMock, MagicMock

import pytest

from gateway.config import Platform, PlatformConfig
from gateway.platforms.base import BasePlatformAdapter, SendResult
from gateway.platforms.event import MessageEvent
from gateway.session import SessionSource, build_session_key


# Mock slack-bolt if not installed (same bootstrap as the other Slack suites).
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


def _slack_adapter() -> SlackAdapter:
    adapter = SlackAdapter(
        PlatformConfig(
            enabled=True,
            extra={"bot_token": "xoxb-test-token", "app_token": "xapp-test-token"},
        )
    )
    adapter.platform = Platform.SLACK
    adapter._add_reaction = AsyncMock(return_value=True)
    return adapter


def _event(message_id: str = "1751999999.000100", chat_id: str = "C123") -> MessageEvent:
    return MessageEvent(
        text="follow-up in a thread",
        source=SessionSource(
            platform=Platform.SLACK,
            chat_id=chat_id,
            chat_type="group",
            user_id="U_USER",
            thread_id="1751999999.000001",
        ),
        message_id=message_id,
    )


# ---------------------------------------------------------------------------
# 1. The filter itself, executed on a real SlackAdapter
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_silent_returns_none_and_fires_no_reaction():
    adapter = _slack_adapter()
    assert await adapter._consume_decision_sentinels("[[silent]]", event=_event()) is None
    adapter._add_reaction.assert_not_awaited()


@pytest.mark.asyncio
async def test_react_returns_none_and_reaches_slack_with_the_emoji():
    """The whole reply is the token → reply suppressed, reaction added on Slack."""
    adapter = _slack_adapter()
    result = await adapter._consume_decision_sentinels("[[react:eyes]]", event=_event("1751.42", "C999"))

    assert result is None
    adapter._add_reaction.assert_awaited_once()
    channel_id, ts, emoji = adapter._add_reaction.await_args.args
    assert (channel_id, ts, emoji) == ("C999", "1751.42", "eyes")


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "text,expected_emoji",
    [
        ("[[react:eyes]]", "eyes"),
        ("[[react:+1]]", "+1"),
        ("[[react::thumbsup:]]", "thumbsup"),        # model wrapped the emoji in colons
        ("[[react: white_check_mark ]]", "white_check_mark"),  # stray spaces
        ("[[react::white_check_mark:]]", "white_check_mark"),
        ("  [[react:eyes]]\n", "eyes"),
    ],
)
async def test_react_token_grammar(text, expected_emoji):
    adapter = _slack_adapter()
    assert await adapter._consume_decision_sentinels(text, event=_event()) is None
    assert adapter._add_reaction.await_args.args[2] == expected_emoji


@pytest.mark.asyncio
async def test_react_token_inside_prose_still_suppresses_the_text():
    """Documented semantics: a react token ANYWHERE means react-only.

    The token is a reserved control token, so prose shipped alongside it must
    not be posted — otherwise the model's "draft + decision token" shape leaks.
    """
    adapter = _slack_adapter()
    response = "Sure, I'll take that — pinging Ben now.\n\n[[react:eyes]]"

    assert await adapter._consume_decision_sentinels(response, event=_event()) is None
    assert adapter._add_reaction.await_args.args[2] == "eyes"


@pytest.mark.asyncio
async def test_plain_reply_passes_through_untouched():
    adapter = _slack_adapter()
    text = "Here is the answer, no control tokens here."
    assert await adapter._consume_decision_sentinels(text, event=_event()) == text
    adapter._add_reaction.assert_not_awaited()


@pytest.mark.asyncio
async def test_non_string_response_is_returned_as_is():
    """Ephemeral/tuple and None responses must never be treated as text."""
    adapter = _slack_adapter()
    marker = object()
    assert await adapter._consume_decision_sentinels(marker, event=_event()) is marker
    assert await adapter._consume_decision_sentinels(None, event=_event()) is None


@pytest.mark.asyncio
async def test_resend_path_synthetic_event_carries_chat_and_message_id():
    """The queued-follow-up resend has only a SessionSource — pass chat_id+message_id."""
    adapter = _slack_adapter()
    assert await adapter._consume_decision_sentinels(
        "[[react:eyes]]", chat_id="C777", message_id="1752.99") is None
    channel_id, ts, emoji = adapter._add_reaction.await_args.args
    assert (channel_id, ts, emoji) == ("C777", "1752.99", "eyes")


@pytest.mark.asyncio
async def test_reaction_failure_does_not_eat_the_turn():
    adapter = _slack_adapter()
    adapter._add_reaction = AsyncMock(side_effect=RuntimeError("slack down"))
    assert await adapter._consume_decision_sentinels("[[react:eyes]]", event=_event()) is None


# ---------------------------------------------------------------------------
# 2. The chokepoint, executed — nothing reaches send()
# ---------------------------------------------------------------------------

class _StubAdapter(BasePlatformAdapter):
    """Minimal concrete adapter; records what would have been sent."""

    def __init__(self, reaction_emoji: list):
        super().__init__(PlatformConfig(enabled=True, token="fake"), Platform.DISCORD)
        self.sent = []
        self._reaction_emoji = reaction_emoji

    async def connect(self, *, is_reconnect: bool = False):
        return True

    async def disconnect(self):
        pass

    async def send(self, chat_id, content, reply_to=None, metadata=None):
        self.sent.append(content)
        return SendResult(success=True, message_id="msg1")

    async def send_typing(self, chat_id, metadata=None):
        pass

    async def get_chat_info(self, chat_id):
        return {"id": chat_id}

    async def _apply_decision_reaction(self, event, emoji):  # noqa: D401 - base hook
        self._reaction_emoji.append(emoji)
        return True


def _stub_event() -> MessageEvent:
    return MessageEvent(
        text="follow-up in a thread",
        source=SessionSource(
            platform=Platform.DISCORD, chat_id="c1", chat_type="group", user_id="u1"),
        message_id="m1",
    )


async def _run_turn(response: str):
    reactions: list[str] = []
    adapter = _StubAdapter(reactions)

    async def handler(event):
        return response

    adapter.set_message_handler(handler)
    event = _stub_event()
    await adapter._process_message_background(event, build_session_key(event.source))
    await adapter.cancel_background_tasks()
    return adapter.sent, reactions


@pytest.mark.asyncio
async def test_chokepoint_suppresses_a_silent_turn_end_to_end():
    sent, reactions = await _run_turn("[[silent]]")
    assert sent == [], f"[[silent]] turn still sent: {sent}"
    assert reactions == []


@pytest.mark.asyncio
async def test_chokepoint_suppresses_a_react_turn_end_to_end():
    sent, reactions = await _run_turn("[[react:eyes]]")
    assert sent == [], f"[[react:eyes]] turn still sent: {sent}"
    assert reactions == ["eyes"]


@pytest.mark.asyncio
async def test_chokepoint_still_sends_a_plain_turn():
    sent, reactions = await _run_turn("plain answer")
    assert sent == ["plain answer"]
    assert reactions == []
