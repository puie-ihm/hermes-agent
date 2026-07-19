import asyncio

import pytest

from gateway.admission import (
    AdmissionLane,
    AdmissionScheduler,
    DuplicateRequestError,
    QueueFullError,
)


def acquire_task(
    scheduler: AdmissionScheduler,
    request_id: str,
    lane: AdmissionLane | str,
) -> asyncio.Task:
    return asyncio.create_task(
        scheduler.acquire(
            request_id=request_id,
            session_key=f"session-{request_id}",
            lane=lane,
        )
    )


@pytest.mark.asyncio
async def test_two_heavy_slots_run_and_waiters_promote_fifo():
    scheduler = AdmissionScheduler(
        heavy_capacity=2,
        short_capacity=1,
        queue_capacity=4,
    )
    first = await scheduler.acquire(
        request_id="heavy-1", session_key="session-1", lane="heavy"
    )
    second = await scheduler.acquire(
        request_id="heavy-2", session_key="session-2", lane=AdmissionLane.HEAVY
    )
    third_task = acquire_task(scheduler, "heavy-3", "heavy")
    fourth_task = acquire_task(scheduler, "heavy-4", "heavy")
    await asyncio.sleep(0)

    assert scheduler.active_count_for("heavy") == 2
    assert scheduler.waiting_count_for("heavy") == 2
    assert not third_task.done()
    assert not fourth_task.done()

    await second.release()
    third = await third_task
    assert third.request_id == "heavy-3"
    assert not fourth_task.done()

    await first.release()
    fourth = await fourth_task
    assert fourth.request_id == "heavy-4"
    assert scheduler.active_count == 2
    assert scheduler.waiting_count == 0

    await third.release()
    await fourth.release()


@pytest.mark.asyncio
async def test_shared_waiting_queue_rejects_overflow_with_depth_and_capacity():
    scheduler = AdmissionScheduler(
        heavy_capacity=1,
        short_capacity=1,
        queue_capacity=2,
    )
    heavy = await scheduler.acquire(
        request_id="heavy-active", session_key="heavy-session", lane="heavy"
    )
    short = await scheduler.acquire(
        request_id="short-active", session_key="short-session", lane="short"
    )
    heavy_waiter = acquire_task(scheduler, "heavy-waiting", "heavy")
    short_waiter = acquire_task(scheduler, "short-waiting", "short")
    await asyncio.sleep(0)

    with pytest.raises(QueueFullError) as raised:
        await scheduler.acquire(
            request_id="overflow",
            session_key="overflow-session",
            lane="heavy",
        )

    assert raised.value.depth == 2
    assert raised.value.capacity == 2
    assert scheduler.waiting_count == 2

    await scheduler.cancel_waiters()
    with pytest.raises(asyncio.CancelledError):
        await heavy_waiter
    with pytest.raises(asyncio.CancelledError):
        await short_waiter
    await heavy.release()
    await short.release()


@pytest.mark.asyncio
async def test_duplicate_active_or_waiting_request_is_rejected():
    scheduler = AdmissionScheduler(
        heavy_capacity=1,
        short_capacity=1,
        queue_capacity=2,
    )
    active = await scheduler.acquire(
        request_id="same-active", session_key="session-a", lane="heavy"
    )

    with pytest.raises(DuplicateRequestError) as active_duplicate:
        await scheduler.acquire(
            request_id="same-active",
            session_key="different-session",
            lane="short",
        )
    assert active_duplicate.value.request_id == "same-active"

    waiter = acquire_task(scheduler, "same-waiting", "heavy")
    await asyncio.sleep(0)
    with pytest.raises(DuplicateRequestError):
        await scheduler.acquire(
            request_id="same-waiting",
            session_key="different-session",
            lane="heavy",
        )

    waiter.cancel()
    with pytest.raises(asyncio.CancelledError):
        await waiter
    await active.release()


@pytest.mark.asyncio
async def test_short_bypasses_blocked_heavy_queue_when_short_slot_is_free():
    scheduler = AdmissionScheduler(
        heavy_capacity=1,
        short_capacity=1,
        queue_capacity=2,
    )
    heavy = await scheduler.acquire(
        request_id="heavy-active", session_key="heavy-session", lane="heavy"
    )
    blocked_heavy = acquire_task(scheduler, "heavy-waiting", "heavy")
    await asyncio.sleep(0)

    short = await scheduler.acquire(
        request_id="short", session_key="short-session", lane="short"
    )

    assert short.lane is AdmissionLane.SHORT
    assert scheduler.active_count_for("short") == 1
    assert scheduler.waiting_count_for("heavy") == 1
    assert not blocked_heavy.done()

    await short.release()
    await heavy.release()
    promoted_heavy = await blocked_heavy
    await promoted_heavy.release()


