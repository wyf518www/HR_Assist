from typing import Literal
from urllib.parse import urlsplit
import re

from pydantic import BaseModel, ConfigDict, Field, model_validator
from .semantic import SemanticReference


class Input(BaseModel):
    model_config = ConfigDict(extra="forbid")


class Login(Input):
    username: str = Field(max_length=100)
    password: str = Field(max_length=300)


class JDFields(Input):
    education: str = Field(default="", max_length=2000)
    experience: str = Field(default="", max_length=2000)
    location: str = Field(default="", max_length=2000)
    certificates: str = Field(default="", max_length=2000)
    availability: str = Field(default="", max_length=2000)
    responsibilities: str = Field(default="", max_length=5000)
    skills: str = Field(default="", max_length=5000)


class Criterion(Input):
    id: str = Field(pattern=r"^[a-zA-Z0-9_-]{1,50}$")
    dimension: Literal["hard", "skills", "experience", "achievements", "stability"]
    description: str = Field(min_length=1, max_length=6000)
    points: int = Field(ge=1, le=30)
    hard: bool = False
    source: Literal["jd", "policy"] = "jd"


class RoleInput(Input):
    name: str = Field(min_length=1, max_length=60)
    is_sales: bool = False
    jd_text: str = Field(default="", max_length=100_000)
    source_file_id: str | None = None
    fields: JDFields = Field(default_factory=JDFields)
    criteria: list[Criterion] = Field(default_factory=list, max_length=60)
    approved: bool = False

    @model_validator(mode="after")
    def validate_role(self):
        if self.name.strip() != self.name or re.search(r'[<>:"/\\|?*\x00-\x1f]', self.name) or self.name in {".", ".."}:
            raise ValueError("岗位名称不能包含路径或非法字符")
        if self.criteria:
            totals = {key: sum(c.points for c in self.criteria if c.dimension == key) for key in ("hard", "skills", "experience", "achievements", "stability")}
            if totals != {"hard":20,"skills":30,"experience":30,"achievements":10,"stability":10}:
                raise ValueError("评分细则的五维总分必须为 20/30/30/10/10")
            if len({c.id for c in self.criteria}) != len(self.criteria):
                raise ValueError("评分条目 ID 必须唯一")
        return self


class SettingsInput(Input):
    mail_account: str = Field(default="", max_length=200)
    mail_host: str = Field(default="imap.qq.com", max_length=200)
    mail_port: int = Field(default=993, ge=1, le=65535)
    mail_password: str | None = Field(default=None, max_length=500)
    mail_enabled: bool = False
    subject_mode: Literal["all", "any"] = "all"
    llm_endpoint: str = Field(default="", max_length=500)
    llm_model: str = Field(default="", max_length=200)
    llm_api_key: str | None = Field(default=None, max_length=2000)
    llm_timeout_seconds: int | None = Field(default=None, ge=30, le=600)
    llm_reasoning_effort: Literal["default", "low", "high", "max"] | None = None
    llm_max_tokens: int | None = Field(default=None, ge=1024, le=131072, strict=True)
    llm_approved: bool = False
    approval_note: str = Field(default="", max_length=2000)
    max_calls_per_run: int = Field(default=30, ge=1, le=1000)
    schedule_enabled: bool = False
    schedule_time: str = "09:00"
    diagnostics_enabled: bool | None = None
    diagnostics_retention_days: int | None = Field(default=None, ge=1, le=30)
    diagnostics_cleanup_time: str | None = None
    policy_confirmed: bool = False
    master_bypass: bool = True
    schools: list[str] = Field(default_factory=list, max_length=600)
    school_source: str = Field(default="", max_length=2000)

    @model_validator(mode="after")
    def check(self):
        if "llm_reasoning_effort" in self.model_fields_set and self.llm_reasoning_effort is None:
            raise ValueError("思考强度不能为空；恢复供应商默认请选 default")
        if not re.fullmatch(r"(?:[01]\d|2[0-3]):[0-5]\d", self.schedule_time):
            raise ValueError("计划时间格式必须为 HH:MM")
        if self.diagnostics_cleanup_time is not None and not re.fullmatch(r"(?:[01]\d|2[0-3]):[0-5]\d", self.diagnostics_cleanup_time):
            raise ValueError("日志清理时间格式必须为 HH:MM")
        if self.llm_endpoint:
            parsed = urlsplit(self.llm_endpoint)
            if parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.query or parsed.fragment:
                raise ValueError("云模型地址必须为不含凭据、查询参数的 HTTPS 完整接口地址")
        if self.llm_approved and not (self.llm_endpoint and self.llm_model and self.approval_note.strip()):
            raise ValueError("启用云模型前需填写接口、模型及公司批准说明")
        if any(not school.strip() or len(school) > 150 for school in self.schools):
            raise ValueError("院校名称不能为空或过长")
        if self.schools and not self.school_source.strip():
            raise ValueError("院校名单需要注明已核验的来源")
        return self


class JDImportConfirm(Input):
    role: RoleInput
    reviewed: Literal[True]
    target_role_id: str | None = None
    expected_version: str | None = None


