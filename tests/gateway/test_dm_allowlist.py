"""Tests for the DM-scoped allowlist feature.

`<PLATFORM>_DM_ALLOWED_USERS` lets operators run an open bot in channels
(via `<PLATFORM>_ALLOW_ALL_USERS=true`) while locking direct messages
(incl. Slack Assistant) to a small list. Verifies:

  * DM gate enforced when env set
  * DM gate overrides `<PLATFORM>_ALLOW_ALL_USERS=true`
  * Channel/group traffic NOT affected by DM gate
  * Wildcard `*` opens DMs
  * Pairing-store approvals still bypass
  * Falls back to existing platform allowlist when DM env not set
  * `_get_unauthorized_dm_behavior` returns "ignore" when DM env is set
"""

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

from gateway.config import GatewayConfig, Platform, PlatformConfig
from gateway.session import SessionSource


def _clear_auth_env(monkeypatch) -> None:
    for key in (
        "TELEGRAM_ALLOWED_USERS", "TELEGRAM_GROUP_ALLOWED_USERS",
        "TELEGRAM_DM_ALLOWED_USERS",
        "DISCORD_ALLOWED_USERS", "DISCORD_DM_ALLOWED_USERS",
        "WHATSAPP_ALLOWED_USERS", "WHATSAPP_DM_ALLOWED_USERS",
        "SLACK_ALLOWED_USERS", "SLACK_DM_ALLOWED_USERS",
        "SIGNAL_ALLOWED_USERS", "SIGNAL_DM_ALLOWED_USERS",
        "EMAIL_ALLOWED_USERS", "EMAIL_DM_ALLOWED_USERS",
        "GATEWAY_ALLOWED_USERS",
        "TELEGRAM_ALLOW_ALL_USERS", "DISCORD_ALLOW_ALL_USERS",
        "WHATSAPP_ALLOW_ALL_USERS", "SLACK_ALLOW_ALL_USERS",
        "GATEWAY_ALLOW_ALL_USERS",
    ):
        monkeypatch.delenv(key, raising=False)


def _make_runner(platform: Platform, *, pairing_approved: bool = False):
    from gateway.run import GatewayRunner

    runner = object.__new__(GatewayRunner)
    runner.config = GatewayConfig(platforms={platform: PlatformConfig(enabled=True)})
    runner.adapters = {platform: SimpleNamespace(send=AsyncMock())}
    runner.pairing_store = MagicMock()
    runner.pairing_store.is_approved.return_value = pairing_approved
    runner.pairing_store._is_rate_limited.return_value = False
    return runner


def _dm_source(platform: Platform, user_id: str) -> SessionSource:
    return SessionSource(
        platform=platform,
        user_id=user_id,
        chat_id=f"D{user_id}",
        user_name="tester",
        chat_type="dm",
    )


def _channel_source(platform: Platform, user_id: str, chat_id: str = "C1") -> SessionSource:
    return SessionSource(
        platform=platform,
        user_id=user_id,
        chat_id=chat_id,
        user_name="tester",
        chat_type="group",
    )


# ---------------------------------------------------------------------------
# Core DM gate behavior
# ---------------------------------------------------------------------------

def test_slack_dm_allowlist_admits_listed_user(monkeypatch):
    _clear_auth_env(monkeypatch)
    monkeypatch.setenv("SLACK_DM_ALLOWED_USERS", "U_PUIE,U_AGUNG")

    runner = _make_runner(Platform.SLACK)
    assert runner._is_user_authorized(_dm_source(Platform.SLACK, "U_PUIE")) is True
    assert runner._is_user_authorized(_dm_source(Platform.SLACK, "U_AGUNG")) is True


def test_slack_dm_allowlist_rejects_unlisted_user(monkeypatch):
    _clear_auth_env(monkeypatch)
    monkeypatch.setenv("SLACK_DM_ALLOWED_USERS", "U_PUIE,U_AGUNG")

    runner = _make_runner(Platform.SLACK)
    assert runner._is_user_authorized(_dm_source(Platform.SLACK, "U_STRANGER")) is False


def test_slack_dm_allowlist_overrides_allow_all_users(monkeypatch):
    """The key use case: open bot in channels, lock DMs to admins."""
    _clear_auth_env(monkeypatch)
    monkeypatch.setenv("SLACK_ALLOW_ALL_USERS", "true")
    monkeypatch.setenv("SLACK_DM_ALLOWED_USERS", "U_PUIE")

    runner = _make_runner(Platform.SLACK)

    # DM from non-listed user is rejected even with ALLOW_ALL
    assert runner._is_user_authorized(_dm_source(Platform.SLACK, "U_STRANGER")) is False
    # DM from listed user passes
    assert runner._is_user_authorized(_dm_source(Platform.SLACK, "U_PUIE")) is True
    # Channel traffic still allowed for everyone (ALLOW_ALL governs channels)
    assert runner._is_user_authorized(_channel_source(Platform.SLACK, "U_STRANGER")) is True
    assert runner._is_user_authorized(_channel_source(Platform.SLACK, "U_PUIE")) is True


