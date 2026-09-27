"""3D task ownership, billing guard and persistence, without paid provider calls."""

import io
import zipfile

import httpx
import pytest
from fastapi.testclient import TestClient
from PIL import Image

from app import config, models
from app.agent import models3d
from app.agent.assets import save_image
from app.agent.migrate import migrate
from app.agent.store import AgentError, create, require, transaction


@pytest.fixture()
def setup(tmp_path, monkeypatch):
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path / 'agent.sqlite3'}")
    monkeypatch.setenv("ASSETS_DIR", str(tmp_path / "assets"))
    monkeypatch.setenv("AGENT_WORKER_ENABLED", "false")
    monkeypatch.setenv("AGENT_3D_ENABLED", "true")
    monkeypatch.setenv("AGENT_3D_API_KEY", "synthetic-test-key")
    monkeypatch.setenv("AGENT_3D_MONTHLY_ALLOCATION_FEN", "180")
    config.get_config.cache_clear()
    models.reset_engine_for_tests()
    models.init_db()
    migrate()
    with models.session() as db:
        project = models.Project(name="3D 合成测试")
        db.add(project)
        db.commit()
        pid = project.id
    image = io.BytesIO()
    Image.new("RGB", (128, 128), "red").save(image, format="PNG")
    saved = save_image(image.getvalue())
    with transaction() as db:
        version = create(db, pid, "version", {"image": saved, "spec_id": "test-spec"}, "ready_for_review")
        vid = version.id
    yield pid, vid, tmp_path
    models.reset_engine_for_tests()
    config.get_config.cache_clear()


def test_one_version_one_3d_task_and_budget(setup):
    pid, version_id, _ = setup
    first = models3d.submit(version_id, models3d.Generate3DIn(authorized=True, idempotency_key="test-key-1"))
    repeated = models3d.submit(version_id, models3d.Generate3DIn(authorized=True, idempotency_key="test-key-2"))
    assert repeated["id"] == first["id"]
    with transaction() as db:
        another = create(db, pid, "version", {"image": require(db, version_id).payload["image"]}, "ready_for_review")
        another_id = another.id
    with pytest.raises(AgentError) as error:
        models3d.submit(another_id, models3d.Generate3DIn(authorized=True, idempotency_key="test-key-3"))
    assert error.value.code == "3D_BUDGET_EXHAUSTED"


def test_task_uses_only_selected_image_and_serves_saved_glb(setup, monkeypatch):
    _, version_id, tmp_path = setup
    task = models3d.submit(version_id, models3d.Generate3DIn(authorized=True, idempotency_key="test-key-1"))
    calls = []

    def fake_request(method, url, *, json_body=None):
        calls.append((method, url, json_body))
        return {"id": "cgt-synthetic12345"} if method == "POST" else {
            "status": "succeeded", "content": {"file_url": "https://valid.volces.com/model.zip"},
            "usage": {"completion_tokens": 30000},
        }

    filename = "a" * 32 + ".glb"
    root = tmp_path / "assets" / "design-agent" / "models3d"
    root.mkdir(parents=True)
    (root / filename).write_bytes(b"glTF" + (2).to_bytes(4, "little") + (12).to_bytes(4, "little"))
    monkeypatch.setattr(models3d, "_request", fake_request)
    monkeypatch.setattr(models3d, "_download_model", lambda url: filename)
    models3d.tick()
    assert calls[0][0] == "POST"
    assert calls[0][2]["content"][0]["image_url"]["url"].startswith("data:image/png;base64,")
    assert len([part for part in calls[0][2]["content"] if part["type"] == "image_url"]) == 1
    with transaction() as db:
        row = require(db, task["id"], "model3d")
        row.payload = {**row.payload, "next_poll_at": 0}
    models3d.tick()
    with transaction() as db:
        row = require(db, task["id"], "model3d")
        assert row.status == "succeeded"
        assert row.payload["version_id"] == version_id
        assert row.payload["usage"]["completion_tokens"] == 30000
    from app.main import app
    with TestClient(app) as client:
        response = client.get(f"/api/design-models3d/{task['id']}/file")
        assert response.status_code == 200
        assert response.headers["content-type"].startswith("model/gltf-binary")
        assert response.content[:4] == b"glTF"


def test_submitting_after_restart_is_not_replayed(setup, monkeypatch):
    _, version_id, _ = setup
    task = models3d.submit(version_id, models3d.Generate3DIn(authorized=True, idempotency_key="test-key-1"))
    with transaction() as db:
        require(db, task["id"], "model3d").status = "submitting"
        models3d.recover(db)
    monkeypatch.setattr(models3d, "_request", lambda *args, **kwargs: pytest.fail("charged request replayed"))
    models3d.tick()
    with transaction() as db:
        assert require(db, task["id"], "model3d").status == "interrupted"


def test_disabled_route_never_creates_a_paid_task(setup, monkeypatch):
    _, version_id, _ = setup
    monkeypatch.setenv("AGENT_3D_ENABLED", "false")
    from app.main import app
    with TestClient(app) as client:
        response = client.post(
            f"/api/design-versions/{version_id}/model3d",
            json={"authorized": True, "idempotency_key": "test-key-1"},
        )
    assert response.status_code == 503
    assert response.json()["error"]["code"] == "3D_NOT_CONFIGURED"
    with transaction() as db:
        assert list(db.query(models3d.Record).filter_by(kind="model3d")) == []


def test_model_archive_requires_glb_and_vendor_https(setup, monkeypatch):
    archive = io.BytesIO()
    valid = b"glTF" + (2).to_bytes(4, "little") + (12).to_bytes(4, "little")
    with zipfile.ZipFile(archive, "w") as out:
        out.writestr("output/model.glb", valid)
    original_client = httpx.Client
    transport = httpx.MockTransport(lambda request: httpx.Response(200, content=archive.getvalue()))
    monkeypatch.setattr(models3d.httpx, "Client", lambda **kwargs: original_client(transport=transport))
    filename = models3d._download_model("https://files.volces.com/model.zip")
    assert filename.endswith(".glb")
    with pytest.raises(AgentError) as error:
        models3d._download_model("http://localhost/private")
    assert error.value.code == "3D_FILE_HOST_INVALID"
