# AgentPair 开发 Roadmap

> 适合 vibe coding 的迭代路线:每个阶段都是一个**能独立跑通、能立即看到效果**的闭环。做完一个阶段先玩一下,有手感了再进下一个。

---

## 当前总览

| 阶段 | 名称 | 状态 |
|------|------|------|
| 0 | 最小骨架跑通 | ✅ 已完成 |
| 1 | 单 react_agent 跑通真实审计 | ✅ 已完成 |
| 2 | 加沙箱,代码在沙箱里跑 | ✅ 已完成 |
| 3 | 加 Semgrep 和 CVE 查询 | ✅ 已完成 |
| 4 | 引入 user_agent,双智能体协作 | ✅ 已完成 |
| 5 | SKILL 机制 | ✅ 已完成 |
| 6 | 用户系统与模型配置 | ✅ 已完成 |
| 7 | 网站前端 | ✅ 已完成 |
| 8 | 微信小程序 | ⬜ 未开始 |
| 9 | 生产化与上线 | 🔨 部分完成 |

### 超出原规划已实现的功能

以下功能在迭代过程中自发产生,已落地到代码中:

- **执行器抽象层(ExecutorAgent)**:把"执行智能体"抽象为统一接口,支持 builtin(内置 react_agent)和外部 CLI agent(通过 ACP 协议通信)两种 provider。新增 agent 类型只需在 registry 注册,无需改核心代码。
- **ACP Bridge**:HTTP ↔ stdio 桥接服务,运行在沙箱内,让外部 CLI agent(如 Qoder CLI / Qoder CN CLI / Kimi Code CLI)通过 ACP 协议接入。
- **Qoder CLI Agent**:通过 ACP 协议调用 Qoder CLI(国际版 + 国内版)作为 react 角色,模型由 CLI 账号配额管理,不走后端 LLM 配置。
- **Kimi Code CLI Agent**:通过 ACP 协议调用开源 Kimi Code CLI 作为 react 角色,模型经 `KIMI_MODEL_*` 环境变量注入(支持自部署 LLM 端点),凭证由用户在智能体配置页填写。
- **场景降级**:checklist 不再从场景固定读取,改为 user_agent 第 0 轮动态生成 + 用户编辑确认。prompt 通用化,工具全部开放,结果结构通用化。
- **任务暂停/恢复**:用户可暂停运行中的任务,后台线程在检查点(迭代边界/工具调用前)阻塞,恢复后继续。
- **用户补充消息**:任务运行中/完成后,用户可追加消息触发新一轮协作(resume_audit_with_message)。
- **跨轮记忆传递**:三级压缩策略(完整 → 丢工具摘要 → LLM 压缩早期轮次),控制 token 成本。
- **循环检测**:滑动窗口检测重复工具调用(连续相同 + 交替循环),打破死循环。
- **Plan 状态管理**:react_agent 在思考中输出 `<plan>` 清单,代码维护状态,跨轮续接避免重复规划。
- **工作区浏览**:前端可浏览 react_agent clone 的工作区文件结构和内容(任务完成后保留 1 小时)。
- **Git 平台抽象层(GitHub / Gitee)**:统一 `GitProvider` 抽象层,支持 GitHub + Gitee 双平台 OAuth 登录与仓库绑定。同一用户可同时绑定两平台,clone 时按仓库 URL 主机自动选用对应 token。取代旧的 `User.github_id` 字段,改为 `UserGitBinding` 表(含一次性数据迁移)。
- **思考链流式推送**:LLM 的 reasoning_content 通过 SSE thinking_delta 事件实时推给前端(打字机效果)。
- **删除账号**:硬删除 + 级联清理(任务/配置/token)。
- **多厂商 LLM 支持**:DashScope(通义千问)/ DeepSeek / 智谱 / Kimi / 豆包 / MiniMax 等,通过 models_catalog.json 统一管理差异。
- **代码审查场景模板**:除安全审计外,新增 code_review 场景(预设提示词 + 推荐 skill)。

---

## 阶段 0:最小骨架跑通 ✅

**目标**:后端能起,数据库能连,一个空的 agent 接口能返回假数据。

**做什么**:
- FastAPI 项目骨架 + 路由分层
- SQLite + SQLAlchemy 模型(User / Task / Conversation / Result)
- 一个 `/tasks` POST 接口,接受 repo_url,返回 task_id
- 一个 `/tasks/{id}` GET 接口,返回 task 状态
- 一个假的 agent runner,直接返回"发现 1 个 SQL 注入"这种硬编码结果

**验证方式**:
```bash
uvicorn app.main:app --reload
# curl 提交任务,看 task_id
# curl 查状态,看到假结果
```

**完成标志**:curl 能跑通提交 → 查询 → 拿到假结果的完整链路。

---

