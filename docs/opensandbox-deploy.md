# OpenSandbox 部署指南(Linux 服务器)

本文档指导如何在 Linux 服务器上部署 OpenSandbox Server,并让 AgentPair 后端连接到它。

> 配置项以 OpenSandbox 官方 `server/configuration.md` 为准。本文只覆盖 AgentPair 接入需要的最小配置。

## 前置条件

- **Linux 服务器**(Ubuntu 20.04+ / CentOS 7+ / Debian 11+ 推荐;macOS / Windows WSL2 也支持)
- **Docker Engine 20.10+** 已安装并运行(`docker --version` 能输出版本号)
- **Python 3.10+**(`python3 --version`)
- **pip / uv** 任一即可
- **服务器对外开放端口 8080**(或你自定义的端口),供后端连接

## 一、安装 OpenSandbox Server

### 1.1 安装 Server

`opensandbox-server` 是 CLI 应用(不是库),Debian/Ubuntu 12+ 的 Python 是 PEP 668 externally-managed,直接 `pip install` 会被拦。正确做法是用 `uv tool install` 或 `pipx`:它自动建独立 venv,把命令放到 `~/.local/bin/opensandbox-server`。

```bash
# 方式 A(推荐):uv tool install —— 需先装 uv
curl -LsSf https://astral.sh/uv/install.sh | sh
source $HOME/.local/bin/env
uv tool install opensandbox-server

# 方式 B:pipx —— 需先装 pipx
sudo apt install -y pipx
pipx ensurepath
pipx install opensandbox-server

# 让当前 shell 能找到 ~/.local/bin(两种方式装完都要执行一次,或重登)
export PATH="$HOME/.local/bin:$PATH"

# 验证:能列出子命令即装好(此 CLI 没有 --version)
opensandbox-server --help
```

> 不要用 `uvx opensandbox-server` 长期运行:`uvx` 是临时执行,每次启动都可能重新拉包,systemd 托管时路径也不好定位。装成正式命令更稳。
> 也不要用 `pip install --break-system-packages`:会污染系统 Python,可能破坏 apt 管理的包。

### 1.2 生成配置文件

```bash
# 生成 Docker runtime 配置模板(推荐)
opensandbox-server init-config ~/.sandbox.toml --example docker

# 覆盖已有配置
opensandbox-server init-config ~/.sandbox.toml --example docker --force
```

生成的模板包含 `[runtime]`、`[docker]`、`[egress]` 等全部必要字段。**必须检查/修改以下两项**:

```toml
[server]
# 模板默认是 127.0.0.1,只能本机访问。要让后端远程连接,必须改成 0.0.0.0
host = "0.0.0.0"
port = 8080

# 鉴权:留空则不鉴权,但非交互启动需设环境变量 OPENSANDBOX_INSECURE_SERVER=YES(见 1.4)
# 生产环境强烈建议设一个随机长字符串:
# api_key = "your-secret-api-key"

[runtime]
type = "docker"
# init-config 会自动填入当前版本的 execd 镜像,手写时不能省,否则启动失败
execd_image = "opensandbox/execd:v1.0.21"

[storage]
# 允许挂载到沙箱的宿主机路径前缀。空列表 = 禁止任何 host 挂载(安全默认)
# 要把 SSH key 挂载进沙箱,需放行对应路径前缀,例如:
# allowed_host_paths = ["/home"]
```

完整配置参考:https://github.com/opensandbox-group/OpenSandbox/blob/main/server/configuration.md

### 1.3 启动 Server

```bash
# 前台运行,看日志
opensandbox-server

# 后台运行 + 日志
nohup opensandbox-server > ~/opensandbox.log 2>&1 &

# 验证:返回 JSON 即成功
curl http://localhost:8080/health
```

### 1.4 关于鉴权的重要说明

`[server].api_key` 留空时,Server 仍然可以启动,但**非交互环境**(systemd / nohup / Docker / CI)下必须显式确认风险,否则启动会卡住:

```bash
# 方式 A:设环境变量(推荐用于 systemd / nohup)
export OPENSANDBOX_INSECURE_SERVER=YES

# 方式 B:在配置里设 api_key(生产推荐)
# [server]
# api_key = "your-secret-api-key"
```

设了 `api_key` 后,所有 API 请求(除 `/health`、`/docs`、`/redoc`)必须带 header `OPEN-SANDBOX-API-KEY: your-secret-api-key`。AgentPair 后端通过 `SANDBOX_API_KEY` 环境变量传入。

### 1.5 配置 systemd(可选,生产推荐)

`opensandbox-server` 装在 `~/.local/bin/`,systemd 的非登录 shell 默认不加载它,所以 ExecStart 必须用**绝对路径**,不能用 `$(which ...)`(在 `sudo tee` 的 heredoc 里 PATH 往往不含 `~/.local/bin`,会展开成空串)。

