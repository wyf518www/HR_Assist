from typing import Literal
from urllib.parse import urlsplit
import re

from pydantic import BaseModel, ConfigDict, Field, model_validator


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
    description: str = Field(min_length=1, max_length=3000)
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
    llm_approved: bool = False
    approval_note: str = Field(default="", max_length=2000)
    max_calls_per_run: int = Field(default=30, ge=1, le=200)
    schedule_enabled: bool = False
    schedule_time: str = "09:00"
    policy_confirmed: bool = False
    master_bypass: bool = True
    schools: list[str] = Field(default_factory=list, max_length=600)
    school_source: str = Field(default="", max_length=2000)

    @model_validator(mode="after")
    def check(self):
        if not re.fullmatch(r"(?:[01]\d|2[0-3]):[0-5]\d", self.schedule_time):
            raise ValueError("计划时间格式必须为 HH:MM")
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


class ClassifyInput(Input):
    role_id: str


class DocumentReview(Input):
    text: str = Field(min_length=10,max_length=100000)
    note: str = Field(min_length=2,max_length=1000)
    confirmed: Literal[True]


class RunInput(Input):
    mode: Literal["mail", "pending"] = "pending"
    idempotency_key: str = Field(min_length=8, max_length=100)


class Evidence(Input):
    span_id: str
    quote: str = Field(min_length=1, max_length=5000)


class Judgment(Input):
    criterion_id: str
    verdict: Literal["met", "partial", "not_met", "unknown"]
    evidence: list[Evidence] = Field(default_factory=list, max_length=12)
    reason: str = Field(max_length=1500)
    question: str = Field(default="", max_length=1500)


class Fact(Input):
    field: Literal["company", "education_level", "admission", "institution", "experience"]
    value: str = Field(max_length=200)
    evidence: Evidence


class ModelResult(Input):
    judgments: list[Judgment] = Field(max_length=60)
    facts: list[Fact] = Field(default_factory=list, max_length=15)
