"""Embedding 客户端

用途:测试用户配置的 Embedding 服务连通性,返回向量维度。

支持两种后端(由 models_catalog.json 的 provider 决定):
1. DashScope 原生:POST /services/embeddings/text-embedding/text-embedding
   - body: { model, input: { texts: [text] }, parameters: { text_type: 'document' } }
2. OpenAI 兼容(智谱/百度/火山/Jina 等):POST /embeddings
   - body: { model, input: text, dimensions?: 1024 }
   - 部分模型通过 dimensions 参数指定输出维度

设计参考:C:\\Users\\njwjx\\Documents\\BaiduSyncdisk\\course_大四\\pro\\ai-plugin\\lib\\embedding.js
"""
import time
from typing import Any

import httpx

from app.llm.client import _load_catalog, find_provider


def find_embedding_provider(provider_id: str) -> dict[str, Any] | None:
    """按 id 查找 embedding 厂商元信息"""
    catalog = _load_catalog()
    for p in catalog.get("embeddingProviders", []):
        if p["id"] == provider_id:
            return p
    return None


def find_embedding_model_meta(
    provider: dict[str, Any], model_id: str
) -> dict[str, Any] | None:
    """在 embedding provider 内查找模型元信息(维度/多模态/dimensionsParam)"""
    for m in provider.get("models", []):
        if m["id"] == model_id:
            return m
    return None


class EmbeddingClient:
    """Embedding 测试客户端

    阶段 6:仅用于连通性测试,尚未接入向量检索流程
    """

    def __init__(
        self,
        provider_id: str,
        api_key: str,
        model: str,
        base_url: str | None = None,
    ):
        if not api_key:
            raise ValueError("未配置 Embedding API Key")

        self.provider_id = provider_id
        self.api_key = api_key
        self.model = model

        self.provider = find_embedding_provider(provider_id)
        if not self.provider:
            raise ValueError(
                f"未知 embedding provider: {provider_id},请检查 models_catalog.json"
            )

        # baseUrl 优先级:用户自定义 > catalog 预设
        self.base_url = (base_url or self.provider.get("baseUrl", "")).rstrip("/")

        self.model_meta = find_embedding_model_meta(self.provider, model)
        # 多模态/维度参数:模型级 fallback 到 provider 级
        self.dimensions_param = bool(
            (self.model_meta or {}).get("dimensionsParam")
            or self.provider.get("fallbackDimensionsParam")
        )
        self.expected_dimension = (
            (self.model_meta or {}).get("dimension")
            or self.provider.get("fallbackDimension")
            or 1024
        )

    def embed(self, text: str) -> list[float]:
        """生成单条文本的向量(用于测试)

        返回向量(list[float])。失败抛异常。
        """
        # DashScope 原生后端:provider id 为 dashscope 且 baseUrl 是原生 API 地址
        is_dashscope_native = (
            self.provider_id == "dashscope"
            and "/compatible-mode" not in self.base_url
        )
        if is_dashscope_native:
            return self._embed_dashscope_native(text)
        return self._embed_openai_compatible(text)

    def test(self, text: str = "你好,这是一个连通性测试") -> dict[str, Any]:
        """测试连通性

        返回 { success, message, latency_ms, dimension }
        """
        start = time.perf_counter()
        try:
            vec = self.embed(text)
            latency_ms = int((time.perf_counter() - start) * 1000)
            return {
                "success": True,
                "message": "Embedding 测试成功",
                "latency_ms": latency_ms,
                "dimension": len(vec),
            }
        except Exception as e:
            latency_ms = int((time.perf_counter() - start) * 1000)
            return {
                "success": False,
                "message": f"Embedding 测试失败: {e}",
                "latency_ms": latency_ms,
                "dimension": None,
            }

    # ============================================================
    # DashScope 原生 API
    # ============================================================

    def _embed_dashscope_native(self, text: str) -> list[float]:
        """DashScope 原生 text-embedding 端点

        POST {base_url}/services/embeddings/text-embedding/text-embedding
        body: { model, input: { texts: [text] }, parameters: { text_type: 'document' } }
        """
        url = f"{self.base_url}/services/embeddings/text-embedding/text-embedding"
        body = {
            "model": self.model,
            "input": {"texts": [text]},
            "parameters": {"text_type": "document"},
        }
        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
        }
        with httpx.Client(timeout=30) as client:
            resp = client.post(url, json=body, headers=headers)
            resp.raise_for_status()
            data = resp.json()

        embeddings = (
            data.get("output", {}).get("embeddings", []) if isinstance(data, dict) else []
        )
        if not embeddings or "embedding" not in embeddings[0]:
            raise ValueError(f"响应缺少 embedding 字段: {str(data)[:200]}")
        return embeddings[0]["embedding"]

    # ============================================================
    # OpenAI 兼容 /embeddings
    # ============================================================

    def _embed_openai_compatible(self, text: str) -> list[float]:
        """OpenAI 兼容 /embeddings 端点

        POST {base_url}/embeddings
        body: { model, input: text, dimensions?: 1024 }(部分模型支持 dimensions 参数)
        """
        # baseUrl 可能不含版本前缀,补 /v1
        from app.llm.client import build_chat_url  # 复用版本前缀拼接逻辑

        # build_chat_url 拼的是 /chat/completions,这里替换为 /embeddings
        chat_url = build_chat_url(self.base_url)
        url = chat_url.replace("/chat/completions", "/embeddings")

        body: dict[str, Any] = {
            "model": self.model,
            "input": text,
        }
        if self.dimensions_param:
            body["dimensions"] = self.expected_dimension

        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
        }
        with httpx.Client(timeout=30) as client:
            resp = client.post(url, json=body, headers=headers)
            resp.raise_for_status()
            data = resp.json()

        embeddings = data.get("data", []) if isinstance(data, dict) else []
        if not embeddings or "embedding" not in embeddings[0]:
            raise ValueError(f"响应缺少 embedding 字段: {str(data)[:200]}")
        return embeddings[0]["embedding"]
