"""
OKR 管理平台 —— API 路由模块
所有 /api/okr/* 端点 + 页面路由（多公司版本）
"""
import json
import hashlib
import os
import sqlite3
import string
import re
import secrets
from datetime import datetime, timedelta, timezone
from urllib.parse import quote

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import FileResponse, RedirectResponse

from .auth import (
    get_authenticated_user,
    get_current_user,
    require_role,
    hash_password,
    verify_password,
    create_token,
    get_available_roles,
    get_default_role,
)
from .db import get_db, dict_from_row, dicts_from_rows
from .evaluation import objective_score
from .progress import attach_key_results, calculate_kr_progress
from .evaluation_api import evaluation_router
from .evaluation_service import read_member_evaluations
from .models import (
    SendSmsRequest, PhoneVerifyRequest, RegisterRequest, LoginRequest, RoleSwitchRequest, ChangePasswordRequest, ProfileUpdate,
    CompanyCreate, CompanyUpdate,
    DepartmentCreate, DepartmentUpdate,
    UserCreate, UserUpdate,
    ObjectiveCreate, ObjectiveUpdate, ObjectiveCopyFromPeriod,
    KeyResultCreate, KeyResultUpdate,
    EmployeeBoardNoteUpdate,
    ReviewAction,
    ConfigBatchUpdate,
    MonthlyReviewCreate, SelfReviewSubmit, ManagerReviewSubmit,
)

from .sms import get_tencent_sms_config, send_tencent_verification_code
from .wecom import (
    build_wecom_auth_url,
    fetch_wecom_user,
    get_okr_wecom_config,
)
okr_router = APIRouter()
okr_router.include_router(evaluation_router)

from .paths import CONFIG_PATH, STATIC_DIR

_STATIC = str(STATIC_DIR)
_CFG_PATH = str(CONFIG_PATH)


_WECOM_STATE_COOKIE = "okr_wecom_state"
_SMS_PURPOSE = "wecom_phone_bind"
def _audit_log(db, user: dict, action: str, target_type: str, target_id, detail: str = "", ip: str = ""):
    """写入审计日志"""
    db.execute(
        "INSERT INTO audit_logs (user_id, user_name, action, target_type, target_id, detail, ip) "
        "VALUES (?, ?, ?, ?, ?, ?, ?)",
        (user["id"], user["name"], action, target_type, target_id, detail, ip),
    )


def _load_config():
    with open(_CFG_PATH, "r", encoding="utf-8") as f:
        return json.load(f)


def _normalize_phone(value: str) -> str:
    phone = re.sub(r"\s+", "", str(value or ""))
    if phone.startswith("+86"):
        phone = phone[3:]
    if not re.fullmatch(r"1\d{10}", phone):
        raise HTTPException(status_code=400, detail="请输入正确的11位手机号")
    return phone


def _sms_code_hash(phone: str, code: str) -> str:
    secret = str(_load_config().get("jwt_secret") or "")
    return hashlib.sha256(f"{phone}:{code}:{secret}".encode("utf-8")).hexdigest()


def _request_is_secure(request: Request) -> bool:
    forwarded = (request.headers.get("x-forwarded-proto") or "").split(",")[0].strip().lower()
    return forwarded == "https" or request.url.scheme == "https"


def _resolve_wecom_company(db, config) -> int:
    row = None
    if config.company_id is not None:
        row = db.execute(
            "SELECT id, status FROM companies WHERE id = ?",
            (config.company_id,),
        ).fetchone()
        if not row:
            raise HTTPException(status_code=500, detail="OKR 企业微信配置的 company_id 不存在")
    if row is None and config.corp_id:
        row = db.execute(
            "SELECT id, status FROM companies WHERE wecom_corp_id = ? LIMIT 1",
            (config.corp_id,),
        ).fetchone()
    if row is None and config.corp_name:
        row = db.execute(
            "SELECT id, status FROM companies WHERE name = ? OR name LIKE ? ORDER BY id LIMIT 1",
            (config.corp_name, f"%{config.corp_name}%"),
        ).fetchone()
    if row is None:
        cursor = db.execute(
            "INSERT INTO companies(name, wecom_corp_id, status) VALUES (?, ?, 'active')",
            (config.corp_name or "企业微信组织", config.corp_id or None),
        )
        return int(cursor.lastrowid)
    if row["status"] != "active":
        raise HTTPException(status_code=403, detail="企业微信所属 OKR 公司已停用")
    if config.corp_id:
        db.execute(
            "UPDATE companies SET wecom_corp_id = COALESCE(wecom_corp_id, ?), updated_at = datetime('now') WHERE id = ?",
            (config.corp_id, row["id"]),
        )
    return int(row["id"])


def _sync_wecom_departments(db, company_id: int, details: list[dict]) -> dict[int, int]:
    mapping: dict[int, int] = {}
    for detail in details:
        external_id = detail.get("id")
        if not str(external_id).isdigit():
            continue
        external_id = int(external_id)
        # WeCom department 1 is the enterprise root, not an OKR business department.
        # Persisting it created a duplicate company-like department on every login.
        if external_id == 1 or detail.get("parentid") == 0:
            continue
        name = str(detail.get("name") or external_id).strip()
        row = db.execute(
            "SELECT id FROM departments WHERE company_id = ? AND wecom_department_id = ?",
            (company_id, external_id),
        ).fetchone()
        if not row:
            row = db.execute(
                "SELECT id FROM departments WHERE company_id = ? AND name = ?",
                (company_id, name),
            ).fetchone()
        if row:
            department_id = int(row["id"])
            db.execute(
                "UPDATE departments SET name = ?, wecom_department_id = ?, updated_at = datetime('now') WHERE id = ?",
                (name, external_id, department_id),
            )
        else:
            cursor = db.execute(
                "INSERT INTO departments(name, company_id, wecom_department_id) VALUES (?, ?, ?)",
                (name, company_id, external_id),
            )
            department_id = int(cursor.lastrowid)
        mapping[external_id] = department_id
    return mapping


def _wecom_mobile(user: dict) -> str:
    raw = str(user.get("mobile") or "").strip()
    try:
        return _normalize_phone(raw) if raw else ""
    except HTTPException:
        return ""


def _upsert_wecom_account(wecom_user: dict) -> dict:
    config = get_okr_wecom_config()
    userid = str(wecom_user.get("userid") or wecom_user.get("UserId") or "").strip()
    if not userid:
        raise HTTPException(status_code=502, detail="企业微信未返回 UserId")
    details = list(wecom_user.get("department_details") or [])
    leader = any(bool(item.get("is_leader")) for item in details)
    mobile = _wecom_mobile(wecom_user)
    with get_db() as db:
        company_id = _resolve_wecom_company(db, config)
        department_mapping = _sync_wecom_departments(db, company_id, details)
        main_external_id = wecom_user.get("main_department")
        if main_external_id not in department_mapping:
            main_external_id = next(
                (item.get("id") for item in details if item.get("id") in department_mapping),
                None,
            )
        department_id = department_mapping.get(main_external_id)
        existing = db.execute(
            "SELECT * FROM users WHERE wecom_userid = ? LIMIT 1",
            (userid,),
        ).fetchone()
        if not existing and mobile:
            existing = db.execute("SELECT * FROM users WHERE phone = ? LIMIT 1", (mobile,)).fetchone()

        if existing and existing["wecom_userid"] and existing["wecom_userid"] != userid:
            raise HTTPException(status_code=409, detail="企业微信手机号已绑定其他企业微信账号")
        first_wecom_link = bool(existing and not existing["wecom_userid"])

        if existing and existing["role"] == "superAdmin" and not bool(existing["wecom_role_managed"]):
            role = "superAdmin"
            role_managed = 0
        elif leader:
            role = config.leader_role
            role_managed = 1
        elif existing and bool(existing["wecom_role_managed"]):
            role = "user"
            role_managed = 1
        elif existing:
            role = existing["role"]
            role_managed = 0
        else:
            role = "user"
            role_managed = 1

        name = str(wecom_user.get("name") or userid).strip()
        if existing:
            phone = existing["phone"]
            if not bool(existing["phone_verified"]) and mobile:
                conflict = db.execute(
                    "SELECT id FROM users WHERE phone = ? AND id != ?",
                    (mobile, existing["id"]),
                ).fetchone()
                if not conflict:
                    phone = mobile
            db.execute(
                "UPDATE users SET phone = ?, name = ?, role = ?, company_id = ?, department_id = ?, "
                "wecom_userid = ?, phone_verified = ?, wecom_role_managed = ?, status = 'active', "
                "updated_at = datetime('now') "
                "WHERE id = ?",
                (phone, name, role, company_id, department_id, userid,
                 0 if first_wecom_link else existing["phone_verified"], role_managed, existing["id"]),
            )
            user_id = int(existing["id"])
        else:
            phone = mobile or f"wecom:{config.corp_id}:{userid}"
            password_hash = hash_password(secrets.token_urlsafe(32))
            cursor = db.execute(
                "INSERT INTO users(phone, password_hash, name, role, company_id, department_id, status, "
                "wecom_userid, phone_verified, wecom_role_managed) "
                "VALUES (?, ?, ?, ?, ?, ?, 'active', ?, 0, ?)",
                (phone, password_hash, name, role, company_id, department_id, userid, role_managed),
            )
            user_id = int(cursor.lastrowid)
        if leader:
            for detail in details:
                external_id = detail.get("id")
                leader_department_id = department_mapping.get(external_id)
                if detail.get("is_leader") and leader_department_id:
                    db.execute("UPDATE departments SET head_user_id = ? WHERE id = ?", (user_id, leader_department_id))
        row = db.execute("SELECT * FROM users WHERE id = ?", (user_id,)).fetchone()
    return dict_from_row(row)


def _auth_user_payload(
    user: dict,
    active_role: str = None,
    available_roles: list[str] = None,
) -> dict:
    raw_phone = str(user.get("phone") or "")
    phone = raw_phone if re.fullmatch(r"1\d{10}", raw_phone) else ""
    base_role = user.get("base_role") or user["role"]
    roles = available_roles or get_available_roles(
        user["id"], base_role, user.get("department_id")
    )
    with get_db() as db:
        is_company_head = _is_company_head(db, user)
    role = active_role or get_default_role(roles)
    return {
        "id": user["id"],
        "phone": phone,
        "phone_verified": bool(user.get("phone_verified")),
        "name": user["name"],
        "role": role,
        "base_role": base_role,
        "available_roles": roles,
        "company_id": user.get("company_id"),
        "department_id": user.get("department_id"),
        "is_wecom": bool(user.get("wecom_userid")),
        "is_company_head": is_company_head,
    }


def _token_response(user: dict, active_role: str = None) -> dict:
    base_role = user.get("base_role") or user["role"]
    available_roles = get_available_roles(
        user["id"], base_role, user.get("department_id")
    )
    role = active_role or get_default_role(available_roles)
    if role not in available_roles:
        raise HTTPException(status_code=403, detail="\u5f53\u524d\u8d26\u53f7\u4e0d\u80fd\u5207\u6362\u5230\u8be5\u89d2\u8272")
    token = create_token(
        user["id"], role, user["name"], user.get("company_id"), base_role=base_role
    )
    return {
        "ok": True,
        "token": token,
        "user": _auth_user_payload(user, role, available_roles),
    }


# ── 多公司辅助函数 ─────────────────────────────────────────────────────

def _get_company_filter(user, param_company_id=None):
    """Returns company_id for filtering. None means no filter (superAdmin sees all)."""
    if user["role"] == "superAdmin":
        return param_company_id  # optional filter for superAdmin
    return user["company_id"]


def _check_company_access(user, target_company_id):
    """检查用户是否有权访问指定公司的数据。superAdmin 可访问所有。"""
    if user["role"] == "superAdmin":
        return
    if user["company_id"] != target_company_id:
        raise HTTPException(403, "无权访问其他公司的数据")


# ══════════════════════════════════════════════════════════════════════════
#  页面路由
# ══════════════════════════════════════════════════════════════════════════

def _annual_period(period: str | None) -> str | None:
    """Company-level OKRs are stored once per calendar year."""
    if period is None:
        return None
    match = re.fullmatch(r"(\d{4})(?:-(?:0[1-9]|1[0-2]))?", str(period))
    if not match:
        raise HTTPException(400, "周期格式不正确")
    return match.group(1)


def _require_month_period(period: str) -> str:
    if not re.fullmatch(r"\d{4}-(?:0[1-9]|1[0-2])", str(period or "")):
        raise HTTPException(400, "月度周期格式不正确")
    return period


def _previous_month(period: str) -> str:
    year, month = (int(part) for part in _require_month_period(period).split("-"))
    if month == 1:
        return f"{year - 1}-12"
    return f"{year}-{month - 1:02d}"


def _is_company_head(db, user: dict, company_id: int | None = None) -> bool:
    cid = company_id if company_id is not None else user.get("company_id")
    if cid is None:
        return False
    row = db.execute(
        "SELECT 1 FROM companies WHERE id = ? AND head_user_id = ? LIMIT 1",
        (cid, user["id"]),
    ).fetchone()
    return bool(row)


def _can_manage_company_goals(db, user: dict, company_id: int) -> bool:
    return (
        user["role"] in ("superAdmin", "admin")
        or (bool(user.get("is_company_head")) and user.get("company_id") == company_id)
        or _is_company_head(db, user, company_id)
    )


def _personal_view_user(db, user: dict, target_user_id: int | None = None) -> dict:
    """Resolve a personal-OKR owner while keeping company-head delegation company-scoped."""
    target_id = target_user_id if target_user_id is not None else user["id"]
    target = dict_from_row(db.execute(
        "SELECT u.id, u.name, u.company_id, u.department_id, u.status, "
        "d.name AS department_name "
        "FROM users u LEFT JOIN departments d ON d.id = u.department_id "
        "WHERE u.id = ?",
        (target_id,),
    ).fetchone())
    if not target or target["status"] != "active":
        raise HTTPException(404, "员工不存在或账号未启用")
    if target_id != user["id"]:
        if (
            target["company_id"] != user.get("company_id")
            or not _is_company_head(db, user, target["company_id"])
        ):
            raise HTTPException(403, "只有公司负责人可以查看或维护本公司其他员工的个人 OKR")
    return target


def _record_company_head_activation(db, objective_id: int, company_head_id: int) -> None:
    """Keep an audit trail without putting delegated changes into the pending queue."""
    db.execute(
        "UPDATE okr_reviews SET status = 'approved', reviewer_id = ?, "
        "comment = '公司负责人代维护，已直接生效', created_at = datetime('now') "
        "WHERE objective_id = ? AND status = 'pending'",
        (company_head_id, objective_id),
    )
    latest = db.execute(
        "SELECT status FROM okr_reviews WHERE objective_id = ? ORDER BY id DESC LIMIT 1",
        (objective_id,),
    ).fetchone()
    if not latest or latest["status"] != "approved":
        db.execute(
            "INSERT INTO okr_reviews(objective_id, reviewer_id, status, comment) "
            "VALUES (?, ?, 'approved', '公司负责人代维护，已直接生效')",
            (objective_id, company_head_id),
        )


@okr_router.get("/okr")
async def page_okr_index():
    return FileResponse(os.path.join(_STATIC, "index.html"))


@okr_router.get("/okr/login")
async def page_okr_login():
    return FileResponse(os.path.join(_STATIC, "login.html"))


@okr_router.get("/okr/admin")
async def page_okr_admin():
    return FileResponse(os.path.join(_STATIC, "admin.html"))


# ══════════════════════════════════════════════════════════════════════════
#  认证接口 /api/okr/auth
# ══════════════════════════════════════════════════════════════════════════

@okr_router.get("/api/okr/auth/wecom/options")
async def auth_wecom_options(request: Request):
    """返回不含密钥的企业微信登录可用状态。"""
    config = get_okr_wecom_config()
    return {
        "enabled": config.enabled,
        "configured": config.configured,
        "agent_id": config.agent_id if config.configured else "",
        "callback_url": config.callback_url or str(request.url_for("auth_wecom_callback")),
    }


