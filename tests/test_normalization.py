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


def test_canonical_url_stays_a_valid_url() -> None:
    from pydantic import HttpUrl

    from qmemo_radar.application.normalization import canonicalize_url

    # Regression: brackets of an IPv6 host were dropped and the query was re-encoded
    # (`/` -> `%2F`), so valid feed and GDELT URLs failed validation during ingestion.
    assert canonicalize_url("http://[2001:DB8::1]:8080/story/") == "http://[2001:db8::1]:8080/story"
    long_query = "https://news.example/r?u=" + "/" * 2000 + "&utm_source=x&UTM_Medium=y&a=b+c"
    assert canonicalize_url(long_query) == "https://news.example/r?u=" + "/" * 2000 + "&a=b+c"
    assert canonicalize_url("https://news.example/?utm%5Fsource=x&&id=1#top") == (
        "https://news.example?id=1"
    )
    HttpUrl(canonicalize_url(long_query))
