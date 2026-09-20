"""Forwarded Slack messages must reach the agent, text and files alike.

Regression for 2026-09-19: in #agent-monitoring a human forwarded a thread
from another channel. Slack delivers a forwarded message as an
``attachments[]`` entry with ``is_msg_unfurl: true`` — the same flag it sets
when a link to one of OUR OWN messages is unfurled. The inbound path skipped
every ``is_msg_unfurl`` attachment as an echo guard, so the forwarded body and
the screenshot inside it were both discarded. The agent saw only the bare
mention and answered from unrelated in-flight work.

The payload below is the real event pulled from Slack (ts 1789825843.874019),
trimmed to the fields the adapter reads.
"""

import sys
from unittest.mock import MagicMock

import pytest


# ---------------------------------------------------------------------------
# Mock slack-bolt if not installed (same as test_slack_mention.py)
# ---------------------------------------------------------------------------

def _ensure_slack_mock():
    if "slack_bolt" in sys.modules and hasattr(sys.modules["slack_bolt"], "__file__"):
        return

    slack_bolt = MagicMock()
    slack_bolt.async_app.AsyncApp = MagicMock
    slack_bolt.adapter.socket_mode.async_handler.AsyncSocketModeHandler = MagicMock
    sys.modules["slack_bolt"] = slack_bolt
    sys.modules.setdefault("slack_bolt.async_app", slack_bolt.async_app)
    sys.modules.setdefault("slack_bolt.adapter", slack_bolt.adapter)
    sys.modules.setdefault("slack_bolt.adapter.socket_mode", slack_bolt.adapter.socket_mode)
    sys.modules.setdefault(
        "slack_bolt.adapter.socket_mode.async_handler",
        slack_bolt.adapter.socket_mode.async_handler,
    )


_ensure_slack_mock()

from plugins.platforms.slack.adapter import (  # noqa: E402
    _extract_forwarded_attachment,
    _is_own_message_unfurl,
)

OUR_BOT = "U0AH8J6H5A9"

# Real forwarded message: human "Snow" forwarded a thread out of C05RWRCTFDK.
FORWARDED_ATTACHMENT = {
    "fallback": "[September 19th, 2026 12:29 AM] snow: <@U0BLTGBT1DL> check for both web leads and inbound calls",
    "ts": "1789752548.042889",
    "author_id": "U02KNFZHAHG",
    "author_subname": "Snow",
    "channel_id": "C05RWRCTFDK",
    "channel_team": "T01QESTM9GA",
    "is_msg_unfurl": True,
    "is_thread_root_unfurl": True,
    "blocks": [
        {
            "type": "rich_text",
            "block_id": "3EYDi",
            "elements": [
                {
                    "type": "rich_text_section",
                    "elements": [
                        {"type": "user", "user_id": "U0BLTGBT1DL"},
                        {"type": "text", "text": " check for both web leads and inbound calls"},
                    ],
                }
            ],
        }
    ],
    "text": "<@U0BLTGBT1DL> check for both web leads and inbound calls",
    "files": [
        {
            "id": "F0C31KPM4F6",
            "mimetype": "image/png",
            "filetype": "png",
            "name": "image.png",
            "url_private": "https://files.slack.com/files-pri/T01QESTM9GA-F0C31KPM4F6/image.png",
            "url_private_download": "https://files.slack.com/files-pri/T01QESTM9GA-F0C31KPM4F6/download/image.png",
        }
    ],
}

# An unfurl of one of our own messages — must still be dropped.
OWN_MESSAGE_ATTACHMENT = {
    "is_msg_unfurl": True,
    "author_id": OUR_BOT,
    "text": "Cronjob Response: daily-memory-ingest (job_id: 3f6e8322b835)",
    "channel_id": "C0AR4A8S6DP",
}


def test_forwarded_body_is_extracted():
    body, files = _extract_forwarded_attachment(FORWARDED_ATTACHMENT)

    assert "check for both web leads and inbound calls" in body
    # The author/channel context is what tells the agent this came from elsewhere.
    assert "Snow" in body
    assert "C05RWRCTFDK" in body
    # The Slack fallback is "[date] author: <same text>" — it must not be
    # appended a second time once the body is present.
    assert body.count("check for both web leads and inbound calls") == 1
    assert len(files) == 1
    assert files[0]["id"] == "F0C31KPM4F6"


