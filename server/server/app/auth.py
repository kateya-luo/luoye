import base64
import hashlib
import hmac
import json
import os
import secrets
import time
from collections import defaultdict, deque
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from fastapi import APIRouter, Depends, Header, HTTPException, Request, status
from pydantic import BaseModel, Field

router = APIRouter(prefix="/api/v1", tags=["auth"])
token_ttl_seconds = max(900, int(os.getenv("AUTH_TOKEN_TTL_SECONDS", "43200")))
failed_attempts: dict[str, deque[float]] = defaultdict(deque)

TEST_USERNAMES = ("TEST1", "TEST2", "TEST3", "TEST4", "TEST5")
DEFAULT_TEST_PASSWORD = os.getenv("TEST_ACCOUNT_PASSWORD", "123456")
_store: "UserStore | None" = None
_signing_key = secrets.token_bytes(32)


@dataclass(frozen=True)
class CurrentUser:
    id: str
    username: str
    role: str = "member"
    token_version: int = 1


class LoginRequest(BaseModel):
    username: str = ""
    password: str


class ChangePasswordRequest(BaseModel):
    current_password: str = Field(min_length=1, max_length=256)
    new_password: str = Field(min_length=8, max_length=128)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _normalize_username(value: str) -> str:
    return str(value or "").strip().upper()


def hash_password(password: str) -> str:
    """stdlib scrypt：密码只以带盐哈希入库，不保存明文。"""
    salt = secrets.token_bytes(16)
    digest = hashlib.scrypt(password.encode("utf-8"), salt=salt, n=2 ** 14, r=8, p=1, dklen=32)
    return f"scrypt$16384$8$1${_encode(salt)}${_encode(digest)}"


def verify_password(password: str, encoded: str) -> bool:
    try:
        algorithm, n, r, p, salt, expected = encoded.split("$", 5)
        if algorithm != "scrypt":
            return False
        actual = hashlib.scrypt(password.encode("utf-8"), salt=_decode(salt),
                                n=int(n), r=int(r), p=int(p), dklen=len(_decode(expected)))
        return hmac.compare_digest(actual, _decode(expected))
    except (ValueError, TypeError):
        return False


class UserStore:
    def __init__(self, db: Any) -> None:
        self.db = db

    def seed_test_users(self, password: str = DEFAULT_TEST_PASSWORD) -> None:
        for username in TEST_USERNAMES:
            if self.by_username(username) is None:
                now = _now()
                self.db.execute(
                    "INSERT INTO users(id,username,password_hash,role,active,token_version,created_at,updated_at)"
                    " VALUES(?,?,?,?,1,1,?,?)",
                    (username, username, hash_password(password), "member", now, now))

    def by_username(self, username: str) -> dict | None:
        row = self.db.query_one("SELECT * FROM users WHERE username=? COLLATE NOCASE",
                                (_normalize_username(username),))
        return dict(row) if row else None

    def by_id(self, user_id: str) -> dict | None:
        row = self.db.query_one("SELECT * FROM users WHERE id=?", (user_id,))
        return dict(row) if row else None

    def authenticate(self, username: str, password: str) -> CurrentUser | None:
        row = self.by_username(username)
        if not row or not row["active"] or not verify_password(password, row["password_hash"]):
            return None
        return CurrentUser(row["id"], row["username"], row["role"], int(row["token_version"]))

    def change_password(self, user_id: str, current_password: str,
                        new_password: str) -> CurrentUser | None:
        row = self.by_id(user_id)
        if not row or not verify_password(current_password, row["password_hash"]):
            return None
        cursor = self.db.execute(
            "UPDATE users SET password_hash=?, token_version=token_version+1, updated_at=? "
            "WHERE id=? AND password_hash=?",
            (hash_password(new_password), _now(), user_id, row["password_hash"]),
        )
        # 防止两个并发修改请求都用同一份旧密码成功。
        if cursor.rowcount != 1:
            return None
        updated = self.by_id(user_id)
        return CurrentUser(updated["id"], updated["username"], updated["role"],
                           int(updated["token_version"]))


def configure_auth(storage: Any, *, seed_test_users: bool = True) -> None:
    """绑定应用共用的 SQLite；在 main import 完成后调用，避免认证/WS 循环依赖。"""
    global _store, _signing_key
    _store = UserStore(storage.db)
    secret = os.getenv("AUTH_SECRET", "").strip()
    if not secret:
        secret = storage.db.get_meta("auth_secret")
        if not secret:
            secret = secrets.token_urlsafe(48)
            storage.db.set_meta("auth_secret", secret)
    _signing_key = hashlib.sha256(f"clearmeeting-auth:{secret}".encode()).digest()
    if seed_test_users:
        _store.seed_test_users()


