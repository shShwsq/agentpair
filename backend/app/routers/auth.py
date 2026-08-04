"""认证授权路由

路由:
- POST   /auth/register              邮箱密码注册
- POST   /auth/verify-email          验证邮箱
- POST   /auth/resend-verification   重发验证邮件
- POST   /auth/login                邮箱密码登录
- POST   /auth/refresh               刷新 access token
- GET    /auth/me                    当前用户信息(需登录)
- POST   /auth/password/forgot       忘记密码(发重置邮件)
- POST   /auth/password/reset        重置密码
- POST   /auth/password/change       修改密码(需登录)
- POST   /auth/oauth/github          GitHub OAuth 登录
- DELETE /auth/account               删除账号(硬删除,连带 task+配置+token)
- GET    /auth/me/trae-cli-pat       查询 TRAE CLI PAT 是否已设置
- PUT    /auth/me/trae-cli-pat       设置/更新 TRAE CLI PAT(加密存储)
- DELETE /auth/me/trae-cli-pat       清除 TRAE CLI PAT
"""
import logging
import uuid
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.orm import Session

from app.database import get_db
from app.deps import get_current_user
from app.email_service import (
    create_email_verification_token,
    create_password_reset_token,
    mark_token_used,
    send_password_reset_email,
    send_verification_email,
    verify_token,
)
from app.github_oauth import GitHubOAuthError, github_oauth_login
from app.models.email_token import EmailTokenType
from app.models.user import User
from app.schemas.user import (
    ChangePasswordRequest,
    DeleteAccountRequest,
    ForgotPasswordRequest,
    GitHubOAuthRequest,
    LoginRequest,
    MessageResponse,
    RefreshRequest,
    RefreshResponse,
    RegisterRequest,
    ResendVerificationRequest,
    ResetPasswordRequest,
    TokenResponse,
    TraeCLIPatRequest,
    TraeCLIPatStatus,
    UserResponse,
    VerifyEmailRequest,
)
from app.security import (
    TokenExpiredError,
    TokenInvalidError,
    create_token_pair,
    create_access_token,
    encrypt_secret,
    extract_user_id_from_token,
    hash_password,
    verify_password,
)

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/auth", tags=["auth"])


# ============================================================
# 注册
# ============================================================


@router.post("/register", response_model=MessageResponse, status_code=status.HTTP_201_CREATED)
def register(req: RegisterRequest, db: Session = Depends(get_db)) -> MessageResponse:
    """邮箱密码注册

    - 邮箱已存在 → 409
    - 注册成功 → 自动发验证邮件,返回 201
    """
    existing = db.query(User).filter(User.email == req.email).first()
    if existing:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="该邮箱已注册",
        )

    user = User(
        email=req.email,
        password_hash=hash_password(req.password),
    )
    db.add(user)
    db.flush()

    # 生成验证 token + 发邮件
    token_plain = create_email_verification_token(db, user.id)
    db.commit()

    # 发邮件(commit 后再发,避免事务失败但邮件已发)
    send_verification_email(user, token_plain)

    return MessageResponse(message="注册成功,请查收邮件验证邮箱")


# ============================================================
# 邮箱验证
# ============================================================


@router.post("/verify-email", response_model=MessageResponse)
def verify_email(req: VerifyEmailRequest, db: Session = Depends(get_db)) -> MessageResponse:
    """验证邮箱(token 来自注册邮件)"""
    token_record = verify_token(db, req.token, EmailTokenType.VERIFY_EMAIL)
    if not token_record:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="验证链接无效或已过期,请重新申请",
        )

    user = db.query(User).filter(User.id == token_record.user_id).first()
    if not user:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="用户不存在",
        )

    if user.is_email_verified:
        # 已验证,标记 token 已用(幂等)
        mark_token_used(db, token_record)
        db.commit()
        return MessageResponse(message="邮箱已验证(此前已完成)")

    user.email_verified_at = datetime.now(timezone.utc)
    mark_token_used(db, token_record)
    db.commit()

    return MessageResponse(message="邮箱验证成功,现在可以登录")