@okr_router.get("/api/okr/auth/wecom/start")
async def auth_wecom_start(request: Request, mode: str = Query("auto")):
    """进入企业微信移动端授权或桌面端扫码登录。"""
    state = secrets.token_urlsafe(32)
    target = build_wecom_auth_url(request, state, mode)
    response = RedirectResponse(target, status_code=302)
    response.set_cookie(
        _WECOM_STATE_COOKIE,
        state,
        max_age=600,
        httponly=True,
        secure=_request_is_secure(request),
        samesite="lax",
        path="/api/okr/auth/wecom",
    )
    return response


@okr_router.get("/api/okr/auth/wecom/callback", name="auth_wecom_callback")
async def auth_wecom_callback(request: Request, code: str = Query(""), state: str = Query("")):
    """企业微信授权回调：同步组织身份、自动创建账号并签发 JWT。"""
    expected_state = request.cookies.get(_WECOM_STATE_COOKIE, "")
    if not expected_state or not state or not secrets.compare_digest(expected_state, state):
        raise HTTPException(status_code=400, detail="企业微信登录状态已失效，请重新扫码")
    wecom_user = await fetch_wecom_user(code)
    user = _upsert_wecom_account(wecom_user)
    payload = _token_response(user)
    response = RedirectResponse(
        f"/okr/login#wecom_token={quote(payload['token'], safe='')}",
        status_code=302,
    )
    response.delete_cookie(_WECOM_STATE_COOKIE, path="/api/okr/auth/wecom")
    return response


@okr_router.post("/api/okr/auth/phone/send")
@okr_router.post("/api/okr/auth/send-sms", include_in_schema=False)
async def auth_send_sms(
    req: SendSmsRequest,
    user: dict = Depends(get_authenticated_user),
):
    """为企业微信账号发送腾讯云短信验证码；发送失败时不落库。"""
    if not user.get("wecom_userid"):
        raise HTTPException(status_code=403, detail="仅企业微信授权账号可绑定手机号")
    if user.get("phone_verified"):
        raise HTTPException(status_code=409, detail="当前账号已完成手机号验证")
    phone = _normalize_phone(req.phone)
    now = datetime.now(timezone.utc)
    cutoff = (now - timedelta(seconds=60)).strftime("%Y-%m-%d %H:%M:%S")
    with get_db() as db:
        conflict = db.execute(
            "SELECT id, wecom_userid FROM users WHERE phone = ? AND id != ? LIMIT 1",
            (phone, user["id"]),
        ).fetchone()
        if conflict and conflict["wecom_userid"] and conflict["wecom_userid"] != user["wecom_userid"]:
            raise HTTPException(status_code=409, detail="该手机号已绑定其他企业微信账号")
        recent = db.execute(
            "SELECT id FROM sms_codes WHERE user_id = ? AND purpose = ? AND created_at > ? AND used = 0 LIMIT 1",
            (user["id"], _SMS_PURPOSE, cutoff),
        ).fetchone()
        if recent:
            raise HTTPException(status_code=429, detail="验证码发送过于频繁，请60秒后再试")

    code = "".join(secrets.choice(string.digits) for _ in range(6))
    sms_config = get_tencent_sms_config()
    request_id = await send_tencent_verification_code(phone, code)
    expires = (now + timedelta(minutes=sms_config.ttl_minutes)).strftime("%Y-%m-%d %H:%M:%S")
    with get_db() as db:
        db.execute(
            "UPDATE sms_codes SET used = 1 WHERE user_id = ? AND purpose = ? AND used = 0",
            (user["id"], _SMS_PURPOSE),
        )
        db.execute(
            "INSERT INTO sms_codes(phone, code, user_id, purpose, request_id, expires_at) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (phone, _sms_code_hash(phone, code), user["id"], _SMS_PURPOSE, request_id, expires),
        )
    return {"ok": True, "msg": "验证码已发送", "expires_in": sms_config.ttl_minutes * 60}


@okr_router.post("/api/okr/auth/phone/verify")
async def auth_verify_phone(
    req: PhoneVerifyRequest,
    user: dict = Depends(get_authenticated_user),
):
    """校验真实短信验证码并完成手机号绑定；可合并同手机号的历史 OKR 账号。"""
    if not user.get("wecom_userid"):
        raise HTTPException(status_code=403, detail="仅企业微信授权账号可绑定手机号")
    if user.get("phone_verified"):
        raise HTTPException(status_code=409, detail="当前账号已完成手机号验证")
    phone = _normalize_phone(req.phone)
    code = str(req.code or "").strip()
    if not re.fullmatch(r"\d{6}", code):
        raise HTTPException(status_code=400, detail="请输入6位短信验证码")
    now_str = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
    with get_db() as db:
        sms = db.execute(
            "SELECT id FROM sms_codes WHERE user_id = ? AND phone = ? AND purpose = ? "
            "AND code = ? AND used = 0 AND expires_at > ? ORDER BY id DESC LIMIT 1",
            (user["id"], phone, _SMS_PURPOSE, _sms_code_hash(phone, code), now_str),
        ).fetchone()
        if not sms:
            raise HTTPException(status_code=400, detail="验证码错误或已过期")
        current = db.execute("SELECT * FROM users WHERE id = ?", (user["id"],)).fetchone()
        if not current:
            raise HTTPException(status_code=401, detail="当前账号不存在")
        existing = db.execute(
            "SELECT * FROM users WHERE phone = ? AND id != ? LIMIT 1",
            (phone, user["id"]),
        ).fetchone()
        if existing and existing["wecom_userid"] and existing["wecom_userid"] != current["wecom_userid"]:
            raise HTTPException(status_code=409, detail="该手机号已绑定其他企业微信账号")

        db.execute("UPDATE sms_codes SET used = 1 WHERE id = ?", (sms["id"],))
        if existing:
            if existing["status"] == "disabled":
                raise HTTPException(status_code=403, detail="该手机号对应的历史账号已被禁用")
            target_role = existing["role"] if existing["role"] == "superAdmin" else current["role"]
            # 目标历史账号写入企业微信标识前，先释放临时账号持有的唯一值。
            # 两步操作处于同一事务中，任一步失败都会整体回滚。
            db.execute(
                "UPDATE users SET wecom_userid = NULL WHERE id = ?", (current["id"],)
            )
            db.execute(
                "UPDATE users SET name = ?, role = ?, company_id = ?, department_id = ?, "
                "wecom_userid = ?, phone_verified = 1, wecom_role_managed = ?, status = 'active', "
                "updated_at = datetime('now') WHERE id = ?",
                (
                    current["name"], target_role, current["company_id"], current["department_id"],
                    current["wecom_userid"], current["wecom_role_managed"], existing["id"],
                ),
            )
            db.execute(
                "UPDATE departments SET head_user_id = ? WHERE head_user_id = ?",
                (existing["id"], current["id"]),
            )
            db.execute("DELETE FROM users WHERE id = ?", (current["id"],))
            user_id = int(existing["id"])
        else:
            db.execute(
                "UPDATE users SET phone = ?, phone_verified = 1, updated_at = datetime('now') WHERE id = ?",
                (phone, current["id"]),
            )
            user_id = int(current["id"])
        row = db.execute("SELECT * FROM users WHERE id = ?", (user_id,)).fetchone()
    return _token_response(dict_from_row(row))


@okr_router.post("/api/okr/auth/register")
async def auth_register(req: RegisterRequest):
    """注册入口已取消，账号由企业微信授权自动创建。"""
    raise HTTPException(status_code=410, detail="注册功能已取消，请通过企业微信授权登录")


@okr_router.post("/api/okr/auth/login")
async def auth_login(req: LoginRequest):
    """仅保留超级管理员应急密码登录，普通成员必须使用企业微信。"""
    phone = _normalize_phone(req.phone)
    with get_db() as db:
        row = db.execute("SELECT * FROM users WHERE phone = ?", (phone,)).fetchone()
    if not row or row["role"] != "superAdmin" or not verify_password(req.password, row["password_hash"]):
        raise HTTPException(status_code=410, detail="普通账号密码登录已停用，请通过企业微信授权登录")
    if row["status"] != "active":
        raise HTTPException(status_code=403, detail="超级管理员账号已被禁用")
    return _token_response(dict_from_row(row))


@okr_router.get("/api/okr/auth/me")
async def auth_me(user: dict = Depends(get_authenticated_user)):
    """获取当前用户信息；未完成手机验证的企业微信账号也可调用。"""
    with get_db() as db:
        row = db.execute(
            "SELECT u.id, u.phone, u.name, u.role, u.company_id, u.department_id, u.status, "
            "COALESCE(u.phone_verified, 1) AS phone_verified, u.wecom_userid, "
            "d.name AS department_name, c.name AS company_name, "
            "COALESCE(c.okr_period_type, 'monthly') AS company_period_type, "
            "CASE WHEN c.head_user_id = u.id THEN 1 ELSE 0 END AS is_company_head "
            "FROM users u LEFT JOIN departments d ON u.department_id = d.id "
            "LEFT JOIN companies c ON u.company_id = c.id WHERE u.id = ?",
            (user["id"],),
        ).fetchone()
    if not row:
        raise HTTPException(status_code=404, detail="用户不存在")
    payload = dict_from_row(row)
    payload["base_role"] = user["base_role"]
    payload["role"] = user["role"]
    payload["available_roles"] = user["available_roles"]
    if not re.fullmatch(r"1\d{10}", str(payload.get("phone") or "")):
        payload["phone"] = ""
    payload["phone_verified"] = bool(payload.get("phone_verified"))
    payload["is_company_head"] = bool(payload.get("is_company_head"))
    payload["is_wecom"] = bool(payload.pop("wecom_userid", None))
    return payload




@okr_router.post("/api/okr/auth/role")
async def auth_switch_role(
    req: RoleSwitchRequest,
    user: dict = Depends(get_current_user),
):
    """Switch between department-head and super-admin active permissions."""
    if req.role not in user["available_roles"]:
        raise HTTPException(status_code=403, detail="\u5f53\u524d\u8d26\u53f7\u4e0d\u80fd\u5207\u6362\u5230\u8be5\u89d2\u8272")
    with get_db() as db:
        row = db.execute("SELECT * FROM users WHERE id = ?", (user["id"],)).fetchone()
    if not row:
        raise HTTPException(status_code=404, detail="\u7528\u6237\u4e0d\u5b58\u5728")
    return _token_response(dict_from_row(row), req.role)



@okr_router.put("/api/okr/auth/password")
async def auth_change_password(
    req: ChangePasswordRequest, user: dict = Depends(get_current_user)
):
    """修改密码"""
    with get_db() as db:
        row = db.execute(
            "SELECT password_hash FROM users WHERE id = ?", (user["id"],)
        ).fetchone()
        if not row or not verify_password(req.old_password, row["password_hash"]):
            raise HTTPException(400, "原密码错误")
        new_hash = hash_password(req.new_password)
        db.execute(
            "UPDATE users SET password_hash = ?, updated_at = datetime('now') WHERE id = ?",
            (new_hash, user["id"]),
        )
    return {"ok": True, "msg": "密码修改成功"}


@okr_router.put("/api/okr/auth/profile")
async def auth_update_profile(
    req: ProfileUpdate, user: dict = Depends(get_current_user)
):
    """修改个人信息（姓名）"""
    if not req.name or not req.name.strip():
        raise HTTPException(400, "姓名不能为空")
    with get_db() as db:
        db.execute(
            "UPDATE users SET name = ?, updated_at = datetime('now') WHERE id = ?",
            (req.name.strip(), user["id"]),
        )
    return {"ok": True, "msg": "个人信息更新成功", "name": req.name.strip()}


# ══════════════════════════════════════════════════════════════════════════
#  后台管理 - 公司 /api/okr/admin/companies
# ══════════════════════════════════════════════════════════════════════════

@okr_router.get("/api/okr/admin/companies")
async def admin_list_companies(user: dict = Depends(require_role("superAdmin"))):
    """获取所有公司列表（含部门数、用户数）"""
    with get_db() as db:
        rows = db.execute(
            "SELECT c.*, h.name AS head_name, "
            "(SELECT COUNT(*) FROM departments WHERE company_id = c.id) as dept_count, "
            "(SELECT COUNT(*) FROM users WHERE company_id = c.id) as user_count "
            "FROM companies c LEFT JOIN users h ON c.head_user_id = h.id ORDER BY c.id"
        ).fetchall()
    return dicts_from_rows(rows)


@okr_router.post("/api/okr/admin/companies")
async def admin_create_company(
    req: CompanyCreate, user: dict = Depends(require_role("superAdmin"))
):
    """创建公司"""
    with get_db() as db:
        existing = db.execute(
            "SELECT id FROM companies WHERE name = ?", (req.name.strip(),)
        ).fetchone()
        if existing:
            raise HTTPException(400, "公司名称已存在")
        if req.head_user_id is not None:
            head = db.execute(
                "SELECT id FROM users WHERE id = ? AND status = 'active'", (req.head_user_id,)
            ).fetchone()
            if not head:
                raise HTTPException(400, "公司负责人不存在或未启用")
        cur = db.execute(
            "INSERT INTO companies (name, okr_period_type, head_user_id) VALUES (?, 'monthly', ?)",
            (req.name.strip(), req.head_user_id),
        )
    return {"ok": True, "msg": "公司创建成功", "id": cur.lastrowid}


@okr_router.put("/api/okr/admin/companies/{company_id}")
async def admin_update_company(
    company_id: int, req: CompanyUpdate,
    user: dict = Depends(require_role("superAdmin")),
):
    """更新公司名称/状态"""
    with get_db() as db:
        company = db.execute(
            "SELECT * FROM companies WHERE id = ?", (company_id,)
        ).fetchone()
        if not company:
            raise HTTPException(404, "公司不存在")

        if req.name is not None:
            dup = db.execute(
                "SELECT id FROM companies WHERE name = ? AND id != ?",
                (req.name.strip(), company_id),
            ).fetchone()
            if dup:
                raise HTTPException(400, "公司名称已存在")
            db.execute(
                "UPDATE companies SET name = ?, updated_at = datetime('now') WHERE id = ?",
                (req.name.strip(), company_id),
            )

        if req.status is not None:
            if req.status not in ("active", "disabled"):
                raise HTTPException(400, "状态只能是 active 或 disabled")
            db.execute(
                "UPDATE companies SET status = ?, updated_at = datetime('now') WHERE id = ?",
                (req.status, company_id),
            )

        if req.okr_period_type is not None:
            if req.okr_period_type not in ("monthly", "quarterly"):
                raise HTTPException(400, "OKR周期只能是 monthly 或 quarterly")
            db.execute(
                "UPDATE companies SET okr_period_type = ?, updated_at = datetime('now') WHERE id = ?",
                (req.okr_period_type, company_id),
            )
        if req.head_user_id is not None:
            head = db.execute(
                "SELECT id FROM users WHERE id = ? AND company_id = ? AND status = 'active'",
                (req.head_user_id, company_id),
            ).fetchone()
            if not head:
                raise HTTPException(400, "公司负责人必须是本公司在职成员")
            db.execute(
                "UPDATE companies SET head_user_id = ?, updated_at = datetime('now') WHERE id = ?",
                (req.head_user_id, company_id),
            )
    return {"ok": True, "msg": "公司更新成功"}


@okr_router.delete("/api/okr/admin/companies/{company_id}")
async def admin_delete_company(
    company_id: int, user: dict = Depends(require_role("superAdmin"))
):
    """删除公司（仅当无部门和用户时）"""
    with get_db() as db:
        company = db.execute(
            "SELECT id FROM companies WHERE id = ?", (company_id,)
        ).fetchone()
        if not company:
            raise HTTPException(404, "公司不存在")

        dept_count = db.execute(
            "SELECT COUNT(*) as c FROM departments WHERE company_id = ?", (company_id,)
        ).fetchone()["c"]
        if dept_count > 0:
            raise HTTPException(400, f"该公司下还有 {dept_count} 个部门，请先删除")

        user_count = db.execute(
            "SELECT COUNT(*) as c FROM users WHERE company_id = ?", (company_id,)
        ).fetchone()["c"]
        if user_count > 0:
            raise HTTPException(400, f"该公司下还有 {user_count} 名用户，请先转移或删除")

        db.execute("DELETE FROM companies WHERE id = ?", (company_id,))
    return {"ok": True, "msg": "公司已删除"}


# ══════════════════════════════════════════════════════════════════════════
#  后台管理 - 部门 /api/okr/admin/departments
# ══════════════════════════════════════════════════════════════════════════

