/**
 * GitHub 绑定与仓库访问相关类型
 *
 * 对应后端 routers/github.py 的请求/响应模型。
 */

/** GitHub 绑定状态(GET /github/status) */
export interface GitHubStatus {
  /** 是否已绑定(有 access_token,可访问私有仓库) */
  bound: boolean
  /** GitHub 用户 ID(可能登录时绑定但未授权 repo) */
  github_id: string | null
  /** GitHub 用户名(实时查 /user,失败则空) */
  github_login: string | null
  /** 头像 URL */
  avatar_url: string | null
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
