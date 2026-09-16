from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import yaml
from pydantic import BaseModel, ConfigDict, Field, SecretStr, model_validator
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
    sources_path: Path = Path("sources.yaml")

    telegram_bot_token: SecretStr | None = None
    allowed_telegram_id: int | None = None

    x_bearer_token: SecretStr | None = None
    llm_base_url: str | None = None
    llm_api_key: SecretStr | None = None
    llm_model: str | None = None
    llm_temperature: float = Field(default=0.0, ge=0.0, le=1.0)

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



class _SourcesModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class XAccountSource(_SourcesModel):
    handle: str = Field(pattern=r"^[A-Za-z0-9_]{1,15}$")
    enabled: bool = True


class XQuerySource(_SourcesModel):
    name: str = Field(pattern=r"^[a-z0-9_]{1,40}$")
    query: str = Field(min_length=1, max_length=4096)
    enabled: bool = True


class XSources(_SourcesModel):
    accounts: tuple[XAccountSource, ...] = ()
    queries: tuple[XQuerySource, ...] = ()
    max_pages_per_query: int = Field(default=3, ge=1, le=10)


class SourcesConfig(_SourcesModel):
    """Contents of sources.yaml: what to read from X and what to always drop."""

    x: XSources = XSources()
    blocked_authors: tuple[str, ...] = ()
    blocked_terms: tuple[str, ...] = ()

    @model_validator(mode="after")
    def validate_unique_keys(self) -> "SourcesConfig":
        handles = [account.handle.casefold() for account in self.x.accounts]
        names = [query.name for query in self.x.queries]
        if len(handles) != len(set(handles)) or len(names) != len(set(names)):
            raise ValueError("Account handles and query names in sources.yaml must be unique")
        return self


def load_sources(path: Path) -> SourcesConfig:
    return SourcesConfig.model_validate(yaml.safe_load(path.read_text(encoding="utf-8")) or {})
