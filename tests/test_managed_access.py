"""Managed isolation and closed provider gates; synthetic local records only."""

import base64
import hashlib
import io
import json
import time
from contextlib import ExitStack

import pytest
from fastapi.testclient import TestClient
from PIL import Image, ImageDraw

from app import config, models
from app.agent import assets, models3d
from app.agent.managed_store import ManagedReceipt
from app.agent.providers import Provider
from app.agent.schemas import Constraint, SpecIn
from app.agent.store import AgentError, Record, create, head, transaction
from app.design_auth import cookie_name, password_hash, session_context, token_hash
from app.main import app
from tests.managed_fixtures import make_delivery

PASSWORD = "synthetic-local-password-123"
PASSWORD_HASH = password_hash(PASSWORD)


@pytest.fixture()
def managed(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "_load_env", lambda: None)
    values = {
        "DATABASE_URL": f"sqlite:///{tmp_path / 'managed-synthetic.sqlite3'}",
        "ASSETS_DIR": str(tmp_path / "synthetic-assets"),
        "AGENT_WORKER_ENABLED": "false",
        "DESIGN_INTEGRATION_MODE": "managed",
        "DESIGN_AUTH_REQUIRED": "false",  # Managed mode must override this legacy switch.
        "DESIGN_INSTANCE_ID": "design-test-isolated",
        "DESIGN_SCOPE_ID": "synthetic-scope",
        "DESIGN_ALLOWED_ORIGINS": "http://127.0.0.1:3092",
        "DESIGN_MANAGER_USERNAME": "lead",
        "DESIGN_MANAGER_PASSWORD_HASH": PASSWORD_HASH,
        "AGENT_ENABLED": "true",
        "AGENT_REFERENCE_IMAGES_ENABLED": "true",
        "AGENT_3D_ENABLED": "true",
        "MODEL_BASE_URL": "https://synthetic.invalid/v1",
        "MODEL_ID": "synthetic-text",
        "MODEL_API_KEY": "synthetic-not-a-credential",
        "AGENT_VISION_BASE_URL": "https://synthetic.invalid/v1",
        "AGENT_VISION_MODEL": "synthetic-vision",
        "AGENT_VISION_API_KEY": "synthetic-not-a-credential",
        "IMAGE_API_KEY": "synthetic-not-a-credential",
        "IMAGE_MODEL": "synthetic-image",
        "AGENT_REASONING_CALL_MAX_FEN": "1",
        "AGENT_IMAGE_CALL_MAX_FEN": "1",
        "AGENT_3D_MONTHLY_ALLOCATION_FEN": "180",
    }
    for key, value in values.items():
        monkeypatch.setenv(key, value)
    monkeypatch.delenv("DESIGN_PAID_PROVIDERS_ENABLED", raising=False)
    monkeypatch.delenv("DESIGN_SESSION_COOKIE_NAME", raising=False)
    config.get_config.cache_clear()
    models.reset_engine_for_tests()
    try:
        with ExitStack() as stack:
            manager = stack.enter_context(TestClient(app))
            clients = {"lead": manager, "alice": TestClient(app), "bob": TestClient(app), "anonymous": TestClient(app)}
            for name in ("alice", "bob", "anonymous"):
                stack.callback(clients[name].close)
            with models.session() as db:
                for name in ("alice", "bob"):
                    db.add(
                        models.DesignerUser(
                            username=name,
                            display_name=f"合成{name}",
                            role="designer",
                            active=True,
                            password_hash=PASSWORD_HASH,
                        )
                    )
                for name in ("lead", "alice", "bob"):
                    token = f"synthetic-session-{name}"
                    db.add(
                        models.DesignerSession(
                            token_hash=token_hash(token), username=name, expires_at=int(time.time()) + 3600
                        )
                    )
                    clients[name].cookies.set(cookie_name(), token)
                    clients[name].headers["X-Design-Context"] = session_context(token_hash(token))
                db.commit()
            image = io.BytesIO()
            picture = Image.new("RGB", (256, 128), "blue")
            ImageDraw.Draw(picture).text((12, 50), "SYNTHETIC TEST ONLY", fill="white")
            picture.save(image, "PNG")
            saved = assets.save_image(image.getvalue())
            projects = {}
            with transaction() as db:
                for index, name in enumerate(("alice", "bob", "orphan", "mismatch"), start=1):
                    project = models.Project(name=f"合成权限夹具-{name}")
                    db.add(project)
                    db.flush()
                    head(db, project.id)
                    spec = create(
                        db, project.id, "spec", {"spec": {"intent": "合成权限夹具", "constraints": []}}, "confirmed"
                    )
                    asset = create(db, project.id, "asset", saved, "ready")
                    version = create(
                        db,
                        project.id,
                        "version",
                        {"image": saved, "spec_id": spec.id, "generation_mode": "synthetic_fixture"},
                        "confirmed",
                    )
                    task = create(db, project.id, "task", {}, "succeeded")
                    style = create(db, project.id, "style_plan", {}, "confirmed")
                    flat = create(db, project.id, "technical_flat", {}, "confirmed")
                    model = create(db, project.id, "model3d", {"model_file": "a" * 32 + ".glb"}, "succeeded")
                    projects[name] = {
                        "pid": project.id,
                        "spec": spec.id,
                        "asset": asset.id,
                        "version": version.id,
                        "task": task.id,
                        "style": style.id,
                        "flat": flat.id,
                        "model": model.id,
                        "token": f"synthetic-public-image-token-{name}-0123456789",
                    }
                    assignee = name if name in {"alice", "bob"} else "alice"
                    db.add(models.DesignProjectAccess(project_id=project.id, username=assignee))
                    if name != "orphan":
                        # Correctly bound synthetic internal receipt; no real source pull is claimed.
                        wire = make_delivery(
                            source_instance_id="product-test-isolated",
                            target_instance_id="design-test-isolated",
                            scope_id="synthetic-scope",
                            version=f"v{index}",
                            content_suffix=b"\n ",
                        )
                        envelope = json.loads(wire)
                        package = json.loads(base64.b64decode(envelope["content_base64"]))
                        requirement = json.loads(base64.b64decode(package["requirement_base64"]))
                        prompt = json.loads(base64.b64decode(package["prompt_base64"]))
                        constraints = [
                            Constraint(id=f"c_product_{i}", kind="must_keep", text=value)
                            for i, value in enumerate(requirement["constraints"])
                        ]
                        constraints += [
                            Constraint(id=f"c_avoid_{i}", kind="forbidden", text=value)
                            for i, value in enumerate(prompt["avoid_items"])
                        ]
                        spec.payload = {
                            "spec": SpecIn(intent=prompt["positive_prompt"], constraints=constraints).model_dump(
                                exclude={"expected_spec_id"}
                            )
                        }
                        head(db, project.id).payload = {"spec_id": spec.id, "confirmed_version_id": version.id}
                        handoff_id = f"synthetic-{name}@v1"
                        db.add(
                            models.DesignHandoff(
                                id=handoff_id,
                                package_id=package["package_id"],
                                version=package["version"],
                                snapshot=package,
                                status="assigned",
                                assignee="bob" if name == "mismatch" else assignee,
                                project_id=project.id,
                                submitted_version_id=version.id,
                                image_token=projects[name]["token"],
                            )
                        )
                        db.add(
                            ManagedReceipt(
                                handoff_id=handoff_id,
                                source_instance_id="product-test-isolated",
                                target_instance_id="design-test-isolated",
                                scope_id="synthetic-scope",
                                request_id=f"synthetic-access-receive-{name}",
                                package_id=package["package_id"],
                                package_version=package["version"],
                                envelope_bytes=wire,
                                envelope_sha256=hashlib.sha256(wire).hexdigest(),
                                proof={
                                    "verification_method": "synthetic_internal_fixture_not_network",
                                    "package": package,
                                    "content_base64": envelope["content_base64"],
                                    "approval": package["approval"],
                                    "requirement": requirement,
                                    "prompt": prompt,
                                },
                                receipt={
                                    "design_receive_id": f"synthetic-access-receive-{name}",
                                    "content_digest": envelope["content_digest"],
                                    "status": "received",
                                },
                            )
                        )
            model_dir = config.get_config().assets_dir / "design-agent" / "models3d"
            model_dir.mkdir(parents=True)
            (model_dir / ("a" * 32 + ".glb")).write_bytes(
                b"glTF" + (2).to_bytes(4, "little") + (12).to_bytes(4, "little")
            )
            yield {"clients": clients, "projects": projects, "tmp_path": tmp_path}
    finally:
        models.reset_engine_for_tests()
        config.get_config.cache_clear()


