"""Infrastructure tests using explicit synthetic fixtures; NOT visual-quality acceptance."""

import io
import json
import subprocess
import sys
import zipfile
from copy import deepcopy

import pytest
from fastapi.testclient import TestClient
from PIL import Image
from sqlalchemy import inspect

from app import config, models
from app.agent import service, style, technical_flat
from app.agent.assets import data_url, save_image
from app.agent.migrate import migrate
from app.agent.providers import Provider
from app.agent.runner import event, recover, run_task
from app.agent.schemas import RevisionIn, SpecIn, StylePlanEdit, TaskIn
from app.agent.store import AgentError, change, create, require, transaction


def png(color="red"):
    out = io.BytesIO()
    Image.new("RGB", (128, 192), color).save(out, format="PNG")
    return out.getvalue()


@pytest.mark.parametrize("format", ["JPEG", "PNG", "WEBP"])
def test_image_service_formats_are_normalized_to_static_png(env, format):
    from app.agent.assets import file_path

    raw = io.BytesIO()
    Image.new("RGB", (256, 384), "red").save(raw, format=format)
    saved = save_image(raw.getvalue())
    with Image.open(file_path(saved)) as image:
        assert image.format == "PNG"
        assert image.size == (256, 384)
        assert image.getpixel((128, 192))[0] > 240


def test_animated_reference_is_still_rejected(env):
    raw = io.BytesIO()
    Image.new("RGB", (128, 192), "red").save(
        raw,
        format="PNG",
        save_all=True,
        append_images=[Image.new("RGB", (128, 192), "blue")],
        duration=100,
        loop=0,
    )
    with pytest.raises(AgentError) as error:
        save_image(raw.getvalue())
    assert error.value.code == "INVALID_IMAGE"


def test_image_receipt_tolerates_non_token_usage():
    assert Provider.receipt({"usage": [1]})["usage"] == {}


def test_enabling_images_keeps_existing_understanding_conversation_valid(env):
    class ConfigProvider(FakeProvider):
        settings = {
            "understand": True,
            "design": False,
            "image_call_max_fen": 0,
            "monthly_allocation_fen": 2000,
            "vision_model": "same-model",
        }

        def capabilities(self):
            return dict(self.settings)

    provider = ConfigProvider()
    tid = task(env, spec(env), provider, mode="understand")
    provider.settings = {**provider.settings, "design": True, "image_call_max_fen": 50, "monthly_allocation_fen": 5000}
    with transaction() as db:
        event(db, require(db, tid, "task"), "inspect_assets", provider)
    provider.settings["vision_model"] = "different-model"
    with transaction() as db, pytest.raises(AgentError) as error:
        event(db, require(db, tid, "task"), "inspect_assets", provider)
    assert error.value.code == "PROVIDER_CHANGED"


@pytest.fixture()
def env(tmp_path, monkeypatch):
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path / 'agent.sqlite3'}")
    monkeypatch.setenv("ASSETS_DIR", str(tmp_path / "assets"))
    monkeypatch.setenv("MODEL_PROVIDER", "mock")
    monkeypatch.setenv("AGENT_WORKER_ENABLED", "false")
    monkeypatch.setenv("AGENT_ENABLED", "false")
    config.get_config.cache_clear()
    models.reset_engine_for_tests()
    models.init_db()
    migrate()
    with models.session() as db:
        project = models.Project(name="合成图片工程测试（非效果验收）")
        db.add(project)
        db.commit()
        pid = project.id
    yield pid
    models.reset_engine_for_tests()
    config.get_config.cache_clear()


def test_technical_flat_passes_saved_image_to_vision_provider(env):
    image = save_image(png())
    with transaction() as db:
        spec = create(db, env, "spec", {"spec": {"intent": "连衣裙", "constraints": []}})
        version = create(db, env, "version", {"spec_id": spec.id, "image": image}, "confirmed")
        version_id = version.id

    class FlatProvider:
        def require(self, mode):
            assert mode == "understand"

        def structured(self, prompt, context, schema, images):
            assert prompt == "flat-v1"
            assert images[0][1] == image
            assert data_url(images[0][1]).startswith("data:image/png;base64,")
            return {
                "category": "dress", "neckline": "round", "sleeves": "short",
                "closure": "none", "waist": "none", "skirt": "a_line",
                "visible_details": [], "uncertain_details": [],
            }

    result = technical_flat.generate(version_id, provider=FlatProvider())
    assert result["status"] == "draft"


class FakeProvider:
    reasoning_fen, image_fen, monthly_fen = 1, 10, 20000

    def __init__(self, actions=None, deviation=False):
        self.actions = iter(actions) if actions else None
        self.calls = []
        self.deviation = deviation

    def require(self, mode):
        pass

    def capabilities(self):
        return {"test_only": True}

    def plan_directions(self, context):
        self.calls.append(("plan_directions", deepcopy(context)))
        return {
            "directions": [
                {
                    "name": f"合成方向 {i}",
                    "theme": f"合成测试主题 {i}",
                    "rationale": f"合成测试适配理由 {i}",
                    "explore": f"领口和裙摆变化 {i}",
                    "color_story": f"颜色 {i}",
                    "structure": f"结构 {i}",
                    "material_story": f"材料外观 {i}",
                }
                for i in range(1, context["count"] + 1)
            ]
        }

    def plan(self, context):
        self.calls.append(("plan", context))
        if self.actions:
            return next(self.actions)
        if context["spec"]["references"] and not context["observations"]:
            tool = "inspect_assets"
        elif context["mode"] == "understand":
            return {"action": "propose_spec", "summary": "测试要求整理", "proposed_spec": context["spec"]}
        elif not context["current_version_id"]:
            tool = "edit_design" if context["spec"]["base_version_id"] else "generate_design"
        elif not context["review"]:
            tool = "compare_design"
        else:
            tool = "finish"
        return {"action": tool, "summary": "测试动作，不代表模型能力"}

    def inspect(self, spec, images):
        self.calls.append(("inspect", images))
        return {
            "observations": [
                {
                    "asset_id": r["asset_id"],
                    "observable_features": ["测试红色矩形"],
                    "inferences": [],
                    "unknowns": ["物理性能未知"],
                }
                for r in spec["references"]
            ],
            "conflicts": [],
        }

    def render(self, spec, images, feedback):
        self.calls.append(("render", deepcopy(spec), images))
        assert all(data_url(p).startswith("data:image/png;base64,") for _, p in images)
        return png("blue")

    def compare(self, spec, images):
        self.calls.append(("compare", images, deepcopy(spec)))
        return {
            "checks": [
                {
                    "constraint_id": c["id"],
                    "status": "deviation" if self.deviation else "pass",
                    "candidate_region": c["region"],
                    "evidence": "合成测试检查，不代表真实视觉判断",
                    "reference_ids": [],
                }
                for c in spec["constraints"]
            ],
            "goal": {"status": "pass", "evidence": "合成测试意图符合"},
            "preservation": {"status": "pass", "evidence": "合成测试底图比较"},
            "summary": "测试结果",
        }


def spec(pid, refs=0, physical=False):
    with transaction() as db:
        references = []
        for i in range(refs):
            asset = create(db, pid, "asset", {**save_image(png()), "name": f"测试-{i}"}, "ready")
            references.append({"asset_id": asset.id, "role": ["structure", "fabric", "detail"][i], "region": "整体"})
        row = service.save_spec(
            db,
            pid,
            SpecIn(
                intent="方领高腰连衣裙",
                references=references,
                constraints=[
                    {
                        "id": "c_collar",
                        "kind": "must_keep",
                        "text": "保留方领",
                        "region": "领口",
                        "verification": "physical" if physical else "visual",
                    },
                    {"id": "c_ruffle", "kind": "forbidden", "text": "禁止荷叶边", "region": "整体"},
                ],
            ),
        )
        return row.id


def task(pid, sid, provider, mode="design", key="unique-key", **kwargs):
    if mode == "design":
        service.confirm_spec(sid)
    result = service.submit(
        pid, TaskIn(spec_id=sid, mode=mode, idempotency_key=key, authorized=True, **kwargs), provider
    )
    with transaction() as db:
        require(db, result["id"]).status = "running"
    return result["id"]


def read_task(id):
    with transaction() as db:
        return require(db, id).status, deepcopy(require(db, id).payload)


