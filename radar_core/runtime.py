"""Daemon and healthcheck scaffolding shared by every radar.

The app supplies the tick coroutine and cleanup; the core owns the scheduler
wiring, signal handling and the graceful-shutdown discipline (an in-flight
tick finishes before resources close — killing it mid-alert would drop ledger
writes and cause duplicate alerts after restart).
"""

from __future__ import annotations

import asyncio
import logging
import signal
import sqlite3
import sys
from collections.abc import Awaitable, Callable, Coroutine
from datetime import UTC, datetime

from apscheduler.schedulers.asyncio import AsyncIOScheduler

from radar_core.store import TIMESTAMP_FMT

log = logging.getLogger(__name__)

_MIN_HEALTH_THRESHOLD_S = 900


def setup_logging() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    # httpx logs every request URL at INFO — noisy for a daemon, and URLs must
    # never carry secrets anyway.
    logging.getLogger("httpx").setLevel(logging.WARNING)


async def run_daemon(
    name: str,
    tick: Callable[[], Awaitable[object]],
    interval_s: int,
    cleanup: Callable[[], Awaitable[None]] | None = None,
    background: tuple[Callable[[], Coroutine[None, None, None]], ...] = (),
) -> None:
    """Run `tick` every interval_s (first run immediately) until SIGINT/SIGTERM.

    Serializes ticks and waits for the in-flight one on shutdown, then runs
    `cleanup` (close HTTP clients etc.).
    """
    tick_lock = asyncio.Lock()

    async def guarded_tick() -> None:
        async with tick_lock:
            await tick()

    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, stop.set)

    scheduler = AsyncIOScheduler(timezone="UTC")
    scheduler.add_job(
        guarded_tick,
        "interval",
        seconds=interval_s,
        next_run_time=datetime.now(UTC),
        max_instances=1,
        coalesce=True,
    )
    scheduler.start()
    tasks: list[asyncio.Task[None]] = [
        asyncio.create_task(factory()) for factory in background
    ]
    log.info("%s daemon started (tick every %ds)", name, interval_s)
    try:
        await stop.wait()
    finally:
        for task in tasks:
            task.cancel()
        scheduler.shutdown(wait=False)
        async with tick_lock:  # let an in-flight tick finish
            pass
        if cleanup is not None:
            await cleanup()
        log.info("%s daemon stopped", name)


def healthcheck_main(db_path: str, meta_key: str, tick_interval_s: int) -> int:
    """Exit-code healthcheck: 0 if the last tick (meta[meta_key], UTC) is
    recent, 1 otherwise. Sync sqlite3 — a one-shot probe needs no async."""
    threshold_s = max(3 * tick_interval_s, _MIN_HEALTH_THRESHOLD_S)
    try:
        conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
        try:
            row = conn.execute(
                "SELECT value FROM meta WHERE key = ?", (meta_key,)
            ).fetchone()
        finally:
            conn.close()
    except sqlite3.Error as exc:
        print(f"unhealthy: cannot read {db_path}: {exc}", file=sys.stderr)
        return 1
    if row is None:
        print("unhealthy: no tick recorded yet", file=sys.stderr)
        return 1
    last_tick = datetime.strptime(row[0], TIMESTAMP_FMT).replace(tzinfo=UTC)
    age_s = (datetime.now(UTC) - last_tick).total_seconds()
    if age_s > threshold_s:
        print(f"unhealthy: last tick {age_s:.0f}s ago (> {threshold_s}s)", file=sys.stderr)
        return 1
    print(f"healthy: last tick {age_s:.0f}s ago")
    return 0
