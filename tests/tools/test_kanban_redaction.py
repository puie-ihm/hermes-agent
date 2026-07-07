"""Tests: redact_sensitive_text is applied in kanban tool handlers.

Verifies that secrets embedded in kanban_comment body, kanban_complete
summary/result/metadata, and kanban_block reason are masked before the
values reach the DB.  Uses the same worker_env fixture pattern as
test_kanban_tools.py.
"""
from __future__ import annotations

import json

import pytest


# ---------------------------------------------------------------------------
# Shared fixture — mirrors test_kanban_tools.py
# ---------------------------------------------------------------------------

@pytest.fixture
def worker_env(monkeypatch, tmp_path):
    """Isolated HERMES_HOME with a running task; returns the task id."""
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setenv("HERMES_PROFILE", "test-worker")
    monkeypatch.delenv("HERMES_SESSION_ID", raising=False)
    from pathlib import Path as _Path
    monkeypatch.setattr(_Path, "home", lambda: tmp_path)

    from hermes_cli import kanban_db as kb
    kb._INITIALIZED_PATHS.clear()
    kb.init_db()
    conn = kb.connect()
    try:
        tid = kb.create_task(conn, title="worker-test", assignee="test-worker")
        kb.claim_task(conn, tid)
    finally:
        conn.close()
    monkeypatch.setenv("HERMES_KANBAN_TASK", tid)
    return tid


# ---------------------------------------------------------------------------
# Positive tests — secrets are masked
# ---------------------------------------------------------------------------

def test_kanban_comment_body_scrubbed_github_pat(worker_env):
    """ghp_ PAT in comment body must be masked before DB write."""
    from tools import kanban_tools as kt
    from hermes_cli import kanban_db as kb
    secret = "ghp_" + "A" * 40
    kt._handle_comment({"task_id": worker_env, "body": f"token: {secret}"})
    conn = kb.connect()
    try:
        comments = kb.list_comments(conn, worker_env)
    finally:
        conn.close()
    assert comments, "expected at least one comment"
    stored = comments[-1].body
    assert secret not in stored
    assert stored  # something was stored


def test_kanban_comment_body_scrubbed_openai_key(worker_env):
    """sk- key in comment body must be masked before DB write."""
    from tools import kanban_tools as kt
    from hermes_cli import kanban_db as kb
    secret = "sk-" + "A" * 48
    kt._handle_comment({"task_id": worker_env, "body": f"key={secret}"})
    conn = kb.connect()
    try:
        comments = kb.list_comments(conn, worker_env)
    finally:
        conn.close()
    stored = comments[-1].body
    assert secret not in stored


def test_kanban_complete_summary_scrubbed(worker_env):
    """sk-ant- key in summary must be masked before DB write."""
    from tools import kanban_tools as kt
    from hermes_cli import kanban_db as kb
    secret = "sk-ant-" + "A" * 40
    kt._handle_complete({"summary": f"done, key={secret}"})
    conn = kb.connect()
    try:
        run = kb.latest_run(conn, worker_env)
    finally:
        conn.close()
    assert run is not None
    stored = run.summary or ""
    assert secret not in stored


def test_kanban_complete_metadata_scrubbed(worker_env):
    """Token in metadata dict must be masked in JSON stored in DB."""
    from tools import kanban_tools as kt
    from hermes_cli import kanban_db as kb
    secret = "ghp_" + "B" * 40
    metadata = {"token": secret, "count": 5}
    kt._handle_complete({"summary": "done", "metadata": metadata})
    conn = kb.connect()
    try:
        run = kb.latest_run(conn, worker_env)
    finally:
        conn.close()
    assert run is not None
    # metadata is stored on the run; serialize to catch any nesting
    meta_raw = json.dumps(run.metadata) if run.metadata else "{}"
    assert secret not in meta_raw


