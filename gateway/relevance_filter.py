"""LLM-based relevance filter for incoming gateway messages.

When the bot is configured to respond to non-mention messages in a thread
(Slack's ``reply_to_bot_thread`` / ``in_mentioned_thread`` / ``has_session``
paths, Telegram group follow-ups, Discord channel-prompt chats, etc.) it
ends up replying to *every* subsequent message in that thread — including
chatter between humans that wasn't directed at it.

This module gates the platform-agnostic ``handle_message`` entry point with:

1. Cheap bypass rules first — DMs, slash commands, replies to the bot,
   internal/system events, and messages shorter than ``min_text_length``.
2. An auxiliary-LLM classifier for the ambiguous cases.

Design notes:

- Fail-open: any internal error or missing auxiliary client returns
  ``True`` (respond).  We never want this filter to silently swallow a
  message the user genuinely intended for the bot.
- The classifier uses the same auxiliary-LLM provider chain Hermes already
  uses for compression / web extraction, so no new credentials are
  required.  Operators can override the model via
  ``HERMES_RELEVANCE_FILTER_MODEL``.
- Decisions are cached per ``(platform, chat_id, message_id)`` for 60s so
  retried/duplicated webhook deliveries don't re-spend tokens.
"""

from __future__ import annotations

import asyncio
import logging
import os
import time
from dataclasses import dataclass, field
from typing import Any, Optional, TYPE_CHECKING

if TYPE_CHECKING:
    from gateway.platforms.base import MessageEvent

logger = logging.getLogger(__name__)


_CACHE_MAX = 4096
_CACHE_TTL_SECONDS = 60.0
_CLASSIFIER_TIMEOUT_SECONDS = 8.0
_DEFAULT_MIN_TEXT_LENGTH = 3

_decision_cache: dict[str, tuple[bool, float]] = {}
_decision_cache_lock = asyncio.Lock()


@dataclass
class RelevanceFilterConfig:
    """Runtime configuration for the gateway relevance filter.

    All fields can be overridden by environment variables; ``from_env``
    reads them lazily so test suites can mutate the environment.
    """

    enabled: bool = False
    model: str = ""
    min_text_length: int = _DEFAULT_MIN_TEXT_LENGTH
    bypass_platforms: list[str] = field(default_factory=list)
    log_decisions: bool = True

    def is_bypassed(self, platform_name: str) -> bool:
        if not self.bypass_platforms:
            return False
        target = (platform_name or "").lower()
        return target in {p.strip().lower() for p in self.bypass_platforms if p}

    def to_dict(self) -> dict[str, Any]:
        return {
            "enabled": self.enabled,
            "model": self.model,
            "min_text_length": self.min_text_length,
            "bypass_platforms": list(self.bypass_platforms),
            "log_decisions": self.log_decisions,
        }

    @classmethod
    def from_dict(cls, data: Optional[dict[str, Any]]) -> "RelevanceFilterConfig":
        if not data:
            return cls()
        bypass = data.get("bypass_platforms") or []
        if isinstance(bypass, str):
            bypass = [item.strip() for item in bypass.split(",") if item.strip()]
        try:
            min_len = int(data.get("min_text_length", _DEFAULT_MIN_TEXT_LENGTH))
        except (TypeError, ValueError):
            min_len = _DEFAULT_MIN_TEXT_LENGTH
        return cls(
            enabled=bool(data.get("enabled", False)),
            model=str(data.get("model") or ""),
            min_text_length=max(0, min_len),
            bypass_platforms=[str(p) for p in bypass],
            log_decisions=bool(data.get("log_decisions", True)),
        )

    @classmethod
    def from_env(cls, base: Optional["RelevanceFilterConfig"] = None) -> "RelevanceFilterConfig":
        cfg = base or cls()
        env = os.environ
        if "HERMES_RELEVANCE_FILTER_ENABLED" in env:
            cfg.enabled = env["HERMES_RELEVANCE_FILTER_ENABLED"].strip().lower() in {
                "1", "true", "yes", "on",
            }
        if env.get("HERMES_RELEVANCE_FILTER_MODEL"):
            cfg.model = env["HERMES_RELEVANCE_FILTER_MODEL"].strip()
        if "HERMES_RELEVANCE_FILTER_MIN_LEN" in env:
            try:
                cfg.min_text_length = max(0, int(env["HERMES_RELEVANCE_FILTER_MIN_LEN"]))
            except ValueError:
                pass
        if env.get("HERMES_RELEVANCE_FILTER_BYPASS_PLATFORMS"):
            cfg.bypass_platforms = [
                p.strip()
                for p in env["HERMES_RELEVANCE_FILTER_BYPASS_PLATFORMS"].split(",")
                if p.strip()
            ]
        if "HERMES_RELEVANCE_FILTER_LOG" in env:
            cfg.log_decisions = env["HERMES_RELEVANCE_FILTER_LOG"].strip().lower() in {
                "1", "true", "yes", "on",
            }
        return cfg


