"""轻量剪辑用户账户与会话支持。"""
from __future__ import annotations

import hashlib
import hmac
import os
import re
import secrets
import sqlite3
from pathlib import Path
from typing import Optional

from fastapi import HTTPException, Request

from app.config import settings

USERNAME_RE = re.compile(r"^[A-Za-z0-9_\u4e00-\u9fff]{3,24}$")
PHONE_RE = re.compile(r"^1[3-9]\d{9}$")
DEV_SMS_CODE = "123456"
ITERATIONS = 600_000


def _db_path() -> Path:
    path = settings.storage_path / "auth" / "users.sqlite3"
    path.parent.mkdir(parents=True, exist_ok=True)
    return path


def session_secret() -> str:
    path = settings.storage_path / "auth" / "session.secret"
    path.parent.mkdir(parents=True, exist_ok=True)
    if not path.exists():
        path.write_text(os.urandom(48).hex(), encoding="ascii")
    return path.read_text(encoding="ascii").strip()


def init_auth_db() -> None:
    with sqlite3.connect(_db_path()) as connection:
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS users (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                username TEXT NOT NULL UNIQUE COLLATE NOCASE,
                password_hash TEXT NOT NULL,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                phone TEXT UNIQUE
            )
            """
        )
        columns = {row[1] for row in connection.execute("PRAGMA table_info(users)")}
        if "phone" not in columns:
            connection.execute("ALTER TABLE users ADD COLUMN phone TEXT")
            connection.execute("CREATE UNIQUE INDEX IF NOT EXISTS users_phone_idx ON users(phone)")


def _hash_password(password: str, salt: Optional[bytes] = None) -> str:
    salt = salt or os.urandom(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode(), salt, ITERATIONS)
    return f"pbkdf2_sha256${ITERATIONS}${salt.hex()}${digest.hex()}"


def _verify_password(password: str, encoded: str) -> bool:
    try:
        algorithm, iterations, salt_hex, expected_hex = encoded.split("$", 3)
        if algorithm != "pbkdf2_sha256":
            return False
        actual = hashlib.pbkdf2_hmac("sha256", password.encode(), bytes.fromhex(salt_hex), int(iterations))
        return hmac.compare_digest(actual, bytes.fromhex(expected_hex))
    except (ValueError, TypeError):
        return False


def validate_phone_code(phone: str, code: str) -> None:
    if not PHONE_RE.fullmatch(phone.strip()):
        raise ValueError("请输入有效的11位手机号")
    if not hmac.compare_digest(code.strip(), DEV_SMS_CODE):
        raise ValueError("手机验证码不正确")


def _user_dict(row: sqlite3.Row | tuple) -> dict:
    return {"id": int(row[0]), "username": row[1], "phone": row[2]}


def create_user(username: str, phone: str, code: str, password: str) -> dict:
    username, phone = username.strip(), phone.strip()
    if not USERNAME_RE.fullmatch(username):
        raise ValueError("用户名须为3至24位中文、字母、数字或下划线")
    validate_phone_code(phone, code)
    if len(password) < 8 or len(password) > 128:
        raise ValueError("密码长度须为8至128位")
    try:
        with sqlite3.connect(_db_path()) as connection:
            cursor = connection.execute(
                "INSERT INTO users(username, phone, password_hash) VALUES (?, ?, ?)",
                (username, phone, _hash_password(password)),
            )
            user_id = cursor.lastrowid
    except sqlite3.IntegrityError as exc:
        message = "手机号已绑定其他账号" if "phone" in str(exc).lower() else "用户名已存在"
        raise ValueError(message) from exc
    return {"id": int(user_id), "username": username, "phone": phone}


def authenticate_user(account: str, password: str) -> Optional[dict]:
    with sqlite3.connect(_db_path()) as connection:
        row = connection.execute(
            "SELECT id, username, phone, password_hash FROM users WHERE username = ? OR phone = ?",
            (account.strip(), account.strip()),
        ).fetchone()
    if not row or not _verify_password(password, row[3]):
        return None
    return _user_dict(row)


def authenticate_phone(phone: str, code: str) -> Optional[dict]:
    validate_phone_code(phone, code)
    with sqlite3.connect(_db_path()) as connection:
        row = connection.execute(
            "SELECT id, username, phone FROM users WHERE phone = ?", (phone.strip(),)
        ).fetchone()
    return _user_dict(row) if row else None


def bind_phone(user_id: int, phone: str, code: str) -> dict:
    validate_phone_code(phone, code)
    try:
        with sqlite3.connect(_db_path()) as connection:
            connection.execute("UPDATE users SET phone = ? WHERE id = ?", (phone.strip(), user_id))
    except sqlite3.IntegrityError as exc:
        raise ValueError("手机号已绑定其他账号") from exc
    user = get_user(user_id)
    if not user:
        raise ValueError("用户不存在")
    return user


def update_profile(user_id: int, username: str, phone: str, code: str) -> dict:
    username, phone = username.strip(), phone.strip()
    if not USERNAME_RE.fullmatch(username):
        raise ValueError("用户名须为3至24位中文、字母、数字或下划线")
    current = get_user(user_id)
    if not current:
        raise ValueError("用户不存在")
    if phone != (current.get("phone") or ""):
        validate_phone_code(phone, code)
    try:
        with sqlite3.connect(_db_path()) as connection:
            connection.execute(
                "UPDATE users SET username = ?, phone = ? WHERE id = ?",
                (username, phone or None, user_id),
            )
    except sqlite3.IntegrityError as exc:
        message = "手机号已绑定其他账号" if "phone" in str(exc).lower() else "用户名已存在"
        raise ValueError(message) from exc
    return get_user(user_id) or current


def change_password(user_id: int, old_password: str, new_password: str) -> None:
    if len(new_password) < 8 or len(new_password) > 128:
        raise ValueError("新密码长度须为8至128位")
    with sqlite3.connect(_db_path()) as connection:
        row = connection.execute("SELECT password_hash FROM users WHERE id = ?", (user_id,)).fetchone()
        if not row or not _verify_password(old_password, row[0]):
            raise ValueError("当前密码不正确")
        connection.execute(
            "UPDATE users SET password_hash = ? WHERE id = ?",
            (_hash_password(new_password), user_id),
        )


def get_user(user_id: int) -> Optional[dict]:
    with sqlite3.connect(_db_path()) as connection:
        row = connection.execute("SELECT id, username, phone FROM users WHERE id = ?", (user_id,)).fetchone()
    return _user_dict(row) if row else None


def current_user(request: Request) -> Optional[dict]:
    user_id = request.session.get("user_id")
    return get_user(user_id) if isinstance(user_id, int) else None


def visitor_identity(request: Request) -> dict:
    user = current_user(request)
    if user:
        return {**user, "temporary": False}
    guest_id = request.session.get("guest_id")
    if not isinstance(guest_id, str):
        guest_id = f"游客-{secrets.token_hex(3).upper()}"
        request.session["guest_id"] = guest_id
    return {"id": None, "username": guest_id, "phone": None, "temporary": True}


def require_user(request: Request) -> dict:
    user = current_user(request)
    if not user:
        raise HTTPException(401, "视频链接剪辑仅限登录账号使用，请先登录或注册")
    return user
