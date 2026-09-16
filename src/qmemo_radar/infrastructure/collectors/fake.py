from qmemo_radar.domain import RawSourceItem


class FakeCollector:
    def __init__(self, items: list[RawSourceItem]) -> None:
        self._items = items

    async def collect(self) -> list[RawSourceItem]:
        return list(self._items)