```bash
# 先确认绝对路径(应该是 ~/.local/bin/opensandbox-server)
which opensandbox-server
# 例如输出:/home/admin/.local/bin/opensandbox-server

sudo tee /etc/systemd/system/opensandbox.service > /dev/null <<EOF
[Unit]
Description=OpenSandbox Server
After=docker.service network.target
Requires=docker.service

[Service]
Type=simple
User=$(whoami)
# 用绝对路径!不要用 $(which ...),sudo 上下文里 PATH 可能不含 ~/.local/bin
ExecStart=/home/$(whoami)/.local/bin/opensandbox-server
Restart=on-failure
RestartSec=5
Environment=PATH=/usr/local/bin:/usr/bin:/bin:/home/$(whoami)/.local/bin
# 若 [server].api_key 留空,必须加这一行,否则非交互启动会卡住
Environment=OPENSANDBOX_INSECURE_SERVER=YES

[Install]
WantedBy=multi-user.target
EOF

sudo systemctl daemon-reload
sudo systemctl enable --now opensandbox
sudo systemctl status opensandbox
```

如果 `which opensandbox-server` 输出的不是 `/home/<user>/.local/bin/opensandbox-server`(比如用了 pipx 装在别的位置),把 ExecStart 和 PATH 里的路径换成实际输出。

## 二、准备沙箱镜像

AgentPair 在沙箱里执行 `git` / `rg`(ripgrep)/ `python3` / `awk` / `find` 等命令。官方 `ubuntu` 镜像不含 `git` 和 `rg`,需构建自定义镜像。

### 2.1 一键构建(推荐)

仓库提供了 `scripts/build-sandbox-image.sh`,自动生成 Dockerfile、构建镜像、验证必要工具都在:

```bash
# 在服务器上,进入 AgentPair 仓库根目录
# 默认同时预装五款 CLI:Qoder 国际版 + 国内版 + Kimi Code + Hermes + Codex
bash scripts/build-sandbox-image.sh

# 只装国内版 + Hermes + Codex(零 Node 依赖的国内版 + 不需 Node 的 Hermes + 需 Node 的 Codex)
bash scripts/build-sandbox-image.sh --no-qoder-cli --no-kimi-cli

# 只装 Hermes(Python 包,官方 install.sh 安装,不需要 Node.js)
bash scripts/build-sandbox-image.sh --no-qoder-cli --no-qoder-cli-cn --no-kimi-cli --no-codex-cli

# 不装 Hermes
bash scripts/build-sandbox-image.sh --no-hermes-cli

# 不装 Codex
bash scripts/build-sandbox-image.sh --no-codex-cli

# 仅基础工具(不装任何 CLI)
bash scripts/build-sandbox-image.sh --no-qoder-cli --no-qoder-cli-cn --no-kimi-cli --no-hermes-cli --no-codex-cli

# 服务器在国内时加 --cn-mirror 一键国内加速(避免 docker.io 拉取 ubuntu 超时)
bash scripts/build-sandbox-image.sh --cn-mirror

# 或仅换 Docker 基础镜像源(apt/npm 仍用官方源)
bash scripts/build-sandbox-image.sh --registry docker.m.daocloud.io
```

脚本会:
1. 检查 docker 可用(含 daemon 是否运行、当前用户是否在 docker 组)
2. 按参数生成 `Dockerfile.sandbox`(已存在且配置一致则跳过;配置变更会备份原文件后重新生成)
3. `docker build -t agentpair-sandbox:latest`
4. 逐个验证镜像内 `git` / `rg` / `python3` / `awk` / `find` / `curl` 及所选 CLI 都能找到

**国内镜像加速**(服务器在国内时推荐):`--cn-mirror` 一键启用四项国内源:
- Docker 基础镜像:`docker.m.daocloud.io/ubuntu:24.04`(DaoCloud 镜像,路径与 Docker Hub 一致)
- apt 源:阿里云 `mirrors.aliyun.com`(ubuntu 24.04 DEB822 格式 + 旧 sources.list 兼容)
- npm 源:`registry.npmmirror.com`(加速 qodercli/kimi/codex 的 `npm install -g`)
- PyPI 源:阿里云 `mirrors.aliyun.com/pypi/simple`(加速 Hermes install.sh 的 uv 依赖解析与下载)

若只换 Docker 基础镜像源(apt/npm 保持官方),用 `--registry <prefix>`,如 `--registry docker.m.daocloud.io`。注意阿里云容器镜像服务需带 `library/` 前缀,如 `--registry registry.cn-hangzhou.aliyuncs.com/library`。镜像源变更会触发 Dockerfile 重新生成(检测 `# @registry:` 标记)。

五款 CLI 的差异:
- **Qoder CLI 国际版**(`qodercli`):npm 包,需 Node.js >= 20.0.0,账号在 qoder.com
- **Qoder CN CLI 国内版**(`qoderclicn`,原通义灵码):零依赖二进制,仅需 curl 拉安装脚本,账号在 qoder.cn
- **Kimi Code CLI**(`kimi`):npm 包,需 Node.js >= 22.19,账号为任意 OpenAI 兼容端点
- **Hermes CLI**(`hermes`):Python 包(未发布 PyPI,官方 install.sh 装 uv+Python 3.11+源码),不需要 Node.js,支持 7 种 LLM 供应商
- **Codex CLI**(`codex`):npm 包,需 Node.js >= 16,OpenAI 官方,支持自定义 OpenAI 兼容端点

> Node 版本策略:qodercli 要求 >= 20.0.0,kimi 要求 >= 22.19,codex 要求 >= 16。只要 qoder_cli / kimi_cli / codex_cli 任一启用,统一装 Node 22.x(三者都兼容)。Hermes 是 Python 包(官方 install.sh 安装,需 Python >=3.11),不需要 Node.js,不影响 Node 决策。