_active_config: RelevanceFilterConfig = RelevanceFilterConfig()
_config_initialized: bool = False


def configure(config: RelevanceFilterConfig) -> None:
    """Install the active configuration. Called by the gateway runner on startup."""
    global _active_config, _config_initialized
    _active_config = RelevanceFilterConfig.from_env(base=config)
    _config_initialized = True
    if _active_config.enabled:
        logger.info(
            "[relevance_filter] enabled (model=%s, min_len=%d, bypass=%s)",
            _active_config.model or "<auxiliary default>",
            _active_config.min_text_length,
            ",".join(_active_config.bypass_platforms) or "<none>",
        )


def get_active_config() -> RelevanceFilterConfig:
    """Return the current config, populating it from env vars on first call.

    Lets adapters call the filter without the runner having to wire things
    up explicitly — useful for tests and adapter-only invocations.
    """
    global _active_config, _config_initialized
    if not _config_initialized:
        _active_config = RelevanceFilterConfig.from_env()
        _config_initialized = True
    return _active_config


CLASSIFIER_SYSTEM_PROMPT = (
    "You are a strict relevance classifier for an AI assistant that "
    "shares chat rooms and threads with human users.\n\n"
    "You will be given the bot's handle, the latest message, and any "
    "prior thread context.  Decide whether the message is **directed at "
    "the bot** or is **chatter between humans** the bot should stay "
    "out of.\n\n"
    "Reply with ONLY one word: RELEVANT or IGNORE.  Do not explain.\n\n"
    "Hard rules (apply in order):\n\n"
    "1. If the message explicitly addresses the bot by its handle, "
    "@mentions it, or names it directly — RELEVANT.\n\n"
    "2. If the message addresses a specific *human* by name or @handle "
    "(e.g. \"hey John, ...\", \"John, where are you?\", \"@alice can "
    "you ...\", \"thanks @bob\") and that name is NOT the bot's "
    "handle — IGNORE.  Two humans are talking to each other; the bot "
    "is a bystander in the thread.\n\n"
    "3. If the message is a short reaction or acknowledgement (\"ok\", "
    "\"thanks\", \"555\", \"lol\", emoji-only, sticker) not clearly "
    "directed at the bot — IGNORE.\n\n"
    "4. If the message is logistics between humans (\"I'll handle "
    "it\", \"can you join the call?\", \"meeting in 10\") with no "
    "request for the bot to act — IGNORE.\n\n"
    "5. Otherwise, if the message asks a question, requests "
    "information, or gives an instruction the bot could plausibly "
    "act on, and there is no clearer human addressee — RELEVANT.\n\n"
    "Tie-breaker: when rules 1 and 2 both could apply (e.g. the bot's "
    "handle appears AND another human's name appears), follow rule 1 "
    "(RELEVANT).  Otherwise, when genuinely uncertain about intent, "
    "prefer IGNORE — the bot can always be re-summoned with an "
    "@mention, but unwanted interjections derail the conversation.\n"
)


