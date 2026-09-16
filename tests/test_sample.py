"""`qmemo-radar sample`: a read-only look at stored items without SQL."""

import hashlib
import json
import os
from datetime import UTC, datetime
from pathlib import Path

import pytest

from qmemo_radar.application.normalization import build_candidate
from qmemo_radar.domain import RawSourceItem, SourceType
from qmemo_radar.infrastructure.storage import SQLiteEventRepository
from qmemo_radar.interfaces.cli import build_parser, execute


def item(source: SourceType, number: int, key: str) -> RawSourceItem:
    return RawSourceItem(
        source=source,
        external_id=f"{source.value}-{number}",
        url=f"https://news.example/{number}",
        original_text=f"Quote number {number} " + "x" * 600,
        language="ENGLISH",
        published_at=datetime(2026, 9, 16, 12, number, tzinfo=UTC),
        raw_payload={"title": f"Title {number}"},
        source_key=key,
    )


EMPTY = hashlib.sha256(b"").hexdigest()


def files_digest(folder: Path) -> dict[str, str]:
    return {
        path.name: hashlib.sha256(path.read_bytes()).hexdigest() for path in folder.iterdir()
    }


async def test_sample_shows_newest_items_of_a_source_and_changes_nothing(
    repository: SQLiteEventRepository,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.chdir(repository._db_path.parent)  # no stray .env from the working directory
    await repository.add_events(
        [
            build_candidate(item(SourceType.GDELT, 1, "gdelt:gqg")),
            build_candidate(item(SourceType.RSS, 2, "rss:wire")),
            build_candidate(item(SourceType.GDELT, 3, "gdelt:gqg")),
        ]
    )
    before = files_digest(repository._db_path.parent)

    assert await execute("sample", db_path=repository._db_path, source="GDELT", limit=5) == 0
    gdelt = json.loads(capsys.readouterr().out)
    assert await execute("sample", db_path=repository._db_path, source="rss:wire", random=True) == 0
    rss = json.loads(capsys.readouterr().out)

    assert [entry["title"] for entry in gdelt["items"]] == ["Title 3", "Title 1"]
    first = gdelt["items"][0]
    assert first["source"] == "gdelt:gqg" and first["url"] == "https://news.example/3"
    assert len(first["text"]) == 500 and first["published_at"] == "2026-09-16T12:03:00+00:00"
    assert (rss["count"], rss["items"][0]["title"]) == (1, "Title 2")
    after = files_digest(repository._db_path.parent)
    assert after["radar.db"] == before["radar.db"]
    # A read-only reader of a WAL database may create the shared-memory file, never write data.
    assert after.get("radar.db-wal", EMPTY) == EMPTY


async def test_sample_never_creates_a_database(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    assert await execute("sample", db_path=tmp_path / "missing.db") == 1
    assert "database not found" in capsys.readouterr().out
    assert os.listdir(tmp_path) == []


def test_sample_limit_is_capped() -> None:
    parser = build_parser()
    assert parser.parse_args(["sample", "--limit", "100"]).limit == 100
    for bad in ("0", "101"):
        with pytest.raises(SystemExit):
            parser.parse_args(["sample", "--limit", bad])
