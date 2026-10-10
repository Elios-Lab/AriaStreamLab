"""In-process event bus with bounded per-subscriber queues and explicit drop policies.

Why this design (docs/ARCHITECTURE.md §1.2, §7):

* SDK callbacks run on native receive threads; ``publish`` must therefore be cheap and must
  never block them. Publishing costs one lock + one ``deque.append`` per matching subscriber.
* Every subscriber owns its own bounded queue, so a slow consumer (a heavy processor, a
  closed dashboard tab, a stalled disk) can only lose *its own* samples, never anyone else's,
  and every loss is counted and visible.
* ``BLOCK`` (publisher waits) exists for recorders, where losing data is worse than
  back-pressure. The configuration layer forbids it on the path fed by a live source.
"""

from __future__ import annotations

import fnmatch
import threading
from collections import deque
from collections.abc import Iterable
from dataclasses import dataclass
from enum import Enum
from typing import Any, Generic, TypeVar

T = TypeVar("T")


class DropPolicy(str, Enum):
    """What a bounded subscription does when it is full and a new item arrives."""

    BLOCK = "block"
    """Publisher waits until there is room (never allowed on a live source path)."""
    DROP_OLDEST = "drop_oldest"
    """Discard the oldest queued item to make room (ring buffer)."""
    DROP_NEWEST = "drop_newest"
    """Discard the incoming item."""
    LATEST = "latest"
    """Keep only the most recent item (queue size forced to 1)."""


@dataclass(frozen=True, slots=True)
class SubscriptionStats:
    """Counters of one subscription."""

    name: str
    pattern: str
    enqueued: int
    delivered: int
    dropped: int
    depth: int
    maxsize: int
    policy: DropPolicy


class Subscription(Generic[T]):
    """A bounded queue bound to one topic pattern. Created by :meth:`EventBus.subscribe`."""

    def __init__(
        self, bus: EventBus, name: str, pattern: str, maxsize: int, policy: DropPolicy
    ) -> None:
        if maxsize < 1:
            raise ValueError("maxsize must be >= 1")
        self._bus = bus
        self.name = name
        self.pattern = pattern
        self.policy = policy
        self.maxsize = 1 if policy is DropPolicy.LATEST else maxsize
        self._queue: deque[T] = deque()
        self._lock = threading.Lock()
        self._not_empty = threading.Condition(self._lock)
        self._not_full = threading.Condition(self._lock)
        self._closed_ev = threading.Event()
        self.enqueued = 0
        self.delivered = 0
        self.dropped = 0

    # -- producer side (called by the bus) ------------------------------------------------
    def _offer(self, item: T) -> None:
        with self._lock:
            if self._closed_ev.is_set():
                return
            self.enqueued += 1
            if len(self._queue) >= self.maxsize:
                if self.policy is DropPolicy.BLOCK:
                    while len(self._queue) >= self.maxsize:
                        if self._closed_ev.is_set():  # close() wakes us up
                            return
                        self._not_full.wait()
                elif self.policy is DropPolicy.DROP_NEWEST:
                    self.dropped += 1
                    return
                else:  # DROP_OLDEST and LATEST
                    self._queue.popleft()
                    self.dropped += 1
            self._queue.append(item)
            self._not_empty.notify()

    # -- consumer side -----------------------------------------------------------------
    def get(self, timeout: float | None = None) -> T | None:
        """Pop the next item, waiting up to ``timeout`` seconds. ``None`` on timeout/close."""
        with self._lock:
            if not self._queue and not self._closed_ev.is_set():
                self._not_empty.wait(timeout)
            if not self._queue:
                return None
            item = self._queue.popleft()
            self.delivered += 1
            self._not_full.notify()
            return item

    def get_nowait(self) -> T | None:
        """Pop the next item without waiting."""
        return self.get(timeout=0)

    def drain(self) -> list[T]:
        """Pop everything currently queued."""
        with self._lock:
            items = list(self._queue)
            self._queue.clear()
            self.delivered += len(items)
            self._not_full.notify_all()
            return items

    def __len__(self) -> int:
        return len(self._queue)

    @property
    def closed(self) -> bool:
        """True once :meth:`close` was called."""
        return self._closed_ev.is_set()

    def close(self) -> None:
        """Detach from the bus; wakes any waiting consumer or blocked publisher."""
        with self._lock:
            if self._closed_ev.is_set():
                return
            self._closed_ev.set()
            self._not_empty.notify_all()
            self._not_full.notify_all()
        self._bus._remove(self)

    def stats(self) -> SubscriptionStats:
        """Snapshot of counters."""
        with self._lock:
            return SubscriptionStats(
                self.name,
                self.pattern,
                self.enqueued,
                self.delivered,
                self.dropped,
                len(self._queue),
                self.maxsize,
                self.policy,
            )

    def __enter__(self) -> Subscription[T]:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()


@dataclass(frozen=True, slots=True)
class BusStats:
    """Snapshot of every subscription on the bus."""

    published: int
    subscriptions: tuple[SubscriptionStats, ...]


class EventBus:
    """Topic-based publish/subscribe between acquisition, processing, storage and dashboard.

    Topic naming convention: ``raw.<stream_id>``, ``derived.<stream_id>``, ``events.<kind>``,
    ``health.<kind>``. Subscriptions use ``fnmatch`` patterns (``"raw.*"``, ``"derived.head_*"``).
    """

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._subs: list[Subscription[Any]] = []
        # exact-topic cache: topic -> tuple of matching subscriptions
        self._cache: dict[str, tuple[Subscription[Any], ...]] = {}
        self._topics: dict[str, int] = {}
        self.published = 0

    def subscribe(
        self,
        pattern: str,
        *,
        maxsize: int = 64,
        policy: DropPolicy = DropPolicy.DROP_OLDEST,
        name: str | None = None,
    ) -> Subscription[Any]:
        """Create a subscription for every topic matching ``pattern``."""
        sub: Subscription[Any] = Subscription(self, name or pattern, pattern, maxsize, policy)
        with self._lock:
            self._subs.append(sub)
            self._cache.clear()
        return sub

    def publish(self, topic: str, item: Any) -> int:
        """Deliver ``item`` to every matching subscription; returns how many received it."""
        subs = self._cache.get(topic)
        if subs is None:
            with self._lock:
                subs = tuple(s for s in self._subs if fnmatch.fnmatchcase(topic, s.pattern))
                self._cache[topic] = subs
                self._topics[topic] = self._topics.get(topic, 0)
        self.published += 1
        self._topics[topic] = self._topics.get(topic, 0) + 1
        for sub in subs:
            sub._offer(item)
        return len(subs)

    def topics(self) -> dict[str, int]:
        """Topics seen so far with their publish counts."""
        with self._lock:
            return dict(self._topics)

    def subscriptions(self) -> Iterable[Subscription[Any]]:
        """Live subscriptions."""
        with self._lock:
            return tuple(self._subs)

    def stats(self) -> BusStats:
        """Snapshot of all counters."""
        with self._lock:
            return BusStats(self.published, tuple(s.stats() for s in self._subs))

    def close(self) -> None:
        """Close every subscription."""
        with self._lock:
            subs = tuple(self._subs)
        for s in subs:
            s.close()

    def _remove(self, sub: Subscription[Any]) -> None:
        with self._lock:
            if sub in self._subs:
                self._subs.remove(sub)
            self._cache.clear()
