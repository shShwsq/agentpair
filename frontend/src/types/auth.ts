/**
 * 认证相关类型定义
 *
 * 与后端 app/schemas/user.py 一一对应
 */

/** 用户信息(后端 UserResponse) */
export interface User {
  id: string
  email: string
  email_verified: boolean
  github_id: string | null
  has_password: boolean
  /** 是否已设置 TRAE CLI PAT(后端 has_trae_cli_pat) */
  has_trae_cli_pat: boolean
  created_at: string
}

/** 登录成功响应(后端 TokenResponse) */
export interface TokenResponse {
  access_token: string
  refresh_token: string
  token_type: string
  user: User
}

/** 刷新成功响应(后端 RefreshResponse) */
export interface RefreshResponse {
  access_token: string
  token_type: string
}

/** 通用消息响应(后端 MessageResponse) */
export interface MessageResponse {
  message: string
}

/** 修改密码请求(后端 ChangePasswordRequest) */
export interface ChangePasswordRequest {
  /** 当前密码;OAuth 用户(无密码)可不传 */
  current_password?: string
  new_password: string
}

/** TRAE CLI PAT 状态(后端 TraeCLIPatStatus,不暴露实际值) */
export interface TraeCLIPatStatus {
  has_pat: boolean
}

/** TRAE CLI PAT 设置请求(后端 TraeCLIPatRequest) */
export interface TraeCLIPatRequest {
  /** PAT 明文;空串等价于清除 */
  pat: string
}

/** 后端错误响应(FastAPI HTTPException 格式) */
export interface ApiError {
  detail: string
}
