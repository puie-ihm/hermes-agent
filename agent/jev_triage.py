"""Jev (TypeSafe System One) triage for Slack thread follow-ups.

Why this exists: with ``slack.thread_followup_mode=agent``, LaHermes ran a full
LLM turn in order to decide nothing more than reply / react / silent. Measured
2026-09-19: 1 API call, $0.002-0.005 and 1-5s of model time per follow-up, and
the decision step was also where unrelated memory got pulled into the answer.
Jev answers the same question with a typed classifier call: ~0.9s from Thailand,
~$0.00002, and no retrieval — a classifier with an explicit rubric cannot decide
to summarise a different conversation.

Fail-open by design, and that is the safety property that matters here: no key,
transport error, timeout, malformed body, low confidence, or an unexpected
choice all return ``action=None``, which means "fall through to the previous
behaviour" (the model triage turn). Jev can therefore never silence the bot.

Enablement: the profile must have ``TYPESAFE_API_KEY`` in its environment and
must not set ``JEV_TRIAGE_DISABLED``. This is wired for the `default` (LaHermes)
profile only; other lanes stay on the model path.
"""

from __future__ import annotations

import json
import logging
import os
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Any, Dict, Optional

logger = logging.getLogger(__name__)

JEV_ENDPOINT = "https://api.typesafe.ai/v1/systemone"
# Pinned, not `jev-latest`: the confidence floor below is tuned against a
# version, and TypeSafe states that the alias moves.
JEV_MODEL = "jev-1.13.0"

DEFAULT_TIMEOUT_S = 1.5
DEFAULT_CONFIDENCE_FLOOR = 0.6
DEFAULT_REACT_EMOJI = "+1"

QUESTION_ID = "response"

# Three independent questions over the same state, combined in code. TypeSafe's
# guidance is to ask one judgment per question and compose the answers yourself
# ("speculative fan-out" + "composite scoring"), and it measured better here:
# a single how-should-you-respond question scored 60% against today's labelled
# decisions and, worse, answered `react`/`reply` for side conversations between
# other people -- the exact noise this was meant to remove. The fan-out version
# scored 70% and every miss was "fall through to the model", i.e. no behaviour
# change, so its errors are inert instead of loud.
#
# Rubric notes, from the cases that actually went wrong on 2026-09-19:
#   * "LaHermes ช่วยบอกอีกที..." -> reply (addressed by name)
#   * "ให้พี่ปุ้ยช่วยเช็คให้หน่อย" -> silent (a name that is not LaHermes is
#     another person, even though the message asks that person to do something)
#   * "ขอบคุณครับ" / "รับทราบ ขอบคุณครับ" -> react (acknowledgement)
#   * "เดี๋ยวผมคุยกับทีมก่อนนะ" -> silent (status note, nobody is waiting)
# Criteria describe situations rather than degrees, and every choice carries an
# `unclear` / `other` escape hatch so the model is never forced onto a wrong
# option.
QUESTIONS: Dict[str, Any] = {
    "directed_at": {
        "type": "choice",
        "instructions": (
            "In this Slack thread, who is `message` addressed to? The assistant is "
            "called LaHermes."
        ),
        "criteria": {
            "assistant": (
                "It addresses the assistant: by the name LaHermes, as @bot, or as an "
                "answer to something the assistant asked."
            ),
            "another_person": (
                "It addresses a different person. Any name or @mention that is not "
                "LaHermes is another person, including when the message asks that "
                "person to do something. Situation: a colleague asks another "
                "colleague to check something."
            ),
            "everyone": (
                "Addressed to the thread or to nobody in particular. Situation: a "
                "general note."
            ),
            "unclear": "Cannot tell who is being addressed.",
        },
    },
    "request_type": {
        "type": "choice",
        "instructions": "What kind of message is `message`?",
        "criteria": {
            "question_or_task": "It asks something or requests work.",
            "acknowledgement": (
                "It only acknowledges something already done. Situation: thanks, "
                "noted, ok, received, done."
            ),
            "status_or_note": (
                "It reports status or informs without asking for anything. "
                "Situation: I will do that later, I sent the file."
            ),
            "other": "None of the above.",
        },
    },
    "needs_answer": {
        "type": "noul",
        "instructions": "Does `message` require the assistant to produce an answer?",
        "criteria": {
            "true": "The assistant is expected to reply with information or work.",
            "false": (
                "Nobody is waiting for the assistant, even if other people are talking."
            ),
        },
    },
}


