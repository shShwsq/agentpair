"""用户相关请求/响应模型"""
import uuid
from datetime import datetime

from pydantic import BaseModel, EmailStr, Field


# ============================================================
# 请求模型
# ============================================================


class RegisterRequest(BaseModel):
    """邮箱密码注册"""

    email: EmailStr
    password: str = Field(min_length=8, max_length=128)


class LoginRequest(BaseModel):
    """邮箱密码登录"""

    email: EmailStr
    password: str


class RefreshRequest(BaseModel):
    """刷新 access token"""

    refresh_token: str


class VerifyEmailRequest(BaseModel):
    """邮箱验证(token)"""

    token: str


class ResendVerificationRequest(BaseModel):
    """重发验证邮件"""

    email: EmailStr


class ForgotPasswordRequest(BaseModel):
    """忘记密码(发重置邮件)"""

    email: EmailStr


class ResetPasswordRequest(BaseModel):
    """重置密码(token + 新密码)"""

    token: str
    new_password: str = Field(min_length=8, max_length=128)


class GitHubOAuthRequest(BaseModel):
    """GitHub OAuth 登录(传 code)"""

    code: str


# ============================================================
# 响应模型
# ============================================================


class UserResponse(BaseModel):
    """用户信息"""

    id: uuid.UUID
    email: str
    email_verified: bool
    github_id: str | None
    created_at: datetime

    model_config = {"from_attributes": True}

    @classmethod
    def from_user(cls, user) -> "UserResponse":
        """从 User ORM 模型构造(转 email_verified_at → bool)"""
        return cls(
            id=user.id,
            email=user.email,
            email_verified=user.is_email_verified,
            github_id=user.github_id,
            created_at=user.created_at,
        )


class TokenResponse(BaseModel):
    """登录成功响应"""

    access_token: str
    refresh_token: str
    token_type: str = "Bearer"
    user: UserResponse


class RefreshResponse(BaseModel):
    """刷新成功响应"""

    access_token: str
    token_type: str = "Bearer"


class MessageResponse(BaseModel):
    """通用消息响应"""

    message: str
