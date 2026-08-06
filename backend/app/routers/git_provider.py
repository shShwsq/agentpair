"""Git 平台绑定与仓库访问路由(统一 GitHub / Gitee)

已登录用户在设置页"绑定 GitHub/Gitee"后,后端存加密的 access_token(scope 含仓库),
任务执行时解密传给 clone_repo 工具,用于克隆私有仓库。

端点(带 {provider} 路径参数,provider ∈ github|gitee):
- POST   /git/{provider}/bind        用授权码换 token 并加密落库(绑定/升级 scope)
- GET    /git/{provider}/status      查看当前用户该平台绑定状态
- DELETE /git/{provider}/bind        解绑(清 token,保留 provider_user_id 关联)
- GET    /git/{provider}/repos       列出当前用户该平台仓库(含私有)
- PATCH  /git/{provider}/sync-email  将账号邮箱同步为平台邮箱(仅支持可验证邮箱的平台)
"""
import logging
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel
from sqlalchemy.orm import Session

from app.database import get_db
from app.deps import get_current_user
from app.git_provider import GitProviderError, get_provider
from app.models.user import User
from app.models.user_git_binding import UserGitBinding
from app.security import decrypt_secret, encrypt_secret

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/git", tags=["git"])


# ============================================================
# 请求/响应模型
# ============================================================


class GitBindRequest(BaseModel):
    """绑定请求体:前端从 OAuth callback 拿到的 code"""

    code: str


class GitProviderStatusResponse(BaseModel):
    """绑定状态"""

    provider: str  # github / gitee
    bound: bool  # 是否已绑定(有 access_token)
    provider_user_id: str | None  # 平台用户 ID(可能登录时绑定但未授权 repo)
    provider_login: str | None  # 平台用户名(实时查 /user,失败则空)
    avatar_url: str | None  # 头像
    # 邮箱不一致提示(仅 bind 响应中可能为 true,status 查询恒为 false)
    email_mismatch: bool = False
    provider_email: str | None = None  # 平台可验证邮箱(不一致时用于弹窗展示)
    current_email: str | None = None  # 账号当前邮箱


class GitRepoItem(BaseModel):
    """仓库列表项(精简)"""

    full_name: str  # owner/repo
    name: str  # repo
    private: bool
    html_url: str
    clone_url: str
    default_branch: str


class GitReposResponse(BaseModel):
    repos: list[GitRepoItem]


class SyncEmailResponse(BaseModel):
    """邮箱同步结果"""

    email: str  # 更新后的邮箱
    email_verified: bool  # 是否已验证


# ============================================================
# 辅助
# ============================================================


def _resolve_provider(provider: str):
    """把路径参数转成 provider 实例,未知 id → 400"""
    try:
        return get_provider(provider)
    except GitProviderError as e:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail=str(e)
        ) from e


def _get_binding(db: Session, user_id, provider: str) -> UserGitBinding | None:
    return (
        db.query(UserGitBinding)
        .filter(
            UserGitBinding.user_id == user_id,
            UserGitBinding.provider == provider,
        )
        .first()
    )


# ============================================================
# 端点
# ============================================================


@router.post("/{provider}/bind", response_model=GitProviderStatusResponse)
def bind_provider(
    provider: str,
    req: GitBindRequest,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> GitProviderStatusResponse:
    """绑定 Git 平台:用 code 换 token,加密落库,更新 provider_user_id

    场景:
    - 密码注册用户首次绑定:新建 binding(provider_user_id + access_token)
    - OAuth 登录用户升级权限:binding 已存在,只更新 access_token
    """
    p = _resolve_provider(provider)

    try:
        access_token = p.exchange_code_for_token(req.code)
        info = p.get_user_info(access_token)
    except GitProviderError as e:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail=str(e)
        ) from e

    # provider_user_id 冲突检查:若已被其他用户占用,拒绝绑定
    if info.provider_user_id:
        existing = (
            db.query(UserGitBinding)
            .filter(
                UserGitBinding.provider == provider,
                UserGitBinding.provider_user_id == info.provider_user_id,
                UserGitBinding.user_id != current_user.id,
            )
            .first()
        )
        if existing:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail=f"{p.display_name} 账号已被其他用户绑定",
            )

    # upsert:同一用户同一 provider 只有一行
    binding = _get_binding(db, current_user.id, provider)
    if binding is None:
        binding = UserGitBinding(
            user_id=current_user.id,
            provider=provider,
            provider_user_id=info.provider_user_id,
            access_token=encrypt_secret(access_token),
        )
        db.add(binding)
    else:
        binding.provider_user_id = info.provider_user_id
        binding.access_token = encrypt_secret(access_token)
    db.commit()
    db.refresh(binding)

    logger.info(
        "user %s 绑定 %s 成功,provider_user_id=%s",
        current_user.id,
        p.display_name,
        info.provider_user_id,
    )

    # 邮箱不一致检测:仅支持可验证邮箱的平台才做
    provider_email = info.email
    if not provider_email and p.supports_verified_email:
        try:
            emails = p.get_user_emails(access_token)
            if emails:
                provider_email = emails[0]
        except GitProviderError:
            pass  # 拿不到就不做对比,不阻塞绑定
    email_mismatch = bool(
        provider_email and provider_email.lower() != current_user.email.lower()
    )

    return GitProviderStatusResponse(
        provider=provider,
        bound=True,
        provider_user_id=binding.provider_user_id,
        provider_login=info.login,
        avatar_url=info.avatar_url,
        email_mismatch=email_mismatch,
        provider_email=provider_email,
        current_email=current_user.email,
    )