def test_confirmed_style_plan_controls_multi_design_and_revision_lineage(env):
    sid = spec(env)
    service.confirm_spec(sid)
    provider = FakeProvider()
    planning = task(env, sid, provider, mode="style", key="style-plan", design_count=3, max_cost_fen=1)
    run_task(planning, provider)
    with transaction() as db:
        plan = require(db, require(db, planning).payload["style_plan_id"], "style_plan", env)
        assert plan.status == "draft"
        assert len(plan.payload["directions"]) == 3
        directions = deepcopy(plan.payload["directions"])
        plan_id = plan.id
    directions[-1]["selected"] = False
    revised = style.revise(plan_id, StylePlanEdit(expected_plan_id=plan_id, directions=directions))
    style.confirm(revised["id"])
    selected_ids = [item["id"] for item in directions if item["selected"]]
    generation = task(
        env, sid, provider, key="style-generation", design_count=2, max_cost_fen=22,
        style_plan_id=revised["id"], style_direction_ids=selected_ids,
    )
    run_task(generation, provider)
    status, payload = read_task(generation)
    assert status == "awaiting_review"
    assert len(payload["direction_version_ids"]) == 2
    with transaction() as db:
        versions = [require(db, vid, "version", env) for vid in payload["direction_version_ids"]]
        assert [row.payload["style_direction_id"] for row in versions] == selected_ids
        assert [row.payload["style_direction"]["explore"] for row in versions] == [
            item["explore"] for item in directions[:2]
        ]
    revised_spec = service.revise(
        versions[1].id,
        RevisionIn(text="只调整裙摆长度", edit_region="裙摆", expected_spec_id=sid),
    )
    edit = task(env, revised_spec["id"], provider, key="edit-one", max_cost_fen=30)
    run_task(edit, provider)
    _, edit_payload = read_task(edit)
    with transaction() as db:
        child = require(db, edit_payload["current_version_id"], "version", env)
        assert child.payload["parent_version_id"] == versions[1].id
        assert child.payload["style_direction_id"] == selected_ids[1]
        assert child.payload["style_plan_id"] == revised["id"]


def test_style_plan_rejected_after_requirements_change(env):
    sid = spec(env)
    service.confirm_spec(sid)
    provider = FakeProvider()
    planning = task(env, sid, provider, mode="style", key="style-before-change", design_count=3, max_cost_fen=1)
    run_task(planning, provider)
    with transaction() as db:
        old_plan_id = require(db, planning).payload["style_plan_id"]
        old_spec = require(db, sid).payload["spec"]
    models.reset_engine_for_tests()
    models.init_db()
    migrate()
    with transaction() as db:
        assert require(db, old_plan_id, "style_plan", env).payload["spec_id"] == sid
        assert len(require(db, old_plan_id, "style_plan", env).payload["directions"]) == 3
    with transaction() as db:
        service.save_spec(db, env, SpecIn(**{**old_spec, "expected_spec_id": sid, "intent": "改为圆领外套"}))
    with pytest.raises(AgentError, match="最新"):
        style.confirm(old_plan_id)


def test_shoe_plan_rejects_apparel_structure(env):
    class WrongCategoryProvider(FakeProvider):
        def plan_directions(self, context):
            result = super().plan_directions(context)
            result["directions"][0]["structure"] = "领口加宽"
            return result

    with transaction() as db:
        sid = service.save_spec(db, env, SpecIn(intent="通勤平底凉鞋")).id
    service.confirm_spec(sid)
    provider = WrongCategoryProvider()
    planning = task(env, sid, provider, mode="style", key="wrong-category", design_count=3, max_cost_fen=1)
    run_task(planning, provider)
    status, payload = read_task(planning)
    assert status == "failed"
    assert payload["error"]["code"] == "CATEGORY_MISMATCH"
    assert payload["style_plan_id"] is None


def test_migration_is_additive_idempotent_and_backup(env, tmp_path):
    migrate()
    assert "design_agent_record" in inspect(models.engine()).get_table_names()
    with models.session() as db:
        assert db.get(models.Project, env).name.startswith("合成图片")
    assert (tmp_path / "agent.before-design-agent.sqlite3").is_file()


@pytest.mark.parametrize(
    "raw", [b"<svg><script>alert(1)</script></svg>", b"fake jpg", b"", b"x" * (10 * 1024 * 1024 + 1)]
)
def test_reject_bad_uploads(env, raw):
    with pytest.raises(AgentError):
        save_image(raw)


def test_uploaded_content_is_reencoded_and_no_user_path(env):
    saved = save_image(png())
    assert len(saved["file"]) == 36 and saved["file"].endswith(".png")
    assert saved["width"] == 128


def test_cross_project_reference_and_stale_spec(env):
    sid = spec(env, refs=1)
    with transaction() as db:
        other = models.Project(name="另一个项目")
        db.add(other)
        db.flush()
        data = require(db, sid).payload["spec"]
        with pytest.raises(AgentError, match="不属于"):
            service.save_spec(db, other.id, SpecIn(**data))
        with pytest.raises(AgentError, match="新版本"):
            service.save_spec(db, env, SpecIn(intent="覆盖旧要求"))


def test_human_confirmation_and_capability_are_required(env):
    sid = spec(env)
    data = TaskIn(spec_id=sid, mode="design", authorized=True, idempotency_key="safe-key")
    with pytest.raises(AgentError, match="先确认"):
        service.submit(env, data, FakeProvider())
    service.confirm_spec(sid)
    with pytest.raises(AgentError, match="尚未就绪"):
        service.submit(env, data, Provider())


def test_idempotency_does_not_create_second_task_and_rejects_changed_body(env):
    sid = spec(env)
    service.confirm_spec(sid)
    data = TaskIn(spec_id=sid, mode="design", authorized=True, idempotency_key="same-key")
    one = service.submit(env, data, FakeProvider())
    two = service.submit(env, data, FakeProvider())
    assert one["id"] == two["id"]
    with pytest.raises(AgentError, match="不同任务"):
        service.submit(env, data.model_copy(update={"max_cost_fen": 123}), FakeProvider())


def test_three_refs_actually_reach_generation_and_comparison(env):
    sid = spec(env, refs=3)
    provider = FakeProvider()
    id = task(env, sid, provider)
    run_task(id, provider)
    status, data = read_task(id)
    assert status == "awaiting_review", data
    assert data["image_calls"] == 1 and data["reasoning_calls"] == 6
    render = next(c for c in provider.calls if c[0] == "render")
    assert len(render[2]) == 3
    assert "fabric" in render[2][1][0]
    compared = next(c for c in provider.calls if c[0] == "compare")
    assert len(compared[1]) == 4
    version = service.confirm_version(data["current_version_id"])
    assert version["status"] == "confirmed"


def test_understanding_proposes_spec_without_generation(env):
    sid = spec(env, refs=1)
    provider = FakeProvider()
    id = task(env, sid, provider, mode="understand")
    run_task(id, provider)
    status, data = read_task(id)
    assert status == "awaiting_review"
    assert data["image_calls"] == 0 and data["proposed_spec_id"] != sid
    with transaction() as db:
        assert require(db, data["proposed_spec_id"]).status == "draft"


def test_model_cannot_drop_constraints(env):
    sid = spec(env)
    provider = FakeProvider(
        [{"action": "propose_spec", "summary": "错误地删约束", "proposed_spec": {"intent": "随意新设计"}}]
    )
    id = task(env, sid, provider, mode="understand")
    run_task(id, provider)
    assert read_task(id)[1]["error"]["code"] == "CONSTRAINT_LOSS"


def test_no_fake_finish_without_actual_image_review(env):
    sid = spec(env)
    provider = FakeProvider([{"action": "finish", "summary": "已经全部完成"}])
    id = task(env, sid, provider)
    run_task(id, provider)
    assert read_task(id)[1]["error"]["code"] == "REVIEW_REQUIRED"


def test_two_round_revisions_preserve_constraints_and_parent_lineage(env):
    sid = spec(env, refs=2)
    provider = FakeProvider()
    first = task(env, sid, provider)
    run_task(first, provider)
    parent = read_task(first)[1]["current_version_id"]
    for i in range(2):
        revised = service.revise(
            parent, RevisionIn(text=f"第 {i + 1} 轮只改袖子", edit_region="袖子", expected_spec_id=sid)
        )
        assert {c["id"] for c in revised["spec"]["constraints"]} >= {"c_collar", "c_ruffle"}
        sid = revised["id"]
        provider = FakeProvider()
        id = task(env, sid, provider, key=f"revision-{i}")
        run_task(id, provider)
        assert read_task(id)[0] == "awaiting_review"
        vid = read_task(id)[1]["current_version_id"]
        with transaction() as db:
            assert require(db, vid).payload["parent_version_id"] == parent
        assert any("修改底图" in label for label, _ in next(c for c in provider.calls if c[0] == "render")[2])
        parent = vid


def test_second_color_revision_replaces_first_color_instruction(env):
    sid = spec(env)
    provider = FakeProvider()
    first_task = task(env, sid, provider)
    run_task(first_task, provider)
    original_id = read_task(first_task)[1]["current_version_id"]

    white = service.revise(
        original_id, RevisionIn(text="换成白色", edit_region="颜色", expected_spec_id=sid)
    )
    white_provider = FakeProvider()
    white_task = task(env, white["id"], white_provider, key="white-revision")
    run_task(white_task, white_provider)
    white_id = read_task(white_task)[1]["current_version_id"]

    red = service.revise(
        white_id, RevisionIn(text="换成红色", edit_region="颜色", expected_spec_id=white["id"])
    )
    assert red["spec"]["base_version_id"] == white_id
    assert [c["text"] for c in red["spec"]["constraints"] if c["kind"] == "may_change"] == ["换成红色"]
    assert "换成白色" not in json.dumps(red["spec"], ensure_ascii=False)
    assert len(red["spec"]["assumptions"]) == len(set(red["spec"]["assumptions"]))


