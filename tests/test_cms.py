"""CMS package import and design-response writeback. CMS is mocked; no live network."""

import io
import json
import sqlite3

import httpx
import pytest
from fastapi.testclient import TestClient
from PIL import Image

from app import config, models
from app.agent import cms
from app.agent.assets import save_image
from app.agent.migrate import migrate
from app.agent.store import create, transaction


def png():
    out = io.BytesIO()
    Image.new("RGB", (64, 64), "red").save(out, format="PNG")
    return out.getvalue()


class Response:
    def __init__(self, status, body):
        self.status_code = status
        self._body = body

    def json(self):
        if isinstance(self._body, Exception):
            raise self._body
        return self._body


class Scripted:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def request(self, method, url, headers=None, json=None):
        self.calls.append({"method": method, "url": url, "headers": headers, "json": json})
        item = self.responses.pop(0)
        if isinstance(item, Exception):
            raise item
        return item


@pytest.fixture()
def env(tmp_path, monkeypatch):
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path / 'cms.sqlite3'}")
    monkeypatch.setenv("ASSETS_DIR", str(tmp_path / "assets"))
    monkeypatch.setenv("AGENT_WORKER_ENABLED", "false")
    monkeypatch.setenv("CMS_API_KEY", "design-secret")
    monkeypatch.setenv("CMS_BASE_URL", "http://cms.test")
    monkeypatch.setenv("PUBLIC_BASE_URL", "http://public.test")
    monkeypatch.setenv("CMS_TIMEOUT", "10")
    config.get_config.cache_clear()
    models.reset_engine_for_tests()
    models.init_db()
    migrate()
    with models.session() as db:
        project = models.Project(name="CMS 设计项目")
        db.add(project)
        db.commit()
        pid = project.id
    yield pid
    models.reset_engine_for_tests()
    config.get_config.cache_clear()


def _script(monkeypatch, responses):
    scripted = Scripted(responses)
    monkeypatch.setattr(cms.httpx, "Client", lambda *args, **kwargs: scripted)
    return scripted


def _confirmed_version(pid):
    image = save_image(png())
    with transaction() as db:
        spec = create(
            db,
            pid,
            "spec",
            {"spec": {"intent": "方领连衣裙", "constraints": [{"id": "c1", "text": "保留方领"}]}},
            "confirmed",
        )
        version = create(
            db,
            pid,
            "version",
            {
                "spec_id": spec.id,
                "image": image,
                "design_index": 2,
                "review": {"summary": "领口符合方领"},
            },
            "confirmed",
        )
        return version.id


def _link(pid, package_id="PKG-1", body=None):
    raw = body or {
        "package_id": package_id,
        "signal_ids": ["SIG-1", "SIG-2"],
        "requirement_desc": "做一条方领连衣裙",
        "constraints": ["保留方领", {"text": "裙长到膝"}],
        "style_demand": "通勤",
        "version": 3,
        "status": "approved",
        "extra_field": "keep-me",
    }
    snapshot = cms.normalize_package(raw, package_id)
    with models.session() as db:
        project = db.get(models.Project, pid)
        project.cms_package_id = package_id
        project.cms_package_snapshot = json.dumps(snapshot, ensure_ascii=False)
        db.commit()
    return snapshot


def test_package_normalizes_missing_and_extra_fields(env, monkeypatch):
    raw = {"status": "approved", "constraints": "不能改领口", "note": {"from": "cms"}}
    scripted = _script(monkeypatch, [Response(200, raw)])
    from app.main import app

    with TestClient(app) as client:
        response = client.get("/api/cms/packages/PKG-9")
    assert response.status_code == 200
    body = response.json()
    assert body["package_id"] == "PKG-9"
    assert body["signal_count"] == 0
    assert body["requirement_desc"] == ""
    assert body["constraints_text"] == "不能改领口"
    assert body["raw"]["note"] == {"from": "cms"}
    assert scripted.calls[0]["headers"]["X-API-Key"] == "design-secret"
    assert scripted.calls[0]["url"] == "http://cms.test/api/packages/PKG-9"
    assert "design-secret" not in response.text


def test_package_errors_are_chinese(env, monkeypatch):
    from app.main import app

    _script(monkeypatch, [Response(403, {"error": {"code": "NOT_APPROVED", "message": "no"}})])
    with TestClient(app) as client:
        denied = client.get("/api/cms/packages/PKG-1")
    assert denied.status_code == 403
    assert denied.json()["error"]["message"] == cms.NOT_APPROVED

    _script(monkeypatch, [Response(404, {"error": {"code": "MISSING", "message": "gone"}})])
    with TestClient(app) as client:
        missing = client.get("/api/cms/packages/PKG-1")
    assert missing.status_code == 404
    assert "没有找到这个证据包" in missing.json()["error"]["message"]

    _script(monkeypatch, [httpx.TimeoutException("slow")])
    with TestClient(app) as client:
        timed_out = client.get("/api/cms/packages/PKG-1")
    assert timed_out.status_code == 504
    assert "超时" in timed_out.json()["error"]["message"]

    _script(monkeypatch, [httpx.ConnectError("down")])
    with TestClient(app) as client:
        offline = client.get("/api/cms/packages/PKG-1")
    assert offline.status_code == 502
    assert "无法连接 CMS" in offline.json()["error"]["message"]


