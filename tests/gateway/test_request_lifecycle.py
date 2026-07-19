import asyncio
from types import SimpleNamespace

import pytest

from gateway.admission import AdmissionScheduler
from gateway.config import Platform, PlatformConfig
from gateway.platforms.base import BasePlatformAdapter, MessageEvent, MessageType, SendResult
from gateway.run import (
    GatewayRunner,
    _GATEWAY_QUEUE_FULL_MESSAGE,
    _GATEWAY_RESTART_MESSAGE,
)
from gateway.session import SessionSource
from hermes_state import SessionDB


class _Adapter(BasePlatformAdapter):
    def __init__(self):
        super().__init__(PlatformConfig(enabled=True), Platform.SLACK)
        self.sent = []
        self.config.typing_indicator = False

    async def connect(self, *, is_reconnect: bool = False):
        return True

    async def disconnect(self):
        return None

    async def get_chat_info(self, chat_id):
        return {"name": chat_id, "type": "dm"}

    async def send(self, chat_id, content, reply_to=None, metadata=None):
        self.sent.append((chat_id, content, metadata))
        return SendResult(success=True, message_id=f"sent-{len(self.sent)}")


def _event(message_id: str, text: str = "investigate campaign performance"):
    return MessageEvent(
        text=text,
        message_type=MessageType.TEXT,
        source=SessionSource(
            platform=Platform.SLACK,
            chat_id=f"chat-{message_id}",
            chat_type="dm",
            user_id="U1",
        ),
        message_id=message_id,
    )


@pytest.fixture()
def runner(tmp_path):
    db = SessionDB(db_path=tmp_path / "state.db")
    instance = GatewayRunner.__new__(GatewayRunner)
    cleared_resume_pending = []
    instance.session_store = SimpleNamespace(
        _db=db,
        clear_resume_pending=lambda key: cleared_resume_pending.append(key),
    )
    instance._cleared_resume_pending = cleared_resume_pending
    instance._admission_scheduler = AdmissionScheduler(
        heavy_capacity=2,
        short_capacity=2,
        queue_capacity=10,
    )
    instance._startup_restore_in_progress = False
    instance._is_user_authorized = lambda source: True
    instance._session_key_for_source = lambda source: f"slack:{source.chat_id}"
    instance._gateway_replayed_request_ids = set()
    yield instance
    db.close()


@pytest.mark.asyncio
async def test_base_adapter_duplicate_inbound_executes_and_delivers_once(runner):
    calls = 0

    async def handle(event):
        nonlocal calls
        calls += 1
        return "done"

    runner._handle_message_without_ledger = handle
    adapter = _Adapter()
    adapter.set_message_handler(runner._handle_message)

    first = _event("same-id")
    await adapter._process_message_background(first, "slack:chat-same-id")
    duplicate = _event("same-id")
    await adapter._process_message_background(duplicate, "slack:chat-same-id")

    assert calls == 1
    assert [content for _, content, _ in adapter.sent] == ["done"]
    request_id = first.metadata["request_id"]
    row = runner.session_store._db.get_gateway_request(request_id)
    assert row["status"] == "FINAL"
    assert row["delivery_state"] == "DELIVERED"
    assert row["response_hash"]
    assert row["payload_json"].find("done") == -1


@pytest.mark.asyncio
async def test_failed_actual_send_can_be_reclaimed(runner):
    async def handle(event):
        return "answer"

    runner._handle_message_without_ledger = handle
    adapter = _Adapter()

    async def fail_send(chat_id, content, reply_to=None, metadata=None):
        return SendResult(success=False, error="not delivered")

    adapter.send = fail_send
    adapter.set_message_handler(runner._handle_message)
    event = _event("failed-send")
    await adapter._process_message_background(event, "slack:chat-failed-send")

    db = runner.session_store._db
    row = db.get_gateway_request(event.metadata["request_id"])
    assert row["status"] == "FAILED"
    assert row["delivery_state"] == "FAILED"
    reclaimed = db.claim_gateway_request_delivery(event.metadata["request_id"])
    assert reclaimed["delivery_state"] == "SENDING"


