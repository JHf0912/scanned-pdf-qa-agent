"""登录与两级权限（普通用户 / 管理员）。

账号存放在本地 JSON（默认 `data/users.json`），密码用 PBKDF2-HMAC-SHA256 +
每用户随机盐哈希，不引入第三方依赖。会话由 API 层用 HttpOnly Cookie 承载。
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import secrets
import time
from contextvars import ContextVar
from dataclasses import dataclass
from pathlib import Path

ROLE_USER = "user"
ROLE_ADMIN = "admin"
ROLES = frozenset({ROLE_USER, ROLE_ADMIN})

_ITERATIONS = 200_000
MIN_PASSWORD_LEN = 8
MIN_USERNAME_LEN = 2
MAX_USERNAME_LEN = 32


class AuthError(ValueError):
    """用户可见的认证/账号错误（消息可直接回显）。"""


@dataclass(frozen=True)
class User:
    username: str
    role: str
    disabled: bool = False

    def to_dict(self) -> dict:
        return {"username": self.username, "role": self.role, "disabled": self.disabled}


# ---------------------------------------------------------------- 当前请求用户
#
# 用 ContextVar 承载"本次请求是谁"，让 _discover()/state 这类闭包按用户隔离。
_current_user: ContextVar[dict | None] = ContextVar("docqa_current_user", default=None)


def set_current_user(user: dict | None) -> None:
    _current_user.set(user)


def current_user() -> dict | None:
    return _current_user.get()


def is_admin() -> bool:
    u = current_user()
    return bool(u and u.get("role") == ROLE_ADMIN)


def require_admin() -> None:
    if not is_admin():
        raise PermissionError("需要管理员权限")


# ---------------------------------------------------------------- 哈希

def _hash_pw(pw: str, salt: str) -> str:
    return hashlib.pbkdf2_hmac(
        "sha256", pw.encode("utf-8"), bytes.fromhex(salt), _ITERATIONS
    ).hex()


def validate_username(username: str) -> str:
    u = (username or "").strip()
    if not (MIN_USERNAME_LEN <= len(u) <= MAX_USERNAME_LEN):
        raise AuthError(f"用户名长度需 {MIN_USERNAME_LEN}-{MAX_USERNAME_LEN} 个字符")
    if not all(ch.isalnum() or ch in "._-" for ch in u):
        raise AuthError("用户名只能包含字母、数字、点、下划线、连字符")
    return u


def validate_pw(pw: str) -> str:
    if not isinstance(pw, str) or len(pw) < MIN_PASSWORD_LEN:
        raise AuthError(f"密码至少 {MIN_PASSWORD_LEN} 位")
    return pw


class UserStore:
    """本地 JSON 账号库。原子写盘，读时容错。"""

    def __init__(self, path: Path):
        self.path = Path(path)
        self._users: dict[str, dict] = {}
        self._load()

    def _load(self) -> None:
        if not self.path.exists():
            return
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return
        self._users = data.get("users") or {}

    def _save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(self.path.suffix + ".tmp")
        tmp.write_text(
            json.dumps({"users": self._users}, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        os.replace(tmp, self.path)

    # ------------------------------------------------------------ 查询
    def list_users(self) -> list[User]:
        return [
            User(u, d.get("role", ROLE_USER), bool(d.get("disabled")))
            for u, d in sorted(self._users.items())
        ]

    def get(self, username: str) -> dict | None:
        d = self._users.get(username)
        if not d:
            return None
        return {
            "username": username,
            "role": d.get("role", ROLE_USER),
            "disabled": bool(d.get("disabled")),
            "created_at": d.get("created_at"),
        }

    def verify(self, username: str, pw: str) -> dict | None:
        d = self._users.get(username)
        if not d or d.get("disabled"):
            return None
        salt = d.get("salt") or ""
        stored = d.get("hash") or ""
        if not salt or not stored:
            return None
        if hmac.compare_digest(_hash_pw(pw, salt), stored):
            return {"username": username, "role": d.get("role", ROLE_USER)}
        return None

    # ------------------------------------------------------------ 变更
    def create(self, username: str, pw: str, role: str = ROLE_USER) -> dict:
        u = validate_username(username)
        validate_pw(pw)
        if role not in ROLES:
            raise AuthError("非法角色")
        if u in self._users:
            raise AuthError("用户已存在")
        salt = secrets.token_hex(16)
        self._users[u] = {
            "salt": salt,
            "hash": _hash_pw(pw, salt),
            "role": role,
            "disabled": False,
            "created_at": int(time.time()),
        }
        self._save()
        return self.get(u)

    def set_role(self, username: str, role: str) -> dict:
        if role not in ROLES:
            raise AuthError("非法角色")
        d = self._require(username)
        if d.get("role") == ROLE_ADMIN and role != ROLE_ADMIN and self._admins() <= 1:
            raise AuthError("不能降级最后一个管理员")
        d["role"] = role
        self._save()
        return self.get(username)

    def set_pw(self, username: str, pw: str) -> None:
        validate_pw(pw)
        d = self._require(username)
        salt = secrets.token_hex(16)
        d["salt"] = salt
        d["hash"] = _hash_pw(pw, salt)
        self._save()

    def set_disabled(self, username: str, disabled: bool) -> dict:
        d = self._require(username)
        if disabled and d.get("role") == ROLE_ADMIN and self._enabled_admins() <= 1:
            raise AuthError("不能禁用最后一个启用中的管理员")
        d["disabled"] = bool(disabled)
        self._save()
        return self.get(username)

    def delete(self, username: str) -> None:
        d = self._require(username)
        if d.get("role") == ROLE_ADMIN and self._admins() <= 1:
            raise AuthError("不能删除最后一个管理员")
        del self._users[username]
        self._save()

    # ------------------------------------------------------------ 内部
    def _require(self, username: str) -> dict:
        d = self._users.get(username)
        if not d:
            raise AuthError("用户不存在")
        return d

    def _admins(self) -> int:
        return sum(1 for d in self._users.values() if d.get("role") == ROLE_ADMIN)

    def _enabled_admins(self) -> int:
        return sum(
            1
            for d in self._users.values()
            if d.get("role") == ROLE_ADMIN and not d.get("disabled")
        )


def ensure_admin(store: UserStore, env_pw: str | None = None) -> str | None:
    """确保至少有一个启用中的管理员。

    返回新建 admin 的初始密码（仅当本次真的创建了 admin），否则 None。
    密码优先取环境变量，否则随机生成——绝不留 admin/admin 这种默认口令。
    """
    if any(u.role == ROLE_ADMIN for u in store.list_users()):
        return None
    pw = env_pw or secrets.token_urlsafe(12)
    store.create("admin", pw, ROLE_ADMIN)
    return pw
