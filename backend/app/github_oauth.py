"""GitHub OAuth 集成

两套流程,共用授权码模式:
- 登录流程(scope=user:email):仅拿 github_id + email,用于登录/创建账号
- 绑定流程(scope=user:email repo):额外拿 access_token 落库,用于克隆私有仓库

登录流程:
1. 前端跳到 https://github.com/login/oauth/authorize?...&scope=user:email
2. 用户授权后 GitHub 回跳到 redirect_uri?code=XXX
3. 前端把 code 提交到后端 POST /auth/oauth/github { code }
4. 后端用 code 换 access_token,再用 access_token 调 /user 拿 github_id + email
5. 若 github_id 已绑定用户 → 登录该用户
6. 若未绑定且 email 已注册 → 关联 github_id
7. 若未绑定且 email 未注册 → 自动创建账号(无密码,只走 OAuth)

绑定流程(已登录用户在设置页发起):
1. 前端跳到 authorize?...&scope=user:email repo
2. GitHub 回跳 redirect_uri?code=XXX
3. 前端把 code 提交到 POST /github/bind { code }
4. 后端换 token + 取 /user 信息,加密存 access_token,更新 github_id
"""
import logging

import httpx

from app.config import settings

logger = logging.getLogger(__name__)


GITHUB_TOKEN_URL = "https://github.com/login/oauth/access_token"
GITHUB_USER_URL = "https://api.github.com/user"
GITHUB_REPOS_URL = "https://api.github.com/user/repos"


# OAuth scope 常量
SCOPE_LOGIN = "user:email"  # 登录用,只取邮箱
SCOPE_BIND = "user:email repo"  # 绑定用,额外申请仓库访问权限


class GitHubOAuthError(Exception):
    """GitHub OAuth 错误"""


class GitHubUserInfo:
    """GitHub 用户信息(从 /user 接口拿到)"""

    def __init__(self, github_id: str, email: str | None, name: str | None, avatar_url: str | None):
        self.github_id = github_id
        self.email = email
        self.name = name
        self.avatar_url = avatar_url


def exchange_code_for_token(code: str) -> str:
    """用授权码换 access_token

    抛出 GitHubOAuthError 表示失败
    """
    if not settings.GITHUB_OAUTH_CLIENT_ID or not settings.GITHUB_OAUTH_CLIENT_SECRET:
        raise GitHubOAuthError(
            "GitHub OAuth 未配置:请在 .env 设置 GITHUB_OAUTH_CLIENT_ID 和 GITHUB_OAUTH_CLIENT_SECRET"
        )

    payload = {
        "client_id": settings.GITHUB_OAUTH_CLIENT_ID,
        "client_secret": settings.GITHUB_OAUTH_CLIENT_SECRET,
        "code": code,
        "redirect_uri": settings.GITHUB_OAUTH_REDIRECT_URI,
    }
    headers = {"Accept": "application/json"}

    try:
        with httpx.Client(timeout=10) as client:
            r = client.post(GITHUB_TOKEN_URL, data=payload, headers=headers)
            r.raise_for_status()
    except httpx.HTTPError as e:
        raise GitHubOAuthError(f"换取 access_token 失败: {e}") from e

    data = r.json()
    access_token = data.get("access_token")
    if not access_token:
        err = data.get("error_description") or data.get("error") or "未知错误"
        raise GitHubOAuthError(f"换取 access_token 失败: {err}")

    return access_token


def get_github_user_info(access_token: str) -> GitHubUserInfo:
    """用 access_token 调 /user 拿 GitHub 用户信息

    抛出 GitHubOAuthError 表示失败
    """
    headers = {
        "Authorization": f"Bearer {access_token}",
        "Accept": "application/vnd.github+json",
    }

    try:
        with httpx.Client(timeout=10) as client:
            r = client.get(GITHUB_USER_URL, headers=headers)
            r.raise_for_status()
    except httpx.HTTPError as e:
        raise GitHubOAuthError(f"获取用户信息失败: {e}") from e

    data = r.json()
    github_id = str(data.get("id") or "")
    if not github_id:
        raise GitHubOAuthError("GitHub 用户信息缺少 id 字段")

    # email 可能是 private,需另外调 /user/emails,这里先取公开 email
    email = data.get("email")
    name = data.get("name") or data.get("login")
    avatar_url = data.get("avatar_url")

    return GitHubUserInfo(
        github_id=github_id,
        email=email,
        name=name,
        avatar_url=avatar_url,
    )


def get_github_user_emails(access_token: str) -> list[str]:
    """调 /user/emails 拿所有邮箱(含 primary + verified)

    用于 email 是 private 的情况
    """
    headers = {
        "Authorization": f"Bearer {access_token}",
        "Accept": "application/vnd.github+json",
    }

    try:
        with httpx.Client(timeout=10) as client:
            r = client.get("https://api.github.com/user/emails", headers=headers)
            r.raise_for_status()
    except httpx.HTTPError:
        return []

    emails = []
    for item in r.json():
        if item.get("primary") and item.get("verified"):
            emails.append(item.get("email", ""))
    return [e for e in emails if e]


def github_oauth_login(code: str) -> GitHubUserInfo:
    """完整 GitHub OAuth 流程:code → access_token → /user

    返回 GitHubUserInfo。失败抛 GitHubOAuthError。
    """
    access_token = exchange_code_for_token(code)
    info = get_github_user_info(access_token)

    # 若 email 为空(private),尝试拿 verified emails
    if not info.email:
        emails = get_github_user_emails(access_token)
        if emails:
            info.email = emails[0]

    return info


def list_github_repos(access_token: str) -> list[dict]:
    """列出当前 GitHub 用户可访问的所有仓库(含私有)

    用于任务创建页下拉选择私有仓库。返回精简字段列表:
        [{ "full_name", "name", "private", "html_url", "clone_url", "default_branch" }, ...]

    失败抛 GitHubOAuthError。
    """
    headers = {
        "Authorization": f"Bearer {access_token}",
        "Accept": "application/vnd.github+json",
    }
    params = {
        "affiliation": "owner,collaborator",  # 自己拥有 + 协作的仓库
        "per_page": 100,
        "sort": "updated",  # 按更新时间倒序,最近活跃的在前
        "direction": "desc",
    }

    try:
        with httpx.Client(timeout=15) as client:
            r = client.get(GITHUB_REPOS_URL, headers=headers, params=params)
            r.raise_for_status()
    except httpx.HTTPError as e:
        raise GitHubOAuthError(f"列出 GitHub 仓库失败: {e}") from e

    repos = []
    for item in r.json():
        repos.append({
            "full_name": item.get("full_name", ""),
            "name": item.get("name", ""),
            "private": bool(item.get("private", False)),
            "html_url": item.get("html_url", ""),
            "clone_url": item.get("clone_url", ""),
            "default_branch": item.get("default_branch", "main"),
        })
    return repos