def assert_error(response, status, code):
    assert response.status_code == status, response.text
    assert response.json()["error"]["code"] == code
    assert response.headers["Cache-Control"] == "private, no-store"


def test_managed_forces_auth_and_context_is_session_bound(managed):
    clients = managed["clients"]
    assert config.get_config().design_auth_required is True
    assert cookie_name() != "design_session"
    first = clients["alice"].get("/api/design-auth/me").json()
    assert first == clients["alice"].get("/api/design-auth/me").json()
    assert first["instance_id"] == "design-test-isolated"
    assert first["scope_id"] == "synthetic-scope"
    assert first["auth_context_id"] not in {"synthetic-session-alice", token_hash("synthetic-session-alice")}
    assert first["auth_context_id"] != clients["bob"].get("/api/design-auth/me").json()["auth_context_id"]
    fresh = clients["anonymous"].post("/api/design-auth/login", json={"username": "alice", "password": PASSWORD})
    assert fresh.status_code == 200
    assert "HttpOnly" in fresh.headers["set-cookie"] and "SameSite=strict" in fresh.headers["set-cookie"]
    assert clients["anonymous"].get("/api/design-auth/me").json()["auth_context_id"] != first["auth_context_id"]


@pytest.mark.parametrize(
    "path",
    [
        "/api/design-auth/me",
        "/api/design-projects",
        "/api/design-agent/capabilities",
        "/api/design-handoffs",
        "/api/design-public/anything.png",
    ],
)
def test_anonymous_cannot_reach_managed_data(managed, path):
    assert_error(managed["clients"]["anonymous"].get(path), 401, "LOGIN_REQUIRED")