def test_kanban_block_reason_scrubbed_jwt(worker_env):
    """JWT in block reason must be masked before DB write."""
    from tools import kanban_tools as kt
    from hermes_cli import kanban_db as kb
    # Minimal valid-ish JWT (header.payload.sig)
    jwt = (
        "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9"
        ".eyJzdWIiOiIxMjM0NTY3ODkwIn0"
        ".dozjgNryP4J3jVmNHl0w5N_5NjP1-iXkpHgcth826Iw"
    )
    kt._handle_block({"reason": f"Bearer {jwt}"})
    conn = kb.connect()
    try:
        run = kb.latest_run(conn, worker_env)
    finally:
        conn.close()
    # block_task stores reason as run.summary
    assert run is not None
    stored = run.summary or ""
    assert jwt not in stored


# ---------------------------------------------------------------------------
# Negative test — plain text passes through unchanged
# ---------------------------------------------------------------------------

def test_kanban_comment_no_secret_passthrough(worker_env):
    """Plain text without credential patterns must pass through unchanged."""
    from tools import kanban_tools as kt
    from hermes_cli import kanban_db as kb
    plain = "hello from the pipeline — no secrets here"
    kt._handle_comment({"task_id": worker_env, "body": plain})
    conn = kb.connect()
    try:
        comments = kb.list_comments(conn, worker_env)
    finally:
        conn.close()
    stored = comments[-1].body
    assert stored == plain


# ---------------------------------------------------------------------------
# Negative test — force=True bypasses HERMES_REDACT_SECRETS=false
# ---------------------------------------------------------------------------

def test_scrub_respects_force_flag_regardless_of_config(worker_env, monkeypatch):
    """force=True must fire even when HERMES_REDACT_SECRETS=false is set."""
    monkeypatch.setenv("HERMES_REDACT_SECRETS", "false")
    from tools import kanban_tools as kt
    from hermes_cli import kanban_db as kb
    secret = "ghp_" + "C" * 40
    kt._handle_comment({"task_id": worker_env, "body": f"token: {secret}"})
    conn = kb.connect()
    try:
        comments = kb.list_comments(conn, worker_env)
    finally:
        conn.close()
    stored = comments[-1].body
    assert secret not in stored


# ---------------------------------------------------------------------------
# Negative test — legacy result field is also scrubbed
# ---------------------------------------------------------------------------

def test_kanban_complete_result_field_scrubbed(worker_env):
    """Legacy result field must be scrubbed just like summary."""
    from tools import kanban_tools as kt
    from hermes_cli import kanban_db as kb
    secret = "sk-" + "D" * 48
    kt._handle_complete({"result": f"finished with key={secret}"})
    conn = kb.connect()
    try:
        run = kb.latest_run(conn, worker_env)
    finally:
        conn.close()
    assert run is not None
    stored = run.summary or run.result if hasattr(run, "result") else run.summary or ""
    assert secret not in (stored or "")


# ---------------------------------------------------------------------------
# Gateway notifier egress: redact at send + gate artifact uploads (Commit 3)
# ---------------------------------------------------------------------------

import asyncio  # noqa: E402


class _RecordingAdapter:
    def __init__(self):
        self.sent = []

    async def send(self, chat_id, text, metadata=None):
        self.sent.append({"chat_id": chat_id, "text": text})


async def _run_one_notifier_tick(monkeypatch, runner):
    real_sleep = asyncio.sleep

    async def fake_sleep(delay):
        if delay == 5:
            return None
        runner._running = False
        await real_sleep(0)

    monkeypatch.setattr(asyncio, "sleep", fake_sleep)
    await runner._kanban_notifier_watcher(interval=1)


def _make_runner(adapter):
    from gateway.config import Platform
    from gateway.run import GatewayRunner
    runner = GatewayRunner.__new__(GatewayRunner)
    runner._running = True
    runner.adapters = {Platform.TELEGRAM: adapter}
    runner._kanban_sub_fail_counts = {}
    return runner


