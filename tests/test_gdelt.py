"""GDELT Global Quotation Graph collector: cursor, gaps, errors, limits and idempotency."""

import gzip
import json
import sqlite3
from collections.abc import Callable, Mapping
from datetime import UTC, datetime, timedelta

import httpx
import pytest

from qmemo_radar.application.collection import MultiSourceCollector
from qmemo_radar.application.filtering import FilterPolicy
from qmemo_radar.application.pipeline import PipelineThresholds, RadarPipeline
from qmemo_radar.application.ports import SourceCollector
from qmemo_radar.infrastructure.collectors import gdelt_gqg
from qmemo_radar.infrastructure.collectors.gdelt_gqg import SOURCE_KEY, GdeltQuotationCollector
from qmemo_radar.infrastructure.storage import SQLiteEventRepository

NOW = datetime(2026, 9, 16, 12, 30, 30, tzinfo=UTC)
LAG = timedelta(minutes=10)  # newest minute ever requested: 12:20


def minute(hhmm: str) -> str:
    return f"20260916{hhmm.replace(':', '')}00"


def article(url: str, *quotes: str, lang: str = "ENGLISH") -> dict[str, object]:
    return {
        "date": "2026-09-16T12:05:59Z",
        "url": url,
        "title": "Budget",
        "lang": lang,
        "quotes": [
            {"pre": "The minister said ", "quote": text, "post": " on Monday."} for text in quotes
        ],
    }


def gz(*rows: object) -> bytes:
    lines = [row if isinstance(row, bytes) else json.dumps(row).encode() for row in rows]
    return gzip.compress(b"\n".join(lines) + b"\n")


FIRST = 'We will not raise taxes this year, whatever happens in parliament'
SECOND = 'The budget is balanced for the first time in a decade and it stays so'
SPANISH = "Una frase bastante larga para el radar"
FRENCH = "Une phrase assez longue pour le radar"


class Gdelt:
    """Fake data.gdeltproject.org: files by minute, anything else is 404."""

    def __init__(self, files: Mapping[str, bytes | int | Callable[[], httpx.Response]]) -> None:
        self.files = dict(files)
        self.requested: list[str] = []

    def handler(self, request: httpx.Request) -> httpx.Response:
        assert request.url.host == "data.gdeltproject.org"
        name = request.url.path.rsplit("/", 1)[-1].removesuffix(".gqg.json.gz")
        self.requested.append(name)
        found = self.files.get(name, 404)
        if callable(found):
            return found()
        if isinstance(found, int):
            return httpx.Response(found)
        return httpx.Response(200, content=found)

    def collector(self, **overrides: object) -> GdeltQuotationCollector:
        async def no_sleep(_: float) -> None:
            return None

        options: dict[str, object] = {
            "safety_lag": LAG,
            "max_minutes_per_run": 60,
            "first_run_lookback": timedelta(minutes=30),  # first minute: 12:00
            "languages": frozenset({"english"}),
            "allow_unknown_language": False,
            "clock": lambda: NOW,
            "sleep": no_sleep,
        } | overrides
        http = httpx.AsyncClient(transport=httpx.MockTransport(self.handler))
        return GdeltQuotationCollector(http, **options)  # type: ignore[arg-type]


def pipeline(collector: SourceCollector, repository: SQLiteEventRepository) -> RadarPipeline:
    return RadarPipeline(
        collector=MultiSourceCollector({"gdelt_gqg": collector}),
        ranker=None,
        repository=repository,
        filter_policy=FilterPolicy(max_age=timedelta(days=36500)),
        thresholds=PipelineThresholds(),
    )


def stored(repository: SQLiteEventRepository) -> list[tuple[object, ...]]:
    with sqlite3.connect(repository._db_path) as db:
        return db.execute(
            "SELECT url, original_text, author_display_name, language, published_at,"
            " raw_payload_json FROM radar_events ORDER BY rowid"
        ).fetchall()


