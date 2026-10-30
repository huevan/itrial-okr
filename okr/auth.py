"""
OKR 管理平台 —— 认证模块
JWT 签发/验证、密码哈希、FastAPI 依赖
"""
import json
import os
from datetime import datetime, timedelta, timezone
from typing import Optional, List

import bcrypt
import jwt
from fastapi import Depends, HTTPException, Request

from .db import get_db
from .paths import CONFIG_PATH

# ── 配置 ────────────────────────────────────────────────────────────────

_CFG_PATH = str(CONFIG_PATH)


def _load_config():
    with open(_CFG_PATH, "r", encoding="utf-8") as f:
        return json.load(f)


def _jwt_secret() -> str:
    return _load_config()["jwt_secret"]


def _jwt_expire_hours() -> int:
    return _load_config().get("jwt_expire_hours", 72)


# ── 密码 ────────────────────────────────────────────────────────────────

def hash_password(plain: str) -> str:
    return bcrypt.hashpw(plain.encode("utf-8"), bcrypt.gensalt()).decode("utf-8")


def verify_password(plain: str, hashed: str) -> bool:
    return bcrypt.checkpw(plain.encode("utf-8"), hashed.encode("utf-8"))


# ── JWT ─────────────────────────────────────────────────────────────────

def create_token(
    user_id: int,
    role: str,
    name: str,
    company_id: int = None,
    *,
    base_role: str = None,
) -> str:
    payload = {
        "sub": str(user_id),
        "role": role,
        "active_role": role,
        "base_role": base_role or role,
        "name": name,
        "exp": datetime.now(timezone.utc) + timedelta(hours=_jwt_expire_hours()),
    }
    if company_id is not None:
        payload["company_id"] = company_id
    return jwt.encode(payload, _jwt_secret(), algorithm="HS256")


def get_available_roles(user_id: int, base_role: str, department_id: int = None) -> List[str]:
    """Return the roles this account may actively use in the OKR module."""
    if base_role != "superAdmin":
        return [base_role]
    with get_db() as db:
        is_operational_head = db.execute(
            "SELECT 1 FROM departments WHERE head_user_id = ? "
            "UNION ALL SELECT 1 FROM companies WHERE head_user_id = ? LIMIT 1",
            (user_id, user_id),
        ).fetchone()
    if is_operational_head:
        return ["dept_head", "superAdmin"]
    return ["superAdmin"]


def get_default_role(available_roles: List[str]) -> str:
    """Prefer least privilege for a department head who is also a super admin."""
    if "dept_head" in available_roles:
        return "dept_head"
    return available_roles[0]


def decode_token(token: str) -> dict:
    try:
        payload = jwt.decode(token, _jwt_secret(), algorithms=["HS256"])
        payload["sub"] = int(payload["sub"])
        return payload
    except jwt.ExpiredSignatureError:
        raise HTTPException(status_code=401, detail="Token 已过期，请重新登录")
    except jwt.InvalidTokenError:
        raise HTTPException(status_code=401, detail="无效的 Token")


# ── FastAPI 依赖 ────────────────────────────────────────────────────────

async def get_authenticated_user(request: Request) -> dict:
    """验证 JWT，并从数据库读取当前角色、状态和手机号验证状态。"""
    auth = request.headers.get("Authorization", "")
    if not auth.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="未登录，请先登录")
    token = auth[7:]
    payload = decode_token(token)
    with get_db() as db:
        row = db.execute(
            "SELECT id, phone, name, role, company_id, department_id, status, "
            "COALESCE(phone_verified, 1) AS phone_verified, wecom_userid, "
            "EXISTS(SELECT 1 FROM companies c WHERE c.id = users.company_id "
            "AND c.head_user_id = users.id) AS is_company_head "
            "FROM users WHERE id = ?",
            (payload["sub"],),
        ).fetchone()
    if not row:
        raise HTTPException(status_code=401, detail="用户不存在或已被删除")
    if row["status"] == "disabled":
        raise HTTPException(status_code=403, detail="账号已被禁用，请联系管理员")
    if row["status"] == "pending":
        raise HTTPException(status_code=403, detail="账号尚未激活")
    available_roles = get_available_roles(
        row["id"], row["role"], row["department_id"]
    )
    active_role = payload.get("active_role") or get_default_role(available_roles)
    if active_role not in available_roles:
        raise HTTPException(status_code=401, detail="\u5f53\u524d\u89d2\u8272\u5df2\u5931\u6548\uff0c\u8bf7\u91cd\u65b0\u767b\u5f55")
    return {
        "id": row["id"],
        "phone": row["phone"],
        "phone_verified": bool(row["phone_verified"]),
        "wecom_userid": row["wecom_userid"],
        "role": active_role,
        "base_role": row["role"],
        "available_roles": available_roles,
        "name": row["name"],
        "company_id": row["company_id"],
        "department_id": row["department_id"],
        "is_company_head": bool(row["is_company_head"]),
    }


async def get_current_user(user: dict = Depends(get_authenticated_user)) -> dict:
    """业务接口统一要求企业微信账号已经完成短信手机号验证。"""
    if not user["phone_verified"]:
        raise HTTPException(status_code=403, detail="请先通过短信验证码绑定手机号")
    return user


def require_role(*roles: str):
    """角色权限检查依赖工厂"""
    async def _check(user: dict = Depends(get_current_user)):
        has_company_admin_access = bool(user.get("is_company_head")) and "admin" in roles
        if user["role"] not in roles and not has_company_admin_access:
            raise HTTPException(status_code=403, detail="权限不足")
        return user
    return _check
