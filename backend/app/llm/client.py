"""LLM 客户端封装

统一 OpenAI 兼容后端,所有厂商(DashScope/DeepSeek/智谱/Kimi/豆包/MiniMax)走同一套调用代码,
差异通过 models_catalog.json 描述:
- thinkingParam:思考参数名(enable_thinking 或 thinking)
- thinkingEnabledType:开启时的取值(enabled 或 adaptive)
- reasoningSplit:是否需要额外传 reasoning_split(MiniMax)
- thinkingTemperature/nonThinkingTemperature:思考/非思考模式不同温度(Kimi)
- thinking 模式:hybrid(可开关)/ only(强制)/ none(不支持)

流式:chat_stream() 返回生成器,逐 chunk 产出 (reasoning_delta, content_delta, tool_call_delta)。
- reasoning_delta:思考链增量(DeepSeek-R1 / Qwen-QwQ / Kimi-k2.6 等模型才有)
- content_delta:正式回答增量
- tool_call_delta:工具调用增量(累积 index + arguments 片段)
所有 LLM 调用统一走流式,前端通过 SSE 实时看到思考过程。

设计参考:C:\\Users\\njwjx\\Documents\\BaiduSyncdisk\\course_大四\\pro\\ai-plugin\\lib\\llm.js
"""
import json
from collections.abc import Generator
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

    配置来源优先级:显式构造参数 > 用户保存的配置(阶段 6) > env 默认(阶段 1)
    """

    def __init__(
        self,
        provider_id: str | None = None,
        api_key: str | None = None,
        model: str | None = None,
        enable_thinking: bool | None = None,
        base_url: str | None = None,
    ):
        self.provider_id = provider_id or settings.LLM_PROVIDER
        self.api_key = api_key or settings.LLM_API_KEY
        self.model = model or settings.LLM_MODEL
        self.enable_thinking = (
            enable_thinking if enable_thinking is not None else settings.LLM_ENABLE_THINKING
        )
        # 可选:自定义 baseUrl 覆盖 catalog 预设(留空则用 catalog 的 baseUrl)
        self.base_url_override = base_url

        if not self.api_key:
            raise ValueError("未配置 LLM_API_KEY,请在 .env 中设置或在前端模型设置中配置")

        self.provider = find_provider(self.provider_id)
        if not self.provider:
            raise ValueError(f"未知 provider: {self.provider_id},请检查 models_catalog.json")

        self.model_meta = find_model_meta(self.provider, self.model)
        # 豆包用 Endpoint ID,model_meta 可能匹配不上,用 fallbackThinking 兜底
        # 此时 thinking_mode 取 fallbackThinking

        # baseUrl 优先级:用户自定义 > catalog 预设
        effective_base_url = self.base_url_override or self.provider["baseUrl"]

        # openai SDK 客户端(指向厂商的 OpenAI 兼容端点)
        self.client = OpenAI(
            api_key=self.api_key,
            base_url=effective_base_url,
        )

    @classmethod
    def from_config_dict(cls, cfg: dict) -> "LLMClient":
        """从单个配置字典构造客户端(列表式配置,阶段 6 重构)

        cfg: 用户保存的某个 LLM 配置项
        { id, name, provider, api_key, model, enable_thinking, base_url }

        失败时(如缺 api_key)抛 ValueError,由调用方决定是否回退到 env 默认。
        """
        api_key = cfg.get("api_key", "")
        if not api_key:
            raise ValueError("配置缺少 api_key")
        return cls(
            provider_id=cfg.get("provider"),
            api_key=api_key,
            model=cfg.get("model"),
            enable_thinking=cfg.get("enable_thinking", True),
            base_url=cfg.get("base_url"),
        )

    def chat_stream(
        self,
        messages: list[dict[str, str]],
        *,
        temperature: float | None = None,
        max_tokens: int = 4096,
        tools: list[dict[str, Any]] | None = None,
        tool_choice: str | dict[str, Any] | None = None,
    ) -> Generator["StreamChunk", None, None]:
        """流式对话(统一入口,所有 agent 都走这个)

        每次产出 StreamChunk,描述这一片增量:
        - reasoning_delta: 思考链增量(可空,部分模型才有)
        - content_delta: 正式回答增量(可空)
        - tool_call_deltas: 工具调用增量列表(可空,带 index 用于累积)
        - finish_reason: 流结束时为 'tool_calls' / 'stop' / 'length' 等,中途为 None

        工具调用流式累积说明:
        OpenAI 流式响应中,一个 tool_call 会跨多个 chunk:
        - 第一个 chunk: tool_calls[i].id / .type / .function.name
        - 后续 chunk: tool_calls[i].function.arguments 片段
        所以调用方需要按 index 累积 arguments 字符串,完整后才能 json.loads。

        参考:ai-plugin/lib/llm.js 的 _parseSSE
        """
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
            "stream": True,
        }
        if tools:
            kwargs["tools"] = tools
            if tool_choice:
                kwargs["tool_choice"] = tool_choice
        if extras:
            kwargs["extra_body"] = extras

        # 流式调用,SDK 返回迭代器
        stream = self.client.chat.completions.create(**kwargs)

        for chunk in stream:
            if not chunk.choices:
                continue
            choice = chunk.choices[0]
            delta = choice.delta

            # reasoning_content:思考链增量(DeepSeek/Qwen/Kimi 等)
            # OpenAI SDK 1.x+ 把厂商扩展字段放在 delta 的 model_extra 里
            reasoning_delta = ""
            if hasattr(delta, "reasoning_content") and delta.reasoning_content:
                reasoning_delta = delta.reasoning_content
            else:
                # 兜底:从 model_extra(原始 dict)取
                model_extra = getattr(delta, "model_extra", None) or {}
                if isinstance(model_extra, dict):
                    rc = model_extra.get("reasoning_content")
                    if rc:
                        reasoning_delta = rc

            content_delta = delta.content or ""

            # 工具调用增量(可能同时有多个 tool_call 并行)
            tool_call_deltas: list[ToolCallDelta] = []
            if delta.tool_calls:
                for tc in delta.tool_calls:
                    tool_call_deltas.append(ToolCallDelta(
                        index=tc.index if tc.index is not None else 0,
                        id=tc.id,
                        name=tc.function.name if tc.function and tc.function.name else None,
                        arguments_fragment=tc.function.arguments if tc.function and tc.function.arguments else "",
                    ))

            # 跳过完全空的 chunk(SSE keep-alive 等)
            if (
                not reasoning_delta
                and not content_delta
                and not tool_call_deltas
                and not choice.finish_reason
            ):
                continue

            yield StreamChunk(
                reasoning_delta=reasoning_delta,
                content_delta=content_delta,
                tool_call_deltas=tool_call_deltas or None,
                finish_reason=choice.finish_reason,
            )

    def test(self, prompt: str = "你好,请介绍一下你自己。") -> dict[str, Any]:
        """测试 LLM 连通性并收集完整回复

        返回 { success, message, latency_ms, reply }
        用流式接口调用,收集完整 content 后返回,供前端展示模型实际回复。
        测试时强制关闭深度思考,避免思考链耗时过长(thinking_mode=only 的模型无法关闭)。
        """
        import time

        start = time.perf_counter()
        # 临时关闭深度思考,测试完恢复
        original_thinking = self.enable_thinking
        self.enable_thinking = False
        try:
            messages = [{"role": "user", "content": prompt}]
            # 收集完整 content,max_tokens 限制在合理范围(一句口号)
            collected: list[str] = []
            has_any = False
            for chunk in self.chat_stream(messages, max_tokens=128):
                if chunk.content_delta:
                    collected.append(chunk.content_delta)
                    has_any = True
                if chunk.finish_reason in ("stop", "tool_calls", "length"):
                    break
            latency_ms = int((time.perf_counter() - start) * 1000)
            reply = "".join(collected).strip()
            if has_any and reply:
                return {
                    "success": True,
                    "message": "LLM 测试成功",
                    "latency_ms": latency_ms,
                    "reply": reply,
                }
            # 没拿到 content 但流正常结束,也算成功(部分模型只输出 reasoning)
            return {
                "success": True,
                "message": "LLM 测试成功(未返回 content,可能仅思考)",
                "latency_ms": latency_ms,
                "reply": None,
            }
        except Exception as e:
            latency_ms = int((time.perf_counter() - start) * 1000)
            return {
                "success": False,
                "message": f"LLM 测试失败: {e}",
                "latency_ms": latency_ms,
                "reply": None,
            }
        finally:
            self.enable_thinking = original_thinking


# ============================================================
# 流式数据结构
# ============================================================


class ToolCallDelta:
    """工具调用增量(对应 OpenAI stream chunk 中的 tool_calls[i])

    一个完整的 tool_call 会跨多个 chunk:
    - 第一个 chunk 带 id 和 name(以及 arguments 起始片段)
    - 后续 chunk 只带 arguments 增量片段
    调用方需要按 index 累积 arguments,完整后才能 json.loads。
    """

    __slots__ = ("index", "id", "name", "arguments_fragment")

    def __init__(
        self,
        index: int,
        id: str | None = None,
        name: str | None = None,
        arguments_fragment: str = "",
    ) -> None:
        self.index = index
        self.id = id  # 仅第一个 chunk 有
        self.name = name  # 仅第一个 chunk 有
        self.arguments_fragment = arguments_fragment  # 多个 chunk 累积

    def __repr__(self) -> str:
        return (
            f"ToolCallDelta(index={self.index}, id={self.id!r}, "
            f"name={self.name!r}, args_len={len(self.arguments_fragment)})"
        )


class StreamChunk:
    """流式响应的单个 chunk

    三个 delta 字段互斥(同一 chunk 通常只会有其中一个有值):
    - reasoning_delta: 思考链增量(reasoning_content)
    - content_delta: 正式回答增量(content)
    - tool_call_deltas: 工具调用增量列表(tool_calls[i])

    finish_reason 在流结束时非空('stop' / 'tool_calls' / 'length' 等)
    """

    __slots__ = ("reasoning_delta", "content_delta", "tool_call_deltas", "finish_reason")

    def __init__(
        self,
        *,
        reasoning_delta: str = "",
        content_delta: str = "",
        tool_call_deltas: list[ToolCallDelta] | None = None,
        finish_reason: str | None = None,
    ) -> None:
        self.reasoning_delta = reasoning_delta
        self.content_delta = content_delta
        self.tool_call_deltas = tool_call_deltas
        self.finish_reason = finish_reason

    def __repr__(self) -> str:
        parts = []
        if self.reasoning_delta:
            parts.append(f"reasoning({len(self.reasoning_delta)})")
        if self.content_delta:
            parts.append(f"content({len(self.content_delta)})")
        if self.tool_call_deltas:
            names = [tc.name or '?' for tc in self.tool_call_deltas]
            parts.append(f"tool_call({','.join(names)})")
        if self.finish_reason:
            parts.append(f"finish={self.finish_reason}")
        return f"StreamChunk({', '.join(parts) or 'empty'})"
