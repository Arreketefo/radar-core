import asyncio
import time

from radar_core.ratelimit import TokenBucket


async def test_burst_capacity_is_immediate():
    bucket = TokenBucket(rate=1.0, capacity=5.0)
    start = time.monotonic()
    for _ in range(5):
        await bucket.acquire()
    assert time.monotonic() - start < 0.1


async def test_refill_paces_beyond_burst():
    bucket = TokenBucket(rate=50.0, capacity=1.0)
    await bucket.acquire()
    start = time.monotonic()
    await bucket.acquire()  # must wait ~1/50 s for refill
    assert time.monotonic() - start >= 0.015


async def test_concurrent_acquirers_do_not_serialize_behind_sleeper():
    # Sleeping happens OUTSIDE the lock: two waiters make progress in parallel.
    bucket = TokenBucket(rate=100.0, capacity=2.0)
    await bucket.acquire()
    await bucket.acquire()  # bucket drained
    start = time.monotonic()
    await asyncio.gather(bucket.acquire(), bucket.acquire())
    # Both refill waits overlap: ~2/100 s total, not 2 sequential lock-held sleeps
    assert time.monotonic() - start < 0.2
