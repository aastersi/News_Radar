class RadarError(Exception):
    """Base application error."""


class PublishingDisabled(RadarError):
    """Raised when an external publisher is intentionally disabled."""


class ProductionAdapterNotConfigured(RadarError):
    """Raised when run mode is requested before production adapters are wired."""


class SourceUnavailable(RadarError):
    """A read-only source request failed after the allowed retries."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


class RankingFailed(RadarError):
    """A ranking batch could not be scored with a valid structured result."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code
