from __future__ import annotations

import threading
import time

import pytest

from ariastreamlab.core.bus import DropPolicy, EventBus


def test_publish_reaches_matching_subscriptions_only(bus: EventBus) -> None:
    raw = bus.subscribe("raw.*", name="raw")
    imu = bus.subscribe("raw.imu_*", name="imu")
    derived = bus.subscribe("derived.*", name="derived")
    assert bus.publish("raw.imu_left", 1) == 2
    assert bus.publish("raw.rgb", 2) == 1
    assert bus.publish("events.marker", 3) == 0
    assert raw.drain() == [1, 2]
    assert imu.drain() == [1]
    assert derived.drain() == []
    assert bus.topics() == {"raw.imu_left": 1, "raw.rgb": 1, "events.marker": 1}


def test_drop_oldest_keeps_newest_and_counts(bus: EventBus) -> None:
    sub = bus.subscribe("t", maxsize=3, policy=DropPolicy.DROP_OLDEST)
    for i in range(6):
        bus.publish("t", i)
    assert sub.drain() == [3, 4, 5]
    s = sub.stats()
    assert (s.enqueued, s.dropped, s.delivered) == (6, 3, 3)


def test_drop_newest_keeps_oldest(bus: EventBus) -> None:
    sub = bus.subscribe("t", maxsize=3, policy=DropPolicy.DROP_NEWEST)
    for i in range(6):
        bus.publish("t", i)
    assert sub.drain() == [0, 1, 2]
    assert sub.stats().dropped == 3


def test_latest_policy_forces_size_one(bus: EventBus) -> None:
    sub = bus.subscribe("t", maxsize=100, policy=DropPolicy.LATEST)
    assert sub.maxsize == 1
    for i in range(10):
        bus.publish("t", i)
    assert sub.get_nowait() == 9
    assert sub.get_nowait() is None
    assert sub.stats().dropped == 9


def test_block_policy_applies_backpressure(bus: EventBus) -> None:
    sub = bus.subscribe("t", maxsize=1, policy=DropPolicy.BLOCK)
    bus.publish("t", 0)
    done = threading.Event()

    def publisher() -> None:
        bus.publish("t", 1)  # blocks until the consumer pops
        done.set()

    t = threading.Thread(target=publisher)
    t.start()
    time.sleep(0.05)
    assert not done.is_set()
    assert sub.get(timeout=1) == 0
    assert done.wait(1)
    assert sub.get(timeout=1) == 1
    assert sub.stats().dropped == 0


def test_close_unblocks_waiting_consumer_and_publisher(bus: EventBus) -> None:
    sub = bus.subscribe("t", maxsize=1, policy=DropPolicy.BLOCK)
    bus.publish("t", 0)
    results: list[object] = []

    def consumer() -> None:
        results.append(sub.get(timeout=5))
        results.append(sub.get(timeout=5))

    def publisher() -> None:
        bus.publish("t", 1)
        results.append("published")

    tc = threading.Thread(target=consumer)
    tp = threading.Thread(target=publisher)
    tc.start()
    time.sleep(0.02)
    tp.start()
    time.sleep(0.05)
    sub.close()
    tc.join(2)
    tp.join(2)
    assert not tc.is_alive()
    assert not tp.is_alive()
    assert "published" in results
    assert bus.publish("t", 2) == 0  # subscription detached


def test_get_timeout_returns_none(bus: EventBus) -> None:
    sub = bus.subscribe("t")
    t0 = time.monotonic()
    assert sub.get(timeout=0.05) is None
    assert time.monotonic() - t0 >= 0.04


def test_context_manager_closes(bus: EventBus) -> None:
    with bus.subscribe("t") as sub:
        bus.publish("t", 1)
    assert sub.closed
    assert bus.publish("t", 2) == 0


def test_invalid_maxsize(bus: EventBus) -> None:
    with pytest.raises(ValueError, match="maxsize"):
        bus.subscribe("t", maxsize=0)


def test_many_publishers_no_loss_with_large_queue(bus: EventBus) -> None:
    sub = bus.subscribe("t", maxsize=100_000)
    n_threads, per_thread = 8, 2_000

    def worker(k: int) -> None:
        for i in range(per_thread):
            bus.publish("t", (k, i))

    threads = [threading.Thread(target=worker, args=(k,)) for k in range(n_threads)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    items = sub.drain()
    assert len(items) == n_threads * per_thread
    assert sub.stats().dropped == 0
    assert bus.stats().published == n_threads * per_thread