def test_forwarded_screenshot_is_handed_to_the_media_pipeline():
    """The image lives at att['files'], not in the event's top-level files."""
    _, files = _extract_forwarded_attachment(FORWARDED_ATTACHMENT)

    assert [f["mimetype"] for f in files] == ["image/png"]
    # The adapter's downloader keys off this field.
    assert files[0]["url_private_download"].startswith("https://files.slack.com/")


def test_forwarded_message_is_not_treated_as_our_own_echo():
    assert _is_own_message_unfurl(FORWARDED_ATTACHMENT, {OUR_BOT}) is False


def test_own_message_unfurl_is_still_skipped():
    assert _is_own_message_unfurl(OWN_MESSAGE_ATTACHMENT, {OUR_BOT}) is True


def test_plain_attachment_is_not_flagged_as_unfurl():
    assert _is_own_message_unfurl({"title": "Notion page"}, {OUR_BOT}) is False


@pytest.mark.parametrize("key", ["bot_id", "author_id", "user"])
def test_own_bot_detected_under_every_author_field(key):
    att = {"is_msg_unfurl": True, key: OUR_BOT}
    assert _is_own_message_unfurl(att, {OUR_BOT}) is True


def test_other_bots_unfurl_is_kept():
    """A forwarded message from another bot is still content, not our echo."""
    att = {"is_msg_unfurl": True, "bot_id": "B0BMJ5YKV7B", "text": "revenue figures"}
    assert _is_own_message_unfurl(att, {OUR_BOT}) is False


# ---------------------------------------------------------------------------
# End-to-end through the real inbound handler: what the AGENT receives
# ---------------------------------------------------------------------------

import os  # noqa: E402
import time  # noqa: E402
from unittest.mock import AsyncMock, MagicMock, patch  # noqa: E402

import pytest  # noqa: E402


def _make_adapter():
    from gateway.config import PlatformConfig
    from plugins.platforms.slack.adapter import SlackAdapter

    a = SlackAdapter(PlatformConfig(enabled=True, token="xoxb-fake-token"))
    a._app = MagicMock()
    a._app.client = AsyncMock()
    a._bot_user_id = OUR_BOT
    a._team_bot_user_ids = {"T_TEAM": OUR_BOT}
    a._running = True
    a.handle_message = AsyncMock()
    return a


def _forwarded_event():
    # Fresh ts: the adapter drops events older than its staleness window, and
    # the real payload's ts is historical by the time tests run.
    ts = f"{time.time():.6f}"
    return {
        "text": f"<@{OUR_BOT}> <@U0BMA0XUH6F>",
        "user": "U028TNBPA22",
        "channel": "C0AR4A8S6DP",
        "channel_type": "channel",
        "ts": ts,
        "thread_ts": ts,
        "files": [],
        "blocks": [],
        "attachments": [FORWARDED_ATTACHMENT],
    }


@pytest.mark.asyncio
async def test_agent_receives_forwarded_text_and_nested_image(tmp_path):
    """The whole point: forwarded body AND its screenshot reach the agent."""
    adapter = _make_adapter()
    cached_png = tmp_path / "forwarded.png"
    cached_png.write_bytes(b"\x89PNG\r\n\x1a\n fake")

    with patch.object(
        adapter, "_download_slack_file", new_callable=AsyncMock
    ) as dl:
        dl.return_value = str(cached_png)
        await adapter._handle_slack_message(_forwarded_event())

    msg_event = adapter.handle_message.call_args[0][0]

    # (1) the forwarded text survived, with its origin labelled
    assert "check for both web leads and inbound calls" in msg_event.text
    assert "Forwarded message from Snow" in msg_event.text
    assert "C05RWRCTFDK" in msg_event.text

    # (2) the screenshot nested in the attachment reached the media pipeline
    assert msg_event.media_types == ["image/png"]
    assert msg_event.media_urls == [str(cached_png)]
    # ...downloaded from the attachment's file, which Slack only exposes there
    assert "F0C31KPM4F6" in dl.call_args[0][0]


@pytest.mark.asyncio
async def test_own_message_unfurl_is_not_injected():
    """Echo guard intact: our own unfurled message contributes no content."""
    adapter = _make_adapter()
    event = _forwarded_event()
    event["attachments"] = [OWN_MESSAGE_ATTACHMENT]

    await adapter._handle_slack_message(event)

    text = adapter.handle_message.call_args[0][0].text
    assert "Cronjob Response" not in text
