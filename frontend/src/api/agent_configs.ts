/**
 * 智能体 CLI 配置 API 模块
 *
 * 对应后端 app/routers/agents.py 的端点(动态多 agent 架构)。
 * - GET    /agents/types              所有已注册 agent 类型(无需登录)
 * - GET    /agents/configs             当前用户已配置的 agent 列表(鉴权)
 * - GET    /agents/configs/{type}     单个配置详情(鉴权)
 * - PUT    /agents/configs/{type}      保存配置(鉴权)
 * - DELETE /agents/configs/{type}      删除配置(鉴权,返回剩余列表)
 *
 * 返回值已解包(取 response.data),调用方直接拿业务数据。
 */
import client from './client'
import type {
  AgentConfigDetailOut,
  AgentConfigListResponse,
  AgentTypeMeta,
  SaveAgentConfigRequest,
} from '@/types/agent_configs'

/** 获取所有已注册 agent 类型及其凭据字段定义(无需登录) */
export function getAgentTypes(): Promise<AgentTypeMeta[]> {
  return client.get('/agents/types').then((r) => r.data)
}

/** 获取当前用户已配置的 agent 列表(不含凭据明文) */
export function getAgentConfigs(): Promise<AgentConfigListResponse> {
  return client.get('/agents/configs').then((r) => r.data)
}

/** 获取指定 agent 类型的配置详情(含各凭据字段是否已设置) */
export function getAgentConfig(agent_type: string): Promise<AgentConfigDetailOut> {
  return client.get(`/agents/configs/${agent_type}`).then((r) => r.data)
}

/** 保存指定 agent 类型的配置(凭据 secret 字段空串=保留,非空=更新) */
export function saveAgentConfig(
  agent_type: string,
  body: SaveAgentConfigRequest,
): Promise<AgentConfigDetailOut> {
  return client.put(`/agents/configs/${agent_type}`, body).then((r) => r.data)
}

/** 删除指定 agent 类型的配置(返回删除后剩余的列表) */
export function deleteAgentConfig(agent_type: string): Promise<AgentConfigListResponse> {
  return client.delete(`/agents/configs/${agent_type}`).then((r) => r.data)
}