def test_revision_direction_uses_latest_color_instead_of_old_palette():
    from app.agent.runner import revision_direction

    old = {"name": "复古水洗做旧款", "color_story": "衣身浅炭灰色", "structure": "宽松落肩"}
    spec_data = {
        "base_version_id": "white-version",
        "edit_region": "颜色",
        "constraints": [{"kind": "may_change", "region": "颜色", "text": "换成红色"}],
    }
    updated = revision_direction(old, spec_data)
    assert updated["color_story"] == "换成红色"
    assert "换成红色" in updated["revision_override"]
    assert updated["structure"] == old["structure"]
    assert old["color_story"] == "衣身浅炭灰色"


@pytest.mark.parametrize("physical,deviation", [(True, False), (False, True)])
def test_hard_unknown_or_deviation_cannot_be_confirmed(env, physical, deviation):
    sid = spec(env, physical=physical)
    provider = FakeProvider(deviation=deviation)
    id = task(env, sid, provider)
    run_task(id, provider)
    with pytest.raises(AgentError, match="硬性要求"):
        service.confirm_version(read_task(id)[1]["current_version_id"])


def test_count_and_money_budgets_stop_before_next_call(env):
    sid = spec(env)
    provider = FakeProvider()
    id = task(env, sid, provider, max_cost_fen=1)
    run_task(id, provider)
    status, data = read_task(id)
    assert status == "budget_exhausted" and data["reasoning_calls"] == 1 and data["image_calls"] == 0


def test_new_design_task_has_no_app_money_ceiling(env):
    sid = spec(env)
    provider = FakeProvider()
    tid = task(env, sid, provider)
    run_task(tid, provider)
    status, data = read_task(tid)
    assert status == "awaiting_review"
    assert data["max_cost_fen"] is None
    assert data["reserved_cost_fen"] > provider.reasoning_fen
    assert data["reserved_cost_fen"] > 0


def test_cancel_during_image_call_keeps_returned_image_but_stops_checks(env):
    sid = spec(env)
    provider = FakeProvider()
    id = task(env, sid, provider)
    original = provider.render

    def render(*args):
        service.task_control(id, "cancel")
        return original(*args)

    provider.render = render
    run_task(id, provider)
    status, data = read_task(id)
    assert status == "cancelled" and data["current_version_id"]
    assert not any(c[0] == "compare" for c in provider.calls)


def test_restart_unknown_call_is_not_replayed_in_fresh_process(env):
    sid = spec(env)
    provider = FakeProvider()
    id = task(env, sid, provider)
    with transaction() as db:
        event(db, require(db, id), "plan", provider)
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            "from app.agent.store import transaction; "
            "from app.agent.runner import recover; "
            "from app.models import engine; "
            "\nwith transaction() as db: recover(db)",
        ],
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr
    status, data = read_task(id)
    assert status == "interrupted" and data["steps"][0]["status"] == "unknown"
    with pytest.raises(AgentError, match="禁止自动重放"):
        service.task_control(id, "resume")
    assert data["reasoning_calls"] == 1


def test_safe_restart_preserves_budget_and_pending_action(env):
    sid = spec(env)
    provider = FakeProvider()
    id = task(env, sid, provider)
    with transaction() as db:
        row = require(db, id)
        change(
            row,
            reasoning_calls=2,
            reserved_cost_fen=2,
            pending_action={"action": "generate_design", "summary": "已决策"},
        )
        recover(db)
    service.task_control(id, "resume")
    with transaction() as db:
        require(db, id).status = "running"
    run_task(id, provider)
    assert read_task(id)[0] == "awaiting_review"
    assert provider.calls[0][0] == "render"  # Saved plan reused, no repeated model decision.
    assert read_task(id)[1]["reserved_cost_fen"] >= 12


def test_stale_version_cannot_be_confirmed(env):
    sid = spec(env)
    provider = FakeProvider()
    id = task(env, sid, provider)
    run_task(id, provider)
    vid = read_task(id)[1]["current_version_id"]
    with transaction() as db:
        service.save_spec(db, env, SpecIn(intent="改为圆领", expected_spec_id=sid))
    with pytest.raises(AgentError, match="已变更"):
        service.confirm_version(vid)


def test_api_upload_validation_error_download_and_persistence(env):
    from app.main import app

    with TestClient(app) as client:
        bad = client.post(f"/api/project/{env}/design-assets", content=b"not a photo")
        assert bad.status_code == 422 and bad.json()["error"]["code"] == "INVALID_IMAGE"
        good = client.post(f"/api/project/{env}/design-assets?name=../../red.png", content=png())
        assert good.status_code == 201 and good.json()["name"] == "red.png"
        invalid = client.post(f"/api/project/{env}/design-specs", json={})
        assert invalid.status_code == 422 and "error" in invalid.json()
        sid = spec(env)
        provider = FakeProvider()
        id = task(env, sid, provider)
        run_task(id, provider)
        vid = read_task(id)[1]["current_version_id"]
        response = client.get(f"/api/design-versions/{vid}/delivery")
        assert response.status_code == 200
        z = zipfile.ZipFile(io.BytesIO(response.content))
        assert set(z.namelist()) == {"design.png", "requirements.json", "design-notes.md"}
        assert json.loads(z.read("requirements.json"))["version"]["id"] == vid
        assert client.get(f"/api/project/{env}/design-workspace").json()["versions"][0]["id"] == vid


def test_sampling_draft_belongs_to_confirmed_design_and_downloads_with_image(env):
    from app.main import app

    sid = spec(env)
    tid = task(env, sid, FakeProvider())
    run_task(tid, FakeProvider())
    vid = read_task(tid)[1]["current_version_id"]
    path = f"/api/design-versions/{vid}/sampling-sheet"
    with TestClient(app) as client:
        assert client.post(path, json={"material": "棉布"}).json()["error"]["code"] == "CONFIRM_REQUIRED"
        service.confirm_version(vid)
        first = client.post(path, json={"material": "棉布，克重待核对", "measurements": "M 码胸围待测量"})
        assert first.status_code == 201
        first_id = first.json()["id"]
        assert first.json()["version_id"] == vid
        assert first.json()["status"] == "draft"
        assert client.post(path, json={"material": "另一块布"}).json()["error"]["code"] == "STALE_SOURCE"
        second = client.post(path, json={"expected_sheet_id": first_id, "material": "棉布，已确认克重"})
        assert second.status_code == 201
        assert second.json()["previous_sheet_id"] == first_id
        workspace = client.get(f"/api/project/{env}/design-workspace").json()
        assert len(workspace["sampling_sheets"]) == 2
        archive = zipfile.ZipFile(io.BytesIO(client.get(f"/api/design-versions/{vid}/delivery").content))
        assert {"sampling-draft.json", "sampling-draft.md"} <= set(archive.namelist())
        assert json.loads(archive.read("sampling-draft.json"))["sheet"]["id"] == second.json()["id"]
        assert "待核对" in archive.read("sampling-draft.md").decode()
        with transaction() as db:
            service.save_spec(db, env, SpecIn(intent="新设计方向", expected_spec_id=sid))
        historical = client.post(path, json={"expected_sheet_id": second.json()["id"], "material": "其他"})
        assert historical.json()["error"]["code"] == "CONFIRM_REQUIRED"


def test_agent_organizes_sampling_basis_once_without_inventing_physical_details(env):
    from app.main import app

    with transaction() as db:
        sid = service.save_spec(
            db,
            env,
            SpecIn(
                intent="宽松落肩小狗图案T恤",
                constraints=[{"id": "c_dog", "kind": "must_keep", "text": "胸前小狗图案"}],
            ),
        ).id
    tid = task(env, sid, FakeProvider())
    run_task(tid, FakeProvider())
    vid = read_task(tid)[1]["current_version_id"]
    with transaction() as db:
        version = require(db, vid, "version")
        review = deepcopy(version.payload["review"])
        review["checks"][0]["evidence"] = "效果图胸前正中可见小狗图案"
        change(version, review=review)
    service.confirm_version(vid)
    with TestClient(app) as client:
        path = f"/api/design-versions/{vid}/sampling-sheet/auto"
        first = client.post(path)
        assert first.status_code == 200
        body = first.json()
        assert body["basis"]["intent"] == "宽松落肩小狗图案T恤"
        assert body["basis"]["requirements"] == ["胸前小狗图案"]
        assert body["fields"]["graphic_placement"].startswith("效果图参考：")
        assert body["fields"]["material"] == body["fields"]["measurements"] == ""
        assert client.post(path).json()["id"] == body["id"]
        saved = client.post(f"/api/design-versions/{vid}/sampling-sheet", json={
            "expected_sheet_id": body["id"], "material": "全棉，克重待核对"
        }).json()
        assert saved["basis"] == body["basis"]
        assert saved["fields"]["material"] == "全棉，克重待核对"


