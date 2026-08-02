/**
 * GitHub 绑定与仓库访问相关类型
 *
 * 对应后端 routers/github.py 的请求/响应模型。
 */

/** GitHub 绑定状态(GET /github/status, POST /github/bind) */
export interface GitHubStatus {
  /** 是否已绑定(有 access_token,可访问私有仓库) */
  bound: boolean
  /** GitHub 用户 ID(可能登录时绑定但未授权 repo) */
  github_id: string | null
  /** GitHub 用户名(实时查 /user,失败则空) */
  github_login: string | null
  /** 头像 URL */
  avatar_url: string | null
  /** 邮箱不一致(仅 bind 响应可能为 true,status 查询恒为 false) */
  email_mismatch?: boolean
  /** GitHub verified primary email(不一致时用于弹窗展示) */
  github_email?: string | null
  /** 账号当前邮箱(不一致时用于弹窗展示) */
  current_email?: string | null
}

/** 邮箱同步结果(PATCH /github/sync-email) */
export interface SyncEmailResponse {
  /** 更新后的邮箱 */
  email: string
  /** 是否已验证(GitHub verified primary 视为已验证) */
  email_verified: boolean
}

/** 仓库列表项(GET /github/repos) */
export interface GitHubRepoItem {
  /** owner/repo 全名 */
  full_name: string
  /** 仓库名(不含 owner) */
  name: string
  /** 是否私有 */
  private: boolean
  /** 网页 URL */
  html_url: string
  /** 克隆 URL(HTTPS) */
  clone_url: string
  /** 默认分支 */
  default_branch: string
}

/** 仓库列表响应(GET /github/repos) */
export interface GitHubReposResponse {
  repos: GitHubRepoItem[]
}

/** 绑定请求体(POST /github/bind) */
export interface GitHubBindRequest {
  code: string
}
