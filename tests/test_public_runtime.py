"""Initialize only new disposable DBs from paired synthetic keys; no provider requests."""
import json
import os
import sqlite3

import pytest

from app import config, models
from app.agent import public_budget
from app.agent.managed_bridge import Settings
from app.agent.managed_store import PublicProviderCall
from app.agent.store import transaction
from scripts import public_design_runtime as runtime


@pytest.fixture
def isolated_runtime(tmp_path, monkeypatch):
    monkeypatch.setattr(os, "environ", dict(os.environ))
    monkeypatch.setattr(runtime, "DATA_ROOT", tmp_path / "new-public-design")
    monkeypatch.setattr(runtime, "SECRETS_ROOT", tmp_path / "new-secrets")
    monkeypatch.setattr(config, "_load_env", lambda: None)
    env = tmp_path / "provider-input.json"
    env.write_text("{}")
    env.chmod(0o600)
    keys = {key: "synthetic-paired-key-" + key.replace("_", "-") + "x" * 32 for key in runtime.PAIRING_FIELDS}
    pairing = tmp_path / "pairing-input.json"
    pairing.write_text(json.dumps(keys))
    pairing.chmod(0o600)
    models.reset_engine_for_tests()
    try:
        yield env, pairing, keys
    finally:
        models.reset_engine_for_tests()
        config.get_config.cache_clear()


def test_new_public_init_pairing_accounts_and_restart_preserves_ledger(isolated_runtime):
    env, pairing, keys = isolated_runtime
    cfg = runtime.load_runtime(env)
    runtime.initialize(pairing)
    secret_before = cfg.design_demo_secret_file.read_bytes()
    settings = Settings()
    for name, purpose in [("package_reader", "design_package_reader"), ("event_writer", "design_event_writer"),
                          ("asset_reader", "design_asset_reader")]:
        assert settings.credential(name)["token"] == keys[purpose]
    with models.session() as db:
        assert db.query(models.Project).count() == db.query(models.DesignerSession).count() == 0
        assert {u.username: u.role for u in db.query(models.DesignerUser)} == {
            "design-employee-a": "designer", "design-manager": "manager"}
    with transaction() as db:
        public_budget.reserve_dispatch(db, "text", "synthetic-unknown-dispatch", "synthetic-run")
    models.reset_engine_for_tests()
    runtime.load_runtime(env)
    runtime.initialize(pairing)
    assert cfg.design_demo_secret_file.read_bytes() == secret_before
    with models.session() as db:
        assert public_budget.remaining(db, "text") == 19
        assert db.query(PublicProviderCall).count() == 1
    assert (runtime.DATA_ROOT / "design.sqlite3").stat().st_mode & 0o777 == 0o600


def test_private_env_whitelist_and_immutable_paths(isolated_runtime):
    env, _, _ = isolated_runtime
    for body in [{"DATABASE_URL": "sqlite:////private/local.sqlite3"}, {"DESIGN_CREATION_AUTHORIZATION_FILE": "x"},
                 {"MODEL_API_KEY": 12}]:
        env.write_text(json.dumps(body))
        with pytest.raises(ValueError):
            runtime.load_runtime(env)
    env.write_text("{}")
    env.chmod(0o644)
    with pytest.raises(ValueError):
        runtime.load_runtime(env)


def test_foreign_database_not_migrated_or_overwritten(isolated_runtime):
    env, pairing, _ = isolated_runtime
    runtime.load_runtime(env)
    database = runtime.DATA_ROOT / "design.sqlite3"
    with sqlite3.connect(database) as db:
        db.execute("CREATE TABLE private_photo (value TEXT)")
        db.execute("INSERT INTO private_photo VALUES ('synthetic-private-sentinel')")
    before = database.read_bytes()
    with pytest.raises(sqlite3.OperationalError):
        runtime.initialize(pairing)
    assert database.read_bytes() == before
    assert not (runtime.SECRETS_ROOT / "design-pairing.json").exists()


def test_pairing_input_must_match_root_keys_and_no_rotation(isolated_runtime):
    env, pairing, keys = isolated_runtime
    runtime.load_runtime(env)
    runtime.initialize(pairing)
    original = (runtime.SECRETS_ROOT / "design-pairing.json").read_bytes()
    keys["design_asset_reader"] += "new"
    pairing.write_text(json.dumps(keys))
    with pytest.raises(ValueError):
        runtime.initialize(pairing)
    assert (runtime.SECRETS_ROOT / "design-pairing.json").read_bytes() == original


def test_ambient_private_configuration_never_read(isolated_runtime):
    env, _, _ = isolated_runtime
    os.environ["DESIGN_CREATION_AUTHORIZATION_FILE"] = "/private/local-grant"
    os.environ["MODEL_API_KEY"] = "synthetic-ambient-secret"
    os.environ["DESIGN_MANAGER_PASSWORD_HASH"] = "synthetic-old-account-hash"
    cfg = runtime.load_runtime(env)
    assert "MODEL_API_KEY" not in os.environ
    assert "DESIGN_CREATION_AUTHORIZATION_FILE" not in os.environ
    assert not cfg.design_manager_password_hash
    assert not cfg.design_paid_providers_enabled


def test_private_operator_can_restore_normal_login_without_resetting_public_budget(isolated_runtime):
    env, pairing, _ = isolated_runtime
    runtime.load_runtime(env)
    runtime.initialize(pairing)
    with transaction() as db:
        public_budget.reserve_dispatch(db, "image", "synthetic-before-auth-restore", "synthetic-run")
    env.write_text(json.dumps({"DESIGN_DEMO_ACCESS_ENABLED": "false"}))
    cfg = runtime.load_runtime(env)
    assert not cfg.design_demo_access_enabled and cfg.public_demo
    with models.session() as db:
        assert public_budget.remaining(db, "image") == 3
