"""Tencent Cloud SMS adapter for OKR phone verification.

The signing and response-validation behavior mirrors the verified iMovieX
implementation while keeping credentials in environment variables/config.json.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import os
import re
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import httpx
from fastapi import HTTPException


from .paths import CONFIG_PATH
SMS_ENDPOINT = "https://sms.tencentcloudapi.com"
SMS_HOST = "sms.tencentcloudapi.com"
SMS_ACTION = "SendSms"
SMS_VERSION = "2021-01-11"


def _load_config() -> dict[str, Any]:
    if not CONFIG_PATH.exists():
        return {}
    try:
        return json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}


def _env_or(config: dict[str, Any], env_name: str, key: str, default: str = "") -> str:
    return str(os.getenv(env_name) or config.get(key) or default).strip()


def _template_params(value: str) -> list[str]:
    params = [item.strip() for item in value.split(",") if item.strip()]
    return params or ["code"]


@dataclass(frozen=True)
class TencentSmsConfig:
    secret_id: str
    secret_key: str
    sdk_app_id: str
    sign_name: str
    template_id: str
    template_params: tuple[str, ...]
    region: str
    ttl_minutes: int

    @property
    def configured(self) -> bool:
        return all(
            (
                self.secret_id,
                self.secret_key,
                self.sdk_app_id,
                self.sign_name,
                self.template_id,
            )
        )

    def validate(self) -> None:
        missing = [
            name
            for name, value in (
                ("TENCENTCLOUD_SECRET_ID", self.secret_id),
                ("TENCENTCLOUD_SECRET_KEY", self.secret_key),
                ("TENCENT_SMS_SDK_APP_ID", self.sdk_app_id),
                ("TENCENT_SMS_SIGN_NAME", self.sign_name),
                ("TENCENT_SMS_TEMPLATE_ID", self.template_id),
            )
            if not value
        ]
        if missing:
            raise HTTPException(status_code=503, detail=f"腾讯云短信配置不完整：{', '.join(missing)}")

    def build_template_param_set(self, code: str) -> list[str]:
        result: list[str] = []
        for item in self.template_params:
            if item in {"code", "{code}"}:
                result.append(code)
            elif item in {"ttl", "{ttl}", "minutes", "{minutes}", "ttl_minutes", "{ttl_minutes}"}:
                result.append(str(self.ttl_minutes))
            else:
                result.append(item)
        return result


def get_tencent_sms_config() -> TencentSmsConfig:
    root = _load_config()
    config = ((root.get("okr") or {}).get("sms") or root.get("tencent_sms") or {})
    raw_ttl = _env_or(config, "TENCENT_SMS_CODE_TTL_MINUTES", "code_ttl_minutes", "5")
    try:
        ttl_minutes = max(1, min(30, int(raw_ttl)))
    except ValueError:
        ttl_minutes = 5
    params = _env_or(config, "TENCENT_SMS_TEMPLATE_PARAMS", "template_params", "code")
    return TencentSmsConfig(
        secret_id=_env_or(config, "TENCENTCLOUD_SECRET_ID", "secret_id"),
        secret_key=_env_or(config, "TENCENTCLOUD_SECRET_KEY", "secret_key"),
        sdk_app_id=_env_or(config, "TENCENT_SMS_SDK_APP_ID", "sdk_app_id"),
        sign_name=_env_or(config, "TENCENT_SMS_SIGN_NAME", "sign_name"),
        template_id=_env_or(config, "TENCENT_SMS_TEMPLATE_ID", "template_id"),
        template_params=tuple(_template_params(params)),
        region=_env_or(config, "TENCENT_SMS_REGION", "region", "ap-guangzhou"),
        ttl_minutes=ttl_minutes,
    )


def _sha256_hex(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _hmac_sha256(key: bytes, value: bytes) -> bytes:
    return hmac.new(key, value, hashlib.sha256).digest()


def build_tencent_sms_headers(
    config: TencentSmsConfig,
    encoded_body: bytes,
    timestamp: int | None = None,
) -> dict[str, str]:
    timestamp = int(timestamp or time.time())
    date = datetime.fromtimestamp(timestamp, timezone.utc).strftime("%Y-%m-%d")
    content_type = "application/json; charset=utf-8"
    signed_headers = "content-type;host;x-tc-action"
    canonical_request = (
        "POST\n/\n\n"
        f"content-type:{content_type}\n"
        f"host:{SMS_HOST}\n"
        f"x-tc-action:{SMS_ACTION.lower()}\n\n"
        f"{signed_headers}\n"
        f"{_sha256_hex(encoded_body)}"
    )
    scope = f"{date}/sms/tc3_request"
    string_to_sign = (
        "TC3-HMAC-SHA256\n"
        f"{timestamp}\n"
        f"{scope}\n"
        f"{_sha256_hex(canonical_request.encode('utf-8'))}"
    )
    secret_date = _hmac_sha256(f"TC3{config.secret_key}".encode("utf-8"), date.encode("utf-8"))
    secret_service = _hmac_sha256(secret_date, b"sms")
    secret_signing = _hmac_sha256(secret_service, b"tc3_request")
    signature = _hmac_sha256(secret_signing, string_to_sign.encode("utf-8")).hex()
    authorization = (
        "TC3-HMAC-SHA256 "
        f"Credential={config.secret_id}/{scope}, "
        f"SignedHeaders={signed_headers}, Signature={signature}"
    )
    return {
        "Authorization": authorization,
        "Content-Type": content_type,
        "Host": SMS_HOST,
        "X-TC-Action": SMS_ACTION,
        "X-TC-Timestamp": str(timestamp),
        "X-TC-Version": SMS_VERSION,
        "X-TC-Region": config.region,
    }


def _validate_tencent_response(payload: Any) -> str:
    response = payload.get("Response") if isinstance(payload, dict) else None
    if not isinstance(response, dict):
        raise HTTPException(status_code=502, detail="腾讯云短信返回格式无效")
    error = response.get("Error")
    if isinstance(error, dict):
        code = str(error.get("Code") or "UnknownError")
        message = str(error.get("Message") or "短信请求失败")
        raise HTTPException(status_code=502, detail=f"腾讯云短信拒绝请求：{code}: {message}")
    statuses = response.get("SendStatusSet")
    if not isinstance(statuses, list) or not statuses:
        raise HTTPException(status_code=502, detail="腾讯云短信未返回发送状态")
    failed = next(
        (
            item
            for item in statuses
            if str((item or {}).get("Code") or "").lower() != "ok"
        ),
        None,
    )
    if failed:
        code = str(failed.get("Code") or "UnknownError")
        message = str(failed.get("Message") or "短信发送失败")
        raise HTTPException(status_code=502, detail=f"腾讯云短信发送失败：{code}: {message}")
    return str(response.get("RequestId") or "")


async def send_tencent_verification_code(phone: str, code: str) -> str:
    if not re.fullmatch(r"1\d{10}", phone):
        raise HTTPException(status_code=400, detail="请输入正确的11位手机号")
    if not re.fullmatch(r"\d{6}", code):
        raise HTTPException(status_code=500, detail="验证码生成失败")
    config = get_tencent_sms_config()
    config.validate()
    body = {
        "PhoneNumberSet": [f"+86{phone}"],
        "SmsSdkAppId": config.sdk_app_id,
        "SignName": config.sign_name,
        "TemplateId": config.template_id,
        "TemplateParamSet": config.build_template_param_set(code),
        "SessionContext": "iTrial OKR phone verification",
    }
    encoded = json.dumps(body, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    headers = build_tencent_sms_headers(config, encoded)
    try:
        async with httpx.AsyncClient(timeout=30) as client:
            response = await client.post(SMS_ENDPOINT, headers=headers, content=encoded)
    except httpx.HTTPError as exc:
        raise HTTPException(status_code=502, detail=f"腾讯云短信请求失败：{exc}") from exc
    try:
        payload = response.json()
    except ValueError as exc:
        raise HTTPException(status_code=502, detail="腾讯云短信返回了非 JSON 响应") from exc
    if response.status_code >= 400:
        raise HTTPException(status_code=502, detail=f"腾讯云短信 HTTP {response.status_code}")
    return _validate_tencent_response(payload)
