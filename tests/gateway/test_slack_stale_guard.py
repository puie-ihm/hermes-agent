"""The stale-event guard must not drop messages queued across a restart.

Measured 2026-09-20: the gateway restarted at 13:16:36 and Slack reconnected at
13:18:11 -- 95s of startup (channel directory build for 847 targets). Four real
messages posted in that window were delivered at 13:18:49 with a Slack-side ts of
67-81s old, and the guard dropped all four: no reply, no error, nothing in the
channel to explain it. A restart always produces that backlog, and the guard was
on by default.
"""

import sys
import time
from unittest.mock import MagicMock

import pytest

sys.path.insert(0, ".")

from gateway.config import Platform, PlatformConfig  # noqa: E402


def _ensure_slack_mock():
    if "slack_bolt" in sys.modules and hasattr(sys.modules["slack_bolt"], "__file__"):
        return
    slack_bolt = MagicMock()
    slack_bolt.async_app.AsyncApp = MagicMock
    slack_bolt.adapter.socket_mode.async_handler.AsyncSocketModeHandler = MagicMock
    slack_sdk = MagicMock()
    slack_sdk.web.async_client.AsyncWebClient = MagicMock
    for name, mod in [
        ("slack_bolt", slack_bolt), ("slack_bolt.async_app", slack_bolt.async_app),
        ("slack_bolt.adapter", slack_bolt.adapter),
        ("slack_bolt.adapter.socket_mode", slack_bolt.adapter.socket_mode),
        ("slack_bolt.adapter.socket_mode.async_handler",
         slack_bolt.adapter.socket_mode.async_handler),
        ("slack_sdk", slack_sdk), ("slack_sdk.web", slack_sdk.web),
        ("slack_sdk.web.async_client", slack_sdk.web.async_client),
    ]:
        sys.modules.setdefault(name, mod)


_ensure_slack_mock()

from plugins.platforms.slack.adapter import SlackAdapter  # noqa: E402


def _adapter():
    a = object.__new__(SlackAdapter)
    a.platform = Platform.SLACK
    a.config = PlatformConfig(enabled=True, extra={})
    return a


def test_default_is_off_so_a_restart_backlog_is_processed(monkeypatch):
    """The exact 2026-09-20 case: an 81s-old message after a restart is handled."""
    monkeypatch.delenv("SLACK_STALE_EVENT_TTL_SECONDS", raising=False)
    old_ts = f"{time.time() - 81:.6f}"
    assert _adapter()._drop_stale_event(old_ts) is False


def test_opt_in_drops_old_events(monkeypatch):
    monkeypatch.setenv("SLACK_STALE_EVENT_TTL_SECONDS", "60")
    assert _adapter()._drop_stale_event(f"{time.time() - 81:.6f}") is True


def test_opt_in_keeps_fresh_events(monkeypatch):
    monkeypatch.setenv("SLACK_STALE_EVENT_TTL_SECONDS", "60")
    assert _adapter()._drop_stale_event(f"{time.time() - 5:.6f}") is False


def test_missing_or_invalid_ts_never_drops(monkeypatch):
    monkeypatch.setenv("SLACK_STALE_EVENT_TTL_SECONDS", "60")
    adapter = _adapter()
    assert adapter._drop_stale_event("") is False
    assert adapter._drop_stale_event(None) is False
    assert adapter._drop_stale_event("not-a-number") is False


def test_invalid_ttl_falls_back_to_off(monkeypatch):
    monkeypatch.setenv("SLACK_STALE_EVENT_TTL_SECONDS", "sixty")
    assert _adapter()._drop_stale_event(f"{time.time() - 600:.6f}") is False