@okr_router.get("/api/okr/admin/departments")
async def admin_list_departments(
    company_id: int = Query(None),
    user: dict = Depends(require_role("superAdmin", "admin")),
):
    """部门列表。superAdmin 可查看所有/按公司筛选；admin 只看自己公司。"""
    cid = _get_company_filter(user, company_id)

    sql = (
        "SELECT d.*, u.name as head_name, c.name as company_name, "
        "(SELECT COUNT(*) FROM users WHERE department_id = d.id) as member_count "
        "FROM departments d "
        "LEFT JOIN users u ON d.head_user_id = u.id "
        "LEFT JOIN companies c ON d.company_id = c.id "
        "WHERE 1=1"
    )
    params = []
    if cid is not None:
        sql += " AND d.company_id = ?"
        params.append(cid)
    sql += " ORDER BY d.id"

    with get_db() as db:
        rows = db.execute(sql, params).fetchall()
    return dicts_from_rows(rows)


@okr_router.post("/api/okr/admin/departments")
async def admin_create_department(
    req: DepartmentCreate, user: dict = Depends(require_role("superAdmin", "admin"))
):
    """创建部门。superAdmin 用 req.company_id；admin 用自己的 company_id。"""
    if user["role"] == "superAdmin":
        cid = req.company_id
        if not cid:
            raise HTTPException(400, "superAdmin 创建部门必须指定 company_id")
    else:
        cid = user["company_id"]

    with get_db() as db:
        # 验证公司存在
        company = db.execute("SELECT id FROM companies WHERE id = ?", (cid,)).fetchone()
        if not company:
            raise HTTPException(400, "指定的公司不存在")

        # 检查同公司下部门名称唯一性
        existing = db.execute(
            "SELECT id FROM departments WHERE company_id = ? AND name = ?",
            (cid, req.name.strip()),
        ).fetchone()
        if existing:
            raise HTTPException(400, "该公司下部门名称已存在")

        cur = db.execute(
            "INSERT INTO departments (name, company_id, head_user_id) VALUES (?, ?, ?)",
            (req.name.strip(), cid, req.head_user_id),
        )
        # 如果设置了部门负责人，更新用户角色
        if req.head_user_id:
            db.execute(
                "UPDATE users SET role = 'dept_head', department_id = ?, updated_at = datetime('now') "
                "WHERE id = ?",
                (cur.lastrowid, req.head_user_id),
            )
    return {"ok": True, "msg": "部门创建成功", "id": cur.lastrowid}


@okr_router.put("/api/okr/admin/departments/{dept_id}")
async def admin_update_department(
    dept_id: int, req: DepartmentUpdate,
    user: dict = Depends(require_role("superAdmin", "admin"))
):
    with get_db() as db:
        dept = db.execute("SELECT * FROM departments WHERE id = ?", (dept_id,)).fetchone()
        if not dept:
            raise HTTPException(404, "部门不存在")

        # 非 superAdmin 检查部门所属公司
        if user["role"] != "superAdmin":
            if dept["company_id"] != user["company_id"]:
                raise HTTPException(403, "无权修改其他公司的部门")

        if req.name is not None:
            # 同公司下部门名称唯一性
            dup = db.execute(
                "SELECT id FROM departments WHERE company_id = ? AND name = ? AND id != ?",
                (dept["company_id"], req.name.strip(), dept_id),
            ).fetchone()
            if dup:
                raise HTTPException(400, "该公司下部门名称已存在")
            db.execute(
                "UPDATE departments SET name = ?, updated_at = datetime('now') WHERE id = ?",
                (req.name.strip(), dept_id),
            )

        if req.head_user_id is not None:
            # 取消旧负责人角色
            old_head = dept["head_user_id"]
            if old_head and old_head != req.head_user_id:
                db.execute(
                    "UPDATE users SET role = 'user', updated_at = datetime('now') WHERE id = ?",
                    (old_head,),
                )
            # 设置新负责人
            if req.head_user_id > 0:
                db.execute(
                    "UPDATE departments SET head_user_id = ?, updated_at = datetime('now') WHERE id = ?",
                    (req.head_user_id, dept_id),
                )
                db.execute(
                    "UPDATE users SET role = 'dept_head', department_id = ?, updated_at = datetime('now') "
                    "WHERE id = ?",
                    (dept_id, req.head_user_id),
                )
            else:
                db.execute(
                    "UPDATE departments SET head_user_id = NULL, updated_at = datetime('now') WHERE id = ?",
                    (dept_id,),
                )
    return {"ok": True, "msg": "部门更新成功"}


@okr_router.delete("/api/okr/admin/departments/{dept_id}")
async def admin_delete_department(
    dept_id: int, user: dict = Depends(require_role("superAdmin", "admin"))
):
    with get_db() as db:
        dept = db.execute("SELECT * FROM departments WHERE id = ?", (dept_id,)).fetchone()
        if not dept:
            raise HTTPException(404, "部门不存在")

        # 非 superAdmin 检查公司归属
        if user["role"] != "superAdmin":
            if dept["company_id"] != user["company_id"]:
                raise HTTPException(403, "无权删除其他公司的部门")

        member_count = db.execute(
            "SELECT COUNT(*) as c FROM users WHERE department_id = ?", (dept_id,)
        ).fetchone()["c"]
        if member_count > 0:
            raise HTTPException(400, f"该部门下还有 {member_count} 名成员，请先转移或删除")
        db.execute("DELETE FROM departments WHERE id = ?", (dept_id,))
    return {"ok": True, "msg": "部门已删除"}


# ══════════════════════════════════════════════════════════════════════════
#  后台管理 - 用户 /api/okr/admin/users
# ══════════════════════════════════════════════════════════════════════════

@okr_router.get("/api/okr/admin/users")
async def admin_list_users(
    role: str = Query(None),
    department_id: int = Query(None),
    company_id: int = Query(None),
    status: str = Query(None),
    keyword: str = Query(None),
    user: dict = Depends(require_role("superAdmin", "admin")),
):
    """用户列表。superAdmin 可按 company_id 筛选；admin 只看自己公司。"""
    cid = _get_company_filter(user, company_id)

    sql = (
        "SELECT u.id, u.phone, u.name, u.role, u.company_id, u.department_id, u.status, "
        "u.created_at, d.name as department_name, c.name as company_name "
        "FROM users u "
        "LEFT JOIN departments d ON u.department_id = d.id "
        "LEFT JOIN companies c ON u.company_id = c.id "
        "WHERE 1=1"
    )
    params = []
    if cid is not None:
        sql += " AND u.company_id = ?"
        params.append(cid)
    if role:
        sql += " AND u.role = ?"
        params.append(role)
    if department_id:
        sql += " AND u.department_id = ?"
        params.append(department_id)
    if status:
        sql += " AND u.status = ?"
        params.append(status)
    if keyword:
        sql += " AND (u.name LIKE ? OR u.phone LIKE ?)"
        params.extend([f"%{keyword}%", f"%{keyword}%"])
    sql += " ORDER BY u.id"

    with get_db() as db:
        rows = db.execute(sql, params).fetchall()
    return dicts_from_rows(rows)


@okr_router.post("/api/okr/admin/users")
async def admin_create_user(
    req: UserCreate, user: dict = Depends(require_role("superAdmin", "admin"))
):
    """创建用户。superAdmin 用 req.company_id；admin 用自己的 company_id。"""
    # 普通管理员不能创建 superAdmin
    if req.role == "superAdmin" and user["role"] != "superAdmin":
        raise HTTPException(403, "只有超级管理员才能创建超级管理员")
    # admin 只能由 superAdmin 创建
    if req.role == "admin" and user["role"] != "superAdmin":
        raise HTTPException(403, "只有超级管理员才能创建管理员")

    # 确定 company_id
    if user["role"] == "superAdmin":
        cid = req.company_id  # superAdmin 可以不设（创建另一个 superAdmin 时）
    else:
        cid = user["company_id"]

    with get_db() as db:
        existing = db.execute(
            "SELECT id FROM users WHERE phone = ?", (req.phone.strip(),)
        ).fetchone()
        if existing:
            raise HTTPException(400, "该手机号已注册")
        pwd_hash = hash_password(req.password)
        cur = db.execute(
            "INSERT INTO users (phone, password_hash, name, role, company_id, department_id, status) "
            "VALUES (?, ?, ?, ?, ?, ?, 'active')",
            (req.phone.strip(), pwd_hash, req.name.strip(), req.role, cid, req.department_id),
        )
    return {"ok": True, "msg": "用户创建成功", "id": cur.lastrowid}


@okr_router.put("/api/okr/admin/users/{uid}")
async def admin_update_user(
    uid: int, req: UserUpdate,
    user: dict = Depends(require_role("superAdmin", "admin")),
):
    with get_db() as db:
        target = db.execute("SELECT * FROM users WHERE id = ?", (uid,)).fetchone()
        if not target:
            raise HTTPException(404, "用户不存在")

        # 非 superAdmin 检查目标用户是否在同一公司
        if user["role"] != "superAdmin":
            if target["company_id"] != user["company_id"]:
                raise HTTPException(403, "无权修改其他公司的用户")

        # 不允许修改 superAdmin 的角色和状态（除非自己是 superAdmin 且不是修改自己）
        if target["role"] == "superAdmin" and user["role"] != "superAdmin":
            raise HTTPException(403, "不能修改超级管理员信息")
        if target["role"] == "superAdmin" and uid == user["id"]:
            if req.status == "disabled" or (req.role and req.role != "superAdmin"):
                raise HTTPException(400, "不能禁用或降级自己的超级管理员账号")

        updates = []
        params = []
        if req.name is not None:
            updates.append("name = ?")
            params.append(req.name.strip())
        if req.role is not None:
            if req.role in ("superAdmin", "admin") and user["role"] != "superAdmin":
                raise HTTPException(403, "只有超级管理员才能设置管理员角色")
            updates.append("role = ?")
            params.append(req.role)
        if req.department_id is not None:
            updates.append("department_id = ?")
            params.append(req.department_id if req.department_id > 0 else None)
        if req.status is not None:
            updates.append("status = ?")
            params.append(req.status)

        if updates:
            updates.append("updated_at = datetime('now')")
            params.append(uid)
            db.execute(
                f"UPDATE users SET {', '.join(updates)} WHERE id = ?", params
            )

        # 同步部门负责人：角色改为 dept_head 时自动设为部门 head_user_id
        if req.role is not None:
            dept_id = req.department_id if req.department_id is not None else target["department_id"]
            if req.role == "dept_head" and dept_id:
                db.execute(
                    "UPDATE departments SET head_user_id = ?, updated_at = datetime('now') WHERE id = ?",
                    (uid, dept_id),
                )
            elif req.role != "dept_head":
                # 角色不再是 dept_head，清除该用户在部门中的负责人身份
                db.execute(
                    "UPDATE departments SET head_user_id = NULL, updated_at = datetime('now') "
                    "WHERE head_user_id = ?",
                    (uid,),
                )
    return {"ok": True, "msg": "用户信息更新成功"}


@okr_router.delete("/api/okr/admin/users/{uid}")
async def admin_delete_user(
    uid: int, user: dict = Depends(require_role("superAdmin", "admin"))
):
    with get_db() as db:
        _validate_user_deletion(db, uid, user)
        try:
            db.execute("DELETE FROM users WHERE id = ?", (uid,))
        except sqlite3.IntegrityError as exc:
            raise HTTPException(
                status_code=409,
                detail="无法删除该用户：数据库中仍存在关联数据，请先转移业务数据或停用该用户。",
            ) from exc
    return {"ok": True, "msg": "用户已删除"}


# ══════════════════════════════════════════════════════════════════════════
#  后台管理 - 注册审核 /api/okr/admin/registrations
# ══════════════════════════════════════════════════════════════════════════

@okr_router.get("/api/okr/admin/registrations")
async def admin_list_registrations(
    company_id: int = Query(None),
    user: dict = Depends(require_role("superAdmin", "admin")),
):
    """获取待审核注册列表"""
    cid = _get_company_filter(user, company_id)
    with get_db() as db:
        sql = (
            "SELECT u.id, u.phone, u.name, u.company_id, u.department_id, u.created_at, "
            "c.name as company_name, d.name as department_name "
            "FROM users u "
            "LEFT JOIN companies c ON u.company_id = c.id "
            "LEFT JOIN departments d ON u.department_id = d.id "
            "WHERE u.status = 'pending'"
        )
        params = []
        if cid is not None:
            sql += " AND u.company_id = ?"
            params.append(cid)
        sql += " ORDER BY u.created_at DESC"
        rows = db.execute(sql, params).fetchall()
    return dicts_from_rows(rows)


@okr_router.put("/api/okr/admin/registrations/{uid}/approve")
async def admin_approve_registration(
    uid: int, user: dict = Depends(require_role("superAdmin", "admin")),
):
    """审核通过注册"""
    with get_db() as db:
        target = db.execute("SELECT * FROM users WHERE id = ?", (uid,)).fetchone()
        if not target:
            raise HTTPException(404, "用户不存在")
        if target["status"] != "pending":
            raise HTTPException(400, "该用户不在待审核状态")
        # admin 只能审核自己公司的
        if user["role"] != "superAdmin" and target["company_id"] != user["company_id"]:
            raise HTTPException(403, "只能审核本公司的注册申请")
        db.execute(
            "UPDATE users SET status = 'active', updated_at = datetime('now') WHERE id = ?",
            (uid,),
        )
    return {"ok": True, "msg": "已通过审核"}


@okr_router.put("/api/okr/admin/registrations/{uid}/reject")
async def admin_reject_registration(
    uid: int, user: dict = Depends(require_role("superAdmin", "admin")),
):
    """驳回注册申请（删除用户）"""
    with get_db() as db:
        target = db.execute("SELECT * FROM users WHERE id = ?", (uid,)).fetchone()
        if not target:
            raise HTTPException(404, "用户不存在")
        if target["status"] != "pending":
            raise HTTPException(400, "该用户不在待审核状态")
        if user["role"] != "superAdmin" and target["company_id"] != user["company_id"]:
            raise HTTPException(403, "只能审核本公司的注册申请")
        db.execute("DELETE FROM users WHERE id = ?", (uid,))
    return {"ok": True, "msg": "已驳回并删除"}


# ══════════════════════════════════════════════════════════════════════════
#  后台管理 - 系统配置 /api/okr/admin/config
# ══════════════════════════════════════════════════════════════════════════

@okr_router.get("/api/okr/admin/config")
async def admin_get_config(user: dict = Depends(require_role("superAdmin"))):
    with get_db() as db:
        rows = db.execute("SELECT * FROM system_config ORDER BY id").fetchall()
    return dicts_from_rows(rows)


@okr_router.put("/api/okr/admin/config")
async def admin_update_config(
    req: ConfigBatchUpdate, user: dict = Depends(require_role("superAdmin"))
):
    with get_db() as db:
        for item in req.items:
            db.execute(
                "INSERT INTO system_config (key, value, description, updated_at) "
                "VALUES (?, ?, ?, datetime('now')) "
                "ON CONFLICT(key) DO UPDATE SET value = ?, description = ?, updated_at = datetime('now')",
                (item.key, item.value, item.description, item.value, item.description),
            )
    return {"ok": True, "msg": "配置更新成功"}


# ══════════════════════════════════════════════════════════════════════════
#  后台管理 - 数据库管理 /api/okr/admin/db
# ══════════════════════════════════════════════════════════════════════════

_ALLOWED_TABLES = {
    "companies", "departments", "users", "okr_objectives", "okr_key_results",
    "okr_reviews", "okr_monthly_reviews", "system_config", "sms_codes",
}


_DELETE_REFERENCE_LABELS = {
    ("departments", "head_user_id"): "\u8d1f\u8d23\u90e8\u95e8",
    ("okr_objectives", "owner_id"): "OKR \u76ee\u6807",
    ("okr_reviews", "reviewer_id"): "OKR \u5ba1\u6838\u8bb0\u5f55",
    ("okr_monthly_reviews", "manager_id"): "\u6708\u5ea6\u8003\u6838\u8bb0\u5f55",
    ("audit_logs", "user_id"): "\u64cd\u4f5c\u65e5\u5fd7",
}


def _quote_sqlite_identifier(value: str) -> str:
    """Quote an identifier obtained from SQLite's own schema metadata."""
    return '"' + value.replace('"', '""') + '"'


