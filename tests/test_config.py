import pytest
from pydantic import ValidationError

from qmemo_radar.config import RadarSettings


def test_threshold_order_is_validated() -> None:
    with pytest.raises(ValidationError):
        RadarSettings(archive_threshold=80, digest_threshold=65, urgent_threshold=70)


def test_publishing_is_disabled_by_default() -> None:
    settings = RadarSettings(_env_file=None)
    assert settings.qmemo_publishing_enabled is False
    assert settings.x_publishing_enabled is False

