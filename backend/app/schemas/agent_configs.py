"""智能体配置相关的 Pydantic 模型

用户可配置多种外部智能体 CLI,任务创建时选择一种作为执行器。

安全约定(与 schemas/model_configs.py 一致):
- 响应(GET)绝不回传凭据原文,只返回 has_credentials 布尔,避免凭据经 HTTP 泄露
- 请求(PUT)中 secret 类型字段传空字符串表示"保留已存的值",非空表示"更新为新值"
"""
from pydantic import BaseModel, Field


# ============================================================
# Agent 类型元数据(前端渲染表单用)
# ============================================================


class CredentialField(BaseModel):
    """凭证字段定义(前端据此渲染输入框)"""

    key: str
    label: str
    # secret:敏感凭据(password 输入,加密存储,API 不回传原文)
    # text:非敏感配置(明文存储)
    type: str
    required: bool = True
    placeholder: str = ""
    help_url: str | None = None
    help_text: str | None = None


class AgentTypeMeta(BaseModel):
    """已注册的 agent 类型元数据(GET /agents/types 返回)"""

    agent_type: str
    display_name: str
    description: str = ""
    credential_fields: list[CredentialField] = Field(default_factory=list)
    help_url: str | None = None


# ============================================================
# 凭证写入请求(PUT /agents/configs/{agent_type})
# ============================================================


class CredentialValue(BaseModel):
    """单个凭证字段的值

    secret 类型:value 传空串表示保留已存值,非空表示更新
    text 类型:value 直接存储(可为空)
    """

    key: str
    value: str = Field(default="", max_length=4096)


class SaveAgentConfigRequest(BaseModel):
    """保存某 agent 配置请求"""

    credentials: list[CredentialValue] = Field(default_factory=list)
    is_active: bool = True


# ============================================================
# 配置读取响应(GET,不含凭据原文)
# ============================================================


class AgentConfigOut(BaseModel):
    """用户已配置的 agent(不含凭据原文)"""

    agent_type: str
    display_name: str
    is_active: bool
    # 是否已填写凭据(任一 secret 字段有值即为 True)
    has_credentials: bool

    model_config = {"from_attributes": True}


class AgentConfigDetailOut(AgentConfigOut):
    """单个 agent 配置详情(含各凭证字段的 has_value 状态,不含原文)"""

    # 各凭证字段的填写状态:{"pat": true, ...}
    credential_status: dict[str, bool] = Field(default_factory=dict)


class AgentConfigListResponse(BaseModel):
    """用户已配置的 agent 列表"""

    configs: list[AgentConfigOut] = Field(default_factory=list)


# ============================================================
# 凭证测试响应(POST /agents/configs/{agent_type}/test)
# ============================================================


class AgentTestResponse(BaseModel):
    """测试 agent 凭证连通性的响应

    用于「智能体配置」页面的「测试连接」按钮。
    在临时沙箱内启动 CLI + ACP 握手,验证凭证有效性。
    """

    ok: bool
    message: str
