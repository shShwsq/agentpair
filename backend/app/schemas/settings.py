"""模型配置相关的 Pydantic 模型

安全约定:
- 响应(GET)绝不回传 api_key 原文,只返回 has_api_key 布尔,避免凭据经 HTTP 泄露
- 请求(PUT)中 api_key 为空字符串表示"保留已存的 key",非空表示"更新为新 key"
- 测试端点(POST /test)使用已保存的配置,不接收前端传来的明文 key
"""
from pydantic import BaseModel


# ============================================================
# 配置写入(请求体)
# ============================================================


class LLMConfigIn(BaseModel):
    """LLM 配置写入

    api_key 约定:
    - 首次保存:必填,传完整 key
    - 后续更新:传空串 "" 表示保留已存的 key;传非空串表示更新为新 key
    """

    provider: str
    api_key: str = ""
    model: str
    enable_thinking: bool = True
    # 可选:自定义 baseUrl 覆盖 catalog 中的预设(留空则用 catalog 的 baseUrl)
    base_url: str | None = None


class EmbeddingConfigIn(BaseModel):
    """Embedding 配置写入(api_key 约定同 LLMConfigIn)"""

    provider: str
    api_key: str = ""
    model: str
    base_url: str | None = None
    dimension: int = 1024


class SaveModelsRequest(BaseModel):
    """保存模型配置请求(两区可独立保存,未传的一侧保持不变)"""

    llm: LLMConfigIn | None = None
    embedding: EmbeddingConfigIn | None = None


# ============================================================
# 配置读取(响应体)
# ============================================================


class LLMConfigOut(BaseModel):
    """LLM 配置读取(不含 api_key 原文)"""

    provider: str | None = None
    model: str | None = None
    enable_thinking: bool = True
    base_url: str | None = None
    has_api_key: bool = False

    model_config = {"from_attributes": True}


class EmbeddingConfigOut(BaseModel):
    """Embedding 配置读取(不含 api_key 原文)"""

    provider: str | None = None
    model: str | None = None
    base_url: str | None = None
    dimension: int = 1024
    has_api_key: bool = False

    model_config = {"from_attributes": True}


class UserModelsResponse(BaseModel):
    """用户已保存的模型配置"""

    llm: LLMConfigOut | None = None
    embedding: EmbeddingConfigOut | None = None


# ============================================================
# 测试连通性响应
# ============================================================


class TestResponse(BaseModel):
    """测试 LLM / Embedding 连通性的响应"""

    success: bool
    message: str
    # 耗时(毫秒)
    latency_ms: int | None = None
    # 仅 embedding 测试返回:实际向量维度
    dimension: int | None = None
