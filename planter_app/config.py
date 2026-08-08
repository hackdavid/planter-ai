"""Application configuration loaded from environment variables."""

import os
from pathlib import Path
from dataclasses import dataclass


def _load_dotenv() -> None:
    """Load .env file if python-dotenv is available."""
    try:
        from dotenv import load_dotenv
        env_path = Path(__file__).parent.parent / ".env"
        if env_path.exists():
            load_dotenv(dotenv_path=env_path, override=False)
    except ImportError:
        pass


@dataclass(frozen=True)
class Settings:
    """Immutable application settings."""

    google_places_api_key: str
    google_gemini_api_key: str
    cache_db_path: Path
    cache_ttl_days: int = 7
    default_quantity_target: int = 5000
    default_grid_radius_meters: int = 2000
    street_view_search_radius_meters: int = 30
    chain_blacklist_path: Path = Path(__file__).parent / "data" / "chain_blacklist.json"
    request_timeout: int = 30
    max_api_calls: int = 100
    rate_limit_delay_seconds: float = 0.5
    replicate_api_token: str | None = None
    replicate_model: str = "black-forest-labs/flux-kontext-pro"

    @classmethod
    def from_env(cls) -> "Settings":
        """Load settings from environment variables with sensible defaults.

        API keys are optional — the app can start without them and will
        only raise if a service that needs the key is actually invoked.
        This lets reviewers test the demo endpoint without providing keys.
        """
        _load_dotenv()
        base_dir = Path(__file__).parent
        data_dir = base_dir / "data"
        data_dir.mkdir(parents=True, exist_ok=True)

        places_key = os.getenv("GOOGLE_PLACES_API_KEY", "")
        gemini_key = os.getenv("GOOGLE_GEMINI_API_KEY", places_key)

        return cls(
            google_places_api_key=places_key,
            google_gemini_api_key=gemini_key,
            cache_db_path=Path(
                os.getenv("CACHE_DB_PATH", str(data_dir / "venue_cache.db"))
            ),
            cache_ttl_days=int(os.getenv("CACHE_TTL_DAYS", "7")),
            default_quantity_target=int(os.getenv("DEFAULT_QUANTITY_TARGET", "5000")),
            default_grid_radius_meters=int(os.getenv("DEFAULT_GRID_RADIUS_METERS", "2000")),
            street_view_search_radius_meters=int(os.getenv("STREET_VIEW_SEARCH_RADIUS_METERS", "30")),
            request_timeout=int(os.getenv("REQUEST_TIMEOUT", "30")),
            max_api_calls=int(os.getenv("MAX_API_CALLS", "100")),
            rate_limit_delay_seconds=float(os.getenv("RATE_LIMIT_DELAY_SECONDS", "0.5")),
            replicate_api_token=os.getenv("REPLICATE_API_TOKEN") or None,
            replicate_model=os.getenv("REPLICATE_MODEL", "black-forest-labs/flux-kontext-pro"),
        )