async def test_quotes_of_a_file_are_stored_one_row_each_and_a_rerun_adds_nothing(
    repository: SQLiteEventRepository,
) -> None:
    gdelt = Gdelt(
        {
            minute("12:05"): gz(
                article("https://news.example/a", FIRST, SECOND),
                article("https://news.example/b", FIRST),  # same quote in another article
            )
        }
    )
    collector = gdelt.collector()

    first = await pipeline(collector, repository).run_once(run_id="r1")
    rows = stored(repository)
    second = await pipeline(collector, repository).run_once(run_id="r2")

    assert gdelt.requested[:3] == [minute("12:00"), minute("12:01"), minute("12:02")]
    assert gdelt.requested[20] == minute("12:20") and len(gdelt.requested) == 21
    assert (first.collected, first.inserted, first.duplicates, first.source_errors) == (3, 3, 1, 0)
    assert [row[:4] for row in rows] == [
        ("https://news.example/a", FIRST, None, "ENGLISH"),
        ("https://news.example/a", SECOND, None, "ENGLISH"),
        ("https://news.example/b", FIRST, None, "ENGLISH"),
    ]
    assert rows[0][4] == "2026-09-16T12:05:59+00:00"
    assert json.loads(str(rows[0][5])) == {
        "title": "Budget",
        "pre": "The minister said ",
        "quote": FIRST,
        "post": " on Monday.",
        "url": "https://news.example/a",
        "lang": "ENGLISH",
        "date": "2026-09-16T12:05:59Z",
        "gqg_file": minute("12:05"),
    }
    # The cursor is the next minute to check; the rerun starts there and finds nothing new.
    assert await repository.get_checkpoints() == {SOURCE_KEY: minute("12:21")}
    assert (second.collected, second.inserted) == (0, 0)
    assert len(gdelt.requested) == 21  # 12:21 is still inside the safety window

    metrics = await repository.metrics_since(datetime(2000, 1, 1, tzinfo=UTC))
    assert metrics[SOURCE_KEY] == {
        "articles_seen": 2,
        "collected": 3,
        "exact_duplicates": 1,
        "expected_gaps": 20,
        "files_checked": 21,
        "files_found": 1,
        "inserted": 3,
        "quotes_accepted": 3,
        "quotes_seen": 3,
    }
    health = {item.source_key: item for item in await repository.source_health()}
    assert health[SOURCE_KEY].last_error is None


async def test_repeating_a_file_or_losing_the_checkpoint_creates_no_duplicates(
    repository: SQLiteEventRepository,
) -> None:
    body = gz(article("https://news.example/a", FIRST, FIRST, SECOND))
    gdelt = Gdelt({minute("12:05"): body, minute("12:06"): body})

    first = await pipeline(gdelt.collector(), repository).run_once()
    with sqlite3.connect(repository._db_path) as db:
        db.execute("DELETE FROM source_checkpoints")  # checkpoint lost: whole window again
    again = await pipeline(gdelt.collector(), repository).run_once()

    assert (first.collected, first.inserted, first.duplicates) == (6, 2, 4)
    assert (again.collected, again.inserted, again.duplicates) == (6, 0, 6)
    assert len(stored(repository)) == 2


async def test_404_minutes_are_gaps_and_the_safety_window_is_never_requested(
    repository: SQLiteEventRepository,
) -> None:
    gdelt = Gdelt({})
    collector = gdelt.collector(clock=lambda: NOW, first_run_lookback=timedelta(minutes=12))

    counters = await pipeline(collector, repository).run_once()

    # 12:18..12:20 are older than the lag; 12:21..12:30 might still be published.
    assert gdelt.requested == [minute("12:18"), minute("12:19"), minute("12:20")]
    assert counters.source_errors == 0
    assert await repository.get_checkpoints() == {SOURCE_KEY: minute("12:21")}

    later = gdelt.collector(clock=lambda: NOW + timedelta(minutes=2))
    await pipeline(later, repository).run_once()
    assert gdelt.requested[3:] == [minute("12:21"), minute("12:22")]
    assert await repository.get_checkpoints() == {SOURCE_KEY: minute("12:23")}


@pytest.mark.parametrize(
    ("failure", "code"),
    [
        (503, "gdelt_server_error"),
        (lambda: (_ for _ in ()).throw(httpx.ReadTimeout("slow")), "gdelt_network_error"),
        (403, "gdelt_http_403"),
    ],
)
async def test_a_failing_minute_is_retried_never_skipped(
    repository: SQLiteEventRepository, failure: object, code: str
) -> None:
    gdelt = Gdelt(
        {
            minute("12:02"): gz(article("https://news.example/a", FIRST)),
            minute("12:04"): failure,  # type: ignore[dict-item]
            minute("12:06"): gz(article("https://news.example/b", SECOND)),
        }
    )

    first = await pipeline(gdelt.collector(), repository).run_once()
    retries = gdelt.requested.count(minute("12:04"))
    assert (first.inserted, first.source_errors) == (1, 1)
    # Progress up to the failure is kept, the failing minute becomes the cursor.
    assert await repository.get_checkpoints() == {SOURCE_KEY: minute("12:04")}
    assert minute("12:05") not in gdelt.requested

    second = await pipeline(gdelt.collector(), repository).run_once()
    assert gdelt.requested.count(minute("12:04")) == 2 * retries
    assert await repository.get_checkpoints() == {SOURCE_KEY: minute("12:04")}
    [health] = [h for h in await repository.source_health() if h.source_key == SOURCE_KEY]
    # No progress in the second run, so the failures keep counting up.
    assert (health.last_error, health.consecutive_failures) == (code, 2)
    assert second.source_errors == 1

    gdelt.files.pop(minute("12:04"))
    third = await pipeline(gdelt.collector(), repository).run_once()
    assert third.inserted == 1 and third.source_errors == 0
    assert await repository.get_checkpoints() == {SOURCE_KEY: minute("12:21")}
    metrics = await repository.metrics_since(datetime(2000, 1, 1, tzinfo=UTC))
    assert metrics[SOURCE_KEY]["source_errors"] == 2
    # Run 1: 12:00, 12:01, 12:03. Run 3: 12:04 (now missing), 12:05 and 12:07..12:20.
    assert metrics[SOURCE_KEY]["expected_gaps"] == 3 + 0 + 16
    assert retries == (3 if code != "gdelt_http_403" else 1)