## 阶段 1:单 react_agent 跑通真实审计 ✅

**目标**:不要双 agent,先让一个 react_agent 真的能审计一个 GitHub 仓库。

**做什么**:
- 接入 LLM(OpenAI 兼容接口,先写死一个模型)
- react_agent 基础循环:Thought → Action → Observation
- 工具实现(最小集):
  - `clone_repo`:本地 git clone
  - `read_file`:读仓库文件
  - `search_code`:grep 搜索
- agent 跑完把发现写入 Finding 表

**验证方式**:
- 找一个故意有漏洞的小仓库(自己造一个,放几个 SQL 注入和硬编码密钥)
- 提交任务,等 agent 跑完,查结果
- 看到 react_agent 真的找到漏洞

**完成标志**:react_agent 能在一个真实小仓库上跑完,找到预设的漏洞。

---

## 阶段 2:加沙箱,代码在沙箱里跑 ✅

**目标**:把 clone 和工具执行从宿主机挪进 OpenSandbox,真正的隔离。

**做什么**:
- OpenSandbox 接入(创建沙箱、文件推送、命令执行)
- `clone_repo` 改为在沙箱里执行
- 新增 `run_in_sandbox`:agent 可以生成脚本扔进去跑
- react_agent 能用 `run_in_sandbox` 验证漏洞(比如构造 payload 测注入点)

**验证方式**:
- 提交一个仓库,看 agent 是否在沙箱里 clone + 跑脚本
- 试着让 agent 生成一个验证脚本(比如 `python -c "..."` 测 SQL 注入)
- 确认宿主机没被污染

**完成标志**:所有代码执行都在沙箱里,宿主机只做调度。

**实际实现**:
- `SandboxSession` 封装,支持 sandbox(真实 OpenSandbox)和 local(本地模式,不用沙箱)两种模式
- 使用 OpenSandbox 官方 `SandboxSync` 同步 API,对齐 react_agent 的同步循环
- 支持 SSH key 挂载(私有仓库 clone)、资源限制(CPU/内存/执行时间)、端口转发
- 后台命令管理(ACP bridge 等长驻服务)

---

## 阶段 3:加 Semgrep 和 CVE 查询 ✅

**目标**:工具集补全到 spec 里的完整版。

**做什么**:
- `semgrep_scan`:沙箱里预装 Semgrep,agent 调用跑扫描,结果解析回 Finding
- `cve_query`:解析依赖文件(requirements.txt / package.json / go.mod),查 OSV API
- 工具结果格式化:让 LLM 能读懂 Semgrep 和 CVE 的输出

**验证方式**:
- 找一个有已知漏洞依赖的仓库(比如故意 pin 一个老版本 `requests==2.20.0`),看 CVE 工具能不能查出来
- 找一个有典型漏洞模式的代码,看 Semgrep 能扫出来
- agent 能把 Semgrep / CVE 结果纳入最终报告

**完成标志**:react_agent 工具集达到 spec 8.4 的完整版。

**实际实现**:
- `run_semgrep` 工具:在沙箱中执行 Semgrep,支持自定义规则
- `query_cve` 工具:按依赖逐个查 OSV API
- 额外工具:`list_files`(目录结构)、`find_files`(glob 查找)、`write_file`(工作区写文件)、`run_python_code`(沙箱执行 Python)
- `list_skills` / `skill`:查看并加载专家技能

---

## 阶段 4:引入 user_agent,双智能体协作 ✅

**目标**:把 user_agent 加进来,跑通真正的双 agent 闭环。**这是核心创新,前面所有阶段都是为它做准备。**

**做什么**:
- user_agent 模块:输入用户意图 + react_agent 结果,输出评估 + 追问
- 漏洞类别 checklist 落地成结构化数据(不是 prompt 里的自然语言)
- 协作流程编排:
  1. user_agent 意图对齐(可选提问)
  2. react_agent 首轮扫描
  3. user_agent 对照 checklist 评估
  4. user_agent 生成定向追问 → react_agent 续查
  5. 终止条件判定
- 对话记录全部写入 Conversation 表

**验证方式**:
- 提交一个复杂点的仓库(故意留多个类别的漏洞)
- 观察 Conversation 表,看到 user_agent 真的在追问
- 对比:同一个仓库,单 react_agent(阶段 3)vs 双 agent,看双 agent 是否发现更多
- 终止条件触发后,checklist 所有类别都有结论

**完成标志**:user_agent 真的会追问,且追问后 react_agent 能补出新发现。