CLASSIFIER_USER_TEMPLATE = (
    "Bot name: {bot_name}\n"
    "Platform: {platform}\n"
    "Prior thread context (most recent last, may be empty):\n"
    "---\n"
    "{context}\n"
    "---\n"
    "{reply_block}"
    "Latest message (from {sender}):\n"
    "{message}\n\n"
    "Should the assistant respond? Answer with one word: RELEVANT or IGNORE."
)


async def should_respond(
    event: "MessageEvent",
    *,
    bot_name: str = "the assistant",
    config: Optional[RelevanceFilterConfig] = None,
) -> bool:
    """Return ``True`` if the bot should respond to this event.

    Fail-open: any internal error returns ``True`` so users never lose
    messages because of classifier outages.
    """
    cfg = config or get_active_config()
    if not cfg.enabled:
        return True

    source = getattr(event, "source", None)
    platform_name = ""
    if source is not None:
        raw_platform = getattr(source, "platform", None)
        platform_name = getattr(raw_platform, "value", None) or str(raw_platform or "")
    if cfg.is_bypassed(platform_name):
        return True

    if event.is_command():
        return True

    chat_type = getattr(source, "chat_type", None) if source else None
    if chat_type == "dm":
        return True

    if getattr(event, "internal", False):
        return True

    # NOTE: we deliberately do NOT bypass on ``reply_to_text`` here.
    # In Slack (and other thread-aware adapters) every message inside a
    # thread carries ``reply_to_text`` set to the thread parent — even
    # when the user is addressing another human in the same thread.
    # Letting the LLM classifier see the ``[Replying to: "..."]`` prefix
    # that the runner injects into ``event.text`` gives it enough context
    # to make the right call without us mistaking thread continuation
    # for "directed at the bot".

    text = (getattr(event, "text", "") or "").strip()
    min_len = max(1, cfg.min_text_length)
    if not text or len(text) < min_len:
        if cfg.log_decisions:
            logger.info(
                "[relevance_filter] drop short message len=%d platform=%s chat=%s",
                len(text),
                platform_name or "?",
                getattr(source, "chat_id", "?"),
            )
        return False

    cache_key = _build_cache_key(event, platform_name)
    if cache_key:
        async with _decision_cache_lock:
            hit = _decision_cache.get(cache_key)
            if hit is not None:
                decision, ts = hit
                if time.monotonic() - ts < _CACHE_TTL_SECONDS:
                    return decision

    try:
        decision = await _classify(
            event,
            cfg,
            bot_name=bot_name,
            platform_name=platform_name,
        )
    except (asyncio.TimeoutError, asyncio.CancelledError):
        logger.warning(
            "[relevance_filter] classifier timed out for %s/%s; failing open",
            platform_name or "?",
            getattr(source, "chat_id", "?"),
        )
        return True
    except Exception as exc:  # noqa: BLE001 — log + fail open
        logger.warning(
            "[relevance_filter] classifier error (%s); failing open",
            exc,
        )
        return True

    if cache_key:
        async with _decision_cache_lock:
            _decision_cache[cache_key] = (decision, time.monotonic())
            if len(_decision_cache) > _CACHE_MAX:
                _evict_oldest_locked()

    if cfg.log_decisions:
        logger.info(
            "[relevance_filter] %s platform=%s chat=%s msg=%s text=%r",
            "RELEVANT" if decision else "IGNORE",
            platform_name or "?",
            getattr(source, "chat_id", "?"),
            getattr(event, "message_id", None),
            text[:160],
        )

    return decision


