"""智能体类型注册表

声明系统支持的外部智能体 CLI 类型及其元数据(显示名、凭证字段、沙箱配置)。
新增一种 agent 只需在此注册,后端 API / executor / 前端表单均据此动态生成。

每种 agent 的元数据包含:
- display_name / description:前端展示
- credential_fields:该 agent 需要的凭证字段(前端据此渲染表单,后端据此校验)
- sandbox:沙箱内运行配置(bin / install_cmd / acp 启动参数)
- help_url:凭证获取指引链接

agent_type 字符串同时作为 task.executor 的值,executor_agent.get_executor()
据此从 registry 查找对应的 executor 实现。
"""
from __future__ import annotations

from typing import Any


# ============================================================
# 凭证字段类型定义
# ============================================================

# type='secret':敏感凭据(加密存储,前端用 password 输入,API 不回传原文)
# type='text':非敏感配置(明文存储,如 base_url)
CREDENTIAL_FIELD_SECRET = "secret"
CREDENTIAL_FIELD_TEXT = "text"


# ============================================================
# Agent 类型注册表
# ============================================================

AGENT_REGISTRY: dict[str, dict[str, Any]] = {
    "qoder_cli": {
        "display_name": "Qoder CLI",
        "description": "沙箱内运行 Qoder CLI,通过 ACP 协议通信,模型由 Qoder 账号配额管理",
        "credential_fields": [
            {
                "key": "pat",
                "label": "Personal Access Token",
                "type": CREDENTIAL_FIELD_SECRET,
                "required": True,
                "placeholder": "粘贴你的 Qoder PAT",
                "help_url": "https://qoder.com/account/integrations",
                "help_text": "在 qoder.com/account/integrations 生成,用于沙箱内 Qoder CLI 认证",
            },
        ],
        "sandbox": {
            # 可执行文件名(沙箱内 PATH 查找或绝对路径)
            # 实际值从 config.py 的 QODER_CLI_BIN 读取,这里仅声明默认值
            "bin_config_key": "QODER_CLI_BIN",
            "bin_default": "qodercli",
            # 安装命令(沙箱内未检测到时执行)
            "install_cmd_config_key": "QODER_CLI_INSTALL_CMD",
            "install_cmd_default": "npm install -g @qoder-ai/qodercli",
            # ACP 启动参数(qodercli --acp --yolo)
            #   --acp:启动 ACP 协议服务
            #   --yolo:等价 --permission-mode bypass_permissions,跳过权限确认
            #     见 https://docs.qoder.com/cli/permissions
            "acp_args": ["--acp", "--yolo"],
            # PAT 注入用的环境变量名
            "credential_env": {"pat": "QODER_PERSONAL_ACCESS_TOKEN"},
        },
        "executor_module": "app.agents.qoder_cli_agent",
        "executor_func": "run_qoder_cli_agent",
    },
    "kimi_cli": {
        "display_name": "Kimi Code CLI",
        "description": (
            "沙箱内运行 Kimi Code CLI(开源 https://github.com/MoonshotAI/kimi-code),"
            "通过 ACP 协议通信,模型经 KIMI_MODEL_* 环境变量注入(支持自部署 LLM 端点)"
        ),
        "credential_fields": [
            {
                "key": "api_key",
                "label": "API Key",
                "type": CREDENTIAL_FIELD_SECRET,
                "required": True,
                "placeholder": "粘贴你的 LLM API Key",
                "help_url": "https://platform.moonshot.cn/console/api-keys",
                "help_text": "LLM 供应商的 API Key(如 Moonshot / OpenAI 兼容端点),经 KIMI_MODEL_API_KEY 注入",
            },
            {
                "key": "base_url",
                "label": "API Base URL",
                "type": CREDENTIAL_FIELD_TEXT,
                "required": False,
                "placeholder": "https://api.moonshot.ai/v1",
                "help_text": "LLM API 基址(留空用 provider 默认;自部署填你的端点),经 KIMI_MODEL_BASE_URL 注入",
            },
            {
                "key": "model",
                "label": "模型名",
                "type": CREDENTIAL_FIELD_TEXT,
                "required": False,
                "placeholder": "kimi-for-coding",
                "help_text": "发送给 API 的模型 ID(留空默认 kimi-for-coding),经 KIMI_MODEL_NAME 注入",
            },
        ],
        "sandbox": {
            # kimi CLI 可执行文件名(沙箱内 PATH 查找或绝对路径)
            "bin_config_key": "KIMI_CLI_BIN",
            "bin_default": "kimi",
            # 安装命令(沙箱内未检测到 kimi 时执行)
            # 前提:沙箱镜像已装 Node.js >= 20(见 docs/opensandbox-deploy.md)
            "install_cmd_config_key": "KIMI_CLI_INSTALL_CMD",
            "install_cmd_default": "npm install -g @moonshot-ai/kimi-code",
            # ACP 启动参数:kimi 用 `kimi acp` 子命令(非 --acp 标志)
            "acp_args": ["acp"],
            # Kimi ACP 模式不支持 --model / --reasoning-effort 等 CLI 参数
            # 模型经 KIMI_MODEL_NAME 环境变量注入,思考强度经 set_config_option 设置
            "inject_cli_model_args": False,
            # 凭证 → 环境变量映射(KIMI_MODEL_* 系列,合成临时 provider)
            "credential_env": {
                "api_key": "KIMI_MODEL_API_KEY",
                "base_url": "KIMI_MODEL_BASE_URL",
                "model": "KIMI_MODEL_NAME",
            },
            # 默认环境变量(仅当凭证未设置时注入)
            # KIMI_MODEL_NAME:启用开关 + 默认模型名
            # KIMI_MODEL_PROVIDER_TYPE:默认 kimi provider(Moonshot 兼容)
            "credential_env_defaults": {
                "KIMI_MODEL_NAME": "kimi-for-coding",
                "KIMI_MODEL_PROVIDER_TYPE": "kimi",
            },
        },
        "executor_module": "app.agents.kimi_cli_agent",
        "executor_func": "run_kimi_cli_agent",
    },
    "qoder_cli_cn": {
        "display_name": "Qoder CN CLI",
        "description": "沙箱内运行 Qoder CN CLI(国内版,原通义灵码),通过 ACP 协议通信,模型由 Qoder CN 账号配额管理",
        "credential_fields": [
            {
                "key": "pat",
                "label": "Personal Access Token",
                "type": CREDENTIAL_FIELD_SECRET,
                "required": True,
                "placeholder": "粘贴你的 Qoder CN PAT",
                "help_url": "https://qoder.cn/account/integrations",
                "help_text": "在 qoder.cn/account/integrations 生成,用于沙箱内 Qoder CN CLI 认证",
            },
        ],
        "sandbox": {
            # qoderclicn 是国内版 CLI,与国际版 qodercli 账号体系不互通
            "bin_config_key": "QODER_CLI_CN_BIN",
            "bin_default": "qoderclicn",
            "install_cmd_config_key": "QODER_CLI_CN_INSTALL_CMD",
            "install_cmd_default": "curl -fsSL https://qoder.cn/install | bash",
            # ACP 启动参数(与国际版一致,见 docs.qoder.cn/cli)
            "acp_args": ["--acp", "--yolo"],
            # PAT 环境变量名与国际版不同(多了 CN 后缀)
            "credential_env": {"pat": "QODERCN_PERSONAL_ACCESS_TOKEN"},
        },
        "executor_module": "app.agents.qoder_cli_agent",
        "executor_func": "run_qoder_cli_agent",
    },
}


