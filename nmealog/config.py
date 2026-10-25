from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path


def _env_bool(name: str, default: bool = False) -> bool:
    value = os.environ.get(name)
    if value is None:
        return default
    return value.strip().lower() in ("1", "true", "yes", "on")


@dataclass(frozen=True)
class Config:
    data_dir: Path
    user: str | None = None
    password_hash: str | None = None
    allow_no_auth: bool = False
    secure_cookies: bool = False
    trust_proxy: bool = False

    @property
    def db_path(self) -> Path:
        return self.data_dir / "nmealog.sqlite3"

    @property
    def backup_dir(self) -> Path:
        return self.data_dir / "backups"

    @property
    def secret_key_path(self) -> Path:
        return self.data_dir / "secret_key"

    def ensure_dirs(self) -> None:
        self.backup_dir.mkdir(parents=True, exist_ok=True)


def load_config() -> Config:
    return Config(
        data_dir=Path(os.environ.get("NMEALOG_DATA_DIR", "/data")),
        user=os.environ.get("NMEALOG_USER") or None,
        password_hash=os.environ.get("NMEALOG_PASSWORD_HASH") or None,
        allow_no_auth=_env_bool("NMEALOG_ALLOW_NO_AUTH"),
        secure_cookies=_env_bool("NMEALOG_SECURE_COOKIES"),
        trust_proxy=_env_bool("NMEALOG_TRUST_PROXY", True),
    )
