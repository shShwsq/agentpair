# Kimi Code CLI 部署指南(自部署 + AgentPair 接入)

本文档指导如何把开源 [Kimi Code CLI](https://github.com/MoonshotAI/kimi-code) 接入 AgentPair,作为 `react_agent` 的执行器之一(与 Qoder CLI 并列)。

> 前置条件:已按 [opensandbox-deploy.md](./opensandbox-deploy.md) 部署好 OpenSandbox Server,后端 `SANDBOX_MODE=sandbox` 能正常创建沙箱。本文只覆盖 Kimi Code 特有部分。

## 一、整体架构

```
┌─────────────┐  ACP over stdio   ┌──────────────────┐
│ AgentPair   │◄──────────────────►│ acp_bridge.py    │
│ 后端        │  HTTP/SSE 桥接     │ (沙箱内 Python)  │
│             │                   └────────┬─────────┘
│ run_acp_    │                            │ spawn stdin/stdout
│ agent()     │                            ▼
│             │                   ┌──────────────────┐
│ _build_     │  KIMI_MODEL_* env │ kimi acp         │
│ credential_ │ ──────────────── ►│ (Kimi Code CLI)  │
│ envs()      │                   └────────┬─────────┘
└─────────────┘                            │ HTTPS
                                           ▼
                                  ┌──────────────────┐
                                  │ LLM API 端点     │
                                  │ (Moonshot 官方   │
                                  │  或自部署)       │
                                  └──────────────────┘
```

关键设计:

1. **Kimi Code CLI 跑在沙箱内**,通过 `kimi acp` 子命令启动 ACP 协议服务
2. **`acp_bridge.py`** 把后端的 HTTP/SSE 请求桥接到 CLI 的 stdio
3. **凭证不经命令行明文传递**:后端把用户配置的 `api_key` / `base_url` / `model` 映射成 `KIMI_MODEL_API_KEY` / `KIMI_MODEL_BASE_URL` / `KIMI_MODEL_NAME` 环境变量,注入到 bridge 进程,CLI 子进程继承后**在内存里合成临时 provider**(不写 `config.toml`)
4. **权限绕过**:Kimi ACP 模式无 `--yolo` 启动参数,通过 `session/set_config_option(mode=yolo)` 在 `session/new` 之后设置
5. **模型选择**:Kimi ACP 模式不支持 `--model` CLI 参数,模型经 `KIMI_MODEL_NAME` 环境变量注入

## 二、沙箱镜像准备

### 2.1 依赖

| 依赖 | 版本 | 用途 | 来源 |
|------|------|------|------|
| Node.js | `>= 22.19.0` | kimi code 运行时(见 `apps/kimi-code/package.json` 的 `engines`) | NodeSource |
| npm | 随 Node | 安装 kimi code 包 | NodeSource |
| Python 3 | 任意 | `acp_bridge.py` 运行时(基础镜像已含) | apt |
| git / rg / awk / find / curl | — | 基础工具(已在 `agentpair-sandbox:latest`) | apt |

> ⚠️ Node 版本要求比 Qoder CLI(>= 20.0.0)高。如果同一镜像要同时预装 qodercli + kimi,统一用 Node 22(qodercli 兼容 Node 22)。

### 2.2 方式 A:镜像预装(推荐,启动快)

在已有的 `agentpair-sandbox:latest` 基础上,追加 Kimi Code 安装块。完整 Dockerfile 示例:

```dockerfile
FROM ubuntu:22.04

ENV DEBIAN_FRONTEND=noninteractive

# 基础工具(与 opensandbox-deploy.md 2.2 一致)
RUN apt-get update && apt-get install -y --no-install-recommends \
        git ripgrep python3 python3-pip ca-certificates openssh-client \
        coreutils findutils gawk curl \
    && rm -rf /var/lib/apt/lists/* \
    && ln -sf /usr/bin/python3 /usr/bin/python

# 装 Node.js 22.x(kimi code 要求 >= 22.19;qodercli 也兼容)
USER root
RUN curl -fsSL https://deb.nodesource.com/setup_22.x | bash - \
    && apt-get install -y --no-install-recommends nodejs \
    && rm -rf /var/lib/apt/lists/*

# 全局安装 Kimi Code CLI(官方 npm 包 @moonshot-ai/kimi-code,bin 名 kimi)
RUN npm install -g @moonshot-ai/kimi-code \
    && kimi --version

# 沙箱默认非 root 用户
RUN useradd -m -s /bin/bash user
USER user
WORKDIR /home/user
```

构建并验证:

```bash
docker build -f Dockerfile.sandbox -t agentpair-sandbox:latest .

# 验证
docker run --rm agentpair-sandbox:latest kimi --version
docker run --rm agentpair-sandbox:latest kimi acp --help 2>&1 | head -5
docker run --rm agentpair-sandbox:latest node --version   # 应输出 v22.x
```

构建命令与 `.env` 配置同 [opensandbox-deploy.md 2.2](./opensandbox-deploy.md#22-手动构建了解脚本做了什么),完成后 `SANDBOX_IMAGE=agentpair-sandbox:latest` 不变。

### 2.3 方式 B:运行时自动安装(首次启动慢)

不在镜像里预装,让 [kimi_cli_agent.py](../backend/app/agents/kimi_cli_agent.py) 在首次启动时执行 `KIMI_CLI_INSTALL_CMD` 安装。前提是镜像已含 Node.js >= 22.19(否则 `npm` 不存在或版本太低)。

`.env` 配置(默认值已可用):

```bash
KIMI_CLI_BIN=kimi
KIMI_CLI_INSTALL_CMD=npm install -g @moonshot-ai/kimi-code
```

> 注意:[BRIDGE_STARTUP_TIMEOUT 默认 30 秒](../backend/app/agents/acp_base.py),首次自动安装 `@moonshot-ai/kimi-code` 含 native postinstall 脚本,可能超时。生产环境建议用方式 A 预装。

### 2.4 方式 C:从源码构建(适合二次开发)

如果想改 kimi code 源码再用,可以本地 build 后复制到镜像:

```bash
# 在能联网的开发机上
git clone https://github.com/MoonshotAI/kimi-code.git
cd kimi-code
pnpm install              # 需要 pnpm >= 9(见 .nvmrc / package.json)
pnpm --filter @moonshot-ai/kimi-code build

# 产物在 apps/kimi-code/dist/main.mjs,复制到镜像内任意路径
# Dockerfile 里:
# COPY apps/kimi-code/dist/main.mjs /usr/local/lib/kimi/main.mjs
# RUN ln -s /usr/local/lib/kimi/main.mjs /usr/local/bin/kimi
```

> 不推荐:每次 kimi code 升级都要重新构建并同步到镜像,维护成本高。除非要改源码,否则用方式 A。

## 三、准备 LLM API 凭证

Kimi Code CLI **不从 shell 读取 `KIMI_API_KEY` 等密钥**(设计决策,见 `references/kimi-code-main/docs/zh/configuration/env-vars.md`),而是通过 `KIMI_MODEL_*` 环境变量族在内存里合成临时 provider。AgentPair 把用户在「智能体配置」中填写的三个字段映射成这些环境变量。

### 3.1 场景一:用 Moonshot 官方 API(最简单)

1. 在 https://platform.moonshot.cn/console/api-keys 申请 API Key
2. AgentPair「智能体配置」→ Kimi Code CLI:
   - **API Key**:粘贴 Moonshot API Key
   - **API Base URL**:留空(用 `kimi` provider 默认值 `https://api.moonshot.ai/v1`)
   - **模型名**:留空(默认 `kimi-for-coding`)或填具体模型如 `kimi-k2-0905-preview`

### 3.2 场景二:自部署 LLM 端点(开源模型本地推理)

适合企业内网 / 数据不出域 / 用开源模型(Kimi-K2 / DeepSeek / Qwen 等)的场景。任何兼容 OpenAI Chat Completions 协议的端点都行:

| 推理框架 | base_url 示例 | 模型名示例 |
|----------|---------------|------------|
| vLLM | `http://192.168.1.100:8000/v1` | `kimi-k2-instruct` |
| Xinference | `http://192.168.1.100:9997/v1` | `kimi-k2-instruct` |
| Ollama(OpenAI 兼容) | `http://192.168.1.100:11434/v1` | `kimi-k2:latest` |
| 自部署 Moonshot 兼容服务 | `https://your-internal.example.com/v1` | `kimi-for-coding` |

AgentPair「智能体配置」→ Kimi Code CLI:

- **API Key**:填端点要求的 key(无鉴权填任意非空字符串,如 `dummy`)
- **API Base URL**:填端点地址(必须含 `/v1` 后缀)
- **模型名**:填端点实际暴露的模型 ID

> 沙箱内能否访问该端点取决于沙箱网络策略。OpenSandbox 默认允许出网,自部署端点若在内网,需确认沙箱容器能路由到该 IP/域名。

### 3.3 环境变量映射对照

后端 `_build_credential_envs()`([acp_base.py](../backend/app/agents/acp_base.py))按 [registry.py](../backend/app/agents/registry.py) 中的 `credential_env` 映射:

| 用户填写字段 | 注入的环境变量 | 是否必填 | 默认值(由 registry 注入) |
|--------------|----------------|----------|---------------------------|
| `api_key` | `KIMI_MODEL_API_KEY` | 是 | — |
| `base_url` | `KIMI_MODEL_BASE_URL` | 否 | provider 内置默认(kimi 类型为 Moonshot 官方) |
| `model` | `KIMI_MODEL_NAME` | 否 | `kimi-for-coding`(registry 默认) |
| — | `KIMI_MODEL_PROVIDER_TYPE` | — | `kimi`(registry 默认,不暴露给用户) |

> `KIMI_MODEL_NAME` 同时是「启用开关」:设了之后 CLI 才会合成临时 provider。后端通过 `credential_env_defaults` 保证它始终有值,即使用户留空。

## 四、AgentPair 后端配置

在 `backend/.env` 中,确保以下项已正确设置:

```bash
# 沙箱模式(必须是 sandbox,mock 模式无 CLI)
SANDBOX_MODE=sandbox

# OpenSandbox Server 地址(见 opensandbox-deploy.md 第四节)
SANDBOX_SERVER_URL=http://your-server-ip:8080
SANDBOX_API_KEY=
SANDBOX_IMAGE=agentpair-sandbox:latest

# Kimi Code CLI 执行器配置(task.executor=kimi_cli 时生效)
# kimi 可执行文件名(沙箱内 PATH 查找或绝对路径)
KIMI_CLI_BIN=kimi
# 安装命令(沙箱内未检测到 kimi 时执行,需镜像有 Node.js >= 22.19)
KIMI_CLI_INSTALL_CMD=npm install -g @moonshot-ai/kimi-code
```

`KIMI_CLI_BIN` / `KIMI_CLI_INSTALL_CMD` 已有默认值,通常无需改动。重启后端:

```bash
cd backend
uvicorn app.main:app --reload
```

## 五、用户凭证配置

1. 登录 AgentPair 前端,点右上角齿轮图标进入「设置」
2. 在「智能体配置」区块找到 **Kimi Code CLI** 行,点「设置」
3. 按第三节填写三个字段:
   - API Key(必填,密码框)
   - API Base URL(选填,留空用默认)
   - 模型名(选填,留空默认 `kimi-for-coding`)
4. 勾选「启用此执行器」,点「保存」
5. 点「测试连接」验证:
   - 后端会创建临时沙箱,启动 `kimi acp` bridge
   - 注入 `KIMI_MODEL_*` 环境变量
   - ACP 握手 → `session/new` → `set_config_option(mode=yolo)` → 发送「你好」prompt
   - 流式推送阶段进度、模型思考增量、模型回答增量
   - 看到绿色「✓ 连接成功」即通过;失败会显示具体错误(认证失败 / 配额不足 / 网络不通等)

测试通过后,在「任务创建」页面的「执行器」下拉里就能看到 **Kimi Code CLI** 选项,选中即可提交任务。

## 六、验证

提交一个审计任务,执行器选 Kimi Code CLI,看后端日志:

```
[kimi_cli] 环境就绪: kimi 可用,bridge 脚本已写入 /home/user/.acp/acp_bridge.py
[kimi_cli] ACP bridge 后台启动: execution_id=...
[kimi_cli] ACP bridge 就绪
[kimi_cli] 跳过 authenticate(凭证经环境变量自动认证),authMethods=['login']
[kimi_cli] 已设置 mode=yolo(跳过权限确认)
[task=...] kimi_cli 第 1 轮完成: content=...字符, reasoning=...字符, tool_calls=N
```

ACP 原始通信日志落盘到 `backend/logs/acp/{task_id}_r{round_idx}_{timestamp}.jsonl`,可事后分析。

## 七、常见问题

### 7.1 `kimi: command not found` / `kimi acp` 启动失败

**原因**:沙箱镜像没装 kimi,或 Node 版本太低。

**排查**:

```bash
docker run --rm agentpair-sandbox:latest bash -lc "command -v kimi && kimi --version && node --version"
```

- `command -v kimi` 无输出 → 没装,检查 Dockerfile 是否含 `npm install -g @moonshot-ai/kimi-code`
- `kimi --version` 报 `engine` 错误 → Node 版本 < 22.19,改用 `setup_22.x`
- 镜像没问题但运行时报错 → 检查 `.env` 的 `KIMI_CLI_BIN` 是否与镜像内实际命令名一致

### 7.2 测试连接报「ACP bridge 启动超时」

**原因**:首次运行时自动安装 `@moonshot-ai/kimi-code`,30 秒超时不够;或沙箱无法访问 npm registry。

**解决**:
- 用方式 A(2.2)预装到镜像,避免运行时安装
- 或加大 `BRIDGE_STARTUP_TIMEOUT`([acp_base.py](../backend/app/agents/acp_base.py) 中常量)
- 国内网络可配 npm 镜像:`npm config set registry https://registry.npmmirror.com`

### 7.3 测试连接报「凭证认证失败」/「模型未响应」

**原因**:`KIMI_MODEL_API_KEY` 无效,或 `KIMI_MODEL_BASE_URL` 不可达,或模型名错误。

**排查**:

1. 在沙箱内直接验证端点可达:
   ```bash
   docker run --rm agentpair-sandbox:latest \
     curl -sS -o /dev/null -w "%{http_code}\n" \
       -H "Authorization: Bearer YOUR_KEY" \
       YOUR_BASE_URL/models
   ```
   返回 200 即端点可达
2. 确认 `KIMI_MODEL_NAME` 与端点实际暴露的模型 ID 一致(自部署端点的模型名通常在启动命令里指定)
3. 看后端日志的 ACP 错误码:
   - `-32000 Authentication required` → API Key 无效或未注入
   - `401 Unauthorized` → 端点拒绝认证
   - `404 model not found` → 模型名错误

### 7.4 `session/set_config_option(mode=yolo)` 失败

**原因**:某些 kimi code 版本不支持 `mode` configId,或值不是 `yolo`。

**排查**:看后端日志的 warning。`set_config_option` 失败**不阻塞主流程**,只是后续工具调用会触发权限确认(沙箱无 TTY 时会卡住或被拒绝)。

**解决**:升级 kimi code 到最新版(`npm update -g @moonshot-ai/kimi-code`),旧版可能只支持 `set_mode` 而非 `set_config_option`。

### 7.5 沙箱内访问自部署 LLM 端点超时

**原因**:OpenSandbox 沙箱默认走 bridge 网络,可能无法路由到内网 IP。

**解决**:
- 确认 `~/.sandbox.toml` 的 `[egress]` 段未禁用出网
- 自部署端点若在内网,确保 Server 宿主机能路由到该 IP(沙箱通过 Server 的网络栈出网)
- 必要时把端点部署在 Server 同一主机,用 `host.docker.internal` 或宿主机 IP 访问

### 7.6 Kimi 与 Qoder 同时预装,Node 版本冲突

qodercli 要求 Node >= 20,kimi 要求 Node >= 22.19。**统一用 Node 22.x**(qodercli 兼容 Node 22)。Dockerfile 示例:

```dockerfile
USER root
RUN curl -fsSL https://deb.nodesource.com/setup_22.x | bash - \
    && apt-get install -y --no-install-recommends nodejs \
    && rm -rf /var/lib/apt/lists/*
RUN npm install -g @qoder-ai/qodercli @moonshot-ai/kimi-code
```

## 八、与 Qoder CLI 的差异对照

| 方面 | Qoder CLI | Kimi Code CLI |
|------|-----------|---------------|
| `task.executor` | `qoder_cli` / `qoder_cli_cn` | `kimi_cli` |
| CLI 命令 | `qodercli --acp --yolo` | `kimi acp` |
| 权限绕过 | `--yolo` 启动参数 | `set_config_option(mode=yolo)` after `session/new` |
| 模型选择 | `--model` CLI 参数 | `KIMI_MODEL_NAME` 环境变量 |
| 凭证字段 | PAT(单字段) | api_key + base_url + model(三字段) |
| 凭证环境变量 | `QODER_PERSONAL_ACCESS_TOKEN` | `KIMI_MODEL_API_KEY` / `KIMI_MODEL_BASE_URL` / `KIMI_MODEL_NAME` |
| 账号体系 | qoder.com / qoder.cn(配额管理) | 任意 OpenAI 兼容端点(支持自部署) |
| Node 版本 | >= 20.0.0 | >= 22.19.0 |
| CLI 模型参数 | 支持(`inject_cli_model_args=True`) | 不支持(`inject_cli_model_args=False`) |
| 思考强度 | `--reasoning-effort` CLI 参数 | `set_config_option(thinking=...)` after `session/new` |
| 计费 | Qoder 账号配额(credits) | 由 LLM 端点决定(Moonshot 按 token 计费 / 自部署免费) |

## 参考链接

- Kimi Code 官方仓库:https://github.com/MoonshotAI/kimi-code
- Kimi Code 文档(中文):`references/kimi-code-main/docs/zh/`
  - 命令参考:`reference/kimi-command.md`
  - ACP 协议:`reference/kimi-acp.md`
  - 环境变量:`configuration/env-vars.md`
  - 供应商配置:`configuration/providers.md`
- Moonshot 开放平台:https://platform.moonshot.cn/
- OpenSandbox 部署:[opensandbox-deploy.md](./opensandbox-deploy.md)
- AgentPair 接入代码:
  - [kimi_cli_agent.py](../backend/app/agents/kimi_cli_agent.py)(薄封装)
  - [acp_base.py](../backend/app/agents/acp_base.py)(共享 ACP 基础设施)
  - [registry.py](../backend/app/agents/registry.py)(agent 类型注册表)
