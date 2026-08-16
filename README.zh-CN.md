# AgentPair

> 双智能体协作的代码分析平台

[English](./README.md) · **简体中文**

AgentPair 的核心创新是 **user_agent 模拟用户追问** 的交互模式 —— 在单 ReAct 架构之上叠加意图对齐与结果审视能力。`user_agent` 负责动态生成覆盖度清单、对照清单审视执行结果、并在覆盖不足时构造定向追问;`react_agent`(或外部 CLI 执行器)负责实际执行代码分析。两者多轮协作,直到覆盖度满足才输出结构化结果。

## 核心特性

- **双智能体协作**:`user_agent`(意图对齐 + 结果审视)+ `react_agent`(执行)多轮迭代,自动补齐覆盖盲区
- **检查点评估**:react_agent 执行过程中,user_agent 在迭代边界做轻量方向纠偏,发现跑偏时软中断拉回
- **执行器抽象层**:内置 react_agent / Qoder CLI / Kimi Code CLI / Hermes CLI / Codex CLI 等可插拔执行器,通过 ACP 协议统一通信
- **场景模板化**:安全审计、代码审查等场景作为快捷模板(预设提示词 + 推荐 skill),checklist 由 LLM 动态生成 + 用户编辑确认
- **沙箱隔离**:基于 [OpenSandbox](https://github.com/opensandbox/opensandbox) 的容器化执行,工具调用在隔离环境完成
- **多 Git 平台**:统一抽象层支持 GitHub / Gitee,OAuth 登录 + 私有仓库绑定 + 自动克隆(Gitee 令牌经 refresh_token 自动续期)
- **练习题生成与自适应练习**:把任务真实发现经 LLM 改编为客观题(网络安全 / 架构设计 / 通用代码能力三主题),SM-2 遗忘曲线排期,结合薄弱点强化与难度匹配即时组卷
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
│   │   ├── agents/           # 智能体(user_agent / react_agent / orchestrator / verifier / CLI wrapper)
│   │   ├── llm/              # LLM 客户端封装
│   │   ├── models/           # SQLAlchemy 数据模型(含 practice / agent_policy / task_artifact)
│   │   ├── routers/          # API 路由(auth / tasks / git_provider / practice / ...)
│   │   ├── sandbox/          # OpenSandbox 客户端封装
│   │   ├── scenarios/        # 场景模板(安全审计 / 代码审查 / 通用)
│   │   ├── schemas/          # Pydantic 请求/响应模型
│   │   ├── services/         # 练习引擎(SM-2 / selector / generator)+ 记忆 / 工作区 diff
│   │   ├── skills/           # 技能加载器 + skill 注册表
│   │   ├── tools/            # ReAct 工具(clone_repo / search_code / run_lint / ...)
│   │   ├── agent_checkpoint.py # 检查点评估(迭代边界方向纠偏)
│   │   ├── config.py         # 环境变量配置(pydantic-settings)
│   │   ├── git_provider.py   # Git 平台抽象层(GitHub / Gitee)
│   │   └── main.py           # FastAPI 入口
│   ├── skills/               # SKILL.md 技能定义文件
│   ├── requirements.txt
│   └── .env.example
├── frontend/                 # Vue 3 前端
│   ├── src/
│   │   ├── api/              # API 客户端(含 practice / practiceStream)
│   │   ├── components/       # 可复用组件
│   │   ├── composables/      # Composables(主题 / 引导 / 功能开关)
│   │   ├── stores/           # Pinia 状态管理
│   │   ├── views/            # 页面视图(含 PracticeView)
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
- **OpenSandbox Server**(可选,未部署时可设 `SANDBOX_MODE=local` 用本地模式(不用沙箱))

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
| `PRACTICE_ENABLED` | 出题 & 练习功能总开关(`false` = `/practice/*` 路由不注册、任务完成不自动出题;已建表与题库数据保留) | `true` |

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

**Gitee 权限勾选说明**(创建应用页 https://gitee.com/oauth/applications,过高权限用户可能拒绝授权,只勾实际需要的):

| 权限 | 是否勾选 | 用途 |
|---|:---:|---|
| `user_info` | ✅ 必勾 | 登录:获取用户 ID / 用户名 / 头像 / 昵称(对应 `scope_login`) |
| `projects` | ✅ 必勾 | 仓库绑定:列出用户私有仓库 + HTTPS 克隆鉴权(对应 `scope_bind`) |
| `emails` | ❌ 不勾 | Gitee 无可验证邮箱端点,登录用 `user_info` 返回的 email 字段即可 |
| `pull_requests` / `issues` / `notes` | ❌ 不勾 | 本系统只读取/克隆代码,不操作 PR / Issue / 评论 |
| `keys` / `hook` / `groups` / `gists` / `enterprises` | ❌ 不勾 | 未使用 |

> 权限与代码中 scope 定义一一对应(`user_info` = `scope_login`,`user_info projects` = `scope_bind`),见 [backend/app/git_provider.py](backend/app/git_provider.py) 的 `GiteeProvider`。

#### LLM 配置(开发期默认 provider)

| 变量 | 说明 | 默认值 |
|---|---|---|
| `LLM_PROVIDER` | provider id,对应 `models_catalog.json` 中的 `llmProviders[].id` | `dashscope` |
| `LLM_API_KEY` | LLM API Key(从厂商控制台获取) | **必填** |
| `LLM_MODEL` | 模型 id | `qwen3.6-flash` |
| `LLM_ENABLE_THINKING` | 是否启用思考(混合思考模型可开关) | `true` |
| `LLM_RATE_LIMIT_MAX_RETRIES` | 429 限流退避重试次数(指数退避 + 抖动,0=不重试) | `3` |

> 生产环境/多用户场景下,LLM 配置由用户在「模型配置」页面自行管理,这几个环境变量仅作开发期兜底。

#### 仓库克隆

| 变量 | 说明 | 默认值 |
|---|---|---|
| `REPO_CLONE_DIR` | 本地 clone 临时目录(`SANDBOX_MODE=local` 时使用) | `./_repos` |
| `REPO_CLONE_DEPTH` | 克隆深度:`0`=完整克隆(默认,保留 git 历史供 log/blame 追溯);`>0`=浅克隆 `--depth N`(超大仓库可加速) | `0` |
| `REPO_CLONE_TIMEOUT` | 克隆超时(秒),完整克隆比浅克隆慢,超大仓库可调大 | `600` |

#### 沙箱(OpenSandbox)

| 变量 | 说明 | 默认值 |
|---|---|---|
| `SANDBOX_MODE` | `local`(本地模式,不用沙箱)/ `sandbox`(连真实 OpenSandbox Server) | `local` |
| `SANDBOX_SERVER_URL` | OpenSandbox Server 地址,形如 `http://your-server:8080` | `http://localhost:8080` |
| `SANDBOX_API_KEY` | Server 鉴权 API Key(对应 server `[server].api_key`,留空不鉴权) | 空 |
| `SANDBOX_IMAGE` | 沙箱镜像(必须预装 git / ripgrep / python3 / awk / coreutils) | `ubuntu` |
| `SANDBOX_TIMEOUT_MINUTES` | 沙箱超时(分钟) | `30` |
| `SANDBOX_RENEW_INTERVAL_MINUTES` | 会话续期间隔(分钟):访问时距上次续期超过此值就 renew TTL,防长任务被 Server 回收 | `5` |
| `SANDBOX_USE_SERVER_PROXY` | 是否走 Server 代理(跨机部署必须 `true`) | `true` |
| `SANDBOX_SSH_KEY_HOST_PATH` | Server 宿主机 SSH key 目录(只读挂载到沙箱供 SSH clone 用,绝对路径) | 空 |
| `SANDBOX_CPU` | 沙箱 CPU 限制(如 `2`) | 空 |
| `SANDBOX_MEMORY` | 沙箱内存限制(如 `4Gi`) | 空 |

> 沙箱镜像官方 `ubuntu` 不含 git 和 ripgrep,需按 [docs/opensandbox-deploy.md](docs/opensandbox-deploy.md) 构建自定义镜像,或用 `scripts/build-sandbox-image.sh`。

#### CLI 执行器(可选,按 `task.executor` 生效)

每个 CLI 执行器有两类配置:二进制名/路径 + 安装命令(沙箱内未检测到时自动安装)。

**CLI 挂死兜底**(对所有 CLI 执行器生效):

| 变量 | 说明 | 默认值 |
|---|---|---|
| `ACP_IDLE_TIMEOUT_OUTPUT_SECONDS` | 无活动工具(等模型输出)时的 idle 超时,超时则 cancel + 用已累积输出收尾本轮(`0`=关闭) | `300` |
| `ACP_IDLE_TIMEOUT_TOOL_SECONDS` | 有工具在跑(克隆/构建等长命令本就无输出)时的最后防线超时,防 CLI 中途崩溃没发 completed(`0`=关闭) | `1800` |

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
| `HERMES_CLI_INSTALL_CMD` | 官方 `install.sh`(见 [文档](docs/opensandbox-deploy.md) §2.5)|

> Hermes CLI 是 Python 包,**未发布到 PyPI**,用[官方安装脚本](https://github.com/NousResearch/hermes-agent#quick-install)安装(`curl -fsSL https://hermes-agent.nousresearch.com/install.sh | bash`,自动装 uv + Python 3.11 源码,不需要 Node.js);`pip install hermes-agent` 会失败。凭证(`provider` / `api_key` / `base_url` / `model`)在「智能体配置」页面填写。后端通过 `credential_env_builder` 按 provider 动态映射 API Key 环境变量名(`OPENROUTER_API_KEY` / `ANTHROPIC_API_KEY` 等),通过 `pre_bridge_hook` 写入 `~/.hermes/config.yaml`(模型/provider/base_url)。权限绕过:`HERMES_YOLO_MODE=1` 自动注入(等价 `--yolo`)。支持 7 种供应商:OpenRouter / Anthropic / OpenAI / z.ai / Kimi / MiniMax / Gemini。

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

未部署沙箱时,可设 `SANDBOX_MODE=local` 用本地模式(不用沙箱)(仅用于开发调试,工具调用在本机执行)。

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

> **注意**:后端存在进程内存态(出题 job、任务 SSE 事件流),**不要启用多 worker**;gunicorn 时用 `--workers 1`,容器部署见下方 Docker 方案。

### Docker 一键部署(推荐)

生产环境推荐 `deploy/` 下的 Docker Compose 方案:2 个容器(`backend` + `frontend/nginx`),外部依赖(阿里云 RDS PostgreSQL、另一台服务器的 OpenSandbox)不进编排,经环境变量直连。

```bash
# 在 Linux 服务器上(需预装 Docker + Compose 插件)
git clone <repo-url> AgentPair && cd AgentPair/deploy
cp .env.production.example .env.production   # 填写数据库/密钥/沙箱地址/OAuth 等
bash deploy.sh                               # 一键构建 + 启动
```

要点:

- **后端强制单 worker**(uvicorn `--workers 1`):出题 job 与任务 SSE 事件流是进程内存态,多 worker 会导致事件流错连、job 丢失
- **nginx 关闭 `proxy_buffering`**:任务流/出题流是 SSE 实时推送,缓冲会卡住前端
- **`/api` 前缀反代去前缀**:前端 baseURL 为 `/api`,nginx 反代到后端时自动去掉
- **持久化卷**:`backend/logs`(practice_generate.log / perf.log / acp)、`user_skills`(用户上传 skill)、`_repos`
- 修改 OAuth 回调等 `VITE_*` 变量后需重新执行 `bash deploy.sh`(构建期注入)

各配置项逐条注释见 [deploy/.env.production.example](deploy/.env.production.example)。

## 文档

- [规格说明](docs/spec.md) —— 完整产品规格与架构设计
- [开发路线图](docs/Roadmap.md) —— 阶段规划与进度
- [沙箱部署指南](docs/opensandbox-deploy.md) —— OpenSandbox Server 部署与镜像构建
- [智能体架构](docs/agent-architecture.md) —— user_agent / react_agent / CLI 智能体内幕、上下文传递与检查点评估
- [任务详情页结构](docs/task-detail-view-structure.md) —— TaskDetailView 布局与渲染管线

## 开发说明

- **数据库 schema 变更**:临时设 `DB_REBUILD_ON_START=true` 重启重建表(会清空数据),改完记得改回 `false`。生产环境应使用 Alembic 迁移。
- **新增场景模板**:在 `backend/app/scenarios/` 注册,提供 `preset_prompt` 和 `recommended_skills` 即可。
- **新增技能**:在 `backend/skills/<category>/<name>/` 下放 `SKILL.md`,启动时自动扫描加载。
- **新增 Git 平台**:在 `git_provider.py` 的 `PROVIDERS` 注册表添加实现类即可,前后端路由/UI 已参数化。
- **新增 CLI 执行器**:在 `backend/app/agents/` 的 registry 注册 agent_type,实现 ACP 协议通信。
- **练习功能**:`PRACTICE_ENABLED=false` 会关闭 `/practice/*` 路由与自动出题;出题日志落在 `backend/logs/practice_generate.log`。

## License

[MIT License](./LICENSE) © 2026 shShwsq