@dataclass(frozen=True)
class TriageDecision:
    """``action`` None means "no confident decision — use the previous path"."""

    action: Optional[str] = None
    confidence: Optional[float] = None
    reason: str = ""
    latency_ms: int = 0
    tokens: Optional[int] = None


def _api_key() -> str:
    """Resolve the TypeSafe key the way the rest of the fork resolves credentials.

    ``os.environ`` is NOT enough here. The gateway multiplexes profiles and
    installs a per-turn secret scope; ``agent.secret_scope.get_secret`` documents
    that in that mode the scope is authoritative and ``os.environ`` is not
    consulted, because it may hold another profile's value (the framework also
    withholds credentials from ``os.environ`` for the same reason). Reading the
    environment directly made ``is_enabled()`` return False in the running
    gateway while it returned True in a shell that had loaded ``~/.hermes/.env``
    itself -- the Jev path was silently skipped and every follow-up kept running
    a full LLM turn, with no log line to show for it.
    """
    try:
        from agent.secret_scope import get_secret

        value = get_secret("TYPESAFE_API_KEY")
        if value:
            return str(value).strip()
    except Exception:
        pass
    return (os.environ.get("TYPESAFE_API_KEY") or "").strip()


def _env_truthy(name: str, default: bool = True) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() not in {"false", "0", "no", "off", ""}


def is_enabled() -> bool:
    """True only when a key is present and the profile has not disabled it."""
    if not _api_key():
        return False
    return _env_truthy("JEV_TRIAGE_ENABLED", default=True) and not os.getenv(
        "JEV_TRIAGE_DISABLED", ""
    ).strip()


def react_emoji() -> str:
    return (os.getenv("JEV_TRIAGE_REACT_EMOJI", "") or DEFAULT_REACT_EMOJI).strip()


def _floor() -> float:
    try:
        return float(os.getenv("JEV_TRIAGE_CONFIDENCE_FLOOR", "") or DEFAULT_CONFIDENCE_FLOOR)
    except ValueError:
        return DEFAULT_CONFIDENCE_FLOOR


def _timeout() -> float:
    try:
        return float(os.getenv("JEV_TRIAGE_TIMEOUT_S", "") or DEFAULT_TIMEOUT_S)
    except ValueError:
        return DEFAULT_TIMEOUT_S


