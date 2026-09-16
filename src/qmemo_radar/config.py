from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import Field, SecretStr, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class RadarSettings(BaseSettings):
    """Runtime configuration loaded only at the composition root."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_prefix="RADAR_",
        extra="ignore",
        case_sensitive=False,
    )

    environment: str = "development"
    log_level: str = "INFO"
    timezone: str = "Asia/Ho_Chi_Minh"
    db_path: Path = Path("data/radar.db")

    telegram_bot_token: SecretStr | None = None
    allowed_telegram_id: int | None = None

    x_bearer_token: SecretStr | None = None
    llm_base_url: str | None = None
    llm_api_key: SecretStr | None = None
    llm_model: str | None = None

    collect_interval_minutes: int = Field(default=30, ge=5, le=1440)
    max_event_age_minutes: int = Field(default=60, ge=5, le=10080)
    daily_card_limit: int = Field(default=10, ge=1, le=50)
    urgent_threshold: int = Field(default=80, ge=0, le=100)
    digest_threshold: int = Field(default=65, ge=0, le=100)
    archive_threshold: int = Field(default=50, ge=0, le=100)
    event_ttl_hours: int = Field(default=48, ge=1, le=720)

    qmemo_publishing_enabled: bool = False
    x_publishing_enabled: bool = False

    @model_validator(mode="after")
    def validate_thresholds_and_timezone(self) -> "RadarSettings":
        if not (
            self.archive_threshold <= self.digest_threshold <= self.urgent_threshold
        ):
            raise ValueError(
                "Thresholds must satisfy archive <= digest <= urgent"
            )
        try:
            ZoneInfo(self.timezone)
        except ZoneInfoNotFoundError as exc:
            raise ValueError(f"Unknown timezone: {self.timezone}") from exc
        return self

    def ensure_data_directory(self) -> None:
        self.db_path.parent.mkdir(parents=True, exist_ok=True)