def test_first_sample_pack_keeps_unknowns_and_uses_selected_version(env):
    from app.main import app

    with transaction() as db:
        sid = service.save_spec(
            db,
            env,
            SpecIn(
                intent="宽松落肩小狗图案T恤",
                constraints=[{"id": "c_dog", "kind": "must_keep", "text": "胸前小狗图案"}],
            ),
        ).id
    provider = FakeProvider()
    tid = task(env, sid, provider)
    run_task(tid, provider)
    vid = read_task(tid)[1]["current_version_id"]
    with TestClient(app) as client:
        url = f"/api/design-versions/{vid}/sample-pack"
        assert client.get(url).json()["error"]["code"] == "CONFIRM_REQUIRED"
        service.confirm_version(vid)
        response = client.get(url)
        assert response.status_code == 200
        archive = zipfile.ZipFile(io.BytesIO(response.content))
        assert {
            "selected-design.png", "front-flat.svg", "back-flat.svg",
            "first-sample-brief.pdf", "first-sample-brief.json", "README.txt",
        } == set(archive.namelist())
        data = json.loads(archive.read("first-sample-brief.json"))
        assert data["version_id"] == vid
        assert data["confirmed_requirements"] == ["胸前小狗图案"]
        assert "面料与辅料" in data["unknown_fields"]
        assert "尺码与关键尺寸" in data["unknown_fields"]
        assert data["fields"]["material"] == data["fields"]["measurements"] == ""
        assert b"<svg" in archive.read("front-flat.svg")
        assert archive.read("first-sample-brief.pdf").startswith(b"%PDF-")
        assert client.get(f"{url}/front.svg").status_code == 200


def test_sample_pack_without_reliable_flat_omits_placeholder_svg(env):
    from app.main import app

    with transaction() as db:
        sid = service.save_spec(db, env, SpecIn(intent="利落收腰通勤连衣裙")).id
    provider = FakeProvider()
    tid = task(env, sid, provider)
    run_task(tid, provider)
    vid = read_task(tid)[1]["current_version_id"]
    service.confirm_version(vid)
    with TestClient(app) as client:
        url = f"/api/design-versions/{vid}/sample-pack"
        response = client.get(url)
        assert response.status_code == 200
        archive = zipfile.ZipFile(io.BytesIO(response.content))
        assert "selected-design.png" in archive.namelist()
        assert "first-sample-brief.pdf" in archive.namelist()
        assert "front-flat.svg" not in archive.namelist()
        assert "back-flat.svg" not in archive.namelist()
        assert client.get(f"{url}/front.svg").status_code == 404


def test_model_call_failure_has_no_secrets_or_automatic_retry(env, monkeypatch):
    import httpx

    calls = []

    def fail(*args, **kwargs):
        calls.append(1)
        raise httpx.ConnectError("secret-key-this-must-not-leak")

    monkeypatch.setattr(httpx, "Client", fail)
    with pytest.raises(AgentError) as exc:
        Provider._post("https://invalid", "secret", {})
    assert exc.value.code == "CALL_OUTCOME_UNKNOWN"
    assert "secret" not in exc.value.message and len(calls) == 1


@pytest.mark.parametrize("finding", ["goal", "preservation"])
def test_overall_goal_and_non_target_drift_block_confirmation(env, finding):
    sid = spec(env)
    provider = FakeProvider()
    first = task(env, sid, provider)
    run_task(first, provider)
    vid = read_task(first)[1]["current_version_id"]
    revised = service.revise(vid, RevisionIn(text="只改短袖", edit_region="袖子", expected_spec_id=sid))
    provider = FakeProvider()
    original_compare = provider.compare

    def compare(*args):
        result = original_compare(*args)
        result[finding] = {"status": "deviation", "evidence": "测试整体目标/非目标区域偏差"}
        return result

    provider.compare = compare
    second = task(env, revised["id"], provider, key="finding-check")
    run_task(second, provider)
    with pytest.raises(AgentError) as exc:
        service.confirm_version(read_task(second)[1]["current_version_id"])
    assert exc.value.code in {"GOAL_UNRESOLVED", "PRESERVATION_UNRESOLVED"}


def test_legacy_monthly_allocation_does_not_block_design_tasks(env):
    sid = spec(env)
    provider = FakeProvider()
    provider.monthly_fen = 1
    id = task(env, sid, provider)
    run_task(id, provider)
    assert read_task(id)[0] == "awaiting_review"
    second = task(env, sid, provider, key="next-month-test")
    run_task(second, provider)
    assert read_task(second)[0] == "awaiting_review"
    assert read_task(second)[1]["reasoning_calls"] > 0


def test_understanding_calls_do_not_consume_image_task_allocation(env):
    from app.agent.store import now

    sid = spec(env)
    with transaction() as db:
        create(
            db,
            env,
            "task",
            {"mode": "understand", "steps": [{"reserved_cost_fen": 100, "started_at": now()}]},
            status="completed",
        )
    provider = FakeProvider()
    provider.monthly_fen = 20
    tid = task(env, sid, provider, key="design-after-long-chat")
    run_task(tid, provider)
    status, data = read_task(tid)
    assert status == "awaiting_review"
    assert data["reserved_cost_fen"] <= provider.monthly_fen


def test_real_adapter_contract_passes_all_images_not_only_prompt(env, monkeypatch):
    provider = Provider()
    provider.enabled, provider.image_verified = True, True
    provider.vision_url = "https://example.invalid/v1"
    provider.vision_model, provider.vision_key = "test-model", "not-a-real-key"
    provider.image_key, provider.image_model = "not-a-real-key", "test-image-model"
    provider.reasoning_fen, provider.image_fen, provider.monthly_fen = 1, 10, 100
    requests = []

    def post(url, key, payload):
        import base64

        requests.append(payload)
        return {"data": [{"b64_json": base64.b64encode(png()).decode()}]}

    monkeypatch.setattr(provider, "_post", post)
    refs = [("structure", save_image(png())), ("fabric", save_image(png("green")))]
    assert provider.render({"intent": "test"}, refs, None) == png()
    assert len(requests[0]["image"]) == 2
    assert requests[0]["image"][0] != requests[0]["image"][1]
    assert requests[0]["sequential_image_generation"] == "disabled"
    assert requests[0]["response_format"] == "b64_json"


def _generation_provider(monkeypatch, calls):
    import httpx

    provider = Provider()
    provider.enabled, provider.image_verified = True, True
    provider.vision_url = "https://example.invalid/v1"
    provider.vision_model, provider.vision_key = "test-model", "not-a-real-key"
    provider.image_url = "https://image.example.test/v1"
    provider.image_key, provider.image_model = "not-a-real-key", "test-image-model"
    provider.reasoning_fen, provider.image_fen, provider.monthly_fen = 1, 10, 100
    original = httpx.Client

    def handler(request):
        import base64

        calls.append(request)
        return httpx.Response(200, json={"data": [{"b64_json": base64.b64encode(png()).decode()}]})

    monkeypatch.setattr(httpx, "Client", lambda **kwargs: original(transport=httpx.MockTransport(handler), **kwargs))
    return provider


def test_image_reference_format_setting(env, monkeypatch):
    monkeypatch.delenv("IMAGE_REFERENCE_FORMAT", raising=False)
    config.get_config.cache_clear()
    assert config.get_config().image_reference_format == "list"
    assert Provider().image_reference_format == "list"

    monkeypatch.setenv("IMAGE_REFERENCE_FORMAT", " SINGLE ")
    config.get_config.cache_clear()
    assert config.get_config().image_reference_format == "single"
    assert Provider().image_reference_format == "single"

    monkeypatch.setenv("IMAGE_REFERENCE_FORMAT", "array")
    config.get_config.cache_clear()
    assert config.get_config().image_reference_format == "list"


def test_render_list_format_posts_reference_images_as_array(env, monkeypatch, caplog):
    import logging

    monkeypatch.delenv("IMAGE_REFERENCE_FORMAT", raising=False)
    config.get_config.cache_clear()
    calls = []
    provider = _generation_provider(monkeypatch, calls)
    refs = [
        ("asset_id=fabric; role=fabric; region=整体; 面料", save_image(png("green"))),
        ("base_version_id=design-1; 修改底图", save_image(png("blue"))),
    ]
    with caplog.at_level(logging.INFO):
        assert provider.render({"intent": "test"}, refs, None) == png()
    assert str(calls[0].url) == "https://image.example.test/v1/images/generations"
    body = json.loads(calls[0].content)
    assert body["image"] == [data_url(refs[0][1]), data_url(refs[1][1])]
    assert body["sequential_image_generation"] == "disabled"
    assert body["response_format"] == "b64_json"
    assert body["watermark"] is True
    assert body["stream"] is False
    assert "已忽略其余" not in caplog.text


def test_render_single_format_posts_primary_reference_as_string(env, monkeypatch, caplog):
    import logging

    monkeypatch.setenv("IMAGE_REFERENCE_FORMAT", "single")
    config.get_config.cache_clear()
    calls = []
    provider = _generation_provider(monkeypatch, calls)
    user = ("asset_id=fabric; role=fabric; region=整体; 面料", save_image(png("green")))
    detail = ("asset_id=detail; role=detail; region=领口; 领口", save_image(png("white")))
    base = ("base_version_id=design-1; 修改底图", save_image(png("blue")))
    current = ("current_version_id=design-2; 本轮修正底图", save_image(png("red")))
    caplog.set_level(logging.INFO)

    assert provider.render({"intent": "edit"}, [user, base, detail, current], None) == png()
    body = json.loads(calls[0].content)
    assert str(calls[0].url) == "https://image.example.test/v1/images/generations"
    assert body["image"] == data_url(current[1])
    assert body["sequential_image_generation"] == "disabled"
    assert body["watermark"] is True
    assert body["stream"] is False
    assert body["response_format"] == "b64_json"
    assert "已忽略其余 3 张参考图" in caplog.text

    caplog.clear()
    provider.render({"intent": "edit"}, [user, detail, base], None)
    assert json.loads(calls[1].content)["image"] == data_url(base[1])
    assert "已忽略其余 2 张参考图" in caplog.text

    caplog.clear()
    plain = [("structure", save_image(png())), ("fabric", save_image(png("green")))]
    provider.render({"intent": "new"}, plain, None)
    assert json.loads(calls[2].content)["image"] == data_url(plain[0][1])
    assert "已忽略其余 1 张参考图" in caplog.text

    caplog.clear()
    provider.render({"intent": "new"}, [plain[0]], None)
    assert json.loads(calls[3].content)["image"] == data_url(plain[0][1])
    assert "已忽略其余" not in caplog.text


