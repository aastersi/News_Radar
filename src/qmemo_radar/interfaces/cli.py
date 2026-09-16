import argparse
import asyncio
import json
from datetime import UTC, datetime
from pathlib import Path
from typing import NoReturn

from pydantic import HttpUrl

from qmemo_radar.bootstrap import build_application, build_pipeline
from qmemo_radar.config import RadarSettings
from qmemo_radar.domain import Engagement, RawSourceItem, SourceType
from qmemo_radar.infrastructure.collectors import FakeCollector
from qmemo_radar.infrastructure.ranking import DeterministicFixtureRanker


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="qmemo-radar")
    parser.add_argument(
        "command",
        choices=("init-db", "status", "dry-run", "run"),
    )
    parser.add_argument("--db", type=Path, help="Override SQLite path")
    return parser


async def execute(command: str, *, db_path: Path | None = None) -> int:
    settings = RadarSettings(db_path=db_path) if db_path else RadarSettings()
    app = build_application(settings)

    if command == "init-db":
        await app.repository.initialize()
        print(json.dumps({"status": "ok", "database": str(settings.db_path)}))
        return 0

    if command == "status":
        await app.repository.initialize()
        counts = await app.repository.count_by_status()
        print(
            json.dumps(
                {
                    "status": "ok",
                    "database": str(settings.db_path),
                    "events": counts,
                    "qmemo_publishing": settings.qmemo_publishing_enabled,
                    "x_publishing": settings.x_publishing_enabled,
                },
                ensure_ascii=False,
            )
        )
        return 0

    if command == "dry-run":
        await app.repository.initialize()
        collector = FakeCollector([_sample_item()])
        pipeline = build_pipeline(
            app,
            collector=collector,
            ranker=DeterministicFixtureRanker(),
        )
        counters = await pipeline.run_once()
        print(counters.model_dump_json())
        return 0

    if command == "run":
        return _production_not_ready()

    raise AssertionError(f"Unknown command: {command}")


def _sample_item() -> RawSourceItem:
    return RawSourceItem(
        source=SourceType.X,
        external_id="dry-run-1",
        url=HttpUrl("https://x.com/example/status/dry-run-1?utm_source=test"),
        author_handle="example",
        author_display_name="Example Founder",
        original_text='Founder said: "Predictions should be remembered, not rewritten."',
        language="en",
        published_at=datetime.now(UTC),
        engagement=Engagement(likes=120, reposts=15, replies=40, quotes=8),
    )


def _production_not_ready() -> NoReturn:
    raise SystemExit(
        "Production run is fail-closed until X, LLM and Telegram adapters are configured. "
        "Use dry-run to verify the foundation."
    )


def main() -> None:
    args = build_parser().parse_args()
    raise SystemExit(asyncio.run(execute(args.command, db_path=args.db)))
