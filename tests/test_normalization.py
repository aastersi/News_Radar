from datetime import UTC, datetime

from qmemo_radar.application.normalization import build_candidate
from qmemo_radar.domain import RawSourceItem, SourceType


def test_normalization_keeps_original_and_canonicalizes_url() -> None:
    item = RawSourceItem(
        source=SourceType.X,
        external_id="1",
        url="https://twitter.com/alice/status/1/?utm_source=test",
        original_text="  A   quoted\nstatement  ",
        published_at=datetime.now(UTC),
    )

    event = build_candidate(item)

    assert event.original_text == "  A   quoted\nstatement  "
    assert event.normalized_text == "a quoted statement"
    assert str(event.url) == "https://x.com/alice/status/1"
    assert len(event.content_hash) == 64

