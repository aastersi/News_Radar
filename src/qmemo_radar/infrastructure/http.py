import asyncio
import logging
import time
from collections.abc import Awaitable, Callable, Mapping
from typing import Any

import httpx

logger = logging.getLogger(__name__)

Sleep = Callable[[float], Awaitable[None]]


class HttpFailure(Exception):
    """Final HTTP failure after retries. The message never contains credentials."""

    def __init__(self, code: str, status_code: int | None = None) -> None:
        super().__init__(code if status_code is None else f"{code} ({status_code})")
        self.code = code
        self.status_code = status_code


async def send_with_retry(
    client: httpx.AsyncClient,
    method: str,
    url: str,
    *,
    params: Mapping[str, str | int] | None = None,
    json: Any = None,
    attempts: int = 3,
    max_wait_seconds: float = 60.0,
    sleep: Sleep = asyncio.sleep,
) -> httpx.Response:
    """Retries network errors, 5xx and 429 up to `attempts` in total; other 4xx fail at once."""
    for attempt in range(1, attempts + 1):
        last = attempt == attempts
        try:
            response = await client.request(method, url, params=params, json=json)
        except httpx.TransportError as exc:
            logger.warning(
                "http request failed",
                extra={"operation": "http", "result": "retry", "error_code": type(exc).__name__},
            )
            if last:
                raise HttpFailure("network_error") from exc
            await sleep(float(2 ** (attempt - 1)))
            continue

        status = response.status_code
        if status == 429:
            wait = _rate_limit_wait(response)
            if last or wait > max_wait_seconds:
                raise HttpFailure("rate_limited", status)
            await sleep(wait)
            continue
        if status >= 500:
            if last:
                raise HttpFailure("server_error", status)
            await sleep(float(2 ** (attempt - 1)))
            continue
        if status >= 400:
            raise HttpFailure("client_error", status)
        return response
    raise AssertionError("unreachable")


def _rate_limit_wait(response: httpx.Response) -> float:
    retry_after = response.headers.get("retry-after")
    if retry_after and retry_after.strip().isdigit():
        return float(retry_after)
    reset = response.headers.get("x-rate-limit-reset")
    if reset and reset.strip().isdigit():
        return max(0.0, float(reset) - time.time())
    return 1.0
