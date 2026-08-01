"""GitHub 绑定与仓库访问路由

已登录用户在设置页"绑定 GitHub"后,后端存加密的 access_token(scope=repo),
任务执行时解密传给 clone_repo 工具,用于克隆私有仓库。

端点:
- POST /github/bind         用授权码换取 token 并加密落库(绑定/升级 scope)
- GET  /github/status       查看当前用户 GitHub 绑定状态
- DELETE /github/bind       解绑(清除 token,保留 github_id 关联)
- GET  /github/repos        列出当前用户 GitHub 仓库(含私有)
"""
import logging

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel
from sqlalchemy.orm import Session

from app.database import get_db
from app.deps import get_current_user
from app.github_oauth import (
    GitHubOAuthError,
    GitHubUserInfo,
    get_github_user_info,
    list_github_repos,
)
from app.models.user import User
from app.security import decrypt_secret, encrypt_secret

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/github", tags=["github"])


# ============================================================
# 请求/响应模型
# ============================================================


class GitHubBindRequest(BaseModel):
    """绑定请求体:前端从 OAuth callback 拿到的 code"""

    code: str


class GitHubStatusResponse(BaseModel):
    """绑定状态"""

    bound: bool  # 是否已绑定(有 access_token)
    github_id: str | None  # GitHub 用户 ID(可能登录时绑定但未授权 repo)
    github_login: str | None  # GitHub 用户名(实时查 /user,失败则空)
    avatar_url: str | None  # 头像


class GitHubRepoItem(BaseModel):
    """仓库列表项(精简)"""

    full_name: str  # owner/repo
    name: str  # repo
    private: bool
    html_url: str
    clone_url: str
    default_branch: str


class GitHubReposResponse(BaseModel):
    repos: list[GitHubRepoItem]


# ============================================================
# 端点
# ============================================================


@router.post("/bind", response_model=GitHubStatusResponse)
def bind_github(
    req: GitHubBindRequest,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> GitHubStatusResponse:
    """绑定 GitHub:用 code 换 token,加密落库,更新 github_id

    场景:
    - 密码注册用户首次绑定:写入 github_id + access_token
    - GitHub OAuth 登录用户升级权限:github_id 已存在,只更新 access_token
    """
    from app.github_oauth import exchange_code_for_token

    try:
        access_token = exchange_code_for_token(req.code)
        info: GitHubUserInfo = get_github_user_info(access_token)
    except GitHubOAuthError as e:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=str(e),
        ) from e

    # github_id 冲突检查:若已被其他用户占用,拒绝绑定
    if info.github_id:
        existing = (
            db.query(User)
            .filter(User.github_id == info.github_id, User.id != current_user.id)
            .first()
        )
        if existing:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail=f"GitHub 账号已被其他用户绑定(github_id={info.github_id})",
            )

    # 写入:加密 token + github_id(若空)
    current_user.github_access_token = encrypt_secret(access_token)
    if not current_user.github_id:
        current_user.github_id = info.github_id
    db.commit()
    db.refresh(current_user)

    logger.info(
        "user %s 绑定 GitHub 成功,github_id=%s",
        current_user.id,
        info.github_id,
    )

    return GitHubStatusResponse(
        bound=True,
        github_id=current_user.github_id,
        github_login=info.name,
        avatar_url=info.avatar_url,
    )


@router.get("/status", response_model=GitHubStatusResponse)
def get_github_status(
    current_user: User = Depends(get_current_user),
) -> GitHubStatusResponse:
    """查看当前用户 GitHub 绑定状态

    bound=true 表示有 access_token(可访问私有仓库);
    github_id 非空但 bound=false 表示仅用 OAuth 登录过(scope=user:email),
    未授权仓库访问,需重新绑定升级 scope。
    """
    bound = bool(current_user.github_access_token)
    github_login: str | None = None
    avatar_url: str | None = None

    # 若已绑定,实时查一次 /user 拿 login + avatar
    if bound:
        try:
            token = decrypt_secret(current_user.github_access_token)
            info = get_github_user_info(token)
            github_login = info.name
            avatar_url = info.avatar_url
        except (GitHubOAuthError, ValueError) as e:
            # token 失效或解密失败,只标记 bound 但不抛错
            logger.warning("user %s 的 GitHub token 查询失败: %s", current_user.id, e)

    return GitHubStatusResponse(
        bound=bound,
        github_id=current_user.github_id,
        github_login=github_login,
        avatar_url=avatar_url,
    )


@router.delete("/bind", response_model=GitHubStatusResponse)
def unbind_github(
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> GitHubStatusResponse:
    """解绑 GitHub:清除 access_token

    保留 github_id(因为这是账号关联标识,OAuth 登录仍可能用到);
    若想彻底解除账号关联,需走账号设置里的"删除账号"。
    """
    current_user.github_access_token = ""
    db.commit()
    db.refresh(current_user)

    logger.info("user %s 解绑 GitHub(清除 access_token)", current_user.id)

    return GitHubStatusResponse(
        bound=False,
        github_id=current_user.github_id,
        github_login=None,
        avatar_url=None,
    )


@router.get("/repos", response_model=GitHubReposResponse)
def list_repos(
    current_user: User = Depends(get_current_user),
) -> GitHubReposResponse:
    """列出当前用户 GitHub 仓库(含私有)

    用于任务创建页下拉选择私有仓库。返回按更新时间倒序的前 100 个。
    """
    if not current_user.github_access_token:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="尚未绑定 GitHub 或未授权仓库访问,请先在设置页绑定",
        )

    try:
        token = decrypt_secret(current_user.github_access_token)
        raw = list_github_repos(token)
    except GitHubOAuthError as e:
        # token 可能已失效,提示用户重新绑定
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail=f"GitHub token 失效,请重新绑定: {e}",
        ) from e
    except ValueError as e:
        logger.error("user %s 的 GitHub token 解密失败: %s", current_user.id, e)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="token 解密失败,请联系管理员",
        ) from e

    repos = [GitHubRepoItem(**item) for item in raw]
    return GitHubReposResponse(repos=repos)