def test_slack_dm_allowlist_does_not_affect_channel_messages(monkeypatch):
    """DM gate must not bleed into channel auth."""
    _clear_auth_env(monkeypatch)
    monkeypatch.setenv("SLACK_DM_ALLOWED_USERS", "U_PUIE")
    monkeypatch.setenv("SLACK_ALLOWED_USERS", "U_PUIE,U_AGUNG,U_BOB")

    runner = _make_runner(Platform.SLACK)

    # Channel messages obey SLACK_ALLOWED_USERS, not the DM gate
    assert runner._is_user_authorized(_channel_source(Platform.SLACK, "U_AGUNG")) is True
    assert runner._is_user_authorized(_channel_source(Platform.SLACK, "U_BOB")) is True
    # But Agung still can't DM (not on DM allowlist)
    assert runner._is_user_authorized(_dm_source(Platform.SLACK, "U_AGUNG")) is False


def test_slack_dm_allowlist_wildcard_opens_dms(monkeypatch):
    _clear_auth_env(monkeypatch)
    monkeypatch.setenv("SLACK_DM_ALLOWED_USERS", "*")

    runner = _make_runner(Platform.SLACK)
    assert runner._is_user_authorized(_dm_source(Platform.SLACK, "U_RANDOM")) is True


def test_dm_gate_unset_falls_back_to_platform_allowlist(monkeypatch):
    """When DM env is unset, existing platform allowlist still governs."""
    _clear_auth_env(monkeypatch)
    monkeypatch.setenv("SLACK_ALLOWED_USERS", "U_PUIE")

    runner = _make_runner(Platform.SLACK)
    assert runner._is_user_authorized(_dm_source(Platform.SLACK, "U_PUIE")) is True
    assert runner._is_user_authorized(_dm_source(Platform.SLACK, "U_STRANGER")) is False


def test_pairing_approval_bypasses_dm_allowlist(monkeypatch):
    """Pairing is an explicit per-user grant; it must still admit DMs."""
    _clear_auth_env(monkeypatch)
    monkeypatch.setenv("SLACK_DM_ALLOWED_USERS", "U_PUIE")

    runner = _make_runner(Platform.SLACK, pairing_approved=True)
    # U_NEWLY_PAIRED is not in the DM list, but pairing store approved them
    assert runner._is_user_authorized(_dm_source(Platform.SLACK, "U_NEWLY_PAIRED")) is True


# ---------------------------------------------------------------------------
# Cross-platform parity
# ---------------------------------------------------------------------------

def test_telegram_dm_allowlist_works_same_pattern(monkeypatch):
    _clear_auth_env(monkeypatch)
    monkeypatch.setenv("TELEGRAM_ALLOW_ALL_USERS", "true")
    monkeypatch.setenv("TELEGRAM_DM_ALLOWED_USERS", "111")

    runner = _make_runner(Platform.TELEGRAM)

    assert runner._is_user_authorized(_dm_source(Platform.TELEGRAM, "111")) is True
    assert runner._is_user_authorized(_dm_source(Platform.TELEGRAM, "999")) is False
    # Channels open
    assert runner._is_user_authorized(_channel_source(Platform.TELEGRAM, "999")) is True


def test_discord_dm_allowlist_works_same_pattern(monkeypatch):
    _clear_auth_env(monkeypatch)
    monkeypatch.setenv("DISCORD_DM_ALLOWED_USERS", "111")

    runner = _make_runner(Platform.DISCORD)
    assert runner._is_user_authorized(_dm_source(Platform.DISCORD, "111")) is True
    assert runner._is_user_authorized(_dm_source(Platform.DISCORD, "222")) is False


# ---------------------------------------------------------------------------
# unauthorized_dm_behavior integration
# ---------------------------------------------------------------------------

def test_unauthorized_dm_behavior_ignores_when_dm_allowlist_set(monkeypatch):
    """Setting DM allowlist signals "lock DMs" — unauthorized DMs should be
    silently dropped rather than respond with a pairing code (parity with
    other allowlist env vars; see #9337)."""
    _clear_auth_env(monkeypatch)
    monkeypatch.setenv("SLACK_DM_ALLOWED_USERS", "U_PUIE")

    runner = _make_runner(Platform.SLACK)
    assert runner._get_unauthorized_dm_behavior(Platform.SLACK) == "ignore"


def test_unauthorized_dm_behavior_pairs_when_nothing_set(monkeypatch):
    """No allowlist of any kind → default to "pair" (open-gateway default)."""
    _clear_auth_env(monkeypatch)
    runner = _make_runner(Platform.SLACK)
    assert runner._get_unauthorized_dm_behavior(Platform.SLACK) == "pair"
