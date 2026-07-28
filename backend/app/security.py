"""安全工具:密码哈希 + JWT 签发/校验

双 token 设计:
- access_token: 短期(15 分钟),用于 API 鉴权
- refresh_token: 长期(7 天),仅用于刷新 access_token

JWT payload:
- sub: 用户 ID(str)
- type: "access" | "refresh"
- iat: 签发时间
- exp: 过期时间
"""
import uuid
from datetime import datetime, timedelta, timezone

import bcrypt
import jwt as pyjwt

from app.config import settings


# ============================================================
# 密码哈希
# ============================================================


def hash_password(password: str) -> str:
    """bcrypt 加盐哈希

    返回值含 salt 和 hash,可直接存数据库
    """
    salt = bcrypt.gensalt(rounds=12)
    return bcrypt.hashpw(password.encode("utf-8"), salt).decode("utf-8")


def verify_password(password: str, password_hash: str) -> bool:
    """校验密码与哈希是否匹配"""
    try:
        return bcrypt.checkpw(
            password.encode("utf-8"), password_hash.encode("utf-8")
        )
    except (ValueError, TypeError):
        return False


# ============================================================
# JWT 签发
# ============================================================


def _create_token(
    user_id: uuid.UUID,
    token_type: str,
    expires_delta: timedelta,
) -> str:
    """内部:签发单个 JWT"""
    now = datetime.now(timezone.utc)
    payload = {
        "sub": str(user_id),
        "type": token_type,
        "iat": int(now.timestamp()),
        "exp": int((now + expires_delta).timestamp()),
    }
    return pyjwt.encode(payload, settings.JWT_SECRET, algorithm=settings.JWT_ALGORITHM)


def create_access_token(user_id: uuid.UUID) -> str:
    """签发 access token(短期)"""
    return _create_token(user_id, "access", timedelta(minutes=15))


def create_refresh_token(user_id: uuid.UUID) -> str:
    """签发 refresh token(长期 7 天)"""
    return _create_token(user_id, "refresh", timedelta(days=7))


def create_token_pair(user_id: uuid.UUID) -> tuple[str, str]:
    """签发 access + refresh 一对 token"""
    return create_access_token(user_id), create_refresh_token(user_id)


# ============================================================
# JWT 校验
# ============================================================


class TokenError(Exception):
    """token 校验失败基类"""


class TokenExpiredError(TokenError):
    """token 已过期"""


class TokenInvalidError(TokenError):
    """token 无效(签名错/格式错/类型错)"""


def decode_token(token: str, expected_type: str | None = None) -> dict:
    """解码并校验 JWT

    参数:
        token: JWT 字符串
        expected_type: 期望类型("access" / "refresh"),None 不校验

    抛出:
        TokenExpiredError: token 已过期
        TokenInvalidError: token 无效(签名错/格式错/类型错)

    返回:payload dict,含 sub / type / iat / exp
    """
    try:
        payload = pyjwt.decode(
            token,
            settings.JWT_SECRET,
            algorithms=[settings.JWT_ALGORITHM],
        )
    except pyjwt.ExpiredSignatureError as e:
        raise TokenExpiredError("token 已过期") from e
    except pyjwt.InvalidTokenError as e:
        raise TokenInvalidError(f"token 无效: {e}") from e

    if expected_type and payload.get("type") != expected_type:
        raise TokenInvalidError(
            f"token 类型错误:期望 {expected_type},实际 {payload.get('type')}"
        )

    return payload


def extract_user_id_from_token(
    token: str, expected_type: str | None = None
) -> uuid.UUID:
    """从 token 中提取用户 ID"""
    payload = decode_token(token, expected_type)
    try:
        return uuid.UUID(payload["sub"])
    except (KeyError, ValueError) as e:
        raise TokenInvalidError(f"token payload 缺少 sub 或格式错: {e}") from e