@pytest.mark.asyncio
async def test_base_adapter_heavy_fifo_and_short_bypass_global_cap(runner):
    started = []
    release = {name: asyncio.Event() for name in ("h1", "h2", "h3", "h4")}

    async def handle(event):
        started.append(event.message_id)
        gate = release.get(event.message_id)
        if gate is not None:
            await gate.wait()
        return event.message_id

    runner._handle_message_without_ledger = handle
    runner._claim_active_session_slot = lambda *_args: (None, "global cap rejected")
    adapter = _Adapter()
    adapter.set_message_handler(runner._handle_message)
    events = {
        name: _event(name)
        for name in ("h1", "h2", "h3", "h4")
    }

    async def process(event):
        await adapter._process_message_background(
            event, f"slack:{event.source.chat_id}"
        )

    tasks = [
        asyncio.create_task(process(events[name]))
        for name in ("h1", "h2")
    ]
    await asyncio.sleep(0)
    queued3 = asyncio.create_task(process(events["h3"]))
    queued4 = asyncio.create_task(process(events["h4"]))
    await asyncio.sleep(0)
    assert started == ["h1", "h2"]

    short = asyncio.create_task(process(_event("short", "status")))
    await asyncio.sleep(0)
    assert started[-1] == "short"
    await short

    release["h1"].set()
    await asyncio.sleep(0)
    await asyncio.sleep(0)
    assert started[:4] == ["h1", "h2", "short", "h3"]
    release["h2"].set()
    await asyncio.sleep(0)
    await asyncio.sleep(0)
    assert started[-1] == "h4"
    release["h3"].set()
    release["h4"].set()
    await asyncio.gather(*tasks, queued3, queued4)
    assert "global cap rejected" not in [
        content for _, content, _ in adapter.sent
    ]
    queued_row = runner.session_store._db.get_gateway_request(
        events["h3"].metadata["request_id"]
    )
    assert queued_row["status"] == "FINAL"
    assert (
        queued_row["received_at"]
        <= queued_row["queued_at"]
        <= queued_row["started_at"]
        <= queued_row["finalized_at"]
    )


@pytest.mark.asyncio
async def test_shared_waiting_queue_eleventh_waiter_fails_immediately(runner):
    release = asyncio.Event()

    async def handle(event):
        await release.wait()
        return "done"

    runner._handle_message_without_ledger = handle
    active = [asyncio.create_task(runner._handle_message(_event(f"active-{i}"))) for i in range(2)]
    await asyncio.sleep(0)
    waiting = [asyncio.create_task(runner._handle_message(_event(f"wait-{i}"))) for i in range(10)]
    await asyncio.sleep(0)

    overflow_event = _event("overflow")
    assert await runner._handle_message(overflow_event) == _GATEWAY_QUEUE_FULL_MESSAGE
    overflow_row = runner.session_store._db.get_gateway_request(
        overflow_event.metadata["request_id"]
    )
    assert overflow_row["status"] == "FAILED"
    assert overflow_row["public_error"] == _GATEWAY_QUEUE_FULL_MESSAGE

    release.set()
    await asyncio.gather(*active, *waiting)


@pytest.mark.asyncio
async def test_reconcile_fails_stale_work_and_explicitly_fails_unsafe_queue(runner):
    db = runner.session_store._db
    working, _ = db.create_or_get_gateway_request(
        request_id="working",
        platform="slack",
        session_key="slack:c1",
        user_id="U1",
        chat_id="c1",
        platform_message_id="m1",
        payload_json='{"version":1}',
    )
    db.compare_and_set_gateway_request_status(
        working["request_id"], from_statuses=["RECEIVED"], to_status="WORKING"
    )
    queued, _ = db.create_or_get_gateway_request(
        request_id="queued",
        platform="slack",
        session_key="slack:c2",
        user_id="U1",
        chat_id="c2",
        platform_message_id="m2",
        payload_json='{"version":1,"replayable":false}',
    )
    db.compare_and_set_gateway_request_status(
        queued["request_id"], from_statuses=["RECEIVED"], to_status="QUEUED"
    )
    received, _ = db.create_or_get_gateway_request(
        request_id="received",
        platform="slack",
        session_key="slack:c3",
        user_id="U1",
        chat_id="c3",
        platform_message_id="m3",
        payload_json='{"version":1,"replayable":false}',
    )

    failed_sessions = await runner._reconcile_gateway_requests()

    for request_id in ("working", "queued", received["request_id"]):
        row = db.get_gateway_request(request_id)
        assert row["status"] == "FAILED"
        assert row["public_error"] == _GATEWAY_RESTART_MESSAGE
    assert failed_sessions == {"slack:c1", "slack:c2", "slack:c3"}
    assert runner._gateway_failed_resume_session_keys == failed_sessions
    assert set(runner._cleared_resume_pending) == failed_sessions