@pytest.mark.parametrize(
    "template",
    [
        "/api/project/{pid}/design-workspace",
        "/api/project/{pid}/design-versions",
        "/api/project/{pid}/design-events",
        "/api/design-assets/{asset}/image",
        "/api/design-tasks/{task}",
        "/api/design-versions/{version}/image",
        "/api/design-versions/{version}/delivery",
        "/api/design-versions/{version}/sample-pack",
        "/api/design-versions/{version}/sample-pack/front.svg",
        "/api/design-models3d/{model}/file",
        "/api/technical-flats/{flat}/front.svg",
        "/api/design-public/{token}.png",
    ],
)
def test_other_designers_cannot_read_objects_downloads_or_sse(managed, template):
    path = template.format(**managed["projects"]["alice"])
    assert_error(managed["clients"]["bob"].get(path), 404, "NOT_FOUND")


@pytest.mark.parametrize(
    "template",
    [
        "/api/design-specs/{spec}/confirm",
        "/api/style-plans/{style}/confirm",
        "/api/design-tasks/{task}/cancel",
        "/api/design-versions/{version}/confirm",
        "/api/technical-flats/{flat}/confirm",
        "/api/project/{pid}/design-assets",
        "/api/project/{pid}/design-specs",
        "/api/design-versions/{version}/model3d",
    ],
)
def test_other_designers_cannot_write_any_object_family(managed, template):
    path = template.format(**managed["projects"]["alice"])
    assert_error(managed["clients"]["bob"].post(path, json={}), 404, "NOT_FOUND")