def _blocking_delete_references(db, target_table: str, row_id: int) -> list[dict]:
    """Return non-cascading foreign-key references that would block deletion."""
    references = []
    tables = db.execute(
        "SELECT name FROM sqlite_master "
        "WHERE type = 'table' AND name NOT LIKE 'sqlite_%'"
    ).fetchall()
    for table_row in tables:
        source_table = table_row["name"]
        quoted_table = _quote_sqlite_identifier(source_table)
        foreign_keys = db.execute(f"PRAGMA foreign_key_list({quoted_table})").fetchall()
        for foreign_key in foreign_keys:
            if foreign_key["table"] != target_table:
                continue
            on_delete = (foreign_key["on_delete"] or "NO ACTION").upper()
            if on_delete in {"CASCADE", "SET NULL", "SET DEFAULT"}:
                continue
            source_column = foreign_key["from"]
            quoted_column = _quote_sqlite_identifier(source_column)
            count = db.execute(
                f"SELECT COUNT(*) AS c FROM {quoted_table} WHERE {quoted_column} = ?",
                (row_id,),
            ).fetchone()["c"]
            if count:
                references.append({
                    "table": source_table,
                    "column": source_column,
                    "count": count,
                    "label": _DELETE_REFERENCE_LABELS.get(
                        (source_table, source_column), f"{source_table}.{source_column}"
                    ),
                })

    # departments.head_user_id is a historical logical reference without an FK.
    if target_table == "users":
        department_count = db.execute(
            "SELECT COUNT(*) AS c FROM departments WHERE head_user_id = ?", (row_id,)
        ).fetchone()["c"]
        if department_count:
            references.append({
                "table": "departments",
                "column": "head_user_id",
                "count": department_count,
                "label": _DELETE_REFERENCE_LABELS[("departments", "head_user_id")],
            })
    return references


def _raise_delete_conflict(table: str, references: list[dict]) -> None:
    if not references:
        return
    summary = "\u3001".join(
        f"{item['label']} {item['count']} \u6761" for item in references
    )
    if table == "users":
        raise HTTPException(
            status_code=409,
            detail=(
                f"\u65e0\u6cd5\u5220\u9664\u8be5\u7528\u6237\uff1a"
                f"\u4ecd\u6709\u5173\u8054\u6570\u636e\uff08{summary}\uff09\u3002"
                "\u8bf7\u5148\u5c06\u76f8\u5173\u4e1a\u52a1\u6570\u636e\u8f6c\u79fb"
                "\u7ed9\u5176\u4ed6\u7528\u6237\uff1b\u5982\u9700\u4fdd\u7559\u5386"
                "\u53f2\u8bb0\u5f55\uff0c\u8bf7\u5728\u7528\u6237\u7ba1\u7406\u4e2d"
                "\u5c06\u8be5\u7528\u6237\u8bbe\u4e3a disabled\u3002"
            ),
        )
    raise HTTPException(
        status_code=409,
        detail=(
            f"\u65e0\u6cd5\u5220\u9664\u8be5\u8bb0\u5f55\uff1a"
            f"\u4ecd\u88ab\u4ee5\u4e0b\u6570\u636e\u5f15\u7528\uff08{summary}\uff09\u3002"
            "\u8bf7\u5148\u5904\u7406\u5173\u8054\u6570\u636e\u3002"
        ),
    )


def _validate_user_deletion(db, uid: int, actor: dict):
    target = db.execute(
        "SELECT role, company_id FROM users WHERE id = ?", (uid,)
    ).fetchone()
    if not target:
        raise HTTPException(404, "\u7528\u6237\u4e0d\u5b58\u5728")
    if target["role"] == "superAdmin":
        raise HTTPException(400, "\u8d85\u7ea7\u7ba1\u7406\u5458\u8d26\u53f7\u4e0d\u53ef\u5220\u9664")
    if uid == actor["id"]:
        raise HTTPException(400, "\u4e0d\u80fd\u5220\u9664\u81ea\u5df1\u7684\u8d26\u53f7")
    if actor["role"] != "superAdmin" and target["company_id"] != actor["company_id"]:
        raise HTTPException(403, "\u65e0\u6743\u5220\u9664\u5176\u4ed6\u516c\u53f8\u7684\u7528\u6237")
    _raise_delete_conflict("users", _blocking_delete_references(db, "users", uid))
    return target


@okr_router.get("/api/okr/admin/db/tables")
async def admin_db_tables(user: dict = Depends(require_role("superAdmin"))):
    """获取所有表名"""
    with get_db() as db:
        rows = db.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' ORDER BY name"
        ).fetchall()
    return [r["name"] for r in rows]


@okr_router.get("/api/okr/admin/db/tables/{table}")
async def admin_db_table_data(
    table: str,
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=200),
    user: dict = Depends(require_role("superAdmin")),
):
    """分页查询表数据"""
    if table not in _ALLOWED_TABLES:
        raise HTTPException(400, f"不允许访问表: {table}")

    with get_db() as db:
        # 获取列信息
        cols = db.execute(f"PRAGMA table_info({table})").fetchall()
        columns = [{"name": c["name"], "type": c["type"], "pk": c["pk"]} for c in cols]

        # 总数
        total = db.execute(f"SELECT COUNT(*) as cnt FROM {table}").fetchone()["cnt"]

        # 分页数据
        offset = (page - 1) * page_size
        rows = db.execute(
            f"SELECT * FROM {table} ORDER BY id DESC LIMIT ? OFFSET ?",
            (page_size, offset),
        ).fetchall()

    return {
        "columns": columns,
        "rows": dicts_from_rows(rows),
        "total": total,
        "page": page,
        "page_size": page_size,
    }


@okr_router.put("/api/okr/admin/db/tables/{table}/{row_id}")
async def admin_db_update_row(
    table: str, row_id: int,
    request: Request,
    user: dict = Depends(require_role("superAdmin")),
):
    """修改单行数据"""
    if table not in _ALLOWED_TABLES:
        raise HTTPException(400, f"不允许访问表: {table}")

    data = await request.json()

    with get_db() as db:
        # 获取列信息（名称+类型）
        cols = db.execute(f"PRAGMA table_info({table})").fetchall()
        col_map = {c["name"]: c["type"].upper() for c in cols}

        updates = []
        params = []
        for col, val in data.items():
            if col == "id":
                continue
            if col not in col_map:
                raise HTTPException(400, f"无效列名: {col}")

            # 类型转换：空字符串 → NULL，数字类型转换
            if val == "" or val is None:
                val = None
            elif col_map[col] in ("INTEGER", "INT", "BIGINT"):
                try:
                    val = int(val)
                except (ValueError, TypeError):
                    val = None
            elif col_map[col] in ("REAL", "FLOAT", "DOUBLE"):
                try:
                    val = float(val)
                except (ValueError, TypeError):
                    val = None

            updates.append(f"{col} = ?")
            params.append(val)

        if not updates:
            raise HTTPException(400, "没有要更新的字段")

        params.append(row_id)
        try:
            db.execute(
                f"UPDATE {table} SET {', '.join(updates)} WHERE id = ?", params
            )
        except Exception as e:
            raise HTTPException(400, f"更新失败: {e}")
    return {"ok": True, "msg": "数据更新成功"}


@okr_router.delete("/api/okr/admin/db/tables/{table}/{row_id}")
async def admin_db_delete_row(
    table: str, row_id: int,
    user: dict = Depends(require_role("superAdmin")),
):
    """删除单行数据"""
    if table not in _ALLOWED_TABLES:
        raise HTTPException(400, f"不允许访问表: {table}")

    with get_db() as db:
        row = db.execute(f"SELECT id FROM {table} WHERE id = ?", (row_id,)).fetchone()
        if not row:
            raise HTTPException(404, "记录不存在")
        if table == "users":
            _validate_user_deletion(db, row_id, user)
        else:
            _raise_delete_conflict(
                table, _blocking_delete_references(db, table, row_id)
            )
        try:
            db.execute(f"DELETE FROM {table} WHERE id = ?", (row_id,))
        except sqlite3.IntegrityError as exc:
            raise HTTPException(
                status_code=409,
                detail="无法删除该记录：数据库中仍存在关联数据，请先处理关联数据。",
            ) from exc
    return {"ok": True, "msg": "记录已删除"}


# ══════════════════════════════════════════════════════════════════════════
#  OKR 目标 /api/okr/objectives
# ══════════════════════════════════════════════════════════════════════════

@okr_router.get("/api/okr/objectives")
async def list_objectives(
    type: str = Query(None),
    owner_id: int = Query(None),
    department_id: int = Query(None),
    company_id: int = Query(None),
    period: str = Query(None),
    status: str = Query(None),
    include_krs: int = Query(0),
    user: dict = Depends(get_current_user),
):
    """目标列表。非 superAdmin 只能看到自己公司的数据。include_krs=1 时附加 key_results。"""
    cid = _get_company_filter(user, company_id)
    if type == "company" and period:
        period = _annual_period(period)

    sql = (
        "SELECT o.*, u.name as owner_name, d.name as department_name "
        "FROM okr_objectives o "
        "LEFT JOIN users u ON o.owner_id = u.id "
        "LEFT JOIN departments d ON o.department_id = d.id "
        "WHERE 1=1"
    )
    params = []
    if cid is not None:
        sql += " AND o.company_id = ?"
        params.append(cid)
    if type:
        sql += " AND o.type = ?"
        params.append(type)
    if owner_id:
        sql += " AND o.owner_id = ?"
        params.append(owner_id)
    if department_id:
        sql += " AND o.department_id = ?"
        params.append(department_id)
    if period:
        sql += " AND o.period = ?"
        params.append(period)
    if status:
        sql += " AND o.status = ?"
        params.append(status)
    sql += " ORDER BY o.created_at DESC"

    with get_db() as db:
        rows = dicts_from_rows(db.execute(sql, params).fetchall())
        if include_krs:
            _attach_krs(db, rows)
    return rows


@okr_router.post("/api/okr/objectives/copy-from-period")
async def copy_objectives_from_period(
    req: ObjectiveCopyFromPeriod, user: dict = Depends(get_current_user)
):
    """从指定 OKR 批量复制到新周期（O + KR 一起复制，KR 进度归零）"""
    if not req.source_objective_ids:
        raise HTTPException(400, "请选择要复制的 OKR")

    created_ids = []
    with get_db() as db:
        for src_id in req.source_objective_ids:
            src = db.execute(
                "SELECT * FROM okr_objectives WHERE id = ?", (src_id,)
            ).fetchone()
            if not src:
                continue

            obj_type = src["type"]
            if obj_type == "personal":
                raise HTTPException(400, "个人 OKR 每月必须重新设置，不能从历史月份复制")
            if not _can_manage_company_goals(db, user, src["company_id"]):
                raise HTTPException(403, "只有公司负责人或管理员可以复制公司 OKR")
            # 公司隔离
            if user["role"] != "superAdmin" and src["company_id"] != user["company_id"]:
                raise HTTPException(403, "无权复制其他公司的 OKR")

            target_period = _annual_period(req.target_period) if obj_type == "company" else _require_month_period(req.target_period)
            # 重复检测：同标题同周期跳过
            dup = db.execute(
                "SELECT id FROM okr_objectives WHERE title = ? AND period = ? AND type = ? AND company_id IS ?",
                (src["title"], target_period, obj_type, src["company_id"]),
            ).fetchone()
            if dup:
                continue

            dept_id = None
            cur = db.execute(
                "INSERT INTO okr_objectives "
                "(title, description, type, owner_id, company_id, department_id, "
                "aligned_objective_id, is_public, weight, status, period) "
                "VALUES (?, ?, ?, ?, ?, ?, NULL, ?, ?, 'draft', ?)",
                (
                    src["title"], src["description"], obj_type, user["id"],
                    src["company_id"], dept_id,
                    src["is_public"], src["weight"], target_period,
                ),
            )
            new_id = cur.lastrowid
            created_ids.append(new_id)

            # 复制 KR（进度归零）
            krs = db.execute(
                "SELECT * FROM okr_key_results WHERE objective_id = ? ORDER BY id",
                (src_id,),
            ).fetchall()
            for kr in krs:
                db.execute(
                    "INSERT INTO okr_key_results "
                    "(objective_id, title, target_value, current_value, unit, weight, progress) "
                    "VALUES (?, ?, ?, 0, ?, ?, 0)",
                    (new_id, kr["title"], kr["target_value"], kr["unit"], kr["weight"]),
                )

    return {
        "ok": True,
        "msg": f"已复制 {len(created_ids)} 个 OKR",
        "ids": created_ids,
    }


@okr_router.post("/api/okr/objectives")
async def create_objective(req: ObjectiveCreate, user: dict = Depends(get_current_user)):
    """创建年度公司、公司月度或个人月度 OKR。"""
    with get_db() as db:
        # 获取用户部门信息
        u = db.execute(
            "SELECT department_id, company_id FROM users WHERE id = ?", (user["id"],)
        ).fetchone()
        owner = {"id": user["id"], "company_id": user["company_id"], "department_id": u["department_id"] if u else None}
        delegated_by_company_head = False
        if req.owner_id is not None:
            if req.type != "personal":
                raise HTTPException(400, "只有个人 OKR 可以指定员工")
            owner = _personal_view_user(db, user, req.owner_id)
            delegated_by_company_head = _is_company_head(db, user, owner["company_id"])

        dept_id = req.department_id or owner["department_id"]

        # 确定 company_id：优先请求参数 > 部门推断 > 用户所属公司
        obj_company_id = owner["company_id"] if req.type == "personal" else (req.company_id or user["company_id"])
        if obj_company_id is None and dept_id:
            dept_row = db.execute("SELECT company_id FROM departments WHERE id = ?", (dept_id,)).fetchone()
            if dept_row:
                obj_company_id = dept_row["company_id"]

        if obj_company_id is None:
            raise HTTPException(400, "请先选择公司")
        _check_company_access(user, obj_company_id)
        if req.type in ("company", "department") and not _can_manage_company_goals(db, user, obj_company_id):
            raise HTTPException(403, "只有公司负责人或管理员可以维护公司 OKR")

        period = req.period
        aligned_id = req.aligned_objective_id
        if req.type == "company":
            period = _annual_period(period)
            dept_id = None
            aligned_id = None
        elif req.type == "department":
            period = _require_month_period(period)
            dept_id = None
            if aligned_id:
                annual = db.execute(
                    "SELECT id FROM okr_objectives WHERE id = ? AND type = 'company' AND company_id = ? AND period = ?",
                    (aligned_id, obj_company_id, period[:4]),
                ).fetchone()
                if not annual:
                    raise HTTPException(400, "公司月度 OKR 只能对齐本年度公司 OKR")
        else:
            period = _require_month_period(period)
            dept_id = owner["department_id"]
            if aligned_id:
                monthly = db.execute(
                    "SELECT id FROM okr_objectives WHERE id = ? AND type = 'department' AND company_id = ? AND period = ?",
                    (aligned_id, obj_company_id, period),
                ).fetchone()
                if not monthly:
                    raise HTTPException(400, "个人 OKR 只能对齐本月公司月度 OKR")

        # 幂等校验：同一用户 5 秒内创建相同 title 的 OKR 视为重复提交
        cutoff = (datetime.now(timezone.utc) - timedelta(seconds=5)).strftime(
            "%Y-%m-%d %H:%M:%S"
        )
        dup = db.execute(
            "SELECT id FROM okr_objectives "
            "WHERE owner_id = ? AND title = ? AND created_at > ?",
            (owner["id"], req.title.strip(), cutoff),
        ).fetchone()
        if dup:
            return {"ok": True, "msg": "OKR 创建成功", "id": dup["id"]}

        initial_status = "active" if delegated_by_company_head else "draft"
        cur = db.execute(
            "INSERT INTO okr_objectives "
            "(title, description, type, owner_id, company_id, department_id, aligned_objective_id, is_public, weight, status, period) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                req.title.strip(),
                req.description,
                req.type,
                owner["id"],
                obj_company_id,
                dept_id,
                aligned_id,
                req.is_public,
                req.weight,
                initial_status,
                period,
            ),
        )
        if delegated_by_company_head:
            _record_company_head_activation(db, cur.lastrowid, user["id"])
    return {
        "ok": True,
        "msg": "OKR 已创建并直接生效" if delegated_by_company_head else "OKR 创建成功",
        "id": cur.lastrowid,
        "status": initial_status,
        "auto_activated": delegated_by_company_head,
    }


