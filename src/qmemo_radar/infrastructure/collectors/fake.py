from collections.abc import Mapping

from qmemo_radar.domain import RawSourceItem, SourceFetch


class FakeCollector:
    def __init__(self, items: list[RawSourceItem], *, source_key: str = "fake") -> None:
        self._items = items
        self._source_key = source_key

    async def collect(self, checkpoints: Mapping[str, str]) -> list[SourceFetch]:
        return [SourceFetch(source_key=self._source_key, items=tuple(self._items))]