@pytest.mark.asyncio
async def test_busy_continuation_claims_and_delivers_once(runner):
    calls = 0

    async def handle(event):
        nonlocal calls
        calls += 1
        return "continued"

    runner._handle_message_without_ledger = handle
    adapter = _Adapter()
    adapter.set_message_handler(runner._handle_message)
    event = _event("continued")
    event.metadata["_gateway_continuation"] = True
    request_id = runner._gateway_request_id(
        "slack", event.source.chat_id, event.message_id
    )
    runner.session_store._db.create_or_get_gateway_request(
        request_id=request_id,
        platform="slack",
        session_key=f"slack:{event.source.chat_id}",
        user_id="U1",
        chat_id=event.source.chat_id,
        platform_message_id=event.message_id,
        client_message_id=event.message_id,
        payload_json=runner._gateway_request_payload(event),
    )

    await adapter._process_message_background(
        event, f"slack:{event.source.chat_id}"
    )

    row = runner.session_store._db.get_gateway_request(request_id)
    assert calls == 1
    assert [content for _, content, _ in adapter.sent] == ["continued"]
    assert row["status"] == "FINAL"
    assert row["delivery_state"] == "DELIVERED"


@pytest.mark.asyncio
async def test_reconcile_restart_notice_is_claimed_and_sent_once(runner):
    adapter = _Adapter()
    runner._adapter_for_source = lambda source: adapter
    runner._thread_metadata_for_source = lambda source: None
    event = _event("stale-working")
    db = runner.session_store._db
    row, _ = db.create_or_get_gateway_request(
        request_id="stale-working",
        platform="slack",
        session_key=f"slack:{event.source.chat_id}",
        user_id="U1",
        chat_id=event.source.chat_id,
        platform_message_id=event.message_id,
        client_message_id=event.message_id,
        payload_json=runner._gateway_request_payload(event),
    )
    db.compare_and_set_gateway_request_status(
        row["request_id"],
        from_statuses=["RECEIVED"],
        to_status="WORKING",
    )
    db.claim_gateway_request_delivery(row["request_id"])

    await runner._reconcile_gateway_requests()
    await runner._reconcile_gateway_requests()

    final = db.get_gateway_request(row["request_id"])
    assert [content for _, content, _ in adapter.sent] == [
        _GATEWAY_RESTART_MESSAGE
    ]
    assert final["status"] == "FAILED"
    assert final["delivery_state"] == "DELIVERED"


@pytest.mark.asyncio
async def test_startup_replay_preserves_same_session_request_boundaries(runner):
    calls = []

    async def handle(event):
        calls.append(event.metadata["request_id"])
        await asyncio.sleep(0)
        return event.metadata["request_id"]

    adapter = _Adapter()
    runner._handle_message_without_ledger = handle
    adapter.set_message_handler(runner._handle_message)
    runner._adapter_for_source = lambda source: adapter
    runner._startup_restore_in_progress = True
    db = runner.session_store._db
    for request_id in ("replay-1", "replay-2"):
        event = _event(request_id)
        event.source.chat_id = "same-replay-chat"
        row, _ = db.create_or_get_gateway_request(
            request_id=request_id,
            platform="slack",
            session_key="slack:same-replay-chat",
            user_id="U1",
            chat_id="same-replay-chat",
            platform_message_id=request_id,
            client_message_id=request_id,
            payload_json=runner._gateway_request_payload(event),
        )
        db.compare_and_set_gateway_request_status(
            row["request_id"],
            from_statuses=["RECEIVED"],
            to_status="QUEUED",
        )

    await runner._reconcile_gateway_requests()
    tasks = list(adapter._background_tasks)
    if tasks:
        await asyncio.gather(*tasks)

    assert calls == ["replay-1", "replay-2"]
    assert [
        db.get_gateway_request(request_id)["status"]
        for request_id in calls
    ] == ["FINAL", "FINAL"]
    assert "slack:same-replay-chat" in runner._cleared_resume_pending