def test_visual_correction_preserves_machine_review_but_physical_is_blocked(env):
    from app.agent.schemas import CorrectionIn

    sid = spec(env)
    provider = FakeProvider(deviation=True)
    id = task(env, sid, provider)
    run_task(id, provider)
    vid = read_task(id)[1]["current_version_id"]
    correction = CorrectionIn(
        constraint_id="c_collar", status="pass", evidence="设计师核对了候选正面领口，四角与草图方领一致"
    )
    row = service.correct_check(vid, correction)
    assert row["review"]["checks"][0]["source"] == "designer_correction"
    assert row["reviews"][0]["checks"][0]["status"] == "deviation"
    with transaction() as db:
        require(db, sid).payload = {
            **require(db, sid).payload,
            "spec": {
                **require(db, sid).payload["spec"],
                "constraints": [
                    {**c, "verification": "physical"} for c in require(db, sid).payload["spec"]["constraints"]
                ],
            },
        }
    with pytest.raises(AgentError, match="不能勾选"):
        service.correct_check(vid, correction)


def test_confirmed_design_becomes_historical_when_requirements_change(env):
    sid = spec(env)
    provider = FakeProvider()
    id = task(env, sid, provider)
    run_task(id, provider)
    vid = read_task(id)[1]["current_version_id"]
    service.confirm_version(vid)
    assert read_task(id)[0] == "completed"
    with transaction() as db:
        service.save_spec(db, env, SpecIn(intent="改变设计方向", expected_spec_id=sid))
        assert require(db, vid).status == "superseded"
    with pytest.raises(AgentError, match="已变更"):
        service.confirm_version(vid)


def test_confirmed_design_remains_selected_during_revision_until_new_version_is_confirmed(env):
    sid = spec(env)
    provider = FakeProvider()
    first_task = task(env, sid, provider)
    run_task(first_task, provider)
    first_id = read_task(first_task)[1]["current_version_id"]
    service.confirm_version(first_id)

    revised = service.revise(first_id, RevisionIn(text="只把领口改为圆领", edit_region="领口", expected_spec_id=sid))
    with transaction() as db:
        assert require(db, first_id, "version").status == "confirmed"
        assert service.head(db, env).payload["confirmed_version_id"] == first_id
    service.confirm_spec(revised["id"])
    second_task = task(env, revised["id"], provider, key="revised-image")
    run_task(second_task, provider)
    second_id = read_task(second_task)[1]["current_version_id"]
    with transaction() as db:
        assert require(db, first_id, "version").status == "confirmed"
        assert service.head(db, env).payload["confirmed_version_id"] == first_id
    service.confirm_version(second_id)
    with transaction() as db:
        assert require(db, first_id, "version").status == "superseded"
        assert service.head(db, env).payload["confirmed_version_id"] == second_id


def test_sibling_designs_can_each_be_confirmed_and_keep_individual_handoff(env):
    sid = spec(env)
    provider = FakeProvider()
    generation = task(env, sid, provider)
    run_task(generation, provider)
    first_id = read_task(generation)[1]["current_version_id"]
    with transaction() as db:
        first = require(db, first_id, "version", env)
        second_id = create(
            db, env, "version", {**first.payload, "design_index": 2, "design_count": 2}, "ready_for_review"
        ).id
    service.confirm_version(first_id)
    service.confirm_version(second_id)
    with transaction() as db:
        assert require(db, first_id, "version").status == "confirmed"
        assert require(db, second_id, "version").status == "confirmed"
    assert service.auto_sampling_sheet(first_id)["version_id"] == first_id
    assert service.auto_sampling_sheet(second_id)["version_id"] == second_id


def test_older_task_reservations_do_not_block_new_design(env):
    from app.agent.store import now

    sid = spec(env)
    provider = FakeProvider()
    provider.monthly_fen = 10
    with transaction() as db:
        older = create(
            db, env, "task", {"mode": "design", "steps": [{"started_at": now(), "reserved_cost_fen": 10}]}, "cancelled"
        )
        older.created_at = "2025-01-01T00:00:00+00:00"
    id = task(env, sid, provider)
    run_task(id, provider)
    assert read_task(id)[0] == "awaiting_review"
    assert provider.calls


@pytest.mark.parametrize(
    ("status", "vendor_code", "expected"),
    [
        (404, "ModelNotOpen", "PROVIDER_MODEL_NOT_OPEN"),
        (404, "InvalidEndpointOrModel.NotFound", "PROVIDER_MODEL_UNAVAILABLE"),
        (403, "Forbidden", "PROVIDER_ACCESS_DENIED"),
        (429, "RateLimitExceeded", "PROVIDER_RATE_LIMITED"),
        (400, "InvalidParameter", "PROVIDER_REJECTED"),
    ],
)
def test_provider_errors_are_actionable_without_vendor_secrets(env, monkeypatch, status, vendor_code, expected):
    import httpx

    calls = []
    original_client = httpx.Client

    def handler(request):
        calls.append(request)
        return httpx.Response(status, json={"error": {"code": vendor_code, "message": "private-secret-account-id"}})

    monkeypatch.setattr(
        httpx, "Client", lambda **kwargs: original_client(transport=httpx.MockTransport(handler), **kwargs)
    )
    with pytest.raises(AgentError) as exc:
        Provider._post("https://ark.example.test/chat/completions", "private-secret-key", {})
    assert exc.value.code == expected
    assert "private" not in exc.value.message
    assert len(calls) == 1


def test_vision_adapter_sends_images_json_and_explicit_thinking(env, monkeypatch):
    from app.agent.schemas import Inspection

    provider = Provider()
    provider.enabled = True
    provider.vision_url = "https://ark.example.test/v3"
    provider.vision_key, provider.vision_model = "private-secret-key", "vision-test"
    provider.vision_thinking = "disabled"
    provider.reasoning_fen, provider.monthly_fen = 100, 2000
    requests = []

    def post(url, key, payload):
        requests.append(payload)
        return {
            "id": "test-receipt",
            "usage": {"prompt_tokens": 123, "completion_tokens": 12},
            "choices": [{"message": {"content": json.dumps({"observations": [], "conflicts": []})}}],
        }

    monkeypatch.setattr(provider, "_post", post)
    provider.structured("inspect-v1", {"intent": "test"}, Inspection, [("structure", save_image(png()))])
    request = requests[0]
    assert request["thinking"] == {"type": "disabled"}
    assert request["response_format"] == {"type": "json_object"}
    assert request["max_tokens"] == 4096
    assert request["messages"][1]["content"][2]["image_url"]["url"].startswith("data:image/png;base64,")
    assert provider.last_receipt["usage"]["prompt_tokens"] == 123
    assert "private" not in json.dumps(provider.capabilities())


def test_vision_adapter_rejects_invalid_structured_response(env, monkeypatch):
    provider = Provider()
    provider.enabled = True
    provider.vision_url, provider.vision_model, provider.vision_key = "https://example.test", "vision", "secret"
    provider.reasoning_fen, provider.monthly_fen = 100, 2000
    monkeypatch.setattr(provider, "_post", lambda *args: {"choices": [{"message": {"content": '{"invented":true}'}}]})
    with pytest.raises(AgentError) as exc:
        provider.inspect({}, [])
    assert exc.value.code == "MODEL_SCHEMA_INVALID"


class ChatProvider(FakeProvider):
    def capabilities(self):
        return {"test_only": True, "understand": True}


def test_chat_disabled_persists_turns_without_fake_assistant(env):
    from app.agent.chat import send
    from app.agent.schemas import ChatIn

    first = ChatIn(text="做方领裙", idempotency_key="first-message")
    saved = send(env, first)
    assert send(env, first)["id"] == saved["id"]
    state = service.workspace(env)
    send(env, ChatIn(text="裙摆加长", expected_spec_id=state["head"]["spec_id"], idempotency_key="second-message"))
    state = service.workspace(env)
    assert [m["role"] for m in state["messages"]] == ["user", "system", "user", "system"]
    assert [m["text"] for m in state["messages"] if m["role"] == "user"] == ["做方领裙", "裙摆加长"]
    assert not state["tasks"]
    assert len(state["specs"]) == 1
    with pytest.raises(AgentError, match="同一消息编号"):
        send(env, first.model_copy(update={"text": "其他内容"}))