def auth_required() -> bool:
    return _store is not None


def _encode(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


def _decode(value: str) -> bytes:
    return base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))


def create_token(user: CurrentUser) -> str:
    payload = {
        "sub": user.id,
        "username": user.username,
        "role": user.role,
        "ver": user.token_version,
        "exp": int(time.time()) + token_ttl_seconds,
        "nonce": secrets.token_urlsafe(12),
    }
    body = _encode(json.dumps(payload, separators=(",", ":")).encode())
    signature = _encode(hmac.new(_signing_key, body.encode(), hashlib.sha256).digest())
    return f"{body}.{signature}"


def token_user(token: str | None) -> CurrentUser | None:
    if not token or "." not in token:
        return None
    try:
        body, signature = token.split(".", 1)
        expected = _encode(hmac.new(_signing_key, body.encode(), hashlib.sha256).digest())
        if not hmac.compare_digest(signature, expected):
            return None
        payload = json.loads(_decode(body))
        if int(payload["exp"]) < int(time.time()):
            return None
        user = CurrentUser(str(payload["sub"]), str(payload["username"]),
                           str(payload.get("role", "member")), int(payload.get("ver", 1)))
        if _store is not None:
            row = _store.by_id(user.id)
            if not row or not row["active"] or int(row["token_version"]) != user.token_version:
                return None
            return CurrentUser(row["id"], row["username"], row["role"], int(row["token_version"]))
        return user
    except (ValueError, KeyError, TypeError, json.JSONDecodeError):
        return None


def verify_token(token: str | None) -> bool:
    return authenticated_user(token) is not None


def authenticated_user(token: str | None) -> CurrentUser | None:
    """HTTP query token / WebSocket 共用；未配置认证的纯单元测试保持旧行为。"""
    if not auth_required():
        return CurrentUser("TEST1", "TEST1")
    return token_user(token)


def bearer_token(authorization: str | None) -> str | None:
    if not authorization or not authorization.lower().startswith("bearer "):
        return None
    return authorization.split(" ", 1)[1].strip()


async def require_auth(authorization: str | None = Header(default=None)) -> CurrentUser:
    if not auth_required():
        return CurrentUser("TEST1", "TEST1")
    user = token_user(bearer_token(authorization))
    if user is None:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="登录已失效，请重新登录")
    return user


def _client_ip(request: Request) -> str:
    forwarded = request.headers.get("x-forwarded-for", "")
    # nginx 使用 $proxy_add_x_forwarded_for，直接客户 IP 在最右侧；取最左侧可被伪造头绕过限速。
    return forwarded.rsplit(",", 1)[-1].strip() or (request.client.host if request.client else "unknown")


def _is_rate_limited(client: str) -> bool:
    now = time.time()
    attempts = failed_attempts[client]
    while attempts and attempts[0] < now - 300:
        attempts.popleft()
    return len(attempts) >= 5


@router.get("/auth/status")
async def auth_status(authorization: str | None = Header(default=None)):
    user = token_user(bearer_token(authorization))
    return {
        "required": auth_required(),
        "authenticated": user is not None,
        "user": {"id": user.id, "username": user.username, "role": user.role} if user else None,
    }


@router.post("/auth/login")
async def login(payload: LoginRequest, request: Request):
    username = _normalize_username(payload.username)
    client = f"{_client_ip(request)}:{username}"
    if _is_rate_limited(client):
        raise HTTPException(status_code=status.HTTP_429_TOO_MANY_REQUESTS, detail="尝试次数过多，请稍后再试")
    user = _store.authenticate(username, payload.password) if _store is not None else None
    if user is None:
        failed_attempts[client].append(time.time())
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="用户名或密码错误")
    failed_attempts.pop(client, None)
    return {"token": create_token(user), "expires_in": token_ttl_seconds,
            "user": {"id": user.id, "username": user.username, "role": user.role}}


@router.get("/auth/verify")
async def verify(user: CurrentUser = Depends(require_auth)):
    return {"authenticated": True, "user": {"id": user.id, "username": user.username, "role": user.role}}


@router.post("/auth/change-password")
async def change_password(payload: ChangePasswordRequest,
                          user: CurrentUser = Depends(require_auth)):
    if payload.current_password == payload.new_password:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST,
                            detail="新密码不能与当前密码相同")
    updated = _store.change_password(user.id, payload.current_password, payload.new_password) \
        if _store is not None else None
    if updated is None:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="当前密码错误")
    return {
        "token": create_token(updated),
        "expires_in": token_ttl_seconds,
        "user": {"id": updated.id, "username": updated.username, "role": updated.role},
    }


@router.get("/me")
async def me(user: CurrentUser = Depends(require_auth)):
    return {"user": {"id": user.id, "username": user.username, "role": user.role}}