@router.post("/resend-verification", response_model=MessageResponse)
def resend_verification(
    req: ResendVerificationRequest, db: Session = Depends(get_db)
) -> MessageResponse:
    """重发验证邮件(若用户未验证)"""
    user = db.query(User).filter(User.email == req.email).first()
    if not user:
        # 不暴露邮箱是否注册,统一返回成功
        return MessageResponse(message="若该邮箱已注册且未验证,邮件已发送")
    if user.is_email_verified:
        return MessageResponse(message="若该邮箱已注册且未验证,邮件已发送")

    token_plain = create_email_verification_token(db, user.id)
    db.commit()
    send_verification_email(user, token_plain)

    return MessageResponse(message="若该邮箱已注册且未验证,邮件已发送")


# ============================================================
# 登录 / 刷新
# ============================================================


@router.post("/login", response_model=TokenResponse)
def login(req: LoginRequest, db: Session = Depends(get_db)) -> TokenResponse:
    """邮箱密码登录

    - 邮箱不存在 / 密码错 → 401(统一信息,防爆破枚举)
    - 未验证邮箱 → 403
    """
    user = db.query(User).filter(User.email == req.email).first()
    if not user or not verify_password(req.password, user.password_hash):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="邮箱或密码错误",
        )

    if not user.is_email_verified:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="邮箱未验证,请先查收验证邮件",
        )

    access, refresh = create_token_pair(user.id)
    return TokenResponse(
        access_token=access,
        refresh_token=refresh,
        user=UserResponse.from_user(user),
    )


@router.post("/refresh", response_model=RefreshResponse)
def refresh(req: RefreshRequest, db: Session = Depends(get_db)) -> RefreshResponse:
    """用 refresh token 换新的 access token"""
    try:
        user_id = extract_user_id_from_token(req.refresh_token, expected_type="refresh")
    except TokenExpiredError:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="refresh token 已过期,请重新登录",
        )
    except TokenInvalidError as e:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail=f"refresh token 无效: {e}",
        )

    # 校验用户仍存在
    user = db.query(User).filter(User.id == user_id).first()
    if not user:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="用户不存在",
        )

    return RefreshResponse(access_token=create_access_token(user.id))


# ============================================================
# 当前用户
# ============================================================


@router.get("/me", response_model=UserResponse)
def get_me(user: User = Depends(get_current_user)) -> UserResponse:
    """获取当前登录用户信息"""
    return UserResponse.from_user(user)


# ============================================================
# 忘记密码 / 重置密码
# ============================================================


@router.post("/password/forgot", response_model=MessageResponse)
def forgot_password(
    req: ForgotPasswordRequest, db: Session = Depends(get_db)
) -> MessageResponse:
    """忘记密码(发重置邮件)

    - 邮箱不存在也返回成功(防爆破枚举)
    """
    user = db.query(User).filter(User.email == req.email).first()
    if not user:
        return MessageResponse(message="若该邮箱已注册,重置邮件已发送")

    token_plain = create_password_reset_token(db, user.id)
    db.commit()
    send_password_reset_email(user, token_plain)

    return MessageResponse(message="若该邮箱已注册,重置邮件已发送")


@router.post("/password/reset", response_model=MessageResponse)
def reset_password(
    req: ResetPasswordRequest, db: Session = Depends(get_db)
) -> MessageResponse:
    """重置密码(token 来自重置邮件)"""
    token_record = verify_token(db, req.token, EmailTokenType.RESET_PASSWORD)
    if not token_record:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="重置链接无效或已过期,请重新申请",
        )

    user = db.query(User).filter(User.id == token_record.user_id).first()
    if not user:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="用户不存在",
        )

    user.password_hash = hash_password(req.new_password)
    mark_token_used(db, token_record)
    db.commit()

    return MessageResponse(message="密码重置成功,请用新密码登录")


@router.post("/password/change", response_model=MessageResponse)
def change_password(
    req: ChangePasswordRequest,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> MessageResponse:
    """修改密码(已登录用户)

    - 已有密码的用户:必须传 current_password 且匹配
    - OAuth 用户(password_hash 为空):可跳过 current_password 直接设置
    - 新密码不能与当前密码相同(已有密码时)
    """
    has_password = bool(user.password_hash)

    if has_password:
        if not req.current_password:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="请输入当前密码",
            )
        if not verify_password(req.current_password, user.password_hash):
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="当前密码错误",
            )
        if verify_password(req.new_password, user.password_hash):
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="新密码不能与当前密码相同",
            )

    user.password_hash = hash_password(req.new_password)
    db.commit()

    return MessageResponse(message="密码修改成功" if has_password else "密码设置成功")