def test_manager_only_reads_handoff_projects_and_access_table_alone_is_insufficient(managed):
    c, p = managed["clients"], managed["projects"]
    assert c["lead"].get(f"/api/project/{p['alice']['pid']}/design-workspace").status_code == 200
    assert_error(c["lead"].post(f"/api/design-specs/{p['alice']['spec']}/confirm"), 403, "ROLE_FORBIDDEN")
    for name in ("lead", "alice"):
        assert_error(c[name].get(f"/api/project/{p['orphan']['pid']}/design-workspace"), 404, "NOT_FOUND")
    for name in ("alice", "bob"):
        assert_error(c[name].get(f"/api/project/{p['mismatch']['pid']}/design-workspace"), 404, "NOT_FOUND")


def test_public_image_requires_authorized_person_and_bound_version(managed):
    c, p = managed["clients"], managed["projects"]
    with models.session() as db:
        db.get(models.DesignHandoff, "synthetic-alice@v1").status = "approved"
        db.commit()
    path = f"/api/design-public/{p['alice']['token']}.png"
    for name in ("alice", "lead"):
        response = c[name].get(path)
        assert response.status_code == 200, response.text
        assert response.headers["Cache-Control"] == "private, no-store"
    assert_error(c["anonymous"].get(path), 401, "LOGIN_REQUIRED")
    assert_error(c["bob"].get(path), 404, "NOT_FOUND")
    with models.session() as db:
        db.get(models.DesignHandoff, "synthetic-alice@v1").submitted_version_id = p["bob"]["version"]
        db.commit()
    assert_error(c["alice"].get(path), 404, "NOT_FOUND")


def test_own_images_and_downloads_are_never_publicly_cached(managed):
    c, p = managed["clients"]["alice"], managed["projects"]["alice"]
    for template in (
        "/api/design-assets/{asset}/image",
        "/api/design-versions/{version}/image",
        "/api/design-versions/{version}/delivery",
        "/api/design-models3d/{model}/file",
    ):
        response = c.get(template.format(**p))
        assert response.status_code == 200, response.text
        assert response.headers["Cache-Control"] == "private, no-store"


def test_registration_project_creation_and_cms_are_closed(managed, monkeypatch):
    from app.agent import cms

    monkeypatch.setattr(cms, "fetch_packages", lambda *_: pytest.fail("CMS network access"))
    monkeypatch.setattr(cms, "fetch_package", lambda *_: pytest.fail("CMS network access"))
    monkeypatch.setattr(cms, "submit_response", lambda *_: pytest.fail("CMS network access"))
    c = managed["clients"]
    assert_error(c["anonymous"].post("/api/design-auth/register", json={}), 403, "ROLE_FORBIDDEN")
    for name in ("lead", "alice"):
        assert_error(c[name].post("/api/project", json={"name": "must not be created"}), 403, "ROLE_FORBIDDEN")
        for path in ("/api/cms/packages", "/api/cms/packages/known"):
            assert_error(c[name].get(path), 410, "LEGACY_ROUTE_DISABLED")
        assert_error(c[name].post("/api/projects/1/cms-response", json={}), 410, "LEGACY_ROUTE_DISABLED")


def test_context_required_for_engine_and_personnel_writes(managed):
    c, p = managed["clients"], managed["projects"]["alice"]
    for path, client in (
        (f"/api/project/{p['pid']}/design-specs", c["alice"]),
        ("/api/design-auth/staff", c["lead"]),
        ("/api/design-auth/password", c["alice"]),
    ):
        expected = client.headers.pop("X-Design-Context")
        assert_error(client.post(path, json={}), 409, "AUTH_CONTEXT_CHANGED")
        client.headers["X-Design-Context"] = "previous-session-context"
        assert_error(client.post(path, json={}), 409, "AUTH_CONTEXT_CHANGED")
        client.headers["X-Design-Context"] = expected
    with models.session() as db:
        approved_spec = db.get(Record, p["spec"]).payload["spec"]
    saved = c["alice"].post(
        f"/api/project/{p['pid']}/design-specs",
        json={**approved_spec, "intent": "合成权限测试新要求", "expected_spec_id": p["spec"]},
        headers={"X-Design-Action-Id": "access-save-source-spec", "X-Design-Revision": "1"},
    )
    assert saved.status_code == 201, saved.text
    assert_error(
        c["alice"].post(
            "/api/design-auth/staff", json={"username": "another", "display_name": "合成", "password": PASSWORD}
        ),
        403,
        "ROLE_FORBIDDEN",
    )


