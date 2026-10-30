"""Enterprise WeChat OAuth and organization helpers for the OKR module."""
from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urlencode

import httpx
from fastapi import HTTPException, Request

# httpx 的 INFO 日志包含完整查询参数；企业微信 Secret、access_token 和 OAuth code
# 都不能进入服务日志。
logging.getLogger("httpx").setLevel(logging.WARNING)


from .paths import CONFIG_PATH


def _load_config() -> dict[str, Any]:
    if not CONFIG_PATH.exists():
        return {}
    try:
        return json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}


def _env_or(config: dict[str, Any], env_name: str, key: str, default: str = "") -> str:
    return str(os.getenv(env_name) or config.get(key) or default).strip()


def _env_bool(config: dict[str, Any], env_name: str, key: str, default: bool = False) -> bool:
    raw = os.getenv(env_name)
    if raw is None:
        raw = config.get(key, default)
    if isinstance(raw, bool):
        return raw
    return str(raw).strip().lower() in {"1", "true", "yes", "on"}


@dataclass(frozen=True)
class OkrWecomConfig:
    enabled: bool
    corp_id: str
    agent_id: str
    app_secret: str
    callback_url: str
    corp_name: str
    company_id: int | None
    leader_role: str

    @property
    def configured(self) -> bool:
        return self.enabled and bool(self.corp_id and self.agent_id and self.app_secret)

    def validate(self) -> None:
        if not self.enabled:
            raise HTTPException(status_code=503, detail="OKR 企业微信登录未启用")
        missing = [
            name
            for name, value in (
                ("CorpID", self.corp_id),
                ("AgentID", self.agent_id),
                ("Secret", self.app_secret),
            )
            if not value
        ]
        if missing:
            raise HTTPException(status_code=503, detail=f"OKR 企业微信配置不完整，缺少：{', '.join(missing)}")


def get_okr_wecom_config() -> OkrWecomConfig:
    root = _load_config()
    config = ((root.get("okr") or {}).get("wecom") or {})
    raw_company_id = _env_or(config, "OKR_WECOM_COMPANY_ID", "company_id")
    try:
        company_id = int(raw_company_id) if raw_company_id else None
    except ValueError:
        company_id = None
    leader_role = _env_or(config, "OKR_WECOM_LEADER_ROLE", "leader_role", "superAdmin")
    if leader_role not in {"superAdmin", "admin", "dept_head"}:
        leader_role = "superAdmin"
    return OkrWecomConfig(
        enabled=_env_bool(config, "OKR_WECOM_ENABLED", "enabled", False),
        corp_id=_env_or(config, "OKR_WECOM_CORP_ID", "corp_id"),
        agent_id=_env_or(config, "OKR_WECOM_AGENT_ID", "agent_id"),
        app_secret=_env_or(config, "OKR_WECOM_APP_SECRET", "app_secret")
        or _env_or(config, "OKR_WECOM_SECRET", "secret"),
        callback_url=_env_or(config, "OKR_WECOM_CALLBACK_URL", "callback_url"),
        corp_name=_env_or(config, "OKR_WECOM_CORP_NAME", "corp_name", "企业微信组织"),
        company_id=company_id,
        leader_role=leader_role,
    )


def callback_url(request: Request) -> str:
    config = get_okr_wecom_config()
    if config.callback_url:
        return config.callback_url
    forwarded_proto = (request.headers.get("x-forwarded-proto") or request.url.scheme or "https").split(",")[0]
    return f"{forwarded_proto}://{request.url.netloc}/api/okr/auth/wecom/callback"


def choose_login_mode(request: Request, requested: str = "auto") -> str:
    if requested in {"mobile", "pc"}:
        return requested
    user_agent = (request.headers.get("user-agent") or "").lower()
    return "mobile" if "wxwork" in user_agent or "micromessenger" in user_agent else "pc"