@pytest.mark.asyncio
async def test_short_waits_at_short_capacity_even_when_heavy_slot_is_free():
    scheduler = AdmissionScheduler(
        heavy_capacity=1,
        short_capacity=1,
        queue_capacity=2,
    )
    first = await scheduler.acquire(
        request_id="short-1", session_key="session-1", lane="short"
    )
    second_task = acquire_task(scheduler, "short-2", "short")
    await asyncio.sleep(0)

    assert scheduler.active_count_for("heavy") == 0
    assert scheduler.active_count_for("short") == 1
    assert scheduler.waiting_count_for("short") == 1
    assert not second_task.done()

    await first.release()
    second = await second_task
    assert scheduler.active_count_for("short") == 1
    await second.release()


@pytest.mark.asyncio
async def test_cancelling_waiting_acquire_removes_it_from_queue():
    scheduler = AdmissionScheduler(
        heavy_capacity=1,
        short_capacity=1,
        queue_capacity=1,
    )
    active = await scheduler.acquire(
        request_id="active", session_key="session-active", lane="heavy"
    )
    waiter = acquire_task(scheduler, "cancelled", "heavy")
    await asyncio.sleep(0)
    assert scheduler.waiting_count == 1

    waiter.cancel()
    with pytest.raises(asyncio.CancelledError):
        await waiter

    assert scheduler.waiting_count == 0
    replacement = acquire_task(scheduler, "replacement", "heavy")
    await asyncio.sleep(0)
    assert scheduler.waiting_count == 1

    await active.release()
    replacement_lease = await replacement
    await replacement_lease.release()


@pytest.mark.asyncio
async def test_cancelled_waiter_cleanup_cannot_release_reused_request_id():
    scheduler = AdmissionScheduler(
        heavy_capacity=1,
        short_capacity=1,
        queue_capacity=1,
    )
    active = await scheduler.acquire(
        request_id="blocker", session_key="blocker-session", lane="heavy"
    )
    cancelled = acquire_task(scheduler, "reused", "heavy")
    await asyncio.sleep(0)

    assert await scheduler.cancel_waiters() == 1
    replacement = await scheduler.acquire(
        request_id="reused", session_key="replacement-session", lane="short"
    )
    with pytest.raises(asyncio.CancelledError):
        await cancelled

    assert not replacement.released
    assert scheduler.active_count_for("short") == 1

    await replacement.release()
    await active.release()


@pytest.mark.asyncio
async def test_lease_double_release_is_safe_and_promotes_only_once():
    scheduler = AdmissionScheduler(
        heavy_capacity=1,
        short_capacity=1,
        queue_capacity=1,
    )
    first = await scheduler.acquire(
        request_id="first", session_key="session-first", lane="heavy"
    )
    second_task = acquire_task(scheduler, "second", "heavy")
    await asyncio.sleep(0)

    await first.release()
    second = await second_task
    await first.release()

    assert first.released
    assert scheduler.active_count_for("heavy") == 1
    assert second.request_id == "second"
    await second.release()


@pytest.mark.asyncio
async def test_shutdown_cancellation_wakes_every_waiter():
    scheduler = AdmissionScheduler(
        heavy_capacity=1,
        short_capacity=1,
        queue_capacity=4,
    )
    heavy = await scheduler.acquire(
        request_id="heavy-active", session_key="heavy-session", lane="heavy"
    )
    short = await scheduler.acquire(
        request_id="short-active", session_key="short-session", lane="short"
    )
    waiters = [
        acquire_task(scheduler, "heavy-1", "heavy"),
        acquire_task(scheduler, "short-1", "short"),
        acquire_task(scheduler, "heavy-2", "heavy"),
        acquire_task(scheduler, "short-2", "short"),
    ]
    await asyncio.sleep(0)

    assert await scheduler.cancel_waiters() == 4
    results = await asyncio.gather(*waiters, return_exceptions=True)

    assert all(isinstance(result, asyncio.CancelledError) for result in results)
    assert scheduler.waiting_count == 0
    assert scheduler.active_count == 2

    await heavy.release()
    await short.release()
