"""OpenCode requests use opaque, per-conversation backend affinity."""

import pytest

from agent import auxiliary_client as aux
from agent.chat_completion_helpers import build_api_kwargs
from agent.opencode_affinity import opencode_session_headers
from gateway.config import Platform
from gateway.session import SessionSource, build_session_key
from run_agent import AIAgent


def _slack_session(thread_id: str, user_id: str) -> str:
    source = SessionSource(
        platform=Platform.SLACK,
        chat_id="C0B4BDU050V",
        chat_type="group",
        thread_id=thread_id,
        user_id=user_id,
    )
    return build_session_key(
        source, group_sessions_per_user=False, thread_sessions_per_user=False
    )


def test_shared_slack_thread_has_stable_opaque_affinity_across_users():
    agung = _slack_session("1789958081.509219", "U_AGUNG")
    puie = _slack_session("1789958081.509219", "U_PUIE")
    assert agung == puie

    first = opencode_session_headers(
        "opencode-go", "https://opencode.ai/zen/go/v1", agung
    )["x-opencode-session"]
    second = opencode_session_headers(
        "opencode-go", "https://opencode.ai/zen/go/v1", puie
    )["x-opencode-session"]
    assert first == second
    assert first.startswith("hermes_")
    assert agung not in first
    assert "1789958081.509219" not in first


def test_different_slack_threads_have_different_affinity():
    first = _slack_session("1789958081.509219", "U_AGUNG")
    second = _slack_session("1789959999.000001", "U_AGUNG")
    assert opencode_session_headers("opencode-go", None, first) != (
        opencode_session_headers("opencode-go", None, second)
    )


def test_auxiliary_calls_use_the_active_main_session_affinity():
    token = aux.set_runtime_main(
        "opencode-go",
        "glm-5.3-flash",
        base_url="https://opencode.ai/zen/go/v1",
        session_id="agent:main:slack:thread:C1:T1",
    )
    try:
        kwargs = aux._build_call_kwargs(
            "opencode-go",
            "glm-5.3-flash",
            [{"role": "user", "content": "hi"}],
            base_url="https://opencode.ai/zen/go/v1",
        )
        assert kwargs["extra_headers"]["x-opencode-session"].startswith("hermes_")
    finally:
        aux.reset_runtime_main(token)


def test_non_opencode_target_gets_no_affinity_header():
    assert opencode_session_headers(
        "openrouter", "https://openrouter.ai/api/v1", "session-1"
    ) == {}


@pytest.mark.parametrize(
    "model,api_mode",
    [
        ("glm-5.3-flash", None),
        ("gpt-5.6-luna", None),
        ("minimax-m2.7", "anthropic_messages"),
    ],
)
def test_main_turn_sends_affinity_on_every_transport(model, api_mode):
    agent = AIAgent(
        api_key="test-key",
        base_url="https://opencode.ai/zen/go/v1",
        model=model,
        provider="opencode-go",
        quiet_mode=True,
        skip_context_files=True,
        skip_memory=True,
        session_id="session-main-1",
    )
    if api_mode:
        agent.api_mode = api_mode
        agent._transport = None
        agent._anthropic_base_url = agent.base_url

    kwargs = build_api_kwargs(agent, [{"role": "user", "content": "hi"}])
    assert kwargs["extra_headers"]["x-opencode-session"].startswith("hermes_")