# ============================================================
# 删除账号
# ============================================================


@router.delete("/account", response_model=MessageResponse)
def delete_account(
    req: DeleteAccountRequest,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> MessageResponse:
    """删除账号(硬删除,不可恢复)

    要求用户输入完整邮箱作为二次确认,后端校验匹配后执行硬删除:
    - 连带删除该用户的 task、user_llm_config(email_token 已 CASCADE)
    - 解除 GitHub 关联(github_id 释放,可被其他账号绑定)

    安全考虑:
    - 邮箱不匹配 → 400,不执行删除
    - 只删除当前登录用户,不影响他人
    """
    # 二次确认:输入的邮箱必须与当前账号邮箱完全一致(忽略大小写)
    if req.email.strip().lower() != user.email.lower():
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="邮箱不匹配,无法删除账号",
        )

    # 删 user 即可,所有关联数据由数据库外键 ondelete=CASCADE 自动级联删除:
    # user → tasks → conversations/results, user → user_llm_config, user → email_token
    db.delete(user)
    db.commit()

    logger.info("用户 %s (%s) 已删除账号", user.id, user.email)

    return MessageResponse(message="账号已删除")


# ============================================================
# GitHub OAuth
# ============================================================


@router.post("/oauth/github", response_model=TokenResponse)
def github_oauth(
    req: GitHubOAuthRequest, db: Session = Depends(get_db)
) -> TokenResponse:
    """GitHub OAuth 登录

    - code 换 access_token 换用户信息
    - github_id 已绑定 → 登录
    - github_id 未绑定,email 已注册 → 关联 github_id
    - github_id 未绑定,email 也未注册 → 自动创建账号(无密码)
    """
    try:
        gh_user = github_oauth_login(req.code)
    except GitHubOAuthError as e:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=str(e),
        )

    # 1. github_id 已绑定
    user = db.query(User).filter(User.github_id == gh_user.github_id).first()
    if not user and gh_user.email:
        # 2. email 已注册,关联 github_id
        user = db.query(User).filter(User.email == gh_user.email).first()
        if user:
            user.github_id = gh_user.github_id

    if not user:
        # 3. 完全新用户,自动创建(无密码,只走 OAuth)
        if not gh_user.email:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="GitHub 账号无可用邮箱,无法自动注册",
            )
        user = User(
            email=gh_user.email,
            password_hash="",  # OAuth 用户无密码
            github_id=gh_user.github_id,
            # OAuth 已隐含邮箱验证(GitHub 已 verified)
            email_verified_at=datetime.now(timezone.utc),
        )
        db.add(user)
        db.flush()

    db.commit()

    access, refresh = create_token_pair(user.id)
    return TokenResponse(
        access_token=access,
        refresh_token=refresh,
        user=UserResponse.from_user(user),
    )


# ============================================================
# TRAE CLI PAT 管理(账户设置页)
# ============================================================


@router.get("/me/trae-cli-pat", response_model=TraeCLIPatStatus)
def get_trae_cli_pat_status(
    user: User = Depends(get_current_user),
) -> TraeCLIPatStatus:
    """查询 TRAE CLI PAT 是否已设置(不暴露实际值)"""
    return TraeCLIPatStatus(has_pat=bool(user.trae_cli_pat))


@router.put("/me/trae-cli-pat", response_model=TraeCLIPatStatus)
def set_trae_cli_pat(
    req: TraeCLIPatRequest,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> TraeCLIPatStatus:
    """设置/更新 TRAE CLI PAT

    PAT 加密后存入 user.trae_cli_pat(Fernet base64 密文)。
    传空字符串等价于清除 PAT。
    """
    pat = req.pat.strip()
    if pat:
        user.trae_cli_pat = encrypt_secret(pat)
    else:
        user.trae_cli_pat = ""
    db.commit()
    logger.info("用户 %s 更新了 TRAE CLI PAT(已%s)", user.id, "设置" if pat else "清除")
    return TraeCLIPatStatus(has_pat=bool(pat))


@router.delete("/me/trae-cli-pat", response_model=TraeCLIPatStatus)
def clear_trae_cli_pat(
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> TraeCLIPatStatus:
    """清除 TRAE CLI PAT"""
    user.trae_cli_pat = ""
    db.commit()
    logger.info("用户 %s 清除了 TRAE CLI PAT", user.id)
    return TraeCLIPatStatus(has_pat=False)
