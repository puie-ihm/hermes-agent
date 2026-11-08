"""Slack Socket Mode listeners must ack the envelope BEFORE doing the work.

slack-bolt sends the WebSocket envelope ack to Slack when the listener calls
``await ack()``; if the listener never calls it, bolt waits until the listener
returns. Hermes' event listener runs the relevance filter, media downloads and
the entire agent turn before returning, so an acked-last envelope can sit
unacked for minutes. Slack then treats it as undelivered and redelivers it
later (the same-ts replay the 24h MessageDeduplicator has to absorb anyway) —
and during a storm of slow turns the un-acked rate can cross Slack's 95%/60min
threshold and get the app's Event Subscriptions auto-disabled.

Fix under test: ``_register_bolt_handlers`` wraps every event listener so it
acks first, then dispatches.

Mock pattern mirrors test_slack_mention.py / test_slack_dedup_ttl.py.
"""

import sys
from unittest.mock import AsyncMock, MagicMock

import pytest

from gateway.config import Platform, PlatformConfig


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


class _RecordingApp:
    """Minimal stand-in for ``AsyncApp`` that records registered listeners."""

    def __init__(self):
        self.events: dict = {}
        self.commands: list = []
        self.actions: list = []

    # slack_bolt decorator form: app.event(type)(listener)
    def event(self, event_type):
        def _register(listener):
            self.events[event_type] = listener
            return listener

        return _register

    def command(self, pattern):
        def _register(listener):
            self.commands.append((pattern, listener))
            return listener

        return _register

    def action(self, action_id):
        def _register(listener):
            self.actions.append((action_id, listener))
            return listener

        return _register


@pytest.fixture
def adapter():
    a = SlackAdapter(
        PlatformConfig(
            enabled=True,
            extra={"bot_token": "xoxb-test-token", "app_token": "xapp-test-token"},
        )
    )
    a.platform = Platform.SLACK
    a._app = _RecordingApp()
    return a


def _listener_for(adapter, event_type):
    adapter._register_bolt_handlers()
    listener = adapter._app.events.get(event_type)
    assert listener is not None, f"no listener registered for {event_type}"
    return listener


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "event_type,handler_attr",
    [
        ("message", "_handle_slack_message"),
        ("app_mention", "_handle_slack_message"),
        ("app_home_opened", "_handle_app_home_opened"),
        ("reaction_added", "_handle_slack_reaction"),
    ],
)
async def test_envelope_is_acked_before_the_handler_runs(adapter, event_type, handler_attr):
    """ack() must already have completed when the (slow) handler starts."""
    order: list[str] = []

    async def _record_ack(*args, **kwargs):
        order.append("ack")

    async def _slow_handler(*args, **kwargs):
        order.append("handler")

    ack = AsyncMock(side_effect=_record_ack)
    setattr(adapter, handler_attr, AsyncMock(side_effect=_slow_handler))

    listener = _listener_for(adapter, event_type)
    await listener(event={"type": event_type}, say=MagicMock(), body={}, ack=ack)

    assert order == ["ack", "handler"], f"{event_type} listener order was {order}"
    assert ack.await_count == 1


@pytest.mark.asyncio
async def test_ack_is_sent_even_when_the_handler_raises(adapter):
    """A failing turn must not leave the envelope unacked (Slack would redeliver it)."""
    ack = AsyncMock()
    adapter._handle_slack_message = AsyncMock(side_effect=RuntimeError("agent blew up"))

    listener = _listener_for(adapter, "message")
    with pytest.raises(RuntimeError):
        await listener(event={"type": "message"}, say=MagicMock(), body={}, ack=ack)

    ack.assert_awaited_once()


@pytest.mark.asyncio
async def test_noop_listeners_are_still_acked(adapter):
    """file_created/file_change exist only to keep Slack from 404-ing them — ack them too."""
    ack = AsyncMock()
    listener = _listener_for(adapter, "file_created")
    await listener(event={"type": "file_created"}, say=MagicMock(), body={}, ack=ack)
    ack.assert_awaited_once()
