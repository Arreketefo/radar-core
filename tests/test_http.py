import json

import httpx
import pytest

from radar_core.http import CircuitBreaker, CircuitOpenError, get_json, post_json
from radar_core.ratelimit import TokenBucket


def bucket() -> TokenBucket:
    return TokenBucket(rate=1000.0, capacity=1000.0)


async def test_get_json_success():
    client = httpx.AsyncClient(
        transport=httpx.MockTransport(lambda r: httpx.Response(200, json={"ok": 1}))
    )
    assert await get_json(client, "https://x.test/a", {}, bucket()) == {"ok": 1}


async def test_post_json_sends_body():
    captured = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured.update(json.loads(request.content))
        return httpx.Response(200, json={"ok": 1})

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    await post_json(client, "https://x.test/a", {"k": "v"}, bucket())
    assert captured == {"k": "v"}


async def test_non_retryable_error_raises_immediately():
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        return httpx.Response(404)

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    with pytest.raises(httpx.HTTPStatusError):
        await get_json(client, "https://x.test/a", {}, bucket())
    assert calls["n"] == 1  # 404 is not retried


async def test_breaker_opens_after_consecutive_failures():
    client = httpx.AsyncClient(
        transport=httpx.MockTransport(lambda r: httpx.Response(404))
    )
    breaker = CircuitBreaker(threshold=3, cooldown_s=600.0)
    for _ in range(3):
        with pytest.raises(httpx.HTTPStatusError):
            await get_json(client, "https://x.test/a", {}, bucket(), breaker)
    with pytest.raises(CircuitOpenError):
        await get_json(client, "https://x.test/a", {}, bucket(), breaker)


async def test_breaker_success_resets_failure_count():
    responses = iter([404, 404, 200, 404, 404, 404])
    client = httpx.AsyncClient(
        transport=httpx.MockTransport(
            lambda r: httpx.Response(next(responses), json={})
        )
    )
    breaker = CircuitBreaker(threshold=3, cooldown_s=600.0)
    for expect_error in (True, True, False):
        if expect_error:
            with pytest.raises(httpx.HTTPStatusError):
                await get_json(client, "https://x.test/a", {}, bucket(), breaker)
        else:
            await get_json(client, "https://x.test/a", {}, bucket(), breaker)
    # Two more failures after the reset: still below threshold -> closed
    for _ in range(2):
        with pytest.raises(httpx.HTTPStatusError):
            await get_json(client, "https://x.test/a", {}, bucket(), breaker)
    breaker.ensure_closed()  # must not raise
