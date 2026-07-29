/**
 * 模型设置相关类型定义
 *
 * 与后端 app/schemas/settings.py + app/llm/models_catalog.json 对应
 *
 * 安全约定:
 * - 后端响应(GET)不返回 api_key 原文,只返回 has_api_key
 * - 前端表单 api_key 字段:已配置时 placeholder 提示"已配置,输入新值以替换"
 * - 保存时 api_key 为空串表示"保留已存的 key"(首次保存必须填)
 */

// ============================================================
// 厂商与模型清单(models_catalog.json)
// ============================================================

/** LLM 模型元信息 */
export interface LLMModelMeta {
  id: string
  /** 思考模式: hybrid(可开关) | only(强制) | none(不支持) */
  thinking?: 'hybrid' | 'only' | 'none'
  thinkingDefault?: boolean
}

/** LLM 厂商 */
export interface LLMProvider {
  id: string
  name: string
  baseUrl: string
  apiKeyUrl?: string
  supportsThinking?: boolean
  thinkingParam?: string
  thinkingEnabledType?: string
  thinkingTemperature?: number
  nonThinkingTemperature?: number
  fallbackThinking?: string
  reasoningSplit?: boolean
  models: LLMModelMeta[]
}

/** Embedding 模型元信息 */
export interface EmbeddingModelMeta {
  id: string
  name: string
  dimension: number
  multimodal?: boolean
  dimensionsParam?: boolean
  multimodalEndpoint?: boolean
}

/** Embedding 厂商 */
export interface EmbeddingProvider {
  id: string
  name: string
  baseUrl: string
  apiKeyUrl?: string
  fallbackDimension?: number
  fallbackMultimodal?: boolean
  fallbackMultimodalEndpoint?: boolean
  fallbackDimensionsParam?: boolean
  models: EmbeddingModelMeta[]
}

/** 完整 catalog */
export interface ModelsCatalog {
  llmProviders: LLMProvider[]
  embeddingProviders: EmbeddingProvider[]
}

// ============================================================
// 用户已保存的配置(响应,不含 api_key 原文)
// ============================================================

export interface LLMConfigOut {
  provider: string | null
  model: string | null
  enable_thinking: boolean
  base_url: string | null
  has_api_key: boolean
}

export interface EmbeddingConfigOut {
  provider: string | null
  model: string | null
  base_url: string | null
  dimension: number
  has_api_key: boolean
}

export interface UserModelsResponse {
  llm: LLMConfigOut | null
  embedding: EmbeddingConfigOut | null
}

// ============================================================
// 保存配置(请求)
// ============================================================

export interface LLMConfigIn {
  provider: string
  /** 空串 = 保留已存的 key(更新时);首次保存必填 */
  api_key: string
  model: string
  enable_thinking: boolean
  /** 自定义 baseUrl,留空用 catalog 预设 */
  base_url: string | null
}

export interface EmbeddingConfigIn {
  provider: string
  api_key: string
  model: string
  base_url: string | null
  dimension: number
}

export interface SaveModelsRequest {
  llm?: LLMConfigIn
  embedding?: EmbeddingConfigIn
}

// ============================================================
// 测试响应
// ============================================================

export interface TestResponse {
  success: boolean
  message: string
  latency_ms?: number | null
  /** 仅 embedding 测试返回 */
  dimension?: number | null
}
