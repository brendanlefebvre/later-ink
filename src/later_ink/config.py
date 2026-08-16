import os
from pathlib import Path


def load_env_file() -> Path | None:
    """Populate os.environ from the first env file found, and return its path.

    Checked in order: $XDG_CONFIG_HOME/later-ink/env (~/.config/later-ink/env
    by default), then ./.env. Variables already present in the real environment
    are never overwritten, so `READWISE_TOKEN=... python -m uvicorn ...` still
    takes precedence over the file.
    """
    xdg = os.environ.get("XDG_CONFIG_HOME", "").strip() or str(Path.home() / ".config")
    for path in (Path(xdg) / "later-ink" / "env", Path(".env")):
        # A candidate we cannot even stat is not a fatal condition, but
        # Path.is_file() only swallows "not there" errors — a permission denial
        # propagates, and this runs at import, so it takes the whole app down.
        # The container hit exactly that: it drops to an unprivileged user
        # while HOME still pointed at root's 0700 home directory.
        try:
            if not path.is_file():
                continue
            contents = path.read_text()
        except OSError:
            continue
        for raw in contents.splitlines():
            line = raw.strip()
            if not line or line.startswith("#"):
                continue
            key, sep, value = line.partition("=")
            key = key.strip()
            if not sep or not key:
                continue
            value = value.strip()
            if len(value) >= 2 and value[0] == value[-1] and value[0] in ("'", '"'):
                value = value[1:-1]
            os.environ.setdefault(key, value)
        return path.resolve()
    return None


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


def signups_enabled() -> bool:
    """True when /start can create users — i.e. this is a multi-tenant instance.

    Distinguishes the two deployment modes: a self-hoster runs with their own
    READWISE_TOKEN and no signup path, so nothing is ever persisted to the user
    table. Once signups are open, stored tokens must survive a restart, which
    is why this gates the ENCRYPTION_KEY requirement in main.lifespan.
    """
    return allow_free_signup() or bool(get_stripe_secret_key())


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


def _int_env(name: str, default: int) -> int:
    raw = os.environ.get(name)
    if raw is None:
        return default
    try:
        value = int(raw)
    except ValueError:
        return default
    return value if value >= 0 else default


def get_feed_rate_limit() -> int:
    """Catalog/feed requests allowed per minute per IP. 0 disables.

    Generous by default: a single e-reader browsing folders and pulling a book
    makes a handful of requests, so this only catches something hammering the
    upstream API. Worth raising on a self-hosted instance where several devices
    share one address behind NAT."""
    return _int_env("RATE_LIMIT_FEED_PER_MIN", 60)


def get_signup_rate_limit() -> int:
    """/start requests allowed per hour per IP. 0 disables.

    Covers both the form and the submission, so a real signup — including
    retyping a rejected token a few times — stays well under it, while bulk
    user creation and Stripe-verification hammering do not."""
    return _int_env("RATE_LIMIT_SIGNUP_PER_HOUR", 20)


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


def get_epub_cache_dir() -> str | None:
    """Where to cache generated EPUBs. Unset (default) = off.

    Off by default because the app otherwise stores nothing: it reads the
    queue live and holds no article content. Turning this on trades that for
    byte-stable downloads, which is what reading-progress sync needs on an
    article whose images are slow enough to fetch differently between runs.

    In Docker, put it under /data so it lands on the volume that already
    persists and the entrypoint can take ownership of it before dropping
    privileges.
    """
    return os.environ.get("EPUB_CACHE_DIR", "").strip() or None


def get_epub_cache_max_bytes() -> int:
    """Total cache size before least-recently-used entries are dropped.

    0 turns the cache off, matching the rate-limit settings above."""
    return _int_env("EPUB_CACHE_MAX_BYTES", 512 * 1024 * 1024)
