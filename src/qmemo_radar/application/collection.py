import asyncio
import logging
from collections.abc import Mapping

from qmemo_radar.application.ports import SourceCollector
from qmemo_radar.domain import SourceFetch

logger = logging.getLogger(__name__)


class MultiSourceCollector:
    """Runs every enabled collector concurrently; a crashing collector never stops the others.

    Each collector reads only its own source keys from the shared checkpoints, so new collectors
    must prefix their keys with their registry name (e.g. `rss:<feed>`). X keeps its historical
    `account:<handle>` and `query:<name>` keys so existing checkpoints stay valid.
    """

    def __init__(self, collectors: Mapping[str, SourceCollector]) -> None:
        self._collectors = dict(collectors)

    @property
    def names(self) -> list[str]:
        return list(self._collectors)

    async def collect(self, checkpoints: Mapping[str, str]) -> list[SourceFetch]:
        results = await asyncio.gather(
            *(collector.collect(checkpoints) for collector in self._collectors.values()),
            return_exceptions=True,
        )
        fetches: list[SourceFetch] = []
        for name, result in zip(self._collectors, results, strict=True):
            if isinstance(result, BaseException):
                if not isinstance(result, Exception):
                    raise result  # cancellation and interpreter exits are not source errors
                code = f"collector_failed:{type(result).__name__}"
                logger.warning(
                    "collector failed",
                    extra={"operation": "collect", "source_key": name, "error_code": code},
                )
                fetches.append(SourceFetch(source_key=name, error_code=code))
            else:
                fetches.extend(result)
        return fetches
