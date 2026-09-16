from dataclasses import dataclass
from datetime import timedelta

from qmemo_radar.application.filtering import FilterPolicy
from qmemo_radar.application.pipeline import PipelineThresholds, RadarPipeline
from qmemo_radar.application.ports import Ranker, SourceCollector
from qmemo_radar.config import RadarSettings
from qmemo_radar.infrastructure.storage import SQLiteEventRepository


@dataclass(frozen=True, slots=True)
class Application:
    settings: RadarSettings
    repository: SQLiteEventRepository


def build_application(settings: RadarSettings | None = None) -> Application:
    resolved = settings or RadarSettings()
    resolved.ensure_data_directory()
    return Application(
        settings=resolved,
        repository=SQLiteEventRepository(resolved.db_path),
    )


def build_pipeline(
    application: Application,
    *,
    collector: SourceCollector,
    ranker: Ranker,
) -> RadarPipeline:
    settings = application.settings
    return RadarPipeline(
        collector=collector,
        ranker=ranker,
        repository=application.repository,
        filter_policy=FilterPolicy(
            max_age=timedelta(minutes=settings.max_event_age_minutes),
        ),
        thresholds=PipelineThresholds(
            archive=settings.archive_threshold,
            digest=settings.digest_threshold,
            urgent=settings.urgent_threshold,
        ),
    )

