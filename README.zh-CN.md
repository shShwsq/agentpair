# AgentPair

> 双智能体协作的代码分析平台

[English](./README.md) · **简体中文**

AgentPair 的核心创新是 **user_agent 模拟用户追问** 的交互模式 —— 在单 ReAct 架构之上叠加意图对齐与结果审视能力。`user_agent` 负责动态生成覆盖度清单、对照清单审视执行结果、并在覆盖不足时构造定向追问;`react_agent`(或外部 CLI 执行器)负责实际执行代码分析。两者多轮协作,直到覆盖度满足才输出结构化结果。

## 核心特性

- **双智能体协作**:`user_agent`(意图对齐 + 结果审视)+ `react_agent`(执行)多轮迭代,自动补齐覆盖盲区
- **执行器抽象层**:内置 ReAct 智能体 / Qoder CLI / Kimi Code CLI / Hermes CLI / Codex CLI 等可插拔执行器,通过 ACP 协议统一通信
- **场景模板化**:安全审计、代码审查等场景作为快捷模板(预设提示词 + 推荐 skill),checklist 由 LLM 动态生成 + 用户编辑确认
- **沙箱隔离**:基于 [OpenSandbox](https://github.com/opensandbox/opensandbox) 的容器化执行,工具调用在隔离环境完成
- **多 Git 平台**:统一抽象层支持 GitHub / Gitee,OAuth 登录 + 私有仓库绑定 + 自动克隆
- **流式输出**:思考过程、工具调用、计划清单实时推送到前端
- **用户澄清循环**:任务启动前 `user_agent` 可向用户提问,意图对齐后再执行
- **技能系统**:可加载专家 SKILL.md 指令,按任务选择性启用

## 技术栈

| 层 | 技术 |
|---|---|
| 后端 | FastAPI · SQLAlchemy 2.0 · PostgreSQL(psycopg3)· Pydantic v2 · OpenAI SDK |
| 前端 | Vue 3 · TypeScript · Vite · Pinia · Vue Router |
| 沙箱 | OpenSandbox(容器化代码执行环境) |
| 认证 | JWT · OAuth 2.0(GitHub / Gitee)· Fernet 对称加密(令牌存储) |
| LLM | 兼容 OpenAI 协议(默认阿里云 DashScope / 通义千问) |

## 项目结构

```
AgentPair/
├── backend/                  # FastAPI 后端
│   ├── app/
│   │   ├── agents/           # 智能体(user_agent / react_agent / orchestrator)
│   │   ├── llm/              # LLM 客户端封装
│   │   ├── models/           # SQLAlchemy 数据模型
│   │   ├── routers/          # API 路由(auth / tasks / git_provider / ...)
│   │   ├── sandbox/          # OpenSandbox 客户端封装
│   │   ├── scenarios/        # 场景模板(安全审计 / 代码审查)
│   │   ├── schemas/          # Pydantic 请求/响应模型
│   │   ├── skills/           # 技能加载器 + skill 注册表
│   │   ├── tools/            # ReAct 工具(clone_repo / search_code / ...)
│   │   ├── config.py         # 环境变量配置(pydantic-settings)
│   │   ├── git_provider.py   # Git 平台抽象层(GitHub / Gitee)
│   │   └── main.py           # FastAPI 入口
│   ├── skills/               # SKILL.md 技能定义文件
│   ├── requirements.txt
│   └── .env.example
├── frontend/                 # Vue 3 前端
│   ├── src/
│   │   ├── api/              # API 客户端
│   │   ├── components/       # 可复用组件
│   │   ├── stores/           # Pinia 状态管理
│   │   ├── views/            # 页面视图
│   │   └── types/            # TypeScript 类型定义
│   ├── package.json
│   └── .env.example
├── docs/                     # 文档(spec / Roadmap / 沙箱部署)
├── scripts/                  # 辅助脚本(沙箱镜像构建等)
└── references/               # 参考资料
```

## 环境要求

- **Python** ≥ 3.13(开发环境使用 3.14)
- **Node.js** ≥ 22.0.0(开发环境使用 24.x)
- **PostgreSQL**(阿里云 RDS 或自建均可)
- **OpenSandbox Server**(可选,未部署时可设 `SANDBOX_MODE=mock` 用本地文件系统模拟)

## 快速开始

### 1. 克隆仓库

```bash
git clone <repo-url> AgentPair
cd AgentPair
```

### 2. 后端配置与启动

```bash
cd backend

# 创建虚拟环境(Python 3.13+)
python -m venv .venv
# Windows
.venv\Scripts\Activate.ps1
# Linux/macOS
source .venv/bin/activate

# 安装依赖
pip install -r requirements.txt

# 复制环境变量模板并填写
cp .env.example .env
# 编辑 .env,至少填写 DATABASE_URL / LLM_API_KEY / JWT_SECRET / GITHUB_TOKEN_SECRET

# 启动开发服务器(默认 0.0.0.0:8000)
uvicorn app.main:app --reload
```

启动后访问 `http://localhost:8000/docs` 查看 API 文档(Swagger UI)。

### 3. 前端配置与启动

```bash
cd frontend

# 安装依赖
npm install

# 复制环境变量模板并填写
cp .env.example .env
# 编辑 .env,填写 VITE_GITHUB_OAUTH_CLIENT_ID 等

# 启动开发服务器(默认 http://localhost:5173)
npm run dev
```

前端通过 Vite proxy 把 `/api/*` 请求代理到后端 `http://localhost:8000`(自动去掉 `/api` 前缀)。

### 4. 访问应用

打开 `http://localhost:5173`,注册账号(或用 GitHub / Gitee OAuth 登录)后即可开始使用。

---

## 环境变量配置

### 后端环境变量(`backend/.env`)

#### 数据库

| 变量 | 说明 | 默认值 |
|---|---|---|
| `DATABASE_URL` | PostgreSQL 连接串,格式 `postgresql+psycopg://用户:密码@主机:端口/库名` | **必填** |
| `DB_REBUILD_ON_START` | 启动时是否 drop_all + create_all 重建表(会清空数据,schema 变更时临时设 `true`) | `false` |

#### 应用

| 变量 | 说明 | 默认值 |
|---|---|---|
| `APP_ENV` | 运行环境(`development` / `production`) | `development` |
| `APP_DEBUG` | 调试模式 | `true` |
| `APP_HOST` | 监听地址 | `0.0.0.0` |
| `APP_PORT` | 监听端口 | `8000` |
| `LOG_LEVEL` | 日志级别(留空则按 `APP_DEBUG` 决定) | 空 |
| `APP_BASE_URL` | 应用基础 URL(用于邮件验证/重置链接) | `http://localhost:5173` |

#### 认证与加密

| 变量 | 说明 | 默认值 |
|---|---|---|
| `JWT_SECRET` | JWT 签名密钥(**生产环境必须修改**) | `change_me_in_production` |
| `JWT_ALGORITHM` | JWT 算法 | `HS256` |
| `JWT_EXPIRE_MINUTES` | JWT 过期时间(分钟) | `1440` |
| `GITHUB_TOKEN_SECRET` | Git access_token 加密密钥(Fernet,32 字节 base64)。**留空则每次重启自动生成,导致已存 token 无法解密;生产环境必须固定**。生成方式:`python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"` | 空 |

#### Git 平台 OAuth

GitHub 和 Gitee 二者均支持,按需配置。留空的平台对应路由会返回错误。

| 变量 | 说明 |
|---|---|
| `GITHUB_OAUTH_CLIENT_ID` | GitHub OAuth App Client ID(在 https://github.com/settings/developers 创建) |
| `GITHUB_OAUTH_CLIENT_SECRET` | GitHub OAuth App Client Secret |
| `GITHUB_OAUTH_REDIRECT_URI` | GitHub 回调地址,默认 `http://localhost:5173/auth/github/callback` |
| `GITEE_OAUTH_CLIENT_ID` | Gitee 第三方应用 Client ID(在 https://gitee.com/oauth/applications 创建,权限勾 `user_info` + `projects`) |
| `GITEE_OAUTH_CLIENT_SECRET` | Gitee 第三方应用 Client Secret |
| `GITEE_OAUTH_REDIRECT_URI` | Gitee 回调地址,默认 `http://localhost:5173/auth/gitee/callback` |

> **OAuth 应用配置要点**
> - GitHub:Authorization callback URL 填后端 `GITHUB_OAUTH_REDIRECT_URI` 的值
> - Gitee:应用回调地址填后端 `GITEE_OAUTH_REDIRECT_URI` 的值,权限至少勾 `user_info`(登录)+ `projects`(仓库绑定)
> - 同一个回调地址同时承担「登录」和「绑定」两种场景,按当前登录态分流

#### LLM 配置(开发期默认 provider)

| 变量 | 说明 | 默认值 |
|---|---|---|
| `LLM_PROVIDER` | provider id,对应 `models_catalog.json` 中的 `llmProviders[].id` | `dashscope` |
| `LLM_API_KEY` | LLM API Key(从厂商控制台获取) | **必填** |
| `LLM_MODEL` | 模型 id | `qwen3.6-flash` |
| `LLM_ENABLE_THINKING` | 是否启用思考(混合思考模型可开关) | `true` |

> 生产环境/多用户场景下,LLM 配置由用户在「模型配置」页面自行管理,这几个环境变量仅作开发期兜底。

#### 仓库克隆

| 变量 | 说明 | 默认值 |
|---|---|---|
| `REPO_CLONE_DIR` | 本地 clone 临时目录(`SANDBOX_MODE=mock` 时使用) | `./_repos` |

#### 沙箱(OpenSandbox)

| 变量 | 说明 | 默认值 |
|---|---|---|
| `SANDBOX_MODE` | `mock`(本地文件系统模拟)/ `sandbox`(连真实 OpenSandbox Server) | `mock` |
| `SANDBOX_SERVER_URL` | OpenSandbox Server 地址,形如 `http://your-server:8080` | `http://localhost:8080` |
| `SANDBOX_API_KEY` | Server 鉴权 API Key(对应 server `[server].api_key`,留空不鉴权) | 空 |
| `SANDBOX_IMAGE` | 沙箱镜像(必须预装 git / ripgrep / python3 / awk / coreutils) | `ubuntu` |
| `SANDBOX_TIMEOUT_MINUTES` | 沙箱超时(分钟) | `30` |
| `SANDBOX_USE_SERVER_PROXY` | 是否走 Server 代理(跨机部署必须 `true`) | `true` |
| `SANDBOX_SSH_KEY_HOST_PATH` | Server 宿主机 SSH key 目录(只读挂载到沙箱供 SSH clone 用,绝对路径) | 空 |
| `SANDBOX_CPU` | 沙箱 CPU 限制(如 `2`) | 空 |
| `SANDBOX_MEMORY` | 沙箱内存限制(如 `4Gi`) | 空 |

> 沙箱镜像官方 `ubuntu` 不含 git 和 ripgrep,需按 [docs/opensandbox-deploy.md](docs/opensandbox-deploy.md) 构建自定义镜像,或用 `scripts/build-sandbox-image.sh`。

#### CLI 执行器(可选,按 `task.executor` 生效)

每个 CLI 执行器有两类配置:二进制名/路径 + 安装命令(沙箱内未检测到时自动安装)。

**Qoder CLI(国际版)** —— `task.executor=qoder_cli`

| 变量 | 默认值 |
|---|---|
| `QODER_CLI_BIN` | `qodercli` |
| `QODER_CLI_INSTALL_CMD` | `npm install -g @qoder-ai/qodercli` |

**Qoder CN CLI(国内版/原通义灵码)** —— `task.executor=qoder_cli_cn`

| 变量 | 默认值 |
|---|---|
| `QODER_CLI_CN_BIN` | `qoderclicn` |
| `QODER_CLI_CN_INSTALL_CMD` | `curl -fsSL https://qoder.cn/install \| bash` |

**Kimi Code CLI** —— `task.executor=kimi_cli`

| 变量 | 默认值 |
|---|---|
| `KIMI_CLI_BIN` | `kimi` |
| `KIMI_CLI_INSTALL_CMD` | `npm install -g @moonshot-ai/kimi-code` |

> Kimi Code 凭证由用户在「智能体配置」页面填写 `api_key` / `base_url` / `model`,后端经 `KIMI_MODEL_*` 环境变量族注入到 bridge 进程,无需 `config.toml`。默认 `KIMI_MODEL_NAME=kimi-for-coding`、`KIMI_MODEL_PROVIDER_TYPE=kimi`(由 registry 注入)。

**Hermes CLI** —— `task.executor=hermes_cli`

| 变量 | 默认值 |
|---|---|
| `HERMES_CLI_BIN` | `hermes` |
| `HERMES_CLI_INSTALL_CMD` | `pip install hermes-agent` |

> Hermes CLI 是纯 Python 包(不需要 Node.js)。凭证(`provider` / `api_key` / `base_url` / `model`)在「智能体配置」页面填写。后端通过 `credential_env_builder` 按 provider 动态映射 API Key 环境变量名(`OPENROUTER_API_KEY` / `ANTHROPIC_API_KEY` 等),通过 `pre_bridge_hook` 写入 `~/.hermes/config.yaml`(模型/provider/base_url)。权限绕过:`HERMES_YOLO_MODE=1` 自动注入(等价 `--yolo`)。支持 7 种供应商:OpenRouter / Anthropic / OpenAI / z.ai / Kimi / MiniMax / Gemini。

**Codex CLI** —— `task.executor=codex_cli`

| 变量 | 默认值 |
|---|---|
| `CODEX_CLI_BIN` | `codex` |
| `CODEX_CLI_INSTALL_CMD` | `npm install -g @openai/codex` |

> Codex CLI 是 OpenAI 官方编码 CLI(Apache-2.0 开源,需 Node.js >= 16)。与 Hermes/Kimi/Qoder 原生支持 ACP 不同,Codex 通过 `codex exec --json`(JSONL 事件流)经 `codex_bridge.py` 翻译为 ACP 协议。凭证(`api_key` / `base_url` / `model` / `wire_api`)在「智能体配置」页面填写。后端经 `CODEX_API_KEY` 环境变量注入 API Key(registry 静态 `credential_env` 映射),通过 `pre_bridge_hook` 写入 `~/.codex/config.toml`(模型/provider/base_url/wire_api + `approval_policy=full-auto` + `sandbox_mode=danger-full-access`)。默认值:模型 `gpt-5`,wire_api `responses`(Responses API)。支持自定义 OpenAI 兼容端点(vLLM / Ollama 等,建议 `wire_api=chat`)。

### 前端环境变量(`frontend/.env`)

前端只存 Client ID(不含 secret),用于拼接 OAuth 授权链接。

| 变量 | 说明 |
|---|---|
| `VITE_GITHUB_OAUTH_CLIENT_ID` | GitHub OAuth Client ID |
| `VITE_GITHUB_OAUTH_REDIRECT_URI` | GitHub 回调地址(需与后端 `GITHUB_OAUTH_REDIRECT_URI` 一致) |
| `VITE_GITEE_OAUTH_CLIENT_ID` | Gitee OAuth Client ID |
| `VITE_GITEE_OAUTH_REDIRECT_URI` | Gitee 回调地址(需与后端 `GITEE_OAUTH_REDIRECT_URI` 一致) |

---

## 沙箱部署

生产环境需要部署 OpenSandbox Server 以提供隔离的代码执行环境。完整部署步骤见 [docs/opensandbox-deploy.md](docs/opensandbox-deploy.md),关键点:

1. 在 Linux 服务器上部署 OpenSandbox Server,监听 `0.0.0.0:8080`
2. 显式配置 `[runtime].execd_image`,并在 `[storage].allowed_host_paths` 放行挂载路径前缀
3. 构建自定义镜像(预装 git / ripgrep / python3 / Node.js 等),避免每次任务重复安装
4. 后端设置 `SANDBOX_MODE=sandbox`、`SANDBOX_SERVER_URL`、`SANDBOX_USE_SERVER_PROXY=true`

未部署沙箱时,可设 `SANDBOX_MODE=mock` 用本地文件系统模拟(仅用于开发调试,工具调用在本机执行)。

## 构建与部署

### 前端构建

```bash
cd frontend
npm run build      # vue-tsc 类型检查 + vite 构建,产物在 dist/
npm run type-check # 仅类型检查
```

### 后端运行

```bash
cd backend
uvicorn app.main:app --host 0.0.0.0 --port 8000
```

生产环境建议用 `gunicorn -k uvicorn.workers.UvicornWorker`(Linux)或容器化部署。

## 文档

- [规格说明](docs/spec.md) —— 完整产品规格与架构设计
- [开发路线图](docs/Roadmap.md) —— 阶段规划与进度
- [沙箱部署指南](docs/opensandbox-deploy.md) —— OpenSandbox Server 部署与镜像构建

## 开发说明

- **数据库 schema 变更**:临时设 `DB_REBUILD_ON_START=true` 重启重建表(会清空数据),改完记得改回 `false`。生产环境应使用 Alembic 迁移。
- **新增场景模板**:在 `backend/app/scenarios/` 注册,提供 `preset_prompt` 和 `recommended_skills` 即可。
- **新增技能**:在 `backend/skills/<category>/<name>/` 下放 `SKILL.md`,启动时自动扫描加载。
- **新增 Git 平台**:在 `git_provider.py` 的 `PROVIDERS` 注册表添加实现类即可,前后端路由/UI 已参数化。
- **新增 CLI 执行器**:在 `backend/app/agents/` 的 registry 注册 agent_type,实现 ACP 协议通信。

## License

[MIT License](./LICENSE) © 2026 shShwsq
