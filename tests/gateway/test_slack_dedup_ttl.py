"""
Tests for Slack Socket Mode dedup TTL (#4777).

Slack replays un-acked Socket Mode events when the websocket reconnects.
The replay can land several minutes after the original; the dedup window
must outlast that gap so the redelivered event is suppressed instead of
producing a second bot reply. Regression for the 300s-default bug where
replays >5 min later slipped through.

Follows the slack-bolt mocking pattern from test_slack_mention.py.
"""

import os
import sys
import time
from unittest.mock import MagicMock, patch


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

import plugins.platforms.slack.adapter as _slack_mod

_slack_mod.SLACK_AVAILABLE = True

from gateway.platforms.helpers import MessageDeduplicator
from plugins.platforms.slack.adapter import _slack_dedup_ttl_seconds


def test_default_ttl_outlasts_slack_reconnect_redelivery_window():
    # The whole point of the fix: the window must be much longer than the
    # ~6 min reconnect-redelivery gap that caused the duplicate reply.
    with patch.dict(os.environ, {}, clear=True):
        assert _slack_dedup_ttl_seconds() >= 1800.0


def test_default_ttl_is_a_full_day():
    """Fork semantics: a day, not an hour.

    Slack retains un-acked Socket Mode envelopes for 24h, so a replay can land
    hours after the original. An hour-long window let those through.
    """
    with patch.dict(os.environ, {}, clear=True):
        assert _slack_dedup_ttl_seconds() == 24 * 60 * 60.0


def test_env_override_is_respected():
    with patch.dict(os.environ, {"SLACK_DEDUP_TTL_SECONDS": "120"}, clear=True):
        assert _slack_dedup_ttl_seconds() == 120.0


def test_adapter_bounds_memory_by_max_size_not_ttl():
    """A 24h window must not mean an unbounded cache: max_size caps entries.

    Pins the fork's ``MessageDeduplicator(max_size=10000, ttl_seconds=24h)``
    construction on the real adapter — if the TTL is raised without max_size, a
    busy workspace leaks a day of message ts into the dict.
    """
    from gateway.config import Platform, PlatformConfig
    from plugins.platforms.slack.adapter import SlackAdapter

    adapter = SlackAdapter(
        PlatformConfig(
            enabled=True,
            extra={"bot_token": "xoxb-test-token", "app_token": "xapp-test-token"},
        )
    )
    assert adapter._dedup._max_size == 10000
    assert adapter._dedup._ttl == 24 * 60 * 60.0
    assert adapter.platform == Platform.SLACK


