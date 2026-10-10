from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, StrictBool, model_validator


class Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


class Constraint(Strict):
    id: str = Field(pattern=r"^c_[a-zA-Z0-9_-]{1,48}$")
    kind: Literal["must_keep", "may_change", "forbidden", "preference"]
    text: str = Field(min_length=1, max_length=2 * 1024 * 1024)
    region: str = Field(default="整体", max_length=100)
    verification: Literal["visual", "physical"] = "visual"


class Reference(Strict):
    asset_id: str
    role: Literal["structure", "fabric", "color", "detail"]
    region: str = Field(default="整体", min_length=1, max_length=100)
    instruction: str = Field(default="", max_length=500)


class SpecIn(Strict):
    expected_spec_id: str | None = None
    intent: str = Field(min_length=1, max_length=10000)
    references: list[Reference] = Field(default_factory=list, max_length=6)
    constraints: list[Constraint] = Field(default_factory=list)
    deliverables: str = Field(default="正面完整服装效果图", min_length=1, max_length=500)
    base_version_id: str | None = None
    edit_region: str = Field(default="", max_length=200)
    conflicts: list[str] = Field(default_factory=list, max_length=10)
    assumptions: list[str] = Field(default_factory=list, max_length=10)

    @model_validator(mode="after")
    def unique_ids(self):
        ids = [c.id for c in self.constraints]
        if len(ids) != len(set(ids)):
            raise ValueError("约束编号不能重复")
        refs = [r.asset_id for r in self.references]
        if len(refs) != len(set(refs)):
            raise ValueError("同一素材只能明确一种主要用途")
        if self.base_version_id and not self.edit_region:
            raise ValueError("修改已有版本时需说明改动区域")
        return self


class TaskIn(Strict):
    spec_id: str
    mode: Literal["understand", "design", "style"]
    idempotency_key: str = Field(min_length=8, max_length=100)
    authorized: Literal[True]
    # Existing clients may still send a task ceiling; new design tasks omit it.
    max_cost_fen: int | None = Field(default=None, ge=1, le=20000)
    design_count: int = Field(default=1, ge=1, le=6)
    max_image_calls: int = Field(default=3, ge=1, le=6)
    max_reasoning_calls: int = Field(default=8, ge=1, le=8)
    style_plan_id: str | None = None
    style_direction_ids: list[str] = Field(default_factory=list, max_length=6)

    @model_validator(mode="after")
    def design_count_limits(self):
        if self.design_count > 1 and self.mode not in {"design", "style"}:
            raise ValueError("只有风格规划或生成设计任务可以选择多款")
        if self.mode == "style" and self.design_count > 5:
            raise ValueError("一次最多规划五个风格方向")
        if self.mode == "design" and self.design_count > self.max_image_calls:
            raise ValueError("生成次数上限少于所选款数")
        if self.style_plan_id and (self.mode != "design" or len(self.style_direction_ids) != self.design_count):
            raise ValueError("规划方向必须逐一对应生成款数")
        if self.style_direction_ids and not self.style_plan_id:
            raise ValueError("缺少风格规划版本")
        return self


class RecheckIn(Strict):
    authorized: Literal[True]
    idempotency_key: str = Field(min_length=8, max_length=100)


class StyleDirectionDraft(Strict):
    name: str = Field(min_length=2, max_length=60)
    theme: str = Field(min_length=3, max_length=300)
    rationale: str = Field(min_length=5, max_length=500)
    explore: str = Field(min_length=3, max_length=300)
    color_story: str = Field(min_length=1, max_length=300)
    structure: str = Field(min_length=3, max_length=400)
    material_story: str = Field(min_length=1, max_length=300)


class StylePlanOutput(Strict):
    directions: list[StyleDirectionDraft] = Field(min_length=1, max_length=5)


class StyleDirectionEdit(StyleDirectionDraft):
    id: str = Field(pattern=r"^d_[a-f0-9]{12}$")
    selected: bool = True