@okr_router.get("/api/okr/objectives/{obj_id}")
async def get_objective(obj_id: int, user: dict = Depends(get_current_user)):
    with get_db() as db:
        obj = db.execute(
            "SELECT o.*, u.name as owner_name, d.name as department_name "
            "FROM okr_objectives o "
            "LEFT JOIN users u ON o.owner_id = u.id "
            "LEFT JOIN departments d ON o.department_id = d.id "
            "WHERE o.id = ?",
            (obj_id,),
        ).fetchone()
        if not obj:
            raise HTTPException(404, "目标不存在")

        # 公司隔离检查
        if user["role"] != "superAdmin" and obj["company_id"] != user["company_id"]:
            raise HTTPException(403, "无权查看其他公司的目标")

        krs = db.execute(
            "SELECT * FROM okr_key_results WHERE objective_id = ? ORDER BY id",
            (obj_id,),
        ).fetchall()

        # 获取对齐的目标信息
        aligned = None
        if obj["aligned_objective_id"]:
            aligned = db.execute(
                "SELECT id, title, type, progress FROM okr_objectives WHERE id = ?",
                (obj["aligned_objective_id"],),
            ).fetchone()

    result = dict_from_row(obj)
    result["key_results"] = dicts_from_rows(krs)
    result["aligned_objective"] = dict_from_row(aligned)
    return result


@okr_router.put("/api/okr/objectives/{obj_id}")
async def update_objective(
    obj_id: int, req: ObjectiveUpdate, user: dict = Depends(get_current_user)
):
    with get_db() as db:
        obj = db.execute(
            "SELECT * FROM okr_objectives WHERE id = ?", (obj_id,)
        ).fetchone()
        if not obj:
            raise HTTPException(404, "目标不存在")

        # 公司隔离检查
        if user["role"] != "superAdmin" and obj["company_id"] != user["company_id"]:
            raise HTTPException(403, "无权编辑其他公司的目标")

        is_company_goal_manager = _can_manage_company_goals(db, user, obj["company_id"])
        if obj["owner_id"] != user["id"] and user["role"] not in ("superAdmin", "admin") and not is_company_goal_manager:
            raise HTTPException(403, "没有权限编辑此目标")
        delegated_by_company_head = (
            obj["type"] == "personal" and _is_company_head(db, user, obj["company_id"])
        )

        updates = []
        params = []
        if req.title is not None:
            updates.append("title = ?")
            params.append(req.title.strip())
        if req.description is not None:
            updates.append("description = ?")
            params.append(req.description)
        if req.status is not None:
            updates.append("status = ?")
            params.append(req.status)
        if req.progress is not None:
            updates.append("progress = ?")
            params.append(req.progress)
        if req.aligned_objective_id is not None:
            aligned_id = req.aligned_objective_id if req.aligned_objective_id > 0 else None
            if aligned_id:
                expected_type = "company" if obj["type"] == "department" else "department"
                expected_period = obj["period"][:4] if obj["type"] == "department" else obj["period"]
                target = db.execute(
                    "SELECT id FROM okr_objectives WHERE id = ? AND type = ? AND company_id = ? AND period = ?",
                    (aligned_id, expected_type, obj["company_id"], expected_period),
                ).fetchone()
                if obj["type"] == "company" or not target:
                    raise HTTPException(400, "所选对齐目标不属于正确的公司或周期")
            updates.append("aligned_objective_id = ?")
            params.append(aligned_id)
        if req.is_public is not None:
            updates.append("is_public = ?")
            params.append(req.is_public)
        if req.weight is not None:
            updates.append("weight = ?")
            params.append(req.weight)

        if delegated_by_company_head:
            updates.append("status = 'active'")

        if updates:
            updates.append("updated_at = datetime('now')")
            params.append(obj_id)
            db.execute(
                f"UPDATE okr_objectives SET {', '.join(updates)} WHERE id = ?", params
            )
            if delegated_by_company_head:
                _record_company_head_activation(db, obj_id, user["id"])
    return {
        "ok": True,
        "msg": "目标已更新并直接生效" if delegated_by_company_head else "目标更新成功",
        "status": "active" if delegated_by_company_head else None,
        "auto_activated": delegated_by_company_head,
    }


@okr_router.delete("/api/okr/objectives/{obj_id}")
async def delete_objective(obj_id: int, request: Request, user: dict = Depends(get_current_user)):
    with get_db() as db:
        obj = dict_from_row(db.execute(
            "SELECT o.*, u.name as owner_name FROM okr_objectives o "
            "JOIN users u ON o.owner_id = u.id WHERE o.id = ?", (obj_id,)
        ).fetchone())
        if not obj:
            raise HTTPException(404, "目标不存在")

        role   = user["role"]
        uid    = user["id"]
        status = obj["status"]

        # 公司隔离
        if role != "superAdmin" and obj["company_id"] != user["company_id"]:
            raise HTTPException(403, "无权删除其他公司的目标")

        is_owner = obj["owner_id"] == uid
        is_company_goal_manager = _can_manage_company_goals(db, user, obj["company_id"])

        if role != "superAdmin" and is_owner and obj["type"] == "personal" and status == "pending":
            raise HTTPException(403, "待审核的个人 OKR 请先撤回审核，再从草稿中删除")

        if is_company_goal_manager:
            if status == "completed" and role != "superAdmin":
                raise HTTPException(403, "已完成的目标不可删除")

        elif role == "superAdmin":
            pass  # 无状态限制，可删除任何状态

        elif role == "admin":
            if status == "completed":
                raise HTTPException(403, "已完成的目标不可删除，如需删除请联系超级管理员")

        elif role == "dept_head":
            if is_owner:
                if status not in ("draft", "pending", "rejected"):
                    raise HTTPException(403, "已审批/执行中的目标不可删除")
            else:
                # 验证被删除 OKR 的 owner 属于 dept_head 的部门
                dh_row = db.execute(
                    "SELECT department_id FROM users WHERE id = ?", (uid,)
                ).fetchone()
                dh_dept = dh_row["department_id"] if dh_row else None
                owner_row = db.execute(
                    "SELECT department_id FROM users WHERE id = ?", (obj["owner_id"],)
                ).fetchone()
                owner_dept = owner_row["department_id"] if owner_row else None
                if not dh_dept or dh_dept != owner_dept:
                    raise HTTPException(403, "只能删除本部门成员的目标")
                if status not in ("draft", "pending", "rejected", "approved"):
                    raise HTTPException(403, "执行中/已完成的目标不可删除")

        else:  # 普通用户
            if not is_owner:
                raise HTTPException(403, "无权删除他人的目标")
            if status not in ("draft", "pending", "rejected"):
                raise HTTPException(403, "只有草稿、待审核或被拒绝的目标可以删除")

        # 审计日志
        type_label = {"company": "公司", "department": "部门", "personal": "个人"}.get(obj["type"], obj["type"])
        detail = f"删除{type_label}OKR: {obj['title']} (状态={status}, 负责人={obj.get('owner_name', '')})"
        client_ip = request.client.host if request.client else ""
        _audit_log(db, user, "delete_objective", obj["type"], obj_id, detail, client_ip)

        db.execute("DELETE FROM okr_objectives WHERE id = ?", (obj_id,))
    return {"ok": True, "msg": "目标已删除"}


# ══════════════════════════════════════════════════════════════════════════
#  Key Results /api/okr/key-results
# ══════════════════════════════════════════════════════════════════════════


def _calculate_kr_progress(current_value, target_value) -> float:
    return calculate_kr_progress(current_value, target_value)


@okr_router.post("/api/okr/objectives/{obj_id}/key-results")
async def create_key_result(
    obj_id: int, req: KeyResultCreate, user: dict = Depends(get_current_user)
):
    with get_db() as db:
        obj = db.execute(
            "SELECT owner_id, company_id, type, department_id FROM okr_objectives WHERE id = ?", (obj_id,)
        ).fetchone()
        if not obj:
            raise HTTPException(404, "目标不存在")
        can_manage_goal = _can_manage_company_goals(db, user, obj["company_id"])
        if obj["owner_id"] != user["id"] and user["role"] not in ("superAdmin", "admin") and not can_manage_goal:
            raise HTTPException(403, "没有权限添加关键结果")
        progress = _calculate_kr_progress(req.current_value, req.target_value)
        cur = db.execute(
            "INSERT INTO okr_key_results "
            "(objective_id, title, target_value, current_value, unit, weight, progress) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                obj_id, req.title.strip(), req.target_value, req.current_value,
                req.unit, req.weight, progress,
            ),
        )
        _update_objective_progress(db, obj_id)
    return {"ok": True, "msg": "关键结果已添加", "id": cur.lastrowid}


@okr_router.put("/api/okr/key-results/{kr_id}")
async def update_key_result(
    kr_id: int, req: KeyResultUpdate, user: dict = Depends(get_current_user)
):
    with get_db() as db:
        kr = db.execute(
            "SELECT kr.*, o.owner_id, o.type AS obj_type, o.department_id AS obj_dept_id, o.company_id AS obj_company_id "
            "FROM okr_key_results kr "
            "JOIN okr_objectives o ON kr.objective_id = o.id "
            "WHERE kr.id = ?",
            (kr_id,),
        ).fetchone()
        if not kr:
            raise HTTPException(404, "关键结果不存在")
        can_manage_goal = _can_manage_company_goals(db, user, kr["obj_company_id"])
        if kr["owner_id"] != user["id"] and user["role"] not in ("superAdmin", "admin") and not can_manage_goal:
            raise HTTPException(403, "没有权限修改此关键结果")

        updates = []
        params = []
        if req.title is not None:
            updates.append("title = ?")
            params.append(req.title.strip())
        if req.target_value is not None:
            updates.append("target_value = ?")
            params.append(req.target_value)
        if req.current_value is not None:
            updates.append("current_value = ?")
            params.append(req.current_value)
        if req.unit is not None:
            updates.append("unit = ?")
            params.append(req.unit)
        if req.weight is not None:
            updates.append("weight = ?")
            params.append(req.weight)

        if updates:
            # 计算进度
            new_current = req.current_value if req.current_value is not None else kr["current_value"]
            new_target = req.target_value if req.target_value is not None else kr["target_value"]
            progress = _calculate_kr_progress(new_current, new_target)
            updates.append("progress = ?")
            params.append(round(progress, 1))
            updates.append("updated_at = datetime('now')")
            params.append(kr_id)
            db.execute(
                f"UPDATE okr_key_results SET {', '.join(updates)} WHERE id = ?", params
            )

            # 更新目标的整体进度（加权平均）
            _update_objective_progress(db, kr["objective_id"])

    return {"ok": True, "msg": "关键结果更新成功"}


@okr_router.delete("/api/okr/key-results/{kr_id}")
async def delete_key_result(kr_id: int, user: dict = Depends(get_current_user)):
    with get_db() as db:
        kr = db.execute(
            "SELECT kr.objective_id, o.owner_id, o.type AS obj_type, o.department_id AS obj_dept_id, o.company_id AS obj_company_id "
            "FROM okr_key_results kr "
            "JOIN okr_objectives o ON kr.objective_id = o.id "
            "WHERE kr.id = ?",
            (kr_id,),
        ).fetchone()
        if not kr:
            raise HTTPException(404, "关键结果不存在")
        can_manage_goal = _can_manage_company_goals(db, user, kr["obj_company_id"])
        if kr["owner_id"] != user["id"] and user["role"] not in ("superAdmin", "admin") and not can_manage_goal:
            raise HTTPException(403, "没有权限删除此关键结果")
        db.execute("DELETE FROM okr_key_results WHERE id = ?", (kr_id,))
        _update_objective_progress(db, kr["objective_id"])
    return {"ok": True, "msg": "关键结果已删除"}


def _update_objective_progress(db, objective_id: int):
    """根据 KR 的加权平均更新目标进度"""
    rows = db.execute(
        "SELECT progress, weight FROM okr_key_results WHERE objective_id = ?",
        (objective_id,),
    ).fetchall()
    if not rows:
        db.execute(
            "UPDATE okr_objectives SET progress = 0, updated_at = datetime('now') WHERE id = ?",
            (objective_id,),
        )
        return
    total_weight = sum(r["weight"] for r in rows)
    if total_weight == 0:
        progress = 0
    else:
        progress = sum(r["progress"] * r["weight"] for r in rows) / total_weight
    db.execute(
        "UPDATE okr_objectives SET progress = ?, updated_at = datetime('now') WHERE id = ?",
        (round(progress, 1), objective_id),
    )


# ══════════════════════════════════════════════════════════════════════════
#  审核 /api/okr/reviews
# ══════════════════════════════════════════════════════════════════════════

@okr_router.post("/api/okr/objectives/{obj_id}/submit-review")
async def submit_review(obj_id: int, user: dict = Depends(get_current_user)):
    """提交个人 OKR，由所属公司的公司负责人统一审核。"""
    with get_db() as db:
        obj = db.execute(
            "SELECT * FROM okr_objectives WHERE id = ?", (obj_id,)
        ).fetchone()
        if not obj:
            raise HTTPException(404, "目标不存在")
        if obj["owner_id"] != user["id"]:
            raise HTTPException(403, "只能提交自己的 OKR 审核")
        if obj["type"] != "personal":
            raise HTTPException(400, "只有个人 OKR 需要提交审核")
        if obj["status"] not in ("draft", "rejected"):
            raise HTTPException(400, "只有草稿或已驳回的个人 OKR 可以提交审核")

        company = db.execute(
            "SELECT head_user_id FROM companies WHERE id = ?", (obj["company_id"],)
        ).fetchone()
        if not company or not company["head_user_id"]:
            raise HTTPException(400, "公司尚未设置负责人，无法提交审核")

        # 更新目标状态
        db.execute(
            "UPDATE okr_objectives SET status = 'pending', updated_at = datetime('now') WHERE id = ?",
            (obj_id,),
        )
        db.execute("DELETE FROM okr_reviews WHERE objective_id = ? AND status = 'pending'", (obj_id,))
        db.execute(
            "INSERT INTO okr_reviews (objective_id, reviewer_id, status) VALUES (?, ?, 'pending')",
            (obj_id, company["head_user_id"]),
        )
    return {"ok": True, "msg": "已提交审核"}


@okr_router.post("/api/okr/objectives/{obj_id}/withdraw-review")
async def withdraw_review(obj_id: int, user: dict = Depends(get_current_user)):
    """撤回本人待审核的个人 OKR，保留目标内容并退回草稿。"""
    with get_db() as db:
        obj = db.execute(
            "SELECT id, type, owner_id, company_id, status FROM okr_objectives WHERE id = ?",
            (obj_id,),
        ).fetchone()
        if not obj:
            raise HTTPException(404, "目标不存在")
        if obj["owner_id"] != user["id"]:
            raise HTTPException(403, "只能撤回自己的 OKR 审核")
        if obj["type"] != "personal":
            raise HTTPException(400, "只有个人 OKR 可以撤回审核")
        if obj["company_id"] != user["company_id"]:
            raise HTTPException(403, "无权撤回其他公司的目标")
        if obj["status"] != "pending":
            raise HTTPException(400, "只有待审核的个人 OKR 可以撤回")

        updated = db.execute(
            "UPDATE okr_objectives SET status = 'draft', updated_at = datetime('now') "
            "WHERE id = ? AND status = 'pending'",
            (obj_id,),
        )
        if updated.rowcount != 1:
            raise HTTPException(409, "审核状态已发生变化，请刷新后重试")
        db.execute(
            "DELETE FROM okr_reviews WHERE objective_id = ? AND status = 'pending'",
            (obj_id,),
        )
    return {"ok": True, "msg": "已撤回审核，OKR 已退回草稿"}


