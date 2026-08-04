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
            "acp_args": ["--acp", "--yolo"],
            # PAT 注入用的环境变量名
            "credential_env": {"pat": "QODER_PERSONAL_ACCESS_TOKEN"},
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
