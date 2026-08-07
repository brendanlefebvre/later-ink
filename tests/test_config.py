import os

import pytest

from later_ink import config


@pytest.fixture
def env_dir(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    monkeypatch.chdir(tmp_path)
    d = tmp_path / "config" / "later-ink"
    d.mkdir(parents=True)
    return d


def test_loads_xdg_env_file(env_dir, monkeypatch):
    monkeypatch.delenv("READWISE_TOKEN", raising=False)
    (env_dir / "env").write_text("READWISE_TOKEN=from-xdg\n")
    assert config.load_env_file() == env_dir / "env"
    assert os.environ["READWISE_TOKEN"] == "from-xdg"


def test_real_environment_wins_over_file(env_dir, monkeypatch):
    monkeypatch.setenv("READWISE_TOKEN", "from-env")
    (env_dir / "env").write_text("READWISE_TOKEN=from-file\n")
    config.load_env_file()
    assert os.environ["READWISE_TOKEN"] == "from-env"


def test_falls_back_to_dotenv_in_cwd(env_dir, monkeypatch, tmp_path):
    monkeypatch.delenv("READWISE_TOKEN", raising=False)
    (tmp_path / ".env").write_text("READWISE_TOKEN=from-dotenv\n")
    assert config.load_env_file() == tmp_path / ".env"
    assert os.environ["READWISE_TOKEN"] == "from-dotenv"


def test_xdg_file_preferred_over_dotenv(env_dir, monkeypatch, tmp_path):
    monkeypatch.delenv("READWISE_TOKEN", raising=False)
    (env_dir / "env").write_text("READWISE_TOKEN=from-xdg\n")
    (tmp_path / ".env").write_text("READWISE_TOKEN=from-dotenv\n")
    config.load_env_file()
    assert os.environ["READWISE_TOKEN"] == "from-xdg"


def test_no_file_is_a_noop(env_dir):
    assert config.load_env_file() is None


def test_parsing_skips_comments_blanks_and_strips_quotes(env_dir, monkeypatch):
    for key in ("READWISE_TOKEN", "BASE_URL", "STATS_TOKEN"):
        monkeypatch.delenv(key, raising=False)
    (env_dir / "env").write_text(
        "# a comment\n"
        "\n"
        'READWISE_TOKEN="quoted"\n'
        "BASE_URL='single'\n"
        "STATS_TOKEN=with=equals\n"
        "not-a-valid-line\n"
    )
    config.load_env_file()
    assert os.environ["READWISE_TOKEN"] == "quoted"
    assert os.environ["BASE_URL"] == "single"
    assert os.environ["STATS_TOKEN"] == "with=equals"
    assert "not-a-valid-line" not in os.environ