def test_origin_is_exact_for_login_preflight_and_authenticated_data(managed):
    c = managed["clients"]["alice"]
    for origin in (
        "null",
        "https://evil.invalid",
        "http://127.0.0.1:3092.evil.invalid",
        "http://127.0.0.1:3093",
        "http://127.0.0.1:3092/",
    ):
        assert_error(c.get("/api/design-auth/me", headers={"Origin": origin}), 403, "ORIGIN_FORBIDDEN")
        assert_error(c.post("/api/design-auth/login", json={}, headers={"Origin": origin}), 403, "ORIGIN_FORBIDDEN")
    origin = "http://127.0.0.1:3092"
    good = c.get("/api/design-auth/me", headers={"Origin": origin})
    assert good.status_code == 200
    assert good.headers["Access-Control-Allow-Origin"] == origin
    assert good.headers["Access-Control-Allow-Credentials"] == "true"
    preflight = c.options(
        "/api/design-auth/staff",
        headers={
            "Origin": origin,
            "Access-Control-Request-Method": "POST",
            "Access-Control-Request-Headers": "Content-Type,X-Design-Context",
        },
    )
    assert preflight.status_code == 204
    assert preflight.headers["Access-Control-Allow-Origin"] == origin
    assert preflight.headers["Cache-Control"] == "private, no-store"


def test_provider_master_gate_blocks_all_network_boundaries(managed, monkeypatch):
    def network_forbidden(*args, **kwargs):
        pytest.fail("Disabled managed provider attempted network access")

    monkeypatch.setattr("httpx.Client", network_forbidden)
    provider = Provider()
    assert provider.capabilities()["understand"] is False
    assert provider.capabilities()["design"] is False
    assert models3d.capabilities()["enabled"] is False
    calls = [
        lambda: provider.require("understand"),
        lambda: provider.require("design"),
        lambda: Provider._post("https://synthetic.invalid", "synthetic", {}),
        lambda: Provider._download_image("https://synthetic.invalid/image.png"),
        lambda: models3d._request("POST", "https://synthetic.invalid"),
        lambda: models3d._download_model("https://synthetic.invalid/model.zip"),
        lambda: models3d.submit(
            managed["projects"]["alice"]["version"],
            models3d.Generate3DIn(authorized=True, idempotency_key="synthetic-test"),
        ),
    ]
    for call in calls:
        with pytest.raises(AgentError) as caught:
            call()
        assert caught.value.code == "CAPABILITY_UNAVAILABLE"
    with transaction() as db:
        task = create(
            db,
            managed["projects"]["alice"]["pid"],
            "model3d",
            {"provider_task_id": "synthetic", "next_poll_at": 0},
            "running",
        )
        task_id = task.id
    models3d.tick()
    with transaction() as db:
        assert db.get(Record, task_id).status == "running"


def test_invalid_input_and_missing_resource_errors_are_uniform(managed):
    c = managed["clients"]
    assert_error(c["anonymous"].post("/api/design-auth/login", json={}), 422, "INVALID_INPUT")
    assert_error(c["lead"].get("/api/no-such-resource"), 404, "NOT_FOUND")
    assert_error(c["alice"].get("/api/design-assets/no-such-asset/image"), 404, "NOT_FOUND")


@pytest.mark.parametrize(
    "invalid_origin",
    ["*", "https://*.example.invalid", "https://example.invalid/path", "https://name:pass@example.invalid", "null"],
)
def test_managed_configuration_rejects_non_exact_origins(managed, monkeypatch, invalid_origin):
    monkeypatch.setenv("DESIGN_ALLOWED_ORIGINS", invalid_origin)
    with pytest.raises(ValueError, match="exact HTTP origins"):
        config.Config()


def test_managed_configuration_requires_explicit_instance_and_scope(managed, monkeypatch):
    monkeypatch.delenv("DESIGN_SCOPE_ID")
    with pytest.raises(ValueError, match="explicit DESIGN_INSTANCE_ID and DESIGN_SCOPE_ID"):
        config.Config()