def test_create_project_stores_package_without_leaking_key(env, monkeypatch):
    raw = {
        "package_id": "PKG-1",
        "status": "approved",
        "signal_ids": ["SIG-7"],
        "requirement_desc": "低跟乐福鞋",
        "constraints": ["保留圆头"],
        "style_demand": "简洁",
        "version": 1,
    }
    _script(monkeypatch, [Response(200, raw)])
    from app.main import app

    with TestClient(app) as client:
        created = client.post("/api/project", json={"name": "从证据包开始", "cms_package_id": "PKG-1"})
        assert created.status_code == 200
        pid = created.json()["id"]
        workspace = client.get(f"/api/project/{pid}/design-workspace")
    cms_view = workspace.json()["project"]["cms"]
    assert cms_view["package_id"] == "PKG-1"
    assert cms_view["package"]["signal_count"] == 1
    assert cms_view["package"]["requirement_desc"] == "低跟乐福鞋"
    assert "design-secret" not in workspace.text


def test_design_response_is_idempotent_on_the_same_version(env, monkeypatch):
    version_id = _confirmed_version(env)
    _link(env)
    scripted = _script(
        monkeypatch,
        [
            Response(200, {"id": "resp-9", "status": "stored", "data": {"response_id": "resp-9"}}),
            Response(200, {"id": "resp-9", "design_response_id": "resp-9"}),
        ],
    )
    from app.main import app

    payload = {
        "version_id": version_id,
        "response_status": "need_evidence",
        "design_note": "领口按方领处理，裙长仍待样衣确认。",
    }
    with TestClient(app) as client:
        first = client.post(f"/api/projects/{env}/cms-response", json=payload)
        second = client.post(f"/api/projects/{env}/cms-response", json={**payload, "response_status": "adopted"})
    assert first.status_code == 200
    assert second.status_code == 200
    assert first.json()["cms_status"] == "draft"
    assert first.json()["design_version_id"].startswith("DSG-")
    assert second.json()["design_version_id"] == first.json()["design_version_id"]
    assert second.json()["response_ids"] == ["resp-9"]
    assert scripted.calls[0]["json"]["design_version_id"] == scripted.calls[1]["json"]["design_version_id"]
    sent = scripted.calls[0]["json"]
    assert sent["source_agent"] == "design"
    assert sent["package_id"] == "PKG-1"
    assert sent["design_image"] == [f"http://public.test/api/design-versions/{version_id}/image"]
    assert sent["version"] == 2
    assert "方领" in sent["design_note"]
    assert sent["response_status"] == "need_evidence"
    assert scripted.calls[1]["json"]["response_status"] == "adopted"
    with models.session() as db:
        stored = json.loads(db.get(models.Project, env).cms_responses)
    assert len(stored) == 1
    assert stored[0]["response_ids"] == ["resp-9"]
    assert stored[0]["cms_status"] == "draft"


def test_unconfirmed_version_and_missing_package_are_rejected(env, monkeypatch):
    image = save_image(png())
    with transaction() as db:
        spec = create(db, env, "spec", {"spec": {"intent": "衬衫", "constraints": []}}, "confirmed")
        version = create(db, env, "version", {"spec_id": spec.id, "image": image}, "ready_for_review")
        version_id = version.id
    _script(monkeypatch, [])
    from app.main import app

    with TestClient(app) as client:
        missing_package = client.post(
            f"/api/projects/{env}/cms-response",
            json={"version_id": version_id, "response_status": "adopted", "design_note": "说明"},
        )
        assert missing_package.status_code == 409
        assert "还没有关联证据包" in missing_package.json()["error"]["message"]
        _link(env)
        unconfirmed = client.post(
            f"/api/projects/{env}/cms-response",
            json={"version_id": version_id, "response_status": "adopted", "design_note": "说明"},
        )
    assert unconfirmed.status_code == 409
    assert "请先确认" in unconfirmed.json()["error"]["message"]


def test_migration_adds_cms_columns_without_dropping_rows(tmp_path, monkeypatch):
    path = tmp_path / "legacy.sqlite3"
    with sqlite3.connect(path) as conn:
        conn.execute(
            """CREATE TABLE project (
                id INTEGER PRIMARY KEY,
                name VARCHAR(120) NOT NULL,
                category VARCHAR(40) NOT NULL,
                status VARCHAR(40) NOT NULL,
                created_at VARCHAR(32) NOT NULL
            )"""
        )
        conn.execute(
            "INSERT INTO project (name, category, status, created_at) "
            "VALUES ('旧项目', '连衣裙', 'draft', '2020-01-01')"
        )
        conn.execute("CREATE TABLE legacy_garment (id INTEGER PRIMARY KEY, note TEXT)")
        conn.execute("INSERT INTO legacy_garment (note) VALUES ('保留')")
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{path}")
    monkeypatch.setenv("ASSETS_DIR", str(tmp_path / "assets"))
    config.get_config.cache_clear()
    models.reset_engine_for_tests()
    models.init_db()
    migrate()
    with models.session() as db:
        row = db.get(models.Project, 1)
        assert row.name == "旧项目"
        assert row.cms_package_id is None
        assert row.cms_responses is None
    with sqlite3.connect(path) as conn:
        assert conn.execute("SELECT note FROM legacy_garment").fetchone()[0] == "保留"
        columns = {item[1] for item in conn.execute("PRAGMA table_info(project)")}
    assert {"cms_package_id", "cms_package_snapshot", "cms_responses"} <= columns
    models.reset_engine_for_tests()
    config.get_config.cache_clear()
