/**
 * 模型设置 API 模块
 *
 * 对应后端 app/routers/settings.py 的端点。
 * - GET  /settings/catalog        厂商清单
 * - GET  /settings/models         当前用户已存配置
 * - PUT  /settings/models         保存配置
 * - POST /settings/llm/test       测试 LLM(用已存配置)
 * - POST /settings/embedding/test 测试 Embedding(用已存配置)
 */
import client from './client'
import type {
  ModelsCatalog,
  SaveModelsRequest,
  TestResponse,
  UserModelsResponse,
} from '@/types/settings'

/** 获取厂商与模型清单(无需登录,前端选厂商用) */
export function getCatalog(): Promise<ModelsCatalog> {
  return client.get('/settings/catalog').then((r) => r.data)
}

/** 获取当前用户已保存的模型配置(不含 api_key 原文) */
export function getMyModels(): Promise<UserModelsResponse> {
  return client.get('/settings/models').then((r) => r.data)
}

/** 保存模型配置(api_key 空串 = 保留已存的 key) */
export function saveModels(req: SaveModelsRequest): Promise<UserModelsResponse> {
  return client.put('/settings/models', req).then((r) => r.data)
}

/** 测试 LLM 连通性(使用已保存的配置,需先保存) */
export function testLLM(): Promise<TestResponse> {
  return client.post('/settings/llm/test').then((r) => r.data)
}

/** 测试 Embedding 连通性(使用已保存的配置,需先保存) */
export function testEmbedding(): Promise<TestResponse> {
  return client.post('/settings/embedding/test').then((r) => r.data)
}