def test_chat_authorization_atomicity_and_stale_source(env):
    from app.agent.chat import send
    from app.agent.schemas import ChatIn

    provider = ChatProvider()
    data = ChatIn(text="做方领裙", idempotency_key="first-message")
    with pytest.raises(AgentError, match="允许本轮"):
        send(env, data, provider)
    state = service.workspace(env)
    assert not state["specs"] and not state["messages"] and not state["tasks"]
    saved = send(env, data.model_copy(update={"authorized": True}), provider)
    assert send(env, data.model_copy(update={"authorized": True}), provider)["id"] == saved["id"]
    with pytest.raises(AgentError) as error:
        send(env, data.model_copy(update={"idempotency_key": "another-message"}), provider)
    assert error.value.code == "STALE_SOURCE"
    state = service.workspace(env)
    with pytest.raises(AgentError) as error:
        send(
            env,
            data.model_copy(
                update={"idempotency_key": "another-message", "expected_spec_id": state["head"]["spec_id"]}
            ),
            provider,
        )
    assert error.value.code == "TASK_ACTIVE"
    assert len(service.workspace(env)["tasks"]) == 1


def test_chat_ask_resume_reply_context_and_live_preview(env):
    from app.agent.chat import send
    from app.agent.schemas import ChatIn

    class StreamingChatProvider(ChatProvider):
        def plan(self, context):
            if len(self.calls) > 0:
                self.on_preview({"answer": "可以，保留方领"})
                preview = service.workspace(env)["tasks"][-1]
                assert preview["live_text"] == "可以，保留方领"
                assert preview["status"] == "running"
            return super().plan(context)

    provider = StreamingChatProvider(
        [
            {"action": "ask", "summary": "确认长度", "questions": ["裙长到哪里？"]},
            {"action": "reply", "summary": "说明修改方向", "answer": "可以，保留方领并将裙摆延长到脚踝。"},
        ]
    )
    first = send(env, ChatIn(text="帮我调整裙长", idempotency_key="first-message", authorized=True), provider)
    tid = first["task_id"]
    with transaction() as db:
        require(db, tid).status = "running"
    run_task(tid, provider)
    before = service.workspace(env)
    assert before["tasks"][0]["status"] == "awaiting_input"
    answer = ChatIn(text="到脚踝", idempotency_key="answer-message", expected_spec_id=before["head"]["spec_id"])
    sent = send(env, answer, provider)
    assert send(env, answer, provider)["id"] == sent["id"]
    queued = service.workspace(env)
    assert len(queued["tasks"]) == 1
    assert queued["tasks"][0]["reserved_cost_fen"] == before["tasks"][0]["reserved_cost_fen"]
    with transaction() as db:
        require(db, tid).status = "running"
    run_task(tid, provider)
    state = service.workspace(env)
    assert state["tasks"][0]["status"] == "awaiting_review"
    assert state["tasks"][0]["live_text"] == ""
    assert [m["role"] for m in state["messages"]] == ["user", "assistant", "user", "assistant"]
    assert state["messages"][-1]["text"].endswith("延长到脚踝。")
    assert provider.calls[-1][1]["conversation"][-1] == {"role": "user", "text": "到脚踝"}
    assert state["tasks"][0]["image_calls"] == 0


def test_understanding_chat_continues_past_old_turn_and_monthly_limits(env):
    from app.agent.chat import send
    from app.agent.schemas import ChatIn

    provider = ChatProvider([{"action": "ask", "summary": "继续讨论", "questions": ["下一处要怎么调整？"]}] * 10)
    provider.reasoning_fen = 100
    provider.monthly_fen = 100
    first = send(env, ChatIn(text="一起设计T恤", idempotency_key="chat-unlimited-first", authorized=True), provider)
    tid = first["task_id"]
    for index in range(10):
        with transaction() as db:
            require(db, tid).status = "running"
        run_task(tid, provider)
        state = service.workspace(env)
        assert state["tasks"][0]["status"] == "awaiting_input"
        if index < 9:
            response = send(
                env,
                ChatIn(
                    text=f"继续修改第 {index + 1} 处",
                    expected_spec_id=state["head"]["spec_id"],
                    idempotency_key=f"chat-unlimited-{index:02d}",
                ),
                provider,
            )
            assert response["task_id"] == tid
    state = service.workspace(env)
    assert state["tasks"][0]["reasoning_calls"] == 10
    assert state["tasks"][0]["reserved_cost_fen"] == 1000
    assert len(state["tasks"]) == 1


def test_chat_can_resume_task_stopped_by_former_budget(env):
    from app.agent.chat import send
    from app.agent.schemas import ChatIn

    provider = ChatProvider()
    first = send(env, ChatIn(text="做一件T恤", idempotency_key="old-budget-first", authorized=True), provider)
    tid = first["task_id"]
    with transaction() as db:
        task_row = require(db, tid, "task")
        task_row.status = "budget_exhausted"
        change(task_row, error={"code": "BUDGET_EXHAUSTED", "message": "旧额度提示"}, outcome="旧额度提示")
    state = service.workspace(env)
    response = send(
        env,
        ChatIn(text="继续聊领口", expected_spec_id=state["head"]["spec_id"], idempotency_key="old-budget-next"),
        provider,
    )
    assert response["task_id"] == tid
    resumed = service.workspace(env)["tasks"][0]
    assert resumed["status"] == "queued"
    assert resumed["error"] is None


def test_chat_followup_can_add_reference_to_paused_understanding_task(env):
    from app.agent.chat import send
    from app.agent.schemas import ChatIn

    provider = ChatProvider()
    first = send(env, ChatIn(text="设计一件T恤", idempotency_key="first-message", authorized=True), provider)
    with transaction() as db:
        paused = require(db, first["task_id"], "task")
        paused.status = "awaiting_input"
        asset = create(db, env, "asset", {**save_image(png()), "name": "面料.png"}, "ready")
        asset_id = asset.id
    before = service.workspace(env)
    followup = ChatIn(
        text="按这张面料图调整颜色",
        references=[{"asset_id": asset_id, "role": "fabric"}],
        expected_spec_id=before["head"]["spec_id"],
        idempotency_key="followup-with-image",
        authorized=True,
    )
    with pytest.raises(AgentError) as error:
        send(env, followup.model_copy(update={"authorized": False}), provider)
    assert error.value.code == "AUTHORIZATION_REQUIRED"
    sent = send(env, followup, provider)
    assert send(env, followup, provider)["id"] == sent["id"]
    state = service.workspace(env)
    assert state["tasks"][0]["status"] == "queued"
    assert state["tasks"][0]["spec_id"] == state["head"]["spec_id"]
    assert state["specs"][-1]["spec"]["references"][0]["asset_id"] == asset_id
    assert state["messages"][-1]["references"][0]["asset_id"] == asset_id
    with transaction() as db:
        require(db, first["task_id"], "task").status = "running"
    run_task(first["task_id"], provider)
    assert any(call[0] == "inspect" for call in provider.calls)


def test_stream_extracts_public_strings_and_split_unicode_only():
    from app.agent.streaming import collect_stream, public_text

    assert public_text('{"nested":{"answer":"private"},"answer":"你好') == {"answer": "你好"}
    assert public_text('{"reasoning_content":"secret","answer":"裙摆\\ud83d') == {"answer": "裙摆"}
    assert public_text('{"answer":"裙摆\\ud83d\\udc57') == {"answer": "裙摆👗"}
    assert public_text('{"answer":"第一行\\n第二行\\') == {"answer": "第一行\n第二行"}
    previews = []
    chunks = [
        {
            "id": "test",
            "choices": [{"index": 0, "delta": {"reasoning_content": "secret", "content": '{"answer":"方领'}}],
        },
        {
            "choices": [
                {
                    "index": 0,
                    "delta": {"content": '连衣裙","action":"reply","summary":"建议"}'},
                    "finish_reason": "stop",
                }
            ]
        },
        {"usage": {"total_tokens": 20}, "choices": []},
    ]
    result = collect_stream([*("data: " + json.dumps(x) for x in chunks), "data: [DONE]"], previews.append)
    assert previews[0] == {"answer": "方领"}
    assert previews[-1]["answer"] == "方领连衣裙"
    assert result["usage"]["total_tokens"] == 20
    assert "secret" not in json.dumps(result) + json.dumps(previews)


@pytest.mark.parametrize(
    "lines,code",
    [
        (['data: {"choices":[]}'], "CALL_OUTCOME_UNKNOWN"),
        (["data: broken"], "CALL_OUTCOME_UNKNOWN"),
        (['data: {"error":{"message":"private vendor detail"}}'], "CALL_OUTCOME_UNKNOWN"),
        (['data: {"choices":[{"finish_reason":"length"}]}', "data: [DONE]"], "MODEL_SCHEMA_INVALID"),
    ],
)
def test_stream_interruption_never_looks_complete(lines, code):
    from app.agent.streaming import collect_stream

    with pytest.raises(AgentError) as error:
        collect_stream(lines, lambda text: None)
    assert error.value.code == code
    assert "private" not in str(error.value)


def test_workspace_sse_replay_does_not_dispatch_model(env):
    import asyncio

    from app.agent.events import stream

    class Connected:
        async def is_disconnected(self):
            return False

    async def collect():
        return [part async for part in stream(env, Connected(), duration=0)]

    for _ in range(2):
        result = asyncio.run(collect())
        assert result[0].startswith("event: workspace\n")
        assert result[-1].startswith("event: done\n")
        assert '"messages": []' in result[0]
    assert service.workspace(env)["tasks"] == []


