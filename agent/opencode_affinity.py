"""Per-conversation ``x-opencode-session`` affinity for OpenCode relays."""

from __future__ import annotations

import hashlib
from typing import Any, Optional
from urllib.parse import urlparse

OPENCODE_SESSION_HEADER = "x-opencode-session"


def is_opencode_target(provider: Optional[str], base_url: Optional[str]) -> bool:
    """Return whether the provider or endpoint addresses an OpenCode relay."""
    provider_name = str(provider or "").strip().lower()
    if provider_name == "opencode" or provider_name.startswith("opencode-"):
        return True
    try:
        from hermes_cli.models import opencode_provider_family

        if opencode_provider_family(provider) is not None:
            return True
    except Exception:
        pass
    try:
        from agent.anthropic_endpoints import _is_opencode_endpoint

        if _is_opencode_endpoint(str(base_url or "")):
            return True
    except Exception:
        pass
    try:
        hostname = (urlparse(str(base_url or "")).hostname or "").lower()
        return hostname == "opencode.ai" or hostname.endswith(".opencode.ai")
    except Exception:
        return False


def opencode_session_headers(
    provider: Optional[str],
    base_url: Optional[str],
    session_id: Optional[str] = None,
) -> dict[str, str]:
    """Return an opaque, stable conversation-affinity header for OpenCode."""
    if not is_opencode_target(provider, base_url):
        return {}
    key = str(session_id or "")
    try:
        from agent.portal_tags import get_conversation_context

        key = str(get_conversation_context() or key)
    except Exception:
        pass
    if not key:
        return {}
    # Never disclose the native gateway session key (which can contain Slack
    # workspace/channel/thread IDs) to the provider. The digest remains stable
    # for the conversation while different threads receive different affinity.
    opaque_key = "hermes_" + hashlib.sha256(key.encode("utf-8")).hexdigest()[:32]
    return {OPENCODE_SESSION_HEADER: opaque_key}


def merge_opencode_session_headers(
    kwargs: dict[str, Any],
    provider: Optional[str],
    base_url: Optional[str],
    session_id: Optional[str] = None,
) -> dict[str, Any]:
    """Add affinity without replacing an explicit per-request header."""
    headers = opencode_session_headers(provider, base_url, session_id)
    if headers:
        existing = kwargs.get("extra_headers")
        merged = dict(existing) if isinstance(existing, dict) else {}
        for key, value in headers.items():
            merged.setdefault(key, value)
        kwargs["extra_headers"] = merged
    return kwargs
