import tomllib
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, Field, field_validator


class ScheduleConfig(BaseModel):
    cron: str
    episodes_per_run: int = Field(gt=0)


class RankingConfig(BaseModel):
    recency_half_life_days: float = Field(gt=0)
    topic_round_robin: bool = True   # cycle topics when filling episode slots

    def ranking_half_life(self) -> float:
        return self.recency_half_life_days


class TopicConfig(BaseModel):
    name: str
    weight: float = Field(ge=0)
    keywords: list[str]


class SourceConfig(BaseModel):
    name: str
    type: str = "rss"
    url: str
    weight: float = Field(ge=0)
    enabled: bool = True


class NotebookLMConfig(BaseModel):
    nlm_command: str = "nlm"
    audio_format: Literal["deep_dive", "brief", "critique", "debate"] = "deep_dive"
    audio_length: Literal["short", "default", "long"] = "default"
    focus: str = ""


class AudioConfig(BaseModel):
    gain_factor: float = Field(default=1.4, gt=0)   # 1.4 = +40% amplitude (~+2.9 dB)


class PlexConfig(BaseModel):
    base_url: str
    library_name: str
    target_folder: str


class ApisConfig(BaseModel):
    unpaywall_email: str


class NotificationsConfig(BaseModel):
    discord_webhook_keyring_key: str


class Config(BaseModel):
    schedule: ScheduleConfig
    ranking: RankingConfig
    topics: list[TopicConfig]
    sources: list[SourceConfig]
    notebooklm: NotebookLMConfig
    audio: AudioConfig = Field(default_factory=AudioConfig)   # optional; absent [audio] -> 1.4
    plex: PlexConfig
    apis: ApisConfig
    notifications: NotificationsConfig

    @field_validator("topics", "sources")
    @classmethod
    def non_empty(cls, v):
        if not v:
            raise ValueError("must define at least one entry")
        return v


def load_config(path: Path) -> Config:
    path = Path(path)
    if not path.exists():
        raise FileNotFoundError(f"config not found: {path}")
    with path.open("rb") as f:
        data = tomllib.load(f)
    return Config.model_validate(data)