@pytest.mark.asyncio
async def test_busy_fifo_cannot_bypass_shared_waiting_cap(runner):
    started = asyncio.Event()
    release = asyncio.Event()

    async def handle(event):
        if event.message_id == "busy-active":
            started.set()
            await release.wait()
        return "done"

    runner._handle_message_without_ledger = handle
    adapter = _Adapter()
    adapter._busy_input_mode = "queue"
    adapter.set_message_handler(runner._handle_message)

    def same_session(message_id):
        event = _event(message_id)
        event.source.chat_id = "busy-chat"
        return event

    await adapter.handle_message(same_session("busy-active"))
    await started.wait()
    await adapter.handle_message(same_session("busy-admitted"))
    waiting = [
        asyncio.create_task(adapter.handle_message(same_session(f"busy-{i}")))
        for i in range(10)
    ]
    async with asyncio.timeout(1):
        while runner._admission_scheduler.waiting_count < 10:
            await asyncio.sleep(0)

    overflow = same_session("busy-overflow")
    await adapter.handle_message(overflow)

    overflow_row = runner.session_store._db.get_gateway_request(
        overflow.metadata["request_id"]
    )
    assert overflow_row["status"] == "FAILED"
    assert [content for _, content, _ in adapter.sent].count(
        _GATEWAY_QUEUE_FULL_MESSAGE
    ) == 1
    assert runner._admission_scheduler.waiting_count == 10

    await runner._admission_scheduler.cancel_waiters()
    await asyncio.gather(*waiting, return_exceptions=True)
    release.set()
    await adapter.cancel_background_tasks()


@pytest.mark.asyncio
async def test_explicit_prepare_hook_runs_for_wrapped_handler():
    prepared = []

    async def handler(_event):
        raise AssertionError("rejected event reached wrapped handler")

    async def prepare(event):
        prepared.append(event.message_id)
        return False

    adapter = _Adapter()
    adapter.set_message_handler(handler, prepare_handler=prepare)
    await adapter.handle_message(_event("profile-wrapped"))

    assert prepared == ["profile-wrapped"]
    assert not adapter._background_tasks


@pytest.mark.asyncio
async def test_successful_busy_steer_releases_admission_lease(
    runner,
    monkeypatch,
):
    class _RunningAgent:
        @staticmethod
        def steer(_text):
            return True

    event = _event("steer-inline")
    session_key = f"slack:{event.source.chat_id}"
    adapter = _Adapter()
    runner._draining = False
    runner._busy_input_mode = "steer"
    runner._busy_text_mode = "steer"
    runner._running_agents = {session_key: _RunningAgent()}
    runner._agent_has_active_subagents = lambda _agent: False
    runner._session_has_compression_in_flight = lambda _key: False
    runner._adapter_for_source = lambda _source: adapter
    runner._busy_ack_ts = {}
    monkeypatch.setenv("HERMES_GATEWAY_BUSY_ACK_ENABLED", "false")

    assert await runner._prepare_gateway_request(event)
    assert runner._admission_scheduler.active_count == 1

    assert await runner._handle_active_session_busy_message(event, session_key)

    row = runner.session_store._db.get_gateway_request(
        event.metadata["request_id"]
    )
    assert runner._admission_scheduler.active_count == 0
    assert row["status"] == "FINAL"
    assert row["delivery_state"] == "NONE"


@pytest.mark.asyncio
async def test_image_only_response_observes_terminal_delivery():
    observed = []

    async def handler(_event):
        return "![chart](https://example.com/chart.png)"

    async def observe(response, result):
        observed.append((response, result))

    adapter = _Adapter()
    adapter.set_message_handler(handler)
    event = _event("image-only")
    event.metadata["_gateway_delivery_observer"] = observe

    await adapter._process_message_background(
        event,
        f"slack:{event.source.chat_id}",
    )

    assert len(observed) == 1
    response, result = observed[0]
    assert "chart.png" in response
    assert result.success is True


@pytest.mark.asyncio
async def test_profile_prepare_stamps_profile_before_request_payload(runner):
    observed = []

    async def prepare(event):
        observed.append(event.source.profile)
        return True

    runner._prepare_gateway_request = prepare
    event = _event("profile-prepare")

    assert await runner._make_profile_request_prepare_handler("data_analyst")(event)
    assert observed == ["data_analyst"]
    assert event.source.profile == "data_analyst"


@pytest.mark.asyncio
async def test_restart_drain_persists_busy_turn_and_releases_lease(runner):
    adapter = _Adapter()
    event = _event("restart-queued")
    runner._draining = True
    runner._restart_requested = True
    runner._busy_input_mode = "queue"
    runner._adapter_for_source = lambda _source: adapter

    assert await runner._prepare_gateway_request(event)
    assert runner._admission_scheduler.active_count == 1
    assert await runner._handle_active_session_busy_message(
        event,
        f"slack:{event.source.chat_id}",
    )

    row = runner.session_store._db.get_gateway_request(
        event.metadata["request_id"]
    )
    assert row["status"] == "QUEUED"
    assert row["delivery_state"] == "NONE"
    assert runner._admission_scheduler.active_count == 0
    assert f"slack:{event.source.chat_id}" not in adapter._pending_messages