def decide_followup(
    message: str,
    *,
    thread_context: str = "",
    bot_name: str = "LaHermes",
    timeout_s: Optional[float] = None,
    confidence_floor: Optional[float] = None,
) -> TriageDecision:
    """Ask Jev how to respond to a follow-up. Never raises."""
    if not is_enabled():
        return TriageDecision(reason="disabled")

    text = (message or "").strip()
    if not text:
        return TriageDecision(reason="empty_message")

    state: Dict[str, Any] = {"message": text, "assistant_name": bot_name}
    ctx = (thread_context or "").strip()
    if ctx:
        state["thread_context"] = ctx

    body = json.dumps({"model": JEV_MODEL, "state": state, "questions": QUESTIONS}).encode()
    request = urllib.request.Request(
        JEV_ENDPOINT,
        data=body,
        headers={
            "Authorization": "Bearer " + _api_key(),
            "content-type": "application/json",
            # TypeSafe's edge rejects generic SDK user agents.
            "User-Agent": "ihm-lahermes/1.0",
        },
    )

    started = time.monotonic()
    try:
        with urllib.request.urlopen(request, timeout=timeout_s or _timeout()) as response:
            payload = json.loads(response.read())
    except urllib.error.HTTPError as exc:
        return TriageDecision(
            reason=f"http_{exc.code}", latency_ms=int((time.monotonic() - started) * 1000)
        )
    except Exception as exc:  # timeout, DNS, TLS, bad JSON — all fall through
        return TriageDecision(
            reason=f"{type(exc).__name__.lower()}",
            latency_ms=int((time.monotonic() - started) * 1000),
        )

    latency_ms = int((time.monotonic() - started) * 1000)
    usage = payload.get("usage") or {}
    tokens = usage.get("input_tokens")

    try:
        answers = payload.get("answers") or {}
        directed = answers.get("directed_at") or {}
        request = answers.get("request_type") or {}
        needs = answers.get("needs_answer") or {}
        directed_choice = str(directed.get("choice") or "").strip().lower()
        request_choice = str(request.get("choice") or "").strip().lower()
        directed_conf = directed.get("confidence")
        directed_conf = float(directed_conf) if isinstance(directed_conf, (int, float)) else 0.0
        request_conf = request.get("confidence")
        request_conf = float(request_conf) if isinstance(request_conf, (int, float)) else 0.0
        needs_prob = needs.get("noul")
        needs_prob = float(needs_prob) if isinstance(needs_prob, (int, float)) else None
    except Exception:
        return TriageDecision(
            reason="unparseable_answer", latency_ms=latency_ms, tokens=tokens
        )

    floor = _floor() if confidence_floor is None else confidence_floor
    detail = f"directed_at={directed_choice or '?'}({directed_conf:.2f})"

    # 1. Addressed to somebody who is not the assistant: stay out of it, even
    #    when the message asks that person to do work. This is the case the
    #    single-question rubric got wrong in the loud direction (it answered
    #    react/reply for other people's conversations).
    if directed_choice == "another_person" and directed_conf >= floor:
        return TriageDecision(
            action="silent", confidence=directed_conf, reason=f"ok;{detail}",
            latency_ms=latency_ms, tokens=tokens,
        )

    # 2. Addressed to the assistant and waiting for it: let the model answer.
    #    Returned as no-action on purpose — `reply` is the previous behaviour, so
    #    there is nothing to short-circuit and no state to keep in sync.
    if (
        directed_choice == "assistant"
        and directed_conf >= floor
        and (needs_prob or 0.0) >= 0.6
    ):
        return TriageDecision(
            reason=f"choice=reply;{detail}", latency_ms=latency_ms,
            confidence=min(directed_conf, needs_prob or 0.0), tokens=tokens,
        )

    # 3. A finished acknowledgement: one emoji, no turn.
    if (
        request_choice == "acknowledgement"
        and request_conf >= floor
        and not (directed_choice == "assistant" and (needs_prob or 0.0) >= 0.6)
    ):
        return TriageDecision(
            action="react", confidence=request_conf,
            reason=f"ok;{detail};request_type=acknowledgement",
            latency_ms=latency_ms, tokens=tokens,
        )

    # 4. A status note that is not addressed to the assistant: nobody is asking
    #    it anything, so stay out of it ("เดี๋ยวผมคุยกับทีมก่อนนะ"). Guarded on
    #    `directed_at != assistant` so "I sent the file you asked for" still goes
    #    to the model. Measured on the labelled set: +1 correct, +1 turn saved,
    #    no new false silence.
    if (
        request_choice == "status_or_note"
        and request_conf >= floor
        and directed_choice != "assistant"
    ):
        return TriageDecision(
            action="silent", confidence=request_conf,
            reason=f"ok;{detail};request_type=status_or_note",
            latency_ms=latency_ms, tokens=tokens,
        )

    # 5. Nothing anywhere is waiting on the assistant: stay silent.
    if needs_prob is not None and needs_prob <= 0.15:
        return TriageDecision(
            action="silent", confidence=round(1.0 - needs_prob, 4),
            reason=f"ok;needs_answer={needs_prob:.2f}", latency_ms=latency_ms, tokens=tokens,
        )

    # Anything else falls through to the model triage — including every case the
    # rubric is unsure about, which is what keeps this fail-open in practice.
    return TriageDecision(
        reason=f"no_confident_action;{detail}",
        confidence=directed_conf or None, latency_ms=latency_ms, tokens=tokens,
    )
