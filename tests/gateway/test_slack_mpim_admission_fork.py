"""Pinned Slack admission matrix — an MPIM is a GROUP surface on this branch.

Fork semantics (operator decision, 2026-11-09): running the characterization against live
(``382bbee748``) and this branch (``bca854432c``) with a prod-shaped env yields an identical
verdict on every surface:

    surface                chat_type   admit (U_DM, U_CHAN, U_OUT)
    1:1 DM dmessage (D1)   dm          T, F, F
    MPIM message   (G1)    group       T, T, F
    MPIM @mention  (G1)    group       T, T, F
    channel message(C1)    group       T, T, F
    channel @mention (C1)  group       T, T, F

``SLACK_ALLOWED_USERS=U_DM,U_CHAN`` / ``SLACK_DM_ALLOWED_USERS=U_DM`` — the DM list is a strict
subset, which is what makes the matrix discriminating: an MPIM that fell onto the DM allowlist
would deny ``U_CHAN``.

Composed of the real ``SlackAdapter._handle_slack_message`` (so ``source.chat_type`` is whatever
the adapter actually emits) and the real ``GatewayRunner._is_user_authorized`` (the gate that
decides whether that source may drive the bot). No re-implementation of either decision.
"""

import inspect
import os
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from gateway.config import GatewayConfig, Platform, PlatformConfig
from gateway.session import SessionSource
from plugins.platforms.slack.adapter import SlackAdapter

USERS = ("U_DM", "U_CHAN", "U_OUT")

# (case id, channel id, inbound event fields, expected chat_type, expected admit per USERS)
SURFACES = [
    ("dm_message", "D1", {"type": "message", "channel_type": "im"}, "dm", (True, False, False)),
    ("mpim_message", "G1", {"type": "message", "channel_type": "mpim"}, "group", (True, True, False)),
    ("mpim_app_mention", "G1", {"type": "app_mention"}, "group", (True, True, False)),
    ("channel_message", "C1", {"type": "message", "channel_type": "channel"}, "group", (True, True, False)),
    ("channel_app_mention", "C1", {"type": "app_mention"}, "group", (True, True, False)),
]


@pytest.fixture(autouse=True)
def _prod_shaped_allowlists(monkeypatch):
    """Prod shape: a platform-wide list plus a DM list that is a strict subset of it."""
    for var in list(os.environ):
        if var.startswith(("SLACK_", "GATEWAY_ALLOW")):
            monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("SLACK_ALLOWED_USERS", "U_DM,U_CHAN")
    monkeypatch.setenv("SLACK_DM_ALLOWED_USERS", "U_DM")


def _adapter() -> SlackAdapter:
    adapter = SlackAdapter(PlatformConfig(enabled=True, token="xoxb-fake"))
    adapter._app = MagicMock()
    adapter._app.client = AsyncMock()
    adapter._app.client.conversations_replies = AsyncMock(return_value={"messages": []})
    adapter._bot_user_id = "U_BOT"
    adapter._running = True
    adapter.handle_message = AsyncMock()
    return adapter


async def _emitted_source(event: dict):
    """Drive the real inbound handler and return the ``MessageEvent.source`` it emitted."""
    adapter = _adapter()
    emitted = []
    adapter.handle_message = AsyncMock(side_effect=lambda evt: emitted.append(evt))
    patches = [
        patch.object(adapter, name, new=AsyncMock(
            return_value="u" if name == "_resolve_user_name" else None))
        for name in ("_resolve_user_name", "_set_assistant_thread_title", "_fetch_thread_parent_text")
        if hasattr(adapter, name)
    ]
    # Inbound gating knobs are config-derived; force them off so the message reaches handle_message.
    for name, value in (
        ("_slack_require_mention", False),
        ("_slack_strict_mention", False),
        ("_slack_allowed_channels", set()),
    ):
        if hasattr(adapter, name):
            patches.append(patch.object(adapter, name, return_value=value))
    for p in patches:
        p.start()
    try:
        await adapter._handle_slack_message(event)
    finally:
        for p in patches:
            p.stop()
    assert emitted, f"adapter dropped the event instead of emitting a MessageEvent: {event}"
    return emitted[0].source


def _runner():
    """A real ``GatewayRunner`` with only the unrelated plumbing doubled."""
    from gateway.run import GatewayRunner

    runner = object.__new__(GatewayRunner)
    runner.config = GatewayConfig(platforms={Platform.SLACK: PlatformConfig(enabled=True)})
    runner.adapters = {Platform.SLACK: SimpleNamespace(send=AsyncMock())}
    runner.pairing_store = MagicMock()
    runner.pairing_store.is_approved.return_value = False
    runner.pairing_store._is_rate_limited.return_value = False
    return runner


def _admitted(runner, chat_type: str, chat_id: str, user_id: str) -> bool:
    source = SessionSource(
        platform=Platform.SLACK, user_id=user_id, chat_id=chat_id,
        user_name="t", chat_type=chat_type)
    verdict = runner._is_user_authorized(source)
    return bool(verdict) if not inspect.isawaitable(verdict) else False


@pytest.mark.asyncio
@pytest.mark.parametrize("case,channel,fields,chat_type,admit", SURFACES, ids=[s[0] for s in SURFACES])
async def test_admission_matrix(case, channel, fields, chat_type, admit):
    """The adapter's chat_type for the surface and the gate's verdict for every allowlist tier."""
    event = dict(fields, channel=channel, user="U_DM", text="<@U_BOT> hi", ts=f"{time.time():.6f}")

    source = await _emitted_source(event)

    assert source.chat_type == chat_type
    runner = _runner()
    verdicts = tuple(_admitted(runner, source.chat_type, channel, user) for user in USERS)
    assert verdicts == admit


@pytest.mark.asyncio
async def test_mpim_does_not_consult_the_dm_allowlist(monkeypatch):
    """A DM-only user must not gain MPIM access: the DM list is not an MPIM gate.

    Same surface, allowlists inverted (``U_DM`` only in the DM list) — if the MPIM path read
    ``SLACK_DM_ALLOWED_USERS`` this would come back True.
    """
    adapter = _adapter()
    emitted = []
    adapter.handle_message = AsyncMock(side_effect=lambda evt: emitted.append(evt))
    with patch.object(adapter, "_resolve_user_name", new=AsyncMock(return_value="u")):
        await adapter._handle_slack_message({
            "type": "message", "channel": "G1", "channel_type": "mpim", "user": "U_DM",
            "text": "<@U_BOT> hi", "ts": f"{time.time():.6f}",
        })
    assert emitted, "MPIM message was dropped"
    source = emitted[0].source
    assert source.chat_type == "group"

    monkeypatch.setenv("SLACK_ALLOWED_USERS", "U_CHAN")   # U_DM is not a platform-wide user
    monkeypatch.setenv("SLACK_DM_ALLOWED_USERS", "U_DM")  # ...only a DM user
    assert _admitted(_runner(), source.chat_type, "G1", "U_DM") is False
    assert _admitted(_runner(), source.chat_type, "G1", "U_CHAN") is True
