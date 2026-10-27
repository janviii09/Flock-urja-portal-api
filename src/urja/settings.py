"""Typed application settings — all secrets come from environment variables."""

from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    """Configuration loaded from env vars (or .env file via python-dotenv)."""

    # Portal access
    urja_base_url: str = "https://urja-ops.flockenergy.tech"
    urja_email: str = ""
    urja_password: str = ""
    portal_timeout: float = 15.0
    portal_concurrency: int = 5
    portal_max_retries: int = 3
    portal_record_captures: bool = False
    portal_captures_dir: str = "captures"

    # Service
    log_level: str = "INFO"
    db_path: str = "urja.db"

    # Freshness defaults (seconds)
    default_ttl: int = 300  # 5 min
    stale_while_revalidate: int = 60  # serve stale for 1 min while refreshing

    model_config = {"env_file": ".env", "env_file_encoding": "utf-8"}


# Singleton — import this everywhere.
settings = Settings()
