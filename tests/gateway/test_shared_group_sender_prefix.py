import json

import pytest

from gateway.config import GatewayConfig, Platform, PlatformConfig
from gateway.platforms.event import MessageEvent
from gateway.run import GatewayRunner
from gateway.session import SessionSource


def _make_runner(config: GatewayConfig) -> GatewayRunner:
    runner = object.__new__(GatewayRunner)
    runner.config = config
    runner.adapters = {}
    runner._model = "openai/gpt-4.1-mini"
    runner._base_url = None
    return runner


@pytest.mark.asyncio
async def test_preprocess_includes_slack_author_mention_for_shared_thread():
    """Shared Slack threads expose the current author's verifiable user ID
    next to the display name so 'mention me again' requests can bind the
    mention to the CURRENT speaker (#17916).

    The identity is carried in the gateway-signed actor envelope emitted by
    ``gateway/run_inbound.py::_prefix_inbound_sender_context``. The pre-v0.23.0
    fork emitted ``[Alice | Slack user <@U123>]``: the same four fields in a
    different shape. Assert them individually so the identity contract stays
    pinned even if the envelope grows a field, and assert the full string so a
    silent reformat is still caught.
    """
    runner = _make_runner(
        GatewayConfig(
            platforms={
                Platform.SLACK: PlatformConfig(enabled=True, token="fake"),
            },
        )
    )
    source = SessionSource(
        platform=Platform.SLACK,
        chat_id="C123",
        chat_name="team-channel",
        chat_type="group",
        user_id="U123",
        user_name="Alice",
        thread_id="171.000",
    )
    event = MessageEvent(text="mention me again", source=source)

    result = await runner._prepare_inbound_message_text(
        event=event,
        source=source,
        history=[],
    )

    assert result is not None
    prefix, separator, body = result.partition("] ")
    assert separator == "] ", f"no actor envelope in {result!r}"
    assert prefix.startswith("[Gateway actor ")
    assert body == "mention me again"

    actor = json.loads(prefix[len("[Gateway actor "):])
    assert actor == {
        "platform": "slack",
        "user_id": "U123",
        "display_name": "Alice",
        "slack_mention": "<@U123>",
    }
    assert result == (
        '[Gateway actor {"platform":"slack","user_id":"U123","display_name":"Alice",'
        '"slack_mention":"<@U123>"}] mention me again'
    )
