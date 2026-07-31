import os


def get_readwise_token() -> str | None:
    return os.environ.get("READWISE_TOKEN")