@okr_router.get("/api/okr/reviews/pending")
async def list_pending_reviews(user: dict = Depends(get_current_user)):
    """获取待审核列表（部门负责人或管理员），按公司隔离"""
    if user["role"] not in ("superAdmin", "dept_head"):
        raise HTTPException(403, "没有审核权限")

    with get_db() as db:
        if user["role"] == "superAdmin":
            rows = db.execute(
                "SELECT r.*, o.title, o.type, o.period, o.progress, "
                "u.name as submitter_name, d.name as department_name "
                "FROM okr_reviews r "
                "JOIN okr_objectives o ON r.objective_id = o.id "
                "JOIN users u ON o.owner_id = u.id "
                "LEFT JOIN departments d ON o.department_id = d.id "
                "WHERE r.status = 'pending' ORDER BY r.created_at DESC"
            ).fetchall()
        elif user.get("is_company_head"):
            rows = db.execute(
                "SELECT r.*, o.title, o.type, o.period, o.progress, "
                "u.name as submitter_name, d.name as department_name "
                "FROM okr_reviews r "
                "JOIN okr_objectives o ON r.objective_id = o.id "
                "JOIN users u ON o.owner_id = u.id "
                "LEFT JOIN departments d ON o.department_id = d.id "
                "WHERE r.status = 'pending' AND o.company_id = ? ORDER BY r.created_at DESC",
                (user["company_id"],),
            ).fetchall()
        else:
            # 普通部门负责人只看自己负责审核的 + 同公司
            sql = (
                "SELECT r.*, o.title, o.type, o.period, o.progress, "
                "u.name as submitter_name, d.name as department_name "
                "FROM okr_reviews r "
                "JOIN okr_objectives o ON r.objective_id = o.id "
                "JOIN users u ON o.owner_id = u.id "
                "LEFT JOIN departments d ON o.department_id = d.id "
                "WHERE r.status = 'pending' AND r.reviewer_id = ?"
            )
            params = [user["id"]]
            if user["company_id"] is not None:
                sql += " AND o.company_id = ?"
                params.append(user["company_id"])
            sql += " ORDER BY r.created_at DESC"
            rows = db.execute(sql, params).fetchall()
    return dicts_from_rows(rows)


@okr_router.put("/api/okr/reviews/{review_id}")
async def review_action(
    review_id: int, req: ReviewAction, user: dict = Depends(get_current_user)
):
    """审批通过/驳回"""
    comment = req.comment.strip()
    if req.status == "rejected" and not comment:
        raise HTTPException(400, "驳回时必须填写驳回意见")

    with get_db() as db:
        review = db.execute(
            "SELECT * FROM okr_reviews WHERE id = ?", (review_id,)
        ).fetchone()
        if not review:
            raise HTTPException(404, "审核记录不存在")
        if review["reviewer_id"] != user["id"] and user["role"] != "superAdmin":
            objective_company = db.execute(
                "SELECT company_id FROM okr_objectives WHERE id = ?",
                (review["objective_id"],),
            ).fetchone()
            can_review_company = (
                user.get("is_company_head")
                and objective_company
                and objective_company["company_id"] == user["company_id"]
            )
            if not can_review_company:
                raise HTTPException(403, "没有权限执行此审核")
        if review["status"] != "pending":
            raise HTTPException(400, "该审核已处理")

        # 验证目标所属公司与审核人一致
        if user["role"] != "superAdmin":
            obj = db.execute(
                "SELECT company_id FROM okr_objectives WHERE id = ?",
                (review["objective_id"],),
            ).fetchone()
            if obj and obj["company_id"] != user["company_id"]:
                raise HTTPException(403, "无权审核其他公司的目标")

        db.execute(
            "UPDATE okr_reviews SET status = ?, comment = ?, "
            "created_at = datetime('now') WHERE id = ?",
            (req.status, comment, review_id),
        )
        # 更新目标状态
        new_status = "active" if req.status == "approved" else "rejected"
        db.execute(
            "UPDATE okr_objectives SET status = ?, updated_at = datetime('now') WHERE id = ?",
            (new_status, review["objective_id"]),
        )
    return {"ok": True, "msg": "审核已完成"}


# ══════════════════════════════════════════════════════════════════════════
#  审计日志 /api/okr/admin/audit-logs
# ══════════════════════════════════════════════════════════════════════════


@okr_router.get("/api/okr/admin/audit-logs")
async def list_audit_logs(
    page: int = Query(1, ge=1),
    size: int = Query(20, ge=1, le=100),
    action: str = Query(None),
    user_id: int = Query(None),
    user: dict = Depends(require_role("superAdmin")),
):
    """查询审计日志（仅 superAdmin）"""
    with get_db() as db:
        where = []
        params = []
        if action:
            where.append("a.action = ?")
            params.append(action)
        if user_id:
            where.append("a.user_id = ?")
            params.append(user_id)
        where_sql = (" WHERE " + " AND ".join(where)) if where else ""

        total = db.execute(
            f"SELECT COUNT(*) as cnt FROM audit_logs a{where_sql}", params
        ).fetchone()["cnt"]

        rows = dicts_from_rows(db.execute(
            f"SELECT a.*, u.role as user_role FROM audit_logs a "
            f"LEFT JOIN users u ON a.user_id = u.id{where_sql} "
            f"ORDER BY a.created_at DESC LIMIT ? OFFSET ?",
            params + [size, (page - 1) * size],
        ).fetchall())

    return {"total": total, "page": page, "size": size, "items": rows}


# ══════════════════════════════════════════════════════════════════════════
#  看板数据 /api/okr/board
# ══════════════════════════════════════════════════════════════════════════


def _attach_krs(db, objectives: list):
    return attach_key_results(db, objectives)

def _attach_latest_reviews(db, objectives: list):
    obj_ids = [o["id"] for o in objectives]
    if not obj_ids:
        return
    placeholders = ",".join("?" * len(obj_ids))
    rows = dicts_from_rows(db.execute(
        "SELECT r.*, u.name AS reviewer_name FROM okr_reviews r "
        "JOIN users u ON u.id = r.reviewer_id "
        f"WHERE r.id IN (SELECT MAX(id) FROM okr_reviews WHERE objective_id IN ({placeholders}) GROUP BY objective_id)",
        obj_ids,
    ).fetchall())
    review_map = {row["objective_id"]: row for row in rows}
    for objective in objectives:
        objective["latest_review"] = review_map.get(objective["id"])


def _attach_encouragements(db, objectives: list, user_id: int):
    obj_ids = [o["id"] for o in objectives]
    if not obj_ids:
        return
    placeholders = ",".join("?" * len(obj_ids))
    rows = db.execute(
        "SELECT objective_id, COUNT(*) AS encouragement_count, "
        "SUM(CASE WHEN user_id = ? THEN 1 ELSE 0 END) AS encouraged_by_me "
        f"FROM okr_objective_encouragements WHERE objective_id IN ({placeholders}) "
        "GROUP BY objective_id",
        [user_id, *obj_ids],
    ).fetchall()
    encouragement_map = {row["objective_id"]: row for row in rows}
    for objective in objectives:
        summary = encouragement_map.get(objective["id"])
        objective["encouragement_count"] = int(summary["encouragement_count"]) if summary else 0
        objective["encouraged_by_me"] = bool(summary["encouraged_by_me"]) if summary else False


def _recent_encouragements(db, company_id: int, period: str, limit: int = 10) -> list:
    """Return the latest active encouragements for one company's monthly board."""
    safe_limit = max(1, min(int(limit), 10))
    rows = db.execute(
        "SELECT e.objective_id, e.user_id, e.created_at, "
        "o.title AS objective_title, owner.name AS employee_name "
        "FROM okr_objective_encouragements e "
        "JOIN okr_objectives o ON o.id = e.objective_id "
        "JOIN users owner ON owner.id = o.owner_id "
        "WHERE o.company_id = ? AND o.period = ? AND o.type = 'personal' "
        "ORDER BY e.created_at DESC, e.rowid DESC LIMIT ?",
        (company_id, period, safe_limit),
    ).fetchall()
    return [
        {
            "key": f"{row['objective_id']}:{row['user_id']}",
            "objective_id": row["objective_id"],
            "employee_name": row["employee_name"],
            "objective_title": row["objective_title"],
            "created_at": row["created_at"],
        }
        for row in rows
    ]


def _can_edit_employee_note(db, user: dict, employee: dict) -> bool:
    if user["role"] == "superAdmin":
        return True
    if employee.get("company_id") != user.get("company_id"):
        return False
    if user["role"] == "admin" or _is_company_head(db, user, employee.get("company_id")):
        return True
    if user["role"] != "dept_head" or not employee.get("department_id"):
        return False
    return bool(db.execute(
        "SELECT 1 FROM departments WHERE id = ? AND company_id = ? AND head_user_id = ?",
        (employee["department_id"], employee["company_id"], user["id"]),
    ).fetchone())


def _attach_employee_notes(
    db, members: list, company_id: int, period: str | None, user: dict
):
    if not members:
        return
    note_map = {}
    if period:
        member_ids = [member["id"] for member in members]
        placeholders = ",".join("?" * len(member_ids))
        rows = db.execute(
            "SELECT employee_id, note, updated_at FROM okr_employee_notes "
            f"WHERE company_id = ? AND period = ? AND employee_id IN ({placeholders})",
            [company_id, period, *member_ids],
        ).fetchall()
        note_map = {row["employee_id"]: row for row in rows}
    for member in members:
        note = note_map.get(member["id"])
        member["note"] = note["note"] if note else ""
        member["note_updated_at"] = note["updated_at"] if note else None
        member["can_edit_note"] = _can_edit_employee_note(db, user, member)


@okr_router.get("/api/okr/board/company")
async def board_company(
    period: str = Query(None),
    company_id: int = Query(None),
    user: dict = Depends(get_current_user),
):
    """年度公司 OKR 看板；所选月份只用于确定所属年度。"""
    cid = _get_company_filter(user, company_id)
    annual_period = _annual_period(period) if period else None
    with get_db() as db:
        company_name = None
        if cid is not None:
            c = db.execute("SELECT name FROM companies WHERE id = ?", (cid,)).fetchone()
            company_name = c["name"] if c else None
        sql = "SELECT o.*, u.name as owner_name FROM okr_objectives o JOIN users u ON o.owner_id = u.id WHERE o.type = 'company'"
        params = []
        if cid is not None:
            sql += " AND o.company_id = ?"
            params.append(cid)
        if annual_period:
            sql += " AND o.period = ?"
            params.append(annual_period)
        company_okrs = dicts_from_rows(db.execute(sql, params).fetchall())
        _attach_krs(db, company_okrs)
        _attach_latest_reviews(db, company_okrs)
        _attach_encouragements(db, company_okrs, user["id"])
    result = {"company_okrs": company_okrs, "departments": [], "period": annual_period}
    if company_name:
        result["company_name"] = company_name
    return result


def _ensure_monthly_company_goals(db, company_id: int, period: str, owner_id: int) -> int:
    """Idempotently clone the immediately previous month's company goals."""
    current_count = db.execute(
        "SELECT COUNT(*) AS count FROM okr_objectives WHERE company_id = ? AND type = 'department' AND period = ?",
        (company_id, period),
    ).fetchone()["count"]
    if current_count:
        return 0
    source_period = _previous_month(period)
    sources = db.execute(
        "SELECT * FROM okr_objectives WHERE company_id = ? AND type = 'department' AND period = ? ORDER BY id",
        (company_id, source_period),
    ).fetchall()
    created = 0
    for src in sources:
        aligned_id = src["aligned_objective_id"]
        if aligned_id:
            aligned = db.execute(
                "SELECT title, period FROM okr_objectives WHERE id = ? AND type = 'company'",
                (aligned_id,),
            ).fetchone()
            if not aligned or aligned["period"] != period[:4]:
                replacement = db.execute(
                    "SELECT id FROM okr_objectives WHERE company_id = ? AND type = 'company' AND period = ? AND title = ? ORDER BY id DESC LIMIT 1",
                    (company_id, period[:4], aligned["title"] if aligned else ""),
                ).fetchone()
                aligned_id = replacement["id"] if replacement else None
        cursor = db.execute(
            "INSERT INTO okr_objectives "
            "(title, description, type, owner_id, company_id, department_id, aligned_objective_id, is_public, status, progress, weight, period) "
            "VALUES (?, ?, 'department', ?, ?, NULL, ?, ?, 'draft', 0, ?, ?)",
            (src["title"], src["description"], owner_id, company_id,
             aligned_id, src["is_public"], src["weight"], period),
        )
        new_id = cursor.lastrowid
        for kr in db.execute("SELECT * FROM okr_key_results WHERE objective_id = ? ORDER BY id", (src["id"],)).fetchall():
            db.execute(
                "INSERT INTO okr_key_results (objective_id, title, target_value, current_value, unit, weight, progress) "
                "VALUES (?, ?, ?, 0, ?, ?, 0)",
                (new_id, kr["title"], kr["target_value"], kr["unit"], kr["weight"]),
            )
        created += 1
    return created


@okr_router.get("/api/okr/board/monthly-company")
async def board_monthly_company(
    period: str = Query(...),
    company_id: int = Query(None),
    user: dict = Depends(get_current_user),
):
    """Company monthly goals with all aligned people directly beneath them."""
    period = _require_month_period(period)
    cid = _get_company_filter(user, company_id)
    if cid is None:
        raise HTTPException(400, "请先选择公司")
    with get_db() as db:
        company = db.execute(
            "SELECT c.id, c.name, c.head_user_id, h.name AS head_name FROM companies c "
            "LEFT JOIN users h ON c.head_user_id = h.id WHERE c.id = ? AND c.status = 'active'",
            (cid,),
        ).fetchone()
        if not company:
            raise HTTPException(404, "公司不存在")
        cloned = _ensure_monthly_company_goals(db, cid, period, company["head_user_id"] or user["id"])
        goals = dicts_from_rows(db.execute(
            "SELECT o.*, u.name AS owner_name FROM okr_objectives o JOIN users u ON o.owner_id = u.id "
            "WHERE o.company_id = ? AND o.type = 'department' AND o.period = ? ORDER BY o.id",
            (cid, period),
        ).fetchall())
        _attach_krs(db, goals)
        _attach_latest_reviews(db, goals)
        _attach_encouragements(db, goals, user["id"])
        members = dicts_from_rows(db.execute(
            "SELECT u.id, u.name, u.phone, u.role, u.company_id, u.department_id, "
            "d.name AS department_name "
            "FROM users u LEFT JOIN departments d ON u.department_id = d.id "
            "WHERE u.company_id = ? AND u.status = 'active' ORDER BY d.id, u.id",
            (cid,),
        ).fetchall())
        _attach_employee_notes(db, members, cid, period, user)
        personal = dicts_from_rows(db.execute(
            "SELECT o.*, u.name AS owner_name, d.name AS department_name FROM okr_objectives o "
            "JOIN users u ON o.owner_id = u.id LEFT JOIN departments d ON u.department_id = d.id "
            "WHERE o.company_id = ? AND o.type = 'personal' AND o.period = ? ORDER BY o.id",
            (cid, period),
        ).fetchall())
        _attach_krs(db, personal)
        _attach_latest_reviews(db, personal)
        _attach_encouragements(db, personal, user["id"])
        recent_encouragements = _recent_encouragements(db, cid, period)
    by_member = {}
    for objective in personal:
        by_member.setdefault(objective["owner_id"], []).append(objective)
    member_nodes = []
    for member in members:
        okrs = by_member.get(member["id"], [])
        member_nodes.append({"user": member, "okrs": okrs, "has_okr": bool(okrs)})
    return {
        "department": dict_from_row(company),
        "dept_okrs": goals,
        "members": member_nodes,
        "recent_encouragements": recent_encouragements,
        "period": period,
        "cloned_from_previous": cloned,
    }