def _notifier_home(monkeypatch, tmp_path, config_yaml: str = ""):
    """Isolated HERMES_HOME + board DB for a notifier-tick test."""
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    from pathlib import Path as _Path
    monkeypatch.setattr(_Path, "home", lambda: tmp_path)
    if config_yaml:
        (home / "config.yaml").write_text(config_yaml, encoding="utf-8")
    monkeypatch.setenv("HERMES_KANBAN_DB", str(tmp_path / "kanban.db"))
    from hermes_cli import kanban_db as kb
    kb._INITIALIZED_PATHS.clear()
    kb.init_db()
    return home


def test_notifier_masks_gave_up_secret(monkeypatch, tmp_path):
    """A gave_up event whose raw spawn-error carries secret-shaped strings
    must be masked before the notifier text reaches the adapter."""
    _notifier_home(monkeypatch, tmp_path)
    from hermes_cli import kanban_db as kb
    secret_gh = "ghp_" + "A" * 40
    secret_sk = "sk-" + "B" * 48
    conn = kb.connect()
    try:
        tid = kb.create_task(conn, title="boom", assignee="worker")
        kb.add_notify_sub(conn, task_id=tid, platform="telegram", chat_id="chat-1")
        with kb.write_txn(conn):
            kb._append_event(
                conn, tid, "gave_up",
                {"error": f"spawn failed: token={secret_gh} key={secret_sk}"},
            )
    finally:
        conn.close()

    adapter = _RecordingAdapter()
    asyncio.run(_run_one_notifier_tick(monkeypatch, _make_runner(adapter)))

    assert len(adapter.sent) == 1, adapter.sent
    text = adapter.sent[0]["text"]
    assert "gave up" in text
    assert secret_gh not in text
    assert secret_sk not in text


def test_notify_artifact_uploads_false_skips_delivery(monkeypatch, tmp_path):
    """kanban.notify_artifact_uploads=false must suppress the artifact-upload
    call while the (redacted) text notification still fires."""
    _notifier_home(
        monkeypatch, tmp_path,
        config_yaml="kanban:\n  notify_artifact_uploads: false\n",
    )
    from hermes_cli import kanban_db as kb
    conn = kb.connect()
    try:
        tid = kb.create_task(conn, title="done", assignee="worker")
        kb.add_notify_sub(conn, task_id=tid, platform="telegram", chat_id="chat-1")
        kb.complete_task(conn, tid, summary="all finished")
    finally:
        conn.close()

    adapter = _RecordingAdapter()
    runner = _make_runner(adapter)
    calls = []

    async def _spy(**kwargs):
        calls.append(kwargs)

    runner._deliver_kanban_artifacts = _spy
    asyncio.run(_run_one_notifier_tick(monkeypatch, runner))

    assert len(adapter.sent) == 1, adapter.sent
    assert calls == [], "artifact delivery must be gated off"


def test_notify_artifact_uploads_default_on(monkeypatch, tmp_path):
    """With no config override, the artifact-upload call still fires on a
    completed event (default-on, backward compatible)."""
    _notifier_home(monkeypatch, tmp_path)
    from hermes_cli import kanban_db as kb
    conn = kb.connect()
    try:
        tid = kb.create_task(conn, title="done", assignee="worker")
        kb.add_notify_sub(conn, task_id=tid, platform="telegram", chat_id="chat-1")
        kb.complete_task(conn, tid, summary="all finished")
    finally:
        conn.close()

    adapter = _RecordingAdapter()
    runner = _make_runner(adapter)
    calls = []

    async def _spy(**kwargs):
        calls.append(kwargs)

    runner._deliver_kanban_artifacts = _spy
    asyncio.run(_run_one_notifier_tick(monkeypatch, runner))

    assert len(adapter.sent) == 1
    assert len(calls) == 1, "artifact delivery should fire by default"
