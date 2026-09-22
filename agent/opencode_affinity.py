"""Per-conversation ``x-opencode-session`` affinity for OpenCode relays."""

from __future__ import annotations

import hashlib
import uuid
from typing import Any, Optional
from urllib.parse import urlparse

OPENCODE_SESSION_HEADER = "x-opencode-session"

# Fallback affinity for calls that happen OUTSIDE a conversation (no portal
# tags, no session id) — chiefly the gateway's pre-turn image description
# (gateway/run.py::_enrich_message_with_vision). The relay rejects a request
# with no session header at all with HTTP 400 MissingSessionID, so returning {}
# there is not "no affinity", it is a failed call: observed live 2026-09-22
# 17:02:19Z, the first Slack image after vision moved to the OpenCode Go leg
# logged `Error analyzing image: Error code: 400 ... MissingSessionID` and fell
# back to "I couldn't quite see it", leaving the turn to re-read the image with
# its own vision_analyze call. One opaque value per process keeps those
# one-shot calls routable without pooling every install into one bucket.
_PROCESS_FALLBACK_SESSION_KEY = "hermes_proc_" + hashlib.sha256(
    uuid.uuid4().hex.encode("utf-8")
).hexdigest()[:32]


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
        # Out-of-conversation call (see _PROCESS_FALLBACK_SESSION_KEY): return the
        # value as-is instead of digesting it, so the header itself shows that
        # this request had no conversation to attach affinity to.
        return {OPENCODE_SESSION_HEADER: _PROCESS_FALLBACK_SESSION_KEY}
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
