"""
OKR 管理平台 —— Pydantic 请求/响应模型
"""
from typing import Optional, List, Literal
from pydantic import BaseModel, Field


# ── 认证 ────────────────────────────────────────────────────────────────

class SendSmsRequest(BaseModel):
    phone: str


class PhoneVerifyRequest(BaseModel):
    phone: str
    code: str


class RegisterRequest(BaseModel):
    phone: str
    code: str
    password: str
    name: str
    company_id: int
    department_id: int


class LoginRequest(BaseModel):
    phone: str
    password: str


class RoleSwitchRequest(BaseModel):
    role: Literal["dept_head", "superAdmin"]


class ChangePasswordRequest(BaseModel):
    old_password: str
    new_password: str


class ProfileUpdate(BaseModel):
    name: Optional[str] = None


# ── 公司 ────────────────────────────────────────────────────────────────

class CompanyCreate(BaseModel):
    name: str
    okr_period_type: str = "monthly"
    head_user_id: Optional[int] = None


class CompanyUpdate(BaseModel):
    name: Optional[str] = None
    status: Optional[str] = None
    okr_period_type: Optional[str] = None
    head_user_id: Optional[int] = None


# ── 部门 ────────────────────────────────────────────────────────────────

class DepartmentCreate(BaseModel):
    name: str
    company_id: Optional[int] = None
    head_user_id: Optional[int] = None


class DepartmentUpdate(BaseModel):
    name: Optional[str] = None
    head_user_id: Optional[int] = None


# ── 用户管理 ────────────────────────────────────────────────────────────

class UserCreate(BaseModel):
    phone: str
    password: str
    name: str
    role: str = "user"
    company_id: Optional[int] = None
    department_id: Optional[int] = None


class UserUpdate(BaseModel):
    name: Optional[str] = None
    role: Optional[Literal["user", "dept_head", "admin", "superAdmin"]] = None
    department_id: Optional[int] = None
    company_id: Optional[int] = None
    status: Optional[Literal["pending", "active", "disabled"]] = None


# ── OKR 目标 ────────────────────────────────────────────────────────────

class ObjectiveCreate(BaseModel):
    title: str
    description: str = ""
    type: str  # company / department / personal
    company_id: Optional[int] = None  # superAdmin 创建时指定公司
    department_id: Optional[int] = None
    aligned_objective_id: Optional[int] = None
    is_public: int = 1  # 1=公开 0=不公开（部门OKR在公司视图是否可见）
    weight: float = 1.0  # 权重（自由权重，默认1）
    period: str  # e.g. "2026-Q2"
    owner_id: Optional[int] = None  # 公司负责人代员工创建个人 OKR 时指定


class ObjectiveUpdate(BaseModel):
    title: Optional[str] = None
    description: Optional[str] = None
    status: Optional[str] = None
    progress: Optional[float] = None
    aligned_objective_id: Optional[int] = None
    is_public: Optional[int] = None
    weight: Optional[float] = None


class ObjectiveCopyFromPeriod(BaseModel):
    source_objective_ids: list
    target_period: str
    department_id: Optional[int] = None


# ── Key Results ─────────────────────────────────────────────────────────

class KeyResultCreate(BaseModel):
    title: str
    target_value: float = 100.0
    current_value: float = 0.0
    unit: str = "%"
    weight: float = 1.0


class KeyResultUpdate(BaseModel):
    title: Optional[str] = None
    target_value: Optional[float] = None
    current_value: Optional[float] = None
    unit: Optional[str] = None
    weight: Optional[float] = None


# ── 月度看板互动 ──────────────────────────────────────────────────────────────

class EmployeeBoardNoteUpdate(BaseModel):
    period: str
    note: str = Field(default="", max_length=500)


# ── 审核 ────────────────────────────────────────────────────────────────

class ReviewAction(BaseModel):
    status: Literal["approved", "rejected"]
    comment: str = Field(default="", max_length=500)


# ── 系统配置 ────────────────────────────────────────────────────────────

class ConfigItem(BaseModel):
    key: str
    value: str
    description: str = ""


class ConfigBatchUpdate(BaseModel):
    items: List[ConfigItem]


# ── 月度目标评估 ──────────────────────────────────────────────────────────────

class MonthlyReviewCreate(BaseModel):
    """创建/获取某目标某月的考核记录（幂等）"""
    objective_id: int
    year_month: str  # 格式 "YYYY-MM"，如 "2026-04"


class SelfReviewSubmit(BaseModel):
    """员工提交自评"""
    self_score: int      # 0-100
    self_comment: str = ""


class ManagerReviewSubmit(BaseModel):
    """主管提交评分"""
    manager_score: int   # 0-100
    manager_comment: str = ""


class MemberEvaluationSubmit(BaseModel):
    """One supervisor assessment per member/month, independent of individual O's."""
    year_month: str = Field(pattern=r"^[0-9]{4}-(?:0[1-9]|1[0-2])$")
    satisfaction_score: int = Field(strict=True, ge=0, le=100)
    initiative_score: int = Field(strict=True, ge=0, le=100)
    redline_factor: int = Field(strict=True, ge=0, le=1)
    redline_reason: str = Field(default="", max_length=2000)
    comment: str = Field(default="", max_length=2000)
    expected_revision: int = Field(default=0, strict=True, ge=0)


# These model names are already public through the platform's OpenAPI schema.
# Retain the original qualified names when another module defines e.g. UserCreate.
# The legacy okr_models import remains an alias to this module for the same reason.
for _model in tuple(globals().values()):
    if isinstance(_model, type) and issubclass(_model, BaseModel) and _model.__module__ == __name__:
        _model.__module__ = "okr_models"
        _model.model_rebuild(force=True)
del _model