def test_provider_real_http_adapter_consumes_sse_before_final_validation(env, monkeypatch):
    import httpx

    from app.agent.schemas import Action

    original_client = httpx.Client
    previews = []
    chunks = ['{"answer":"保留', '方领。","action":"reply","summary":"建议"}']

    def handler(request):
        payload = json.loads(request.content)
        assert payload["stream"] is True
        assert payload["stream_options"] == {"include_usage": True}
        frames = ["data: " + json.dumps({"choices": [{"index": 0, "delta": {"content": chunk}}]}) for chunk in chunks]
        frames += [
            'data: {"id":"synthetic-stream","choices":[{"finish_reason":"stop"}]}',
            'data: {"choices":[],"usage":{"total_tokens":20}}',
            "data: [DONE]",
        ]
        return httpx.Response(200, headers={"content-type": "text/event-stream"}, text="\n\n".join(frames))

    monkeypatch.setattr(
        httpx, "Client", lambda **kwargs: original_client(transport=httpx.MockTransport(handler), **kwargs)
    )
    provider = Provider()
    provider.enabled = True
    provider.vision_key, provider.vision_model = "synthetic-secret", "synthetic-vision"
    provider.reasoning_fen, provider.monthly_fen = 100, 2000
    provider.vision_url = "https://synthetic.invalid/api/v3"
    provider.on_preview = previews.append
    result = provider.structured("agent-v1", {"intent": "test"}, Action)
    assert previews[0] == {"answer": "保留"}
    assert result["action"] == "reply" and result["answer"] == "保留方领。"
    assert provider.last_receipt["request_id"] == "synthetic-stream"


def protocol_provider(monkeypatch, outputs):
    provider = Provider()
    provider.enabled = True
    provider.vision_url, provider.vision_model, provider.vision_key = "https://example.test", "vision", "secret"
    provider.reasoning_fen, provider.monthly_fen = 1, 20000
    requests = []
    remaining = iter(outputs)

    def post(url, key, payload, **kwargs):
        requests.append(payload)
        output = next(remaining)
        if isinstance(output, Exception):
            raise output
        return {
            "id": "safe-receipt",
            "usage": {"prompt_tokens": 10},
            "choices": [{"message": {"content": json.dumps(output)}}],
        }

    monkeypatch.setattr(provider, "_post", post)
    return provider, requests


def test_schema_error_is_corrected_once_with_budget_and_safe_diagnostics(env, monkeypatch):
    broken = {"action": "ask", "summary": "testing", "questions": None, "designer-secret-field": "private-value"}
    fixed = {"action": "ask", "summary": "继续讨论", "questions": ["偏好合身还是宽松？"]}
    provider, requests = protocol_provider(monkeypatch, [broken, fixed])
    tid = task(env, spec(env, refs=0), provider, mode="understand")
    run_task(tid, provider)
    status, data = read_task(tid)
    assert status == "awaiting_input"
    assert data["reasoning_calls"] == 2 and data["reserved_cost_fen"] == 2
    errors = data["steps"][0]["validation_errors"]
    assert {"path": "questions", "type": "list_type"} in errors
    assert data["steps"][0]["receipt"]["usage"]["prompt_tokens"] == 10
    assert "designer-secret-field" not in json.dumps(data)
    assert "private-value" not in json.dumps(data)
    correction_context = json.loads(requests[1]["messages"][1]["content"][0]["text"])
    assert correction_context["schema_feedback"] == errors
    assert len(service.workspace(env)["messages"]) == 1


def test_second_schema_failure_stops_and_retains_diagnostics(env, monkeypatch):
    provider, requests = protocol_provider(
        monkeypatch, [{"action": "ask", "summary": "testing", "questions": None}] * 2
    )
    tid = task(env, spec(env, refs=0), provider, mode="understand")
    run_task(tid, provider)
    status, data = read_task(tid)
    assert status == "failed" and len(requests) == 2
    assert data["steps"][1]["validation_errors"] == [{"path": "questions", "type": "list_type"}]
    assert data["steps"][1]["receipt"]["usage"]["prompt_tokens"] == 10
    assert not service.workspace(env)["messages"]


def test_schema_correction_cannot_exceed_budget(env, monkeypatch):
    provider, requests = protocol_provider(monkeypatch, [{"action": "ask", "summary": "testing", "questions": None}])
    provider.image_verified = True
    provider.image_key, provider.image_model, provider.image_fen = "synthetic-key", "synthetic-image", 1
    tid = task(env, spec(env, refs=0), provider, mode="design", max_cost_fen=1)
    run_task(tid, provider)
    status, data = read_task(tid)
    assert status == "budget_exhausted" and len(requests) == 1
    assert data["reserved_cost_fen"] == 1


def test_unknown_call_is_not_retried_by_schema_correction(env, monkeypatch):
    provider, requests = protocol_provider(monkeypatch, [AgentError("CALL_OUTCOME_UNKNOWN", "connection lost")])
    tid = task(env, spec(env, refs=0), provider, mode="understand")
    run_task(tid, provider)
    status, data = read_task(tid)
    assert status == "interrupted" and len(requests) == 1
    assert data["steps"][0]["status"] == "unknown"


@pytest.mark.parametrize("count", [3, 4, 5, 6])
def test_multiple_designs_are_independent_and_all_checked(env, count):
    provider = FakeProvider()
    sid = spec(env, refs=1)
    tid = task(env, sid, provider, design_count=count, max_image_calls=count)
    run_task(tid, provider)
    status, state = read_task(tid)
    assert status == "awaiting_review", state
    assert state["image_calls"] == count and state["reasoning_calls"] == count + 1
    versions = service.workspace(env)["versions"]
    assert len(versions) == count
    assert [v["design_index"] for v in versions] == list(range(1, count + 1))
    assert all(v["spec_id"] == sid and v["parent_version_id"] is None and v["review"] for v in versions)
    renders = [call for call in provider.calls if call[0] == "render"]
    assert all(call[1]["constraints"] == renders[0][1]["constraints"] for call in renders)
    assert len({call[1]["design_direction"]["explore"] for call in renders}) == count
    presentation = state["presentation"]
    assert all(call[1]["presentation"] == presentation for call in renders)
    assert all(call[2]["presentation"] == presentation for call in provider.calls if call[0] == "compare")
    assert len([call for call in provider.calls if call[0] == "inspect"]) == 1
    comparisons = [call[2]["candidate_context"] for call in provider.calls if call[0] == "compare"]
    assert [c["number"] for c in comparisons] == list(range(1, count + 1))
    assert all(c["total"] == count for c in comparisons)
    again = service.submit(
        env,
        TaskIn(
            spec_id=sid,
            mode="design",
            authorized=True,
            idempotency_key="unique-key",
            design_count=count,
            max_image_calls=count,
        ),
        provider,
    )
    assert again["id"] == tid
    assert len(service.workspace(env)["versions"]) == count


def test_multiple_designs_reject_insufficient_budget_before_any_call(env):
    provider = FakeProvider()
    sid = spec(env)
    with pytest.raises(AgentError) as error:
        task(env, sid, provider, design_count=6, max_image_calls=6, max_cost_fen=1)
    assert error.value.code == "BUDGET_EXHAUSTED"
    assert not provider.calls
    assert not service.workspace(env)["tasks"]


def test_multiple_designs_keep_completed_candidate_when_later_call_fails(env):
    class FailingProvider(FakeProvider):
        fail_second = True

        def render(self, spec, images, feedback):
            if self.fail_second and spec["design_direction"]["number"] == 2:
                raise AgentError("CALL_OUTCOME_UNKNOWN", "test interruption")
            return super().render(spec, images, feedback)

    provider = FailingProvider()
    tid = task(env, spec(env), provider, design_count=3, max_image_calls=3)
    run_task(tid, provider)
    status, state = read_task(tid)
    assert status == "interrupted"
    versions = service.workspace(env)["versions"]
    assert len(versions) == 1 and versions[0]["review"]
    assert state["steps"][-1]["status"] == "unknown"
    original_spec = service.workspace(env)["specs"][0]
    with transaction() as db:
        duplicate = service.save_spec(
            db, env, SpecIn.model_validate({**original_spec["spec"], "expected_spec_id": original_spec["id"]})
        )
    assert service.workspace(env)["head"]["spec_id"] == duplicate.id

    resumed = service.task_control(tid, "resume")
    assert resumed["status"] == "queued"
    assert service.workspace(env)["head"]["spec_id"] == original_spec["id"]
    assert read_task(tid)[1]["steps"][-1]["status"] == "unknown"
    provider.fail_second = False
    with transaction() as db:
        require(db, tid, "task").status = "running"
    run_task(tid, provider)
    status, state = read_task(tid)
    assert status == "awaiting_review"
    assert [v["design_index"] for v in service.workspace(env)["versions"]] == [1, 2, 3]
    assert state["direction_version_ids"][0] == versions[0]["id"]