# ============================================================
# 查询辅助
# ============================================================


def get_registered_types() -> list[str]:
    """返回所有已注册的 agent 类型标识"""
    return list(AGENT_REGISTRY.keys())


def get_agent_meta(agent_type: str) -> dict[str, Any] | None:
    """获取某 agent 类型的元数据,未注册返回 None"""
    return AGENT_REGISTRY.get(agent_type)


def is_registered(agent_type: str) -> bool:
    """判断 agent 类型是否已注册"""
    return agent_type in AGENT_REGISTRY


def get_executor_location(agent_type: str) -> tuple[str, str] | None:
    """获取 executor 的模块路径和函数名

    返回 (module_path, func_name),未注册返回 None。
    executor_agent 据此延迟导入对应的执行函数。
    """
    meta = AGENT_REGISTRY.get(agent_type)
    if not meta:
        return None
    return meta.get("executor_module", ""), meta.get("executor_func", "")


def get_credential_fields(agent_type: str) -> list[dict[str, Any]]:
    """获取某 agent 类型的凭证字段定义(前端表单渲染用)"""
    meta = AGENT_REGISTRY.get(agent_type)
    if not meta:
        return []
    return meta.get("credential_fields", [])


def get_sandbox_config(agent_type: str) -> dict[str, Any] | None:
    """获取某 agent 类型的沙箱运行配置"""
    meta = AGENT_REGISTRY.get(agent_type)
    if not meta:
        return None
    return meta.get("sandbox")