**实际实现(超出原规划)**:
- **场景降级**:checklist 不再固定,由 user_agent 第 0 轮动态生成 + 用户编辑确认,后续轮按此评估
- **用户澄清(ask_user)**:第 0 轮初始评估时,user_agent 可向用户提问(最多 2 轮),前端弹窗交互
- **跨轮记忆传递**:react_agent 和 user_agent 各自的三级压缩记忆策略
- **Plan 状态管理**:跨轮 plan 续接,避免重复规划已完成项
- **循环检测**:滑动窗口检测连续相同调用 + 交替循环,自动打破
- **任务暂停/恢复**:用户可暂停运行中的任务,后台线程在检查点阻塞
- **用户补充消息**:运行中注入 LLM 上下文 / 完成后触发新一轮协作
- **思考链流式推送**:reasoning_content 通过 SSE 实时推给前端

---

## 阶段 5:SKILL 机制 ✅

**目标**:把常用的多步审计操作封装成可复用技能,让 react_agent 调用。

**做什么**:
- SKILL 定义格式(YAML)与加载器
- SKILL 执行引擎:按 steps 顺序调底层工具,支持 iterate_over / condition
- 首版 7 个技能(check_sql_injection / check_auth_idor / check_deserialization / check_ssrf / check_hardcoded_secrets / check_path_traversal / check_crypto_misuse)
- react_agent 的工具列表里加入 `skill` 工具
- 管理员后台增删技能的接口

**验证方式**:
- 提交仓库,看 react_agent 是否主动调用 skill
- 故意破坏某个 skill 的 steps,看是否报错清晰
- 加一个新 skill(只改 YAML 不改代码),看是否能被加载

**完成标志**:技能可配置、可扩展,react_agent 能按需调用。

**实际实现(与原设计差异)**:
- SKILL 定义格式从 YAML 改为 **SKILL.md**(Markdown frontmatter + body),更接近主流 AI 工具的技能格式
- `list_skills` / `skill` 工具:react_agent 查看可用技能列表并按需加载技能指令
- 管理后台 CRUD API(`/skills`):创建/查看/更新/删除/重载技能
- **场景降级后**:skill 不再与场景绑定,而是全局可用。用户创建任务时可选择允许调用的 skill(`allowed_skills`)

---

## 阶段 6:用户系统与模型配置 ✅

**目标**:从单用户 demo 变成多用户可用。

**做什么**:
- 注册 / 登录 / 邮箱验证 / 重置密码(注意:禁止修改密码)
- JWT 鉴权,任务按 user_id 隔离
- 管理员后台:模型清单 CRUD + 默认模型配置
- 用户设置:选 user_agent / react_agent 各自的模型
- 任务关联用户的模型选择

**验证方式**:
- 两个用户注册登录,各自的任务互不可见
- 用户 A 配置用 GPT-4,用户 B 配置用 DeepSeek,各自任务走对应模型
- 忘记密码流程跑通

**完成标志**:多用户隔离 + 模型可配。

**实际实现(超出原规划)**:
- 邮箱密码注册/登录 + 邮箱验证 + 重置密码 + **修改密码**(spec 原设计禁止修改密码,实际实现为允许,见 spec 8.5 更新说明)
- Git 平台 OAuth 登录(自动注册/关联已有账号):GitHub + Gitee 双平台,统一 `GitProvider` 抽象层
- Git 平台绑定:用于私有仓库 clone(repo / projects scope),同一用户可同时绑定 GitHub + Gitee
- 删除账号(硬删除 + 级联清理,含 UserGitBinding)
- 用户 LLM 配置(UserLLMConfig):列表式配置,每个配置含 provider/api_key/model/enable_thinking/base_url
- 用户 Agent 配置(UserAgentConfig):存储外部 CLI agent 的凭证(如 Qoder CLI PAT / Kimi Code API Key)
- 任务级模型选择:`llm_config_id`(user_agent 用)+ `react_llm_config_id`(react_agent 用,空时回退)
- 任务级执行器选择:`executor` 字段(builtin / qoder_cli / qoder_cli_cn / kimi_cli)

---

## 阶段 7:网站前端 ✅

**目标**:Vue3 + TS 网站,能完整用图形界面跑双智能体任务。前端与场景解耦,同一套 UI 支持任意场景,不绑定安全审查。

**做什么**:
- 登录 / 注册 / 设置页(模型配置)
- 提交任务页:按场景声明动态渲染表单字段(场景自带字段定义,不写死 repo_url/branch)
- 任务列表侧栏(状态、当前阶段)
- 任务详情页:
  - 实时对话流(user_agent / react_agent 的来回)
  - 结果清单:分组维度由场景声明(安全场景才用 severity),点击跳转源码位置
  - 覆盖度看板:评估维度由场景声明(安全场景才用 checklist),不写死
- 报告导出(Markdown / PDF)

**验证方式**:
- 从注册到跑完一个任务,全程不用碰命令行
- 任务运行中能看到 agent 在做什么
- 同一套界面换一个场景,结果清单和看板也能正常展示
- 报告能导出