def test_unknown_image_call_requires_ending_round_before_manual_resubmission(env):
    class ConnectionLost(FakeProvider):
        def render(self, spec, images, feedback):
            raise AgentError("CALL_OUTCOME_UNKNOWN", "connection lost")

    provider = ConnectionLost()
    sid = spec(env)
    first = task(env, sid, provider)
    run_task(first, provider)
    assert read_task(first)[0] == "interrupted"
    with pytest.raises(AgentError) as error:
        task(env, sid, provider, key="new-call-after-unknown")
    assert error.value.code == "UNKNOWN_CALL"
    assert len(service.workspace(env)["tasks"]) == 1

    service.task_control(first, "cancel")
    second = task(env, sid, provider, key="new-call-after-unknown")
    assert second != first
    assert read_task(second)[0] == "running"


def test_candidate_goal_deviation_requires_revision_even_when_constraints_pass(env):
    class GoalDeviationProvider(FakeProvider):
        def compare(self, spec, images):
            review = super().compare(spec, images)
            review["goal"] = {"status": "deviation", "evidence": "整体目标未达到"}
            return review

    provider = GoalDeviationProvider()
    tid = task(env, spec(env), provider, design_count=3, max_image_calls=3)
    run_task(tid, provider)
    versions = service.workspace(env)["versions"]
    assert len(versions) == 3
    assert all(v["status"] == "needs_revision" for v in versions)


@pytest.mark.parametrize("count", [0, 7])
def test_design_count_range_is_validated(count):
    from pydantic import ValidationError

    with pytest.raises(ValidationError):
        TaskIn(spec_id="test", mode="design", authorized=True, idempotency_key="range-test", design_count=count)


@pytest.mark.parametrize("refs", [0, 3])
def test_review_short_aliases_resolve_only_to_supplied_images(env, refs):
    class AliasedProvider(FakeProvider):
        def compare(self, spec, images):
            result = super().compare(spec, images)
            assert spec["available_image_ids"] == [f"image_{i + 1}" for i in range(len(images))]
            for check in result["checks"]:
                check["reference_ids"] = [f"candidate_id={spec['available_image_ids'][-1]}"]
            return result

    provider = AliasedProvider()
    tid = task(env, spec(env, refs=refs), provider, design_count=3, max_image_calls=3)
    run_task(tid, provider)
    assert read_task(tid)[0] == "awaiting_review"
    for version in service.workspace(env)["versions"]:
        assert all(c["reference_ids"] == [version["id"]] for c in version["review"]["checks"])


@pytest.mark.parametrize("foreign", ["image_99", "candidate_id=image_99", "private-foreign-image", "image_1; image_99"])
def test_review_foreign_reference_is_not_guessed(env, foreign):
    from app.agent import review

    with pytest.raises(AgentError) as error:
        review.resolve(
            {
                "goal": {"status": "pass", "evidence": "x"},
                "preservation": {"status": "pass", "evidence": "x"},
                "checks": [
                    {
                        "constraint_id": "c_a",
                        "status": "pass",
                        "candidate_region": "x",
                        "evidence": "x",
                        "reference_ids": [foreign],
                    }
                ],
                "summary": "x",
            },
            {"image_1": "actual-image"},
            {"constraints": [{"id": "c_a"}]},
        )
    assert error.value.code == "REVIEW_INVALID"
    assert foreign not in json.dumps(error.value.details)


@pytest.mark.parametrize("repair_success", [True, False])
def test_review_retry_is_bounded_and_never_regenerates_images(env, repair_success):
    class BrokenReview(FakeProvider):
        def compare(self, spec, images):
            result = super().compare(spec, images)
            # Fail the first attempt of every image; optionally fail the repair too.
            if not repair_success or "validation_feedback" not in spec:
                result["checks"][0]["reference_ids"] = ["invented-image"]
            else:
                result["checks"][0]["reference_ids"] = [spec["available_image_ids"][-1]]
            return result

    provider = BrokenReview()
    tid = task(env, spec(env), provider, design_count=3, max_image_calls=3)
    run_task(tid, provider)
    status, data = read_task(tid)
    assert status == "awaiting_review" and not data.get("error")
    assert data["image_calls"] == 3 and data["reasoning_calls"] == 6
    versions = service.workspace(env)["versions"]
    assert len(versions) == 3
    assert all(v["status"] == ("ready_for_review" if repair_success else "needs_revision") for v in versions)
    assert len([s for s in data["steps"] if s["status"] == "failed"]) == (3 if repair_success else 6)
    if not repair_success:
        assert all(v["review"]["goal"]["status"] == "unknown" for v in versions)
        with pytest.raises(AgentError):
            service.confirm_version(versions[0]["id"])
    assert "invented-image" not in json.dumps(data)


def test_review_repair_cannot_spend_budget_needed_by_remaining_designs(env):
    class BrokenReview(FakeProvider):
        def compare(self, spec, images):
            result = super().compare(spec, images)
            result["checks"][0]["reference_ids"] = ["unknown"]
            return result

    provider = BrokenReview()
    tid = task(env, spec(env), provider, design_count=3, max_image_calls=3, max_cost_fen=33)
    run_task(tid, provider)
    status, data = read_task(tid)
    assert status == "awaiting_review"
    assert data["image_calls"] == 3 and data["reasoning_calls"] == 3
    assert data["reserved_cost_fen"] == 33
    assert len(service.workspace(env)["versions"]) == 3


def test_unknown_review_network_outcome_never_retries(env):
    class InterruptedReview(FakeProvider):
        def compare(self, spec, images):
            raise AgentError("CALL_OUTCOME_UNKNOWN", "test interruption")

    tid = task(env, spec(env), InterruptedReview(), design_count=3, max_image_calls=3)
    run_task(tid, InterruptedReview())
    status, data = read_task(tid)
    assert status == "interrupted" and data["reasoning_calls"] == 1
    assert data["steps"][-1]["status"] == "unknown"
    assert len(service.workspace(env)["versions"]) == 1


def test_review_alias_binding_includes_original_base_and_edit_parent():
    from app.agent import review

    original = {
        "references": [{"asset_id": "fabric-real", "role": "fabric"}],
        "base_version_id": "base-real",
        "constraints": [],
    }
    images = [
        ("base_version_id=base-real; 修改底图", {}),
        ("asset_id=fabric-real; role=fabric", {}),
        ("parent_version_id=parent-real; 修正前底图", {}),
        ("candidate_id=current-real; 待检查候选", {}),
    ]
    context, labeled, mapping = review.prepare(original, images)
    assert context["references"][0]["asset_id"] == "image_2"
    assert context["base_version_id"] == "image_1"
    assert mapping == {
        "image_1": "base-real",
        "image_2": "fabric-real",
        "image_3": "parent-real",
        "image_4": "current-real",
    }
    assert original["references"][0]["asset_id"] == "fabric-real"
    assert all("real" not in label for label, _ in labeled)


def test_review_schema_failure_keeps_single_existing_image_and_blocks_confirmation(env):
    class InvalidReview(FakeProvider):
        def compare(self, spec, images):
            return {"summary": "invalid structured result"}

    provider = InvalidReview()
    tid = task(env, spec(env), provider)
    run_task(tid, provider)
    status, data = read_task(tid)
    assert status == "awaiting_review" and data["image_calls"] == 1
    assert len([s for s in data["steps"] if s["tool"] == "compare_design"]) == 2
    versions = service.workspace(env)["versions"]
    assert len(versions) == 1 and versions[0]["review"]["goal"]["status"] == "unknown"
    assert data["live_text"] == ""
    with pytest.raises(AgentError):
        service.confirm_version(versions[0]["id"])


def test_shoe_presentation_and_directions_use_shoe_category():
    from app.agent.presentation import exploration_axes, presentation_for

    shoe = {"intent": "设计通勤包趾平底凉鞋"}
    assert "鞋头朝左" in presentation_for(shoe)["view"]
    assert "不出现人物" in presentation_for(shoe)["subject"]
    assert all("领口" not in axis and "腰" not in axis for axis in exploration_axes(shoe))
    assert "正面" in presentation_for({"intent": "连衣裙"})["view"]


def test_switching_revision_target_does_not_copy_other_candidate_changes(env):
    provider = FakeProvider()
    sid = spec(env)
    tid = task(env, sid, provider, design_count=3, max_image_calls=3)
    run_task(tid, provider)
    versions = service.workspace(env)["versions"]
    first = service.revise(versions[0]["id"], RevisionIn(text="第一款加口袋", edit_region="裙摆", expected_spec_id=sid))
    second = service.revise(
        versions[1]["id"], RevisionIn(text="第二款改袖口", edit_region="袖子", expected_spec_id=first["id"])
    )
    assert second["spec"]["base_version_id"] == versions[1]["id"]
    assert "第一款加口袋" not in [c["text"] for c in second["spec"]["constraints"]]
    assert "第二款改袖口" in [c["text"] for c in second["spec"]["constraints"]]
    provider = FakeProvider()
    revision_task = task(env, second["id"], provider, key="single-edit", max_cost_fen=11, max_image_calls=1)
    run_task(revision_task, provider)
    status, payload = read_task(revision_task)
    assert status == "awaiting_review", payload
    assert payload["image_calls"] == 1 and payload["reasoning_calls"] == 1
    assert [c[0] for c in provider.calls] == ["render", "compare"]
    child = service.workspace(env)["versions"][-1]
    assert child["parent_version_id"] == versions[1]["id"]
