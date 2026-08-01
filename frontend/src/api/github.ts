/**
 * GitHub 绑定与仓库访问 API
 *
 * 对应后端 app/routers/github.py 的端点:
 * - POST   /github/bind    用 code 换 token 并加密落库
 * - GET    /github/status  查看绑定状态
 * - DELETE /github/bind    解绑(清除 token)
 * - GET    /github/repos   列出当前用户仓库(含私有)
 *
 * 绑定流程:
 * - 前端跳到 GitHub 授权页(scope=user:email repo)
 * - 用户授权后 GitHub 回调到 redirect_uri?code=XXX
 * - 前端把 code 提交到 POST /github/bind
 */
import client from './client'
import type {
  GitHubBindRequest,
  GitHubReposResponse,
  GitHubStatus,
} from '@/types/github'

/** 绑定 GitHub:用 OAuth code 换 token 并加密落库 */
export function bindGitHub(req: GitHubBindRequest): Promise<GitHubStatus> {
  return client.post('/github/bind', req).then((r) => r.data)
}

/** 查询当前用户的 GitHub 绑定状态 */
export function getGitHubStatus(): Promise<GitHubStatus> {
  return client.get('/github/status').then((r) => r.data)
}

/** 解绑 GitHub(清除 access_token,保留 github_id 关联) */
export function unbindGitHub(): Promise<GitHubStatus> {
  return client.delete('/github/bind').then((r) => r.data)
}

/** 列出当前用户 GitHub 仓库(含私有,按更新时间倒序) */
export function listGitHubRepos(): Promise<GitHubReposResponse> {
  return client.get('/github/repos').then((r) => r.data)
}

/**
 * 拼接 GitHub 绑定授权链接(scope=user:email repo)
 *
 * 与登录用 scope=user:email 区分,额外申请仓库访问权限。
 * 复用同一个 redirect_uri(/auth/github/callback),
 * 回调页检测已登录后走绑定流程,未登录走登录流程。
 */
export function getGitHubBindURL(): string {
  const clientId = import.meta.env.VITE_GITHUB_OAUTH_CLIENT_ID
  const redirectUri = import.meta.env.VITE_GITHUB_OAUTH_REDIRECT_URI
  const params = new URLSearchParams({
    client_id: clientId,
    redirect_uri: redirectUri,
    scope: 'user:email repo',
  })
  return `https://github.com/login/oauth/authorize?${params.toString()}`
}
