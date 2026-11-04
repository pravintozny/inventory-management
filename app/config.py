"""Runtime configuration (environment driven)."""
import os


def database_url() -> str:
    return os.environ.get("DATABASE_URL", "sqlite:///./inventory.db")


def allow_self_approval() -> bool:
    """Purchase orders may not be approved by their creator unless explicitly enabled."""
    return os.environ.get("ALLOW_SELF_APPROVAL", "false").lower() in {"1", "true", "yes"}
