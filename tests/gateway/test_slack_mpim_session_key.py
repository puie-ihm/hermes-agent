"""Track 1 regression guard — MPIM (group DM) session-key consistency.

Prod bug: an MPIM (``is_mpim=true``, C/G-prefixed channel) Slack thread was
classified inconsistently by the session-key ``chat_type`` derivation. A plain
``message`` event carries ``channel_type="mpim"`` → the old code's ``is_dm``
(``channel_type in {"im","mpim"}``) made it "dm", while an ``app_mention`` event
carries NO ``channel_type`` and an MPIM channel is C/G-prefixed → ``is_dm`` was
False → "group". The SAME thread therefore split across two session keys
(``agent:main:slack:dm:<chan>:<ts>`` AND ``agent:main:slack:group:<chan>:<ts>``)
and the bot "forgot" mid-thread.

Fix: the session-key ``chat_type`` keys an MPIM as "group" everywhere (only a
genuine 1:1 DM — im + D-prefix = ``is_one_to_one_dm`` — keys as "dm"), matching
the slash-command and ``_has_active_session_for_thread`` paths. The routing /
gating ``is_dm`` (which correctly treats MPIMs as DM-like) is left untouched.

These tests drive the real ``SlackAdapter._handle_slack_message`` path and
assert on the ``MessageEvent.source`` that reaches ``handle_message`` — the exact
object the gateway session store keys on — then feed it through the production
``build_session_key`` to prove one stable key per MPIM thread.
"""
import time
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from gateway.config import PlatformConfig
from gateway.session import build_session_key
from plugins.platforms.slack.adapter import SlackAdapter


@pytest.fixture
def adapter():
    config = PlatformConfig(enabled=True, token="xoxb-fake-token")
    a = SlackAdapter(config)
    a._app = MagicMock()
    a._app.client = AsyncMock()
    a._bot_user_id = "U_BOT"
    a._running = True
    a.handle_message = AsyncMock()
    return a


@pytest.fixture(autouse=True)
def _redirect_cache(tmp_path, monkeypatch):
    monkeypatch.setattr(
        "gateway.platforms.base.DOCUMENT_CACHE_DIR", tmp_path / "doc_cache"
    )


def _fresh_ts() -> str:
    """A recent ts so the fork's 60s staleness guard does not drop the event."""
    return f"{time.time():.6f}"


async def _capture_source(adapter, event) -> object:
    """Drive the real handler and return the MessageEvent.source it built."""
    captured = []
    adapter.handle_message = AsyncMock(side_effect=lambda e: captured.append(e))
    with patch.object(
        adapter, "_resolve_user_name", new=AsyncMock(return_value="testuser")
    ), patch.object(
        adapter, "_set_assistant_thread_title", new=AsyncMock()
    ), patch.object(
        adapter, "_fetch_thread_parent_text", new=AsyncMock(return_value=None)
    ), patch.object(
        # MPIMs are not mention-exempt; disable the mention requirement so both
        # the @mention and the plain reply reach the session-key builder. This
        # isolates the chat_type derivation from routing gating (the thing under
        # test), which is exactly the seam the bug lived on.
        adapter, "_slack_require_mention", return_value=False
    ), patch.object(
        adapter, "_slack_strict_mention", return_value=False
    ), patch.object(
        # Hermetic: neutralize the allowed-channels allowlist so leaked
        # ``SLACK_ALLOWED_CHANNELS`` env from other suites can't drop our
        # events before session keying (this suite's tests are order-robust).
        adapter, "_slack_allowed_channels", return_value=set()
    ):
        await adapter._handle_slack_message(event)
    assert len(captured) == 1, "handler dropped the event before session keying"
    return captured[0].source


# Shared thread root for the MPIM thread the two event types land on.
_MPIM_CHANNEL = "C_MPIM_MPDM"  # group DMs are C/G-prefixed, never D-prefixed
_THREAD_ROOT = "1700000000.100000"


@pytest.mark.asyncio
async def test_mpim_message_and_app_mention_share_one_session_key(adapter):
    """The core Track 1 guarantee: an @mention (app_mention, no channel_type)
    and a plain reply (message, channel_type='mpim') on the SAME MPIM thread
    must produce the SAME session key."""
    # Plain reply in the thread — a `message` event carrying channel_type=mpim.
    message_event = {
        "channel": _MPIM_CHANNEL,
        "channel_type": "mpim",
        "user": "U_USER",
        "text": "and another thing",
        "ts": _fresh_ts(),
        "thread_ts": _THREAD_ROOT,
    }
    # @mention in the same thread — an `app_mention` event with NO channel_type.
    app_mention_event = {
        "channel": _MPIM_CHANNEL,
        "user": "U_USER",
        "text": "<@U_BOT> hey",
        "ts": _fresh_ts(),
        "thread_ts": _THREAD_ROOT,
    }

    src_message = await _capture_source(adapter, message_event)
    src_mention = await _capture_source(adapter, app_mention_event)

    # Both must classify the MPIM thread as a shared "group" surface.
    assert src_message.chat_type == "group", (
        f"MPIM message event keyed as {src_message.chat_type!r}, expected 'group'"
    )
    assert src_mention.chat_type == "group", (
        f"MPIM app_mention keyed as {src_mention.chat_type!r}, expected 'group'"
    )

    key_message = build_session_key(src_message)
    key_mention = build_session_key(src_mention)

    assert key_message == key_mention, (
        "MPIM thread split across two session keys — the bug:\n"
        f"  message     -> {key_message}\n"
        f"  app_mention -> {key_mention}"
    )
    assert ":group:" in key_message and ":dm:" not in key_message, (
        f"MPIM session key must be group-scoped, got {key_message}"
    )


@pytest.mark.asyncio
async def test_one_to_one_dm_still_keys_as_dm(adapter):
    """Genuine 1:1 DMs (im + D-prefix) must be UNCHANGED — still 'dm'."""
    dm_event = {
        "channel": "D_USER123",  # D-prefix == real 1:1 IM
        "channel_type": "im",
        "user": "U_USER",
        "text": "just us",
        "ts": _fresh_ts(),
    }
    src = await _capture_source(adapter, dm_event)
    assert src.chat_type == "dm", (
        f"1:1 DM regressed to {src.chat_type!r}, expected 'dm'"
    )
    assert ":dm:" in build_session_key(src)


@pytest.mark.asyncio
async def test_regular_channel_still_keys_as_group(adapter):
    """Regular channels must be UNCHANGED — still 'group'."""
    chan_event = {
        "channel": "C_CHAN",
        "channel_type": "channel",
        "user": "U_USER",
        "text": "<@U_BOT> hello team",
        "ts": _fresh_ts(),
    }
    src = await _capture_source(adapter, chan_event)
    assert src.chat_type == "group", (
        f"channel regressed to {src.chat_type!r}, expected 'group'"
    )
    assert ":group:" in build_session_key(src)