@okr_router.get("/api/okr/board/department/{dept_id}")
async def board_department(
    dept_id: int, period: str = Query(None),
    user: dict = Depends(get_current_user),
):
    """部门看板：部门OKR -> 成员OKR。验证部门归属。"""
    if period:
        period = _require_month_period(period)
    with get_db() as db:
        dept = db.execute(
            "SELECT d.*, u.name as head_name FROM departments d "
            "LEFT JOIN users u ON d.head_user_id = u.id WHERE d.id = ?",
            (dept_id,),
        ).fetchone()
        if not dept:
            raise HTTPException(404, "部门不存在")

        # 验证部门属于用户的公司
        if user["role"] != "superAdmin" and dept["company_id"] != user["company_id"]:
            raise HTTPException(403, "无权查看其他公司的部门")

        # 部门 OKR
        dept_sql = (
            "SELECT o.*, u.name as owner_name FROM okr_objectives o "
            "JOIN users u ON o.owner_id = u.id "
            "WHERE o.type = 'department' AND o.department_id = ?"
        )
        dept_params = [dept_id]
        if period:
            dept_sql += " AND o.period = ?"
            dept_params.append(period)
        dept_okrs = dicts_from_rows(db.execute(dept_sql, dept_params).fetchall())

        # 附加部门 OKR 的 KR
        _attach_krs(db, dept_okrs)
        _attach_latest_reviews(db, dept_okrs)
        _attach_encouragements(db, dept_okrs, user["id"])

        # 部门成员
        members = dicts_from_rows(
            db.execute(
                "SELECT u.id, u.name, u.phone, u.role, u.company_id, u.department_id, "
                "d.name AS department_name FROM users u "
                "LEFT JOIN departments d ON d.id = u.department_id "
                "WHERE u.department_id = ? AND u.status = 'active' ORDER BY u.id",
                (dept_id,),
            ).fetchall()
        )
        _attach_employee_notes(db, members, dept["company_id"], period, user)

        # 成员个人 OKR
        member_sql = (
            "SELECT o.*, u.name as owner_name FROM okr_objectives o "
            "JOIN users u ON o.owner_id = u.id "
            "WHERE o.type = 'personal' AND o.department_id = ?"
        )
        member_params = [dept_id]
        if period:
            member_sql += " AND o.period = ?"
            member_params.append(period)
        member_okrs = dicts_from_rows(db.execute(member_sql, member_params).fetchall())
        _attach_krs(db, member_okrs)
        _attach_latest_reviews(db, member_okrs)
        _attach_encouragements(db, member_okrs, user["id"])

    # 按成员分组
    member_okr_map = {}
    for okr in member_okrs:
        member_okr_map.setdefault(okr["owner_id"], []).append(okr)

    member_nodes = []
    for m in members:
        okrs = member_okr_map.get(m["id"], [])
        member_nodes.append({
            "user": m,
            "okrs": okrs,
            "has_okr": len(okrs) > 0,
        })

    return {
        "department": dict_from_row(dept),
        "dept_okrs": dept_okrs,
        "members": member_nodes,
        "period": period,
    }


@okr_router.get("/api/okr/personal-view/employees")
async def list_personal_view_employees(user: dict = Depends(get_current_user)):
    """公司负责人个人 OKR 视角切换所需的本公司在职员工列表。"""
    with get_db() as db:
        if not _is_company_head(db, user, user.get("company_id")):
            raise HTTPException(403, "只有公司负责人可以切换员工个人 OKR 视角")
        rows = db.execute(
            "SELECT u.id, u.name, u.department_id, d.name AS department_name "
            "FROM users u LEFT JOIN departments d ON d.id = u.department_id "
            "WHERE u.company_id = ? AND u.status = 'active' "
            "ORDER BY CASE WHEN u.id = ? THEN 0 ELSE 1 END, d.id, u.name, u.id",
            (user["company_id"], user["id"]),
        ).fetchall()
    return dicts_from_rows(rows)


@okr_router.get("/api/okr/board/personal")
async def board_personal(
    period: str = Query(None),
    user: dict = Depends(get_current_user),
    user_id: int | None = None,
):
    """个人看板；公司负责人可切换到本公司任一在职员工。"""
    with get_db() as db:
        view_user = _personal_view_user(db, user, user_id)
        can_manage = _is_company_head(db, user, view_user["company_id"])
        sql = (
            "SELECT o.*, d.name as department_name "
            "FROM okr_objectives o "
            "LEFT JOIN departments d ON o.department_id = d.id "
            "WHERE o.owner_id = ? AND o.type = 'personal'"
        )
        params = [view_user["id"]]
        # 额外的公司隔离安全过滤
        if view_user["company_id"] is not None:
            sql += " AND o.company_id = ?"
            params.append(view_user["company_id"])
        if period:
            sql += " AND o.period = ?"
            params.append(period)
        sql += " ORDER BY o.created_at DESC"
        objectives = dicts_from_rows(db.execute(sql, params).fetchall())
        _attach_krs(db, objectives)
        _attach_latest_reviews(db, objectives)
        _attach_encouragements(db, objectives, user["id"])

        # 为每个目标附加对齐目标信息
        for obj in objectives:
            if obj["aligned_objective_id"]:
                aligned = db.execute(
                    "SELECT id, title, type, progress, period FROM okr_objectives WHERE id = ?",
                    (obj["aligned_objective_id"],),
                ).fetchone()
                obj["aligned_objective"] = dict_from_row(aligned)
            else:
                obj["aligned_objective"] = None

    return {
        "objectives": objectives,
        "view_user": view_user,
        "can_manage": can_manage,
    }


@okr_router.put("/api/okr/board/employees/{employee_id}/note")
async def update_employee_board_note(
    employee_id: int,
    req: EmployeeBoardNoteUpdate,
    user: dict = Depends(get_current_user),
):
    """公司负责人或部门负责人维护员工在指定月份的看板备注。"""
    period = _require_month_period(req.period)
    note = req.note.strip()
    with get_db() as db:
        employee = dict_from_row(db.execute(
            "SELECT id, name, company_id, department_id FROM users "
            "WHERE id = ? AND status = 'active'",
            (employee_id,),
        ).fetchone())
        if not employee:
            raise HTTPException(404, "员工不存在或账号未启用")
        _check_company_access(user, employee["company_id"])
        if not _can_edit_employee_note(db, user, employee):
            raise HTTPException(403, "只有公司负责人或该员工的部门负责人可以维护备注")
        if note:
            db.execute(
                "INSERT INTO okr_employee_notes "
                "(company_id, employee_id, period, note, updated_by) VALUES (?, ?, ?, ?, ?) "
                "ON CONFLICT(company_id, employee_id, period) DO UPDATE SET "
                "note = excluded.note, updated_by = excluded.updated_by, updated_at = datetime('now')",
                (employee["company_id"], employee_id, period, note, user["id"]),
            )
        else:
            db.execute(
                "DELETE FROM okr_employee_notes WHERE company_id = ? AND employee_id = ? AND period = ?",
                (employee["company_id"], employee_id, period),
            )
    return {"ok": True, "employee_id": employee_id, "period": period, "note": note}


@okr_router.post("/api/okr/objectives/{obj_id}/encouragement")
async def toggle_objective_encouragement(
    obj_id: int, user: dict = Depends(get_current_user)
):
    """点赞鼓励其他员工的个人 Object；同一人再次操作会取消。"""
    with get_db() as db:
        objective = db.execute(
            "SELECT o.id, o.owner_id, o.company_id, o.type, o.title, owner.name AS owner_name "
            "FROM okr_objectives o JOIN users owner ON owner.id = o.owner_id WHERE o.id = ?",
            (obj_id,),
        ).fetchone()
        if not objective:
            raise HTTPException(404, "目标不存在")
        _check_company_access(user, objective["company_id"])
        if objective["type"] != "personal":
            raise HTTPException(400, "只能鼓励员工的个人 Object")
        if objective["owner_id"] == user["id"]:
            raise HTTPException(400, "不能给自己的 Object 点赞鼓励")
        existing = db.execute(
            "SELECT 1 FROM okr_objective_encouragements WHERE objective_id = ? AND user_id = ?",
            (obj_id, user["id"]),
        ).fetchone()
        if existing:
            db.execute(
                "DELETE FROM okr_objective_encouragements WHERE objective_id = ? AND user_id = ?",
                (obj_id, user["id"]),
            )
            encouraged = False
            recent_encouragement = None
        else:
            db.execute(
                "INSERT INTO okr_objective_encouragements(objective_id, user_id) VALUES (?, ?)",
                (obj_id, user["id"]),
            )
            encouraged = True
            created_at = db.execute(
                "SELECT created_at FROM okr_objective_encouragements "
                "WHERE objective_id = ? AND user_id = ?",
                (obj_id, user["id"]),
            ).fetchone()[0]
            recent_encouragement = {
                "key": f"{obj_id}:{user['id']}",
                "objective_id": obj_id,
                "employee_name": objective["owner_name"],
                "objective_title": objective["title"],
                "created_at": created_at,
            }
        count = db.execute(
            "SELECT COUNT(*) FROM okr_objective_encouragements WHERE objective_id = ?",
            (obj_id,),
        ).fetchone()[0]
    return {
        "ok": True,
        "objective_id": obj_id,
        "encouraged": encouraged,
        "encouragement_count": count,
        "recent_encouragement": recent_encouragement,
        "removed_encouragement_key": None if encouraged else f"{obj_id}:{user['id']}",
    }


# ══════════════════════════════════════════════════════════════════════════
#  统计报表 /api/okr/statistics
# ══════════════════════════════════════════════════════════════════════════

@okr_router.get("/api/okr/statistics/overview")
async def statistics_overview(
    period: str = Query(None),
    company_id: int = Query(None),
    user: dict = Depends(get_current_user),
):
    """各部门 OKR 完成概览（按公司隔离，所有登录用户可查看本公司数据）"""
    cid = _get_company_filter(user, company_id)

    with get_db() as db:
        dept_sql = "SELECT id, name FROM departments WHERE 1=1"
        dept_params = []
        if cid is not None:
            dept_sql += " AND company_id = ?"
            dept_params.append(cid)
        dept_sql += " ORDER BY id"
        departments = dicts_from_rows(db.execute(dept_sql, dept_params).fetchall())

        result = []
        for dept in departments:
            sql = (
                "SELECT SUM(o.progress * o.weight) / NULLIF(SUM(o.weight), 0) as avg_progress, "
                "COUNT(*) as total, "
                "SUM(CASE WHEN o.status = 'completed' THEN 1 ELSE 0 END) as completed "
                "FROM okr_objectives o WHERE o.department_id = ?"
            )
            params = [dept["id"]]
            if period:
                sql += " AND o.period = ?"
                params.append(period)
            stats = db.execute(sql, params).fetchone()
            result.append({
                "department_id": dept["id"],
                "department_name": dept["name"],
                "avg_progress": round(stats["avg_progress"] or 0, 1),
                "total_objectives": stats["total"],
                "completed_objectives": stats["completed"],
            })
    return result


@okr_router.get("/api/okr/statistics/department/{dept_id}")
async def statistics_department(
    dept_id: int, period: str = Query(None),
    user: dict = Depends(get_current_user),
):
    """部门内成员 OKR 统计"""
    with get_db() as db:
        # 验证部门归属
        if user["role"] != "superAdmin":
            dept = db.execute(
                "SELECT company_id FROM departments WHERE id = ?", (dept_id,)
            ).fetchone()
            if dept and dept["company_id"] != user["company_id"]:
                raise HTTPException(403, "无权查看其他公司的部门统计")

        members = dicts_from_rows(
            db.execute(
                "SELECT id, name, wecom_userid FROM users "
                "WHERE department_id = ? AND status = 'active' ORDER BY id",
                (dept_id,),
            ).fetchall()
        )
        name_counts = {}
        for member in members:
            name_counts[member["name"]] = name_counts.get(member["name"], 0) + 1
        result = []
        for m in members:
            sql = (
                "SELECT SUM(o.progress * o.weight) / NULLIF(SUM(o.weight), 0) as avg_progress, "
                "COUNT(*) as total, "
                "SUM(CASE WHEN o.status NOT IN ('draft') THEN 1 ELSE 0 END) as submitted, "
                "SUM(CASE WHEN o.status = 'pending' THEN 1 ELSE 0 END) as pending_count, "
                "SUM(CASE WHEN o.status = 'completed' THEN 1 ELSE 0 END) as completed "
                "FROM okr_objectives o WHERE o.owner_id = ? AND o.type = 'personal'"
            )
            params = [m["id"]]
            if period:
                sql += " AND o.period = ?"
                params.append(period)
            stats = db.execute(sql, params).fetchone()
            result.append({
                "user_id": m["id"],
                "user_name": m["name"],
                "duplicate_name": name_counts[m["name"]] > 1,
                "account_source": "wecom" if m.get("wecom_userid") else "local",
                "avg_progress": round(stats["avg_progress"] or 0, 1),
                "total_objectives": stats["total"],
                "submitted_objectives": stats["submitted"] or 0,
                "pending_objectives": stats["pending_count"] or 0,
                "completed_objectives": stats["completed"] or 0,
            })
    return result


# ══════════════════════════════════════════════════════════════════════════
#  公共数据接口
# ══════════════════════════════════════════════════════════════════════════

@okr_router.get("/api/okr/companies")
async def public_list_companies():
    """公开接口：获取所有活跃公司列表（注册页用）"""
    with get_db() as db:
        rows = db.execute(
            "SELECT id, name FROM companies WHERE status = 'active' ORDER BY name"
        ).fetchall()
    return dicts_from_rows(rows)


@okr_router.get("/api/okr/departments")
async def public_list_departments(company_id: int = Query(None)):
    """公开部门列表（注册时选择部门用），支持按公司筛选"""
    with get_db() as db:
        sql = "SELECT id, name FROM departments"
        params = []
        if company_id:
            sql += " WHERE company_id = ?"
            params.append(company_id)
        sql += " ORDER BY name"
        rows = db.execute(sql, params).fetchall()
    return dicts_from_rows(rows)


# ═══════════════════════════════════════════════════════════════════════════════
# 月度目标评估 /api/okr/reviews/monthly
# ═══════════════════════════════════════════════════════════════════════════════

def _calc_composite(progress: float, self_score, manager_score):
    """综合评分 = 自评×20% + 主管评分×80%；保留 progress 参数兼容原调用。"""
    return objective_score(progress, self_score, manager_score)


def _review_row_to_dict(r: dict, obj: dict) -> dict:
    """将 review 行 + objective 行组合成前端需要的结构"""
    progress = obj.get("progress", 0.0) or 0.0
    composite = _calc_composite(progress, r.get("self_score"), r.get("manager_score"))
    return {
        "id":                   r["id"],
        "objective_id":         r["objective_id"],
        "objective_title":      obj.get("title", ""),
        "objective_description": obj.get("description", "") or "",
        "objective_type":       obj.get("type", ""),
        "objective_weight":     obj.get("weight", 1.0),
        "key_results":          obj.get("key_results", []),
        "owner_id":             obj.get("owner_id"),
        "owner_name":           obj.get("owner_name", ""),
        "owner_role":           obj.get("owner_role", ""),
        "aligned_objective_id":    obj.get("aligned_objective_id"),
        "aligned_objective_title": obj.get("aligned_objective_title", "") or "",
        "department_id":        obj.get("department_id"),
        "department_name":      obj.get("department_name", ""),
        "year_month":           r["year_month"],
        "kr_completion_rate":   round(progress, 1),
        "self_score":           r["self_score"],
        "self_comment":         r["self_comment"],
        "self_submitted_at":    r["self_submitted_at"],
        "manager_score":        r["manager_score"],
        "manager_comment":      r["manager_comment"],
        "manager_id":           r["manager_id"],
        "manager_submitted_at": r["manager_submitted_at"],
        "status":               r["status"],
        "composite_score":      composite,
        "created_at":           r["created_at"],
        "updated_at":           r["updated_at"],
    }