async def test_malformed_rows_and_filtered_languages_are_counted_and_skipped(
    repository: SQLiteEventRepository,
) -> None:
    quote_too_long = "x" * (gdelt_gqg.MAX_QUOTE_CHARS + 1)
    gdelt = Gdelt(
        {
            minute("12:05"): gz(
                b"{not json",
                b"[1, 2]",
                b"\xff\xfe broken utf-8",
                {"url": "https://news.example/no-quotes", "lang": "ENGLISH", "date": "x"},
                article("https://news.example/es", SPANISH, lang="SPANISH"),
                article("https://news.example/unknown", "A quote without any language", lang=""),
                article("not a url", "A quote from an article without a valid address"),
                article("https://news.example/ok", FIRST, "", quote_too_long),
            )
        }
    )

    counters = await pipeline(gdelt.collector(), repository).run_once()

    assert counters.inserted == 1 and counters.source_errors == 0
    metrics = await repository.metrics_since(datetime(2000, 1, 1, tzinfo=UTC))
    counted = metrics[SOURCE_KEY]
    assert {k: v for k, v in counted.items() if k not in {"files_checked", "expected_gaps"}} == {
        "articles_language_skipped": 2,
        "articles_seen": 5,
        "collected": 1,
        "files_found": 1,
        "inserted": 1,
        "malformed_rows": 4,
        "quotes_accepted": 1,
        "quotes_rejected": 2,  # empty quote and the invalid URL
        "quotes_seen": 6,
        "quotes_too_long": 1,
    }


async def test_several_languages_and_unknown_language_can_be_allowed(
    repository: SQLiteEventRepository,
) -> None:
    gdelt = Gdelt(
        {
            minute("12:05"): gz(
                article("https://news.example/en", FIRST),
                article("https://news.example/es", SPANISH, lang="Spanish"),
                article("https://news.example/fr", FRENCH, lang="FRENCH"),
                article("https://news.example/none", SECOND, lang=""),
            )
        }
    )
    collector = gdelt.collector(
        languages=frozenset({"english", "spanish"}), allow_unknown_language=True
    )

    await pipeline(collector, repository).run_once()

    assert [row[3] for row in stored(repository)] == ["ENGLISH", "Spanish", None]
    everything = Gdelt(gdelt.files).collector(languages=None, allow_unknown_language=False)
    [found, _] = await everything.collect({})
    assert len(found.items) == 3  # every named language, still no unknown one