class StylePlanEdit(Strict):
    expected_plan_id: str
    directions: list[StyleDirectionEdit] = Field(min_length=1, max_length=5)


class FeedbackIn(Strict):
    text: str = Field(min_length=1, max_length=2000)


class RevisionIn(FeedbackIn):
    edit_region: str = Field(min_length=1, max_length=200)
    expected_spec_id: str


class SamplingSheetIn(Strict):
    expected_sheet_id: str | None = None
    material: str = Field(default="", max_length=1200)
    color: str = Field(default="", max_length=1200)
    measurements: str = Field(default="", max_length=3000)
    graphic_placement: str = Field(default="", max_length=1200)
    graphic_dimensions: str = Field(default="", max_length=1200)
    construction: str = Field(default="", max_length=3000)
    notes: str = Field(default="", max_length=3000)


class Check(Strict):
    constraint_id: str
    status: Literal["pass", "deviation", "unknown"]
    candidate_region: str = Field(min_length=1, max_length=200)
    evidence: str = Field(min_length=1, max_length=1000)
    reference_ids: list[str] = Field(default_factory=list, max_length=7)


class Finding(Strict):
    status: Literal["pass", "deviation", "unknown"]
    evidence: str = Field(min_length=1, max_length=1500)


class Review(Strict):
    goal: Finding
    preservation: Finding
    checks: list[Check]
    summary: str = Field(min_length=1, max_length=1000)


class Observation(Strict):
    asset_id: str
    observable_features: list[str] = Field(max_length=20)
    inferences: list[str] = Field(default_factory=list, max_length=10)
    unknowns: list[str] = Field(default_factory=list, max_length=10)


class Inspection(Strict):
    observations: list[Observation] = Field(max_length=6)
    conflicts: list[str] = Field(default_factory=list, max_length=10)


class Action(Strict):
    action: Literal[
        "inspect_assets", "propose_spec", "generate_design", "edit_design", "compare_design", "ask", "finish", "reply"
    ]
    summary: str = Field(min_length=1, max_length=500)
    answer: str = Field(default="", max_length=4000)
    questions: list[str] = Field(default_factory=list, max_length=3)
    proposed_spec: SpecIn | None = None

    @model_validator(mode="after")
    def arguments(self):
        if self.action == "reply" and not self.answer:
            raise ValueError("缺少回复内容")
        if self.action == "ask" and not self.questions:
            raise ValueError("需要具体问题")
        if self.action == "propose_spec" and self.proposed_spec is None:
            raise ValueError("缺少要求单")
        if self.action != "propose_spec" and self.proposed_spec is not None:
            raise ValueError("该工具不接受要求单")
        return self


class CorrectionIn(Strict):
    constraint_id: str
    status: Literal["pass", "deviation", "unknown"]
    evidence: str = Field(min_length=10, max_length=1500)


class SelectedContext(Strict):
    version_id: str | None = Field(default=None, min_length=1, max_length=100)
    asset_id: str | None = Field(default=None, min_length=1, max_length=100)

    @model_validator(mode="after")
    def has_object(self):
        if not self.version_id and not self.asset_id:
            raise ValueError("请选择图片或版本")
        return self


class ChatIn(Strict):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=False)
    text: str = Field(min_length=1, max_length=4000)
    references: list[Reference] = Field(default_factory=list, max_length=6)
    expected_spec_id: str | None = None
    expected_prompt_id: str | None = Field(default=None, min_length=1, max_length=100)
    selected_context: SelectedContext | None = None
    idempotency_key: str = Field(min_length=8, max_length=100)
    authorized: StrictBool = False
    max_cost_fen: int = Field(default=500, ge=1, le=20000)
    intent: Literal["auto", "discuss", "generate_image"] = "discuss"
    output_kind: Literal["effect_image", "design_draft"] | None = None

    @model_validator(mode="after")
    def nonblank_text(self):
        if not self.text.strip():
            raise ValueError("消息不可为空白")
        return self