**完成标志**:网站端完整可用,能替代 curl 流程,且不耦合具体场景。

**实际实现(超出原规划)**:
- Vue3 + TypeScript + Vue Router + Pinia
- 完整页面:LoginView / VerifyEmailView / ResetPasswordView / OAuthCallbackView / HomeView(任务列表)/ TaskCreateView / TaskDetailView / ModelSettingsView / SettingsView
- 实时对话流:ConversationMessage 组件,支持 thinking / tool_call / tool_result / evaluation / question / summary 等消息类型
- 流式思考链:thinking_delta SSE 事件,打字机效果实时展示 reasoning + content
- 覆盖度看板:ChecklistReviewDialog(第 0 轮用户编辑确认清单)+ 任务详情页覆盖度展示
- 工作区侧栏:WorkspaceSidebar 组件,浏览 clone 的文件结构和内容
- 暂停/恢复按钮 + 用户消息输入框(UserMessageInput)
- 报告导出:Markdown 下载 + HTML 打印为 PDF
- 模型配置:ModelConfigDialog + ModelCombobox + AgentConfigDialog
- Git 平台绑定:GitProviderDialog(统一 GitHub / Gitee,按 provider 动态渲染标题/品牌色/scope 文案)
- 多平台仓库选择:TaskCreateView 并行加载已绑定平台仓库,合并到带 `[GitHub]`/`[Gitee]` 标记的统一下拉
- 登录页:GitHub + Gitee 双 OAuth 登录按钮
- 全文搜索:任务列表支持按标题/输入/对话内容/结果内容搜索

---

## 阶段 8:微信小程序 ⬜

**目标**:小程序端,核心功能可用。延续阶段 7 的场景无关方向,小程序 UI 同样不绑定安全审查。

**做什么**:
- 微信登录(复用网站账户体系)
- 提交任务:按场景声明动态渲染表单(不写死 repo_url)
- 任务列表 + 状态轮询(5-10 秒)
- 任务详情:精简版结果卡片(分组维度由场景声明)
- 设置页:模型配置
- 跳转网页版看完整报告

**验证方式**:
- 手机上提交任务,过几分钟回来看结果
- running 状态下能看到当前阶段
- 结果卡片能看清重点
- 换一个场景,小程序表单和结果卡片也能正常展示

**完成标志**:小程序端能完整走一遍提交 → 等待 → 查看结果,且不耦合具体场景。

---

## 阶段 9:生产化与上线 🔨

**目标**:能真上线给别人用。

**做什么**:
- Postgres 替换 SQLite
- 任务队列(Celery / RQ),任务异步执行
- 部署:Docker Compose(后端 + 数据库 + 沙箱)
- 资源限制(单任务 token / 时间上限)
- 监控与日志
- 微信内容安全检测(避免报告被拦截)
- 域名 + HTTPS + 备案

**验证方式**:
- 线上环境部署完成,真实用户能注册使用
- 压测:并发任务不崩
- 跑一个真实开源项目,看完整任务效果(结果质量因场景而异,不预设安全审查)

**完成标志**:线上可访问,真实仓库任务可用。

**当前进度**:
- ✅ 数据库已支持 PostgreSQL(SQLAlchemy + PostgreSQL 方言,JSONB/UUID)
- ✅ 任务异步执行(后台线程 threading,暂未用 Celery/RQ)
- ✅ 沙箱部署脚本(scripts/build-sandbox-image.sh)
- ✅ 资源限制(沙箱 CPU/内存/执行时间限制;react_agent MAX_ITERATIONS + 循环检测)
- ⬜ 任务队列(Celery/RQ):当前用 threading,生产环境需切换
- ⬜ 监控与日志系统
- ⬜ 微信内容安全检测
- ⬜ 域名 + HTTPS + 备案

---

## vibe coding 的几个原则

1. **每个阶段做完先玩**。别急着进下一阶段,先用真实仓库跑几次,发现问题再改。
2. **阶段 4 是关键转折**。前面都是铺垫,阶段 4 才是核心创新落地。如果阶段 4 做出来效果不好(双 agent 没比单 agent 强),停下来想原因,不要硬往后做。
3. **允许回退**。某个阶段发现前面设计有问题,回去改前面,不要将就。
4. **每个阶段都要能 demo**。不能 demo 的阶段就是没做完。
5. **工具优先于 prompt**。react_agent 找不到漏洞,先想是不是工具不够用,而不是先调 prompt。

---

## 不在路线图里但要做的事(穿插进行)

- 安全:输入校验、SSRF 防护(repo_url 不能是内网地址)、rate limit
- 成本:每个任务的 token 消耗统计与展示
- 错误处理:LLM 调用失败、沙箱超时、仓库不存在等
- 可观测性:每个 agent 的执行日志、工具调用耗时