def _build_cache_key(event: "MessageEvent", platform_name: str) -> Optional[str]:
    source = getattr(event, "source", None)
    message_id = getattr(event, "message_id", None)
    if not source or not message_id:
        return None
    chat_id = getattr(source, "chat_id", None) or "?"
    return f"{platform_name or '?'}|{chat_id}|{message_id}"


def _evict_oldest_locked() -> None:
    # Caller must hold ``_decision_cache_lock``.
    if not _decision_cache:
        return
    drop_count = max(1, _CACHE_MAX // 4)
    items = sorted(_decision_cache.items(), key=lambda kv: kv[1][1])
    for key, _ in items[:drop_count]:
        _decision_cache.pop(key, None)


async def _classify(
    event: "MessageEvent",
    config: RelevanceFilterConfig,
    *,
    bot_name: str,
    platform_name: str,
) -> bool:
    """Call the auxiliary LLM. Returns ``True`` if RELEVANT, ``False`` if IGNORE.

    Raises on transport errors so the caller can decide to fail-open.
    """
    try:
        from agent.auxiliary_client import get_async_text_auxiliary_client
    except ImportError:
        logger.debug(
            "[relevance_filter] auxiliary_client module not importable; passing through"
        )
        return True

    client, default_model = get_async_text_auxiliary_client(task="relevance_filter")
    if client is None:
        logger.debug(
            "[relevance_filter] no auxiliary LLM configured; passing through"
        )
        return True

    model = (config.model or "").strip() or (default_model or "")
    if not model:
        logger.debug(
            "[relevance_filter] no model slug available; passing through"
        )
        return True

    source = getattr(event, "source", None)
    sender = "unknown"
    if source is not None:
        sender = (
            getattr(source, "user_name", None)
            or getattr(source, "user_id", None)
            or "unknown"
        )

    context = (getattr(event, "channel_context", None) or "").strip()
    if len(context) > 1500:
        context = "…" + context[-1500:]
    if not context:
        context = "(no prior thread context available)"

    message_excerpt = (getattr(event, "text", "") or "").strip()
    if len(message_excerpt) > 1500:
        message_excerpt = message_excerpt[:1500] + "…"

    # Slack / Telegram / Discord adapters set ``reply_to_text`` to the
    # thread parent (or replied-to message) before gateway.run prepends a
    # ``[Replying to: "..."]`` prefix to the user text.  At filter time
    # that injection hasn't happened yet, so we surface the parent
    # explicitly to the classifier — without it, "Hey John, can you help?"
    # looks identical inside a bot thread and a human-to-human thread.
    reply_excerpt = (getattr(event, "reply_to_text", "") or "").strip()
    if len(reply_excerpt) > 600:
        reply_excerpt = reply_excerpt[:600] + "…"
    reply_block = (
        f"This message is a reply to (parent of the thread):\n"
        f"---\n{reply_excerpt}\n---\n"
        if reply_excerpt
        else ""
    )

    system_prompt = CLASSIFIER_SYSTEM_PROMPT
    user_prompt = CLASSIFIER_USER_TEMPLATE.format(
        bot_name=bot_name or "the assistant",
        platform=platform_name or "?",
        context=context,
        reply_block=reply_block,
        sender=sender,
        message=message_excerpt,
    )

    response = await asyncio.wait_for(
        client.chat.completions.create(
            model=model,
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
            max_tokens=4,
            temperature=0.0,
        ),
        timeout=_CLASSIFIER_TIMEOUT_SECONDS,
    )

    raw = ""
    try:
        raw = (response.choices[0].message.content or "").strip().upper()
    except (AttributeError, IndexError, TypeError):
        raw = ""

    return not raw.startswith("IGNORE")


def reset_cache() -> None:
    """Drop all cached classifier decisions. For tests."""
    _decision_cache.clear()


def _reset_for_tests() -> None:
    """Reset all module-level state. For tests only."""
    global _active_config, _config_initialized
    _active_config = RelevanceFilterConfig()
    _config_initialized = False
    _decision_cache.clear()