完成后在 AgentPair 的 `.env` 里设 `SANDBOX_IMAGE=agentpair-sandbox:latest`。

### 2.2 手动构建(了解脚本做了什么)

脚本生成的 `Dockerfile.sandbox` 内容(基础工具部分,两版 CLI 的安装块见 2.3):

```dockerfile
FROM ubuntu:24.04

# 避免 tzdata 等交互式安装卡住
ENV DEBIAN_FRONTEND=noninteractive

# 基础工具:curl 用于 NodeSource 脚本(国际版)+ qoder.cn/install(国内版)
RUN apt-get update && apt-get install -y --no-install-recommends \
        git \
        ripgrep \
        python3 \
        python3-pip \
        ca-certificates \
        openssh-client \
        coreutils \
        findutils \
        gawk \
        curl \
    && rm -rf /var/lib/apt/lists/* \
    && ln -sf /usr/bin/python3 /usr/bin/python

# 沙箱默认非 root 用户 user,确保 home 目录存在
RUN useradd -m -s /bin/bash user
USER user
WORKDIR /home/user
```

手动构建命令:

```bash
docker build -f Dockerfile.sandbox -t agentpair-sandbox:latest .
# 验证
docker run --rm agentpair-sandbox:latest rg --version
docker run --rm agentpair-sandbox:latest git --version
```

Server 直接用本地 Docker daemon,无需推到 registry。

### 2.3 可选:Qoder CLI 执行器依赖

AgentPair 支持两种 Qoder CLI 执行器,账号体系不互通,可按需选用:

| 执行器 | task.executor | CLI 命令 | 账号 | 依赖 | PAT 环境变量 |
|--------|---------------|----------|------|------|--------------|
| 国际版 | `qoder_cli` | `qodercli` | qoder.com | Node.js >= 20.0.0 + npm | `QODER_PERSONAL_ACCESS_TOKEN` |
| 国内版 | `qoder_cli_cn` | `qoderclicn` | qoder.cn | 无(零依赖二进制) | `QODERCN_PERSONAL_ACCESS_TOKEN` |

[acp_bridge.py](../backend/app/agents/acp_bridge.py) 用 Python 标准库实现,两版均需 **Python3**(2.2 的镜像已含)。安装方式二选一:

#### 方式 A:镜像预装(推荐,启动快、无网络依赖)

**国际版**(在切到 `user` 之前用 root 安装):

```dockerfile
# 装 Node.js 20.x(Qoder CLI 要求 >= 20.0.0)
USER root
RUN curl -fsSL https://deb.nodesource.com/setup_20.x | bash - \
    && apt-get install -y --no-install-recommends nodejs \
    && rm -rf /var/lib/apt/lists/*

# 全局安装 Qoder CLI(官方 npm 包 @qoder-ai/qodercli)
RUN npm install -g @qoder-ai/qodercli
```

**国内版**(零依赖二进制,仅需 curl):

```dockerfile
# install 脚本把版本化二进制装到 ~/.qoder-cn/bin/qoderclicn/qoderclicn-<ver>,
# 并在 ~/.local/bin/qoderclicn 创建 symlink。root 执行时入口点在 /root/.local/bin,
# 切到非 root 用户后 PATH 不含该路径。用 cp -L 跟随 symlink 复制实际二进制到
# /usr/local/bin,不依赖 symlink 目标的版本号路径。
USER root
RUN curl -fsSL https://qoder.cn/install | bash \
    && test -e /root/.local/bin/qoderclicn \
    && cp -L /root/.local/bin/qoderclicn /usr/local/bin/qoderclicn \
    && chmod +x /usr/local/bin/qoderclicn \
    && /usr/local/bin/qoderclicn --version
```

构建后验证:

```bash
docker run --rm agentpair-sandbox:latest qodercli --version      # 国际版
docker run --rm agentpair-sandbox:latest qoderclicn --version    # 国内版
```

#### 方式 B:运行时自动安装(首次启动慢,需沙箱能访问外网)

不在镜像里预装,让 [qoder_cli_agent.py](../backend/app/agents/qoder_cli_agent.py) 在首次启动时执行 `*_INSTALL_CMD` 安装。对应 `.env` 配置(默认值已可用):

```bash
# 国际版(前提:镜像已装 Node.js >= 20.0.0,否则 npm 不存在)
QODER_CLI_BIN=qodercli
QODER_CLI_INSTALL_CMD=npm install -g @qoder-ai/qodercli

# 国内版(零依赖,仅需镜像含 curl)
QODER_CLI_CN_BIN=qoderclicn
QODER_CLI_CN_INSTALL_CMD=curl -fsSL https://qoder.cn/install | bash
```

> 注意:[BRIDGE_STARTUP_TIMEOUT 默认 30 秒](../backend/app/agents/qoder_cli_agent.py),首次自动安装可能超时。生产环境建议用方式 A 预装,避免每次任务都拉包。

### 2.4 可选:Kimi Code CLI 执行器依赖

