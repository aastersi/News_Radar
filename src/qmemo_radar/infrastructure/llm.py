"""Small OpenAI-compatible chat client. Prompts, outputs and keys are never logged."""

import asyncio
import logging
from collections.abc import Callable

import httpx
from pydantic import ValidationError

from qmemo_radar.infrastructure.http import HttpFailure, Sleep, send_with_retry

logger = logging.getLogger(__name__)

_REPAIR_INSTRUCTION = (
    "Your previous answer could not be accepted: {error}. "
    "Answer again with a single JSON object that follows the required format exactly. "
    "Do not add any text outside the JSON."
)


class ChatCompletionsClient:
    def __init__(
        self,
        http: httpx.AsyncClient,
        *,
        model: str,
        temperature: float = 0.0,
        sleep: Sleep = asyncio.sleep,
    ) -> None:
        self._http = http
        self.model = model
        self._temperature = temperature
        self._sleep = sleep

    async def complete(self, messages: list[dict[str, str]], *, max_tokens: int) -> str:
        payload = {
            "model": self.model,
            "messages": messages,
            "temperature": self._temperature,
            "max_tokens": max_tokens,
            # Providers without JSON mode ignore this; the reply is validated either way.
            "response_format": {"type": "json_object"},
        }
        response = await send_with_retry(
            self._http, "POST", "chat/completions", json=payload, sleep=self._sleep
        )
        try:
            content = response.json()["choices"][0]["message"]["content"]
        except (ValueError, KeyError, IndexError, TypeError) as exc:
            raise HttpFailure("invalid_response") from exc
        if not isinstance(content, str):
            raise HttpFailure("invalid_response")
        return content


async def complete_with_repair[T](
    client: ChatCompletionsClient,
    *,
    system: str,
    user: str,
    parse: Callable[[str], T],
    max_tokens: int,
    operation: str,
) -> T:
    """One request plus at most one format-repair request. The second failure propagates."""
    messages = [{"role": "system", "content": system}, {"role": "user", "content": user}]
    content = await client.complete(messages, max_tokens=max_tokens)
    try:
        return parse(content)
    except ValueError as exc:
        logger.warning(
            "llm output rejected, requesting one repair",
            extra={"operation": operation, "result": "repair", "error_code": type(exc).__name__},
        )
        messages += [
            {"role": "assistant", "content": content},
            {"role": "user", "content": _REPAIR_INSTRUCTION.format(error=_describe(exc))},
        ]
    return parse(await client.complete(messages, max_tokens=max_tokens))


def extract_json_object(content: str) -> str:
    start, end = content.find("{"), content.rfind("}")
    if start == -1 or end < start:
        raise ValueError("the answer contains no JSON object")
    return content[start : end + 1]


def _describe(exc: ValueError) -> str:
    if isinstance(exc, ValidationError):
        # Locations and messages only: never echo the (untrusted) input values back.
        problems = [
            f"{'.'.join(str(part) for part in error['loc'])}: {error['msg']}"
            for error in exc.errors(include_input=False, include_url=False)
        ]
        return "; ".join(problems)[:1000]
    return str(exc)[:1000]
