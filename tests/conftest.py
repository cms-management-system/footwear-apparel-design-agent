import pytest

from app.config import get_config


@pytest.fixture(autouse=True)
def default_legacy_api_mode(monkeypatch):
    # Existing agent tests exercise the original project API without a team login.
    monkeypatch.setenv("DESIGN_AUTH_REQUIRED", "false")
    get_config.cache_clear()
    yield
    get_config.cache_clear()
