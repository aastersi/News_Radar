class RadarError(Exception):
    """Base application error."""


class PublishingDisabled(RadarError):
    """Raised when an external publisher is intentionally disabled."""


class ProductionAdapterNotConfigured(RadarError):
    """Raised when run mode is requested before production adapters are wired."""

