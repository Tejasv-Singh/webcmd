"""Configuration, read from the environment with .env as a fallback.

Deliberately dependency-free: no pydantic, no dotenv package. Kosh must be runnable
on a clean machine with nothing but Python, because a day of missed collection is
permanent data loss and "I couldn't install the deps" is not an acceptable reason.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent


def _load_dotenv(path: Path) -> None:
    """Populate os.environ from a .env file. Existing env vars always win."""
    if not path.exists():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, val = line.partition("=")
        key, val = key.strip(), val.strip().strip('"').strip("'")
        os.environ.setdefault(key, val)


_load_dotenv(REPO_ROOT / ".env")


def _path(value: str) -> Path:
    p = Path(value)
    return p if p.is_absolute() else REPO_ROOT / p


@dataclass(frozen=True)
class Settings:
    database_url: str
    blob_root: Path
    min_request_interval: float
    user_agent: str
    http_timeout: float
    max_catchup_days: int
    webcmd_profile_dir: Path
    drift_threshold: float
    drift_window: int
    ring3_enabled: bool

    @classmethod
    def from_env(cls) -> Settings:
        return cls(
            database_url=os.environ.get("KOSH_DATABASE_URL", "sqlite:///data/kosh.sqlite"),
            blob_root=_path(os.environ.get("KOSH_BLOB_ROOT", "data/blobs")),
            min_request_interval=float(os.environ.get("KOSH_MIN_REQUEST_INTERVAL", "1.5")),
            user_agent=os.environ.get("KOSH_USER_AGENT", "kosh/0.1 (+market-data-research)"),
            http_timeout=float(os.environ.get("KOSH_HTTP_TIMEOUT", "30")),
            max_catchup_days=int(os.environ.get("KOSH_MAX_CATCHUP_DAYS", "30")),
            webcmd_profile_dir=_path(
                os.environ.get("KOSH_WEBCMD_PROFILE_DIR", "data/webcmd-profiles")
            ),
            drift_threshold=float(os.environ.get("KOSH_DRIFT_THRESHOLD", "0.4")),
            drift_window=int(os.environ.get("KOSH_DRIFT_WINDOW", "14")),
            ring3_enabled=os.environ.get("KOSH_RING3_ENABLED", "false").lower() == "true",
        )


def settings() -> Settings:
    return Settings.from_env()
