"""Jev triage must be fail-open, and must act only on a confident silent/react.

The dangerous direction is a false silence: Jev deciding "silent" for a message
that needed an answer means the bot ignores the user, with no error anywhere. So
the tests below spend most of their weight on the paths that must NOT act:
low confidence, errors, timeouts, malformed bodies, missing key, and the two
choices that mean "let the model handle it" (reply / other).
"""

import json
import sys
from unittest.mock import MagicMock

import pytest

sys.path.insert(0, ".")

from agent import jev_triage  # noqa: E402


def _fake_response(payload, status=200):
    class _Resp:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def read(self):
            return json.dumps(payload).encode()

    return _Resp()


@pytest.fixture(autouse=True)
def _key(monkeypatch):
    monkeypatch.setenv("TYPESAFE_API_KEY", "apik_test")
    monkeypatch.delenv("JEV_TRIAGE_DISABLED", raising=False)
    monkeypatch.delenv("JEV_TRIAGE_ENABLED", raising=False)
    monkeypatch.delenv("JEV_TRIAGE_CONFIDENCE_FLOOR", raising=False)
    monkeypatch.delenv("JEV_TRIAGE_TIMEOUT_S", raising=False)


def _answers(choice, confidence, *, directed="assistant", needs=0.9):
    """Fan-out answer set. `choice` now means the composed action's source:
    a direct `another_person` routing, an acknowledgement, or a reply."""
    answers = {
        "directed_at": {"type": "choice", "choice": directed, "confidence": confidence,
                        "probabilities": {directed: confidence}},
        "request_type": {"type": "choice", "choice": "question_or_task", "confidence": 0.9,
                         "probabilities": {"question_or_task": 0.9}},
        "needs_answer": {"type": "noul", "noul": needs},
    }
    if choice == "react":
        answers["request_type"] = {"type": "choice", "choice": "acknowledgement",
                                   "confidence": confidence,
                                   "probabilities": {"acknowledgement": confidence}}
    if choice == "silent":
        answers["directed_at"] = {"type": "choice", "choice": "another_person",
                                  "confidence": confidence,
                                  "probabilities": {"another_person": confidence}}
    return {"model": "jev-1.13.0", "answers": answers,
            "usage": {"input_tokens": 321, "output_tokens": 9}}


# ---- the paths that must act ----------------------------------------------

def test_confident_silent_acts(monkeypatch):
    monkeypatch.setattr(jev_triage.urllib.request, "urlopen",
                        lambda *a, **k: _fake_response(_answers("silent", 0.93)))
    d = jev_triage.decide_followup("ให้พี่ปุ้ยช่วยเช็คให้หน่อย")   # another person
    assert d.action == "silent" and d.confidence == 0.93 and d.tokens == 321


def test_confident_react_acts(monkeypatch):
    # needs_answer low: a pure acknowledgement, nobody is waiting on the bot
    monkeypatch.setattr(jev_triage.urllib.request, "urlopen",
                        lambda *a, **k: _fake_response(_answers("react", 0.92, needs=0.2)))
    d = jev_triage.decide_followup("ขอบคุณครับ")
    assert d.action == "react"


# ---- the paths that must NOT act -----------------------------------------

def test_low_confidence_falls_through(monkeypatch):
    monkeypatch.setattr(jev_triage.urllib.request, "urlopen",
                        lambda *a, **k: _fake_response(_answers("silent", 0.31)))
    d = jev_triage.decide_followup("ข้อความทดสอบ 1")
    assert d.action is None and "no_confident_action" in d.reason


def test_reply_falls_through_to_the_model(monkeypatch):
    monkeypatch.setattr(jev_triage.urllib.request, "urlopen",
                        lambda *a, **k: _fake_response(_answers("reply", 0.99)))
    d = jev_triage.decide_followup("LaHermes ช่วยสรุปให้หน่อย")
    assert d.action is None and "choice=reply" in d.reason


def test_other_choice_falls_through(monkeypatch):
    payload = _answers("reply", 0.99)
    payload["answers"]["directed_at"] = {"type": "choice", "choice": "unclear",
                                         "confidence": 0.99}
    monkeypatch.setattr(jev_triage.urllib.request, "urlopen",
                        lambda *a, **k: _fake_response(payload))
    assert jev_triage.decide_followup("???").action is None


