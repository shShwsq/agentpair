"""LLM 客户端封装

统一 OpenAI 兼容后端,所有厂商(DashScope/DeepSeek/智谱/Kimi/豆包/MiniMax)走同一套调用代码,
差异通过 models_catalog.json 描述:
- thinkingParam:思考参数名(enable_thinking 或 thinking)
- thinkingEnabledType:开启时的取值(enabled 或 adaptive)
- reasoningSplit:是否需要额外传 reasoning_split(MiniMax)
- thinkingTemperature/nonThinkingTemperature:思考/非思考模式不同温度(Kimi)
- thinking 模式:hybrid(可开关)/ only(强制)/ none(不支持)

设计参考:C:\\Users\\njwjx\\Documents\\BaiduSyncdisk\\course_大四\\pro\\ai-plugin\\lib\\llm.js
"""
import json
from pathlib import Path
from typing import Any

from openai import OpenAI

from app.config import settings


# ============================================================
# 厂商清单加载与查找
# ============================================================

_catalog: dict[str, Any] | None = None


def _load_catalog() -> dict[str, Any]:
    """加载 models_catalog.json(进程级缓存)"""
    global _catalog
    if _catalog is not None:
        return _catalog
    catalog_path = Path(__file__).parent / "models_catalog.json"
    with catalog_path.open(encoding="utf-8") as f:
        _catalog = json.load(f)
    return _catalog


def find_provider(provider_id: str) -> dict[str, Any] | None:
    """按 id 查找厂商元信息"""
    catalog = _load_catalog()
    for p in catalog.get("llmProviders", []):
        if p["id"] == provider_id:
            return p
    return None


def find_model_meta(provider: dict[str, Any], model_id: str) -> dict[str, Any] | None:
    """在 provider 内查找模型元信息(thinking 模式等)"""
    for m in provider.get("models", []):
        if m["id"] == model_id:
            return m
    return None


# ============================================================
# thinking 参数构造
# ============================================================


def build_thinking_extras(
    provider: dict[str, Any],
    model_meta: dict[str, Any] | None,
    enable_thinking: bool,
) -> dict[str, Any] | None:
    """构造思考参数注入字典

    返回 None 表示该模型/厂商不支持思考或不需要注入
    """
    if not provider.get("supportsThinking"):
        return None

    # thinking 模式:hybrid(可开关)/ only(强制)/ none(不支持)
    thinking_mode = model_meta.get("thinking") if model_meta else None
    if not thinking_mode:
        # 豆包等厂商用 Endpoint ID,model_meta 可能匹配不上,用 provider 兜底
        thinking_mode = provider.get("fallbackThinking", "none")

    if thinking_mode == "none":
        return None

    param_name = provider.get("thinkingParam", "enable_thinking")
    enabled_type = provider.get("thinkingEnabledType", "enabled")

    extras: dict[str, Any] | None = None

    if thinking_mode == "only":
        # 仅思考模式:强制开启
        if param_name == "thinking":
            extras = {"thinking": {"type": enabled_type}}
        else:
            extras = {param_name: True}
    elif thinking_mode == "hybrid":
        # 混合思考:按开关显式传值(关闭时必须显式 false/disabled)
        if param_name == "thinking":
            extras = {"thinking": {"type": enabled_type if enable_thinking else "disabled"}}
        else:
            extras = {param_name: enable_thinking}

    # MiniMax 需额外传 reasoning_split: true
    if extras and provider.get("reasoningSplit"):
        extras["reasoning_split"] = True

    return extras


def resolve_temperature(
    provider: dict[str, Any],
    model_meta: dict[str, Any] | None,
    enable_thinking: bool,
    explicit_temperature: float | None = None,
) -> float:
    """解析温度

    优先级:显式传入 > provider 思考模式动态值 > 默认 0.7
    Kimi k2.6/k2.5:思考模式固定 1.0,非思考模式固定 0.6
    """
    if explicit_temperature is not None:
        return explicit_temperature

    thinking_temp = provider.get("thinkingTemperature")
    non_thinking_temp = provider.get("nonThinkingTemperature")
    if thinking_temp is not None or non_thinking_temp is not None:
        thinking_mode = model_meta.get("thinking") if model_meta else None
        if not thinking_mode:
            thinking_mode = provider.get("fallbackThinking", "none")
        is_thinking = thinking_mode == "only" or (
            thinking_mode == "hybrid" and enable_thinking
        )
        if is_thinking:
            return thinking_temp if thinking_temp is not None else 1.0
        return non_thinking_temp if non_thinking_temp is not None else 0.7

    return provider.get("defaultTemperature", 0.7)


# ============================================================
# OpenAI 兼容 baseUrl 拼接
# ============================================================


def build_chat_url(base_url: str) -> str:
    """拼接 chat/completions 端点

    若 baseUrl 已含版本前缀(/v1、/v2、/v3、/v4),直接拼 /chat/completions;
    否则补 /v1(纯 baseUrl 的厂商,如 MiniMax)
    """
    base = (base_url or "https://api.openai.com").rstrip("/")
    import re

    if re.search(r"/v\d+$", base):
        return f"{base}/chat/completions"
    return f"{base}/v1/chat/completions"


# ============================================================
# LLM 客户端
# ============================================================


class LLMClient:
    """OpenAI 兼容 LLM 客户端

    阶段 1:从 settings 读取单 provider 配置
    阶段 6 起:支持用户自选 provider/model
    """

    def __init__(
        self,
        provider_id: str | None = None,
        api_key: str | None = None,
        model: str | None = None,
        enable_thinking: bool | None = None,
    ):
        self.provider_id = provider_id or settings.LLM_PROVIDER
        self.api_key = api_key or settings.LLM_API_KEY
        self.model = model or settings.LLM_MODEL
        self.enable_thinking = (
            enable_thinking if enable_thinking is not None else settings.LLM_ENABLE_THINKING
        )

        if not self.api_key:
            raise ValueError("未配置 LLM_API_KEY,请在 .env 中设置")

        self.provider = find_provider(self.provider_id)
        if not self.provider:
            raise ValueError(f"未知 provider: {self.provider_id},请检查 models_catalog.json")

        self.model_meta = find_model_meta(self.provider, self.model)
        # 豆包用 Endpoint ID,model_meta 可能匹配不上,用 fallbackThinking 兜底
        # 此时 thinking_mode 取 fallbackThinking

        # openai SDK 客户端(指向厂商的 OpenAI 兼容端点)
        self.client = OpenAI(
            api_key=self.api_key,
            base_url=self.provider["baseUrl"],
        )

    def chat(
        self,
        messages: list[dict[str, str]],
        *,
        temperature: float | None = None,
        max_tokens: int = 4096,
        tools: list[dict[str, Any]] | None = None,
        tool_choice: str | dict[str, Any] | None = None,
    ) -> Any:
        """同步对话

        自动注入 thinking 参数与温度(根据厂商与模型差异)
        """
        # 构造额外参数
        extras = build_thinking_extras(
            self.provider, self.model_meta, self.enable_thinking
        ) or {}

        resolved_temp = resolve_temperature(
            self.provider, self.model_meta, self.enable_thinking, temperature
        )

        kwargs: dict[str, Any] = {
            "model": self.model,
            "messages": messages,
            "temperature": resolved_temp,
            "max_tokens": max_tokens,
            **extras,
        }
        if tools:
            kwargs["tools"] = tools
            if tool_choice:
                kwargs["tool_choice"] = tool_choice

        return self.client.chat.completions.create(**kwargs)
