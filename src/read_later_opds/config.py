import os


def get_readwise_token() -> str | None:
    """Single-user self-host mode: one token from the environment."""
    return os.environ.get("READWISE_TOKEN")


def get_database_path() -> str:
    return os.environ.get("DATABASE_PATH", "./data/app.db")


def get_stripe_secret_key() -> str | None:
    return os.environ.get("STRIPE_SECRET_KEY")


def get_stripe_payment_link() -> str | None:
    return os.environ.get("STRIPE_PAYMENT_LINK")


def get_base_url() -> str:
    return os.environ.get("BASE_URL", "http://localhost:8000").rstrip("/")


def allow_free_signup() -> bool:
    return os.environ.get("ALLOW_FREE_SIGNUP", "").lower() in ("1", "true", "yes")


def get_encryption_key() -> str | None:
    """Fernet key for token encryption at rest. Generate with:
    python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
    """
    return os.environ.get("ENCRYPTION_KEY")


def trust_proxy_headers() -> bool:
    """Only honor fly-client-ip / x-forwarded-for behind a known proxy."""
    return os.environ.get("TRUST_PROXY_HEADERS", "").lower() in ("1", "true", "yes")


def get_stats_token() -> str | None:
    """When set, the landing page records a server-side referrer log (no IPs or
    cookies) and `/stats?token=<STATS_TOKEN>` shows it. Unset (default) = off, so
    self-hosters collect nothing unless they opt in."""
    return os.environ.get("STATS_TOKEN") or None


def get_stats_retention_days() -> int:
    """How long referrer-log hits are kept before being pruned on write.

    Defaults to 90 days so the log doesn't grow without bound — the privacy
    claim is only true if old rows actually go away. Set STATS_RETENTION_DAYS=0
    to keep everything (opt back into unbounded growth)."""
    raw = os.environ.get("STATS_RETENTION_DAYS")
    if raw is None:
        return 90
    try:
        days = int(raw)
    except ValueError:
        return 90
    return days if days >= 0 else 90


def get_wallabag_config() -> dict[str, str] | None:
    """Self-host Wallabag connector settings, or None if not fully configured.

    Wallabag's API needs an OAuth2 client (client id/secret) plus the account
    username/password. All five must be present to enable the connector.
    """
    keys = {
        "url": "WALLABAG_URL",
        "client_id": "WALLABAG_CLIENT_ID",
        "client_secret": "WALLABAG_CLIENT_SECRET",
        "username": "WALLABAG_USERNAME",
        "password": "WALLABAG_PASSWORD",
    }
    values = {k: os.environ.get(env, "").strip() for k, env in keys.items()}
    if not all(values.values()):
        return None
    values["url"] = values["url"].rstrip("/")
    return values


def get_readwise_categories() -> tuple[str, ...]:
    """Readwise categories to surface, e.g. READWISE_CATEGORIES=article,pdf.
    Defaults to every supported category."""
    raw = os.environ.get("READWISE_CATEGORIES")
    if not raw:
        return ("article", "email", "pdf", "epub", "video", "tweet", "podcast")
    return tuple(c.strip() for c in raw.split(",") if c.strip())