def test_http_error_falls_through(monkeypatch):
    import urllib.error

    def _boom(*a, **k):
        raise urllib.error.HTTPError("u", 429, "rate", {}, None)

    monkeypatch.setattr(jev_triage.urllib.request, "urlopen", _boom)
    d = jev_triage.decide_followup("hello")
    assert d.action is None and d.reason == "http_429"


def test_timeout_falls_through(monkeypatch):
    def _boom(*a, **k):
        raise TimeoutError("slow")

    monkeypatch.setattr(jev_triage.urllib.request, "urlopen", _boom)
    d = jev_triage.decide_followup("hello")
    assert d.action is None and "timeout" in d.reason.lower()


def test_malformed_body_falls_through(monkeypatch):
    class _Bad:
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def read(self): return b"not json"

    monkeypatch.setattr(jev_triage.urllib.request, "urlopen", lambda *a, **k: _Bad())
    assert jev_triage.decide_followup("hello").action is None


def test_missing_confidence_falls_through(monkeypatch):
    payload = {"answers": {"directed_at": {"type": "choice", "choice": "another_person"},
                           "request_type": {"type": "choice", "choice": "status_or_note"},
                           "needs_answer": {"type": "noul", "noul": 0.4}},
               "usage": {"input_tokens": 10}}
    monkeypatch.setattr(jev_triage.urllib.request, "urlopen",
                        lambda *a, **k: _fake_response(payload))
    d = jev_triage.decide_followup("hello")
    assert d.action is None and "no_confident_action" in d.reason


def test_no_key_is_disabled(monkeypatch):
    monkeypatch.delenv("TYPESAFE_API_KEY", raising=False)
    assert jev_triage.is_enabled() is False
    assert jev_triage.decide_followup("hello").action is None


def test_profile_can_disable(monkeypatch):
    monkeypatch.setenv("JEV_TRIAGE_DISABLED", "1")
    assert jev_triage.is_enabled() is False


def test_floor_is_configurable(monkeypatch):
    monkeypatch.setenv("JEV_TRIAGE_CONFIDENCE_FLOOR", "0.95")
    monkeypatch.setattr(jev_triage.urllib.request, "urlopen",
                        lambda *a, **k: _fake_response(_answers("silent", 0.9)))
    assert jev_triage.decide_followup("x").action is None


def test_empty_message_does_not_call_out(monkeypatch):
    called = {"n": 0}

    def _urlopen(*a, **k):
        called["n"] += 1
        return _fake_response(_answers("silent", 0.99))

    monkeypatch.setattr(jev_triage.urllib.request, "urlopen", _urlopen)
    assert jev_triage.decide_followup("   ").action is None
    assert called["n"] == 0


# ---- request shape --------------------------------------------------------

def test_request_uses_pinned_model_and_rubric(monkeypatch):
    seen = {}

    def _urlopen(request, **kwargs):
        seen["url"] = request.full_url
        seen["body"] = json.loads(request.data)
        seen["ua"] = request.get_header("User-agent")
        return _fake_response(_answers("reply", 0.9))

    monkeypatch.setattr(jev_triage.urllib.request, "urlopen", _urlopen)
    jev_triage.decide_followup("hi", thread_context="Puie: earlier context")
    assert seen["url"] == "https://api.typesafe.ai/v1/systemone"
    assert seen["body"]["model"] == "jev-1.13.0"          # pinned, not jev-latest
    assert seen["body"]["state"]["thread_context"].startswith("Puie:")
    q = seen["body"]["questions"]
    assert set(q) == {"directed_at", "request_type", "needs_answer"}   # fan-out
    assert "unclear" in q["directed_at"]["criteria"]                   # escape hatches
    assert "other" in q["request_type"]["criteria"]
    assert seen["ua"] == "ihm-lahermes/1.0"


# ---- credential resolution -------------------------------------------------

def test_key_is_read_from_the_secret_scope(monkeypatch):
    """In the multiplexed gateway os.environ does not hold credentials; the
    per-turn secret scope does. Reading the environment directly made
    is_enabled() False in the running gateway (Jev silently skipped)."""
    monkeypatch.delenv("TYPESAFE_API_KEY", raising=False)
    import agent.secret_scope as scope

    monkeypatch.setattr(scope, "get_secret", lambda name, default=None: "apik_from_scope")
    assert jev_triage.is_enabled() is True


