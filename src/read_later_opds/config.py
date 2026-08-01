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


def get_readwise_categories() -> tuple[str, ...]:
    """Readwise categories to surface, e.g. READWISE_CATEGORIES=article,book,pdf.
    Defaults to articles + books."""
    raw = os.environ.get("READWISE_CATEGORIES")
    if not raw:
        return ("article", "email", "pdf", "epub", "video", "tweet", "podcast")
    return tuple(c.strip() for c in raw.split(",") if c.strip())