@pytest.mark.asyncio
async def test_mixed_response_observer_prefers_any_successful_delivery():
    observed = []

    async def handler(_event):
        return "summary\n![chart](https://example.com/chart.png)"

    async def observe(response, result):
        observed.append((response, result))

    adapter = _Adapter()

    async def fail_text(*_args, **_kwargs):
        return SendResult(success=False, error="text failed")

    async def send_images(*_args, **_kwargs):
        return SendResult(success=True, message_id="image-delivered")

    adapter.send = fail_text
    adapter.send_multiple_images = send_images
    adapter.set_message_handler(handler)
    event = _event("mixed-delivery")
    event.metadata["_gateway_delivery_observer"] = observe

    await adapter._process_message_background(
        event,
        f"slack:{event.source.chat_id}",
    )

    assert len(observed) == 1
    assert observed[0][1].success is True


@pytest.mark.asyncio
async def test_media_sender_exception_observes_failed_delivery():
    observed = []

    async def handler(_event):
        return "MEDIA:/tmp/report.pdf"

    async def observe(response, result):
        observed.append((response, result))

    adapter = _Adapter()
    adapter.extract_media = lambda _response: ([("/tmp/report.pdf", False)], "")
    adapter.filter_media_delivery_paths = lambda files: files

    async def raise_document(*_args, **_kwargs):
        raise RuntimeError("upload failed")

    adapter.send_document = raise_document
    adapter.set_message_handler(handler)
    event = _event("media-failure")
    event.metadata["_gateway_delivery_observer"] = observe

    await adapter._process_message_background(
        event,
        f"slack:{event.source.chat_id}",
    )

    assert len(observed) == 1
    assert observed[0][1].success is False
    assert observed[0][1].error == "upload failed"


@pytest.mark.asyncio
async def test_shutdown_drain_cancellation_requeues_durable_request(runner):
    started = asyncio.Event()
    release = asyncio.Event()

    async def handle(_event):
        started.set()
        await release.wait()
        return "unreachable"

    runner._draining = True
    runner._restart_requested = False
    runner._handle_message_without_ledger = handle
    event = _event("shutdown-cancelled")

    task = asyncio.create_task(runner._handle_message(event))
    await started.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    row = runner.session_store._db.get_gateway_request(
        event.metadata["request_id"]
    )
    assert row["status"] == "QUEUED"
    assert row["delivery_state"] == "FAILED"
    assert runner._admission_scheduler.active_count == 0


@pytest.mark.asyncio
async def test_same_session_prequeue_stays_replayable_until_handler_starts(runner):
    started = asyncio.Event()
    release = asyncio.Event()

    async def handle(event):
        if event.message_id == "active-turn":
            started.set()
            await release.wait()
        return "done"

    runner._handle_message_without_ledger = handle
    adapter = _Adapter()
    adapter.set_message_handler(runner._handle_message)

    active = _event("active-turn")
    active.source.chat_id = "same-session"
    follow_up = _event("queued-follow-up")
    follow_up.source.chat_id = "same-session"

    await adapter.handle_message(active)
    await started.wait()
    await adapter.handle_message(follow_up)

    queued = runner.session_store._db.get_gateway_request(
        follow_up.metadata["request_id"]
    )
    assert queued["status"] in {"RECEIVED", "QUEUED"}
    assert queued["delivery_state"] == "NONE"

    release.set()
    tasks = list(adapter._background_tasks)
    if tasks:
        await asyncio.gather(*tasks)

    final = runner.session_store._db.get_gateway_request(
        follow_up.metadata["request_id"]
    )
    assert final["status"] == "FINAL"
    assert final["delivery_state"] == "DELIVERED"


@pytest.mark.asyncio
async def test_drain_interrupted_empty_response_requeues_request(runner):
    async def handle(_event):
        return None

    runner._draining = True
    runner._handle_message_without_ledger = handle
    event = _event("drain-empty")

    assert await runner._handle_message(event) is None

    row = runner.session_store._db.get_gateway_request(
        event.metadata["request_id"]
    )
    assert row["status"] == "QUEUED"
    assert row["delivery_state"] == "FAILED"
    assert runner._admission_scheduler.active_count == 0
