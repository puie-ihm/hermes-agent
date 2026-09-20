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

# Rubric notes, from the cases that actually went wrong on 2026-09-19:
#   * "LaHermes ช่วยบอกอีกที..." -> reply (addressed by name)
#   * "ให้พี่ปุ้ยช่วยเช็คให้หน่อย" -> silent (addressed to another person by name)
#   * "ขอบคุณครับ" / "รับทราบ ขอบคุณครับ" -> react (acknowledgement, not a task)
#   * "เดี๋ยวผมคุยกับทีมก่อนนะ" -> silent (side conversation)
# Criteria describe situations rather than degrees, and `other` is the escape
# hatch: without it the model is forced to pick a wrong option.
QUESTIONS: Dict[str, Any] = {
    QUESTION_ID: {
        "type": "choice",
        "instructions": (
            "How should the assistant respond to `message`, given `thread_context`? "
            "The assistant is already a participant in this Slack thread and has "
            "already replied at least once."
        ),
        "criteria": {
            "reply": (
                "The message asks the assistant something, addresses it by name or "
                "as @bot, requests data/report/work, or is a follow-up that clearly "
                "needs the assistant's answer."
            ),
            "react": (
                "The message is a completed acknowledgement or status note (thanks, "
                "noted, ok, done, received) that needs a light acknowledgement and "
                "no answer."
            ),
            "silent": (
                "The message is between other people — addressed to another person "
                "by name, or continuing a side conversation — or adds nothing the "
                "assistant could usefully contribute."
            ),
            "other": "None of the above is a clear fit.",
        },
    }
}


@dataclass(frozen=True)
class TriageDecision:
    """``action`` None means "no confident decision — use the previous path"."""

    action: Optional[str] = None
    confidence: Optional[float] = None
    reason: str = ""
    latency_ms: int = 0
    tokens: Optional[int] = None


def _env_truthy(name: str, default: bool = True) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() not in {"false", "0", "no", "off", ""}


def is_enabled() -> bool:
    """True only when a key is present and the profile has not disabled it."""
    if not os.getenv("TYPESAFE_API_KEY", "").strip():
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
            "Authorization": "Bearer " + os.environ["TYPESAFE_API_KEY"].strip(),
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
        answer = (payload.get("answers") or {}).get(QUESTION_ID) or {}
        choice = str(answer.get("choice") or "").strip().lower()
        confidence = answer.get("confidence")
        confidence = float(confidence) if isinstance(confidence, (int, float)) else None
    except Exception:
        return TriageDecision(
            reason="unparseable_answer", latency_ms=latency_ms, tokens=tokens
        )

    floor = _floor() if confidence_floor is None else confidence_floor
    if choice not in {"silent", "react"}:
        # `reply` and `other` both mean "let the model handle it": reply is the
        # previous behaviour, and `other` is the rubric's escape hatch.
        return TriageDecision(
            reason=f"choice={choice or 'missing'}", latency_ms=latency_ms,
            confidence=confidence, tokens=tokens,
        )
    if confidence is None:
        return TriageDecision(
            reason="no_confidence", latency_ms=latency_ms, tokens=tokens
        )
    if confidence < floor:
        return TriageDecision(
            reason=f"below_floor({confidence:.2f}<{floor:.2f})",
            confidence=confidence, latency_ms=latency_ms, tokens=tokens,
        )
    return TriageDecision(
        action=choice, confidence=confidence, reason="ok", latency_ms=latency_ms, tokens=tokens
    )
