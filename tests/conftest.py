import pytest

# Connector-defining environment variables. config.load_env_file() runs at
# import (main.py) and copies a developer's local ./.env into os.environ, so a
# machine that self-hosts against Readwise or Wallabag would otherwise register
# real connectors during app startup — pushing len(_connectors) past 1 and
# flipping the adaptive /opds/ root from the single-connector flatten to the
# connector chooser. That silently broke the self-host tests in test_app.py on
# exactly such a machine while CI (no ./.env) stayed green. Strip these before
# every test so the suite is hermetic; tests that want a connector set the vars
# explicitly, which runs after this autouse fixture and overrides it.
_CONNECTOR_ENV = (
    "READWISE_TOKEN",
    "WALLABAG_URL",
    "WALLABAG_CLIENT_ID",
    "WALLABAG_CLIENT_SECRET",
    "WALLABAG_USERNAME",
    "WALLABAG_PASSWORD",
)


@pytest.fixture(autouse=True)
def _isolate_connector_env(monkeypatch):
    for var in _CONNECTOR_ENV:
        monkeypatch.delenv(var, raising=False)
