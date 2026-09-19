"""Auto-extraction must not store conversation scaffolding as memory.

Measured regression (2026-09-19): _auto_extract_facts stored the raw first 400
chars of a user message, which the Slack adapter had prefixed with thread
context (a "[Replying to: ...]" quote plus a "[Thread context - prior messages
in this thread ...]" block) and author labels. 53 of 160 facts contained
"[Thread context", 83 contained "[Replying to:", and an ambiguous "what did we
conclude earlier" question was answered with an unrelated conversation because
those rows matched.

Also: "I want/need ..." are work requests in this workspace, not preferences,
and author labels. 53 of 160 facts contained "[Thread context", 83 contained
"[Replying to:", and an ambiguous "what did we conclude earlier" question was
answered with an unrelated conversation because those rows matched.

Also: `I want/need …` are work requests in this workspace, not preferences, but
they were tagged user_pref (117 of 160 facts were user_pref, mostly requests).
"""

import sys
from unittest.mock import MagicMock

import pytest

sys.path.insert(0, ".")

from plugins.memory.holographic import (  # noqa: E402
    _is_storable_memory_text,
    _strip_leading_author_prefix,
)

# The exact shape found in the store.
SCAFFOLDED = (
    '[Replying to: "WMBR 082426"]  [Thread context — prior messages in this '
    "thread (not yet in conversation history):] [Puie] I always want the CSV "
    "as a code block"
)


def test_thread_context_is_rejected():
    assert _is_storable_memory_text(SCAFFOLDED) is False


def test_reply_quote_is_rejected():
    assert _is_storable_memory_text('[Replying to: "x"] I prefer charts without pie') is False


def test_new_assistant_thread_marker_is_rejected():
    assert _is_storable_memory_text("[Replying to: \"New Assistant Thread\"] I always use EST") is False


def test_plain_preference_is_storable():
    assert _is_storable_memory_text("I prefer charts without pie or donuts") is True


def test_short_or_empty_text_is_rejected():
    assert _is_storable_memory_text("") is False
    assert _is_storable_memory_text("short") is False


def test_author_prefix_is_stripped():
    assert _strip_leading_author_prefix("[Agung] I prefer v1 rows") == "I prefer v1 rows"


def test_repeated_author_prefixes_are_stripped():
    assert _strip_leading_author_prefix("[Puie] [Agung] I always use EST") == "I always use EST"


def test_stripping_author_prefix_then_storing():
    cleaned = _strip_leading_author_prefix("[Agung] I prefer the v1 format")
    assert _is_storable_memory_text(cleaned) is True
    assert cleaned == "I prefer the v1 format"


# ---- the extractor itself -------------------------------------------------

def _make_provider(store):
    from plugins.memory.holographic import HolographicMemoryProvider

    p = object.__new__(HolographicMemoryProvider)
    p._config = {"auto_extract": "true"}
    p._store = store
    return p


def test_requests_are_not_stored_as_preferences():
    store = MagicMock()
    _make_provider(store)._auto_extract_facts([
        {"role": "user", "content": "I need to know my total profits from all ad accounts that starts with UH"},
        {"role": "user", "content": "I want to create new labels. here the columns: label name, affiliate id"},
    ])
    assert store.add_fact.call_count == 0


def test_real_preference_is_stored():
    store = MagicMock()
    _make_provider(store)._auto_extract_facts([
        {"role": "user", "content": "I prefer the CSV posted as a code block rather than a file"},
    ])
    assert store.add_fact.call_count == 1
    # add_fact is called with category= as a keyword
    assert store.add_fact.call_args.kwargs.get("category") == "user_pref"
    assert "I prefer the CSV" in store.add_fact.call_args.args[0]


def test_scaffolded_message_is_not_stored():
    store = MagicMock()
    _make_provider(store)._auto_extract_facts([{"role": "user", "content": SCAFFOLDED}])
    assert store.add_fact.call_count == 0


def test_assistant_messages_are_ignored():
    store = MagicMock()
    _make_provider(store)._auto_extract_facts([
        {"role": "assistant", "content": "I prefer to answer briefly"},
    ])
    assert store.add_fact.call_count == 0
