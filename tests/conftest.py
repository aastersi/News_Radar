import ipaddress
import socket
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import pytest

from qmemo_radar.infrastructure.storage import SQLiteEventRepository


@pytest.fixture(autouse=True)
def _block_external_network(monkeypatch: pytest.MonkeyPatch) -> None:
    """No test may reach X, an LLM, Telegram or Quote Memorial. Loopback stays open for asyncio."""
    original_connect = socket.socket.connect

    def guarded_connect(self: socket.socket, address: Any) -> Any:
        host = address[0] if isinstance(address, tuple) else None
        if host is not None and not ipaddress.ip_address(host).is_loopback:
            raise RuntimeError(f"Tests must not open network connections: {host}")
        return original_connect(self, address)

    monkeypatch.setattr(socket.socket, "connect", guarded_connect)


@pytest.fixture
async def repository(tmp_path: Path) -> AsyncIterator[SQLiteEventRepository]:
    repo = SQLiteEventRepository(tmp_path / "radar.db")
    await repo.initialize()
    yield repo