class JDTextAnalysis(Input):
    text: str = Field(min_length=1, max_length=100_000)
    confirmed: Literal[True]


class ClassifyInput(Input):
    role_id: str


class RoleAction(Input):
    confirmed: Literal[True]
    expected_version: str = Field(min_length=1, max_length=100)


class RoleStatusInput(RoleAction):
    active: bool


class DocumentReview(Input):
    text: str = Field(min_length=10,max_length=100000)
    note: str = Field(min_length=2,max_length=1000)
    confirmed: Literal[True]


class CandidateNameReview(Input):
    name: str = Field(min_length=1, max_length=80)
    expected_name: str = Field(max_length=500)
    confirmed: Literal[True]

    @model_validator(mode="after")
    def check_name(self):
        if self.name.strip() != self.name or self.name == "待确认" or re.search(r"[<>\x00-\x1f\x7f]", self.name):
            raise ValueError("请填写已对照原始 PDF 核验的姓名，不含首尾空格或控制字符")
        return self


class RunInput(Input):
    mode: Literal["mail", "pending", "identity"] = "pending"
    idempotency_key: str = Field(min_length=8, max_length=100)
    application_ids: list[str] = Field(default_factory=list,max_length=500)
    force_identity: bool = False


class IdentityCorrection(Input):
    application_id: str = Field(min_length=1,max_length=100)
    proposal_id: str = Field(min_length=1,max_length=100)
    name: str = Field(min_length=1,max_length=80)
    role_id: str = Field(min_length=1,max_length=100)

    @model_validator(mode='after')
    def validate_name(self):
        if self.name.strip()!=self.name or self.name in {'待确认','待识别'} or re.search(r'[<>\x00-\x1f\x7f]',self.name):
            raise ValueError('请填写经核对的候选人姓名或原文称呼')
        return self


class IdentityCorrections(Input):
    confirmed: Literal[True]
    items: list[IdentityCorrection] = Field(min_length=1,max_length=500)

    @model_validator(mode='after')
    def unique_applications(self):
        if len({i.application_id for i in self.items})!=len(self.items):
            raise ValueError('同一申请只能出现一次')
        return self


class FollowupInput(Input):
    expected_revision: int = Field(ge=0)
    changes: dict[str, str | None] = Field(min_length=1, max_length=5)

    @model_validator(mode="after")
    def check_followup(self):
        allowed={"推荐给用人部门日期","用人部门复筛结果","约面时间","面试状态","面试结果"}
        if not set(self.changes) <= allowed or any(v is not None and len(v)>40 for v in self.changes.values()):
            raise ValueError('仅支持编辑指定的五个跟进字段')
        return self


class FollowupBatchItem(Input):
    application_id: str = Field(min_length=1,max_length=100)
    expected_revision: int = Field(ge=0)


class FollowupBatchInput(Input):
    confirmed: Literal[True]
    items: list[FollowupBatchItem] = Field(min_length=1,max_length=500)
    fields: dict[str,str | None] = Field(min_length=1,max_length=7)

    @model_validator(mode='after')
    def check_batch(self):
        if len({item.application_id for item in self.items})!=len(self.items):
            raise ValueError('同一申请只能出现一次')
        free_text={'面试评价','当前状态'}
        allowed={'推荐给用人部门日期','用人部门复筛结果','约面时间','面试状态','面试结果',*free_text}
        if not set(self.fields)<=allowed:
            raise ValueError('仅支持编辑指定的七个人工跟进字段')
        for column,value in self.fields.items():
            if value is not None and len(value)>(20_000 if column in free_text else 40):
                raise ValueError('人工跟进字段过长')
        return self


class Evidence(Input):
    span_id: str
    quote: str = Field(min_length=1, max_length=5000)


class Judgment(Input):
    criterion_id: str
    verdict: Literal["met", "partial", "not_met", "unknown"]
    evidence: list[SemanticReference] = Field(default_factory=list, max_length=12)
    reason: str = Field(max_length=1500)
    question: str = Field(default="", max_length=1500)
    requirement_mode: Literal["required", "unrestricted"] = "required"


class Fact(Input):
    field: Literal["company", "education_level", "admission", "institution", "experience"]
    value: str = Field(max_length=200)
    evidence: SemanticReference


class EducationResult(Input):
    # All fields describe one education record, not a merge of unrelated schools
    # or degrees. Semantic interpretation is model-reviewed; policy is local.
    degree: Literal["bachelor", "master", "doctor", "other", "unknown"]
    completion: Literal["attained", "in_progress", "unknown"]
    admission: Literal["national_regular", "non_regular", "unknown"]
    institution_match: Literal["catalog", "not_in_catalog", "unknown"]
    catalog_id: str | None = Field(default=None, max_length=100)
    evidence: list[SemanticReference] = Field(default_factory=list, max_length=12)
    interpretation: str = Field(default="", max_length=1500)


class ModelResult(Input):
    judgments: list[Judgment] = Field(max_length=60)
    facts: list[Fact] = Field(default_factory=list, max_length=15)
    education: EducationResult | None = None