def build_wecom_auth_url(request: Request, state: str, mode: str = "auto") -> str:
    config = get_okr_wecom_config()
    config.validate()
    selected = choose_login_mode(request, mode)
    redirect_uri = callback_url(request)
    if selected == "mobile":
        params = {
            "appid": config.corp_id,
            "redirect_uri": redirect_uri,
            "response_type": "code",
            "scope": "snsapi_base",
            "agentid": config.agent_id,
            "state": state,
        }
        return f"https://open.weixin.qq.com/connect/oauth2/authorize?{urlencode(params)}#wechat_redirect"
    params = {
        "login_type": "CorpApp",
        "appid": config.corp_id,
        "agentid": config.agent_id,
        "redirect_uri": redirect_uri,
        "state": state,
    }
    return f"https://login.work.weixin.qq.com/wwlogin/sso/login?{urlencode(params)}"


async def _access_token() -> str:
    config = get_okr_wecom_config()
    config.validate()
    async with httpx.AsyncClient(timeout=12) as client:
        response = await client.get(
            "https://qyapi.weixin.qq.com/cgi-bin/gettoken",
            params={"corpid": config.corp_id, "corpsecret": config.app_secret},
        )
    try:
        payload = response.json()
    except ValueError as exc:
        raise HTTPException(status_code=502, detail="企业微信 gettoken 返回了非 JSON 响应") from exc
    if payload.get("errcode") not in (0, None):
        raise HTTPException(status_code=502, detail=f"企业微信 gettoken 失败：{payload.get('errmsg')}")
    token = str(payload.get("access_token") or "")
    if not token:
        raise HTTPException(status_code=502, detail="企业微信 gettoken 未返回 access_token")
    return token


def _to_int_list(values: Any) -> list[int]:
    return [int(value) for value in (values or []) if str(value).isdigit()]


async def fetch_wecom_user(code: str) -> dict[str, Any]:
    if not code.strip():
        raise HTTPException(status_code=400, detail="企业微信授权 code 不能为空")
    token = await _access_token()
    async with httpx.AsyncClient(timeout=15) as client:
        info_response = await client.get(
            "https://qyapi.weixin.qq.com/cgi-bin/auth/getuserinfo",
            params={"access_token": token, "code": code},
        )
        info = info_response.json()
        userid = str(info.get("UserId") or info.get("userid") or "").strip()
        if not userid:
            raise HTTPException(status_code=502, detail=f"企业微信 getuserinfo 未返回 UserId：{info.get('errmsg')}")
        user_response = await client.get(
            "https://qyapi.weixin.qq.com/cgi-bin/user/get",
            params={"access_token": token, "userid": userid},
        )
        department_response = await client.get(
            "https://qyapi.weixin.qq.com/cgi-bin/department/list",
            params={"access_token": token},
        )
    user = user_response.json()
    if user.get("errcode") not in (0, None):
        raise HTTPException(status_code=502, detail=f"企业微信 user/get 失败：{user.get('errmsg')}")
    department_payload = department_response.json()
    if department_payload.get("errcode") not in (0, None):
        raise HTTPException(status_code=502, detail=f"企业微信 department/list 失败：{department_payload.get('errmsg')}")

    departments = {
        int(item["id"]): item
        for item in (department_payload.get("department") or [])
        if item.get("id") is not None and str(item.get("id")).isdigit()
    }
    department_ids = _to_int_list(user.get("department"))
    leader_flags = user.get("is_leader_in_dept") or []
    details: list[dict[str, Any]] = []
    for index, department_id in enumerate(department_ids):
        department = departments.get(department_id) or {}
        raw_flag = leader_flags[index] if index < len(leader_flags) else 0
        is_leader = bool(int(raw_flag)) if str(raw_flag).isdigit() else False
        details.append(
            {
                "id": department_id,
                "name": str(department.get("name") or department_id),
                "parentid": department.get("parentid"),
                "is_leader": is_leader,
            }
        )
    user["userid"] = userid
    user["department_details"] = details
    user["main_department"] = int(user.get("main_department")) if str(user.get("main_department")).isdigit() else None
    return user