@okr_router.get("/api/okr/reviews/monthly")
async def list_monthly_reviews(
    period: str = Query(None),
    dept_id: int = Query(None),
    company_id: int = Query(None),
    user: dict = Depends(get_current_user),
):
    """
    查询月度考核记录列表。
    - 普通用户：返回自己的目标 + 同部门已完成的评估（部门内公开）
    - dept_head / admin / superAdmin：返回辖区内所有目标的评估记录
    """
    role = user["role"]
    uid = user["id"]
    is_company_head = bool(user.get("is_company_head"))

    with get_db() as db:
        # ── 构建 objectives 过滤条件 ──────────────────────────────────
        obj_sql = """
            SELECT o.id, o.title, o.description, o.type, o.owner_id, o.department_id,
                   o.progress, o.weight, o.period, o.company_id,
                   o.aligned_objective_id,
                   ao.title AS aligned_objective_title,
                   u.name AS owner_name,
                   u.role AS owner_role,
                   d.name AS department_name
            FROM okr_objectives o
            LEFT JOIN users u ON u.id = o.owner_id
            LEFT JOIN departments d ON d.id = o.department_id
            LEFT JOIN okr_objectives ao ON ao.id = o.aligned_objective_id
            WHERE 1=1
        """
        obj_params = []

        # 按周期过滤
        if period:
            obj_sql += " AND o.period = ?"
            obj_params.append(period)

        if role == "superAdmin":
            # superAdmin 可查看全部，支持公司筛选
            eff_company = company_id
            if eff_company:
                obj_sql += " AND o.company_id = ?"
                obj_params.append(eff_company)
        else:
            # 非 superAdmin 只看本公司
            obj_sql += " AND o.company_id = ?"
            obj_params.append(user["company_id"])

        # 按部门筛选（dept_head / admin / 前端传入 dept_id）
        if dept_id:
            obj_sql += " AND o.department_id = ?"
            obj_params.append(dept_id)
        if role == "dept_head" and not is_company_head:
            # dept_head 只看本部门：部门OKR + 本部门成员的个人OKR
            dh_row = db.execute(
                "SELECT department_id FROM users WHERE id = ?", (uid,)
            ).fetchone()
            dh_dept = dh_row["department_id"] if dh_row else None
            if dh_dept:
                obj_sql += (
                    " AND (o.department_id = ?"
                    " OR o.owner_id IN (SELECT id FROM users WHERE department_id = ?))"
                )
                obj_params.extend([dh_dept, dh_dept])
            else:
                obj_sql += " AND o.owner_id = ?"
                obj_params.append(uid)
        elif role == "user":
            # 普通用户只看自己的目标 + 同部门可见已完成
            pass  # 后面单独处理

        obj_sql += " ORDER BY o.owner_id, o.id"
        objectives = dicts_from_rows(db.execute(obj_sql, obj_params).fetchall())

        if not objectives:
            return {"reviews": [], "my_reviews": [], "dept_reviews": []}

        _attach_krs(db, objectives)

        obj_ids = [o["id"] for o in objectives]
        obj_map = {o["id"]: o for o in objectives}

        # ── 拉取对应的 review 记录 ────────────────────────────────────
        placeholders = ",".join("?" * len(obj_ids))
        review_sql = f"SELECT * FROM okr_monthly_reviews WHERE objective_id IN ({placeholders})"
        review_params = list(obj_ids)
        if period:
            review_sql += " AND year_month = ?"
            review_params.append(period)
        reviews_raw = dicts_from_rows(db.execute(review_sql + " ORDER BY id", review_params).fetchall())

        review_map = {r["objective_id"]: r for r in reviews_raw}

        # ── 组装结果 ──────────────────────────────────────────────────
        all_items = []
        for obj in objectives:
            review = review_map.get(obj["id"])
            if review:
                all_items.append(_review_row_to_dict(review, obj))
            else:
                # 目标存在但尚未创建 review 记录
                all_items.append({
                    "id": None,
                    "objective_id":       obj["id"],
                    "objective_title":    obj["title"],
                    "objective_description": obj.get("description", "") or "",
                    "objective_type":     obj["type"],
                    "objective_weight":   obj.get("weight", 1.0),
                    "key_results":        obj.get("key_results", []),
                    "owner_id":           obj["owner_id"],
                    "owner_name":         obj.get("owner_name", ""),
                    "owner_role":         obj.get("owner_role", ""),
                    "aligned_objective_id":    obj.get("aligned_objective_id"),
                    "aligned_objective_title": obj.get("aligned_objective_title", "") or "",
                    "department_id":      obj.get("department_id"),
                    "department_name":    obj.get("department_name", ""),
                    "year_month":         period or "",
                    "kr_completion_rate": round(obj.get("progress", 0.0) or 0.0, 1),
                    "self_score":         None,
                    "self_comment":       "",
                    "self_submitted_at":  None,
                    "manager_score":      None,
                    "manager_comment":    "",
                    "manager_id":         None,
                    "manager_submitted_at": None,
                    "status":             "pending",
                    "composite_score":    None,
                    "created_at":         None,
                    "updated_at":         None,
                })

        # 拆分：自己的 vs 部门成员的
        if role == "superAdmin":
            # 公司总览包括尚未自评的个人目标，避免月度权重漏项。
            my_reviews = []
            dept_reviews = [x for x in all_items if x.get("objective_type") == "personal"]
        elif role == "dept_head" and not is_company_head:
            my_reviews = [x for x in all_items if x["owner_id"] == uid]
            # 部门评分总览排除自己：自己的OKR由 superAdmin 评分，不出现在此区
            dept_reviews = [x for x in all_items if x["owner_id"] != uid]
        elif role == "admin" or is_company_head:
            my_reviews = [x for x in all_items if x["owner_id"] == uid]
            dept_reviews = all_items
        else:
            # 普通用户只能看同部门已完成的（部门内公开规则）
            my_reviews = [x for x in all_items if x["owner_id"] == uid]
            user_dept = db.execute(
                "SELECT department_id FROM users WHERE id = ?", (uid,)
            ).fetchone()
            user_dept_id = user_dept["department_id"] if user_dept else None
            dept_reviews = [
                x for x in all_items
                if x["department_id"] == user_dept_id and x["status"] == "completed"
            ]

        # Only expose records within the same scope as the visible sections.
        if role == "user" and not is_company_head:
            allowed_ids = {x["objective_id"] for x in my_reviews + dept_reviews}
            all_items = [x for x in all_items if x["objective_id"] in allowed_ids]

    return {
        "reviews":      all_items,
        "my_reviews":   my_reviews,
        "dept_reviews": dept_reviews,
    }


@okr_router.post("/api/okr/reviews/monthly")
async def create_monthly_review(
    req: MonthlyReviewCreate,
    user: dict = Depends(get_current_user),
):
    """
    创建月度考核记录（幂等）。
    - 若该目标该月已有记录，直接返回已有记录
    - 只有目标 owner 或管理员可创建
    """
    with get_db() as db:
        obj = db.execute(
            "SELECT id, owner_id, company_id, department_id, title, progress, period FROM okr_objectives WHERE id = ?",
            (req.objective_id,)
        ).fetchone()
        if not obj:
            raise HTTPException(404, "目标不存在")
        if obj["period"] != req.year_month:
            raise HTTPException(400, "考核月份必须与目标周期一致")

        role = user["role"]
        uid  = user["id"]
        # 权限：目标 owner 本人，或 dept_head/admin/superAdmin
        is_owner   = obj["owner_id"] == uid
        is_manager = role in ("dept_head", "admin", "superAdmin")
        if not (is_owner or is_manager):
            raise HTTPException(403, "无权创建此考核记录")
        if not is_owner and role != "superAdmin":
            if obj["company_id"] != user["company_id"]:
                raise HTTPException(403, "只能管理本公司的考核记录")
            if role == "dept_head" and not user.get("is_company_head"):
                user_dept = db.execute(
                    "SELECT department_id FROM users WHERE id = ?", (uid,)
                ).fetchone()
                if not user_dept or user_dept["department_id"] != obj["department_id"]:
                    raise HTTPException(403, "只能管理本部门的考核记录")

        # 幂等：已存在直接返回
        existing = db.execute(
            "SELECT * FROM okr_monthly_reviews WHERE objective_id = ? AND year_month = ?",
            (req.objective_id, req.year_month)
        ).fetchone()
        if existing:
            return {"ok": True, "review": dict_from_row(existing), "created": False}

        now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        cur = db.execute(
            """INSERT INTO okr_monthly_reviews
               (objective_id, year_month, status, created_at, updated_at)
               VALUES (?, ?, 'pending', ?, ?)""",
            (req.objective_id, req.year_month, now, now)
        )
        review = dict_from_row(db.execute(
            "SELECT * FROM okr_monthly_reviews WHERE id = ?", (cur.lastrowid,)
        ).fetchone())
    return {"ok": True, "review": review, "created": True}


@okr_router.put("/api/okr/reviews/monthly/{review_id}/self")
async def submit_self_review(
    review_id: int,
    req: SelfReviewSubmit,
    user: dict = Depends(get_current_user),
):
    """
    员工提交自评（pending → self_done）。
    - 只有目标 owner 本人可提交
    - 提交后不可修改
    """
    if not (0 <= req.self_score <= 100):
        raise HTTPException(400, "自评分必须在 0-100 之间")

    with get_db() as db:
        review = db.execute(
            """SELECT r.*, o.owner_id FROM okr_monthly_reviews r
               JOIN okr_objectives o ON o.id = r.objective_id
               WHERE r.id = ?""",
            (review_id,)
        ).fetchone()
        if not review:
            raise HTTPException(404, "考核记录不存在")
        if review["owner_id"] != user["id"]:
            raise HTTPException(403, "只有目标本人可提交自评")
        if review["status"] != "pending":
            raise HTTPException(400, f"当前状态 '{review['status']}' 不允许提交自评")

        now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        db.execute(
            """UPDATE okr_monthly_reviews
               SET self_score=?, self_comment=?, self_submitted_at=?,
                   status='self_done', updated_at=?
               WHERE id=?""",
            (req.self_score, req.self_comment, now, now, review_id)
        )
        updated = dict_from_row(db.execute(
            "SELECT * FROM okr_monthly_reviews WHERE id = ?", (review_id,)
        ).fetchone())
    return {"ok": True, "review": updated}


@okr_router.put("/api/okr/reviews/monthly/{review_id}/manager")
async def submit_manager_review(
    review_id: int,
    req: ManagerReviewSubmit,
    user: dict = Depends(get_current_user),
):
    """
    主管提交评分（self_done → completed）。
    - dept_head 只能评分本部门成员
    - admin / superAdmin 可评分辖区内所有人
    """
    role = user["role"]
    is_company_head = bool(user.get("is_company_head"))
    if role not in ("dept_head", "admin", "superAdmin"):
        raise HTTPException(403, "无权提交主管评分")
    if not (0 <= req.manager_score <= 100):
        raise HTTPException(400, "主管评分必须在 0-100 之间")

    with get_db() as db:
        review = db.execute(
            """SELECT r.*, o.owner_id, o.department_id, o.company_id
               FROM okr_monthly_reviews r
               JOIN okr_objectives o ON o.id = r.objective_id
               WHERE r.id = ?""",
            (review_id,)
        ).fetchone()
        if not review:
            raise HTTPException(404, "考核记录不存在")
        if review["status"] != "self_done":
            raise HTTPException(400, f"当前状态 '{review['status']}' 不允许提交主管评分（需先完成员工自评）")

        # 部门负责人的OKR只能由 superAdmin 评分
        owner_info = db.execute(
            "SELECT role FROM users WHERE id = ?", (review["owner_id"],)
        ).fetchone()
        if owner_info and owner_info["role"] == "dept_head" and role != "superAdmin" and not is_company_head:
            raise HTTPException(403, "部门负责人的考核只能由超级管理员评分")

        # 权限校验：dept_head 只能评本部门普通成员
        if role == "dept_head" and not is_company_head:
            u_row = db.execute("SELECT department_id FROM users WHERE id = ?", (user["id"],)).fetchone()
            if not u_row or u_row["department_id"] != review["department_id"]:
                raise HTTPException(403, "只能对本部门成员进行评分")
        elif role == "admin" or is_company_head:
            if review["company_id"] != user["company_id"]:
                raise HTTPException(403, "只能对本公司成员进行评分")

        now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        db.execute(
            """UPDATE okr_monthly_reviews
               SET manager_score=?, manager_comment=?, manager_id=?,
                   manager_submitted_at=?, status='completed', updated_at=?
               WHERE id=?""",
            (req.manager_score, req.manager_comment, user["id"], now, now, review_id)
        )
        updated = dict_from_row(db.execute(
            "SELECT * FROM okr_monthly_reviews WHERE id = ?", (review_id,)
        ).fetchone())
    return {"ok": True, "review": updated}


@okr_router.get("/api/okr/reviews/monthly/summary")
async def monthly_review_summary(
    period: str = Query(...),
    dept_id: int = Query(None),
    company_id: int = Query(None),
    user: dict = Depends(get_current_user),
):
    """
    按权限返回成员月度分项及总分，保留部门/成员的汇总结构。
    普通成员仅返回自己，主管范围与成员总评接口一致。
    """
    with get_db() as db:
        member_evaluations = read_member_evaluations(db, period, user, dept_id, company_id)
    departments = {}
    for member in member_evaluations:
        did = member["department_id"] or 0
        department = departments.setdefault(did, {
            "department_id": did, "department_name": member["department_name"], "members": [],
        })
        department["members"].append({
            **member, "weighted_score": member["task_score"],
        })
    return {"period": period, "departments": list(departments.values())}


@okr_router.get("/api/okr/reviews/monthly/export")
async def monthly_review_export(
    period: str = Query(...),
    dept_id: int = Query(None),
    company_id: int = Query(None),
    user: dict = Depends(get_current_user),
):
    """Export existing objective rows and one weighted monthly total per member."""
    role = user["role"]
    is_company_head = bool(user.get("is_company_head"))
    if role not in ("dept_head", "admin", "superAdmin") and not is_company_head:
        raise HTTPException(403, "无权导出考核报表")
    # Reuse the existing objective export shape for existing consumers.
    with get_db() as db:
        members = read_member_evaluations(db, period, user, dept_id, company_id)
        sql = """
            SELECT r.*, o.title AS objective_title, o.progress, o.weight AS obj_weight,
                   o.type AS objective_type,
                   o.owner_id, o.department_id, o.company_id,
                   u.name AS owner_name,
                   d.name AS department_name,
                   c.name AS company_name
            FROM okr_monthly_reviews r
            JOIN okr_objectives o ON o.id = r.objective_id
            LEFT JOIN users u ON u.id = o.owner_id
            LEFT JOIN departments d ON d.id = o.department_id
            LEFT JOIN companies c ON c.id = o.company_id
            WHERE o.period = ? AND r.year_month = ?
        """
        params = [period, period]
        if role == "superAdmin":
            if company_id:
                sql += " AND o.company_id = ?"
                params.append(company_id)
        else:
            sql += " AND o.company_id = ?"
            params.append(user["company_id"])
        if role == "dept_head" and not is_company_head:
            sql += " AND o.department_id = ?"
            params.append(user.get("department_id"))
        if dept_id:
            sql += " AND o.department_id = ?"
            params.append(dept_id)
        sql += " ORDER BY o.department_id, o.owner_id, r.id"
        rows = dicts_from_rows(db.execute(sql, params).fetchall())
    summary_rows = []
    labels = {"completed": "已完成", "no_objectives": "未设置个人目标",
              "pending_tasks": "任务评估未完成", "pending_behavior": "待行为及红线评分"}
    monthly_members = members if re.fullmatch(r"[0-9]{4}-(?:0[1-9]|1[0-2])", period) else []
    for member in monthly_members:
        evaluation = member["evaluation"] or {}
        summary_rows.append({
            "部门": member["department_name"], "姓名": member["user_name"], "考核月份": period,
            "个人目标数": member["objective_count"], "已完成评估数": member["completed_objective_count"],
            "任务目标原始分": member["task_score"], "任务目标得分(70%)": member["task_contribution"],
            "满意度评分": evaluation.get("satisfaction_score"), "主动履职评分": evaluation.get("initiative_score"),
            "行为目标原始分": member["behavior_score"], "行为目标得分(30%)": member["behavior_contribution"],
            "红线系数": member["redline_factor"], "红线说明": evaluation.get("redline_reason", ""),
            "月度总分": member["total_score"], "月度状态": labels[member["status"]],
            "评分说明": evaluation.get("comment", ""), "评分人": evaluation.get("reviewer_name", ""),
        })
    export_rows = []
    personal_progress = {obj["objective_id"]: obj["kr_completion_rate"]
                         for member in members for obj in member["objectives"]}
    for r in rows:
        progress = personal_progress.get(r["objective_id"], r.get("progress", 0) or 0)
        composite = _calc_composite(progress, r.get("self_score"), r.get("manager_score"))
        export_rows.append({
            "部门": r.get("department_name") or r.get("company_name") or "",
            "姓名": r.get("owner_name", ""), "目标标题": r.get("objective_title", ""),
            "权重": r.get("obj_weight", 1), "考核月份": r.get("year_month", ""),
            "KR完成率": f"{round(progress, 1)}%", "自评分": r.get("self_score", ""),
            "自评说明": r.get("self_comment", ""), "主管评分": r.get("manager_score", ""),
            "主管评语": r.get("manager_comment", ""), "综合评分": composite if composite is not None else "",
            "评估状态": {"pending": "待自评", "self_done": "待主管评分", "completed": "已完成"}.get(r["status"], r["status"]),
        })
    return {"period": period, "rows": export_rows, "total": len(export_rows), "summary_rows": summary_rows}