def test_scope_value_is_used_for_auth(monkeypatch):
    monkeypatch.delenv("TYPESAFE_API_KEY", raising=False)
    import agent.secret_scope as scope

    monkeypatch.setattr(scope, "get_secret", lambda name, default=None: "apik_from_scope")
    seen = {}

    def _urlopen(request, **kwargs):
        seen["auth"] = request.get_header("Authorization")
        return _fake_response(_answers("silent", 0.9))

    monkeypatch.setattr(jev_triage.urllib.request, "urlopen", _urlopen)
    jev_triage.decide_followup("x")
    assert seen["auth"] == "Bearer apik_from_scope"


def test_env_still_works_as_a_fallback(monkeypatch):
    import agent.secret_scope as scope

    monkeypatch.setattr(scope, "get_secret", lambda name, default=None: None)
    monkeypatch.setenv("TYPESAFE_API_KEY", "apik_from_env")
    assert jev_triage.is_enabled() is True


# ---- the composed decision (fan-out) --------------------------------------

def test_another_person_silences_even_when_that_person_is_asked_to_work(monkeypatch):
    """The case the single-question rubric got wrong in the loud direction:
    a colleague asking a colleague to check something is not our business."""
    monkeypatch.setattr(jev_triage.urllib.request, "urlopen",
                        lambda *a, **k: _fake_response(_answers("silent", 0.98, needs=0.9)))
    d = jev_triage.decide_followup("ให้พี่ปุ้ยช่วยเช็คให้หน่อยได้ไหม")
    assert d.action == "silent" and d.confidence == 0.98


def test_addressed_to_assistant_replies_via_the_model(monkeypatch):
    monkeypatch.setattr(jev_triage.urllib.request, "urlopen",
                        lambda *a, **k: _fake_response(
                            _answers("reply", 0.96, directed="assistant", needs=0.95)))
    d = jev_triage.decide_followup("บอทช่วยสรุปยอด WMBR ให้หน่อย")
    assert d.action is None and "choice=reply" in d.reason


def test_low_needs_answer_silences(monkeypatch):
    monkeypatch.setattr(jev_triage.urllib.request, "urlopen",
                        lambda *a, **k: _fake_response(
                            _answers("reply", 0.9, directed="everyone", needs=0.05)))
    d = jev_triage.decide_followup("เดี๋ยวผมคุยกับทีมก่อนนะ")
    assert d.action == "silent"


def test_acknowledgement_addressed_to_the_assistant_prefers_reply(monkeypatch):
    """"thanks, can you also send the CSV?" is a request, not an ack."""
    payload = _answers("react", 0.99)
    payload["answers"]["directed_at"] = {"type": "choice", "choice": "assistant",
                                         "confidence": 0.99}
    payload["answers"]["needs_answer"] = {"type": "noul", "noul": 0.9}
    monkeypatch.setattr(jev_triage.urllib.request, "urlopen",
                        lambda *a, **k: _fake_response(payload))
    d = jev_triage.decide_followup("ขอบคุณครับ แล้วช่วยส่ง CSV ด้วย")
    assert d.action is None


def test_status_note_to_the_thread_silences(monkeypatch):
    """'เดี๋ยวผมคุยกับทีมก่อนนะ' — a status note to the thread, nobody is asking.
    Measured: this rule took the labelled set from 8/10 to 9/10 correct and
    turns saved from 4 to 5, with no new false silence."""
    payload = _answers("reply", 0.9, directed="everyone", needs=0.4)
    payload["answers"]["request_type"] = {"type": "choice", "choice": "status_or_note",
                                          "confidence": 0.88}
    monkeypatch.setattr(jev_triage.urllib.request, "urlopen",
                        lambda *a, **k: _fake_response(payload))
    d = jev_triage.decide_followup("เดี๋ยวผมคุยกับทีมก่อนนะ แล้วจะกลับมา")
    assert d.action == "silent" and d.confidence == 0.88


def test_status_note_to_the_assistant_still_reaches_the_model(monkeypatch):
    """"I sent the file you asked for" must not be silenced."""
    payload = _answers("reply", 0.95, directed="assistant", needs=0.8)
    payload["answers"]["request_type"] = {"type": "choice", "choice": "status_or_note",
                                          "confidence": 0.9}
    monkeypatch.setattr(jev_triage.urllib.request, "urlopen",
                        lambda *a, **k: _fake_response(payload))
    assert jev_triage.decide_followup("ผมส่งไฟล์ที่พี่ขอแล้วนะ").action is None