@router.get("/{provider}/status", response_model=GitProviderStatusResponse)
def get_provider_status(
    provider: str,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> GitProviderStatusResponse:
    """查看当前用户该平台绑定状态

    bound=true 表示有 access_token(可访问私有仓库);
    provider_user_id 非空但 bound=false 表示仅用 OAuth 登录过(scope 不含仓库),
    未授权仓库访问,需重新绑定升级 scope。
    """
    p = _resolve_provider(provider)
    binding = _get_binding(db, current_user.id, provider)

    bound = bool(binding and binding.access_token)
    provider_login: str | None = None
    avatar_url: str | None = None

    # 若已绑定,实时查一次 /user 拿 login + avatar
    if bound:
        try:
            token = decrypt_secret(binding.access_token)  # type: ignore[arg-type]
            info = p.get_user_info(token)
            provider_login = info.login
            avatar_url = info.avatar_url
        except (GitProviderError, ValueError) as e:
            # token 失效或解密失败,只标记 bound 但不抛错
            logger.warning(
                "user %s 的 %s token 查询失败: %s",
                current_user.id,
                p.display_name,
                e,
            )

    return GitProviderStatusResponse(
        provider=provider,
        bound=bound,
        provider_user_id=binding.provider_user_id if binding else None,
        provider_login=provider_login,
        avatar_url=avatar_url,
    )


@router.delete("/{provider}/bind", response_model=GitProviderStatusResponse)
def unbind_provider(
    provider: str,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> GitProviderStatusResponse:
    """解绑:清除 access_token(保留 provider_user_id 行,因为 OAuth 登录仍可能用到)

    若想彻底解除账号关联,需走账号设置里的"删除账号"(随用户 CASCADE 删除)。
    """
    _resolve_provider(provider)
    binding = _get_binding(db, current_user.id, provider)
    if binding is not None:
        binding.access_token = ""
        db.commit()
        db.refresh(binding)
        logger.info("user %s 解绑 %s(清除 access_token)", current_user.id, provider)

    return GitProviderStatusResponse(
        provider=provider,
        bound=False,
        provider_user_id=binding.provider_user_id if binding else None,
        provider_login=None,
        avatar_url=None,
    )


@router.patch("/{provider}/sync-email", response_model=SyncEmailResponse)
def sync_email(
    provider: str,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> SyncEmailResponse:
    """将账号邮箱同步为平台可验证邮箱

    仅支持 supports_verified_email 的平台(GitHub)。Gitee 无可验证邮箱语义,
    调用返回 400。

    安全考虑:
    - 未绑定该平台 → 403
    - 平台不支持可验证邮箱 → 400
    - 拿不到平台 email → 400
    - 平台邮箱已被其他账号占用 → 409(不自动更新,避免账号合并风险)
    """
    p = _resolve_provider(provider)
    if not p.supports_verified_email:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"{p.display_name} 暂不支持邮箱同步",
        )

    binding = _get_binding(db, current_user.id, provider)
    if not binding or not binding.access_token:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=f"尚未绑定 {p.display_name},无法同步邮箱",
        )

    try:
        token = decrypt_secret(binding.access_token)
        info = p.get_user_info(token)
    except (GitProviderError, ValueError) as e:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"获取 {p.display_name} 用户信息失败: {e}",
        ) from e

    provider_email = info.email
    if not provider_email:
        try:
            emails = p.get_user_emails(token)
            if emails:
                provider_email = emails[0]
        except GitProviderError as e:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"获取 {p.display_name} 邮箱失败: {e}",
            ) from e

    if not provider_email:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"{p.display_name} 账号无可用 verified primary email",
        )

    # 邮箱一致,无需更新
    if provider_email.lower() == current_user.email.lower():
        return SyncEmailResponse(
            email=current_user.email,
            email_verified=current_user.is_email_verified,
        )

    # 查重:平台邮箱已被其他账号占用 → 拒绝,避免账号合并风险
    existing = (
        db.query(User)
        .filter(User.email == provider_email, User.id != current_user.id)
        .first()
    )
    if existing:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"该 {p.display_name} 邮箱已被其他账号占用,无法同步",
        )

    old_email = current_user.email
    current_user.email = provider_email
    # 可验证邮箱视为已验证
    if not current_user.email_verified_at:
        current_user.email_verified_at = datetime.now(timezone.utc)
    db.commit()
    db.refresh(current_user)

    logger.info(
        "user %s 同步邮箱: %s -> %s",
        current_user.id,
        old_email,
        provider_email,
    )

    return SyncEmailResponse(
        email=current_user.email,
        email_verified=current_user.is_email_verified,
    )


@router.get("/{provider}/repos", response_model=GitReposResponse)
def list_repos(
    provider: str,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> GitReposResponse:
    """列出当前用户该平台仓库(含私有)

    用于任务创建页下拉选择私有仓库。返回按更新时间倒序的前 100 个。
    """
    p = _resolve_provider(provider)
    binding = _get_binding(db, current_user.id, provider)
    if not binding or not binding.access_token:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=f"尚未绑定 {p.display_name} 或未授权仓库访问,请先在设置页绑定",
        )

    try:
        token = decrypt_secret(binding.access_token)
        raw = p.list_repos(token)
    except GitProviderError as e:
        # token 可能已失效,提示用户重新绑定
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail=f"{p.display_name} token 失效,请重新绑定: {e}",
        ) from e
    except ValueError as e:
        logger.error("user %s 的 %s token 解密失败: %s", current_user.id, provider, e)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="token 解密失败,请联系管理员",
        ) from e

    repos = [GitRepoItem(**item) for item in raw]
    return GitReposResponse(repos=repos)
