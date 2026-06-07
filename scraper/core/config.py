from __future__ import annotations

from pathlib import Path
from typing import Optional

import yaml
from pydantic import BaseModel, Field


class StorageConfig(BaseModel):
    backend: str = "sqlite"
    path: str = "data/scraped.db"


class HttpConfig(BaseModel):
    timeout: float = 20.0
    max_retries: int = 4
    http2: bool = True
    proxy: Optional[str] = None


class RateLimitConfig(BaseModel):
    bilibili: float = 1.5
    zhihu: float = 1.0
    weibo: float = 0.8
    tieba: float = 0.8


class PlatformAuth(BaseModel):
    cookie: str = ""


class LoggingConfig(BaseModel):
    level: str = "INFO"
    file: Optional[str] = "data/scraper.log"


class Config(BaseModel):
    storage: StorageConfig = Field(default_factory=StorageConfig)
    http: HttpConfig = Field(default_factory=HttpConfig)
    rate_limit: RateLimitConfig = Field(default_factory=RateLimitConfig)
    bilibili: PlatformAuth = Field(default_factory=PlatformAuth)
    zhihu: PlatformAuth = Field(default_factory=PlatformAuth)
    weibo: PlatformAuth = Field(default_factory=PlatformAuth)
    tieba: PlatformAuth = Field(default_factory=PlatformAuth)
    logging: LoggingConfig = Field(default_factory=LoggingConfig)


def load_config(path: str | Path = "config.yaml") -> Config:
    p = Path(path)
    if not p.exists():
        # Fall back to defaults so first-run examples work without a config file.
        return Config()
    with p.open("r", encoding="utf-8") as f:
        raw = yaml.safe_load(f) or {}
    return Config.model_validate(raw)
