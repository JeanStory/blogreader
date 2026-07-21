"""极简登录鉴权：单密码 + HMAC 签名 token。

面向个人自用的跨端场景（PC/Android/iOS 共用一个 server）：
- 只有一个密码，通过环境变量 BLOGREADER_PASSWORD 配置；未配置则鉴权关闭（纯本地开发）。
- 登录成功后签发一个自包含的 token（无需存 DB），客户端存 localStorage，
  之后每个请求带 Authorization: Bearer <token>。
- token = base64(payload_json).hex(hmac_sha256(payload))，payload 里含过期时间。
- 签名密钥由密码派生（加固定盐），所以 server 重启后已签发的 token 仍然有效，
  改密码则旧 token 立即失效。
"""
from __future__ import annotations

import os
import hmac
import json
import time
import base64
import hashlib

# 未设置密码时鉴权关闭（本地开发直连），设置了才强制登录。
PASSWORD = os.environ.get("BLOGREADER_PASSWORD", "").strip()
# token 有效期（秒），默认 30 天。个人自用不用太短。
TOKEN_TTL = int(os.environ.get("BLOGREADER_TOKEN_TTL", str(30 * 24 * 3600)))

_SALT = b"blogreader.auth.v1"


def auth_enabled() -> bool:
    return bool(PASSWORD)


def _secret() -> bytes:
    """从密码派生签名密钥；密码变了签名就变，旧 token 自动失效。"""
    return hashlib.pbkdf2_hmac("sha256", PASSWORD.encode("utf-8"), _SALT, 100_000)


def _b64e(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def _b64d(s: str) -> bytes:
    pad = "=" * (-len(s) % 4)
    return base64.urlsafe_b64decode(s + pad)


def verify_password(password: str) -> bool:
    if not auth_enabled():
        return True
    return hmac.compare_digest(password or "", PASSWORD)


def issue_token(ttl: int | None = None) -> str:
    exp = int(time.time()) + (ttl if ttl is not None else TOKEN_TTL)
    payload = _b64e(json.dumps({"exp": exp}).encode("utf-8"))
    sig = hmac.new(_secret(), payload.encode("ascii"), hashlib.sha256).hexdigest()
    return f"{payload}.{sig}"


def verify_token(token: str) -> bool:
    if not auth_enabled():
        return True
    if not token:
        return False
    try:
        payload, sig = token.split(".", 1)
    except ValueError:
        return False
    expected = hmac.new(_secret(), payload.encode("ascii"), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(sig, expected):
        return False
    try:
        data = json.loads(_b64d(payload))
    except Exception:  # noqa: BLE001
        return False
    return int(data.get("exp", 0)) > int(time.time())


def token_from_header(authorization: str | None) -> str:
    """从 Authorization: Bearer <token> 取出 token。"""
    if not authorization:
        return ""
    parts = authorization.split(None, 1)
    if len(parts) == 2 and parts[0].lower() == "bearer":
        return parts[1].strip()
    return authorization.strip()
