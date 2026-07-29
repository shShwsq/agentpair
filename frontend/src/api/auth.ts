/**
 * 认证 API 模块
 *
 * 每个函数对应后端 app/routers/auth.py 的一个端点。
 * 返回值已解包(取 response.data),调用方直接拿业务数据。
 */
import client from './client'
import type {
  ChangePasswordRequest,
  MessageResponse,
  RefreshResponse,
  TokenResponse,
  User,
} from '@/types/auth'

// ---- 注册 + 邮箱验证 ----

export function register(email: string, password: string): Promise<MessageResponse> {
  return client.post('/auth/register', { email, password }).then((r) => r.data)
}

export function verifyEmail(token: string): Promise<MessageResponse> {
  return client.post('/auth/verify-email', { token }).then((r) => r.data)
}

export function resendVerification(email: string): Promise<MessageResponse> {
  return client.post('/auth/resend-verification', { email }).then((r) => r.data)
}

// ---- 登录 + 刷新 ----

export function login(email: string, password: string): Promise<TokenResponse> {
  return client.post('/auth/login', { email, password }).then((r) => r.data)
}

export function refreshToken(refresh_token: string): Promise<RefreshResponse> {
  return client.post('/auth/refresh', { refresh_token }).then((r) => r.data)
}

// ---- 当前用户 ----

export function getMe(): Promise<User> {
  return client.get('/auth/me').then((r) => r.data)
}

// ---- 忘记密码 + 重置密码 ----

export function forgotPassword(email: string): Promise<MessageResponse> {
  return client.post('/auth/password/forgot', { email }).then((r) => r.data)
}

export function resetPassword(token: string, new_password: string): Promise<MessageResponse> {
  return client.post('/auth/password/reset', { token, new_password }).then((r) => r.data)
}

// ---- 修改密码(已登录) ----

export function changePassword(req: ChangePasswordRequest): Promise<MessageResponse> {
  return client.post('/auth/password/change', req).then((r) => r.data)
}

// ---- GitHub OAuth ----

export function githubOAuth(code: string): Promise<TokenResponse> {
  return client.post('/auth/oauth/github', { code }).then((r) => r.data)
}

/**
 * 拼接 GitHub OAuth 授权链接
 *
 * 前端直接跳转到此 URL,用户授权后 GitHub 回调到 redirect_uri?code=XXX
 */
export function getGitHubAuthorizeURL(): string {
  const clientId = import.meta.env.VITE_GITHUB_OAUTH_CLIENT_ID
  const redirectUri = import.meta.env.VITE_GITHUB_OAUTH_REDIRECT_URI
  const params = new URLSearchParams({
    client_id: clientId,
    redirect_uri: redirectUri,
    scope: 'user:email',
  })
  return `https://github.com/login/oauth/authorize?${params.toString()}`
}
