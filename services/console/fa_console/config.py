"""Runtime settings from the environment (no login: bind to loopback by default)."""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

LOOPBACK = {"127.0.0.1", "localhost", "::1"}


@dataclass
class Settings:
    home: Path = field(default_factory=lambda: Path(os.environ.get(
        "FA_HOME", Path.home() / ".futureagri" / "console")))
    cache: Path | None = field(default_factory=lambda: (Path(os.environ["FA_DATA_CACHE"])
                                                        if os.environ.get("FA_DATA_CACHE") else None))
    bind: str = field(default_factory=lambda: os.environ.get("FA_BIND", "127.0.0.1"))
    port: int = field(default_factory=lambda: int(os.environ.get("FA_PORT", "8099")))
    local_workers: int = field(default_factory=lambda: int(os.environ.get("FA_LOCAL_WORKERS", "1")))
    max_upload_mb: int = 50
    max_ortho_mb: int = field(default_factory=lambda: int(os.environ.get("FA_MAX_ORTHO_MB", "4096")))
    offline: bool = field(default_factory=lambda: os.environ.get("FA_OFFLINE", "") == "1")

    def __post_init__(self):
        self.home = Path(self.home)
        if self.cache is not None:
            self.cache = Path(self.cache)

    @property
    def db_path(self) -> Path:
        return self.home / "console.db"

    @property
    def jobs_dir(self) -> Path:
        return self.home / "jobs"

    @property
    def lan_exposed(self) -> bool:
        return self.bind not in LOOPBACK

    def data_cache(self):
        from fa_farm.sources.base import Cache
        return Cache(self.cache) if self.cache is not None else Cache()
