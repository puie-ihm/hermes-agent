"""In-process admission scheduling for gateway requests."""

from __future__ import annotations

import asyncio
from collections import deque
from dataclasses import dataclass
from enum import Enum
from typing import Deque


class AdmissionLane(str, Enum):
    """Independent admission lanes."""

    HEAVY = "heavy"
    SHORT = "short"


class AdmissionError(Exception):
    """Base class for admission failures."""


class DuplicateRequestError(AdmissionError):
    """Raised when a request is already active or waiting."""

    def __init__(self, request_id: str) -> None:
        self.request_id = request_id
        super().__init__(f"request {request_id!r} is already admitted or waiting")


class QueueFullError(AdmissionError):
    """Raised when the shared waiting queue has reached its capacity."""

    def __init__(self, *, depth: int, capacity: int) -> None:
        self.depth = depth
        self.capacity = capacity
        super().__init__(f"admission queue is full ({depth}/{capacity})")


@dataclass
class _Waiter:
    request_id: str
    session_key: str
    lane: AdmissionLane
    future: asyncio.Future[AdmissionLease]


class AdmissionLease:
    """An admitted request's idempotently releasable lane slot."""

    def __init__(
        self,
        scheduler: AdmissionScheduler,
        *,
        request_id: str,
        session_key: str,
        lane: AdmissionLane,
    ) -> None:
        self._scheduler = scheduler
        self.request_id = request_id
        self.session_key = session_key
        self.lane = lane
        self._released = False

    @property
    def released(self) -> bool:
        return self._released

    async def release(self) -> None:
        if self._released:
            return
        await self._scheduler._release(self)

    async def __aenter__(self) -> AdmissionLease:
        return self

    async def __aexit__(self, *_exc: object) -> None:
        await self.release()


class AdmissionScheduler:
    """Bounded FIFO scheduler with independent heavy and short capacities."""

    def __init__(
        self,
        *,
        heavy_capacity: int,
        short_capacity: int,
        queue_capacity: int,
    ) -> None:
        if heavy_capacity < 1:
            raise ValueError("heavy_capacity must be at least 1")
        if short_capacity < 1:
            raise ValueError("short_capacity must be at least 1")
        if queue_capacity < 0:
            raise ValueError("queue_capacity cannot be negative")

        self.heavy_capacity = heavy_capacity
        self.short_capacity = short_capacity
        self.queue_capacity = queue_capacity
        self._active: dict[str, AdmissionLease] = {}
        self._waiting: Deque[_Waiter] = deque()
        self._request_ids: set[str] = set()
        self._active_by_lane = {
            AdmissionLane.HEAVY: 0,
            AdmissionLane.SHORT: 0,
        }
        self._lock = asyncio.Lock()

    @property
    def active_count(self) -> int:
        return len(self._active)

    @property
    def waiting_count(self) -> int:
        return len(self._waiting)

    def active_count_for(self, lane: AdmissionLane | str) -> int:
        return self._active_by_lane[AdmissionLane(lane)]

    def waiting_count_for(self, lane: AdmissionLane | str) -> int:
        normalized = AdmissionLane(lane)
        return sum(waiter.lane is normalized for waiter in self._waiting)

    async def acquire(
        self,
        *,
        request_id: str,
        session_key: str,
        lane: AdmissionLane | str,
    ) -> AdmissionLease:
        normalized_lane = AdmissionLane(lane)
        future: asyncio.Future[AdmissionLease] | None = None

        async with self._lock:
            if request_id in self._request_ids:
                raise DuplicateRequestError(request_id)

            self._promote_lane_locked(normalized_lane)
            if self._has_capacity(normalized_lane):
                return self._activate_locked(request_id, session_key, normalized_lane)

            depth = len(self._waiting)
            if depth >= self.queue_capacity:
                raise QueueFullError(depth=depth, capacity=self.queue_capacity)

            future = asyncio.get_running_loop().create_future()
            self._waiting.append(
                _Waiter(request_id, session_key, normalized_lane, future)
            )
            self._request_ids.add(request_id)

        try:
            return await future
        except asyncio.CancelledError:
            await self._abandon_acquire(request_id, future)
            raise

    async def cancel_waiters(self) -> int:
        """Cancel and remove every queued request, returning the number removed."""
        async with self._lock:
            cancelled = len(self._waiting)
            while self._waiting:
                waiter = self._waiting.popleft()
                self._request_ids.discard(waiter.request_id)
                waiter.future.cancel()
            return cancelled

    def _capacity_for(self, lane: AdmissionLane) -> int:
        if lane is AdmissionLane.HEAVY:
            return self.heavy_capacity
        return self.short_capacity

    def _has_capacity(self, lane: AdmissionLane) -> bool:
        return self._active_by_lane[lane] < self._capacity_for(lane)

    def _activate_locked(
        self,
        request_id: str,
        session_key: str,
        lane: AdmissionLane,
    ) -> AdmissionLease:
        lease = AdmissionLease(
            self,
            request_id=request_id,
            session_key=session_key,
            lane=lane,
        )
        self._active[request_id] = lease
        self._request_ids.add(request_id)
        self._active_by_lane[lane] += 1
        return lease

    def _promote_lane_locked(self, lane: AdmissionLane) -> None:
        while self._has_capacity(lane):
            waiter = next(
                (candidate for candidate in self._waiting if candidate.lane is lane),
                None,
            )
            if waiter is None:
                return
            self._waiting.remove(waiter)
            if waiter.future.done():
                self._request_ids.discard(waiter.request_id)
                continue
            lease = self._activate_locked(
                waiter.request_id,
                waiter.session_key,
                waiter.lane,
            )
            waiter.future.set_result(lease)

    async def _release(self, lease: AdmissionLease) -> None:
        async with self._lock:
            if self._active.get(lease.request_id) is not lease:
                lease._released = True
                return
            del self._active[lease.request_id]
            self._request_ids.discard(lease.request_id)
            self._active_by_lane[lease.lane] -= 1
            lease._released = True
            self._promote_lane_locked(lease.lane)

    async def _abandon_acquire(
        self,
        request_id: str,
        future: asyncio.Future[AdmissionLease],
    ) -> None:
        async with self._lock:
            waiter = next(
                (
                    candidate
                    for candidate in self._waiting
                    if candidate.request_id == request_id
                    and candidate.future is future
                ),
                None,
            )
            if waiter is not None:
                self._waiting.remove(waiter)
                self._request_ids.discard(request_id)
                self._promote_lane_locked(waiter.lane)
                return

            if future.cancelled():
                return
            delivered_lease = future.result()
            if self._active.get(request_id) is not delivered_lease:
                return
            del self._active[request_id]
            self._request_ids.discard(request_id)
            self._active_by_lane[delivered_lease.lane] -= 1
            delivered_lease._released = True
            self._promote_lane_locked(delivered_lease.lane)