async def test_a_corrupt_or_oversized_file_is_counted_and_passed(
    repository: SQLiteEventRepository, monkeypatch: pytest.MonkeyPatch
) -> None:
    good = gz(article("https://news.example/a", FIRST), article("https://news.example/b", SECOND))
    truncated_gzip = good[: len(good) // 2]
    gdelt = Gdelt(
        {
            minute("12:03"): b"this is not gzip at all",
            minute("12:04"): truncated_gzip,
            minute("12:05"): lambda: httpx.Response(200, content=b"x" * 2048),
            minute("12:06"): good,
        }
    )
    monkeypatch.setattr(gdelt_gqg, "MAX_COMPRESSED_BYTES", 1024)

    counters = await pipeline(gdelt.collector(), repository).run_once()

    assert counters.source_errors == 0 and counters.inserted == 2
    assert await repository.get_checkpoints() == {SOURCE_KEY: minute("12:21")}
    metrics = (await repository.metrics_since(datetime(2000, 1, 1, tzinfo=UTC)))[SOURCE_KEY]
    assert (metrics["files_corrupt"], metrics["files_oversized"]) == (2, 1)
    assert metrics["files_found"] == 3


async def test_decompressed_size_and_article_count_are_capped(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    rows = [article(f"https://news.example/{n}", f"{FIRST} number {n}") for n in range(10)]
    gdelt = Gdelt({minute("12:05"): gz(*rows)})
    monkeypatch.setattr(gdelt_gqg, "MAX_ARTICLES_PER_FILE", 4)
    [found, final] = await gdelt.collector().collect({})
    assert len(found.items) == 4 and final.stats["files_truncated"] == 1

    monkeypatch.setattr(gdelt_gqg, "MAX_ARTICLES_PER_FILE", 100)
    monkeypatch.setattr(gdelt_gqg, "MAX_DECOMPRESSED_BYTES", 600)
    [found, final] = await gdelt.collector().collect({})
    assert 0 < len(found.items) < 10 and final.stats["files_truncated"] == 1

    monkeypatch.setattr(gdelt_gqg, "MAX_DECOMPRESSED_BYTES", 10**9)
    monkeypatch.setattr(gdelt_gqg, "MAX_LINE_BYTES", 400)
    huge = article("https://news.example/huge", "y" * 1000)
    gdelt.files[minute("12:05")] = gz(rows[0], huge, rows[1])
    [found, final] = await gdelt.collector().collect({})
    assert len(found.items) == 2 and final.stats["malformed_rows"] == 1


async def test_catch_up_after_downtime_is_bounded_per_run(
    repository: SQLiteEventRepository,
) -> None:
    gdelt = Gdelt({minute("10:30"): gz(article("https://news.example/old", FIRST))})
    await repository.record_source_result(SOURCE_KEY, cursor=minute("10:00"), error_code=None)
    collector = gdelt.collector(max_minutes_per_run=45)

    for expected in ("10:45", "11:30", "12:15", "12:21", "12:21"):
        await pipeline(collector, repository).run_once()
        assert await repository.get_checkpoints() == {SOURCE_KEY: minute(expected)}

    assert len(gdelt.requested) == 141 == len(set(gdelt.requested))  # 10:00..12:20, once each
    assert len(stored(repository)) == 1


async def test_an_unreadable_cursor_restarts_from_the_lookback_window() -> None:
    gdelt = Gdelt({})
    fetches = await gdelt.collector().collect({SOURCE_KEY: "garbage"})
    assert gdelt.requested[0] == minute("12:00")
    assert fetches[-1].cursor == minute("12:21")


def test_gdelt_is_opt_in_and_needs_no_key() -> None:
    from qmemo_radar.bootstrap import SourceContext, build_collector, enabled_sources
    from qmemo_radar.config import RadarSettings, SourcesConfig

    off = RadarSettings(_env_file=None)  # type: ignore[call-arg]
    on = RadarSettings(_env_file=None, gdelt_enabled=True, gdelt_languages="English, spanish")  # type: ignore[call-arg]
    assert enabled_sources(off, SourcesConfig()) == []
    assert enabled_sources(on, SourcesConfig()) == ["gdelt_gqg"]
    assert on.gdelt_language_set == frozenset({"english", "spanish"})
    assert RadarSettings(_env_file=None, gdelt_languages="*").gdelt_language_set is None  # type: ignore[call-arg]
    context = SourceContext(on, SourcesConfig(), x_client=None, free_http=httpx.AsyncClient())
    assert build_collector(context).names == ["gdelt_gqg"]


async def test_poison_rows_are_skipped_and_never_stall_the_cursor(
    repository: SQLiteEventRepository,
) -> None:
    # Regression: a lone surrogate or a deeply nested row used to crash the collector or the
    # SQLite insert on every run, so the cursor never moved.
    surrogate_quote = article("https://news.example/s1", FIRST)
    surrogate_title = article("https://news.example/s2", SECOND) | {"title": "\ud800"}
    surrogate_quote["quotes"] = [{"quote": "Lone \ud800 surrogate in the quote"}]
    deep = b'{"quotes": ' + b"[" * 200_000 + b"]" * 200_000 + b"}"
    normal = "A perfectly normal quote that must be stored"
    good = article("https://news.example/good", normal)
    rows = [json.dumps(row).encode() for row in (surrogate_quote, surrogate_title, good)]
    body = gzip.compress(b"\n".join([rows[0], rows[1], deep, rows[2]]))
    gdelt = Gdelt({minute("12:05"): body})

    first = await pipeline(gdelt.collector(), repository).run_once()
    with sqlite3.connect(repository._db_path) as db:
        db.execute("DELETE FROM source_checkpoints")
    second = await pipeline(gdelt.collector(), repository).run_once()

    assert (first.inserted, first.source_errors) == (1, 0)
    assert (second.inserted, second.source_errors) == (0, 0)
    assert [row[1] for row in stored(repository)] == [normal]
    assert await repository.get_checkpoints() == {SOURCE_KEY: minute("12:21")}
    metrics = (await repository.metrics_since(datetime(2000, 1, 1, tzinfo=UTC)))[SOURCE_KEY]
    assert metrics["malformed_rows"] == 2  # the deep row, once per run
    assert metrics["quotes_rejected"] == 2  # the surrogate quote, once per run
    assert metrics["invalid_items"] == 2  # the surrogate title reaches the pipeline and stops there