AgentPair 还支持开源的 [Kimi Code CLI](https://github.com/MoonshotAI/kimi-code) 作为执行器(`task.executor=kimi_cli`),通过 ACP 协议通信,模型经 `KIMI_MODEL_*` 环境变量注入。

| 执行器 | task.executor | CLI 命令 | 账号 | 依赖 | 凭证环境变量 |
|--------|---------------|----------|------|------|--------------|
| Kimi Code | `kimi_cli` | `kimi acp` | 任意 OpenAI 兼容端点(含 Moonshot 官方 / 自部署) | Node.js >= 22.19 + npm | `KIMI_MODEL_API_KEY` / `KIMI_MODEL_BASE_URL` / `KIMI_MODEL_NAME` |

与 Qoder CLI 的关键差异:
- **ACP 启动命令**:`kimi acp` 子命令(非 `--acp` 标志)
- **权限绕过**:无 `--yolo` 启动参数,通过 `session/set_config_option(mode=yolo)` 在 `session/new` 后设置(由 [kimi_cli_agent.py](../backend/app/agents/kimi_cli_agent.py) 自动完成)
- **模型选择**:不支持 `--model` CLI 参数,经 `KIMI_MODEL_NAME` 环境变量注入
- **凭证字段**:不是 PAT,而是 `api_key` + `base_url` + `model` 三字段(用户在「智能体配置」填写)
- **Node 版本**:要求 >= 22.19(比 Qoder 的 20.0.0 高,若同镜像装两者统一用 Node 22.x)

安装方式二选一:

#### 方式 A:镜像预装(推荐,启动快、无网络依赖)

```dockerfile
# 装 Node.js 22.x(Kimi Code 要求 >= 22.19;Qoder CLI 也兼容 Node 22)
USER root
RUN curl -fsSL https://deb.nodesource.com/setup_22.x | bash - \
    && apt-get install -y --no-install-recommends nodejs \
    && rm -rf /var/lib/apt/lists/*

# 全局安装 Kimi Code CLI(官方 npm 包 @moonshot-ai/kimi-code,bin 名 kimi)
RUN npm install -g @moonshot-ai/kimi-code \
    && kimi --version
```

构建后验证:

```bash
docker run --rm agentpair-sandbox:latest kimi --version
docker run --rm agentpair-sandbox:latest node --version   # 应输出 v22.x
```

#### 方式 B:运行时自动安装(首次启动慢,需沙箱能访问外网)

不在镜像里预装,让 [kimi_cli_agent.py](../backend/app/agents/kimi_cli_agent.py) 在首次启动时执行 `KIMI_CLI_INSTALL_CMD` 安装。前提是镜像已含 Node.js >= 22.19。对应 `.env` 配置(默认值已可用):

```bash
KIMI_CLI_BIN=kimi
KIMI_CLI_INSTALL_CMD=npm install -g @moonshot-ai/kimi-code
```

> 注意:[BRIDGE_STARTUP_TIMEOUT 默认 30 秒](../backend/app/agents/acp_base.py),首次自动安装 `@moonshot-ai/kimi-code` 含 native postinstall 脚本,可能超时。生产环境建议用方式 A 预装。

#### 凭证配置

Kimi Code 不从 shell 读取 `KIMI_API_KEY` 等密钥,而是通过 `KIMI_MODEL_*` 环境变量族在内存里合成临时 provider。用户在「智能体配置」→ Kimi Code CLI 中填写三个字段,后端按 [registry.py](../backend/app/agents/registry.py) 的 `credential_env` 映射:

| 用户填写字段 | 注入的环境变量 | 必填 | 默认值(由 registry 注入) |
|--------------|----------------|------|---------------------------|
| API Key | `KIMI_MODEL_API_KEY` | 是 | — |
| API Base URL | `KIMI_MODEL_BASE_URL` | 否 | provider 内置默认(Moonshot 官方) |
| 模型名 | `KIMI_MODEL_NAME` | 否 | `kimi-for-coding` |
| — | `KIMI_MODEL_PROVIDER_TYPE` | — | `kimi`(不暴露给用户) |

两种典型场景:
- **Moonshot 官方 API**:申请 API Key 填入,base_url 和模型名留空(用默认)
- **自部署 LLM 端点**(vLLM / Xinference / Ollama 等 OpenAI 兼容端点):三个字段都填,base_url 含 `/v1` 后缀。沙箱需能访问该端点

### 2.5 可选:Hermes CLI 执行器依赖

AgentPair 还支持开源的 [Hermes CLI](https://github.com/NousResearch/hermes-agent) 作为执行器(`task.executor=hermes_cli`),通过 ACP 协议通信,支持多种 LLM 供应商。

| 执行器 | task.executor | CLI 命令 | 账号 | 依赖 | 凭证注入方式 |
|--------|---------------|----------|------|------|--------------|
| Hermes | `hermes_cli` | `hermes acp` | 任意 LLM 供应商(OpenRouter/Anthropic/OpenAI/GLM/Kimi/MiniMax/Gemini) | Python >=3.11(官方 install.sh) | 环境变量(API Key)+ config.yaml(模型/provider) |

与 Qoder/Kimi 的关键差异:
- **ACP 启动命令**:`hermes acp` 子命令(与 Kimi 相同,非 `--acp` 标志)
- **权限绕过**:无 `--yolo` 启动参数,通过 `HERMES_YOLO_MODE=1` 环境变量绕过(模块导入时读取并冻结,由 [hermes_cli_agent.py](../backend/app/agents/hermes_cli_agent.py) 自动注入)
- **模型配置**:`LLM_MODEL` 环境变量已废弃,模型名/provider/base_url 必须写入 `~/.hermes/config.yaml`(由 `pre_bridge_hook` 在 bridge 启动前自动写入)
- **API Key 动态映射**:Hermes 按 provider 读取不同的环境变量名(如 `OPENROUTER_API_KEY` / `ANTHROPIC_API_KEY`),由 `credential_env_builder` 回调动态构建,无法用 registry 的静态 `credential_env` 映射
- **凭证字段**:不是 PAT,而是 `provider`(选择)+ `api_key` + `base_url`(可选)+ `model`(可选)四字段
- **运行时依赖**:Python 包(未发布 PyPI,用官方 install.sh 装 uv + Python 3.11 + 源码),不需要 Node.js

安装方式二选一:

#### 方式 A:镜像预装(推荐,启动快、无网络依赖)

`build-sandbox-image.sh` 默认预装 Hermes(见 2.1 一键构建,可用 `--no-hermes-cli` 关闭)。其 Dockerfile 片段等价于:

```dockerfile
# hermes-agent 未发布到 PyPI,`pip install hermes-agent` 会失败;改用官方 install.sh(同 README)。
# 脚本自动:装 uv + Python 3.11 → clone 源码 → 建 venv → 装依赖 → 符号链接 hermes 命令。
# 需 Python >=3.11:基础镜像 24.04 自带系统 python3 = 3.12(供 apt + 智能体脚本用);
# install.sh 通用路径仍用 uv 装隔离的 3.11 给 Hermes venv(两者均 >=3.11,互不冲突)。
# root 安装走 FHS 布局:代码 /usr/local/lib/hermes-agent,命令 /usr/local/bin/hermes(全用户 PATH 可达),
# uv 管理的 Python 放 /usr/local/share(世界可读,避免 venv 解释器符号链接被困在 /root,非 root user 无权访问)。
# --skip-setup 跳过交互式配置向导;--skip-browser 跳过 Playwright/Node 浏览器依赖(hermes acp 不需要);
# --non-interactive 防 tty 提示卡住。两步「先下载再执行」比 curl|bash 更安全、可审计。
USER root
RUN curl -fsSL https://hermes-agent.nousresearch.com/install.sh -o /tmp/hermes-install.sh \
    && bash /tmp/hermes-install.sh --skip-setup --skip-browser --non-interactive \
    && rm -f /tmp/hermes-install.sh \
    && hermes --version
```

构建后验证:

```bash
docker run --rm agentpair-sandbox:latest hermes --version
```

> 如需可复现/可审计的构建,可在 install.sh 后追加 `--branch <tag>` 或 `--commit <sha>` 固定版本(见 install.sh `--help`)。

#### 方式 B:运行时自动安装(首次启动慢,需沙箱能访问外网,可能超时)

不在镜像里预装,让 [hermes_cli_agent.py](../backend/app/agents/hermes_cli_agent.py) 在首次启动时执行 `HERMES_CLI_INSTALL_CMD` 安装(官方 install.sh,需沙箱能访问 hermes-agent.nousresearch.com / GitHub / PyPI)。对应 `.env` 配置(默认值已可用):

```bash
HERMES_CLI_BIN=hermes
HERMES_CLI_INSTALL_CMD=curl -fsSL https://hermes-agent.nousresearch.com/install.sh -o /tmp/hermes-install.sh && bash /tmp/hermes-install.sh --skip-setup --skip-browser --non-interactive && rm -f /tmp/hermes-install.sh
```

> 注意:运行时源码安装含 `uv sync`(较多依赖),首次通常需 1-5 分钟,而 [安装超时仅 120 秒](../backend/app/agents/acp_base.py)、`BRIDGE_STARTUP_TIMEOUT` 默认 30 秒,极可能超时。**生产环境强烈建议用方式 A 预装**(root FHS → `/usr/local/bin/hermes`,PATH 必达);方式 B 仅作未预装时的兜底(非 root 装到 `~/.local/bin`,需该目录在 PATH)。

#### 凭证配置

Hermes 不从单一环境变量读取 API Key,而是按 provider 读取对应的 `<PROVIDER>_API_KEY`。用户在「智能体配置」→ Hermes CLI 中填写四个字段,后端按 [registry.py](../backend/app/agents/registry.py) 的 `credential_fields` 动态渲染表单:

| 用户填写字段 | 注入方式 | 必填 | 说明 |
|--------------|----------|------|------|
| LLM 供应商 | 按 provider 映射环境变量名 | 是 | openrouter / anthropic / openai / zai / kimi-coding / minimax / gemini |
| API Key | `<PROVIDER>_API_KEY` 环境变量 | 是 | 所选供应商对应的 API Key |
| API Base URL | config.yaml `model.base_url` | 否 | 留空用供应商官方端点;自部署填完整 URL |
| 模型名 | config.yaml `model.default` | 否 | 留空用供应商默认模型 |

后端注入流程(由 [hermes_cli_agent.py](../backend/app/agents/hermes_cli_agent.py) 自动完成):
1. **`credential_env_builder`**:按 provider 选择注入 `HERMES_YOLO_MODE=1` + `<PROVIDER>_API_KEY`(如 `OPENROUTER_API_KEY` / `ANTHROPIC_API_KEY` / `GLM_API_KEY` 等)
2. **`pre_bridge_hook`**:向沙箱写入 `~/.hermes/config.yaml`,含 `model.default`(模型名)+ `model.provider`(供应商)+ `model.base_url`(端点)

各供应商的环境变量映射:

| 供应商 | config.yaml provider | API Key 环境变量 | 默认 base_url | 默认模型 |
|--------|----------------------|-------------------|---------------|----------|
| OpenRouter(推荐) | `openrouter` | `OPENROUTER_API_KEY` | https://openrouter.ai/api/v1 | anthropic/claude-opus-4.6 |
| Anthropic | `anthropic` | `ANTHROPIC_API_KEY` | (Hermes 内置) | claude-opus-4.6 |
| OpenAI | `custom` | `OPENAI_API_KEY` | https://api.openai.com/v1 | gpt-4o |
| z.ai / ZhipuAI | `zai` | `GLM_API_KEY` | https://api.z.ai/api/paas/v4 | glm-4-plus |
| Kimi / Moonshot | `kimi-coding` | `KIMI_API_KEY` | https://api.kimi.com/coding/v1 | kimi-k2.5 |
| MiniMax | `minimax` | `MINIMAX_API_KEY` | https://api.minimax.io/v1 | MiniMax-M2 |
| Google Gemini | `gemini` | `GOOGLE_API_KEY` | https://generativelanguage.googleapis.com/v1beta/openai | gemini-3-flash-preview |

典型场景:
- **OpenRouter(推荐入门)**:在 [openrouter.ai/keys](https://openrouter.ai/keys) 申请 API Key,选 OpenRouter 供应商,模型用 `anthropic/claude-opus-4.6` 等 OpenRouter 聚合格式
- **Anthropic 直连**:在 [console.anthropic.com](https://console.anthropic.com) 申请 API Key,选 Anthropic 供应商,模型用 `claude-opus-4.6`
- **自部署 LLM 端点**:选对应的供应商(如 OpenAI 兼容端点选 OpenAI),base_url 填完整 URL(含 `/v1` 后缀),沙箱需能访问该端点

### 2.6 可选:Codex CLI 执行器依赖

AgentPair 还支持 [OpenAI Codex CLI](https://github.com/openai/codex)(Apache-2.0 开源)作为执行器(`task.executor=codex_cli`)。与 Hermes/Kimi/Qoder 不同,Codex **不原生支持 ACP 协议**,而是通过 [codex_bridge.py](../backend/app/agents/codex_bridge.py) 翻译 `codex exec --json` 的 JSONL 事件流为 ACP 协议。

| 执行器 | task.executor | CLI 命令 | 账号 | 依赖 | 凭证注入方式 |
|--------|---------------|----------|------|------|--------------|
| Codex | `codex_cli` | `codex exec --json`(经 codex_bridge.py 翻译为 ACP) | OpenAI 或任意 OpenAI 兼容端点 | Node.js >= 16 + npm | 环境变量(`CODEX_API_KEY`)+ config.toml(模型/provider/wire_api) |

与 Hermes/Kimi/Qoder 的关键差异:
- **不原生支持 ACP**:其他三款 CLI 都内置 `acp` 子命令(Hermes/Kimi)或 ACP 协议(Qoder),Codex 没有,改用 `codex exec --json` 非交互模式输出 JSONL 事件,由 [codex_bridge.py](../backend/app/agents/codex_bridge.py) 翻译为 ACP 通知
- **多轮会话**:Codex 用 `codex exec resume <thread_id>` 恢复之前的会话(首次调用提取 thread_id,后续轮次复用),而非 ACP 的 `session/load`
- **配置文件**:用 `~/.codex/config.toml`(TOML 格式),不是 Hermes 的 `~/.hermes/config.yaml`(YAML)或 Kimi 的环境变量合成
- **审批策略**:`approval_policy = "full-auto"` 跳过所有审批(非交互模式必须)
- **沙箱模式**:`sandbox_mode = "danger-full-access"` 关闭 Codex 内部沙箱(我们用 OpenSandbox 隔离)
- **通信协议**:支持 `wire_api` 选择(Responses API / Chat Completions API),第三方端点推荐 `chat`
- **凭证字段**:`api_key` + `base_url`(可选)+ `model`(可选)+ `wire_api`(可选)四字段
- **运行时依赖**:Node.js >= 16(不是 Python pip,与 Kimi/Qoder 国际版同属 Node 类 CLI)

安装方式二选一:

#### 方式 A:镜像预装(推荐,启动快、无网络依赖)

```dockerfile
# Codex CLI 是 Node.js 包,需先装 Node.js >= 16(与 qodercli / kimi 共享 Node 22.x 运行时)
# 见 2.3 / 2.4 的 Node.js 安装块,统一用 setup_22.x(codex 要求 >= 16,Node 22 兼容)
USER root
RUN curl -fsSL https://deb.nodesource.com/setup_22.x | bash - \
    && apt-get install -y --no-install-recommends nodejs \
    && rm -rf /var/lib/apt/lists/*

# 全局安装 Codex CLI(官方 npm 包 @openai/codex,bin 名 codex)
RUN npm install -g @openai/codex \
    && codex --version
```

构建后验证:

```bash
docker run --rm agentpair-sandbox:latest codex --version
```

#### 方式 B:运行时自动安装(首次启动慢,需沙箱能访问外网)

不在镜像里预装,让 [codex_cli_agent.py](../backend/app/agents/codex_cli_agent.py) 在首次启动时执行 `CODEX_CLI_INSTALL_CMD` 安装。前提是镜像已含 Node.js >= 16。对应 `.env` 配置(默认值已可用):

```bash
CODEX_CLI_BIN=codex
CODEX_CLI_INSTALL_CMD=npm install -g @openai/codex
```

> 注意:[BRIDGE_STARTUP_TIMEOUT 默认 30 秒](../backend/app/agents/acp_base.py),首次自动安装 `@openai/codex` 可能超时。生产环境建议用方式 A 预装。

#### 凭证配置

Codex 从 `~/.codex/config.toml` 读取模型/provider 配置,API Key 经 `CODEX_API_KEY` 环境变量注入(config.toml 的 `env_key` 指向它)。用户在「智能体配置」→ Codex CLI 中填写四个字段,后端按 [registry.py](../backend/app/agents/registry.py) 的 `credential_fields` 动态渲染表单:

| 用户填写字段 | 注入方式 | 必填 | 默认值 |
|--------------|----------|------|--------|
| API Key | `CODEX_API_KEY` 环境变量 | 是 | — |
| API Base URL | config.toml `model_providers.agentpair.base_url` | 否 | 留空用 OpenAI 官方端点 |
| 模型名 | config.toml `model` | 否 | `gpt-5` |
| Wire API | config.toml `model_providers.agentpair.wire_api` | 否 | `responses`(Responses API) |

后端注入流程(由 [codex_cli_agent.py](../backend/app/agents/codex_cli_agent.py) 的 `pre_bridge_hook` 自动完成):
1. **`credential_env`**(registry 静态映射):`api_key` → `CODEX_API_KEY` 环境变量
2. **`pre_bridge_hook`**:向沙箱写入 `~/.codex/config.toml`,含:
   - `model`(模型名)
   - `approval_policy = "full-auto"`(跳过审批)
   - `sandbox_mode = "danger-full-access"`(关闭 Codex 内部沙箱)
   - 若填了 `base_url`:额外写 `[model_providers.agentpair]` 表(base_url + wire_api + env_key),并设 `model_provider = "agentpair"`
   - 若 `base_url` 留空:不写自定义 provider,Codex 用默认 OpenAI provider

`wire_api` 两种取值:
- `responses`(默认):OpenAI Responses API,Codex 0.81.0+ 默认,GPT-5/o 系列推荐
- `chat`:Chat Completions API,大多数第三方/本地模型支持(自部署端点推荐)

典型场景:
- **OpenAI 官方 API**:在 [platform.openai.com/api-keys](https://platform.openai.com/api-keys) 申请 API Key,base_url 留空,模型用 `gpt-5` 或 `o4-mini`,wire_api 用默认 `responses`
- **自部署 LLM 端点**(vLLM / Xinference / Ollama 等 OpenAI 兼容端点):三个字段都填,base_url 含 `/v1` 后缀,wire_api 选 `chat`(兼容性更好),沙箱需能访问该端点

## 三、配置 SSH Key(给沙箱用,可选)

沙箱里执行 `git clone git@github.com:...` 需要 SSH 凭证。如果你只用 HTTPS+token 方式 clone(后端 `clone_repo_with_fallback` 会优先用 token),可以跳过本节。

### 3.1 生成专用 SSH Key

```bash
ssh-keygen -t ed25519 -C "opensandbox@your-server" -f ~/.ssh/id_ed25519_opensandbox -N ""
cat ~/.ssh/id_ed25519_opensandbox.pub
```

### 3.2 添加到 GitHub

- 打开 https://github.com/settings/keys
- 点 "New SSH key",把上一步输出的公钥粘贴进去
- Title 随意,比如 `OpenSandbox Server`

### 3.3 配置 SSH 自动用这个 key

```bash
cat >> ~/.ssh/config <<EOF

Host github.com
    HostName github.com
    User git
    IdentityFile ~/.ssh/id_ed25519_opensandbox
    StrictHostKeyChecking no
EOF

chmod 600 ~/.ssh/config

# 测试
ssh -T git@github.com
# 看到 "Hi xxx! You've successfully authenticated" 即成功
```

### 3.4 让沙箱能读到 SSH Key(关键)

OpenSandbox 的 `[docker]` 段**没有** `volumes` 字段。挂载宿主机目录到沙箱的正确方式是:

1. **Server 端**:在 `~/.sandbox.toml` 的 `[storage].allowed_host_paths` 放行 SSH 目录所在路径前缀
2. **后端**:通过 SDK 的 `volumes` 参数挂载(已在 `sandbox/client.py` 实现)

修改 `~/.sandbox.toml`:

```toml
[storage]
# 放行 /home 前缀,允许挂载 ~/.ssh 到沙箱
allowed_host_paths = ["/home"]
```

重启 Server:

```bash
sudo systemctl restart opensandbox
```

在 AgentPair 后端的 `.env` 里设:

```bash
# 挂载宿主机 ~/.ssh 到沙箱 /home/user/.ssh(只读)
SANDBOX_SSH_KEY_HOST_PATH=~/.ssh
```

后端 `client.py` 会自动把这个路径作为只读 Volume 挂载到每个沙箱的 `/home/user/.ssh`。

## 四、后端连接配置

在 AgentPair 后端的 `backend/.env` 里配置:

```bash
# 切换到真实沙箱模式
SANDBOX_MODE=sandbox

# OpenSandbox Server 地址(远程服务器填 IP)
SANDBOX_SERVER_URL=http://your-server-ip:8080

# API Key(对应 Server 的 [server].api_key;Server 留空则这里也留空)
SANDBOX_API_KEY=

# 沙箱镜像(第二节构建的自定义镜像)
SANDBOX_IMAGE=agentpair-sandbox:latest

# 沙箱超时(分钟)
SANDBOX_TIMEOUT_MINUTES=30

# 可选:挂载宿主机 SSH key(第三节)
SANDBOX_SSH_KEY_HOST_PATH=~/.ssh

# 可选:资源限制
SANDBOX_CPU=2
SANDBOX_MEMORY=4Gi
```

确保后端安装了 OpenSandbox SDK:

```bash
cd backend
pip install opensandbox
```

重启后端:

```bash
uvicorn app.main:app --reload
```

## 五、验证

提交一个审计任务,看后端日志里是否出现 `[sandbox] git clone` 而不是 `[mock]`:

```
[sandbox] git clone: git@github.com:xxx/xxx.git
[sandbox] search: rg --line-number ...
```

如果看到 `[mock]`,说明 `SANDBOX_MODE` 没切到 `sandbox`。

## 六、常见问题

### 6.1 Server 启动卡住 / 无输出

**原因**:`[server].api_key` 留空且未设 `OPENSANDBOX_INSECURE_SERVER=YES`,非交互环境会等待 TTY 确认。

**解决**:要么在 `[server].api_key` 设一个值,要么设环境变量 `OPENSANDBOX_INSECURE_SERVER=YES`。

### 6.2 后端连不上 Server

**排查**:
1. 确认 `~/.sandbox.toml` 里 `[server].host = "0.0.0.0"`(模板默认 127.0.0.1,只能本机访问)
2. 确认防火墙放行 8080 端口:`sudo ufw allow 8080` 或 `firewall-cmd --add-port=8080/tcp`
3. 在后端机器上 `curl http://your-server-ip:8080/health` 验证连通性

### 6.3 沙箱里 git clone 失败:Permission denied (publickey)

**原因**:沙箱没读到 SSH key,或 key 没添加到 GitHub。

**排查**:
1. 确认 `.env` 里 `SANDBOX_SSH_KEY_HOST_PATH` 已设
2. 确认 Server 的 `[storage].allowed_host_paths` 放行了对应路径前缀
3. 后端日志看 clone 失败的 stderr

### 6.4 沙箱里 rg 命令不存在

**原因**:用了 `ubuntu` 官方镜像,没装 ripgrep。

**解决**:按第二节构建 `agentpair-sandbox:latest` 自定义镜像,并在 `.env` 设 `SANDBOX_IMAGE=agentpair-sandbox:latest`。

### 6.5 沙箱创建失败:image pull 超时

`ubuntu` / `agentpair-sandbox` 镜像在 Server 本地。若用了远程 registry 镜像,国内拉取可能慢:
- 配置 Docker 镜像加速器(阿里云 ACR 等)
- 或预先 `docker pull` 到本地

### 6.6 沙箱执行命令超时

`SANDBOX_TIMEOUT_MINUTES` 是整个沙箱的生命周期超时。单个命令超时在 `sandbox_tools.py` 里:
- `git clone`:120s
- 其他命令:60s(默认)
- `semgrep`:300s

如需调整,改 `sandbox_tools.py` 对应调用的 `timeout` 参数。

### 6.7 沙箱内存/CPU 不够

在 AgentPair 的 `.env` 配置:

```bash
SANDBOX_CPU=2
SANDBOX_MEMORY=4Gi
```

后端会通过 SDK 的 `resource` 参数传给 Server。注意:**不要**在 `~/.sandbox.toml` 的 `[docker]` 段找 `memory` / `cpus` 字段——官方配置没有这两项,资源限制只能通过 SDK 在创建沙箱时传入。

## 七、生产环境注意事项

1. **API Key 鉴权**:生产环境一定要给 Server 设 `[server].api_key`,否则任何人都能创建沙箱
2. **网络隔离**:Server 端口只对后端服务开放,不要暴露到公网
3. **资源配额**:用 `SANDBOX_CPU` / `SANDBOX_MEMORY` 限制单沙箱资源,防恶意消耗
4. **日志留存**:Server 日志要收集,便于排查沙箱执行问题
5. **定期清理**:沙箱意外退出可能留下 dangling 容器,定期 `docker container prune`

## 配置项对照表

| AgentPair `.env` | OpenSandbox Server 配置 | 说明 |
|---|---|---|
| `SANDBOX_SERVER_URL` | `[server].host` + `[server].port` | 后端解析出 `host:port` 传给 SDK 的 `domain` |
| `SANDBOX_API_KEY` | `[server].api_key` | 两边必须一致,或都留空 |
| `SANDBOX_IMAGE` | — | 沙箱容器镜像,Server 本地需存在 |
| `SANDBOX_SSH_KEY_HOST_PATH` | `[storage].allowed_host_paths` | 后端挂载,Server 放行路径前缀 |
| `SANDBOX_CPU` / `SANDBOX_MEMORY` | — | 通过 SDK `resource` 参数传入 |

## 参考链接

- 官方仓库:https://github.com/opensandbox-group/OpenSandbox
- Server 配置参考:https://github.com/opensandbox-group/OpenSandbox/blob/main/server/configuration.md
- Python SDK 文档:https://open-sandbox.ai/sdks/python
- 安装指南:https://open-sandbox.ai/getting-started/installation
